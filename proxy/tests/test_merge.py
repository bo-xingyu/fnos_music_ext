"""Tests for fnmusic-ext proxy (search merge, streaming tee cache, passthrough)."""
import os
import pytest
import httpx
from fastapi.testclient import TestClient

from proxy.app import (
    app,
    CONF,
    build_online_track,
    _SEARCH_CACHE,
    find_cache_file,
    library_basename,
    remember_media_path,
    write_audio_tags,
)


def _assert_playback_metadata_shape(data: dict, guid: str) -> None:
    """飞牛 _h()：data.track.genres.join / album 对象 / artists 列表，缺一即跳过播放。"""
    track = data["track"]
    assert track["guid"] == guid
    assert isinstance(track["artists"], list)
    assert isinstance(track["genres"], list)
    " / ".join(track["genres"])
    assert isinstance(track["album"], dict)
    assert "name" in track["album"]
    spec = data["audioSpec"]
    assert spec.get("format")
    assert spec.get("channel") == 2
    assert "size" in spec


# ---------------------------------------------------------------------------
# 网易云单源 mock 辅助（v2.0 起唯一在线音源）
# ---------------------------------------------------------------------------


def _netease_song_info(song_id="228908", title="晴天", artist="周杰伦", album="叶惠美",
                       duration_ms=269000, cover="http://img.test/c.jpg",
                       lossless=False, size=28000000):
    """构造 musicbox /api/v1/song/{id}/info 的 data 段。

    lossless=True 时带 sq 字段 —— 代理据此判定 ext=flac，否则 mp3。
    """
    data = {
        "name": title,
        "ar": [{"name": artist}],
        "al": {"name": album, "picUrl": cover},
        "dt": duration_ms,
    }
    data["sq" if lossless else "h"] = {"size": size}
    return data


def _wire_netease(monkeypatch, *, song_id="228908", info=None, lyric="", play_url=None,
                  cdn=None, upstream_handler=None, logged_in=True, calls=None):
    """装好网易云单源链路的全部 mock。

    三层：
      1. ``app.state.upstream_client``  —— 官方后端，默认 500（误透传会立刻暴露）
      2. ``app.state.musicbox_client``  —— 音源服务：/api/v1/song/{id}/{info,lyric,url}、
         /api/v1/auth/status、/healthz
      3. CDN 直链 —— ``stream_track`` 内部会新建一个裸 httpx.AsyncClient 拉直链，
         无法通过 app.state 注入，只能 monkeypatch AsyncClient.__init__。
         ``cdn`` 传 (status, content, headers) 元组即启用该拦截。

    ``play_url`` 为 None 表示该曲目拿不到直链（未登录无权益 / 曲目下架）。
    """
    from proxy import netease_auth

    if calls is None:
        calls = {}

    def _count(key):
        calls[key] = calls.get(key, 0) + 1

    def _upstream(request: httpx.Request) -> httpx.Response:
        _count("upstream")
        if upstream_handler is not None:
            return upstream_handler(request)
        return httpx.Response(500, text="Should not hit upstream")

    def _musicbox(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        _count(path)
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/auth/status":
            return httpx.Response(
                200, json={"ok": True, "data": {"logged_in": logged_in, "nickname": "测试账号"}}
            )
        if path == f"/api/v1/song/{song_id}/info":
            if info is None:
                return httpx.Response(404, json={"ok": False})
            return httpx.Response(200, json={"ok": True, "data": info})
        if path == f"/api/v1/song/{song_id}/lyric":
            return httpx.Response(200, json={"ok": True, "data": {"lyric": lyric, "tlyric": ""}})
        if path == f"/api/v1/song/{song_id}/url":
            if not play_url:
                return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})
            return httpx.Response(200, json={"ok": True, "data": {"code": 200, "url": play_url}})
        return httpx.Response(404, json={"ok": False})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_musicbox), base_url="http://127.0.0.1:8770"
    )
    netease_auth.invalidate_state()

    if cdn is not None:
        cdn_status, cdn_content, cdn_headers = cdn
        orig_init = httpx.AsyncClient.__init__

        def _mock_init(self, *args, **kwargs):
            # 只拦截裸 client（stream_track 建的直链 client）；带 base_url / 已有 transport 的放过
            if "base_url" not in kwargs and not kwargs.get("transport"):
                kwargs["transport"] = httpx.MockTransport(
                    lambda request: httpx.Response(cdn_status, content=cdn_content, headers=cdn_headers)
                )
            orig_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_init)

    return calls


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    cache_dir = str(tmp_path / "cache")
    library_dir = str(tmp_path / "library")
    fav_dir = str(tmp_path / "online_favorites")
    os.makedirs(library_dir, exist_ok=True)
    os.makedirs(fav_dir, exist_ok=True)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "library_dir", library_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "search_list_path", "data.list")
    monkeypatch.setitem(CONF, "online_limit", 30)
    monkeypatch.setitem(CONF, "netease_search_limit", 50)
    monkeypatch.setitem(CONF, "merge_suggest", False)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "netease_wait_s", 3.0)
    monkeypatch.setitem(CONF, "netease_quality", "lossless")
    monkeypatch.setitem(CONF, "search_cache_ttl", 604800.0)
    monkeypatch.setitem(CONF, "late_page_wait_s", 5.0)

    def default_musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False, "data": []})

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(default_musicbox_handler), base_url="http://127.0.0.1:8770"
    )


def test_library_basename_omits_source_id():
    assert library_basename("晴天", "周杰伦") == "周杰伦 - 晴天"
    assert library_basename("不再犹豫", "BEYOND") == "BEYOND - 不再犹豫"
    assert library_basename("晴天", "") == "晴天"
    assert "600902" not in library_basename("晴天", "周杰伦")


def test_find_cache_file_legacy_id_name_and_ref(tmp_path):
    guid = "online:migu:600902000006889366"
    legacy = os.path.join(CONF["library_dir"], "晴天 - 600902000006889366.mp3")
    with open(legacy, "wb") as f:
        f.write(b"x" * 2048)
    assert find_cache_file(guid) == legacy

    renamed = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.mp3")
    os.rename(legacy, renamed)
    remember_media_path(guid, renamed)
    assert find_cache_file(guid) == renamed


def test_write_audio_tags_id3(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    try:
        import mutagen
    except ImportError:
        pytest.skip("mutagen not available")
    path = str(tmp_path / "sample.mp3")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "0.2",
            "-q:a",
            "9",
            path,
        ],
        check=True,
        capture_output=True,
    )
    write_audio_tags(path, title="晴天", artist="周杰伦", album="叶惠美")
    from mutagen import File as MutagenFile

    tagged = MutagenFile(path, easy=True)
    assert tagged is not None
    assert tagged["title"] == ["晴天"]
    assert tagged["artist"] == ["周杰伦"]
    assert tagged["album"] == ["叶惠美"]


def test_search_track_merge_success():
    """用例 a: 上游 code 0 + 列表 → 合并追加 online 条目、guid 前缀正确。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "code": 0,
            "msg": "OK",
            "data": {
                "list": [
                    {
                        "guid": "local:101",
                        "title": "夜曲",
                        "artist": "周杰伦",
                        "album": "十一月的萧邦",
                        "duration": 226000,
                    }
                ],
                "total": 1,
            },
        }
        return httpx.Response(200, json=data)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "ok": True,
            "data": [
                {
                    "song_id": "228908",
                    "song_name": "晴天",
                    "artist": "周杰伦",
                    "album_name": "叶惠美",
                    "duration": 269,
                    "quality": "SQ 2.4M",
                }
            ],
        }
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=夜曲")
        assert resp.status_code == 200
        res_json = resp.json()
        assert res_json["code"] == 0
        items = res_json["data"]["list"]
        assert len(items) == 2
        # 本地条目保持不变
        assert items[0]["guid"] == "local:101"
        assert items[0]["title"] == "夜曲"
        # 在线条目合并追加且格式正确
        assert items[1]["guid"] == "online:netease:228908"
        assert items[1]["title"] == "晴天"
        assert items[1]["artist"] == "周杰伦"
        assert items[1]["albumName"] == "叶惠美" and items[1]["album"]["name"] == "叶惠美"
        assert items[1]["duration_ms"] == 269000
        assert items[1]["durationMs"] == 269000
        assert items[1]["codec"] == "flac"
        assert items[1]["format"] == "flac"
        assert items[1]["is_online"] is True
        assert items[1]["artists"][0]["name"] == "周杰伦"
        assert res_json["data"]["total"] == 2


def test_search_track_merge_with_q_param():
    """前端打包使用 q 而不是 keyword，在线合并仍要生效。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "msg": "OK", "data": {"list": [], "total": 0}},
        )

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("keyword") == "晴天"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "1",
                        "song_name": "晴天",
                        "artist": "周杰伦",
                        "album_name": "叶惠美",
                        "duration": 269,
                        "quality": "LD 128k",
                    }
                ],
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert len(items) == 1
        assert items[0]["guid"] == "online:netease:1"


def test_search_track_declares_lossless_from_netease_quality():
    """网易云 SQ/HR 曲目声明为 flac，普通曲目声明为 mp3（不再一律伪装 mp3）。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "", "data": {"list": [], "total": 0}})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {"song_id": "flac1", "song_name": "不再犹豫", "artist": "Beyond",
                         "album_name": "犹豫", "duration": 240, "quality": "SQ 2.4M"},
                        {"song_id": "hr1", "song_name": "海阔天空", "artist": "Beyond",
                         "album_name": "乐与怒", "duration": 326, "quality": "HR"},
                        {"song_id": "mp31", "song_name": "光辉岁月", "artist": "Beyond",
                         "album_name": "命运派对", "duration": 300, "quality": "LD 128k"},
                    ],
                },
            )
        if request.url.path == "/api/v1/songs/detail":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=不再犹豫&page=1&size=50")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert resp.json()["data"]["total"] == 3
        by_guid = {it["guid"]: it for it in items}

        flac = by_guid["online:netease:flac1"]
        assert flac["format"] == "flac"
        assert flac["audioSpec"]["format"] == "flac"
        assert flac["audioSpec"]["path"].endswith(".flac")
        assert flac["coverId"] == "online:netease:flac1"

        assert by_guid["online:netease:hr1"]["format"] == "flac"
        assert by_guid["online:netease:mp31"]["format"] == "mp3"
        assert by_guid["online:netease:mp31"]["audioSpec"]["path"].endswith(".mp3")


def test_build_online_track_preserves_all_container_formats():
    """audioSpec 格式化表：任意容器都不能被压成 mp3。

    网易云只产出 flac/mp3，但落盘缓存可能是历史多源时代留下的其他容器，
    格式化别名表因此必须继续覆盖全量。
    """
    cases = {
        "flac": "flac", "mp3": "mp3", "mpeg": "mp3", "wav": "wav", "pcm": "wav",
        "ogg": "ogg", "vorbis": "ogg", "opus": "opus", "m4a": "m4a", "aac": "m4a",
        "mp4": "m4a", "alac": "m4a", "ape": "ape", "wv": "wv", "wavpack": "wv",
        "dsf": "dsf", "dff": "dff", "tta": "tta", "aiff": "aiff", "aif": "aiff",
        "wma": "wma", "audio/mpeg": "mp3", "audio/flac": "flac",
    }
    for raw, expected in cases.items():
        track = build_online_track(
            {"id": "netease:1", "source": "netease", "title": "t", "artist": "a",
             "duration_s": 100, "ext": raw}
        )
        assert track["format"] == expected, raw
        assert track["audioSpec"]["format"] == expected, raw
        assert track["audioSpec"]["codec"] == expected, raw
        assert track["audioSpec"]["container"] == expected, raw
        assert track["audioSpec"]["path"].endswith(f".{expected}"), raw
        assert track["codec"] == expected, raw

    # 无损容器带 bitDepth，有损不带
    assert build_online_track({"id": "netease:1", "source": "netease", "title": "t",
                               "ext": "flac"})["audioSpec"]["bitDepth"] == 16
    assert "bitDepth" not in build_online_track(
        {"id": "netease:1", "source": "netease", "title": "t", "ext": "mp3"}
    )["audioSpec"]


def test_search_track_merges_when_upstream_data_null_list_missing():
    """上游 code=0 但 data 为 null 时仍应合成 list 并合并在线结果。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "1",
                        "song_name": "不再犹豫",
                        "artist": "Beyond",
                        "duration": 240,
                        "quality": "SQ",
                    }
                ],
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=不再犹豫")
        items = resp.json()["data"]["list"]
        assert len(items) == 1
        assert items[0]["guid"] == "online:netease:1"
        assert items[0]["format"] == "flac"


def test_metadata_uses_source_ext_not_forced_mp3(monkeypatch):
    """metadata 按源站真实格式声明 audioSpec，不把无损压成 mp3。"""
    guid = "online:netease:flac1"
    calls = _wire_netease(
        monkeypatch,
        song_id="flac1",
        info=_netease_song_info("flac1", title="不再犹豫", artist="Beyond", album="犹豫",
                                duration_ms=240000, lossless=True, size=28000000),
        lyric="[00:00.00]不再犹豫\n",
    )

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/metadata?guid={guid}")
        spec = resp.json()["data"]["audioSpec"]
        assert spec["format"] == "flac"
        assert spec["codec"] == "flac"
        assert spec["path"].endswith(".flac")
        assert spec["size"] == 28000000
        _assert_playback_metadata_shape(resp.json()["data"], guid=guid)
    # metadata 只读 info，不该去请求播放直链
    assert calls.get("/api/v1/song/flac1/url", 0) == 0


def test_search_track_upstream_unauthorized():
    """用例 b: 上游 99999 (未登录/INVALID TOKEN) → 原样透传不合并，且快速路径绝不调用 musicdl。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {"code": 99999, "msg": "INVALID TOKEN", "data": None}
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        # 并行预取可能被触发，但 401 路径必须立刻返回、不依赖该响应
        return httpx.Response(200, json={"ok": True, "items": [{"id": "should-not-merge"}]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=test")
        assert resp.status_code == 200
        res_json = resp.json()
        assert res_json["code"] == 99999
        assert res_json["msg"] == "INVALID TOKEN"
        assert res_json["data"] is None


def test_search_track_upstream_http_401_fast_path():
    """上游 HTTP 401 非 200 响应 → 快速路径原样透传，不调用 musicdl。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 99999, "msg": "UNAUTHORIZED"})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=test")
        assert resp.status_code == 401
        assert resp.json()["code"] == 99999


def test_search_suggest_upstream_unauthorized_fast_path(monkeypatch):
    """开启 suggest 合并时，若上游返回 99999，快速路径直接返回，不调用 musicdl。"""
    monkeypatch.setitem(CONF, "merge_suggest", True)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": [{"title": "should-not-merge"}]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=test")
        assert resp.status_code == 200
        assert resp.json()["code"] == 99999


def test_search_track_deduplication():
    """用例 c: title+artist 与上游重复时去重。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "code": 0,
            "msg": "OK",
            "data": {
                "list": [
                    {
                        "guid": "local:101",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                    }
                ],
                "total": 1,
            },
        }
        return httpx.Response(200, json=data)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "ok": True,
            "data": [
                {
                    "song_id": "228908",
                    "song_name": "晴天",
                    "artist": "周杰伦",
                    "album_name": "叶惠美",
                    "duration": 269,
                    "quality": "LD",
                },
                {
                    "song_id": "228909",
                    "song_name": "晴天 (Live)",
                    "artist": "周杰伦",
                    "album_name": "演唱会",
                    "duration": 300,
                    "quality": "LD",
                },
            ],
        }
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert len(items) == 2
        assert items[0]["guid"] == "local:101"
        assert items[1]["guid"] == "online:netease:228909"
        assert items[1]["title"] == "晴天 (Live)"


def test_stream_online_guid_range_and_tee_cache(monkeypatch):
    """在线播放全链路：解析网易云直链 → 200 流式返回 → tee 落盘到曲库 + 同名 .lrc。"""
    # 文件头必须是真的 MP3（ID3 + MPEG 帧同步），否则落盘时会被嗅探成别的容器
    audio_content = b"ID3\x03\x00\x00\x00\x00\x00\xff\xfb\x90\x00FAKE_MP3_STREAM" * 50
    content_len = str(len(audio_content))
    guid = "online:netease:228908"

    calls = _wire_netease(
        monkeypatch,
        song_id="228908",
        info=_netease_song_info("228908", lossless=False, size=len(audio_content)),
        lyric="[00:00.00]晴天 - 周杰伦\n[00:10.00]故事的小黄花",
        play_url="http://audio.test/song.mp3",
        cdn=(200, audio_content, {
            "Content-Type": "audio/mpeg",
            "Content-Length": content_len,
            "Accept-Ranges": "bytes",
        }),
    )

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid={guid}")
        assert resp.status_code == 200
        assert resp.content == audio_content
        assert resp.headers.get("content-length") == content_len

        # 落盘到飞牛曲库目录：歌手 - 歌名.ext（不含源站 id）+ 同名 .lrc
        cache_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.mp3")
        assert os.path.exists(cache_file)
        assert "228908" not in os.path.basename(cache_file)
        with open(cache_file, "rb") as f:
            assert f.read() == audio_content

        lyric_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.lrc")
        assert os.path.exists(lyric_file)
        with open(lyric_file, encoding="utf-8") as f:
            assert "晴天" in f.read()

        # 官方后端全程不参与在线播放
        assert calls.get("upstream", 0) == 0


def test_cache_ext_follows_real_file_header_not_declaration(monkeypatch):
    """曲目声明 flac、CDN 实际给 MP3 时，落盘扩展名必须按**文件头**纠正成 .mp3。

    回归 bug：以前只信曲目声明 → 落出「.flac 装 MP3」的坏文件，
    写标签报 “is not a valid FLAC file”，本地曲库还把它当无损匹配。
    """
    audio_content = b"ID3\x03\x00\x00\x00\x00\x00\xff\xfb\x90\x00FAKE_MP3_STREAM" * 50

    _wire_netease(
        monkeypatch,
        song_id="228908",
        # lossless=True → 曲目信息里带 sq，代理原本会判定 ext=flac
        info=_netease_song_info("228908", lossless=True, size=len(audio_content)),
        play_url="http://audio.test/song.flac",
        cdn=(200, audio_content, {
            "Content-Type": "audio/mpeg",
            "Content-Length": str(len(audio_content)),
        }),
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:228908")
        assert resp.status_code == 200
        assert resp.content == audio_content

    files = os.listdir(CONF["library_dir"])
    assert not [f for f in files if f.endswith(".flac")], f"仍落出了假 flac：{files}"
    assert "周杰伦 - 晴天.mp3" in files, files


def test_stream_cache_hit_never_touches_source(monkeypatch):
    """已有完整缓存文件时直接本地服务，绝不回源（省外网流量）。"""
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    existing = b"EXISTING_CACHED_AUDIO_CONTENT" * 50
    cache_file = os.path.join(CONF["cache_dir"], "online_netease_228908.mp3")
    with open(cache_file, "wb") as f:
        f.write(existing)

    calls = _wire_netease(
        monkeypatch,
        song_id="228908",
        info=_netease_song_info("228908"),
        play_url="http://audio.test/should-not-be-called.mp3",
        cdn=(200, b"MUST NOT APPEAR", {"Content-Type": "audio/mpeg"}),
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:228908")
        assert resp.status_code == 200
        assert resp.content == existing

        resp2 = client.get(
            "/music/api/v1/track/stream?guid=online:netease:228908",
            headers={"Range": "bytes=0-9"},
        )
        assert resp2.status_code == 206
        assert resp2.content == existing[:10]

    assert calls.get("/api/v1/song/228908/url", 0) == 0, "命中缓存时不应解析直链"
    files = os.listdir(CONF["cache_dir"])
    assert not any(f.endswith(".part") for f in files)


def test_stream_url_cache_reuse_and_stale_retry(monkeypatch):
    """v2.7 播放直链短缓存：有效期内复用（不重取链）；缓存直链被 CDN 拒绝时
    丢弃缓存、强制重取，拿到**不同**的新链才重试一次。"""
    guid = "online:netease:334455"
    audio = b"URL_CACHE_AUDIO_CONTENT" * 60
    state = {"url": "http://cdn.test/old.mp3", "stale": False,
             "resolve_calls": 0, "cdn_old_hits": 0}

    def _musicbox(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/detail":
            return httpx.Response(
                200, json={"ok": True, "data": {"logged_in": True, "nickname": "测试账号"}}
            )
        if path == "/api/v1/song/334455/info":
            return httpx.Response(200, json={"ok": True, "data": _netease_song_info("334455")})
        if path == "/api/v1/song/334455/lyric":
            return httpx.Response(200, json={"ok": True, "data": {"lyric": "", "tlyric": ""}})
        if path == "/api/v1/song/334455/url":
            state["resolve_calls"] += 1
            return httpx.Response(
                200, json={"ok": True, "data": {"code": 200, "url": state["url"]}}
            )
        return httpx.Response(404, json={"ok": False})

    def _cdn(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.mp3":
            state["cdn_old_hits"] += 1
            if state["stale"]:
                return httpx.Response(403, text="expired url")
        return httpx.Response(200, content=audio, headers={"Content-Type": "audio/mpeg"})

    from proxy import netease_auth

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_musicbox), base_url="http://127.0.0.1:8770"
    )
    netease_auth.invalidate_state()

    orig_init = httpx.AsyncClient.__init__

    def _mock_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(_cdn)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_init)

    with TestClient(app) as client:
        # 第一次播放（带非 0 起点的 Range，避免落盘缓存干扰验证）
        resp = client.get(f"/music/api/v1/track/stream?guid={guid}",
                          headers={"Range": "bytes=100-"})
        assert resp.status_code == 200
        assert resp.content == audio
        assert state["resolve_calls"] == 1

        # 第二次播放：直链仍在缓存有效期内，不应再取链
        resp2 = client.get(f"/music/api/v1/track/stream?guid={guid}",
                           headers={"Range": "bytes=100-"})
        assert resp2.status_code == 200
        assert state["resolve_calls"] == 1, "缓存有效期内复用直链，不重取"

        # 模拟直链过期：musicbox 开始发新链，旧链 CDN 一律 403
        state["url"] = "http://cdn.test/new.mp3"
        state["stale"] = True
        resp3 = client.get(f"/music/api/v1/track/stream?guid={guid}",
                           headers={"Range": "bytes=100-"})
        assert resp3.status_code == 200
        assert resp3.content == audio, "缓存直链失效后必须重取新链并重试成功"
        assert state["resolve_calls"] == 2
        assert state["cdn_old_hits"] >= 1


def test_stream_online_guid_range_0_1_safari_probe_no_cache(monkeypatch):
    """Safari 的 Range bytes=0-1 探测不产生任何缓存文件。"""
    probe_content = b"\x00\x01"
    _wire_netease(
        monkeypatch,
        song_id="228908",
        info=_netease_song_info("228908"),
        play_url="http://audio.test/probe.mp3",
        cdn=(206, probe_content, {
            "Content-Type": "audio/mpeg",
            "Content-Range": "bytes 0-1/5000",
            "Content-Length": "2",
            "Accept-Ranges": "bytes",
        }),
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=online:netease:228908",
            headers={"Range": "bytes=0-1"},
        )
        assert resp.status_code == 206
        assert resp.content == probe_content

        for d in (CONF["cache_dir"], CONF["library_dir"]):
            if os.path.exists(d):
                assert not any(f.endswith((".mp3", ".flac", ".lrc")) for f in os.listdir(d))


def test_stream_online_guid_existing_cache_no_part():
    """已存在完整缓存文件时，再次在线播放不再生成 .part。"""
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    cache_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.mp3")
    existing_content = b"EXISTING_CACHED_AUDIO_CONTENT" * 50
    with open(cache_file, "wb") as f:
        f.write(existing_content)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("musicdl should not be called when local cache exists")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:kuwo:228908")
        assert resp.status_code == 200
        assert resp.content == existing_content

        resp2 = client.get(
            "/music/api/v1/track/stream?guid=online:kuwo:228908",
            headers={"Range": "bytes=0-9"},
        )
        assert resp2.status_code == 206
        assert resp2.content == existing_content[:10]

        files = os.listdir(CONF["cache_dir"])
        assert not any(f.endswith(".part") for f in files)
        assert "online_kuwo_228908.mp3" in files
        with open(cache_file, "rb") as f:
            assert f.read() == existing_content


def test_stream_online_guid_nonzero_range_no_cache(monkeypatch):
    """Range 从非 0 开始时只转发不落盘（无法拼出完整文件）。"""
    partial_content = b"PARTIAL_STREAM_DATA"
    _wire_netease(
        monkeypatch,
        song_id="228908",
        info=_netease_song_info("228908"),
        play_url="http://audio.test/partial.mp3",
        cdn=(206, partial_content, {
            "Content-Type": "audio/mpeg",
            "Content-Range": "bytes 100-119/1000",
            "Content-Length": str(len(partial_content)),
            "Accept-Ranges": "bytes",
        }),
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=online:netease:228908",
            headers={"Range": "bytes=100-119"},
        )
        assert resp.status_code == 206
        assert resp.content == partial_content
        assert resp.headers.get("content-range") == "bytes 100-119/1000"

        assert not os.path.exists(os.path.join(CONF["cache_dir"], "online_netease_228908.mp3"))
        assert not os.path.exists(os.path.join(CONF["library_dir"], "unknown.mp3"))


def test_stream_online_guid_unavailable_404(monkeypatch):
    """拿不到真实直链（无权益 / 曲目下架）时返回飞牛格式的 404 JSON。"""
    _wire_netease(monkeypatch, song_id="notfound", info=None, play_url=None)

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:notfound")
        assert resp.status_code == 404
        assert resp.json() == {"code": 404, "msg": "online source unavailable", "data": None}


def test_stream_rejects_unsupported_legacy_source(monkeypatch):
    """旧版多音源遗留 guid 干净 404，且不打任何音源服务。"""
    calls = _wire_netease(monkeypatch, song_id="228908", info=_netease_song_info("228908"))

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:kuwo:flac1")
        assert resp.status_code == 404
        assert resp.json()["msg"] == "unsupported online source"

    assert calls.get("/api/v1/song/228908/url", 0) == 0


def test_stream_non_online_guid_passthrough():
    """用例 e: 非 online: 前缀的 guid → 透传到 unix socket。"""
    local_audio = b"LOCAL_UNIX_SOCKET_AUDIO_BYTES"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/api/v1/track/stream"
        assert request.url.params.get("guid") == "local:9999"
        assert request.headers.get("range") == "bytes=0-100"
        return httpx.Response(
            206,
            content=local_audio,
            headers={
                "Content-Type": "audio/flac",
                "Content-Range": "bytes 0-100/5000",
                "Accept-Ranges": "bytes",
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=local:9999",
            headers={"Range": "bytes=0-100"},
        )
        assert resp.status_code == 206
        assert resp.content == local_audio
        assert resp.headers.get("content-range") == "bytes 0-100/5000"


def test_online_lyrics_and_metadata(monkeypatch):
    """在线歌词与元数据合成（网易云单源）。"""
    guid = "online:netease:228908"
    lyric = "[00:00.00]晴天 - 周杰伦\n[00:10.00]故事的小黄花"
    _wire_netease(
        monkeypatch,
        song_id="228908",
        info=_netease_song_info("228908", lossless=False),
        lyric=lyric,
    )

    with TestClient(app) as client:
        # 兼容旧路径
        resp = client.get(f"/music/api/v1/track/lyrics?guid={guid}")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        assert rj["data"]["guid"] == guid
        assert "[00:00.00]晴天" in rj["data"]["lyric"]

        # 飞牛播放器实际走 /lyric/list?trackGUID=
        resp_list = client.get(f"/music/api/v1/lyric/list?trackGUID={guid}")
        assert resp_list.status_code == 200
        lj = resp_list.json()
        assert lj["code"] == 0
        assert lj["data"]["preferred"] == f"{guid}:lyric"
        assert len(lj["data"]["list"]) == 1
        item = lj["data"]["list"][0]
        assert item["guid"] == f"{guid}:lyric"
        assert item["source"] == 2
        assert item["isLRC"] is True
        assert "[00:00.00]晴天" in item["content"]

        resp2 = client.get(f"/music/api/v1/track/metadata?guid={guid}")
        assert resp2.status_code == 200
        rj2 = resp2.json()
        assert rj2["code"] == 0
        assert rj2["data"]["title"] == "晴天"
        assert rj2["data"]["artist"] == "周杰伦"
        assert rj2["data"]["duration_ms"] == 269000
        assert rj2["data"]["audioSpec"]["format"] == "mp3"
        assert rj2["data"]["audioSpec"]["codec"] == "mp3"
        assert rj2["data"]["audioSpec"]["channel"] == 2
        _assert_playback_metadata_shape(rj2["data"], guid=guid)
        assert rj2["data"]["track"]["hasLyric"] is True


def test_online_lyrics_and_metadata_source_error():
    """在线歌词/元数据获取失败时，安全返回 code 0 和空 data，绝不 500。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "failed"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/lyrics?guid=online:kuwo:err")
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "ok", "data": {}}

        resp_list = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:err")
        assert resp_list.status_code == 200
        assert resp_list.json()["code"] == 0
        assert resp_list.json()["data"]["list"] == []
        assert resp_list.json()["data"]["preferred"] == ""

        resp2 = client.get("/music/api/v1/track/metadata?guid=online:kuwo:err")
        assert resp2.status_code == 200
        # /info 失败也必须给出 _h() 可解构的 stub，否则播放器抛错后直接跳过、永不请求 stream
        _assert_playback_metadata_shape(resp2.json()["data"], guid="online:kuwo:err")


def test_lyric_cache_hit_skips_source():
    """第一次拉歌词落盘后，再次播放只读 cache/*.lrc，不再请求 musicdl。"""
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    lyric_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.lrc")
    with open(lyric_file, "w", encoding="utf-8") as f:
        f.write("[00:00.00]本地缓存的晴天\n[00:10.00]不再请求源站\n")

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("musicdl should not be called when local lyric cache exists")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert resp.status_code == 200
        item = resp.json()["data"]["list"][0]
        assert "本地缓存的晴天" in item["content"]

        resp2 = client.get("/music/api/v1/track/lyrics?guid=online:kuwo:228908")
        assert "本地缓存的晴天" in resp2.json()["data"]["lyric"]


def test_lyric_list_persists_sidecar(monkeypatch):
    """首次 /lyric/list 从网易云取回歌词后写入 .lrc sidecar，二次命中缓存。"""
    calls = _wire_netease(
        monkeypatch,
        song_id="228908",
        info=_netease_song_info("228908"),
        lyric="[00:00.00]晴天 - 周杰伦\n",
    )
    info_path = "/api/v1/song/228908/info"
    guid = "online:netease:228908"

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/lyric/list?trackGUID={guid}")
        assert resp.status_code == 200
        assert calls[info_path] == 1, "写 sidecar 需要一次 info 取标题/歌手"

        lyric_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.lrc")
        assert os.path.exists(lyric_file)
        with open(lyric_file, encoding="utf-8") as f:
            assert "晴天" in f.read()

        before = dict(calls)
        resp2 = client.get(f"/music/api/v1/lyric/list?trackGUID={guid}")
        assert "晴天" in resp2.json()["data"]["list"][0]["content"]
        assert calls == before, "命中 .lrc 缓存后不应再打音源服务"


def test_ext_healthz(monkeypatch):
    """/_ext/healthz 单源时代的返回体形状。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    monkeypatch.setitem(CONF, "daily_enabled", True)

    calls = _wire_netease(monkeypatch, logged_in=True, upstream_handler=lambda r: httpx.Response(200, json={"code": 0}))

    with TestClient(app) as client:
        resp = client.get("/_ext/healthz")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["ok"] is True
        assert rj["upstream"] == "ok"
        assert rj["musicbox"] == "ok"
        assert rj["daily"] == "ok"
        assert rj["netease"]["logged_in"] is True
        # 单源化后这些字段不应再出现
        for gone in ("musicdl", "lxmusic", "llm"):
            assert gone not in rj


def test_search_track_late_wait_catches_slow_source(monkeypatch):
    """首屏预算内没回来 → 进入兜底预算，一旦返回立即合并（不丢在线结果）。"""
    monkeypatch.setitem(CONF, "netease_wait_s", 0.05)   # 阶段一极短，必然超时
    monkeypatch.setitem(CONF, "late_page_wait_s", 2.0)  # 阶段二足够长
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok",
                                         "data": {"list": [{"guid": "local:1", "title": "本地", "artist": "A"}],
                                                  "total": 1}})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        import time as _t
        if request.url.path == "/api/v1/search":
            _t.sleep(0.3)  # 慢于阶段一预算，快于阶段二预算
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "slow1", "song_name": "晴天", "artist": "周杰伦",
                 "album_name": "叶惠美", "duration": 269, "quality": "SQ"},
            ]})
        if request.url.path == "/api/v1/songs/detail":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770")

    with TestClient(app) as client:
        items = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20").json()["data"]["list"]

    assert [it["guid"] for it in items] == ["local:1", "online:netease:slow1"]


def test_search_cache_ttl_default_seven_days():
    """默认搜索缓存有效期为 7 天 (604800 秒)。"""
    assert CONF["search_cache_ttl"] == 604800.0


def test_search_track_within_budget_keeps_order(monkeypatch):
    """首屏预算内返回：本地在前、在线在后，且与本地重复的 (title, artist) 被去重。"""
    monkeypatch.setitem(CONF, "netease_wait_s", 1.0)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": 0, "msg": "ok",
            "data": {"list": [{"guid": "local:101", "title": "晴天", "artist": "周杰伦"}], "total": 1},
        })

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            return httpx.Response(200, json={"ok": True, "data": [
                # 与本地重复 -> 应被去重
                {"song_id": "mb_same", "song_name": "晴天", "artist": "周杰伦", "album_name": "叶惠美"},
                # 不同歌手 -> 保留
                {"song_id": "mb_unique", "song_name": "晴天", "artist": "翻唱歌手", "album_name": "翻唱合辑"},
                # 音源内部重复 -> deduplicate_online_items 去重
                {"song_id": "mb_dup", "song_name": "晴天", "artist": "翻唱歌手", "album_name": "翻唱合辑"},
            ]})
        if request.url.path == "/api/v1/songs/detail":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770")

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]

    assert items[0]["guid"] == "local:101", "本地结果必须排在最前"
    assert items[1]["guid"] == "online:netease:mb_unique"
    assert items[1]["artist"] == "翻唱歌手"
    assert len(items) == 2, "同 (title, artist) 无论来自本地还是音源内部都只保留一条"
    assert "online:netease:mb_same" not in {it["guid"] for it in items}



def test_general_passthrough():
    """非拦截路径透传（如静态资源或登录接口）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/api/v1/user/profile"
        assert request.headers.get("authorization") == "Bearer mytoken123"
        return httpx.Response(200, json={"code": 0, "data": {"username": "admin"}})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/user/profile",
            headers={"Authorization": "Bearer mytoken123"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "data": {"username": "admin"}}


def test_search_suggest_merge(monkeypatch):
    """search/suggest 的开/关行为（单源：网易云）。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": ["本地周杰伦"]})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            # 联想词链路应显式要求 enrich=false，不该再打详情接口
            assert request.url.params.get("limit") == "5"
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "s1", "song_name": "周杰伦 晴天", "artist": "周杰伦", "duration": 269},
                {"song_id": "s2", "song_name": "周杰伦 七里香", "artist": "周杰伦", "duration": 291},
            ]})
        if request.url.path == "/api/v1/songs/detail":
            pytest.fail("联想词不应请求 songs/detail")
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770")

    # 默认关闭：原样透传
    monkeypatch.setitem(CONF, "merge_suggest", False)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=周杰伦")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["本地周杰伦"]

    # 开启合并
    monkeypatch.setitem(CONF, "merge_suggest", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=周杰伦")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["本地周杰伦", "周杰伦 晴天", "周杰伦 七里香"]


def test_search_suggest_skipped_when_login_required(monkeypatch):
    """关闭降级且未登录时，联想词链路也不打音源。"""
    from proxy import netease_auth

    monkeypatch.setitem(CONF, "merge_suggest", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", False)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": ["本地周杰伦"]})

    searched = {"n": 0}

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            searched["n"] += 1
        return httpx.Response(200, json={"ok": True, "data": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770")

    async def _fetch(client, *, force=False):
        return netease_auth.LoginState(logged_in=False)

    monkeypatch.setattr(netease_auth, "fetch_state", _fetch)
    netease_auth.invalidate_state()

    with TestClient(app) as client:
        assert client.get("/music/api/v1/search/suggest?keyword=周杰伦").json()["data"] == ["本地周杰伦"]
    assert searched["n"] == 0


def test_online_hls_playlist():
    """在线曲 HLS 兜底 playlist 指向 stream。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "duration_s": 269, "ext": "mp3", "title": "晴天"},
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/hls/online:kuwo:228908/preset.m3u8")
        assert resp.status_code == 200
        body = resp.text
        assert "#EXTM3U" in body
        assert "guid=online%3Akuwo%3A228908" in body
        assert "#EXT-X-ENDLIST" in body


def test_online_transcode_ready():
    """在线曲 transcode 直接 success，避免前端卡会话。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/track/transcode",
            json={"guid": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        hb = client.post(
            "/music/api/v1/track/transcode/heartbeat",
            json={"guid": "online:kuwo:228908"},
        )
        assert hb.status_code == 200
        assert hb.json()["code"] == 0


def test_favorite_track_create_online_authorized(monkeypatch):
    """已登录用户红心在线曲目：上游透传 + 本地按用户 guid 落盘（多用户隔离）。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok",
                                             "data": {"guid": "user-a", "name": "admin"}})
        return httpx.Response(500, text="Unexpected upstream call")

    _wire_netease(
        monkeypatch,
        song_id="228908",
        info=_netease_song_info("228908", lossless=False),
        upstream_handler=upstream_handler,
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:netease:228908"},
            cookies={"music-token": "valid_token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

        user_fav = os.path.join(CONF["fav_dir"], "user-a.json")
        assert os.path.exists(user_fav)
        import json

        with open(user_fav, "r", encoding="utf-8") as f:
            saved = json.load(f)

    assert len(saved["items"]) == 1
    item = saved["items"][0]
    assert item["guid"] == "online:netease:228908"
    track = item["track"]
    assert track["title"] == "晴天"
    assert track["artists"][0]["name"] == "周杰伦"
    assert track["album"]["name"] == "叶惠美"
    assert track["isFavorite"] is True
    assert track["duration"] == 269000
    assert track["audioSpec"]["format"] == "mp3"


def test_favorite_track_create_online_unauthorized():
    """用例 2: create online 未登录: 上游 mock INVALID TOKEN → 原样透传返回 99999。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 99999, "msg": "INVALID TOKEN", "data": None}
        assert not os.path.exists(os.path.join(CONF["fav_dir"], "user-a.json"))


def test_favorite_track_create_local_passthrough():
    """用例 3: create 本地 guid: 透传上游 mock（验证不写本地）。"""
    upstream_called = {"called": False}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/create":
            upstream_called["called"] = True
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "local:1001"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}
        assert upstream_called["called"] is True
        assert len(os.listdir(CONF["fav_dir"])) == 0


def test_favorite_track_delete_online():
    """用例 4: delete online: 已登录 → code:0 且存储清空；幂等再删仍 code:0。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "晴天"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    user_fav = os.path.join(CONF["fav_dir"], "user-a.json")
    with TestClient(app) as client:
        # 先收藏
        resp1 = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp1.status_code == 200
        import json
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 1

        # 删除
        resp2 = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp2.status_code == 200
        assert resp2.json() == {"code": 0, "msg": "", "data": None}
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 0

        # 再次幂等删除
        resp3 = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp3.status_code == 200
        assert resp3.json() == {"code": 0, "msg": "", "data": None}
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 0


def test_favorite_track_list_merge(monkeypatch):
    """收藏列表合并：官方本地 1 条 + 在线红心 1 条 → total=2，在线条目形状完整。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={
                "code": 0, "msg": "",
                "data": {
                    "list": [{
                        "guid": "local:101", "title": "夜曲",
                        "artists": [{"name": "周杰伦", "guid": "local:artist:1"}],
                        "album": {"name": "十一月的萧邦", "guid": "local:album:1"},
                        "duration": 226000, "isFavorite": True,
                    }],
                    "total": 1, "sort": "favoriteAt,desc",
                },
            })
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-a"}})
        return httpx.Response(500)

    _wire_netease(
        monkeypatch,
        song_id="600908",
        info=_netease_song_info("600908", title="稻香", artist="周杰伦", album="魔杰座",
                                duration_ms=223000, lossless=True),
        upstream_handler=upstream_handler,
    )

    with TestClient(app) as client:
        client.post("/music/api/v1/favorite-track/create",
                    json={"trackGUID": "online:netease:600908"})

        resp = client.get("/music/api/v1/favorite-track/list?page=1&size=100")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        data = rj["data"]
        assert data["total"] == 2
        items = data["list"]
        assert len(items) == 2
        assert items[0]["guid"] == "local:101"
        assert items[0]["isFavorite"] is True

        online_item = items[1]
        assert online_item["guid"] == "online:netease:600908"
        assert online_item["title"] == "稻香"
        assert online_item["duration"] == 223000
        assert online_item["isFavorite"] is True
        assert online_item["isCue"] is False
        assert isinstance(online_item["genres"], list)
        assert isinstance(online_item["artists"], list)
        assert online_item["artists"][0]["name"] == "周杰伦"
        assert isinstance(online_item["album"], dict)
        assert online_item["album"]["name"] == "魔杰座"
        assert isinstance(online_item["audioSpec"], dict)
        assert online_item["audioSpec"]["format"] == "flac"
        assert "createdAt" in online_item and "updatedAt" in online_item


def test_favorite_track_create_unwritable_fav_dir_safe():
    """用例 6: create 时 fav 文件不可写（如路径指向不可写或非法路径）→ 不抛异常，返回仍 code:0（绝不能 500）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "晴天"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    # 将 fav_dir 设置为文件路径而非目录，使在其中创建文件失败
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    bad_file = os.path.join(CONF["cache_dir"], "not_a_dir")
    with open(bad_file, "w") as f:
        f.write("xxx")
    CONF["fav_dir"] = os.path.join(bad_file, "sub")

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == 0


def test_favorite_track_list_upstream_unauthorized():
    """用例 7: list 上游 INVALID TOKEN → 原样返回。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/favorite-track/list?page=1&size=100")
        assert resp.status_code == 200
        assert resp.json() == {"code": 99999, "msg": "INVALID TOKEN", "data": None}


def test_favorite_track_delete_local_passthrough():
    """用例 8: delete 本地 guid: 透传上游 mock。"""
    upstream_called = {"called": False}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/delete":
            upstream_called["called"] = True
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "local:1001"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}
        assert upstream_called["called"] is True


def test_user_isolation_create_and_list():
    """用户隔离用例 1: 用户 A create → 用户 B list 看不到 A 的在线条目；B 自己 create 后只看到自己的。"""
    current_user = {"guid": "user-a"}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": current_user["guid"]}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            gid = request.url.params.get("id")
            title = "A的歌曲" if "aaa" in str(gid) else "B的歌曲"
            return httpx.Response(200, json={"ok": True, "title": title})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # A 收藏曲目 A
        current_user["guid"] = "user-a"
        resp_a_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:aaa"},
        )
        assert resp_a_create.status_code == 200

        # B 查看列表，看不到 A 的收藏
        current_user["guid"] = "user-b"
        resp_b_list1 = client.get("/music/api/v1/favorite-track/list")
        assert resp_b_list1.status_code == 200
        assert resp_b_list1.json()["data"]["total"] == 0
        assert len(resp_b_list1.json()["data"]["list"]) == 0

        # B 收藏曲目 B
        resp_b_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:bbb"},
        )
        assert resp_b_create.status_code == 200

        # B 再次查看列表，只有 B 的歌曲
        resp_b_list2 = client.get("/music/api/v1/favorite-track/list")
        assert resp_b_list2.status_code == 200
        assert resp_b_list2.json()["data"]["total"] == 1
        assert resp_b_list2.json()["data"]["list"][0]["guid"] == "online:kuwo:bbb"

        # A 查看列表，只有 A 的歌曲
        current_user["guid"] = "user-a"
        resp_a_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_a_list.status_code == 200
        assert resp_a_list.json()["data"]["total"] == 1
        assert resp_a_list.json()["data"]["list"][0]["guid"] == "online:kuwo:aaa"


def test_user_isolation_delete():
    """用户隔离用例 2: 用户 A create、用户 B delete 同一 guid → A 的仍在（B 幂等 code:0），互不影响。"""
    current_user = {"guid": "user-a"}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": current_user["guid"]}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "公共在线曲目"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # A 收藏曲目
        current_user["guid"] = "user-a"
        client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:same_song"},
        )

        # B 尝试删除同一曲目（B 本身未收藏）
        current_user["guid"] = "user-b"
        resp_b_del = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:same_song"},
        )
        assert resp_b_del.status_code == 200
        assert resp_b_del.json()["code"] == 0

        # A 检查列表，收藏依然在
        current_user["guid"] = "user-a"
        resp_a_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_a_list.status_code == 200
        assert resp_a_list.json()["data"]["total"] == 1
        assert resp_a_list.json()["data"]["list"][0]["guid"] == "online:kuwo:same_song"


def test_user_me_missing_guid_fallback_shared():
    """用户隔离用例 3: user/me 响应缺 guid → 落 'shared' 桶不报错。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            # 返回没有 guid 字段或者 data 非 dict
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"name": "someone"}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "兜底歌曲"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # 创建收藏
        resp_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:fallback_track"},
        )
        assert resp_create.status_code == 200
        assert resp_create.json()["code"] == 0

        # 检查是否落入 shared.json
        shared_file = os.path.join(CONF["fav_dir"], "shared.json")
        assert os.path.exists(shared_file)
        import json
        with open(shared_file, "r", encoding="utf-8") as f:
            items = json.load(f)["items"]
        assert len(items) == 1
        assert items[0]["guid"] == "online:kuwo:fallback_track"

        # 查询 list
        resp_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_list.status_code == 200
        assert resp_list.json()["data"]["total"] == 1
        assert resp_list.json()["data"]["list"][0]["guid"] == "online:kuwo:fallback_track"


def test_user_guid_sanitization():
    """测试 user_guid 特殊字符文件名过滤安全逻辑。"""
    from proxy.app import sanitize_user_guid
    assert sanitize_user_guid("user-123_ABC") == "user-123_ABC"
    assert sanitize_user_guid("../../etc/passwd") == "______etc_passwd"
    assert sanitize_user_guid("user:name*?<>|") == "user_name_____"
    assert sanitize_user_guid("   ") == "shared"
    assert sanitize_user_guid("") == "shared"
    assert sanitize_user_guid(None) == "shared"


def test_static_cover_online_coverid_redirect(monkeypatch):
    """GET /static/cover?coverId=online:netease:... → 302 跳转到网易云专辑图。"""
    cover_target = "http://p1.music.126.net/cover228908.jpg"
    _wire_netease(
        monkeypatch,
        song_id="228908",
        info=_netease_song_info("228908", cover=cover_target),
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/static/cover?coverId=online:netease:228908&size=120",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers.get("location") == cover_target


def test_static_cover_404_when_no_cover(monkeypatch):
    """没有封面时必须 404，绝不能把 JSON 当图片返回导致客户端裂图。"""
    _wire_netease(monkeypatch, song_id="228908",
                  info=_netease_song_info("228908", cover=""))

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/static/cover?coverId=online:netease:228908",
                          follow_redirects=False)
        assert resp.status_code == 404


def test_static_cover_local_coverid_passthrough():
    """测试 2: coverId 为非 online: 本地 guid → 透传上游（mock 上游 200 二进制），响应原样。"""
    fake_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR..."

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/api/v1/static/cover"
        assert request.url.params.get("coverId") == "local:track:999"
        assert request.url.params.get("size") == "120"
        return httpx.Response(
            200,
            content=fake_image_bytes,
            headers={"content-type": "image/png"},
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach musicdl")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/static/cover?coverId=local:track:999&size=120",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.content == fake_image_bytes
        assert resp.headers.get("content-type") == "image/png"


def test_favorite_track_list_official_items_populate_is_favorite_and_empty_handling():
    """测试 official_list 缺失 isFavorite 时补齐 True，且在官方列表为空或有条目时正确合并和计算 total。"""
    official_data_holder = {"list": []}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "",
                    "data": {
                        "list": official_data_holder["list"],
                        "total": len(official_data_holder["list"]),
                    },
                },
            )
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-fav-test"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "netease:12345",
                    "source": "netease",
                    "title": "测试网易曲目",
                    "artist": "歌手A",
                    "album": "专辑A",
                    "duration_s": 180,
                    "ext": "mp3",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # Case 1: 官方列表为空，无在线收藏 -> total=0, list=[]
        resp = client.get("/music/api/v1/favorite-track/list")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        assert rj["data"]["total"] == 0
        assert rj["data"]["list"] == []

        # 添加一条在线收藏
        client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:netease:12345"},
        )

        # Case 2: 官方列表为空，存在 1 条在线收藏 -> total=1
        resp2 = client.get("/music/api/v1/favorite-track/list")
        assert resp2.status_code == 200
        rj2 = resp2.json()
        assert rj2["data"]["total"] == 1
        assert len(rj2["data"]["list"]) == 1
        assert rj2["data"]["list"][0]["guid"] == "online:netease:12345"
        assert rj2["data"]["list"][0]["isFavorite"] is True

        # Case 3: 官方列表中包含未带 isFavorite 字段（或 isFavorite 为 False）的条目
        official_data_holder["list"] = [
            {"guid": "local:201", "title": "本地曲目1"},
            {"guid": "local:202", "title": "本地曲目2", "isFavorite": False},
        ]
        resp3 = client.get("/music/api/v1/favorite-track/list")
        assert resp3.status_code == 200
        rj3 = resp3.json()
        # 官方 2 条 + 在线 1 条 = 3 条
        assert rj3["data"]["total"] == 3
        items = rj3["data"]["list"]
        assert len(items) == 3
        assert items[0]["guid"] == "local:201"
        assert items[0]["isFavorite"] is True
        assert items[1]["guid"] == "local:202"
        assert items[1]["isFavorite"] is True
        assert items[2]["guid"] == "online:netease:12345"
        assert items[2]["isFavorite"] is True


def test_is_playable_online_track_defense():
    from proxy.app import is_playable_online_track

    # 1. 试听标题过滤
    assert not is_playable_online_track({"id": "netease:1", "title": "夜曲 (试听版)"})
    assert not is_playable_online_track({"id": "netease:2", "title": "晴天（试听）"})
    assert not is_playable_online_track({"id": "kuwo:3", "title": "花海 - 试听片段"})

    # 2. 试听标记/不可播标记
    assert not is_playable_online_track({"id": "netease:4", "title": "稻香", "is_trial": True})
    assert not is_playable_online_track({"id": "netease:5", "title": "稻香", "freeTrialInfo": {"start": 0}})
    assert not is_playable_online_track({"id": "lx:kg:6", "title": "稻香", "is_free_part": 1})
    assert not is_playable_online_track({"id": "lx:kg:7", "title": "稻香", "fail_process": 4})
    assert not is_playable_online_track({"id": "lx:kg:8", "title": "稻香", "pay_type": 1})
    assert not is_playable_online_track({"id": "lx:kg:9", "title": "稻香", "playable": False})
    assert not is_playable_online_track({"id": "lx:kg:10", "title": "稻香", "has_stream": False})

    # 3. 直链无流/404过滤
    assert not is_playable_online_track({"id": "kuwo:11", "title": "七里香", "download_url": ""})
    assert not is_playable_online_track({"id": "kuwo:12", "title": "七里香", "download_url": "http://err.com/404/error.html"})
    assert not is_playable_online_track({"id": "kuwo:13", "title": "七里香", "download_url": "ftp://bad.com/1.mp3"})

    # 4. 正常有效可播歌曲
    assert is_playable_online_track({"id": "netease:100", "title": "晴天", "artist": "周杰伦"})
    assert is_playable_online_track({"id": "kuwo:101", "title": "晴天", "artist": "周杰伦", "download_url": "http://cdn.com/101.mp3"})


def test_merge_online_tracks_filters_unplayable_defense():
    from proxy.app import merge_online_tracks

    upstream_json = {"code": 0, "msg": "OK", "data": {"list": [], "total": 0}}
    online_data = [
        {"id": "netease:1", "title": "枫 (试听)", "artist": "周杰伦", "duration_s": 200},
        {"id": "kuwo:2", "title": "搁浅", "artist": "周杰伦", "download_url": "", "duration_s": 200},
        {"id": "kuwo:3", "title": "退后", "artist": "周杰伦", "download_url": "http://cdn.com/3.mp3", "duration_s": 250},
    ]
    merged = merge_online_tracks(upstream_json, online_data)
    items = merged["data"]["list"]
    # 只有有效且可播的退后 (id=3) 会被合并
    assert len(items) == 1
    assert items[0]["guid"] == "online:kuwo:3"
    assert items[0]["title"] == "退后"






# ---------------------------------------------------------------------------
# v2.7：tee 背压缓冲 + 客户端断开检测
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_tee_backpressure_and_client_gone_completion(monkeypatch):
    """tee 内存安全三件事：

    1. **背压**：队列上限 64 块（约 4MB）。消费端只取 1 块且不断开时，
       下载端必须被卡住（不能把整首歌都堆进内存）；
    2. **断开检测**：消费端断开后，下载端转「只写盘」模式继续下完；
    3. **缓存完整**：断开场景下文件最终仍完整落盘曲库（下次播放直接秒开）。
    """
    import asyncio

    from proxy.app import stream_tee_response

    chunk_size = 65536
    total_chunks = 200                      # ~12.5MB，远超 4MB 缓冲上限
    produced = {"n": 0}

    async def _handler(request: httpx.Request) -> httpx.Response:
        async def _gen():
            for _ in range(total_chunks):
                produced["n"] += 1
                yield b"A" * chunk_size

        return httpx.Response(
            200,
            content=_gen(),
            headers={"Content-Type": "audio/mpeg",
                     "Content-Length": str(total_chunks * chunk_size)},
        )

    cdn = httpx.AsyncClient(transport=httpx.MockTransport(_handler),
                            base_url="http://cdn.test")
    req = cdn.build_request("GET", "http://cdn.test/song.mp3")
    resp = await cdn.send(req, stream=True)

    response = stream_tee_response(
        resp,
        guid="online:netease:999001",
        range_header=None,
        client_to_close=cdn,
        resolved_ext="mp3",
        pre_info={"id": "netease:999001", "source": "netease", "title": "回声",
                  "artist": "测试歌手", "album": "", "duration_s": 200,
                  "ext": "mp3", "file_size": 0, "cover_url": "", "lyric": ""},
    )

    body = response.body_iterator
    first = await body.__anext__()
    assert first

    # 1) 背压：消费端停在 1 块，下载端最多再拉满 64 块缓冲就必须暂停
    await asyncio.sleep(0.3)
    assert produced["n"] < total_chunks, (
        f"无背压：已拉 {produced['n']} 块（上限应为 ~65 块）"
    )

    # 2) 客户端断开 → 下载端转入只写盘模式
    await body.aclose()

    # 3) 文件最终完整落盘（含 content-length 校验通过才会 rename 进曲库）
    cache_file = os.path.join(CONF["library_dir"], "测试歌手 - 回声.mp3")
    for _ in range(200):
        if os.path.exists(cache_file):
            break
        await asyncio.sleep(0.05)
    assert os.path.exists(cache_file), "断开后文件仍应完整落盘曲库"
    assert os.path.getsize(cache_file) == total_chunks * chunk_size
