import importlib.util
import io
import json
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


def test_run_musicbox_missing_binary(monkeypatch, tmp_path):
    """彻底解析不到时必须退出码 127，并给出「去哪找过」的可读诊断。"""
    monkeypatch.setenv("PATH", "")
    # 本用例要验证的是「解析器什么都找不到」这条分支。装了真实 NEMbox 的环境里
    # 另外两级都会命中（那是正确行为，不是这里要测的）：
    #   1) candidate_paths() 是解释器相对路径，PATH="" 拦不住，venv/bin/musicbox 会被找到
    #   2) 模块回退 python -m NEMbox 也能跑起来
    # 两者都会让 CLI 真的执行并返回 2，因此必须一并封掉。
    empty = tmp_path / "no-cli-here"
    empty.mkdir()
    monkeypatch.setattr(runner, "candidate_paths",
                        lambda: [str(empty / "musicbox")])
    monkeypatch.setattr(runner, "module_fallback", lambda: "NoSuchModule_xyz_123")
    runner.reset_cmd_cache()          # run_musicbox 走 musicbox_cmd()，会读缓存
    try:
        code, stdout, stderr = runner.run_musicbox(["health"])
        assert code == 127
        assert "musicbox CLI not found" in stderr
        assert "tried:" in stderr, "必须列出尝试过的路径，否则无从排查"
        assert "console script next to the interpreter" in stderr
    finally:
        runner.reset_cmd_cache()


def test_run_musicbox_calls_ensure_xdg_dirs(monkeypatch):
    called = []
    monkeypatch.setattr(runner, "ensure_xdg_dirs", lambda: called.append(True))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: runner.subprocess.CompletedProcess([], 0, "ok", ""))
    runner.run_musicbox(["version"])
    assert len(called) == 1


def test_musicbox_search_filters_unplayable_songs(monkeypatch):
    """搜索结果按真实可播状态逐首过滤（进程内实现，engine 标注）。"""
    rows = [
        {"song_id": 201, "song_name": "可播", "artist": "A", "album_name": "X",
         "duration": 200, "quality": "LOSSLESS FLAC", "mp3_url": "http://m/1"},
        {"song_id": 202, "song_name": "不可播", "artist": "B", "album_name": "Y",
         "duration": 210, "quality": "LD 128k", "mp3_url": ""},
    ]
    monkeypatch.setattr(mb_app, "ne_search_songs", lambda kw, limit=50: [rows[0]])
    with TestClient(app) as client:
        data = client.get("/api/v1/search",
                          params={"keyword": "test", "type": "song", "limit": 20}).json()
    assert data["ok"] is True
    assert data["engine"] == "in-process"
    assert len(data["data"]) == 1
    assert data["data"][0]["song_id"] == 201


def test_musicbox_search_falls_back_to_cli(monkeypatch):
    """进程内实现抛异常时回退 CLI，至少不比原来差，且如实标注来源。"""
    def boom(kw, limit=50):
        raise RuntimeError("NEMbox 内部炸了")

    monkeypatch.setattr(mb_app, "ne_search_songs", boom)

    def mock_run_musicbox(args, timeout=30.0):
        return 0, json.dumps({"ok": True, "data": [
            {"song_id": 301, "song_name": "CLI 结果", "artist": "C", "album_name": "Z",
             "duration": 180, "quality": "LD 128k"}]}), ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    with TestClient(app) as client:
        data = client.get("/api/v1/search",
                          params={"keyword": "test", "type": "song", "limit": 20}).json()
    assert data["engine"] == "cli-fallback"
    assert len(data["data"]) == 1


def test_musicbox_search_non_song_types_still_use_cli(monkeypatch):
    """album/artist/playlist 没有进程内实现，继续走 CLI。"""
    seen = {}

    def mock_run_musicbox(args, timeout=30.0):
        seen["args"] = args
        return 0, json.dumps({"ok": True, "data": [{"id": 1}]}), ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    monkeypatch.setattr(mb_app, "ne_search_songs",
                        lambda kw, limit=50: pytest.fail("非 song 类型不该走进程内搜索"))
    with TestClient(app) as client:
        client.get("/api/v1/search", params={"keyword": "k", "type": "album", "limit": 5})
    assert seen["args"][:2] == ["search", "k"]
    assert "--type" in seen["args"] and "album" in seen["args"]


def test_musicbox_search_logged_in_vip_playable(monkeypatch):
    """VIP 账号登录后，VIP/无损曲目照常返回（音源就是账号自身权益）。"""
    rows = [{"song_id": 401, "song_name": "VIP曲", "artist": "D", "album_name": "V",
             "duration": 260, "quality": "HIRES FLAC", "album_pic_url": "http://pic/401",
             "has_sq": False, "has_hr": True, "mp3_url": "http://m/401"}]
    monkeypatch.setattr(mb_app, "ne_search_songs", lambda kw, limit=50: rows)
    with TestClient(app) as client:
        data = client.get("/api/v1/search",
                          params={"keyword": "vip", "type": "song", "limit": 20}).json()
    assert len(data["data"]) == 1
    assert data["data"][0]["quality"] == "HIRES FLAC"


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


_DAILY_INPROC = [
    {"song_id": 1, "song_name": "晴天", "artist": "周杰伦", "album_name": "叶惠美",
     "duration": 269, "quality": "LOSSLESS FLAC", "album_pic_url": "http://pic/1",
     "mp3_url": "http://m/1"},
    {"song_id": 3, "song_name": "夜曲", "artist": "周杰伦", "album_name": "十一月的萧邦",
     "duration": 226, "quality": "HD 320k", "album_pic_url": "http://pic/3",
     "mp3_url": "http://m/3"},
]


def _login(monkeypatch, value=True):
    monkeypatch.setattr(mb_app, "ne_check_is_logged_in", lambda: value)


def test_recommend_daily_success_in_process(monkeypatch):
    """主路径：进程内抓取，不经过 CLI/dig_info。"""
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: list(_DAILY_INPROC))
    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily", params={"limit": 20}).json()
    assert body["ok"] is True
    assert body["engine"] == "in-process"
    assert [s["song_id"] for s in body["data"]] == [1, 3]
    assert body["data"][0]["album_pic_url"] == "http://pic/1"


def test_recommend_daily_not_logged_in(monkeypatch):
    """未登录如实上报，且绝不把上游的热门填充当日推返回。"""
    _login(monkeypatch, False)
    monkeypatch.setattr(mb_app, "ne_daily_songs",
                        lambda limit=20: pytest.fail("未登录不该去抓日推"))
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "error": "not_logged_in", "data": []}


def test_recommend_daily_falls_back_to_cli(monkeypatch):
    """进程内返回空 → 回退 CLI，并如实标注来源。"""
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: [])
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda args, timeout=30.0: (0, json.dumps(
                            {"ok": True, "data": _DAILY_INPROC}), ""))
    monkeypatch.setattr(mb_app, "filter_playable_song_ids", lambda ids: {1, 3})
    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body["ok"] is True and body["engine"] == "cli-fallback"
    assert len(body["data"]) == 2


def test_recommend_daily_cli_exit3_maps_to_not_logged_in(monkeypatch):
    """CLI 退出码 3 = 未登录，必须映射成 not_logged_in 而不是 502。"""
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: None)
    monkeypatch.setattr(runner, "run_musicbox", lambda a, timeout=30.0: (3, "", "not logged in"))
    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body == {"ok": False, "error": "not_logged_in", "data": []}
    assert mb_app.CLI_EXIT_NOT_LOGGED_IN == 3


def test_recommend_daily_cli_timeout_is_504(monkeypatch):
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: [])
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda a, timeout=30.0: (_ for _ in ()).throw(
                            runner.MusicboxTimeoutError("timed out")))
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
        assert resp.status_code == 504
        assert resp.json()["error"] == "timeout"


@pytest.mark.parametrize("code", [1, 2, 4, 5, 10, 127])
def test_recommend_daily_cli_other_exit_codes_are_502(monkeypatch, code):
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: [])
    monkeypatch.setattr(runner, "run_musicbox", lambda a, timeout=30.0: (code, "", "boom"))
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily")
        assert resp.status_code == 502
        assert resp.json()["exit_code"] == code


def test_recommend_daily_cli_bad_json_is_502(monkeypatch):
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: [])
    monkeypatch.setattr(runner, "run_musicbox", lambda a, timeout=30.0: (0, "不是 JSON", ""))
    with TestClient(app) as client:
        assert client.get("/api/v1/recommend/daily").status_code == 502


def test_recommend_daily_both_empty_reports_note(monkeypatch):
    """两条路都拿不到时如实说明，不要伪装成功。"""
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: [])
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda a, timeout=30.0: (0, json.dumps({"ok": True, "data": []}), ""))
    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body["ok"] is True and body["data"] == []
    assert "note" in body, "空结果必须附带原因说明"


def test_recommend_daily_honors_limit(monkeypatch):
    _login(monkeypatch, True)
    seen = {}

    def fake(limit=20):
        seen["limit"] = limit
        return list(_DAILY_INPROC)

    monkeypatch.setattr(mb_app, "ne_daily_songs", fake)
    with TestClient(app) as client:
        assert len(client.get("/api/v1/recommend/daily",
                              params={"limit": 1}).json()["data"]) == 1
    assert seen["limit"] == 1


@pytest.mark.parametrize("limit", [0, -1, 101, 999])
def test_recommend_daily_rejects_out_of_range_limit(limit):
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily", params={"limit": limit})
        assert resp.status_code == 400


@pytest.mark.parametrize("limit", [1, 100])
def test_recommend_daily_accepts_boundary_limits(monkeypatch, limit):
    _login(monkeypatch, True)
    seen = {}

    def fake(limit=20):
        seen["limit"] = limit
        return list(_DAILY_INPROC)

    monkeypatch.setattr(mb_app, "ne_daily_songs", fake)
    with TestClient(app) as client:
        resp = client.get("/api/v1/recommend/daily", params={"limit": limit})
        assert resp.status_code == 200
        # limit 是上限而非目标：上游只给 2 首时就返回 2 首
        assert len(resp.json()["data"]) == min(limit, len(_DAILY_INPROC))
    assert seen["limit"] == limit


def test_recommend_daily_truncates_overlimit_rows(monkeypatch):
    _login(monkeypatch, True)
    rows = [dict(r, song_id=1000 + i) for i, r in enumerate(_DAILY_INPROC * 20)]
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: rows)
    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily", params={"limit": 3}).json()
    assert len(body["data"]) == 3, "端点必须按 limit 截断，不能把上游给的量原样吐出去"


def test_recommend_daily_passes_through_inprocess_rows(monkeypatch):
    """逐首过滤是 ne_daily_songs 的职责，端点只按 limit 截断并原样返回。"""
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs", lambda limit=20: list(_DAILY_INPROC))
    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body["ok"] is True
    assert body["engine"] == "in-process"
    assert body["data"] == _DAILY_INPROC


# ===========================================================================
# netease_ext 逐首过滤：上游 dig_info「全或无」缺陷的回归测试
#
# NEMbox 的 dig_info 在任意一首歌取不到直链时会 `return []`，把整个结果集清空，
# HTTP 仍是 200。这就是"搜不到任何网易云在线歌曲"和"每日推荐歌单不出现"的共同根因。
# 本项目的 search_songs / daily_songs 必须逐首判定，坏数据只影响它自己那一首。
# ===========================================================================

import netease_ext as ne


class _FakeApi:
    """最小 NEMbox 替身：只实现本项目用到的 4 个方法。"""

    def __init__(self, songs, urls, logged_in=True):
        self._songs = songs
        self._urls = urls
        self._logged_in = logged_in
        self.calls = []

    def search(self, keywords, stype=1, offset=0, total="true", limit=50):
        self.calls.append(("search", keywords, limit))
        return {"songs": self._songs}

    def recommend_playlist(self, total=True, offset=0, limit=20):
        self.calls.append(("recommend", limit))
        return self._songs[:limit] if limit else self._songs

    def songs_url(self, ids):
        self.calls.append(("songs_url", list(ids)))
        return [self._urls[i] for i in ids if i in self._urls]

    def get_account_info(self):
        self.calls.append(("account_info",))
        if not self._logged_in:
            return {"account": None, "profile": None}
        return {"account": {"id": 1}, "profile": {"nickname": "n"}}


@pytest.fixture
def netease_api(monkeypatch):
    ne.invalidate_login_cache()
    ne._api_instance = None

    def install(api):
        monkeypatch.setattr(ne, "_get_api", lambda: api)
        ne.invalidate_login_cache()
        return api

    yield install
    ne.invalidate_login_cache()
    ne._api_instance = None


_SONGS = [
    {"id": 1, "name": "好歌", "ar": [{"name": "甲"}], "al": {"id": 10, "name": "专辑一",
     "picUrl": "http://pic/1"}, "dt": 269000, "sq": {"size": 30000000}, "fee": 1},
    {"id": 2, "name": "坏歌（无直链）", "ar": [{"name": "乙"}], "al": {"id": 11, "name": "专辑二",
     "picUrl": "http://pic/2"}, "dt": 200000, "fee": 1},
    {"id": 3, "name": "试听片段", "ar": [{"name": "丙"}], "al": {"id": 12, "name": "专辑三",
     "picUrl": "http://pic/3"}, "dt": 30000, "fee": 1},
    {"id": 4, "name": "另一首好歌", "ar": [{"name": "丁"}], "al": {"id": 13, "name": "专辑四",
     "picUrl": "http://pic/4"}, "dt": 180000, "h": {"size": 4000000, "br": 320000}, "fee": 0},
    {"name": "无 id 的畸形行", "ar": [], "al": {}, "dt": 0},
    "完全不是 dict",
]

_URLS = {
    1: {"id": 1, "url": "http://cdn/1.flac", "br": 999000, "type": "flac",
        "level": "lossless", "fee": 1},
    2: {"id": 2, "url": None, "code": 404, "fee": 1},          # 无直链
    3: {"id": 3, "url": "http://cdn/3.mp3", "freeTrialInfo": {"st": 0}, "fee": 1},  # 试听
    4: {"id": 4, "url": "http://cdn/4.mp3", "br": 320000, "type": "mp3",
        "level": "exhigh", "fee": 0},
}


def test_search_songs_keeps_good_tracks_when_one_is_bad(netease_api):
    """核心回归：一首坏数据绝不能清空整个结果集（上游 dig_info 的行为）。"""
    api = netease_api(_FakeApi(_SONGS, _URLS, logged_in=True))
    rows = ne.search_songs("任意关键词", limit=50)
    ids = [r["song_id"] for r in rows]
    assert ids == [1, 4], f"应只剔除无直链与试听的曲目，实际 {ids}"
    assert 2 not in ids and 3 not in ids
    assert api.calls[0][0] == "search"


def test_search_songs_result_shape_is_proxy_compatible(netease_api):
    netease_api(_FakeApi(_SONGS, _URLS, logged_in=True))
    row = ne.search_songs("kw", limit=50)[0]
    assert row["song_name"] == "好歌"
    assert row["artist"] == "甲"
    assert row["album_name"] == "专辑一"
    assert row["duration"] == 269, "duration 必须是秒（与 CLI song_info 一致）"
    assert row["album_pic_url"] == "http://pic/1"
    assert row["mp3_url"].startswith("http://cdn/")
    assert row["has_sq"] is True
    # 音质字符串要与上游 Parse.song_url 同款词汇，代理层的无损判定才认得
    assert "LOSSLESS" in row["quality"].upper() or "FLAC" in row["quality"].upper()


def test_daily_songs_keeps_good_tracks_when_one_is_bad(netease_api):
    api = netease_api(_FakeApi(_SONGS, _URLS, logged_in=True))
    rows = ne.daily_songs(limit=20)
    assert [r["song_id"] for r in rows] == [1, 4]
    assert api.calls[0][0] == "recommend"


def test_daily_songs_respects_limit(netease_api):
    netease_api(_FakeApi(_SONGS, _URLS, logged_in=True))
    assert len(ne.daily_songs(limit=1)) == 1


def test_songs_all_bad_yields_empty_not_crash(netease_api):
    """全部都是坏数据时返回空列表即可，但不能抛异常。"""
    netease_api(_FakeApi(_SONGS, {}, logged_in=True))
    assert ne.search_songs("kw") == []
    assert ne.daily_songs() == []


def test_logged_out_only_free_tracks(netease_api):
    """未登录：fee 非免费的一律剔除，即使碰巧拿到了直链。"""
    netease_api(_FakeApi(_SONGS, _URLS, logged_in=False))
    ids = [r["song_id"] for r in ne.search_songs("kw")]
    assert ids == [4], f"未登录只应保留免费曲目，实际 {ids}"


def test_logged_in_keeps_vip_tracks(netease_api):
    """已登录：VIP 曲目（fee=1）凭账号权益放行。"""
    netease_api(_FakeApi(_SONGS, _URLS, logged_in=True))
    assert 1 in [r["song_id"] for r in ne.search_songs("kw")]


def test_free_only_flag_can_disable_degradation(netease_api, monkeypatch):
    monkeypatch.setattr(ne, "FREE_ONLY_ON_LOGOUT", False)
    netease_api(_FakeApi(_SONGS, _URLS, logged_in=False))
    # 关闭降级后未登录不再做 fee 过滤（能否播完全由真实直链决定）
    ids = [r["song_id"] for r in ne.search_songs("kw")]
    assert 1 in ids


def test_login_state_is_cached(netease_api):
    """check_is_logged_in 每次都要打一次网易云接口，必须有 TTL 缓存。"""
    api = netease_api(_FakeApi(_SONGS, _URLS, logged_in=True))
    ne.search_songs("a")
    ne.search_songs("b")
    n = sum(1 for c in api.calls if c[0] == "account_info")
    assert n == 1, f"两次搜索只应查一次账号信息，实际 {n} 次"


def test_login_cache_invalidated_after_login(netease_api):
    api = netease_api(_FakeApi(_SONGS, _URLS, logged_in=False))
    assert ne.check_is_logged_in() is False
    api._logged_in = True                       # 用户刚扫码登录
    assert ne.check_is_logged_in() is False, "TTL 内应仍走缓存"
    ne.invalidate_login_cache()
    assert ne.check_is_logged_in() is True, "作废缓存后必须立刻反映新登录态"


def test_auth_state_probe_failure_does_not_poison_cache(netease_api):
    """探测异常时不能把"未登录"缓存一整个 TTL，否则 VIP 曲会被误杀 5 分钟。"""

    class _Boom(_FakeApi):
        def get_account_info(self):
            raise RuntimeError("网络抖动")

    api = netease_api(_Boom(_SONGS, _URLS))
    assert ne.check_is_logged_in() is False
    api2 = _FakeApi(_SONGS, _URLS, logged_in=True)
    monkey_ids = ["x"]
    # 下一次探测应能恢复，而不是被上一次的失败结果钉住
    ne._get_api = lambda: api2
    ne.invalidate_login_cache()
    assert ne.check_is_logged_in() is True
    del monkey_ids


def test_quality_of_matches_upstream_vocabulary():
    """音质字符串必须与上游 NEMbox Parse.song_url 用同一套词汇，
    否则代理层的无损判定（认 SQ/HR/无损）会对不上真实数据。"""
    assert ne.quality_of({"level": "lossless", "type": "flac"}) == "LOSSLESS FLAC"
    assert ne.quality_of({"level": "HIRES", "type": "flac"}) == "HIRES FLAC"
    assert ne.quality_of({"level": "jymaster", "type": "flac"}) == "JYMASTER FLAC"
    assert ne.quality_of({"type": "FLAC", "level": "exhigh"}) == "EXHIGH FLAC"
    assert ne.quality_of({"type": "FLAC"}) == "LOSSLESS FLAC"
    assert ne.quality_of({"br": 999000}) == "LOSSLESS"
    assert ne.quality_of({"br": 320000}) == "HD 320k"
    assert ne.quality_of({"br": 192000}) == "MD 192k"
    assert ne.quality_of({"br": 128000}) == "LD 128k"
    assert ne.quality_of({}) == "LD 128k"
    assert ne.quality_of({"br": "abc"}) == "LD 128k"


def test_proxy_lossless_detection_recognises_real_quality_strings():
    """端到端词汇一致性：netease_ext 产出的音质串，代理层必须能认出无损。

    这条守的是「上游真实数据 → 代理层格式判定」的契约。只按 SQ/HR 判定的话，
    真实的 "LOSSLESS FLAC" / "HIRES FLAC" 会被当成 mp3，用户永远拿不到无损格式声明。
    """
    from proxy import netease_items

    for url_info, expect_lossless in [
        ({"level": "lossless", "type": "flac"}, True),
        ({"level": "HIRES", "type": "flac"}, True),
        ({"level": "jymaster", "type": "flac"}, True),
        ({"type": "FLAC"}, True),
        ({"br": 999000}, True),
        ({"br": 320000}, False),
        ({"br": 128000}, False),
        ({}, False),
    ]:
        q = ne.quality_of(url_info)
        assert netease_items.has_lossless({"quality": q}) is expect_lossless, (url_info, q)

    # CLI 时代的旧词汇也要继续认得
    for legacy in ("SQ 2.4M", "HR 1.9G", "无损", "SQ", "HR"):
        assert netease_items.has_lossless({"quality": legacy}) is True, legacy


def test_recommend_daily_default_limit_is_20(monkeypatch):
    """不传 limit 时按 20 请求。"""
    _login(monkeypatch, True)
    seen = {}

    def fake(limit=20):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(mb_app, "ne_daily_songs", fake)
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda a, timeout=30.0: (0, json.dumps({"ok": True, "data": []}), ""))
    with TestClient(app) as client:
        assert client.get("/api/v1/recommend/daily").status_code == 200
    assert seen["limit"] == 20


def test_recommend_daily_survives_inprocess_exception(monkeypatch):
    _login(monkeypatch, True)
    monkeypatch.setattr(mb_app, "ne_daily_songs",
                        lambda limit=20: (_ for _ in ()).throw(RuntimeError("上游炸了")))
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda a, timeout=30.0: (0, json.dumps(
                            {"ok": True, "data": _DAILY_INPROC}), ""))
    monkeypatch.setattr(mb_app, "filter_playable_song_ids", lambda ids: {1, 3})
    with TestClient(app) as client:
        body = client.get("/api/v1/recommend/daily").json()
    assert body["ok"] is True and body["engine"] == "cli-fallback"


def test_recommend_daily_login_check_failure_is_treated_as_logged_out(monkeypatch):
    """登录探测本身异常时按未登录处理，绝不放行到抓取阶段。"""
    monkeypatch.setattr(mb_app, "ne_check_is_logged_in",
                        lambda: (_ for _ in ()).throw(RuntimeError("账号接口挂了")))
    monkeypatch.setattr(mb_app, "ne_daily_songs",
                        lambda limit=20: pytest.fail("探测失败时不该继续抓日推"))
    with TestClient(app) as client:
        assert client.get("/api/v1/recommend/daily").json()["error"] == "not_logged_in"


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
    # 若本机恰好装了真实 NEMbox，模块回退会命中（那是解析器的正确行为）；
    # 本用例要验证的是"什么都找不到"的分支，因此把回退目标也指成不存在的模块。
    monkeypatch.setattr(mb_runner, "module_fallback", lambda: "NoSuchModule_xyz_123")
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


# ===========================================================================
# NEMbox 实例与 cookie 的生命周期（登录后仍搜不到歌的真正根因）
#
# NEMbox 的 NetEase.__init__ 里 cookie_jar.load() 只执行一次，之后永不重读。
# 扫码登录是由 musicbox CLI 子进程写 cookie 的，父进程里被 netease_ext 缓存的
# 长命单例仍握着登录前的旧 cookie —— 于是"CLI 说已登录、进程内说未登录"，
# 可播性过滤按未登录处理，VIP/付费曲目全部拿不到直链被剔除，
# 表现为登录成功后搜索与每日推荐依然为空。
# ===========================================================================

import netease_ext as ne2


@pytest.fixture(autouse=True)
def _reset_cli_probe_cache():
    """_CLI_PROBE 是进程级缓存，用例之间必须清空，否则串味且结果不确定。"""
    mb_app.reset_cli_probe_for_test()
    try:
        yield
    finally:
        mb_app.reset_cli_probe_for_test()

# 下面这些用例要跑真实的 NEMbox 构造流程（cookie_jar.load / Storage / deviceId），
# 因此需要环境里真的装了 NetEase-MusicBox。CI/沙箱没装时自动跳过；
# 本地 `pip install NetEase-MusicBox` 后即可启用。
requires_nembox = pytest.mark.skipif(
    importlib.util.find_spec("NEMbox") is None,
    reason="需要真实 NetEase-MusicBox 包（pip install NetEase-MusicBox）",
)


@pytest.fixture
def real_cookie_dir(tmp_path, monkeypatch):
    """指向一个真实可写的 XDG 目录，让 NEMbox 自己创建 cookie 文件。"""
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    conf = tmp_path / "config"
    for d in (data, cache, conf):
        d.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(conf))
    ne2.reset_api_instance()
    ne2._api_instance = None
    yield data
    ne2.reset_api_instance()
    ne2._api_instance = None


def _cookie_file_for(api):
    """从真实实例上取 cookie 路径。

    不能按 XDG_DATA_HOME 自己拼：NEMbox 的 Constant.cookie_path 在【模块导入时】
    就已经根据当时的环境变量固化了，测试里再 monkeypatch XDG_* 搬不动它。
    """
    from pathlib import Path as _P

    return _P(getattr(api.storage, "cookie_path", ""))


@requires_nembox
def test_get_api_returns_same_instance_when_cookie_unchanged(real_cookie_dir):
    """cookie 没变时必须复用同一个实例，不能每个请求都重建（重建会重算 deviceId 等）。"""
    a = ne2._get_api()
    b = ne2._get_api()
    assert a is b
    assert ne2._cookie_stamp(a) == ne2._api_cookie_stamp


@requires_nembox
def test_get_api_rebuilds_when_cookie_file_changes(real_cookie_dir):
    """核心回归：cookie 文件一变（= 有人扫码登录了），必须重建实例重读 cookie。"""
    before = ne2._get_api()
    cf = _cookie_file_for(before)
    assert str(cf), "应从真实实例拿到 cookie 路径"
    cf.parent.mkdir(parents=True, exist_ok=True)
    original = cf.read_text(encoding="utf-8", errors="replace") if cf.exists() else None

    try:
        # 模拟 CLI 子进程登录成功后写回 cookie（父进程的单例不会自己重读）
        import time as _t
        cf.write_text(
            "# Netscape HTTP Cookie File\n"
            ".music.163.com\tTRUE\t/\tTRUE\t9999999999\tMUSIC_U\tFAKE_LOGGED_IN_TOKEN\n",
            encoding="utf-8",
        )
        os.utime(cf, (_t.time() + 5, _t.time() + 5))

        after = ne2._get_api()
        assert after is not before, "cookie 变化后必须重建 NetEase 实例，否则永远用旧登录态"
        assert ne2._api_cookie_stamp == ne2._cookie_stamp(after)
    finally:
        if original is not None:
            try:
                cf.write_text(original, encoding="utf-8")
            except OSError:
                pass
        ne2.reset_api_instance()


@requires_nembox
def test_cookie_stamp_tolerates_missing_file(real_cookie_dir):
    """cookie 文件不存在时指纹为空，且不能抛异常。"""
    api = ne2._get_api()
    cf = _cookie_file_for(api)
    if not cf.exists():
        assert ne2._cookie_stamp(api) == ()
        return
    backup = cf.read_bytes()
    try:
        cf.unlink()
        assert ne2._cookie_stamp(api) == (), "cookie 文件消失时应视为空指纹"
    finally:
        cf.write_bytes(backup)


@requires_nembox
def test_reset_api_instance_drops_instance_and_login_cache(real_cookie_dir):
    first = ne2._get_api()
    ne2._LOGIN_CACHE = (12345.0, True)          # 伪造一份已缓存的登录态
    ne2.reset_api_instance()
    assert ne2._api_instance is None
    assert ne2._api_cookie_stamp is None
    assert ne2._LOGIN_CACHE == (0.0, False), "重置实例必须同时作废登录态缓存"
    second = ne2._get_api()
    assert second is not first


def test_login_check_803_rebuilds_instance(monkeypatch):
    """扫码成功那一刻就必须重建实例 —— 只清登录态缓存是不够的。"""
    reset_calls = []
    monkeypatch.setattr(mb_app, "reset_api_instance",
                        lambda: reset_calls.append(1))
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda args, timeout=30.0: (0, json.dumps(
                            {"ok": True, "data": {"code": 803, "nickname": "张三"}}), ""))
    with TestClient(app) as client:
        body = client.get("/api/v1/auth/login/check",
                          params={"unikey": "ABC123def456"}).json()
    assert body["data"]["code"] == 803
    assert reset_calls == [1], "803 必须触发 NEMbox 实例重建"


@pytest.mark.parametrize("code", [800, 801, 802])
def test_login_check_other_codes_do_not_rebuild(monkeypatch, code):
    """未扫码/待确认/已过期都不该重建实例（会白丢一次 deviceId 计算与磁盘 IO）。"""
    reset_calls = []
    monkeypatch.setattr(mb_app, "reset_api_instance", lambda: reset_calls.append(1))
    monkeypatch.setattr(runner, "run_musicbox",
                        lambda args, timeout=30.0: (0, json.dumps(
                            {"ok": True, "data": {"code": code}}), ""))
    with TestClient(app) as client:
        client.get("/api/v1/auth/login/check", params={"unikey": "ABC123def456"})
    assert reset_calls == []


def test_playable_filter_uses_current_login_state(real_cookie_dir, monkeypatch):
    """可播性过滤必须基于【当前】登录态，而不是实例创建时那一刻的。"""
    calls = {"n": 0}

    class _Api:
        storage = None

        def songs_url(self, ids):
            calls["ids"] = list(ids)
            # fee=1 是 VIP 曲
            return [{"id": 1, "url": "http://cdn/vip.flac", "fee": 1},
                    {"id": 2, "url": "http://cdn/free.mp3", "fee": 0}]

        def get_account_info(self):
            calls["n"] += 1
            return {"account": {"id": 9}, "profile": {"nickname": "n"}} if calls["n"] > 1 \
                else {"account": None, "profile": None}

    api = _Api()
    monkeypatch.setattr(ne2, "_get_api", lambda: api)
    ne2.invalidate_login_cache()

    # 第一次：未登录 -> VIP 曲被剔除
    assert ne2.filter_playable_song_ids([1, 2]) == {2}
    ne2.invalidate_login_cache()
    # 第二次：账号已登录 -> VIP 曲放行
    assert ne2.filter_playable_song_ids([1, 2]) == {1, 2}


# ===========================================================================
# 试听片段判定 —— 「搜不到任何在线歌曲」的真正根因
#
# 真机抓到的 NetEase song/url 响应里，**每一条**都带 freeTrialPrivilege 结构体：
#     'freeTrialInfo': None,
#     'freeTrialPrivilege': {'resConsumable': False, 'userConsumable': False, ...}
# 而判定曾写成：
#     free_trial = item.get("freeTrialInfo") or item.get("freeTrialPrivilege")
#     if ... or free_trial: continue
# freeTrialInfo 为 None（假值）→ 取 freeTrialPrivilege → 永远是非空 dict（真理值）
# → 5/5 条全部被当成试听片段剔除。与是否登录、是否 VIP 完全无关，
# 所以前面修 cookie 单例、修 dig_info 逐首过滤后症状都不消失。
# 真正的试听信号在结构体内部的 resConsumable / userConsumable 布尔位。
# ===========================================================================

# 真机匿名调用 api.songs_url() 抓到的真实条目（fee=8 有直链的正常曲目）
_REAL_TRACK_OK = {
    "id": 1998849460,
    "url": "http://m701.music.126.net/20260911213658/935a/jdymusic/x.mp3?vuutv=abc",
    "br": 320000, "size": 9653856, "code": 200, "type": "mp3",
    "fee": 8, "payed": 0, "flag": 2064646,
    "freeTrialInfo": None,                     # ← None 表示无试听
    "level": "exhigh",
    "freeTrialPrivilege": {                    # ← 每条必带，存在≠试听
        "resConsumable": False, "userConsumable": False,
        "listenType": None, "cannotListenReason": None,
        "playReason": None, "freeLimitTagType": None,
    },
    "freeTimeTrialPrivilege": {
        "resConsumable": False, "userConsumable": False, "type": 0, "remainTime": 0,
    },
}


def test_free_trial_privilege_presence_is_not_a_trial_flag():
    """核心回归：freeTrialPrivilege 恒存在，不能拿它的存在当试听判定。"""
    assert ne2.is_trial_snippet(_REAL_TRACK_OK) is False, (
        "正常曲目被误判为试听片段 —— 这就是「搜不到任何在线歌曲」的根因"
    )


def test_trial_detected_only_via_inner_booleans():
    """只有内部布尔位为 True 才算试听。"""
    for flag_key in ("resConsumable", "userConsumable"):
        bad = dict(_REAL_TRACK_OK)
        bad["freeTrialPrivilege"] = dict(_REAL_TRACK_OK["freeTrialPrivilege"],
                                         **{flag_key: True})
        assert ne2.is_trial_snippet(bad) is True, f"{flag_key}=True 应判定为试听"
        # 字符串 "true" 也要认（不同接口序列化不一致）
        s = dict(_REAL_TRACK_OK)
        s["freeTrialPrivilege"] = dict(_REAL_TRACK_OK["freeTrialPrivilege"],
                                       **{flag_key: "true"})
        assert ne2.is_trial_snippet(s) is True


def test_trial_info_struct_is_a_real_trial():
    """freeTrialInfo 语义相反：非空即真试听，None/空才是无试听。"""
    t = dict(_REAL_TRACK_OK)
    t["freeTrialInfo"] = {"st": 0, "et": 60}
    assert ne2.is_trial_snippet(t) is True, "带试听区间 = 试听片段"

    f = dict(_REAL_TRACK_OK)
    f["freeTrialInfo"] = {"resConsumable": True}
    assert ne2.is_trial_snippet(f) is True

    assert ne2.is_trial_snippet(dict(_REAL_TRACK_OK)) is False
    assert ne2.is_trial_snippet({"freeTrialInfo": None}) is False
    assert ne2.is_trial_snippet({"freeTrialInfo": {}}) is False, "空结构体不算试听"


def test_is_trial_snippet_never_raises_on_junk():
    """坏字段类型只能返回布尔，绝不能抛出去把整批搜索打成空。"""
    for junk in ({}, {"freeTrialPrivilege": None}, {"freeTrialPrivilege": "x"},
                 {"freeTrialInfo": []}, {"freeTrialInfo": ""},
                 {"freeTrialInfo": 0}, {"freeTrialPrivilege": 7}):
        assert ne2.is_trial_snippet(junk) is False
    # 非空未知值按上游语义保守判为试听，但同样不能抛
    assert ne2.is_trial_snippet({"freeTrialInfo": "y"}) is True


def test_playable_url_map_keeps_real_world_payload(monkeypatch):
    """端到端：把真机抓到的响应喂给 playable_url_map，必须放行。"""
    monkeypatch.setattr(ne2, "check_is_logged_in", lambda force=False: False)
    monkeypatch.setitem(_REAL_TRACK_OK, "id", 1998849460)

    class _Api:
        def songs_url(self, ids):
            return [dict(_REAL_TRACK_OK)]

    monkeypatch.setattr(ne2, "_get_api", lambda: _Api())
    kept = ne2.filter_playable_song_ids([1998849460])
    assert kept == {1998849460}, "修复前这里是 set() —— 全被误杀"

    m = ne2.playable_url_map([1998849460])
    assert m[1998849460]["code"] == 200


def test_search_songs_returns_tracks_for_real_payload(monkeypatch):
    """搜索链路：原始响应含恒存在的 freeTrialPrivilege 时结果不能为空。"""
    raw_songs = [{"id": 1998849460, "name": "拉布拉多",
                  "ar": [{"name": "孙这"}], "al": {"id": 1, "picUrl": ""},
                  "dt": 241285}]

    class _Api:
        def search(self, kw, limit=50, **kw2):
            return {"songs": list(raw_songs), "songCount": 1}

        def songs_url(self, ids):
            return [dict(_REAL_TRACK_OK)]

    monkeypatch.setattr(ne2, "_get_api", lambda: _Api())
    monkeypatch.setattr(ne2, "check_is_logged_in", lambda force=False: True)
    out = ne2.search_songs("拉布拉多", limit=5)
    assert len(out) == 1
    assert out[0]["song_name"] == "拉布拉多"
    assert out[0]["artist"] == "孙这"
    assert out[0]["mp3_url"].startswith("http")
    assert out[0]["quality"], "音质必须判定出来（br=320000 -> HD 320k）"


# ===========================================================================
# 播放热路径改进程内取数 —— 「搜到了却一直缓冲、无法播放」的根因
#
# /api/v1/song/{id}/url 与 /api/v1/song/{id}/info 原先 exec CLI，而它们位于播放热
# 路径：飞牛每首歌各调一次，一次搜索 50 首就是上百次调用。
# 实测（真实 NetEase-MusicBox 0.5.3）：CLI 子进程冷启动 47.37s、稳态 1.4s；
# 代理侧对这两个请求的 httpx 超时只有 10s ⇒ 冷启动必然超时。
# 而 httpx.ReadTimeout('') 的 str() 是【空串】，日志只剩
#   resolve_netease_url error for 94344 (quality=exhigh):
# 连异常类型都看不见。改进程内后实测取链 0.05s、详情 0.07s、歌词 0.13s。
# ===========================================================================


def test_urls_for_level_matches_upstream_songs_url(monkeypatch):
    """核心等价性：_urls_for_level 必须和上游 api.songs_url 给出一致结果。

    上游 songs_url(ids) 的 level 取自全局 Config().get("music_quality")，不接受参数；
    本服务需要按请求音质取链，又不能去改用户的全局配置文件。真机上实测校验过：
    用 Config 当前值走 _urls_for_level，返回与 api.songs_url() 逐字节一致。
    这条用例把该等价关系的参数构造钉住，防止上游变动后我们悄悄漂移。
    """
    captured = {}

    class _Api:
        def eapi_request(self, path, params):
            captured["path"] = path
            captured["params"] = params
            return {"data": [{"id": 7, "code": 200, "url": "http://cdn/7.mp3"}]}

        def request(self, method, path, params=None, **kw):
            captured["weapi"] = (method, path, params)
            return {"data": []}

    monkeypatch.setattr(ne2, "_level_to_encode_type", lambda level: "mp3")
    out = ne2._urls_for_level(_Api(), [7], "exhigh")
    assert out == [{"id": 7, "code": 200, "url": "http://cdn/7.mp3"}]
    assert captured["path"] == "/api/song/enhance/player/url/v1"
    assert captured["params"]["level"] == "exhigh"
    assert captured["params"]["encodeType"] == "mp3"
    assert captured["params"]["ids"] == "[7]", "ids 必须是紧凑 JSON（与上游分隔符一致）"
    assert "weapi" not in captured, "eapi 有结果就不该再走 weapi 降级"


def test_urls_for_level_falls_back_to_weapi_with_rate_map(monkeypatch):
    """eapi 无结果时按上游同款 rate_map 降级到 weapi。"""
    seen = {}

    class _Api:
        def eapi_request(self, path, params):
            return {"data": []}

        def request(self, method, path, params=None, **kw):
            seen["call"] = (method, path, params)
            return {"data": [{"id": 9, "code": 200, "url": "http://cdn/9.mp3"}]}

    monkeypatch.setattr(ne2, "_level_to_encode_type", lambda level: "flac")
    out = ne2._urls_for_level(_Api(), [9], "lossless")
    assert out and out[0]["id"] == 9
    assert seen["call"][1] == "/weapi/song/enhance/player/url"
    assert seen["call"][2]["br"] == 999000, "lossless 必须映射到 999000（上游 rate_map）"
    assert seen["call"][2]["ids"] == [9]


def test_urls_for_level_survives_eapi_exception(monkeypatch):
    """eapi 抛异常不能炸，必须继续走 weapi 降级。"""
    class _Api:
        def eapi_request(self, path, params):
            raise RuntimeError("eapi boom")

        def request(self, method, path, params=None, **kw):
            return {"data": [{"id": 3, "code": 404, "url": None}]}

    monkeypatch.setattr(ne2, "_level_to_encode_type", lambda level: "mp3")
    out = ne2._urls_for_level(_Api(), [3], "exhigh")
    assert out == [{"id": 3, "code": 404, "url": None}]


def test_pick_by_id_prefers_matching_id():
    items = [{"id": 1, "url": "a"}, {"id": 2, "url": "b"}]
    assert ne2._pick_by_id(items, 2)["url"] == "b"
    assert ne2._pick_by_id([{"id": 5, "url": "x"}], 999)["url"] == "x", "单条时退化为返回它"
    assert ne2._pick_by_id([{"id": 5}, {"id": 6}], 999) is None, "多条且对不上不能瞎猜"
    assert ne2._pick_by_id([], 1) is None
    assert ne2._pick_by_id(None, 1) is None
    assert ne2._pick_by_id([None, {"id": 4}], 4)["id"] == 4
    assert ne2._pick_by_id({"id": 8}, 8)["id"] == 8


def test_song_url_info_returns_cli_compatible_shape(monkeypatch):
    """返回结构必须与 CLI `musicbox song url --json` 的 data 字段一致。

    代理 resolve_netease_url 只认 code == 200 and url，因此不能改形状。
    """
    class _Api:
        def eapi_request(self, path, params):
            return {"data": [{"id": 410710837, "code": 200,
                              "url": "http://cdn/a.mp3", "br": 320000, "level": "exhigh"}]}

        def request(self, *a, **kw):
            return {"data": []}

    monkeypatch.setattr(ne2, "_get_api", lambda: _Api())
    monkeypatch.setattr(ne2, "_quality_to_level", lambda q: str(q).lower())
    monkeypatch.setattr(ne2, "_level_to_encode_type", lambda level: "mp3")
    info = ne2.song_url_info(410710837, "exhigh")
    assert info["code"] == 200 and info["url"]
    assert info["id"] == 410710837
    assert ne2.quality_of(info) == "HD 320k"


def test_song_raw_detail_keeps_upstream_shape(monkeypatch):
    """_online_info 期望的是【上游原始】形状（自己解析 ar/al/dt/sq/hr/h），
    不是 _map_song_detail 映射后的 song_name/album_pic_url 那套。"""
    raw = {"id": 7, "name": "屋顶", "ar": [{"name": "周杰伦"}],
           "al": {"name": "范特西", "picUrl": "http://p/1.jpg"},
           "dt": 267232, "sq": None, "hr": None, "h": {"size": 10691439, "br": 320000}}

    class _Api:
        def songs_detail(self, ids):
            assert ids == [7]
            return [raw]

    monkeypatch.setattr(ne2, "_get_api", lambda: _Api())
    got = ne2.song_raw_detail(7)
    assert got == raw
    assert got["ar"][0]["name"] == "周杰伦"
    assert got["h"]["br"] == 320000


# --------- 端点层：进程内优先，CLI 只在问不到上游时兜底 ---------

def test_song_url_endpoint_uses_in_process_and_skips_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(mb_app, "song_url_info",
                        lambda sid, q: {"id": sid, "code": 200, "url": "http://cdn/1.mp3"})
    monkeypatch.setattr(mb_app, "exec_musicbox",
                        lambda args, **kw: calls.append(args) or {"ok": False})
    with TestClient(app) as client:
        body = client.get("/api/v1/song/1/url", params={"quality": "exhigh"}).json()
    assert body["engine"] == "in-process"
    assert body["data"]["url"] == "http://cdn/1.mp3"
    assert calls == [], "进程内已成功就绝不能再 spawn CLI（冷启动实测 47s）"


def test_song_url_structured_404_does_not_fall_back_to_cli(monkeypatch):
    """上游明确说取不到链（code=404）是权威答案：跑慢 CLI 不会有不同结果。

    否则 proxy 依次试 lossless→exhigh 时，每首不可播曲目都会触发两次子进程调用，
    等于把这次修复又抵消掉。
    """
    calls = []
    monkeypatch.setattr(mb_app, "song_url_info",
                        lambda sid, q: {"id": sid, "code": 404, "url": None})
    monkeypatch.setattr(mb_app, "exec_musicbox",
                        lambda args, **kw: calls.append(args) or {"ok": True})
    with TestClient(app) as client:
        body = client.get("/api/v1/song/2/url", params={"quality": "lossless"}).json()
    assert body["engine"] == "in-process"
    assert body["data"]["code"] == 404
    assert calls == [], "结构化 404 不得兜底跑 CLI"


def test_song_url_falls_back_to_cli_only_when_upstream_unreachable(monkeypatch):
    calls = []
    monkeypatch.setattr(mb_app, "song_url_info",
                        lambda sid, q: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(mb_app, "exec_musicbox",
                        lambda args, **kw: calls.append(args) or {
                            "ok": True, "data": {"code": 200, "url": "http://cli/1.mp3"}})
    with TestClient(app) as client:
        body = client.get("/api/v1/song/1/url", params={"quality": "exhigh"}).json()
    assert calls == [["song", "url", "1", "--quality", "exhigh", "--json"]]
    assert body["data"]["url"] == "http://cli/1.mp3"
    assert "engine" not in body, "走 CLI 兜底时不要谎报 in-process"


def test_song_url_empty_response_falls_back_to_cli(monkeypatch):
    """问到了但是空响应（既无 code 也无 id）→ 视为没问到上游，允许兜底。"""
    calls = []
    monkeypatch.setattr(mb_app, "song_url_info", lambda sid, q: {})
    monkeypatch.setattr(mb_app, "exec_musicbox",
                        lambda args, **kw: calls.append(args) or {"ok": True, "data": {}})
    with TestClient(app) as client:
        client.get("/api/v1/song/5/url", params={"quality": "exhigh"})
    assert len(calls) == 1


def test_song_info_endpoint_returns_raw_shape(monkeypatch):
    raw = {"id": 7, "name": "屋顶", "ar": [{"name": "周杰伦"}],
           "al": {"picUrl": "http://p/1.jpg"}, "dt": 267232, "h": {"br": 320000}}
    calls = []
    monkeypatch.setattr(mb_app, "song_raw_detail", lambda sid: dict(raw))
    monkeypatch.setattr(mb_app, "exec_musicbox",
                        lambda args, **kw: calls.append(args) or {"ok": False})
    with TestClient(app) as client:
        body = client.get("/api/v1/song/7/info").json()
    assert body["engine"] == "in-process"
    assert body["data"]["ar"][0]["name"] == "周杰伦"
    assert body["data"]["dt"] == 267232
    assert calls == []


def test_song_info_falls_back_to_cli_on_error(monkeypatch):
    calls = []
    monkeypatch.setattr(mb_app, "song_raw_detail",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(mb_app, "exec_musicbox",
                        lambda args, **kw: calls.append(args) or {"ok": True, "data": {"name": "x"}})
    with TestClient(app) as client:
        client.get("/api/v1/song/7/info")
    assert calls == [["song", "info", "7", "--json"]]


def test_song_url_rejects_bad_quality(monkeypatch):
    """音质白名单照旧生效，别让这次改动放宽校验。"""
    with TestClient(app) as client:
        r = client.get("/api/v1/song/1/url", params={"quality": "bogus"})
    assert r.status_code == 400
    assert "bogus" in r.text


# --------- selftest 的 CLI 探测：快、可缓存、不阻塞诊断页 ---------
# `musicbox` CLI 冷启动实测 47.37s，而管理页面探测 /api/v1/selftest 的超时是 10s。
# 冷启动时整栏显示 undefined/[]，把用户唯一顺手的排障工具变成一片空白；2.1.6 起
# 播放热路径不再 spawn CLI，CLI 再没有顺带预热的机会，该现象会变成常态。


def test_selftest_cli_probe_is_cached(monkeypatch):
    """第二次探测必须命中缓存，不能再 spawn CLI（稳态也要 1.4s）。"""
    calls = []

    def fake_run(args, timeout=30.0):
        calls.append((args, timeout))
        return 0, "NetEase-MusicBox installed version:0.5.3", ""

    monkeypatch.setattr(mb_runner, "run_musicbox", fake_run)
    ok1, d1 = mb_app.probe_cli_exec(timeout_s=3.0)
    ok2, d2 = mb_app.probe_cli_exec(timeout_s=3.0)
    assert (ok1, d1) == (ok2, d2) == (True, "NetEase-MusicBox installed version:0.5.3")
    assert len(calls) == 1, "命中缓存后不得再执行 CLI"
    assert calls[0][1] == 3.0


def test_selftest_cli_probe_timeout_is_not_cached(monkeypatch):
    """超时不能被永久缓存：那多半只是 CLI 还在冷启动，预热完应能拿到真实结果。"""
    seq = []

    def fake_run(args, timeout=30.0):
        seq.append(timeout)
        if len(seq) == 1:
            raise mb_runner.MusicboxTimeoutError("musicbox timed out after 3s")
        return 0, "version:0.5.3", ""

    monkeypatch.setattr(mb_runner, "run_musicbox", fake_run)
    ok, detail = mb_app.probe_cli_exec(timeout_s=3.0)
    assert ok is False
    assert "timeout" in detail.lower() or "timeout" in detail
    assert "MusicboxTimeoutError" in detail, "必须带异常类型名，日志才可 grep"
    assert "deviceId" in detail, "应解释冷启动成因，而不是一句无信息的 timeout"

    ok2, detail2 = mb_app.probe_cli_exec(timeout_s=3.0)
    assert (ok2, detail2) == (True, "version:0.5.3"), "超时未被缓存，重试应拿到真实结果"
    assert len(seq) == 2


def test_selftest_cli_probe_hard_failure_is_cached(monkeypatch):
    """非超时类硬失败（比如 CLI 根本不存在）是确定性结果，应当缓存。"""
    calls = []
    monkeypatch.setattr(
        mb_runner, "run_musicbox",
        lambda args, timeout=30.0: calls.append(1) or (127, "", "musicbox CLI not found"),
    )
    ok, detail = mb_app.probe_cli_exec(timeout_s=3.0)
    ok2, _ = mb_app.probe_cli_exec(timeout_s=3.0)
    assert ok is False and ok2 is False
    assert "not found" in detail
    assert len(calls) == 1, "硬失败应缓存，不必每次重复探测"


def test_selftest_stays_fast_when_cli_is_cold(monkeypatch):
    """端到端：CLI 卡住时 selftest 也必须快速返回完整结构（含其余字段）。

    这正是真机上「诊断页 CLI 区块整栏 undefined」的成因回归。
    """
    def hanging(args, timeout=30.0):
        assert timeout <= 3.0, "selftest 里的探测超时必须远小于管理页面的 10s"
        raise mb_runner.MusicboxTimeoutError("musicbox timed out")

    monkeypatch.setattr(mb_runner, "musicbox_cmd",
                        lambda: (["/venv/bin/musicbox"], "absolute:/venv/bin/musicbox"))
    monkeypatch.setattr(mb_runner, "run_musicbox", hanging)
    with TestClient(app) as client:
        body = client.get("/api/v1/selftest").json()
    assert body["ok"] is True
    d = body["data"]
    assert d["cli_exec_ok"] is False
    assert "timeout" in d["cli_exec_detail"].lower()
    # 关键：CLI 卡住时其余诊断字段仍必须给出，不能整栏空白
    assert d["cli_found"] is True
    assert d["cli_cmd"] == ["/venv/bin/musicbox"]
    assert d["interpreter"]
    assert isinstance(d["xdg"], dict)


def test_startup_warms_cli_probe_in_background(monkeypatch):
    """启动预热必须在后台线程里跑，绝不能阻塞服务就绪。"""
    started = []
    monkeypatch.setattr(mb_app, "runner", mb_runner)
    monkeypatch.setattr(
        mb_runner, "run_musicbox",
        lambda args, timeout=30.0: started.append(timeout) or (0, "version:0.5.3", ""),
    )
    mb_app.reset_cli_probe_for_test()
    mb_app._warm_cli_probe_in_background()
    import time as _t
    for _ in range(100):
        if mb_app._CLI_PROBE is not None:
            break
        _t.sleep(0.02)
    assert started, "预热应真的执行一次 CLI"
    assert started[0] >= 60, "冷启动实测 47s，预热超时余量必须足够大"
    assert mb_app._CLI_PROBE == {"ok": True, "detail": "version:0.5.3"}


def test_warmup_is_idempotent_per_process(monkeypatch):
    """重复调用只起一个预热线程。

    否则每个 TestClient（生产中是每次 startup 重入）都会拉起一个跑
    `musicbox --version` 的后台线程 —— CLI 稳态也要 1.4s，纯属浪费，
    且在真机上会把 CPU/IO 拖慢，反过来拖慢播放。
    """
    calls = []
    monkeypatch.setattr(
        mb_runner, "run_musicbox",
        lambda args, timeout=30.0: calls.append(1) or (0, "version:0.5.3", ""),
    )
    mb_app.reset_cli_probe_for_test()
    mb_app._warm_cli_probe_in_background()
    mb_app._warm_cli_probe_in_background()
    mb_app._warm_cli_probe_in_background()
    import time as _t
    for _ in range(100):
        if mb_app._CLI_PROBE is not None:
            break
        _t.sleep(0.02)
    _t.sleep(0.1)
    assert len(calls) <= 1, f"预热应幂等，实际触发了 {len(calls)} 次"


def test_stale_warmup_write_is_discarded():
    """作废轮次的写入必须被丢弃。

    预热线程可能在很久之后（实测冷启动 47s）才回来写缓存；若期间发生过重置，
    那次写入属于已经不存在的轮次。真实后果是测试随机串味；生产中则是
    诊断页报告一份过期的探测结果。
    """
    gen = mb_app._cli_probe_generation()
    mb_app._cache_cli_probe(True, "fresh", gen)
    assert mb_app._CLI_PROBE == {"ok": True, "detail": "fresh"}

    mb_app.reset_cli_probe_for_test()
    assert mb_app._CLI_PROBE is None
    assert mb_app._cli_probe_generation() != gen, "重置必须推进代号"

    mb_app._cache_cli_probe(True, "stale", gen)
    assert mb_app._CLI_PROBE is None, "旧代号的写入不得落到新一轮缓存里"


def test_lifespan_warms_cli_probe_on_startup(monkeypatch):
    """预热必须挂在 lifespan 上：TestClient 进入时才发生。"""
    calls = []
    monkeypatch.setattr(
        mb_runner, "run_musicbox",
        lambda args, timeout=30.0: calls.append(1) or (0, "version:0.5.3", ""),
    )
    mb_app.reset_cli_probe_for_test()
    assert mb_app._CLI_WARMUP_STARTED is False
    with TestClient(app) as client:
        client.get("/healthz")
    import time as _t
    for _ in range(100):
        if mb_app._CLI_PROBE is not None:
            break
        _t.sleep(0.02)
    assert mb_app._CLI_WARMUP_STARTED is True, "startup 应触发预热"
    assert calls and mb_app._CLI_PROBE and mb_app._CLI_PROBE["ok"] is True


def test_importing_app_module_does_not_spawn_cli(tmp_path):
    """导入 app 模块不得预热 CLI —— 否则光跑单元测试就会 spawn musicbox 子进程。

    预热挂在 lifespan 而非模块级调用，正是为了这一点。用全新解释器验证，
    排除本测试进程里已经 import 过的干扰。
    """
    import subprocess

    env = dict(os.environ)
    env.update({
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PYTHONPATH": MUSICBOX_SERVICE_DIR + os.pathsep + env.get("PYTHONPATH", ""),
    })
    for d in ("data", "config", "cache"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    code = (
        "import json, app;"
        "print(json.dumps({'started': bool(app._CLI_WARMUP_STARTED),"
        " 'probe': app._CLI_PROBE}))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          timeout=90, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-500:]
    out = json.loads(proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    assert out["started"] is False, "导入即预热会让测试 spawn 真实 CLI 子进程"
    assert out["probe"] is None
