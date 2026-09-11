"""fpk 安装/卸载脚本的登录凭证持久化契约。

背景（真机事故）：手动安装新版 fpk 走的是「卸载 + 安装」，而 uninstall_callback 的
数据清理条件尾部写了 ``|| true`` 使其**恒为真**，无条件 ``rm -rf ${RUN_DIR}``；
网易云登录凭证当时正住在 ``${RUN_DIR}/musicbox-data``，于是每装一次新版就被删一次，
用户被迫重新扫码 —— 而频繁扫码登录正是网易云风控最敏感的行为之一。

修复把凭证迁到 ``${PKGVAR}/musicbox-data``（RUN_DIR 之外，跨安装持久），并把条件
改回尊重向导勾选。本模块用**真的 bash 执行**这些脚本片段来验证行为，而不只是比对文本。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
LIB = REPO / "fpk" / "payload" / "bin" / "fnmusic-lib.sh"
START = REPO / "fpk" / "payload" / "bin" / "start.sh"
SETUP = REPO / "fpk" / "payload" / "bin" / "setup.sh"
UNINSTALL = REPO / "fpk" / "cmd" / "uninstall_callback"

bash = shutil.which("bash")
pytestmark = pytest.mark.skipif(bash is None, reason="需要 bash")

COOKIE_REL = "netease-musicbox/cookie.txt"
COOKIE_BODY = ".music.163.com\tTRUE\t/\tTRUE\t9999999999\tMUSIC_U\tSESSION_TOKEN\n"


# --------------------------------------------------------------------- 文本契约


def test_uninstall_data_removal_is_not_a_tautology():
    """核心回归：``if is_true A || is_true B || true`` 会让清理条件恒成立。

    正是这一行把用户的登录凭证在每次「卸载+重装」时删掉。
    """
    src = UNINSTALL.read_text(encoding="utf-8")
    assert re.search(r"if is_true .* \|\| true;", src) is None, (
        "数据清理条件里出现了 `|| true`，会变成无条件删除用户数据"
    )
    assert re.search(r"^if is_true \"\$\{REMOVE_DATA\}\" \|\| is_true \"\$\{REMOVE_LIB\}\"; then$",
                     src, re.M), "应只在用户明确勾选时才清理运行目录"


def test_credential_dir_lives_outside_run_dir():
    """凭证目录必须在 RUN_DIR 之外：RUN_DIR 每次安装都会被 stage 覆盖/删除。"""
    lib = LIB.read_text(encoding="utf-8")
    m = re.search(r'^MUSICBOX_DATA_DIR="(.*)"$', lib, re.M)
    assert m, "fnmusic-lib.sh 必须定义 MUSICBOX_DATA_DIR"
    assert m.group(1) == "${PKGVAR}/musicbox-data", "应挂在 PKGVAR 下而非 RUN_DIR 下"
    assert "${RUN_DIR}" not in m.group(1)


def test_uninstall_callback_defines_credential_dir_locally():
    """uninstall_callback 不 source 那个 lib（卸载阶段 APPDEST 可能已不存在），
    必须就地定义同名变量，否则清理时变量为空 => 删除静默失效。"""
    src = UNINSTALL.read_text(encoding="utf-8")
    m = re.search(r'^MUSICBOX_DATA_DIR="(.*)"$', src, re.M)
    assert m and m.group(1) == "${PKGVAR}/musicbox-data", (
        "两处定义必须一致，否则删的是另一个目录"
    )


def test_start_sh_points_xdg_at_persistent_dir():
    """服务进程的 XDG 必须指向持久目录，否则读写的还是会被删掉的那份。"""
    src = START.read_text(encoding="utf-8")
    for var in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        m = re.search(rf'{var}="\$\{{([^}}]+)\}}(/[^"]*)?"', src)
        assert m, f"{var} 未定义"
        assert m.group(1) == "MUSICBOX_DATA_DIR", f"{var} 仍指向 {m.group(1)}"
    assert "${RUN_DIR}/musicbox-data" not in src, "不得再引用已废弃的旧凭证路径"


def test_setup_sh_migrates_legacy_credential():
    """setup 必须调用迁移，老用户升级后不用重新扫码。"""
    src = SETUP.read_text(encoding="utf-8")
    assert "lib_migrate_musicbox_data" in src
    assert "lib_ensure_musicbox_data_dirs" in src


def test_cookie_permission_not_gated_on_username():
    """cookie 收紧到 0600 不能写在 `TRIM_USERNAME 存在` 的判断之后。

    原先如此：拿不到包用户名（异常环境）时敏感文件就停留在 0644，本机其他用户可读。
    """
    src = LIB.read_text(encoding="utf-8")
    fn = src[src.index("lib_chown_musicbox_data()"):]
    fn = fn[:fn.index("\n}\n") + 1]
    chmod_at, guard_at = fn.index("chmod 600"), fn.index('[ -n "${target}" ]')
    assert chmod_at < guard_at, "必须先无条件收紧权限，再按需改属主"


def test_all_scripts_pass_bash_syntax_check():
    for path in (LIB, START, SETUP, UNINSTALL):
        r = subprocess.run([bash, "-n", str(path)], capture_output=True, timeout=60)
        assert r.returncode == 0, f"{path.name} bash -n 失败: {r.stderr.decode()[:400]}"


# --------------------------------------------------------------------- 真实执行


@pytest.fixture
def pkgvar(tmp_path):
    """伪造一个 PKGVAR 布局：旧凭证在 RUN_DIR 里，日志目录平级。"""
    root = tmp_path / "pkgvar"
    legacy = root / "app" / "musicbox-data" / COOKIE_REL
    legacy.parent.mkdir(parents=True)
    legacy.write_text(COOKIE_BODY, encoding="utf-8")
    (root / "logs").mkdir()
    return root


def _call_lib(pkgvar, body: str, username: str = "") -> subprocess.CompletedProcess:
    script = (
        "set -uo pipefail\n"
        f"export TRIM_PKGVAR='{pkgvar}' APP_NAME=fnmusicext TRIM_USERNAME='{username}'\n"
        f"source '{LIB}'\n"
        f"{body}\n"
    )
    return subprocess.run([bash, "-c", script], capture_output=True, timeout=120)


def test_migration_moves_cookie_to_persistent_dir(pkgvar):
    r = _call_lib(pkgvar, "lib_migrate_musicbox_data; lib_ensure_musicbox_data_dirs")
    assert r.returncode == 0, r.stderr.decode()[:400]
    new = pkgvar / "musicbox-data" / COOKIE_REL
    assert new.exists(), "cookie 必须迁到 PKGVAR/musicbox-data"
    assert new.read_text(encoding="utf-8") == COOKIE_BODY, "内容必须原样保留"
    assert (pkgvar / "app" / "musicbox-data" / COOKIE_REL).exists(), "旧目录不动，交给 stage/卸载逻辑"


def test_migration_tightens_cookie_permission(pkgvar):
    r = _call_lib(pkgvar, "lib_migrate_musicbox_data; lib_ensure_musicbox_data_dirs")
    assert r.returncode == 0
    new = pkgvar / "musicbox-data" / COOKIE_REL
    assert oct(new.stat().st_mode & 0o777) == "0o600", "cookie 必须 0600（本机其他用户不可读）"
    assert oct((pkgvar / "musicbox-data").stat().st_mode & 0o777) == "0o700"


def test_migration_is_idempotent(pkgvar):
    _call_lib(pkgvar, "lib_migrate_musicbox_data")
    before = (pkgvar / "musicbox-data" / COOKIE_REL).read_text(encoding="utf-8")
    r = _call_lib(pkgvar, "lib_migrate_musicbox_data")
    assert r.returncode == 0
    assert (pkgvar / "musicbox-data" / COOKIE_REL).read_text(encoding="utf-8") == before


def test_migration_never_overwrites_a_fresh_login(pkgvar):
    """绝不能覆盖用户刚扫好的新码 —— 那会直接把人踢下线，比不迁移更糟。"""
    r = _call_lib(pkgvar, "lib_migrate_musicbox_data")
    assert r.returncode == 0
    new_cookie = pkgvar / "musicbox-data" / COOKIE_REL
    new_cookie.write_text("FRESH_SCAN_TOKEN\n", encoding="utf-8")

    # 旧目录随后又出现了另一份（例如旧版本残留）
    (pkgvar / "app" / "musicbox-data" / COOKIE_REL).write_text("OLD_TOKEN_AGAIN\n", encoding="utf-8")
    r = _call_lib(pkgvar, "lib_migrate_musicbox_data")
    assert r.returncode == 0
    assert new_cookie.read_text(encoding="utf-8") == "FRESH_SCAN_TOKEN\n"


def test_migration_noop_without_legacy_dir(tmp_path):
    root = tmp_path / "pkgvar"
    (root / "logs").mkdir(parents=True)
    r = _call_lib(root, "lib_migrate_musicbox_data; lib_ensure_musicbox_data_dirs")
    assert r.returncode == 0, "旧目录不存在时必须安静跳过，不能让安装失败"
    assert (root / "musicbox-data" / "netease-musicbox").is_dir()


def test_ensure_dirs_creates_nembox_layout(pkgvar):
    r = _call_lib(pkgvar, "lib_ensure_musicbox_data_dirs")
    assert r.returncode == 0
    base = pkgvar / "musicbox-data"
    for sub in ("cache/netease-musicbox", "config/netease-musicbox", "netease-musicbox"):
        assert (base / sub).is_dir(), f"缺少 {sub}，NEMbox 启动时会因建不出目录而失败"


# ------------------------------------------------- uninstall_callback 真实分支


def _run_uninstall(pkgvar, remove_data: str, remove_lib: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "TRIM_PKGVAR": str(pkgvar),
        "APP_NAME": "fnmusicext",
        "TRIM_USERNAME": "",
        "TRIM_TEMP_LOGFILE": str(pkgvar / "logs" / "tmp.log"),
        "wizard_remove_data": remove_data,
        "wizard_remove_library_cache": remove_lib,
    })
    return subprocess.run([bash, str(UNINSTALL)], capture_output=True, timeout=180, env=env)


@pytest.fixture
def installable(pkgvar):
    """把卸载脚本能找到的 BIN 与 cache/.env 铺好，让脚本走到数据清理那一段。"""
    shutil.copytree(REPO / "fpk" / "payload" / "bin", pkgvar / "app" / "bin", dirs_exist_ok=True)
    (pkgvar / "app" / "cache").mkdir(exist_ok=True)
    (pkgvar / "app" / ".env").write_text('FNMUSIC_PUSHPLUS_TOKEN="tok_abc"\n', encoding="utf-8")
    # 凭证已在 PKGVAR 下（新版布局）
    new_cookie = pkgvar / "musicbox-data" / COOKIE_REL
    new_cookie.parent.mkdir(parents=True, exist_ok=True)
    new_cookie.write_text(COOKIE_BODY, encoding="utf-8")
    return pkgvar


def test_uninstall_default_keeps_credential(installable):
    """默认（不勾选删除）必须保住登录凭证 —— 这是「装新版不用重新扫码」的关键。"""
    r = _run_uninstall(installable, "false", "false")
    assert (installable / "musicbox-data" / COOKIE_REL).exists(), (
        f"凭证被卸载流程删掉了（rc={r.returncode}）"
    )
    snap = installable / ".env.preserved"
    assert snap.exists(), "应快照 .env，重装时恢复 PushPlus token，免得用户重填"
    assert "tok_abc" in snap.read_text(encoding="utf-8")


def test_uninstall_with_remove_data_deletes_credential(installable):
    """用户明确勾选删除数据时，才连凭证一起清理（含删除运行目录）。"""
    _run_uninstall(installable, "true", "false")
    assert not (installable / "musicbox-data" / COOKIE_REL).exists(), "凭证应按用户意愿删除"
    log = (installable / "logs" / "uninstall.log").read_text(encoding="utf-8")
    assert "按用户选择删除网易云登录凭证" in log


def test_uninstall_log_records_preservation(installable):
    r = _run_uninstall(installable, "false", "false")
    log = (installable / "logs" / "uninstall.log").read_text(encoding="utf-8")
    assert "保留运行数据" in log, "日志必须明确说明保留了什么，便于事后排查"
    assert r.returncode == 0, r.stderr.decode()[:300]
