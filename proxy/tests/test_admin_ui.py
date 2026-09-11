"""管理页面（proxy/admin_ui.py）测试：鉴权、配置读写与安全边界。"""
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import admin_ui, env_merge

ADMIN = {"X-Trim-Userid": "1000", "X-Trim-Isadmin": "true", "X-Trim-Username": "gzy"}
USER = {"X-Trim-Userid": "1001", "X-Trim-Isadmin": "false", "X-Trim-Username": "family"}
# 合成假凭据，仅用于断言脱敏行为；不是任何真实 token
FAKE_TOKEN = "syntactic-fake-token-0123456789"
MASK = admin_ui.MASK

SEED_ENV = (
    "FNMUSIC_NETEASE_QUALITY='exhigh'\n"
    f"FNMUSIC_PUSHPLUS_TOKEN='{FAKE_TOKEN}'\n"
    "FNMUSIC_PUSHPLUS_TOPIC='mygroup'\n"
    "FNMUSIC_DAILY_LIMIT='20'\n"
    "FNMUSIC_SEARCH_CACHE_TTL='604800'\n"
    "FNMUSIC_LOGIN_CHECK_INTERVAL='3600'\n"
    "FNMUSIC_MY_CUSTOM='keepme'\n"
)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(SEED_ENV, encoding="utf-8")
    monkeypatch.setenv("FNMUSIC_ADMIN_ENV_FILE", str(path))
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_ADMIN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("FNMUSIC_ADMIN_ALLOW_NO_GATEWAY", raising=False)
    monkeypatch.setattr(admin_ui, "ENV_FILE", str(path))
    monkeypatch.setattr(admin_ui, "RESTART_SCRIPT", "")
    admin_ui._MB_CLIENT = None
    yield path
    admin_ui._MB_CLIENT = None


def read_env(path):
    return dict(env_merge.parse_env_file(path)[0])


def post_cfg(client, values, restart=False):
    return client.post("/api/config", headers=ADMIN,
                       json={"values": values, "restart": restart})


def mock_mb(handler):
    admin_ui._MB_CLIENT = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8770")


# --------------------------------------------------------------------- 鉴权 ----

def test_page_denied_without_gateway_headers():
    with TestClient(admin_ui.app) as c:
        r = c.get("/")
        assert r.status_code == 403
        assert "飞牛桌面" in r.text
        assert c.get("/api/health").status_code == 403
        assert c.get("/api/config").status_code == 403
        assert c.get("/api/logs?what=info").status_code == 403


def test_api_denied_for_non_admin():
    with TestClient(admin_ui.app) as c:
        for path in ("/api/health", "/api/config", "/api/login/state", "/api/logs?what=info"):
            r = c.get(path, headers=USER)
            assert r.status_code == 403, path
            assert "管理员" in r.json()["error"]


def test_write_denied_for_non_admin():
    with TestClient(admin_ui.app) as c:
        r = c.post("/api/config", headers=USER, json={"values": {}, "restart": False})
        assert r.status_code == 403
        r = c.post("/api/login/qr", headers=USER)
        assert r.status_code == 403


def test_admin_page_renders_with_username():
    with TestClient(admin_ui.app) as c:
        r = c.get("/", headers=ADMIN)
        assert r.status_code == 200
        assert "gzy" in r.text
        assert "飞牛音乐扩展" in r.text
        # 单文件、零外部依赖：不允许出现任何 CDN / 外链脚本
        lowered = r.text.lower()
        assert "https://cdn" not in lowered and "<script src" not in lowered


def test_username_is_html_escaped():
    """用户名来自 Header，必须转义，否则就是存储型 XSS 入口。"""
    evil = {"X-Trim-Userid": "1", "X-Trim-Isadmin": "true",
            "X-Trim-Username": '<img src=x onerror="alert(1)">'}
    with TestClient(admin_ui.app) as c:
        r = c.get("/", headers=evil)
        assert r.status_code == 200
        assert "<img src=x onerror=" not in r.text
        assert "&lt;img" in r.text


def test_suspicious_userid_rejected():
    for bad in ("1000\ninjected: true", "a" * 200, "../../etc/passwd", "1;rm -rf /"):
        h = {"X-Trim-Userid": bad, "X-Trim-Isadmin": "true", "X-Trim-Username": "x"}
        with TestClient(admin_ui.app) as c:
            assert c.get("/api/health", headers=h).status_code == 403, bad


def test_dev_override_flag_off_by_default(monkeypatch):
    monkeypatch.setattr(admin_ui, "ALLOW_NO_GATEWAY", False)
    with TestClient(admin_ui.app) as c:
        assert c.get("/api/health").status_code == 403


def test_dev_override_flag_allows_local(monkeypatch):
    """仅用于无网关的开发环境；打开就等于把管理员权限给到任何能连上的人。"""
    monkeypatch.setattr(admin_ui, "ALLOW_NO_GATEWAY", True)
    mock_mb(lambda r: httpx.Response(200, json={"ok": True, "data": {"logged_in": False}}))
    with TestClient(admin_ui.app) as c:
        assert c.get("/api/health").status_code == 200


# --------------------------------------------------------- 网关前缀兼容 ----

@pytest.mark.parametrize("path", ["/api/health", "/app/fnmusicext/api/health",
                                  "/app/fnmusicext", "/app/fnmusicext/"])
def test_gateway_prefix_is_stripped(path):
    mock_mb(lambda r: httpx.Response(200, json={"ok": True, "data": {"logged_in": False}}))
    with TestClient(admin_ui.app) as c:
        r = c.get(path, headers=ADMIN)
        assert r.status_code == 200, path


def test_prefix_strip_does_not_open_unrelated_paths():
    with TestClient(admin_ui.app) as c:
        # 不在网关前缀下、也不在根路由内的路径不该被误判放行
        assert c.get("/app/otherapp/api/health", headers=ADMIN).status_code == 404


# -------------------------------------------------------------- 配置读取 ----

def test_config_masks_secrets(env):
    with TestClient(admin_ui.app) as c:
        r = c.get("/api/config", headers=ADMIN)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["values"]["pushplus_token"] == MASK
        assert body["values"]["pushplus_topic"] == MASK
        assert body["has_token"] is True
        assert FAKE_TOKEN not in r.text
        assert "mygroup" not in r.text, "群组编码同属敏感项，也要打码"


def test_config_converts_units_for_display(env):
    with TestClient(admin_ui.app) as c:
        v = c.get("/api/config", headers=ADMIN).json()["values"]
        assert v["search_cache_ttl_days"] == "7", "604800 秒应展示为 7 天"
        assert v["login_check_interval_h"] == "1", "3600 秒应展示为 1 小时"
        assert v["netease_quality"] == "exhigh"


def test_config_schema_exposed(env):
    with TestClient(admin_ui.app) as c:
        body = c.get("/api/config", headers=ADMIN).json()
        assert body["schema"]["pushplus_token"]["sensitive"] is True
        assert body["schema"]["netease_quality"]["sensitive"] is False
        assert body["env_file"] == str(env)


# -------------------------------------------------------------- 配置写入 ----

def test_save_other_fields_preserves_token(env):
    """关键回归：改音质绝不能把 PushPlus token 冲成打码串。"""
    with TestClient(admin_ui.app) as c:
        r = post_cfg(c, {"netease_quality": "standard"})
        assert r.status_code == 200 and r.json()["ok"] is True
    after = read_env(env)
    assert after["FNMUSIC_PUSHPLUS_TOKEN"] == FAKE_TOKEN
    assert after["FNMUSIC_PUSHPLUS_TOPIC"] == "mygroup"
    assert after["FNMUSIC_NETEASE_QUALITY"] == "standard"
    assert MASK not in env.read_text(encoding="utf-8"), "打码串绝不能落盘"


@pytest.mark.parametrize("submitted", ["", MASK, "   ", MASK + " "])
def test_blank_or_masked_token_keeps_existing(env, submitted):
    with TestClient(admin_ui.app) as c:
        assert post_cfg(c, {"pushplus_token": submitted}).json()["ok"] is True
    assert read_env(env)["FNMUSIC_PUSHPLUS_TOKEN"] == FAKE_TOKEN


def test_new_token_is_written(env):
    with TestClient(admin_ui.app) as c:
        r = post_cfg(c, {"pushplus_token": "brandnewtoken12345"})
        assert r.json()["ok"] is True
        assert "brandnewtoken12345" not in r.text, "响应体不得回显 token"
    assert read_env(env)["FNMUSIC_PUSHPLUS_TOKEN"] == "brandnewtoken12345"


def test_empty_submission_is_noop(env):
    before = read_env(env)
    with TestClient(admin_ui.app) as c:
        r = post_cfg(c, {})
        assert r.json()["ok"] is True
    after = read_env(env)
    for key, value in before.items():
        assert after.get(key) == value, f"{key} 在空提交后被改动了"
    assert after["FNMUSIC_MY_CUSTOM"] == "keepme"


def test_unit_conversion_on_write(env):
    with TestClient(admin_ui.app) as c:
        assert post_cfg(c, {"search_cache_ttl_days": "3",
                            "login_check_interval_h": "6"}).json()["ok"] is True
    after = read_env(env)
    assert after["FNMUSIC_SEARCH_CACHE_TTL"] == "259200"
    assert after["FNMUSIC_LOGIN_CHECK_INTERVAL"] == "21600"


def test_custom_keys_preserved_and_obsolete_dropped(env):
    env.write_text(SEED_ENV + "FNMUSIC_MUSICDL_ENABLED='true'\nFNMUSIC_LX_URL='http://h:8772'\n",
                   encoding="utf-8")
    with TestClient(admin_ui.app) as c:
        assert post_cfg(c, {"netease_quality": "lossless"}).json()["ok"] is True
    after = read_env(env)
    assert after["FNMUSIC_MY_CUSTOM"] == "keepme"
    assert "FNMUSIC_MUSICDL_ENABLED" not in after
    assert "FNMUSIC_LX_URL" not in after


def test_env_file_permission_and_backup(env):
    with TestClient(admin_ui.app) as c:
        post_cfg(c, {"netease_quality": "higher"})
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert env.with_suffix(".env.bak").exists() or os.path.exists(str(env) + ".bak")


@pytest.mark.parametrize("values,reason", [
    ({"netease_quality": "super-hi"}, "非法枚举"),
    ({"daily_limit": "9999"}, "越界"),
    ({"daily_limit": "abc"}, "非数字"),
    ({"daily_limit": "-1"}, "负数"),
    ({"pushplus_token": "short"}, "token 过短"),
    ({"pushplus_token": "bad token!!"}, "token 非法字符"),
    ({"pushplus_token": "x" * 400}, "token 过长"),
    ({"pushplus_url": "ftp://x"}, "非 http(s)"),
    ({"pushplus_url": "javascript:alert(1)"}, "协议注入"),
    ({"pushplus_topic": "a\nb"}, "含换行"),
    ({"pushplus_topic": "x" * 500}, "过长"),
    ({"pushplus_template": "yaml"}, "非法模板"),
])
def test_invalid_values_rejected_and_not_written(env, values, reason):
    with TestClient(admin_ui.app) as c:
        r = post_cfg(c, values)
        assert r.status_code == 422, reason
        assert not r.json()["ok"]
    # 校验失败必须整体不写入（不能出现半保存）
    assert read_env(env)["FNMUSIC_NETEASE_QUALITY"] == "exhigh"
    assert read_env(env)["FNMUSIC_PUSHPLUS_TOKEN"] == FAKE_TOKEN


def test_unknown_field_rejected(env):
    """只允许白名单字段，杜绝任意 .env 键注入。"""
    for payload in ({"FNMUSIC_HOME": "/etc"}, {"PATH": "/tmp"},
                    {"../../evil": "x"}, {"netease_quality_extra": "x"}):
        with TestClient(admin_ui.app) as c:
            r = c.post("/api/config", headers=ADMIN,
                       json={"values": payload, "restart": False})
            assert r.status_code == 400, payload
            assert "不支持的配置项" in r.json()["error"]


def test_malformed_request_body(env):
    with TestClient(admin_ui.app) as c:
        assert c.post("/api/config", headers=ADMIN, content=b"not json").status_code == 400
        assert c.post("/api/config", headers=ADMIN, json=[1, 2]).status_code == 400
        assert c.post("/api/config", headers=ADMIN, json={"restart": False}).status_code == 400
        assert c.post("/api/config", headers=ADMIN, json={"values": "x"}).status_code == 400


def test_save_reports_restart_failure_without_pretending(env, monkeypatch):
    """没配重启脚本时必须如实告知，而不是谎报成功。"""
    with TestClient(admin_ui.app) as c:
        r = post_cfg(c, {"netease_quality": "higher"}, restart=True).json()
    assert r["ok"] is True
    assert r["restarted"] is False
    assert "未找到重启脚本" in r["restart_message"]
    assert "warning" in r


# ------------------------------------------------------------------ 日志 ----

def test_logs_reject_path_traversal(tmp_path, monkeypatch):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    monkeypatch.setenv("FNMUSIC_ADMIN_LOG_DIR", str(logdir))
    with TestClient(admin_ui.app) as c:
        for what in ("../../etc/passwd", "../.env", "info/../secret", "proxy.sh", "/etc/passwd"):
            r = c.get(f"/api/logs?what={what}", headers=ADMIN)
            assert r.status_code == 400, what


def test_logs_reads_tail_and_redacts_token(tmp_path, monkeypatch):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "info.log").write_text(
        "\n".join(f"line {i}" for i in range(500)) + f"\nleaked {FAKE_TOKEN} here\n",
        encoding="utf-8")
    monkeypatch.setenv("FNMUSIC_ADMIN_LOG_DIR", str(logdir))
    admin_ui._MB_CLIENT = None
    with TestClient(admin_ui.app) as c:
        r = c.get("/api/logs?what=info&lines=5", headers=ADMIN).json()
    assert r["ok"] is True
    assert len(r["lines"]) == 5
    assert "line 499" in r["lines"]
    assert "line 100" not in r["lines"], "只返回末尾 N 行"
    assert FAKE_TOKEN not in "\n".join(r["lines"]), "日志里的 token 必须被脱敏"


def test_logs_redact_token_saved_but_not_yet_loaded(tmp_path, monkeypatch):
    """关键回归：用户在页面刚保存的 token 只存在于 .env，尚未被任何进程加载。

    只读进程环境变量做脱敏的话，这条新 token 会原样出现在日志回显里。
    """
    logdir = tmp_path / "logs"
    logdir.mkdir()
    brand_new = "brandnewtoken-not-in-process-env"
    (logdir / "proxy.log").write_text(
        f"some line with {brand_new} inside\nanother clean line\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(f"FNMUSIC_PUSHPLUS_TOKEN='{brand_new}'\n", encoding="utf-8")
    monkeypatch.setattr(admin_ui, "ENV_FILE", str(env_file))
    monkeypatch.setenv("FNMUSIC_ADMIN_LOG_DIR", str(logdir))
    # 进程环境变量里没有这个 token —— 正是真实场景
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_TOKEN", raising=False)

    from proxy import pushplus
    assert pushplus.token() == ""

    with TestClient(admin_ui.app) as c:
        r = c.get("/api/logs?what=proxy", headers=ADMIN).json()
    joined = "\n".join(r["lines"])
    assert brand_new not in joined, "刚落盘、未加载的 token 也必须脱敏"
    assert "***" in joined
    assert "another clean line" in joined


def test_logs_handles_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_ADMIN_LOG_DIR", str(tmp_path / "nope"))
    with TestClient(admin_ui.app) as c:
        r = c.get("/api/logs?what=proxy", headers=ADMIN).json()
    assert r["ok"] is True and r["lines"] == []


def test_logs_line_count_clamped(tmp_path, monkeypatch):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "info.log").write_text("\n".join(f"l{i}" for i in range(2000)), encoding="utf-8")
    monkeypatch.setenv("FNMUSIC_ADMIN_LOG_DIR", str(logdir))
    with TestClient(admin_ui.app) as c:
        n = len(c.get("/api/logs?what=info&lines=999999", headers=ADMIN).json()["lines"])
    assert n <= admin_ui.LOG_LINE_LIMIT


# -------------------------------------------------------------- 扫码登录 ----

def test_login_qr_flow():
    state = {"calls": []}

    def handler(r: httpx.Request) -> httpx.Response:
        state["calls"].append(r.url.path)
        if r.method == "POST" and r.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"ok": True, "data": {"unikey": "ABC123def456"}})
        if r.url.path == "/api/v1/auth/login/qr.png":
            assert r.url.params.get("unikey") == "ABC123def456"
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nFAKE",
                                  headers={"content-type": "image/png"})
        return httpx.Response(404)

    mock_mb(handler)
    with TestClient(admin_ui.app) as c:
        r = c.post("/api/login/qr", headers=ADMIN).json()
        assert r["ok"] is True and r["unikey"] == "ABC123def456"
        assert r["qr_png"].startswith("api/login/qr.png?unikey=")

        png = c.get(r["qr_png"], headers=ADMIN)
        assert png.status_code == 200
        assert png.headers["content-type"] == "image/png"
        assert png.headers["cache-control"] == "no-store"
        assert png.content.startswith(b"\x89PNG")
    # 二维码与轮询必须用同一个 unikey，不能再触发第 4 次登录
    assert state["calls"].count("/api/v1/auth/login") == 1


@pytest.mark.parametrize("unikey", ["", "abc", "a" * 300, "../etc", "a b", "a\nb", "';drop--"])
def test_login_endpoints_validate_unikey(unikey):
    mock_mb(lambda r: httpx.Response(200, json={}))
    with TestClient(admin_ui.app) as c:
        assert c.get("/api/login/qr.png", params={"unikey": unikey},
                     headers=ADMIN).status_code == 400, unikey
        assert c.get("/api/login/check", params={"unikey": unikey},
                     headers=ADMIN).status_code == 400, unikey


def test_login_qr_rejects_malformed_unikey_from_upstream():
    """上游返回形状异常的 unikey 时必须拒绝，不能拿它拼 URL。"""
    mock_mb(lambda r: httpx.Response(200, json={"ok": True, "data": {"unikey": "'; DROP TABLE--"}}))
    with TestClient(admin_ui.app) as c:
        r = c.post("/api/login/qr", headers=ADMIN)
        assert r.status_code == 502


def test_login_qr_upstream_unreachable():
    def boom(r):
        raise httpx.ConnectError("拒绝连接")

    mock_mb(boom)
    with TestClient(admin_ui.app) as c:
        r = c.post("/api/login/qr", headers=ADMIN)
        assert r.status_code == 502
        assert "不可达" in r.json()["error"]


@pytest.mark.parametrize("code,expect", [(801, 801), (802, 802), (800, 800)])
def test_login_check_passthrough_codes(code, expect, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "false")
    mock_mb(lambda r: httpx.Response(200, json={"ok": True, "data": {"code": code}}))
    with TestClient(admin_ui.app) as c:
        body = c.get("/api/login/check", params={"unikey": "ABC123def456"}, headers=ADMIN).json()
    assert body["ok"] is True and body["code"] == expect
    assert "logged_in" not in body, "只有 803 才去查登录态"


def test_login_check_success_refreshes_state(monkeypatch):
    sent = []
    from proxy import netease_auth, pushplus

    async def fake_send(client, title, content, **kw):
        sent.append(title)
        return True

    monkeypatch.setattr(pushplus, "send", fake_send)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_TOKEN", "t" * 32)
    netease_auth.reset_for_test()

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/api/v1/auth/login/check":
            return httpx.Response(200, json={"ok": True, "data": {"code": 803}})
        if r.url.path == "/api/v1/auth/status":
            return httpx.Response(200, json={"ok": True, "data": {
                "logged_in": True, "nickname": "张三"}})
        return httpx.Response(404)

    mock_mb(handler)
    with TestClient(admin_ui.app) as c:
        body = c.get("/api/login/check", params={"unikey": "ABC123def456"}, headers=ADMIN).json()
    assert body["code"] == 803 and body["logged_in"] is True and body["nickname"] == "张三"
    assert sent and "登录成功" in sent[0]
    netease_auth.reset_for_test()


# ------------------------------------------------------------------ 健康 ----

def test_health_shape(monkeypatch):
    mock_mb(lambda r: httpx.Response(200, json={"ok": True, "data": {
        "logged_in": True, "nickname": "张三",
        "profile": {"nickname": "张三", "userId": "1", "vipType": 11,
                    "vipExpiryTime": 1800000000000}}}))
    monkeypatch.setattr(admin_ui, "PROXY_SOCK", "/nonexistent.sock")
    with TestClient(admin_ui.app) as c:
        r = c.get("/api/health", headers=ADMIN)
        assert r.status_code == 200
        body = r.json()
    assert body["ok"] is True and body["version"]
    assert body["netease"]["logged_in"] is True
    assert body["socket_takeover"] is False
    assert body["proxy"]["ok"] is False
    assert FAKE_TOKEN not in r.text


def test_health_survives_musicbox_outage(monkeypatch):
    def boom(r):
        raise httpx.ConnectError("拒绝连接")

    mock_mb(boom)
    monkeypatch.setattr(admin_ui, "PROXY_SOCK", "/nonexistent.sock")
    with TestClient(admin_ui.app) as c:
        r = c.get("/api/health", headers=ADMIN)
        assert r.status_code == 200, "音源挂了页面也必须能打开，否则用户无法自救"
        assert r.json()["ok"] is True


def test_read_config_tolerates_missing_or_corrupt_env(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_ui, "ENV_FILE", str(tmp_path / "nope.env"))
    cfg = admin_ui.read_config_masked()
    assert cfg["values"]["netease_quality"] == "lossless"

    bad = tmp_path / ".env"
    bad.write_text("FNMUSIC_NETEASE_QUALITY='='broken\n\x00binary", encoding="utf-8", errors="replace")
    monkeypatch.setattr(admin_ui, "ENV_FILE", str(bad))
    cfg = admin_ui.read_config_masked()
    assert isinstance(cfg["values"], dict)


def test_restart_reports_failure_when_script_missing(monkeypatch, tmp_path):
    import asyncio

    monkeypatch.setattr(admin_ui, "RESTART_SCRIPT", str(tmp_path / "gone.sh"))
    ok, msg = asyncio.run(admin_ui.restart_services())
    assert ok is False and "未找到重启脚本" in msg


def test_restart_invokes_script(monkeypatch, tmp_path):
    import asyncio

    script = tmp_path / "restart_services.sh"
    script.write_text("#!/bin/bash\necho restarted-ok\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(admin_ui, "RESTART_SCRIPT", str(script))
    ok, msg = asyncio.run(admin_ui.restart_services())
    assert ok is True and "已重启" in msg


def test_restart_propagates_script_failure(monkeypatch, tmp_path):
    import asyncio

    script = tmp_path / "restart_services.sh"
    script.write_text("#!/bin/bash\necho '代理未能接管' >&2\nexit 1\n", encoding="utf-8")
    monkeypatch.setattr(admin_ui, "RESTART_SCRIPT", str(script))
    ok, msg = asyncio.run(admin_ui.restart_services())
    assert ok is False and "代理未能接管" in msg


def test_restart_timeout_is_reported(monkeypatch, tmp_path):
    import asyncio

    script = tmp_path / "restart_services.sh"
    script.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
    monkeypatch.setattr(admin_ui, "RESTART_SCRIPT", str(script))
    monkeypatch.setattr(admin_ui, "RESTART_TIMEOUT_S", 0.3)
    ok, msg = asyncio.run(admin_ui.restart_services())
    assert ok is False and "超时" in msg
