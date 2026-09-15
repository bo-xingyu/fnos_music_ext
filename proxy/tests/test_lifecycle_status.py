"""应用「启用状态」的语义（v2.9.26）：status 的 exit 0/3 到底该回答什么。

飞牛的约定很直白（`cmd/main status`）：exit 0 = 正在运行，exit 3 = 未运行。
应用中心据此显示「已启用 / 未启用」——**而这个状态不会因为应用自己恢复了就自动
翻回来**，用户必须手动再点一次「启用」。

于是「status 该回答什么」就变得要命。以前我们回答的是「这一刻 socket 有没有接管
成功」，官方 trim-music 一重启把接管抢走，我们就报 exit 3——可代理进程毫发无损、
看门狗几十秒内就自己恢复了。**等于一边用看门狗自愈，一边告诉系统自己没在运行，
两个机制打架**，用户看到的就是「明明一切正常，却显示未启用，还得我手动点回来」。

正确语义是「这个应用**是否仍处于启用状态**」：进程还在、且有看门狗在管 = 仍然启用，
只是正在自愈。这组测试锁的就是这个区分——一旦退回「看接管」，用户又得手动点启用。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PAYLOAD_BIN = REPO / "fpk" / "payload" / "bin"

# 追加到 fnmusic-lib.sh 末尾：后定义的函数/变量覆盖真实实现，把外部世界换成
# 完全由环境变量说了算的假象，这样退出码只反映 status.sh 自己的判定逻辑。
OVERRIDE = textwrap.dedent(
    """
    # ---- 测试替身（追加在末尾，覆盖上面的真实实现）----
    PKGVAR="${TEST_PKGVAR}"
    PROXY_PID="${TEST_PKGVAR}/proxy.pid"
    WATCHDOG_PID="${TEST_PKGVAR}/watchdog.pid"
    UI_PID="${TEST_PKGVAR}/ui.pid"
    MUSICBOX_PID="${TEST_PKGVAR}/musicbox.pid"
    MUSICBOX_URL="http://127.0.0.1:1"
    TARGET_SOCK="${TEST_PKGVAR}/trim_music.socket"
    RESTART_MARKER="${TEST_PKGVAR}/restart.inprogress"
    TAKEOVER_LOST_MARKER="${TEST_PKGVAR}/takeover.lost"
    lib_pid_alive() { [ -f "$1" ]; }
    lib_probe_proxy() { [ "${TEST_PROBE_OK:-0}" = "1" ]; }
    lib_restart_in_progress() { return 1; }
    """
)


@pytest.fixture
def app(tmp_path):
    """搭一个最小可运行的 bin/ 目录：真脚本 + 测试替身。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("fnmusic-lib.sh", "status.sh"):
        shutil.copy(PAYLOAD_BIN / name, bindir / name)
    with (bindir / "fnmusic-lib.sh").open("a", encoding="utf-8") as fh:
        fh.write(OVERRIDE)

    pkgvar = tmp_path / "var"
    pkgvar.mkdir()

    class Ctl:
        def __init__(self) -> None:
            self.pkgvar = pkgvar

        def mark(self, name: str) -> None:
            (pkgvar / name).write_text("1234\n", encoding="utf-8")

        def unmark(self, name: str) -> None:
            (pkgvar / name).unlink(missing_ok=True)

        def run(self, probe_ok: bool, grace: int | None = None) -> subprocess.CompletedProcess:
            env = dict(os.environ)
            env["TEST_PKGVAR"] = str(pkgvar)
            env["TEST_PROBE_OK"] = "1" if probe_ok else "0"
            if grace is not None:
                env["FNMUSICEXT_TAKEOVER_GRACE_S"] = str(grace)
            return subprocess.run(
                ["bash", str(bindir / "status.sh")],
                capture_output=True, text=True, env=env, timeout=30,
            )

    return Ctl()


def test_healthy_proxy_reports_running(app):
    app.mark("proxy.pid")
    app.mark("watchdog.pid")
    assert app.run(probe_ok=True).returncode == 0


def test_takeover_lost_with_watchdog_still_reports_running(app):
    """核心修复：接管丢了但看门狗在救 —— 应用仍然「已启用」，不能报未运行。

    报 exit 3 会让飞牛把应用置为未启用，而我们几十秒后明明已经自愈了，
    状态却不会自动翻回来，用户只能手动点一次「启用」。
    """
    app.mark("proxy.pid")
    app.mark("watchdog.pid")
    r = app.run(probe_ok=False)
    assert r.returncode == 0, f"不该报未运行：{r.stderr}"
    assert "接管丢失" in r.stderr, "状态要如实说明正在自愈，不能装作一切正常"


def test_takeover_lost_without_watchdog_reports_not_running(app):
    """没人会来救了，那就得诚实报未运行——否则永远是「已启用但不能用」。"""
    app.mark("proxy.pid")
    r = app.run(probe_ok=False)
    assert r.returncode == 3


def test_proxy_dead_reports_not_running(app):
    app.mark("watchdog.pid")
    assert app.run(probe_ok=False).returncode == 3


def test_grace_expired_stops_pretending(app):
    """宽限期内报 running，超期说明看门狗真救不回来，该让人介入了。"""
    app.mark("proxy.pid")
    app.mark("watchdog.pid")
    assert app.run(probe_ok=False, grace=600).returncode == 0
    # 把丢失标记的时间往回拨，伪造「已经丢了很久」
    marker = app.pkgvar / "takeover.lost"
    marker.touch()
    old = int(marker.stat().st_mtime) - 3600
    os.utime(marker, (old, old))
    r = app.run(probe_ok=False, grace=600)
    assert r.returncode == 3, "丢了一小时还没恢复，不能再装作已启用"


def test_recovered_takeover_resets_the_clock(app):
    """恢复后必须清掉标记，否则下一次丢失会沿用旧时间戳、直接超宽限。"""
    app.mark("proxy.pid")
    app.mark("watchdog.pid")
    app.run(probe_ok=False)
    assert (app.pkgvar / "takeover.lost").exists()
    app.run(probe_ok=True)
    assert not (app.pkgvar / "takeover.lost").exists()
