"""fpk 安装/卸载脚本的登录凭证持久化契约。

背景（真机事故）：手动安装新版 fpk 走的是「卸载 + 安装」，而 uninstall_callback 的
数据清理条件尾部写了 ``|| true`` 使其**恒为真**，无条件 ``rm -rf ${RUN_DIR}``；
网易云登录凭证当时正住在 ``${RUN_DIR}/musicbox-data``，于是每装一次新版就被删一次，
用户被迫重新扫码 —— 而频繁扫码登录正是网易云风控最敏感的行为之一。

修复把凭证迁到 ``${PKGVAR}/musicbox-data``（RUN_DIR 之外，跨安装持久），并把条件
改回尊重向导勾选。本模块用**真的 bash 执行**这些脚本片段来验证行为，而不只是比对文本。
"""
from __future__ import annotations

import time
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


# ---------------------------------------------------------------------------
# v2.4 回归：.env 设置持久化
#
# 真机事故：write_env_file 每次执行都把 .env 整表重写成硬编码默认值（只特殊
# 保留了 PushPlus token）。于是「应用设置」保存一次、或应用升级一次，用户在
# 管理页改过的歌单口径 / 收藏归档目录 / 音质策略等全部被冲回默认。
# 修复后规则：
#   * 升级（FNMUSICEXT_PRESERVE_ENV=true）：旧值一律保留，只补齐缺失键；
#   * 应用设置保存 / 安装：向导键用向导值，其余键保留旧值；
#   * 卸载+重装：从 .env.preserved 快照恢复全部旧值。
# ---------------------------------------------------------------------------

import tempfile


def _fake_layout(tmp_path: Path, env_text: str = "") -> tuple:
    """搭一个最小 fpk 目录布局：APPDEST 载荷 + PKGVAR。"""
    appdest = tmp_path / "target"
    pkgvar = tmp_path / "pkgvar"
    for sub in ("bin", "proxy", "musicbox-service"):
        (appdest / sub).mkdir(parents=True)
    # setup.sh 需要 APPDEST/bin 下的库与脚本本身
    shutil.copy(LIB, appdest / "bin" / "fnmusic-lib.sh")
    shutil.copy(SETUP, appdest / "bin" / "setup.sh")
    (appdest / "VERSION").write_text("9.9.9", encoding="utf-8")
    (pkgvar / "logs").mkdir(parents=True)
    run_dir = pkgvar / "app"
    run_dir.mkdir()
    if env_text:
        (run_dir / ".env").write_text(env_text, encoding="utf-8")
    return appdest, pkgvar


def _run_setup(appdest, pkgvar, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TRIM_APPDEST": str(appdest),
        "TRIM_PKGVAR": str(pkgvar),
        "TRIM_USERNAME": "",
        "FNMUSICEXT_REBUILD_VENV": "false",
        **(extra_env or {}),
    }
    return subprocess.run(
        [bash, str(appdest / "bin" / "setup.sh")],
        capture_output=True, timeout=120, env=env,
    )


USER_ENV = """# 旧配置（用户在管理页保存过的）
FNMUSIC_HOME='/vol1/@appdata/fnmusicext/app'
FNMUSIC_VERSION='2.3.0'
FNMUSIC_NETEASE_CHANNELS='mine,nrec,toplist,category,newalbum,fm'
FNMUSIC_NETEASE_CHANNEL_LIMIT='20'
FNMUSIC_NETEASE_CATEGORY='摇滚'
FNMUSIC_NETEASE_CHANNEL_ORDER='toplist,mine,daily,category,nrec,newalbum,fm'
FNMUSIC_NETEASE_PLAYLIST_ORDER='daily,online:playlist:ne:11'
FNMUSIC_PLAYLIST_TRACK_CACHE_TTL='43200'
FNMUSIC_PLAYLIST_REFRESH_AT='05:30'
FNMUSIC_PLAYLIST_TRACK_LIMIT='500'
FNMUSIC_DOWNLOAD_DIR='/vol1/1000/music/网易云归档'
FNMUSIC_DOWNLOAD_ON_FAVORITE='false'
FNMUSIC_FAV_SYNC_LIKE='false'
FNMUSIC_QUALITY_POLICY='fixed'
FNMUSIC_QUALITY_FIXED='hires'
FNMUSIC_NETEASE_QUALITY='exhigh'
FNMUSIC_PUSHPLUS_TOKEN='old_secret_token_value'
FNMUSIC_LOG_MAX_DAYS='90'
FNMUSIC_MUSIC_DB='/custom/music.db'
MY_CUSTOM_KEY='keep-me'
"""


def _env_dict(path: Path) -> dict:
    from proxy import env_merge as _em  # noqa: PLC0415
    kv, _ = _em.parse_env_file(str(path))
    return dict(kv)


def test_setup_upgrade_preserves_all_user_settings(tmp_path):
    """升级（PRESERVE_ENV=true）：向导值一个都不该覆盖旧设置。"""
    appdest, pkgvar = _fake_layout(tmp_path, USER_ENV)
    r = _run_setup(appdest, pkgvar, {
        "FNMUSICEXT_PRESERVE_ENV": "true",
        # 模拟升级时框架塞进来的旧向导默认值——必须被旧值压住
        "wizard_netease_quality": "lossless",
        "wizard_daily_limit": "20",
        "wizard_pushplus_token": "",
    })
    assert r.returncode == 0, r.stderr.decode()[:800]
    env = _env_dict(pkgvar / "app" / ".env")
    assert env["FNMUSIC_NETEASE_CHANNELS"] == "mine,nrec,toplist,category,newalbum,fm"
    assert env["FNMUSIC_NETEASE_CHANNEL_LIMIT"] == "20"
    assert env["FNMUSIC_NETEASE_CATEGORY"] == "摇滚"
    assert env["FNMUSIC_NETEASE_CHANNEL_ORDER"] == "toplist,mine,daily,category,nrec,newalbum,fm"
    assert env["FNMUSIC_NETEASE_PLAYLIST_ORDER"] == "daily,online:playlist:ne:11", \
        "管理页手动排的歌单顺序绝不能被升级冲掉"
    assert env["FNMUSIC_PLAYLIST_TRACK_CACHE_TTL"] == "43200"
    assert env["FNMUSIC_PLAYLIST_REFRESH_AT"] == "05:30"
    assert env["FNMUSIC_PLAYLIST_TRACK_LIMIT"] == "500"
    assert env["FNMUSIC_DOWNLOAD_DIR"] == "/vol1/1000/music/网易云归档"
    assert env["FNMUSIC_DOWNLOAD_ON_FAVORITE"] == "false"
    assert env["FNMUSIC_FAV_SYNC_LIKE"] == "false"
    assert env["FNMUSIC_QUALITY_POLICY"] == "fixed"
    assert env["FNMUSIC_QUALITY_FIXED"] == "hires"
    assert env["FNMUSIC_NETEASE_QUALITY"] == "exhigh", "升级绝不能把音质冲回 lossless"
    assert env["FNMUSIC_PUSHPLUS_TOKEN"] == "old_secret_token_value"
    assert env["FNMUSIC_LOG_MAX_DAYS"] == "90"
    assert env["FNMUSIC_MUSIC_DB"] == "/custom/music.db"
    assert env["MY_CUSTOM_KEY"] == "keep-me", "用户自定义键必须原样保留"
    # 结构键必须更新为新布局
    assert env["FNMUSIC_VERSION"] == "9.9.9"


def test_setup_config_save_uses_wizard_but_keeps_rest(tmp_path):
    """应用设置保存：向导键采用向导值，非向导键保留旧值。"""
    appdest, pkgvar = _fake_layout(tmp_path, USER_ENV)
    r = _run_setup(appdest, pkgvar, {
        "wizard_netease_quality": "standard",
        "wizard_pushplus_token": "brand_new_token_123",
    })
    assert r.returncode == 0, r.stderr.decode()[:800]
    env = _env_dict(pkgvar / "app" / ".env")
    assert env["FNMUSIC_NETEASE_QUALITY"] == "standard", "用户在向导里改的音质要生效"
    assert env["FNMUSIC_PUSHPLUS_TOKEN"] == "brand_new_token_123"
    # 不在向导里的键：全部保持
    assert env["FNMUSIC_NETEASE_CHANNELS"] == "mine,nrec,toplist,category,newalbum,fm"
    assert env["FNMUSIC_DOWNLOAD_DIR"] == "/vol1/1000/music/网易云归档"
    assert env["FNMUSIC_QUALITY_FIXED"] == "hires"


def test_setup_config_save_empty_token_keeps_old(tmp_path):
    """向导 token 留空 = 不修改，不能把已保存的 token 冲掉。"""
    appdest, pkgvar = _fake_layout(tmp_path, USER_ENV)
    r = _run_setup(appdest, pkgvar, {"wizard_pushplus_token": ""})
    assert r.returncode == 0, r.stderr.decode()[:800]
    env = _env_dict(pkgvar / "app" / ".env")
    assert env["FNMUSIC_PUSHPLUS_TOKEN"] == "old_secret_token_value"


def test_setup_fresh_install_fills_defaults_and_new_keys(tmp_path):
    """全新安装：无旧 .env，全部按向导/默认生成，含新增的 CHANNEL_ORDER。"""
    appdest, pkgvar = _fake_layout(tmp_path, env_text="")
    r = _run_setup(appdest, pkgvar, {"wizard_netease_quality": "exhigh"})
    assert r.returncode == 0, r.stderr.decode()[:800]
    env = _env_dict(pkgvar / "app" / ".env")
    assert env["FNMUSIC_NETEASE_QUALITY"] == "exhigh"
    assert env["FNMUSIC_NETEASE_CHANNELS"] == "mine,toplist,category"
    assert env["FNMUSIC_NETEASE_CHANNEL_ORDER"] == "daily,localdaily,mine,nrec,toplist,category,newalbum,fm"
    assert env["FNMUSIC_DOWNLOAD_DIR"] == ""


def test_setup_reinstall_restores_from_preserved_snapshot(tmp_path):
    """卸载+重装：RUN_DIR/.env 没了，但 .env.preserved 快照里的设置要恢复。"""
    appdest, pkgvar = _fake_layout(tmp_path, env_text="")
    (pkgvar / ".env.preserved").write_text(USER_ENV, encoding="utf-8")
    r = _run_setup(appdest, pkgvar, {"wizard_netease_quality": "lossless"})
    assert r.returncode == 0, r.stderr.decode()[:800]
    env = _env_dict(pkgvar / "app" / ".env")
    # 向导值（用户重装时刚填的）优先，其余从快照恢复
    assert env["FNMUSIC_NETEASE_QUALITY"] == "lossless"
    assert env["FNMUSIC_NETEASE_CHANNELS"] == "mine,nrec,toplist,category,newalbum,fm"
    assert env["FNMUSIC_DOWNLOAD_DIR"] == "/vol1/1000/music/网易云归档"
    assert env["FNMUSIC_PUSHPLUS_TOKEN"] == "old_secret_token_value"
    assert env["MY_CUSTOM_KEY"] == "keep-me"


# ---------------------------------------------------------------------------
# v2.4 回归：重启窗口内 status 必须继续报 running（防「保存即闪退」）
#
# 真机现象：管理页保存配置触发 stop→start，几秒到几十秒的窗口里
# cmd/main status 返回 3（未运行），飞牛桌面随即回收本应用已打开的窗口，
# 用户看到的就是「保存一次、应用闪退一次」。
# ---------------------------------------------------------------------------

STATUS = REPO / "fpk" / "payload" / "bin" / "status.sh"
RESTART = REPO / "fpk" / "payload" / "bin" / "restart_services.sh"


def _run_status(pkgvar) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TRIM_APPDEST": str(pkgvar.parent / "target"),
        "TRIM_PKGVAR": str(pkgvar),
    }
    return subprocess.run([bash, str(STATUS)], capture_output=True, timeout=60, env=env)


def test_status_reports_running_while_restart_marker_fresh(tmp_path):
    pkgvar = tmp_path / "pkgvar"
    pkgvar.mkdir()
    (pkgvar / "logs").mkdir()
    # 没有代理进程、没有 socket —— 本该报「未运行」
    r = _run_status(pkgvar)
    assert r.returncode == 3, "无标记时必须如实报未运行"

    # 打上 fresh 标记：必须报 running，让桌面别收走窗口
    (pkgvar / "restart.inprogress").write_text(str(int(time.time())) + "\n", encoding="utf-8")
    r = _run_status(pkgvar)
    assert r.returncode == 0, "重启窗口内必须报 running，否则桌面会闪退回收窗口"

    # 标记过期（比如 restart 脚本被 kill -9 后留下的死标记）：恢复如实上报
    stale = int(time.time()) - 3600
    (pkgvar / "restart.inprogress").write_text(str(stale) + "\n", encoding="utf-8")
    import os as _os
    _os.utime(pkgvar / "restart.inprogress", (stale, stale))
    r = _run_status(pkgvar)
    assert r.returncode == 3, "死标记超时后不能永远谎报 running"


def test_restart_services_clears_marker_on_exit(tmp_path):
    """restart_services.sh 无论成败都要收掉标记（trap EXIT）。"""
    src = RESTART.read_text(encoding="utf-8")
    assert "lib_restart_marker_begin" in src
    assert "trap 'lib_restart_marker_end' EXIT INT TERM" in src, (
        "必须用 trap 兜底清理标记，否则一次 kill 就留下永久谎报 running 的死标记"
    )
