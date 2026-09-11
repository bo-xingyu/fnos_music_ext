import io
import os
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

# Ensure musicbox-service directory is in sys.path
MUSICBOX_SERVICE_DIR = str(Path(__file__).resolve().parent.parent.parent / "musicbox-service")
if MUSICBOX_SERVICE_DIR not in sys.path:
    sys.path.insert(0, MUSICBOX_SERVICE_DIR)

import runner
from app import app, UpstreamException


def test_ensure_xdg_dirs_creates_all_directories(tmp_path, monkeypatch):
    cache_dir = tmp_path / "custom_cache"
    config_dir = tmp_path / "custom_config"
    data_dir = tmp_path / "custom_data"
    runtime_dir = tmp_path / "custom_runtime"

    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    runner.ensure_xdg_dirs()

    # All base directories and netease-musicbox subdirectories must exist
    assert cache_dir.is_dir()
    assert (cache_dir / "netease-musicbox").is_dir()
    assert config_dir.is_dir()
    assert (config_dir / "netease-musicbox").is_dir()
    assert data_dir.is_dir()
    assert (data_dir / "netease-musicbox").is_dir()
    assert runtime_dir.is_dir()
    assert (runtime_dir / "netease-musicbox").is_dir()


def test_auth_login_qr_with_upstream_qr_ascii(monkeypatch):
    test_ascii = "█▀▀▀▀▀▀▀█\n█ █▀▀▀█ █\n▀▀▀▀▀▀▀▀▀"

    def mock_run_musicbox(args, timeout=30.0):
        assert args == ["auth", "login", "--no-wait", "--json"]
        stdout = (
            '{"ok": true, "data": {"unikey": "test-key-123", "qr_ascii": "'
            + test_ascii.replace("\n", "\\n")
            + '"}}'
        )
        return 0, stdout, ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        # Test main endpoint /api/v1/auth/login/qr
        resp1 = client.get("/api/v1/auth/login/qr")
        assert resp1.status_code == 200
        assert "text/plain" in resp1.headers["content-type"]
        assert test_ascii in resp1.text
        assert resp1.text.endswith("\n")

        # Test alias endpoint /api/v1/auth/qr
        resp2 = client.get("/api/v1/auth/qr")
        assert resp2.status_code == 200
        assert "text/plain" in resp2.headers["content-type"]
        assert test_ascii in resp2.text
        assert resp2.text.endswith("\n")


def test_auth_login_qr_fallback_to_qrcode_generation(monkeypatch):
    def mock_run_musicbox(args, timeout=30.0):
        assert args == ["auth", "login", "--no-wait", "--json"]
        # No qr_ascii in payload, only unikey
        stdout = '{"ok": true, "data": {"unikey": "test-fallback-key"}}'
        return 0, stdout, ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/login/qr")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        # Generated QR should contain black/white blocks
        assert len(resp.text) > 50
        assert resp.text.endswith("\n")


def test_auth_login_qr_missing_unikey_and_ascii(monkeypatch):
    def mock_run_musicbox(args, timeout=30.0):
        return 0, '{"ok": true, "data": {}}', ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/login/qr")
        assert resp.status_code == 502
        rj = resp.json()
        assert rj["error"] == "upstream_error"
        assert "Missing unikey or qr_ascii" in rj["stderr"]


def test_auth_qr_png_endpoint_and_alias(monkeypatch):
    def mock_run_musicbox(args, timeout=30.0):
        assert args == ["auth", "login", "--no-wait", "--json"]
        stdout = '{"ok": true, "data": {"unikey": "png-test-key"}}'
        return 0, stdout, ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        # Test original endpoint /api/v1/auth/login/qr.png
        resp1 = client.get("/api/v1/auth/login/qr.png")
        assert resp1.status_code == 200
        assert resp1.headers["content-type"] == "image/png"
        assert resp1.content[:8] == b"\x89PNG\r\n\x1a\n"

        # Test alias endpoint /api/v1/auth/qr.png
        resp2 = client.get("/api/v1/auth/qr.png")
        assert resp2.status_code == 200
        assert resp2.headers["content-type"] == "image/png"
        assert resp2.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_run_musicbox_missing_binary(monkeypatch):
    """彻底解析不到时必须退出码 127，并给出「去哪找过」的可读诊断。"""
    monkeypatch.setenv("PATH", "")
    code, stdout, stderr = runner.run_musicbox(["health"])
    assert code == 127
    assert "musicbox CLI not found" in stderr
    assert "tried:" in stderr, "必须列出尝试过的路径，否则无从排查"
    assert "console script next to the interpreter" in stderr


def test_run_musicbox_calls_ensure_xdg_dirs(monkeypatch):
    called = []
    monkeypatch.setattr(runner, "ensure_xdg_dirs", lambda: called.append(True))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: runner.subprocess.CompletedProcess([], 0, "ok", ""))
    runner.run_musicbox(["version"])
    assert len(called) == 1


def test_musicbox_search_filters_unplayable_songs(monkeypatch):
    import netease_ext

    # Mock CLI search output: 4 items (free, vip-no-url, trial, empty-url)
    mock_search_data = {
        "ok": True,
        "data": [
            {"song_id": 101, "song_name": "Free Song", "artist": "Singer", "quality": "exhigh"},
            {"song_id": 102, "song_name": "VIP Song No URL", "artist": "Singer", "quality": "lossless"},
            {"song_id": 103, "song_name": "Trial Snippet Song", "artist": "Singer", "quality": "standard"},
            {"song_id": 104, "song_name": "Dead Song", "artist": "Singer", "quality": "standard"},
        ],
    }

    def mock_run_musicbox(args, timeout=30.0):
        import json
        return 0, json.dumps(mock_search_data), ""

    # Mock songs_url responses
    def mock_songs_url(ids):
        return [
            {"id": 101, "url": "http://audio.126.net/101.mp3", "code": 200, "fee": 0, "freeTrialInfo": None},
            {"id": 102, "url": None, "code": 404, "fee": 1, "freeTrialInfo": None},
            {"id": 103, "url": "http://audio.126.net/103_trial.mp3", "code": 200, "fee": 1, "freeTrialInfo": {"start": 0, "end": 30}},
            {"id": 104, "url": "", "code": 200, "fee": 0, "freeTrialInfo": None},
        ]

    class MockApi:
        def songs_url(self, ids):
            return mock_songs_url(ids)

        def get_account_info(self):
            return {"code": 200, "account": None, "profile": None}

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    monkeypatch.setattr(netease_ext, "_get_api", lambda: MockApi())

    with TestClient(app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "test", "type": "song", "limit": 20})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Only 101 is playable
        assert len(data["data"]) == 1
        assert data["data"][0]["song_id"] == 101


def test_musicbox_songs_detail_filters_unplayable(monkeypatch):
    import netease_ext

    def mock_songs_detail(ids):
        return [
            {"id": 101, "name": "Free Song", "ar": [{"name": "A"}], "al": {"name": "Album", "picUrl": "http://img/1.jpg"}, "dt": 200000},
            {"id": 102, "name": "VIP Song", "ar": [{"name": "A"}], "al": {"name": "Album", "picUrl": "http://img/2.jpg"}, "dt": 200000},
            {"id": 103, "name": "Trial Song", "ar": [{"name": "A"}], "al": {"name": "Album", "picUrl": "http://img/3.jpg"}, "dt": 30000},
        ]

    def mock_songs_url(ids):
        return [
            {"id": 101, "url": "http://audio.126.net/101.mp3", "code": 200, "fee": 0, "freeTrialInfo": None},
            {"id": 102, "url": None, "code": 404, "fee": 1, "freeTrialInfo": None},
            {"id": 103, "url": "http://audio.126.net/103_trial.mp3", "code": 200, "fee": 1, "freeTrialInfo": {"start": 0, "end": 30}},
        ]

    class MockApi:
        def songs_detail(self, ids):
            return mock_songs_detail(ids)

        def songs_url(self, ids):
            return mock_songs_url(ids)

        def get_account_info(self):
            return {"code": 200, "account": None, "profile": None}

    monkeypatch.setattr(netease_ext, "_get_api", lambda: MockApi())

    with TestClient(app) as client:
        resp = client.get("/api/v1/songs/detail", params={"ids": "101,102,103"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Only 101 is returned
        assert len(data["data"]) == 1
        assert data["data"][0]["song_id"] == 101


def test_musicbox_search_logged_in_vip_playable(monkeypatch):
    import netease_ext

    mock_search_data = {
        "ok": True,
        "data": [
            {"song_id": 201, "song_name": "VIP Song With Perm", "artist": "Singer", "quality": "lossless"},
            {"song_id": 202, "song_name": "Paid Album Without Perm", "artist": "Singer", "quality": "lossless"},
        ],
    }

    def mock_run_musicbox(args, timeout=30.0):
        import json
        return 0, json.dumps(mock_search_data), ""

    def mock_songs_url(ids):
        return [
            # 201 has full valid url and no trial
            {"id": 201, "url": "http://audio.126.net/vip_full.mp3", "code": 200, "fee": 1, "freeTrialInfo": None},
            # 202 has no url (account didn't buy album)
            {"id": 202, "url": None, "code": 404, "fee": 4, "freeTrialInfo": None},
        ]

    class MockApi:
        def songs_url(self, ids):
            return mock_songs_url(ids)

        def get_account_info(self):
            # Logged in
            return {"code": 200, "account": {"id": 12345}, "profile": {"nickname": "VIPUser"}}

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    monkeypatch.setattr(netease_ext, "_get_api", lambda: MockApi())

    with TestClient(app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "test", "type": "song", "limit": 20})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["song_id"] == 201




# ---------------------------------------------------------------------------
# v2.0 新增接口：/api/v1/auth/detail 与 /api/v1/recommend/daily
# ---------------------------------------------------------------------------

import app as mb_app


def test_auth_detail_returns_login_and_vip_info(monkeypatch):
    monkeypatch.setattr(
        mb_app, "ne_auth_detail",
        lambda: {"logged_in": True, "nickname": "张三", "user_id": "10086",
                 "vip_type": 11, "vip_expires_ms": 1800000000000},
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/detail")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["data"]["logged_in"] is True
        assert body["data"]["nickname"] == "张三"
        assert body["data"]["vip_type"] == 11
        assert body["data"]["vip_expires_ms"] == 1800000000000


def test_auth_detail_logged_out(monkeypatch):
    monkeypatch.setattr(
        mb_app, "ne_auth_detail",
        lambda: {"logged_in": False, "nickname": "", "user_id": "", "vip_type": 0,
                 "vip_expires_ms": 0},
    )
    with TestClient(app) as client:
        body = client.get("/api/v1/auth/detail").json()
        assert body["ok"] is True
        assert body["data"]["logged_in"] is False


def test_auth_detail_never_returns_500_on_upstream_error(monkeypatch):
    """账号信息读取异常必须降级成"未登录"，不能让整个音源服务 500。"""

    def boom():
        raise RuntimeError("NEMbox 内部炸了")

    monkeypatch.setattr(mb_app, "ne_auth_detail", boom)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/detail")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["data"]["logged_in"] is False
        assert "error" in body["data"]


def test_auth_detail_error_message_is_truncated(monkeypatch):
    def boom():
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr(mb_app, "ne_auth_detail", boom)
    with TestClient(app) as client:
        body = client.get("/api/v1/auth/detail").json()
        assert len(body["data"]["error"]) <= 200


_DAILY_ROWS = [
    {"song_id": 1, "song_name": "晴天", "artist": "周杰伦", "album_name": "叶惠美",
     "duration": 269, "quality": "SQ", "mp3_url": "http://m1"},
    {"song_id": 2, "song_name": "VIP曲", "artist": "歌手", "album_name": "专辑",
     "duration": 200, "quality": "LD", "mp3_url": ""},
    {"song_id": 3, "song_name": "夜曲", "artist": "周杰伦", "album_name": "十一月的萧邦",
     "duration": 226, "quality": "HR", "mp3_url": "http://m3"},
]


def test_recommend_daily_success(monkeypatch):
    seen = {}

    def mock_run_musicbox(args, timeout=30.0):
        seen["args"] = args
        seen["timeout"] = timeout
        return 0, __import__("json").dumps({"ok": True, "data": _DAILY_ROWS}), ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    monkeypatch.setattr(mb_app, "filter_playable_song_ids", lambda ids: {1, 3})

    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily", params={"limit": 20})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # 不可播的 VIP 曲被过滤掉
        assert [s["song_id"] for s in body["data"]] == [1, 3]
        assert body["data"][0]["song_name"] == "晴天"

    assert seen["args"] == ["recommend", "songs", "--limit", "20", "--json"]
    assert seen["timeout"] == 40.0


def test_recommend_daily_not_logged_in(monkeypatch):
    """musicbox CLI 退出码 3 = 未登录，必须如实上报而不是 502。"""
    def mock_run_musicbox(args, timeout=30.0):
        return 3, "", "not logged in"

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"ok": False, "error": "not_logged_in", "data": []}


def test_recommend_daily_exit_code_matches_constant():
    assert mb_app.CLI_EXIT_NOT_LOGGED_IN == 3


def test_recommend_daily_timeout_is_504(monkeypatch):
    def mock_run_musicbox(args, timeout=30.0):
        raise runner.MusicboxTimeoutError("musicbox timed out after 40s")

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
        assert resp.status_code == 504
        assert resp.json()["ok"] is False
        assert resp.json()["error"] == "timeout"


@pytest.mark.parametrize("code", [1, 2, 4, 5, 10, 127])
def test_recommend_daily_other_exit_codes_are_502(monkeypatch, code):
    monkeypatch.setattr(runner, "run_musicbox", lambda args, timeout=30.0: (code, "", "boom"))
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
        assert resp.status_code == 502
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "upstream_error"
        assert body["exit_code"] == code


def test_recommend_daily_bad_json_is_502(monkeypatch):
    monkeypatch.setattr(runner, "run_musicbox", lambda args, timeout=30.0: (0, "不是 JSON", ""))
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
        assert resp.status_code == 502
        assert resp.json()["error"] == "bad_upstream_json"


def test_recommend_daily_honors_limit(monkeypatch):
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda args, timeout=30.0: (0, __import__("json").dumps(
                            {"ok": True, "data": _DAILY_ROWS}), ""))
    monkeypatch.setattr(mb_app, "filter_playable_song_ids", lambda ids: {1, 2, 3})
    with TestClient(app) as client:
        assert len(client.get("/api/v1/recommend/daily", params={"limit": 1}).json()["data"]) == 1
        assert len(client.get("/api/v1/recommend/daily", params={"limit": 2}).json()["data"]) == 2


@pytest.mark.parametrize("limit", [0, -1, 101, 999])
def test_recommend_daily_rejects_out_of_range_limit(limit):
    """越界 limit 被拒。本服务用自定义 RequestValidationError handler 统一返回 400
    （而非 FastAPI 默认的 422），与 /api/v1/search 的表现保持一致。"""
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily", params={"limit": limit})
        assert resp.status_code == 400
        assert resp.json()["detail"], "必须带可读的校验详情"


@pytest.mark.parametrize("limit", [1, 100])
def test_recommend_daily_accepts_boundary_limits(monkeypatch, limit):
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda args, timeout=30.0: (0, __import__("json").dumps(
                            {"ok": True, "data": []}), ""))
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily", params={"limit": limit})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "data": []}


def test_recommend_daily_tolerates_malformed_rows(monkeypatch):
    """上游返回混入 null / 字符串 / 缺字段行时不崩，只保留合法曲目。"""
    rows = [None, "junk", 42, {"song_name": "无 id"}, _DAILY_ROWS[0]]
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda args, timeout=30.0: (0, __import__("json").dumps(
                            {"ok": True, "data": rows}), ""))
    monkeypatch.setattr(mb_app, "filter_playable_song_ids", lambda ids: {1})
    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
        assert body["ok"] is True
        assert len(body["data"]) == 1
        assert body["data"][0]["song_id"] == 1


def test_recommend_daily_default_limit_is_20(monkeypatch):
    """不传 limit 时按 20 请求上游（CLI --limit 20），且不因缺参报错。"""
    seen = {}

    def mock_run_musicbox(args, timeout=30.0):
        seen["args"] = args
        return 0, __import__("json").dumps({"ok": True, "data": []}), ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
        assert resp.status_code == 200
    assert seen["args"] == ["recommend", "songs", "--limit", "20", "--json"]


# ===========================================================================
# runner: musicbox CLI 解析（v1.x 遗留缺陷的回归测试）
#
# 事故现场：服务用绝对路径的 venv uvicorn 启动（.venv-musicbox/bin/uvicorn ...），
# venv 的 bin/ 不在 PATH 上；而 runner 用裸命令名 ["musicbox", ...] 起子进程，
# 于是 FileNotFoundError -> 退出码 127 -> 12 个走 CLI 的端点全部 502，
# 但 /healthz 仍返回 200，看起来"服务是好的"。
# ===========================================================================

import runner as mb_runner


@pytest.fixture
def fake_venv(tmp_path, monkeypatch):
    """造一个 <venv>/bin/{python,musicbox} 布局，并保证 PATH 里没有它。"""
    venv = tmp_path / "venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    fake_python = bindir / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    cli = bindir / "musicbox"
    cli.write_text('#!/bin/sh\necho "{\\"ok\\": true, \\"via\\": \\"venv-cli\\"}"\n',
                   encoding="utf-8")
    cli.chmod(0o755)

    # 关键：PATH 里刻意不含 venv/bin，复现真实故障条件
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(mb_runner.sys, "executable", str(fake_python))
    monkeypatch.setattr(mb_runner.sys, "prefix", str(venv))
    monkeypatch.setattr(mb_runner.sys, "base_prefix", "/usr")
    mb_runner.reset_cmd_cache()
    yield venv, cli
    mb_runner.reset_cmd_cache()


def test_run_musicbox_fails_without_cli_on_path(monkeypatch, tmp_path):
    """先证明故障条件成立：PATH 里没有 musicbox 时裸命令名必然失败。"""
    monkeypatch.setenv("PATH", str(tmp_path))          # 空目录
    monkeypatch.setattr(mb_runner.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(mb_runner.sys, "prefix", "/usr")
    monkeypatch.setattr(mb_runner.sys, "base_prefix", "/usr")
    mb_runner.reset_cmd_cache()
    try:
        cmd, how = mb_runner.resolve_musicbox_cmd()
        if not cmd:
            assert how.startswith("not_found:")
            code, out, err = mb_runner.run_musicbox(["--version"])
            assert code == 127
            assert "not found" in err and "tried:" in err, "失败原因必须可读"
    finally:
        mb_runner.reset_cmd_cache()


def test_resolves_cli_next_to_interpreter_when_not_on_path(fake_venv):
    """核心回归：venv/bin 不在 PATH 上时，仍必须从解释器同目录解析到 CLI。"""
    venv, cli = fake_venv
    cmd, how = mb_runner.resolve_musicbox_cmd()
    assert cmd == [str(cli)], f"应解析到 venv 内的 CLI，实际 {cmd} ({how})"
    assert how.startswith("absolute:")


def test_run_musicbox_actually_executes_resolved_cli(fake_venv):
    venv, cli = fake_venv
    code, out, err = mb_runner.run_musicbox(["--version"])
    assert code == 0, f"stderr={err}"
    assert "venv-cli" in out


def test_child_path_includes_venv_bin(fake_venv):
    """子进程 PATH 也要带 venv/bin：CLI 可能自己再派生子进程。"""
    venv, _cli = fake_venv
    env = mb_runner.get_clean_env()
    assert env["PATH"].startswith(str(venv / "bin") + mb_runner.os.pathsep)


def test_child_env_strips_proxy_vars(fake_venv, monkeypatch):
    """网易云需直连，代理变量必须剥离（国内网络下走代理会拿到空结果）。"""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "no_proxy"):
        monkeypatch.setenv(k, "http://proxy.invalid:8080")
    env = mb_runner.get_clean_env()
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "no_proxy"):
        assert k not in env


def test_cmd_resolution_is_cached(fake_venv):
    venv, cli = fake_venv
    first = mb_runner.musicbox_cmd()
    assert first[0] == [str(cli)]
    cli.unlink()                      # 删掉文件
    assert mb_runner.musicbox_cmd() == first, "解析结果应缓存，不随每次请求重扫文件系统"
    mb_runner.reset_cmd_cache()
    after = mb_runner.musicbox_cmd()
    assert after[0] != [str(cli)], "清缓存后重新解析才会发现该路径已失效"


def test_resolve_falls_back_to_which(tmp_path, monkeypatch):
    bindir = tmp_path / "globalbin"
    bindir.mkdir()
    cli = bindir / "musicbox"
    cli.write_text("#!/bin/sh\necho which-ok\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(mb_runner.sys, "executable", "/nonexistent/python")
    monkeypatch.setattr(mb_runner.sys, "prefix", "/nonexistent/prefix")
    monkeypatch.setattr(mb_runner.sys, "base_prefix", "/nonexistent/base")
    mb_runner.reset_cmd_cache()
    try:
        cmd, how = mb_runner.resolve_musicbox_cmd()
        assert cmd == [str(cli)] and how.startswith("which:")
    finally:
        mb_runner.reset_cmd_cache()


def test_candidate_paths_dedupe_and_cover_scripts_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(mb_runner.sys, "executable", str(tmp_path / "py"))
    monkeypatch.setattr(mb_runner.sys, "prefix", str(tmp_path / "p"))
    monkeypatch.setattr(mb_runner.sys, "base_prefix", str(tmp_path / "p"))
    paths = mb_runner.candidate_paths()
    assert len(paths) == len(set(paths)), "候选路径不该有重复"
    assert any(p.endswith(os.sep + "musicbox") for p in paths)
    assert any("Scripts" in p for p in paths), "应兼容 Windows/Scripts 布局"


# ---------------------------------------------------------------- selftest ----

def test_selftest_endpoint_reports_resolution(monkeypatch):
    import netease_ext  # noqa: F401

    monkeypatch.setattr(mb_runner, "musicbox_cmd",
                        lambda: (["/venv/bin/musicbox"], "absolute:/venv/bin/musicbox"))
    monkeypatch.setattr(mb_runner, "run_musicbox", lambda a, timeout=30.0: (0, "1.2.3", ""))
    with TestClient(app) as client:
        body = client.get("/api/v1/selftest").json()
    assert body["ok"] is True
    d = body["data"]
    assert d["cli_found"] is True
    assert d["cli_cmd"] == ["/venv/bin/musicbox"]
    assert d["resolved_by"] == "absolute:/venv/bin/musicbox"
    assert d["cli_exec_ok"] is True
    assert d["venv_bin_dir"] and d["interpreter"]
    assert d["xdg"].get("XDG_DATA_HOME") is not None


def test_selftest_reports_missing_cli(monkeypatch):
    monkeypatch.setattr(mb_runner, "musicbox_cmd",
                        lambda: ([], "not_found:/a/musicbox,/b/musicbox"))
    with TestClient(app) as client:
        body = client.get("/api/v1/selftest").json()
    d = body["data"]
    assert d["cli_found"] is False
    assert d["cli_exec_ok"] is False
    assert d["cli_cmd"] == []
    # healthz 与 selftest 的分歧正是当初的迷惑点，必须能被一眼看出
    assert client.get("/healthz").json()["status"] == "ok"


def test_selftest_survives_cli_hanging(monkeypatch):
    def slow(a, timeout=30.0):
        raise mb_runner.MusicboxTimeoutError("musicbox timed out after 15s")

    monkeypatch.setattr(mb_runner, "musicbox_cmd", lambda: (["/venv/bin/musicbox"], "absolute:x"))
    monkeypatch.setattr(mb_runner, "run_musicbox", slow)
    with TestClient(app) as client:
        body = client.get("/api/v1/selftest").json()
    assert body["ok"] is True
    assert body["data"]["cli_exec_ok"] is False
    assert "timeout" in body["data"]["cli_exec_detail"]
