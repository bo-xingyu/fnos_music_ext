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
