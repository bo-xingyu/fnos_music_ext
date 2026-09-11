"""每日推荐（网易云官方日推）、播放历史合并与配置脱敏测试。"""
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import netease_auth, recommend as dailyrec
from proxy.app import CONF, _DAILY_TASKS, _SEARCH_CACHE, app, _conf_log_value
from proxy.app import build_online_track

DAILY_ROWS = [
    {
        "song_id": "d1",
        "song_name": "晴天",
        "artist": "周杰伦",
        "album_name": "叶惠美",
        "duration": 269,
        "quality": "SQ",
    },
    {
        "song_id": "d2",
        "song_name": "七里香",
        "artist": "周杰伦",
        "album_name": "七里香",
        "duration": 291,
        "quality": "LD",
    },
    {
        "song_id": "d3",
        "song_name": "夜曲",
        "artist": "周杰伦",
        "album_name": "十一月的萧邦",
        "duration": 226,
        "quality": "HR",
    },
]


@pytest.fixture(autouse=True)
def setup_recommend_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    _DAILY_TASKS.clear()
    netease_auth.reset_for_test()
    rec_dir = str(tmp_path / "recommend_cache")
    hist_dir = str(tmp_path / "play_history")
    fav_dir = str(tmp_path / "online_favorites")
    cache_dir = str(tmp_path / "cache")
    os.makedirs(fav_dir, exist_ok=True)
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", rec_dir)
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", hist_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "daily_enabled", True)
    monkeypatch.setitem(CONF, "daily_limit", 20)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    yield
    _DAILY_TASKS.clear()


# ---------------------------------------------------------------- helpers ----


def _auth_user(guid="user-rec-1"):
    """飞牛官方后端 mock：登录用户、歌单列表、播放历史、事件上报。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": guid}})
        if path.endswith("/playlist/list"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"guid": "localpl", "name": "牛一", "coverId": "c1",
                             "createdAt": 1, "updatedAt": 1}
                        ],
                        "total": 1,
                    },
                },
            )
        if path.endswith("/playlist/batch-detail"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"guid": "localpl", "trackCount": 3}]}},
            )
        if path.endswith("/play-history/list"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"list": [{"guid": "local-track-1", "title": "本地"}], "total": 1},
                },
            )
        if path.endswith("/event/report"):
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})
        if "search/track" in path:
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(200, json={"code": 0, "data": None})

    return handler


def _musicbox_handler(*, logged_in=True, rows=None, calls=None, daily_status=200):
    """musicbox 音源服务 mock：登录态 + 每日推荐 + 详情补齐。"""
    rows = DAILY_ROWS if rows is None else rows

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            calls[path] = calls.get(path, 0) + 1
        if path == "/api/v1/auth/status":
            return httpx.Response(
                200,
                json={"ok": True, "data": {"logged_in": logged_in, "nickname": "测试账号"}},
            )
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/recommend/daily":
            if daily_status != 200:
                return httpx.Response(daily_status)
            if not logged_in:
                return httpx.Response(
                    200, json={"ok": False, "error": "not_logged_in", "data": []}
                )
            return httpx.Response(200, json={"ok": True, "data": rows})
        if path == "/api/v1/songs/detail":
            detail = [
                {"song_id": str(r["song_id"]), "album_pic_url": f"http://pic/{r['song_id']}.jpg",
                 "has_sq": "SQ" in str(r.get("quality", "")).upper(),
                 "has_hr": "HR" in str(r.get("quality", "")).upper(),
                 "album_name": r.get("album_name", ""), "artist": r.get("artist", "")}
                for r in rows
            ]
            return httpx.Response(200, json={"ok": True, "data": detail})
        if path == "/api/v1/search":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404, json={"ok": False})

    return handler


def _wire(monkeypatch, *, guid="user-rec-1", **musicbox_kwargs):
    calls = {}
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_auth_user(guid)), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_musicbox_handler(calls=calls, **musicbox_kwargs)),
        base_url="http://127.0.0.1:8770",
    )
    netease_auth.invalidate_state()
    return calls


# ------------------------------------------------------------------- guid ----

def test_daily_guid_and_day_helpers():
    guid = dailyrec.daily_playlist_guid("20260831", "user-1")
    assert dailyrec.is_daily_playlist_guid(guid)
    assert "20260831" in guid
    assert not dailyrec.is_daily_playlist_guid("online:netease:1")
    assert not dailyrec.is_daily_playlist_guid(None)

    day = dailyrec.today_key()
    assert len(day) == 8 and day.isdigit()
    assert dailyrec.daily_playlist_guid(day) == dailyrec.daily_playlist_guid()
    assert dailyrec.daily_playlist_name("20260831") == "每日推荐 08-31"


def test_safe_user_name_sanitizes_path_traversal():
    """user_guid 会被拼进缓存文件路径，绝不能让它带出目录穿越。"""
    for evil in ("../../etc/passwd", "..%2f..%2fetc", "a/b\\c", "\0nul", "  ", "....//x"):
        name = dailyrec._safe_user_name(evil)
        assert ".." not in name.replace("_", ""), evil
        assert "/" not in name and "\\" not in name and "\0" not in name, evil
        assert name, evil
    assert dailyrec._safe_user_name("") == "shared"
    assert dailyrec._safe_user_name("user-1_A") == "user-1_A"


# ------------------------------------------------------------ redaction ----

def test_conf_log_redacts_secrets():
    assert _conf_log_value("pushplus_token", "super-secret") == "***"
    assert _conf_log_value("FNMUSIC_LLM_API_KEY", "sk-x") == "***"
    assert _conf_log_value("musicbox_url", "http://127.0.0.1:8770") == "http://127.0.0.1:8770"
    # 空值不伪装成"已配置"
    assert _conf_log_value("api_key", "") == ""
    # 单源化后 CONF 里不应再有任何 LLM / 多源痕迹
    for gone in ("llm_base_url", "llm_model", "musicdl_url", "lx_url", "online_sources"):
        assert gone not in CONF


# ------------------------------------------------------- fetch_daily_songs ----

@pytest.mark.anyio
async def test_fetch_daily_songs_ok(monkeypatch):
    calls = _wire(monkeypatch, logged_in=True)
    songs, err = await dailyrec.fetch_daily_songs(app.state.musicbox_client, 10)
    assert err == ""
    assert len(songs) == 3
    assert songs[0]["song_name"] == "晴天"
    assert calls["/api/v1/recommend/daily"] == 1


@pytest.mark.anyio
async def test_fetch_daily_songs_respects_limit(monkeypatch):
    _wire(monkeypatch, logged_in=True)
    songs, err = await dailyrec.fetch_daily_songs(app.state.musicbox_client, 2)
    assert err == ""
    assert len(songs) == 2


@pytest.mark.anyio
async def test_fetch_daily_songs_not_logged_in(monkeypatch):
    _wire(monkeypatch, logged_in=False)
    songs, err = await dailyrec.fetch_daily_songs(app.state.musicbox_client, 10)
    assert songs == []
    assert err == "not_logged_in"


@pytest.mark.anyio
async def test_fetch_daily_songs_http_error(monkeypatch):
    _wire(monkeypatch, logged_in=True, daily_status=502)
    songs, err = await dailyrec.fetch_daily_songs(app.state.musicbox_client, 10)
    assert songs == []
    assert err == "http_502"


@pytest.mark.anyio
async def test_fetch_daily_songs_network_error(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接被拒绝")

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(boom), base_url="http://127.0.0.1:8770"
    )
    songs, err = await dailyrec.fetch_daily_songs(app.state.musicbox_client, 10)
    assert songs == []
    assert err == "unreachable"


@pytest.mark.anyio
async def test_fetch_daily_songs_empty_and_bad_json(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/recommend/daily":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8770"
    )
    songs, err = await dailyrec.fetch_daily_songs(app.state.musicbox_client, 10)
    assert songs == [] and err == "empty"

    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all",
                              headers={"content-type": "application/json"})

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(bad_json), base_url="http://127.0.0.1:8770"
    )
    songs, err = await dailyrec.fetch_daily_songs(app.state.musicbox_client, 10)
    assert songs == [] and err == "bad_json"


@pytest.mark.anyio
async def test_fetch_daily_songs_no_client():
    songs, err = await dailyrec.fetch_daily_songs(None, 10)
    assert songs == [] and err == "no_client"


# ------------------------------------------------------- build_daily_items ----

@pytest.mark.anyio
async def test_build_daily_items_enriches_cover_and_quality(monkeypatch):
    _wire(monkeypatch, logged_in=True)
    items, err = await dailyrec.build_daily_items(app.state.musicbox_client, 10)
    assert err == ""
    assert len(items) == 3
    first = items[0]
    assert first["id"] == "netease:d1"
    assert first["source"] == "netease"
    assert first["title"] == "晴天"
    assert first["artist"] == "周杰伦"
    assert first["duration_s"] == 269
    # SQ → 无损，且封面由 /songs/detail 补齐
    assert first["ext"] == "flac"
    assert first["cover_url"] == "http://pic/d1.jpg"
    # LD → mp3
    assert items[1]["ext"] == "mp3"


@pytest.mark.anyio
async def test_build_daily_items_dedupes_by_song_id(monkeypatch):
    dup_rows = DAILY_ROWS + [DAILY_ROWS[0]]
    _wire(monkeypatch, logged_in=True, rows=dup_rows)
    items, err = await dailyrec.build_daily_items(app.state.musicbox_client, 10)
    assert err == ""
    assert len(items) == 3


@pytest.mark.anyio
async def test_build_daily_items_survives_detail_failure(monkeypatch):
    """详情补齐失败时仍应返回曲目（只是没封面）。"""
    calls = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        if path == "/api/v1/auth/status":
            return httpx.Response(200, json={"ok": True, "data": {"logged_in": True}})
        if path == "/api/v1/recommend/daily":
            return httpx.Response(200, json={"ok": True, "data": DAILY_ROWS})
        if path == "/api/v1/songs/detail":
            return httpx.Response(500)
        return httpx.Response(404)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8770"
    )
    items, err = await dailyrec.build_daily_items(app.state.musicbox_client, 10)
    assert err == ""
    assert len(items) == 3
    assert all(it["cover_url"] == "" for it in items)


# ------------------------------------------------------ get_or_build_daily ----

@pytest.mark.anyio
async def test_get_or_build_daily_requires_login(monkeypatch, tmp_path):
    """未登录不出日推：status=unavailable、tracks 空、且不写缓存。"""
    _wire(monkeypatch, logged_in=False)
    monkeypatch.setenv("FNMUSIC_DAILY_ENABLED", "true")

    bundle = await dailyrec.get_or_build_daily(
        "user-rec-1", app.state.musicbox_client, build_online_track
    )
    assert bundle["status"] == "unavailable"
    assert bundle["reason"] == "not_logged_in"
    assert bundle["tracks"] == []
    assert dailyrec.load_daily_cache("user-rec-1", bundle["day"]) is None


@pytest.mark.anyio
async def test_get_or_build_daily_disabled(monkeypatch):
    _wire(monkeypatch, logged_in=True)
    monkeypatch.setenv("FNMUSIC_DAILY_ENABLED", "false")
    bundle = await dailyrec.get_or_build_daily(
        "user-rec-1", app.state.musicbox_client, build_online_track
    )
    assert bundle["status"] == "unavailable"
    assert bundle["reason"] == "disabled"


@pytest.mark.anyio
async def test_get_or_build_daily_disabled_without_client(monkeypatch):
    monkeypatch.setenv("FNMUSIC_DAILY_ENABLED", "true")
    bundle = await dailyrec.get_or_build_daily("user-rec-1", None, build_online_track)
    assert bundle["status"] == "unavailable"
    assert bundle["reason"] == "disabled"


@pytest.mark.anyio
async def test_get_or_build_daily_builds_and_caches(monkeypatch):
    calls = _wire(monkeypatch, logged_in=True)
    bundle = await dailyrec.get_or_build_daily(
        "user-rec-1", app.state.musicbox_client, build_online_track, limit=20
    )
    assert bundle["status"] == "partial"  # 只有 3 首，未达到 limit
    assert len(bundle["tracks"]) == 3
    assert all(t["guid"].startswith("online:netease:") for t in bundle["tracks"])
    assert bundle["source"] == "netease_daily"
    assert bundle["playlist"]["trackCount"] == 3
    assert bundle["playlist"]["isDaily"] is True
    assert dailyrec.is_daily_playlist_guid(bundle["guid"])
    # 飞牛前端 _h() 需要的形状
    t0 = bundle["tracks"][0]
    assert isinstance(t0["artists"], list) and isinstance(t0["album"], dict)
    assert isinstance(t0["genres"], list)
    assert t0["duration"] == 269000

    # 已落盘
    assert dailyrec.load_daily_cache("user-rec-1", bundle["day"]) is not None

    # 二次调用命中缓存，不再打上游
    before = calls.get("/api/v1/recommend/daily", 0)
    bundle2 = await dailyrec.get_or_build_daily(
        "user-rec-1", app.state.musicbox_client, build_online_track, limit=20
    )
    assert bundle2["tracks"] == bundle["tracks"]
    assert calls.get("/api/v1/recommend/daily", 0) == before


@pytest.mark.anyio
async def test_get_or_build_daily_force_ignores_cache(monkeypatch):
    calls = _wire(monkeypatch, logged_in=True)
    await dailyrec.get_or_build_daily("u", app.state.musicbox_client, build_online_track)
    n1 = calls["/api/v1/recommend/daily"]
    await dailyrec.get_or_build_daily(
        "u", app.state.musicbox_client, build_online_track, force=True
    )
    assert calls["/api/v1/recommend/daily"] == n1 + 1


@pytest.mark.anyio
async def test_get_or_build_daily_limit_slices(monkeypatch):
    _wire(monkeypatch, logged_in=True)
    bundle = await dailyrec.get_or_build_daily(
        "u", app.state.musicbox_client, build_online_track, limit=2
    )
    assert len(bundle["tracks"]) == 2
    assert bundle["status"] == "ready"


@pytest.mark.anyio
async def test_get_or_build_daily_isolated_per_user(monkeypatch):
    """不同飞牛用户各自一份缓存与 guid（红心隔离）。"""
    _wire(monkeypatch, logged_in=True)
    b1 = await dailyrec.get_or_build_daily("alice", app.state.musicbox_client, build_online_track)
    b2 = await dailyrec.get_or_build_daily("bob", app.state.musicbox_client, build_online_track)
    assert b1["guid"] != b2["guid"]
    assert "alice" in b1["guid"] and "bob" in b2["guid"]
    assert dailyrec.load_daily_cache("alice", b1["day"]) is not None
    assert dailyrec.load_daily_cache("bob", b2["day"]) is not None


@pytest.mark.anyio
async def test_get_or_build_daily_all_unplayable(monkeypatch):
    """日推全是无 id 的畸形数据 → unavailable 而非空歌单。"""
    _wire(monkeypatch, logged_in=True, rows=[{"song_name": "无 id"}])
    bundle = await dailyrec.get_or_build_daily(
        "u", app.state.musicbox_client, build_online_track
    )
    assert bundle["status"] == "unavailable"
    assert bundle["tracks"] == []


# -------------------------------------------------------------- caching ----

def test_purge_stale_daily_cache_keeps_today_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    for day in ("20260101", "20260102", "20260103"):
        dailyrec.save_daily_cache("u1", day, {"day": day, "status": "ready", "tracks": [{}]})
    dailyrec.purge_stale_daily_cache("u1", "20260103")
    folder = tmp_path / "rc" / "u1"
    remaining = sorted(p.name for p in folder.iterdir())
    assert remaining == ["20260103.json"]


def test_load_daily_cache_rejects_bad_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "rc"))
    day = "20260105"
    # 日期不匹配
    dailyrec.save_daily_cache("u1", "20260106", {"day": "20260101", "status": "ready", "tracks": [{}]})
    assert dailyrec.load_daily_cache("u1", day) is None
    # status 非 ready/partial
    dailyrec.save_daily_cache("u1", day, {"day": day, "status": "unavailable", "tracks": [{"a": 1}]})
    assert dailyrec.load_daily_cache("u1", day) is None
    # tracks 为空
    dailyrec.save_daily_cache("u1", day, {"day": day, "status": "ready", "tracks": []})
    assert dailyrec.load_daily_cache("u1", day) is None
    # 正常载荷可读出
    dailyrec.save_daily_cache("u1", day, {"day": day, "status": "ready", "tracks": [{"guid": "g"}]})
    assert dailyrec.load_daily_cache("u1", day)["tracks"][0]["guid"] == "g"


def test_empty_daily_bundle_shape():
    bundle = dailyrec.empty_daily_bundle("u1", "not_logged_in")
    assert bundle["tracks"] == []
    assert bundle["status"] == "unavailable"
    assert bundle["reason"] == "not_logged_in"
    assert bundle["playlist"]["trackCount"] == 0
    assert bundle["playlist"]["isDaily"] is True
    assert dailyrec.is_daily_playlist_guid(bundle["guid"])


def test_stamp_playlist_tracks_fills_required_fields():
    stamped = dailyrec.stamp_playlist_tracks(
        [{"guid": "online:netease:1", "title": "t", "artist": "a", "artists": [{"name": "a"}],
          "album": {"name": "al"}}]
    )
    t = stamped[0]
    for key in ("createdAt", "updatedAt", "isFavorite", "isCue", "accessStatus"):
        assert key in t
    assert t["artists"][0]["guid"] == "online:netease:1:artist"
    assert t["album"]["releaseDate"] == ""


# ------------------------------------------------------ play history ----

def test_play_history_record_dedupes_and_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    dailyrec.record_online_play("u1", "online:netease:1", {"title": "A"})
    dailyrec.record_online_play("u1", "online:netease:2", {"title": "B"})
    dailyrec.record_online_play("u1", "online:netease:1", {"title": "A2"})
    items = dailyrec.load_online_play_history("u1")
    guids = [x["guid"] for x in items]
    assert guids == ["online:netease:2", "online:netease:1"], "重播应置顶且不重复"
    assert items[-1]["track"]["title"] == "A2"

    for i in range(600):
        dailyrec.record_online_play("u1", f"online:netease:bulk{i}")
    assert len(dailyrec.load_online_play_history("u1")) <= 500


def test_load_online_play_history_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "nope"))
    assert dailyrec.load_online_play_history("ghost") == []


def test_load_online_play_history_tolerates_corrupt_file(tmp_path, monkeypatch):
    d = tmp_path / "ph"
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(d))
    os.makedirs(d, exist_ok=True)
    (d / "u1.json").write_text("{ not json", encoding="utf-8")
    assert dailyrec.load_online_play_history("u1") == []


def test_event_report_records_online_play(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_auth_user()), base_url="http://unix"
    )
    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/event/report",
            json={
                "events": [
                    {"eventType": "track_play", "occurredAt": 1,
                     "payload": {"trackGUID": "online:netease:1"}}
                ]
            },
        )
        assert resp.json()["code"] == 0
    items = dailyrec.load_online_play_history("user-rec-1")
    assert items[-1]["guid"] == "online:netease:1"


def test_play_history_merges_online(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "ph"))
    dailyrec.record_online_play(
        "user-rec-1", "online:netease:99", {"title": "在线歌", "artist": "歌手"}
    )
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_auth_user()), base_url="http://unix"
    )
    with TestClient(app) as client:
        body = client.get("/music/api/v1/play-history/list").json()
    guids = [x["guid"] for x in body["data"]["list"]]
    assert "online:netease:99" in guids
    assert "local-track-1" in guids
    assert body["data"]["total"] == 2


# ------------------------------------------------- playlist_list endpoint ----

def test_playlist_list_injects_daily_when_logged_in(monkeypatch):
    _wire(monkeypatch, logged_in=True)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/playlist/list")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        first = body["data"]["list"][0]
        assert first["isDaily"] is True
        assert dailyrec.is_daily_playlist_guid(first["guid"])
        assert first["trackCount"] == 3
        # 官方歌单保留在日推之后
        assert body["data"]["total"] == 2
        assert body["data"]["list"][1]["guid"] == "localpl"

        detail = client.get(f"/music/api/v1/playlist/detail?guid={first['guid']}")
        assert detail.json()["data"]["guid"] == first["guid"]

        tracks = client.get(
            f"/music/api/v1/track/playlist-detail/list?playlistGUID={first['guid']}&page=1&size=50"
        )
        tj = tracks.json()
        assert tj["code"] == 0
        assert tj["data"]["total"] == 3
        assert tj["data"]["list"][0]["guid"].startswith("online:netease:")
        assert isinstance(tj["data"]["list"][0]["artists"], list)

        batch = client.get(f"/music/api/v1/playlist/batch-detail?guids={first['guid']},localpl")
        bj = batch.json()
        assert any(dailyrec.is_daily_playlist_guid(str(x.get("guid"))) for x in bj["data"]["list"])


def test_playlist_list_not_injected_when_logged_out(monkeypatch):
    """单源时代未登录 = 拿不到账号个性化日推，绝不注入空壳歌单。"""
    _wire(monkeypatch, logged_in=False)
    with TestClient(app) as client:
        body = client.get("/music/api/v1/playlist/list").json()
    assert body["code"] == 0
    guids = [x["guid"] for x in body["data"]["list"]]
    assert not any(dailyrec.is_daily_playlist_guid(g) for g in guids)
    assert guids == ["localpl"]
    assert body["data"]["total"] == 1


def test_playlist_list_drops_stale_daily_entry(monkeypatch):
    """昨天残留的日推条目必须被剔除，避免列表里出现两个/过期日推。"""
    _wire(monkeypatch, logged_in=True)
    yesterday = dailyrec.daily_playlist_guid("20200101", "user-rec-1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlist/list"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"guid": yesterday, "name": "每日推荐 01-01", "coverId": "x",
                             "createdAt": 1, "updatedAt": 1, "trackCount": 9},
                            {"guid": "localpl", "name": "牛一", "coverId": "c1",
                             "createdAt": 1, "updatedAt": 1},
                        ],
                        "total": 2,
                    },
                },
            )
        return _auth_user()(request)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://unix"
    )
    with TestClient(app) as client:
        body = client.get("/music/api/v1/playlist/list").json()

    guids = [x["guid"] for x in body["data"]["list"]]
    assert yesterday not in guids
    daily_guids = [g for g in guids if dailyrec.is_daily_playlist_guid(g)]
    assert len(daily_guids) == 1, "只应保留当天一个日推"
    assert body["data"]["total"] == 2  # 今日日推 + localpl


def test_playlist_list_unauth_passthrough(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://unix"
    )
    with TestClient(app) as client:
        assert client.get("/music/api/v1/playlist/list").json()["code"] == 99999


def test_playlist_detail_non_daily_passthrough(monkeypatch):
    """非日推 guid 必须原样透传给官方后端，不能自己编内容。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "data": {"guid": "localpl", "name": "牛一"}})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://unix"
    )
    with TestClient(app) as client:
        body = client.get("/music/api/v1/playlist/detail?guid=localpl").json()
    assert body["data"]["guid"] == "localpl"
    assert any("playlist/detail" in p for p in seen)


def test_static_cover_for_daily_playlist_redirects_to_first_track(monkeypatch):
    calls = _wire(monkeypatch, logged_in=True)
    dailyrec.save_daily_cache(
        "user-rec-1",
        dailyrec.today_key(),
        {
            "day": dailyrec.today_key(),
            "status": "ready",
            "tracks": [{"guid": "online:netease:d1", "title": "晴天", "artist": "周杰伦"}],
        },
    )

    def cover_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/d1/info":
            return httpx.Response(
                200,
                json={"ok": True, "data": {
                    "name": "晴天", "ar": [{"name": "周杰伦"}],
                    "al": {"name": "叶惠美", "picUrl": "http://pic/cover.jpg"},
                    "dt": 269000, "sq": {"size": 1}},
                },
            )
        if request.url.path == "/api/v1/song/d1/lyric":
            return httpx.Response(200, json={"ok": True, "data": {"lyric": "", "tlyric": ""}})
        return httpx.Response(404)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(cover_handler), base_url="http://127.0.0.1:8770"
    )
    guid = dailyrec.daily_playlist_guid(dailyrec.today_key(), "user-rec-1")
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/static/cover?guid={guid}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://pic/cover.jpg"
