"""Tests for fnmusic-ext netease (musicbox) integration, fast search, and pagination."""
import asyncio
import logging
import os
import time
import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import (
    app,
    CONF,
    _SEARCH_CACHE,
    _set_search_cache,
    _clean_search_cache,
    fetch_netease_search,
    _online_info,
    resolve_netease_url,
    resolve_online_lyric,
    find_cache_file,
)


@pytest.fixture(autouse=True)
def setup_netease_env(tmp_path, monkeypatch):
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
    monkeypatch.setitem(CONF, "netease_wait_s", 2.5)
    monkeypatch.setitem(CONF, "netease_quality", "lossless")
    monkeypatch.setitem(CONF, "search_cache_ttl", 300.0)
    monkeypatch.setitem(CONF, "late_page_wait_s", 5.0)


# =========================================================================
# 1. netease 搜索映射 (quality SQ→flac、LD→mp3; 字段映射正确)
# =========================================================================
@pytest.mark.anyio
async def test_fetch_netease_search_mapping():
    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            assert request.url.params.get("keyword") == "七里香"
            assert request.url.params.get("limit") == "10"
            assert request.url.params.get("type") == "song"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": "186016",
                            "song_name": "七里香",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 299,
                            "quality": "SQ 2.4M",
                        },
                        {
                            "song_id": "186017",
                            "song_name": "借口",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 258,
                            "quality": "LD 128k",
                        },
                        {
                            "song_id": "186018",
                            "song_name": "搁浅",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 200,
                            "quality": "HR 24bit",
                        },
                        {
                            "song_id": "186019",
                            "song_name": "园游会",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 240,
                            "quality": "无损",
                        },
                    ],
                },
            )
        if request.url.path == "/api/v1/songs/detail":
            assert request.url.params.get("ids") == "186016,186017,186018,186019"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": 186016,
                            "name": "七里香",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "album_pic_url": "https://img.test/qlx.jpg",
                            "duration_ms": 299000,
                            "has_sq": True,
                            "has_hr": False,
                        },
                        {
                            "song_id": 186017,
                            "name": "借口",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "album_pic_url": "https://img.test/jk.jpg",
                            "duration_ms": 258000,
                            "has_sq": False,
                            "has_hr": False,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    items = await fetch_netease_search(client, "七里香", 10)
    assert items is not None
    assert len(items) == 4

    # SQ -> flac, cover_url 补全
    assert items[0]["id"] == "netease:186016"
    assert items[0]["source"] == "netease"
    assert items[0]["title"] == "七里香"
    assert items[0]["artist"] == "周杰伦"
    assert items[0]["album"] == "七里香"
    assert items[0]["duration_s"] == 299.0
    assert items[0]["ext"] == "flac"
    assert items[0]["cover_url"] == "https://img.test/qlx.jpg"
    assert items[0]["lyric"] == ""

    # LD -> mp3, cover_url 补全
    assert items[1]["id"] == "netease:186017"
    assert items[1]["ext"] == "mp3"
    assert items[1]["cover_url"] == "https://img.test/jk.jpg"

    # HR -> flac
    assert items[2]["id"] == "netease:186018"
    assert items[2]["ext"] == "flac"
    assert items[2]["cover_url"] == ""

    # 无损 -> flac
    assert items[3]["id"] == "netease:186019"
    assert items[3]["ext"] == "flac"
    assert items[3]["cover_url"] == ""


@pytest.mark.anyio
async def test_fetch_netease_search_error_handling():
    def err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(err_handler), base_url="http://127.0.0.1:8770"
    )
    items = await fetch_netease_search(client, "fail", 10)
    assert items is None
    assert await fetch_netease_search(client, "", 10) is None


@pytest.mark.anyio
async def test_fetch_netease_search_detail_failure_fallback():
    """songs/detail 挂掉（500）时 cover_url 留空，主结果仍在。"""
    def search_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": "186016",
                            "song_name": "七里香",
                            "artist": "周杰伦",
                            "album_name": "七里香",
                            "duration": 299,
                            "quality": "LD 128k",
                        }
                    ],
                },
            )
        if request.url.path == "/api/v1/songs/detail":
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(search_handler), base_url="http://127.0.0.1:8770"
    )
    items = await fetch_netease_search(client, "七里香", 10)
    assert items is not None
    assert len(items) == 1
    assert items[0]["id"] == "netease:186016"
    assert items[0]["title"] == "七里香"
    assert items[0]["cover_url"] == ""
    assert items[0]["ext"] == "mp3"


# =========================================================================
# 2. netease info 映射 (ar 多歌手 join、sq null → mp3、dt 毫秒→秒)
# =========================================================================
@pytest.mark.anyio
async def test_online_info_netease_mapping():
    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "说好不哭",
                        "ar": [{"name": "周杰伦"}, {"name": "阿信"}],
                        "al": {"name": "说好不哭", "picUrl": "http://img.test/shbk.jpg"},
                        "dt": 222000,
                        "sq": {"size": 25000000, "br": 999000},
                        "h": {"size": 9000000, "br": 320000},
                        "hr": None,
                    },
                },
            )
        if request.url.path == "/api/v1/song/186016/lyric":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "lyric": "[00:00.00]说好不哭\n[00:10.00]周杰伦",
                        "tlyric": "",
                    },
                },
            )
        if request.url.path == "/api/v1/song/186017/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "普通音质曲",
                        "ar": [{"name": "单歌手"}],
                        "al": {"name": "专辑名", "picUrl": "http://img.test/pt.jpg"},
                        "dt": 180000,
                        "sq": None,
                        "hr": None,
                        "h": {"size": 4500000, "br": 320000},
                    },
                },
            )
        return httpx.Response(404)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    req = httpx.Request("GET", "http://testserver")
    # 构造 Request 模拟
    from starlette.requests import Request as StarletteRequest
    scope = {"type": "http", "app": app}
    req_obj = StarletteRequest(scope)

    info_sq = await _online_info(req_obj, "online:netease:186016")
    assert info_sq is not None
    assert info_sq["id"] == "netease:186016"
    assert info_sq["source"] == "netease"
    assert info_sq["title"] == "说好不哭"
    assert info_sq["artist"] == "周杰伦 / 阿信"
    assert info_sq["album"] == "说好不哭"
    assert info_sq["cover_url"] == "http://img.test/shbk.jpg"
    assert info_sq["duration_s"] == 222.0
    assert info_sq["ext"] == "flac"
    assert info_sq["file_size"] == 25000000
    assert info_sq["lyric"] == "[00:00.00]说好不哭\n[00:10.00]周杰伦"

    # sq is None -> ext=mp3
    info_mp3 = await _online_info(req_obj, "online:netease:186017")
    assert info_mp3 is not None
    assert info_mp3["artist"] == "单歌手"
    assert info_mp3["duration_s"] == 180.0
    assert info_mp3["ext"] == "mp3"
    assert info_mp3["file_size"] == 4500000


# =========================================================================
# 3. resolve_netease_url 降级链 (lossless 404/code!=200 → exhigh 成功；全失败 None)
# =========================================================================
@pytest.mark.anyio
async def test_resolve_netease_url_downgrade():
    def downgrade_handler(request: httpx.Request) -> httpx.Response:
        quality = request.url.params.get("quality")
        if quality == "lossless":
            # lossless 返回 code 404 或无版权
            return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})
        if quality == "exhigh":
            return httpx.Response(
                200,
                json={"ok": True, "data": {"code": 200, "url": "http://audio.test/exhigh.mp3"}},
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(downgrade_handler), base_url="http://127.0.0.1:8770"
    )
    url = await resolve_netease_url(client, "186016")
    assert url == "http://audio.test/exhigh.mp3"


@pytest.mark.anyio
async def test_resolve_netease_url_all_fail():
    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fail_handler), base_url="http://127.0.0.1:8770"
    )
    url = await resolve_netease_url(client, "186016")
    assert url is None


@pytest.mark.anyio
async def test_resolve_netease_url_logs_exception_type(caplog):
    """回归真机上的诊断盲区：日志必须带异常类型。

    真机日志只有孤零零一行、冒号后面是空的：
        resolve_netease_url error for 94344 (quality=exhigh):
    原因是 httpx 的超时异常 ``ReadTimeout('')`` 的 ``str()`` 是【空字符串】
    （ConnectTimeout / ReadError / TimeoutException 同样如此），
    于是日志里既看不出是超时、连接失败还是解析错误，排查只能靠猜。
    而这里超时的成因是 /song/{id}/url 原先 exec CLI，冷启动实测 47s、
    稳态 1.4s，远超代理侧 10s 的 httpx 超时。
    """
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_handler), base_url="http://127.0.0.1:8770"
    )
    with caplog.at_level(logging.WARNING, logger="fnmusic_proxy"):
        url = await resolve_netease_url(client, "186016")

    assert url is None
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "resolve_netease_url error" in msg
    assert "ReadTimeout" in msg, (
        "httpx 超时异常的 str() 为空，必须把 type(e).__name__ 打出来，"
        "否则日志里冒号后面什么都没有，无法判断故障类型"
    )
    # 代理会依次试 primary(lossless) 与 exhigh，两级都超时都应留痕
    assert msg.count("ReadTimeout") == 2


# =========================================================================
# 4. stream_track netease 分支 (直链 206 透传/tee 落盘，缓存命中直接 206 不打上游)
# =========================================================================
def test_stream_track_netease_direct_stream_and_cache(tmp_path, monkeypatch):
    audio_content = b"FLAC_MAGIC_HEADER_TEST_AUDIO_CONTENT" * 40
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not hit upstream")

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "code": 200,
                        "url": "http://audio.test/song.flac",
                    },
                },
            )
        if request.url.path == "/api/v1/song/186016/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "晴天",
                        "ar": [{"name": "周杰伦"}],
                        "al": {"name": "叶惠美"},
                        "dt": 269000,
                        "sq": {"size": len(audio_content)},
                    },
                },
            )
        return httpx.Response(404)

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            return httpx.Response(
                200,
                content=audio_content,
                headers={
                    "Content-Type": "audio/flac",
                    "Content-Length": content_len,
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    # Mock httpx.AsyncClient for direct stream
    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:186016")
        assert resp.status_code == 200
        assert resp.content == audio_content
        assert resp.headers.get("content-type") == "audio/flac"

        # 检查曲库落盘
        saved_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
        assert os.path.exists(saved_file)
        with open(saved_file, "rb") as f:
            assert f.read() == audio_content

        # 缓存命中测试：第二次播放直接读本地文件，不请求上游
        resp2 = client.get(
            "/music/api/v1/track/stream?guid=online:netease:186016",
            headers={"Range": "bytes=0-9"},
        )
        assert resp2.status_code == 206
        assert resp2.content == audio_content[:10]


def test_stream_track_early_disconnect_background_tee(tmp_path, monkeypatch):
    """Bug1 回归测试：客户端提前断开（流式只读少量 chunk 后 break），后台 task 仍能完整落盘转正，且无 .part 残留。"""
    audio_content = b"FLAC_STREAM_TEST_CHUNK_PAYLOAD_PADDING_" * 128  # 5120 字节
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "code": 200,
                        "url": "http://audio.test/early_disconnect.flac",
                    },
                },
            )
        if request.url.path == "/api/v1/song/186016/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "晴天",
                        "ar": [{"name": "周杰伦"}],
                        "al": {"name": "叶惠美"},
                        "dt": 269000,
                        "sq": {"size": len(audio_content)},
                    },
                },
            )
        return httpx.Response(404)

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            return httpx.Response(
                200,
                content=audio_content,
                headers={
                    "Content-Type": "audio/flac",
                    "Content-Length": content_len,
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    saved_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")

    with TestClient(app) as client:
        # 客户端只消费少量 chunk 后提前 break 断开连接
        with client.stream("GET", "/music/api/v1/track/stream?guid=online:netease:186016") as r:
            assert r.status_code == 200
            for chunk in r.iter_bytes(chunk_size=128):
                break

        # 轮询等待后台 task 完成，最多 5s
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if os.path.exists(saved_file):
                break
            time.sleep(0.05)

        assert os.path.exists(saved_file)
        with open(saved_file, "rb") as f:
            assert f.read() == audio_content

        # 断言无 .part 残留
        parts = [f for f in os.listdir(CONF["library_dir"]) if f.endswith(".part")]
        assert len(parts) == 0


def test_stream_track_netease_mpeg_content_type_override_to_flac(tmp_path, monkeypatch):
    """Bug2 回归测试：网易 CDN 返回 content-type audio/mpeg 但 info 有 sq → 落盘为 .flac 且响应 content-type 为 audio/flac。"""
    audio_content = b"FLAC_AUDIO_CONTENT_WITH_MPEG_HEADER" * 40
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "code": 200,
                        "url": "http://audio.test/song.flac",
                    },
                },
            )
        if request.url.path == "/api/v1/song/186016/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "name": "晴天",
                        "ar": [{"name": "周杰伦"}],
                        "al": {"name": "叶惠美"},
                        "dt": 269000,
                        "sq": {"size": len(audio_content)},
                    },
                },
            )
        return httpx.Response(404)

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            # 网易 CDN 误返回 audio/mpeg
            return httpx.Response(
                200,
                content=audio_content,
                headers={
                    "Content-Type": "audio/mpeg",
                    "Content-Length": content_len,
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:186016")
        assert resp.status_code == 200
        assert resp.content == audio_content
        # 响应头 content-type 被修正为 audio/flac
        assert resp.headers.get("content-type") == "audio/flac"

        # 检查曲库落盘文件为 .flac，而不是 .mp3
        saved_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
        assert os.path.exists(saved_file)
        with open(saved_file, "rb") as f:
            assert f.read() == audio_content

        assert not os.path.exists(os.path.join(CONF["library_dir"], "周杰伦 - 晴天.mp3"))


def test_stream_track_netease_range_no_cache_no_coroutine_warning(tmp_path, monkeypatch):
    """带 Range (bytes=100-200) 时 should_cache 为 False，不应触发 'coroutine was never awaited' RuntimeWarning。"""
    audio_content = b"FLAC_MAGIC_HEADER_TEST_AUDIO_CONTENT" * 40
    range_slice = audio_content[100:201]

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/url":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "code": 200,
                        "url": "http://audio.test/song.flac",
                    },
                },
            )
        return httpx.Response(404)

    def direct_stream_handler(request: httpx.Request) -> httpx.Response:
        if "audio.test" in str(request.url):
            assert request.headers.get("range") == "bytes=100-200"
            return httpx.Response(
                206,
                content=range_slice,
                headers={
                    "Content-Type": "audio/flac",
                    "Content-Range": f"bytes 100-200/{len(audio_content)}",
                    "Content-Length": str(len(range_slice)),
                    "Accept-Ranges": "bytes",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    orig_async_client_init = httpx.AsyncClient.__init__

    def mock_client_init(self, *args, **kwargs):
        if "base_url" not in kwargs and not kwargs.get("transport"):
            kwargs["transport"] = httpx.MockTransport(direct_stream_handler)
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_client_init)

    import warnings
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        with TestClient(app) as client:
            resp = client.get(
                "/music/api/v1/track/stream?guid=online:netease:186016",
                headers={"Range": "bytes=100-200"},
            )
            assert resp.status_code == 206
            assert resp.content == range_slice

    # 确认没有 coroutine was never awaited 警告
    runtime_warnings = [
        w for w in record if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]
    assert len(runtime_warnings) == 0


def test_stream_track_netease_unavailable_404():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:netease:999999")
        assert resp.status_code == 404
        assert resp.json() == {
            "code": 404,
            "msg": "online source unavailable",
            "data": None,
        }


# =========================================================================
# 5. search_track 分页：page=1 快返回、total 抬升、page=2 合并缓存且不重复、TTL 过期
# =========================================================================
def test_search_track_pagination_and_cache_ttl(monkeypatch):
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "OK",
                    "data": {
                        "list": [
                            {
                                "guid": "local:101",
                                "title": "夜曲",
                                "artist": "周杰伦",
                                "album": "十一月的萧邦",
                            }
                        ],
                        "total": 1,
                    },
                },
            )
        return httpx.Response(
            200,
            json={"code": 0, "msg": "OK", "data": {"list": [], "total": 1}},
        )

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": f"mb_{i}",
                        "song_name": f"网易歌曲_{i}",
                        "artist": "歌手A",
                        "album_name": "专辑A",
                        "duration": 200,
                        "quality": "SQ",
                    }
                    for i in range(1, 13)  # 12 首
                ],
            },
        )

    monkeypatch.setitem(CONF, "online_limit", 10)
    monkeypatch.setitem(CONF, "search_cache_ttl", 2.0)  # 短 TTL 测试过期

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        # page=1 请求
        resp1 = client.get("/music/api/v1/search/track?q=周杰伦&page=1&size=50")
        assert resp1.status_code == 200
        data1 = resp1.json()["data"]
        # 单源后 total 抬升：本地 1 + 网易云在线 12 = 13
        assert data1["total"] == 13
        # page=1 包含本地 1 条 + 在线前 10 条
        list1 = data1["list"]
        assert len(list1) == 11
        assert list1[0]["guid"] == "local:101"
        assert list1[1]["guid"] == "online:netease:mb_1"
        assert list1[10]["guid"] == "online:netease:mb_10"

        # page=2 请求（命中缓存，返回剩余 2 条在线：mb_11, mb_12）
        resp2 = client.get("/music/api/v1/search/track?q=周杰伦&page=2&size=50")
        assert resp2.status_code == 200
        data2 = resp2.json()["data"]
        assert data2["total"] == 13
        list2 = data2["list"]
        assert len(list2) == 2
        assert list2[0]["guid"] == "online:netease:mb_11"
        assert list2[1]["guid"] == "online:netease:mb_12"

        # 断言 page=2 与 page=1 的在线条目无重叠
        guids1 = {it["guid"] for it in list1[1:]}
        guids2 = {it["guid"] for it in list2}
        assert guids1.isdisjoint(guids2)

        # page=3 请求 (断言 page=2 与 page=3 的在线条目互不重叠且 page=3 为空切片)
        resp3_page = client.get("/music/api/v1/search/track?q=周杰伦&page=3&size=50")
        assert resp3_page.status_code == 200
        data3 = resp3_page.json()["data"]
        assert data3["total"] == 13
        list3 = data3["list"]
        assert len(list3) == 0
        guids3 = {it["guid"] for it in list3}
        assert guids2.isdisjoint(guids3)

        # 等待 TTL 过期
        time.sleep(2.1)
        assert time.time() - _SEARCH_CACHE["周杰伦"]["ts"] >= 2.0
        # 再次搜索会重新生成缓存
        resp3 = client.get("/music/api/v1/search/track?q=周杰伦&page=1&size=50")
        assert resp3.status_code == 200
        assert resp3.json()["data"]["total"] == 13


# =========================================================================
# 6. 缓存淘汰 (>200 清理)
# =========================================================================
def test_search_cache_eviction():
    _SEARCH_CACHE.clear()
    now = time.time()

    # 填充 2005 条缓存
    for i in range(2005):
        _set_search_cache(f"kw_{i}", {"items": [f"item_{i}"], "ts": now + i, "task": None})

    # 断言超过 2000 时，清理掉最旧的一半
    assert len(_SEARCH_CACHE) <= 2000
    assert "kw_0" not in _SEARCH_CACHE
    assert "kw_500" not in _SEARCH_CACHE
    assert "kw_2004" in _SEARCH_CACHE


# =========================================================================
# 7. healthz 探测
# =========================================================================
def _hz_mocks(monkeypatch, *, upstream=200, musicbox=200, logged_in=True):
    """构造 healthz 需要的上游 + musicbox mock；返回调用计数。

    musicbox handler 同时应答 /healthz 与 /api/v1/auth/status，
    因为新版 healthz 会强制刷新一次登录态。
    """
    from proxy import netease_auth

    calls = {"upstream": 0, "musicbox_health": 0, "auth_status": 0}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        calls["upstream"] += 1
        if upstream != 200:
            return httpx.Response(upstream)
        return httpx.Response(200, json={"code": 0})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            calls["musicbox_health"] += 1
            if musicbox != 200:
                return httpx.Response(musicbox)
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/auth/status":
            calls["auth_status"] += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "logged_in": logged_in,
                        "nickname": "测试账号" if logged_in else "",
                        "user_id": "10086" if logged_in else "",
                    },
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    netease_auth.invalidate_state()
    return calls


def test_healthz_all_green(monkeypatch):
    """上游 + 音源都健康且已登录 → ok=True，daily 可用。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    monkeypatch.setitem(CONF, "daily_enabled", True)
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_TOKEN", raising=False)
    _hz_mocks(monkeypatch, logged_in=True)

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()

    assert rj["ok"] is True
    assert rj["upstream"] == "ok"
    assert rj["musicbox"] == "ok"
    assert rj["daily"] == "ok"
    assert rj["pushplus"] == "disabled"
    ne = rj["netease"]
    assert ne["logged_in"] is True
    assert ne["nickname"] == "测试账号"
    assert ne["free_only"] is False
    # v2.0 单源化后不再有这些字段
    for gone in ("musicdl", "lxmusic", "llm"):
        assert gone not in rj


def test_healthz_logged_out_with_free_only_degradation(monkeypatch):
    """未登录 + 允许降级 → 服务仍 ok，但 netease.free_only=True、daily=need_login。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    monkeypatch.setitem(CONF, "daily_enabled", True)
    _hz_mocks(monkeypatch, logged_in=False)

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()

    assert rj["ok"] is True, "降级模式下服务仍应视为可用"
    assert rj["musicbox"] == "ok"
    assert rj["netease"]["logged_in"] is False
    assert rj["netease"]["free_only"] is True
    assert rj["daily"] == "need_login"


def test_healthz_logged_out_without_degradation_is_unhealthy(monkeypatch):
    """未登录 + 关闭降级 → 在线音源不可用，ok=False。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", False)
    monkeypatch.setitem(CONF, "daily_enabled", True)
    _hz_mocks(monkeypatch, logged_in=False)

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()

    assert rj["ok"] is False
    assert rj["musicbox"] == "ok", "音源服务进程是活的，只是没登录"
    assert rj["netease"]["logged_in"] is False


def test_healthz_musicbox_down_is_unhealthy(monkeypatch):
    """单源时代音源服务挂掉 = 整体不健康（不再有其它音源兜底）。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    _hz_mocks(monkeypatch, musicbox=500)

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()

    assert rj["ok"] is False
    assert rj["upstream"] == "ok"
    assert rj["musicbox"] == "fail"
    assert rj["daily"] == "disabled"


def test_healthz_upstream_down_is_unhealthy(monkeypatch):
    """官方后端不可达时即便音源健康也判定不健康。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    _hz_mocks(monkeypatch, upstream=500)

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()

    assert rj["ok"] is False
    assert rj["upstream"] == "fail"
    assert rj["musicbox"] == "ok"


def test_healthz_netease_disabled(monkeypatch):
    """音源被关闭 → musicbox=disabled，且整体不健康（单源没有替补）。"""
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    calls = _hz_mocks(monkeypatch)

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()

    assert rj["ok"] is False
    assert rj["musicbox"] == "disabled"
    assert rj["daily"] == "disabled"
    # 关闭开关后不应再去探测音源服务与登录态
    assert calls["musicbox_health"] == 0
    assert calls["auth_status"] == 0


def test_healthz_reports_pushplus_enabled(monkeypatch):
    """配了 token 就在 healthz 里如实反映 pushplus=enabled（但不泄露 token）。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_TOKEN", "super-secret-token-value")
    _hz_mocks(monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/_ext/healthz")
        rj = resp.json()

    assert rj["pushplus"] == "enabled"
    assert "super-secret-token-value" not in resp.text


def test_healthz_daily_disabled_flag(monkeypatch):
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    monkeypatch.setitem(CONF, "daily_enabled", False)
    _hz_mocks(monkeypatch, logged_in=True)

    with TestClient(app) as client:
        rj = client.get("/_ext/healthz").json()

    assert rj["daily"] == "disabled"




# =========================================================================
# 8. TASK4: 歌词 + 封面 + 搜索量测试
# =========================================================================
@pytest.mark.anyio
async def test_resolve_online_lyric_netease(tmp_path, monkeypatch):
    """resolve_online_lyric netease：mock lyric 端点返回 LRC，断言写入缓存文件且二次调用不再请求远端；空歌词不写缓存返回 ""。"""
    cache_dir = str(tmp_path / "cache")
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "library_dir", str(tmp_path / "library"))

    lyric_calls = {"n": 0}

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/song/186016/lyric":
            lyric_calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": {
                        "lyric": "[00:01.00]七里香歌词第一行\n[00:05.00]第二行\n",
                        "tlyric": "[00:01.00]翻译行\n",
                    },
                },
            )
        if request.url.path == "/api/v1/song/empty_song/lyric":
            return httpx.Response(
                200,
                json={"ok": True, "data": {"lyric": "", "tlyric": ""}},
            )
        return httpx.Response(404)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    from starlette.requests import Request as StarletteRequest
    scope = {"type": "http", "app": app}
    req_obj = StarletteRequest(scope)

    # 首次调用：请求远端端点，拿到 lyric（保持原文不合并 tlyric），写入缓存
    text1 = await resolve_online_lyric(req_obj, "online:netease:186016")
    assert text1 == "[00:01.00]七里香歌词第一行\n[00:05.00]第二行"
    assert lyric_calls["n"] == 1

    # 二次调用：命中本地缓存，不再请求远端
    text2 = await resolve_online_lyric(req_obj, "online:netease:186016")
    assert text2 == "[00:01.00]七里香歌词第一行\n[00:05.00]第二行"
    assert lyric_calls["n"] == 1

    # 空歌词：不写缓存返回 ""
    text_empty = await resolve_online_lyric(req_obj, "online:netease:empty_song")
    assert text_empty == ""
    cache_file_empty = os.path.join(cache_dir, "online_netease_empty_song.lrc")
    assert not os.path.exists(cache_file_empty)


def test_search_volume_and_default_limits(monkeypatch):
    """搜索量：断言 fetch_netease_search 请求参数 limit=50（netease_search_limit），page=1 在线条目最多 30 条。"""
    captured_limits = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            limit = request.url.params.get("limit")
            captured_limits.append(limit)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "data": [
                        {
                            "song_id": f"mb_{i}",
                            "song_name": f"网易歌曲_{i}",
                            "artist": "歌手A",
                            "album_name": "专辑A",
                            "duration": 200,
                            "quality": "SQ",
                        }
                        for i in range(1, 41)  # 返回 40 首
                    ],
                },
            )
        if request.url.path == "/api/v1/songs/detail":
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": {"list": []}})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=测试&page=1&size=50")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # netease_search_limit = 50
        assert "50" in captured_limits
        # online_limit = 30，第一页最多 30 条在线
        assert len(data["list"]) == 30
        assert data["list"][0]["guid"] == "online:netease:mb_1"
        assert data["list"][29]["guid"] == "online:netease:mb_30"
        # total 为 40
        assert data["total"] == 40


# =========================================================================
# 9. 单源门控：未登录 / 关闭音源时的搜索与播放行为
# =========================================================================
def _gating_mocks(monkeypatch, *, logged_in=False, search_rows=None):
    """搜索链路门控测试用的 mock；返回调用计数。"""
    from proxy import netease_auth

    calls = {"search": 0, "auth_status": 0, "detail": 0}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 0, "msg": "OK", "data": {"list": [], "total": 0}}
        )

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/search":
            calls["search"] += 1
            return httpx.Response(
                200,
                json={"ok": True, "data": search_rows if search_rows is not None else []},
            )
        if path == "/api/v1/auth/status":
            calls["auth_status"] += 1
            return httpx.Response(
                200,
                json={"ok": True, "data": {"logged_in": logged_in, "nickname": "n"}},
            )
        if path == "/api/v1/songs/detail":
            calls["detail"] += 1
            return httpx.Response(200, json={"ok": True, "data": []})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )
    netease_auth.invalidate_state()
    return calls


_ROWS = [
    {
        "song_id": "228908",
        "song_name": "晴天",
        "artist": "周杰伦",
        "album_name": "叶惠美",
        "duration": 269,
        "quality": "SQ",
    }
]


def test_search_allowed_when_logged_out_and_degradation_on(monkeypatch):
    """默认降级模式：未登录也照常搜索，由服务端过滤成免费曲。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    calls = _gating_mocks(monkeypatch, logged_in=False, search_rows=_ROWS)

    with TestClient(app) as client:
        items = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20").json()["data"]["list"]

    assert calls["search"] == 1
    assert len(items) == 1
    assert items[0]["guid"] == "online:netease:228908"


def test_search_skipped_when_logged_out_and_degradation_off(monkeypatch):
    """关闭降级后未登录必须完全不打音源服务，返回纯本地结果。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", False)
    calls = _gating_mocks(monkeypatch, logged_in=False, search_rows=_ROWS)

    with TestClient(app) as client:
        body = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20").json()

    assert calls["search"] == 0, "未登录 + 关闭降级时绝不应请求网易云"
    assert body["data"]["list"] == []
    assert body["data"]["total"] == 0


def test_search_allowed_when_logged_in_and_degradation_off(monkeypatch):
    """关闭降级但已登录 → 正常放行，使用账号自身权益。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", False)
    calls = _gating_mocks(monkeypatch, logged_in=True, search_rows=_ROWS)

    with TestClient(app) as client:
        items = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20").json()["data"]["list"]

    assert calls["search"] == 1
    assert len(items) == 1


def test_search_skipped_when_netease_disabled(monkeypatch):
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    calls = _gating_mocks(monkeypatch, logged_in=True, search_rows=_ROWS)

    with TestClient(app) as client:
        body = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20").json()

    assert calls["search"] == 0
    assert body["data"]["list"] == []


def test_stream_rejects_legacy_non_netease_guid(monkeypatch):
    """旧版多音源遗留的 guid（online:migu:... / online:lx:kg:...）必须干净 404，
    不能因为找不到对应音源而抛异常或返回 500。"""
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    _gating_mocks(monkeypatch, logged_in=True)

    with TestClient(app) as client:
        for guid in ("online:migu:600929", "online:lx:kg:abcdef", "online:kuwo:kw_1"):
            resp = client.get(f"/music/api/v1/track/stream?guid={guid}")
            assert resp.status_code == 404, guid
            assert resp.json()["code"] == 404


def test_online_search_allowed_pure_function(monkeypatch):
    """_online_search_allowed 的三态门控。"""
    import asyncio

    from proxy import netease_auth
    from proxy.app import _online_search_allowed

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})),
        base_url="http://127.0.0.1:8770",
    )

    def run():
        return asyncio.run(_online_search_allowed(client))

    monkeypatch.setitem(CONF, "netease_enabled", False)
    assert run() is False

    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "free_only_on_logout", True)
    assert run() is True

    monkeypatch.setitem(CONF, "free_only_on_logout", False)
    netease_auth.invalidate_state()
    monkeypatch.setattr(
        netease_auth, "fetch_state",
        _fake_fetch(netease_auth.LoginState(logged_in=False)),
    )
    assert run() is False

    netease_auth.invalidate_state()
    monkeypatch.setattr(
        netease_auth, "fetch_state",
        _fake_fetch(netease_auth.LoginState(logged_in=True, nickname="x")),
    )
    assert run() is True


def _fake_fetch(state):
    async def _f(client, *, force=False):
        return state

    return _f


def test_wait_task_reports_budget_hit():
    """_wait_task：预算内完成返回 True，超时返回 False 且不取消任务。"""
    import asyncio

    from proxy.app import _wait_task

    async def scenario():
        async def fast():
            return "fast"

        async def slow():
            await asyncio.sleep(1.0)
            return "slow"

        t_fast = asyncio.create_task(fast())
        assert await _wait_task(t_fast, 1.0) is True
        assert t_fast.result() == "fast"

        t_slow = asyncio.create_task(slow())
        assert await _wait_task(t_slow, 0.05) is False
        assert not t_slow.cancelled(), "超时不应取消后台任务，翻页还要用它的结果"
        t_slow.cancel()

        # 预算为 0 时立即返回当前状态，不阻塞
        t_done = asyncio.create_task(fast())
        await t_done
        assert await _wait_task(t_done, 0) is True

    asyncio.run(scenario())


def test_collect_search_swallows_task_exception():
    """后台聚合任务失败时写入空结果而不是抛异常（源故障必须被隔离）。"""
    import asyncio

    from proxy.app import _collect_search

    async def scenario():
        async def boom():
            raise RuntimeError("音源炸了")

        entry: dict = {"items": None}
        await _collect_search(entry, asyncio.create_task(boom()))
        assert entry["items"] == []

        async def ok():
            return [{"id": "netease:1", "source": "netease", "title": "t", "artist": "a"}]

        entry2: dict = {"items": None}
        await _collect_search(entry2, asyncio.create_task(ok()))
        assert len(entry2["items"]) == 1

        entry3: dict = {"items": ["sentinel"]}
        await _collect_search(entry3, None)
        assert entry3["items"] == []

    asyncio.run(scenario())


def test_online_unavailable_response_shape():
    from proxy.app import _online_unavailable

    resp = _online_unavailable()
    assert resp.status_code == 404

    import json as _json

    assert _json.loads(resp.body)["code"] == 404
    assert _json.loads(resp.body)["msg"] == "online source unavailable"



# ===========================================================================
# /_ext/cache/invalidate —— 代理侧缓存失效
#
# 代理与管理页面是两个独立进程。页面里扫码登录成功只重置了页面进程自己的登录态，
# 代理这边仍会拿旧的 _SEARCH_CACHE（可能全是登录前的空结果，TTL 最长 60s）和旧的
# netease_auth 登录态（TTL 默认 300s）继续服务，表现为"明明登录了，搜索还是只有
# 本地歌曲"。这个端点让登录成功后立刻生效。
# ===========================================================================

from proxy import netease_auth
from proxy.app import _DAILY_TASKS
import proxy.app as proxy_app


@pytest.fixture(autouse=True)
def _isolate_auth_state(monkeypatch):
    """别把测试里的登录态写进真实缓存，也别触发真实网络探测。"""
    netease_auth.reset_for_test()
    calls = []
    monkeypatch.setattr(netease_auth, "invalidate_state", lambda: calls.append(1))
    yield calls
    netease_auth.reset_for_test()


def test_cache_invalidate_clears_search_cache(_isolate_auth_state):
    with TestClient(app) as client:
        _set_search_cache("周杰伦", [])
        assert "周杰伦" in _SEARCH_CACHE
        r = client.post("/_ext/cache/invalidate")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["cleared"]["search_entries"] == 1
    assert body["cleared"]["login_state"] is True
    assert _SEARCH_CACHE == {}, "登录/登出后必须丢掉旧搜索结果（可能全是空结果）"
    assert _isolate_auth_state == [1], "必须同时重置登录态探测缓存"


def test_cache_invalidate_purges_daily_cache_files(tmp_path, monkeypatch, _isolate_auth_state):
    """每日推荐歌单是按"未登录/旧账号"生成的，必须一并作废。"""
    root = tmp_path / "daily"
    u1 = root / "user-a"
    u1.mkdir(parents=True)
    (u1 / "2026-09-11.json").write_text("{}", encoding="utf-8")
    (root / "user-b").mkdir()
    (root / "user-b" / "2026-09-10.json").write_text("{}", encoding="utf-8")
    (root / "stray.txt").write_text("keep", encoding="utf-8")   # 非 json，不该动
    monkeypatch.setattr(proxy_app.dailyrec, "recommend_cache_dir", lambda: str(root))

    with TestClient(app) as client:
        body = client.post("/_ext/cache/invalidate").json()

    assert body["cleared"]["daily_cache_files"] == 2
    assert not (u1 / "2026-09-11.json").exists()
    assert not (root / "user-b" / "2026-09-10.json").exists()
    assert (root / "stray.txt").exists(), "只清 .json，别误删别的文件"


class _FakeTask:
    """替身 asyncio.Task：done()==False 表示还在跑，cancel() 应被调用。"""

    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True
        _CANCELLED.append(self)


_CANCELLED: list = []


def test_cache_invalidate_cancels_pending_daily_tasks(_isolate_auth_state):
    """未完成的每日推荐后台任务要取消，不能让旧账号的任务把结果又写回缓存。"""
    with TestClient(app) as client:
        _DAILY_TASKS.clear()
        _DAILY_TASKS["user-a"] = _FakeTask()
        _DAILY_TASKS["user-b"] = _FakeTask()
        body = client.post("/_ext/cache/invalidate").json()

    assert body["cleared"]["daily_tasks"] == 2
    assert _DAILY_TASKS == {}
    assert all(t.cancelled for t in _CANCELLED), "pending 任务必须被 cancel"


def test_cache_invalidate_idempotent_and_survives_missing_dir(
    monkeypatch, _isolate_auth_state
):
    """缓存目录不存在 / 连打两次都不能炸——登录回调里失败不该影响主流程。"""
    monkeypatch.setattr(
        proxy_app.dailyrec, "recommend_cache_dir",
        lambda: "/nonexistent/path/that/does/not/exist",
    )
    with TestClient(app) as client:
        first = client.post("/_ext/cache/invalidate")
        second = client.post("/_ext/cache/invalidate")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["ok"] is True
    assert second.json()["cleared"]["search_entries"] == 0


def test_search_cache_empty_result_not_served_after_login(monkeypatch, _isolate_auth_state):
    """端到端回归：登录前缓存的空结果，登录后不能再被拿来当答案。"""
    with TestClient(app) as client:
        _set_search_cache("林俊杰", [])   # 未登录时的空结果

        async def _fake_fetch(*a, **kw):
            return [{"name": "JJ-新结果", "guid": "online:1"}]

        monkeypatch.setattr(proxy_app, "fetch_netease_search", _fake_fetch)
        client.post("/_ext/cache/invalidate")
        assert "林俊杰" not in _SEARCH_CACHE


# ===========================================================================
# 在线元数据 / 封面缓存（2.1.7：点开一首歌约 4s 的主要构成）
#
# 单曲的标题/艺术家/专辑/时长/封面/歌词是静态数据，不会变。原先每次
# /static/metadata、/lyric/list、/static/cover 与播放路径都各自向 musicbox 发一次
# /api/v1/song/{id}/info + 一次 /api/v1/song/{id}/lyric（两个上游往返），
# 一首歌点开要重复好几轮。实测：真正的流式转发首字节仅 0.02~0.84s，
# 代理自身 resolve+info 也只 0.30s，剩下的时间就耗在这些重复往返上。
# ===========================================================================

import proxy.app as P            # noqa: E402  (静态封面用例要打桩 P.httpx)
from proxy.app import (          # noqa: E402
    _ONLINE_COVER_CACHE,
    _ONLINE_COVER_TTL,
    _ONLINE_INFO_CACHE,
    _ONLINE_INFO_TTL,
    _cache_put_prune,
    _online_cover_url,
    invalidate_online_info_cache,
)

PIC = "https://p1.music.126.net/CoverKey==/109951168064202445.jpg"


def _mb_request(songs, hit_log, *, lyric=True, fail=False):
    """构造 musicbox 侧 MockTransport：可按需禁用歌词、模拟失败、记录调用。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        hit_log.append(path)
        if fail:
            return httpx.Response(500, json={"ok": False})
        sid = path.split("/")[-2] if "/song/" in path else ""
        if path.endswith("/info"):
            return httpx.Response(200, json={"ok": True, "data": songs.get(sid, {})})
        if path.endswith("/lyric"):
            return httpx.Response(200, json={"ok": True, "data": {"lyric": "[00:01]测"}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


_SONG_RAW = {
    "id": 186016, "name": "说好不哭",
    "ar": [{"name": "周杰伦"}],
    "al": {"name": "说好不哭", "picUrl": PIC},
    "dt": 180000, "sq": None, "hr": None, "h": {"size": 4500000, "br": 320000},
}


def _req_for(monkeypatch, transport):
    app.state.musicbox_client = httpx.AsyncClient(transport=transport,
                                                 base_url="http://127.0.0.1:8770")
    from starlette.requests import Request as StarletteRequest

    return StarletteRequest({"type": "http", "app": app})


@pytest.mark.anyio
async def test_online_info_second_call_hits_cache(monkeypatch):
    hits = []
    req = _req_for(monkeypatch, _mb_request({"186016": _SONG_RAW}, hits))
    guid = "online:netease:186016"

    first = await _online_info(req, guid)
    n_after_first = len(hits)
    second = await _online_info(req, guid)
    assert second == first
    assert len(hits) == n_after_first, "第二次必须命中缓存，零上游往返"
    assert hits[:1] == ["/api/v1/song/186016/info"]


@pytest.mark.anyio
async def test_online_info_cache_expires_by_ttl(monkeypatch):
    hits = []
    req = _req_for(monkeypatch, _mb_request({"186016": _SONG_RAW}, hits))
    guid = "online:netease:186016"
    await _online_info(req, guid)
    before = len(hits)
    # 把写入时间推到 TTL 之前
    ts, val = _ONLINE_INFO_CACHE[guid]
    _ONLINE_INFO_CACHE[guid] = (ts - _ONLINE_INFO_TTL - 1, val)
    await _online_info(req, guid)
    assert len(hits) > before, "过期后必须重新回源"


@pytest.mark.anyio
async def test_online_info_failure_is_not_cached(monkeypatch):
    """失败不缓存：那多半是上游瞬时抖动，缓存住会把偶发失败固化成整段 TTL 无元数据。"""
    hits = []
    req = _req_for(monkeypatch, _mb_request({}, hits, fail=True))
    guid = "online:netease:186016"
    assert await _online_info(req, guid) is None
    assert guid not in _ONLINE_INFO_CACHE
    assert await _online_info(req, guid) is None
    assert len(hits) >= 2, "失败后应允许重试回源"


@pytest.mark.anyio
async def test_online_info_populates_cover_cache(monkeypatch):
    hits = []
    req = _req_for(monkeypatch, _mb_request({"186016": _SONG_RAW}, hits))
    info = await _online_info(req, "online:netease:186016")
    assert info["cover_url"] == PIC
    assert _ONLINE_COVER_CACHE["online:netease:186016"][1] == PIC, (
        "元数据里已有封面，应顺带填上封面缓存，让随后的 /static/cover 零往返命中"
    )


@pytest.mark.anyio
async def test_online_cover_url_never_fetches_lyric(monkeypatch):
    """取一张缩略图绝不该顺带去拉歌词——那是另一个上游往返，对封面毫无意义。"""
    hits = []
    req = _req_for(monkeypatch, _mb_request({"186016": _SONG_RAW}, hits))
    cover = await _online_cover_url(req, "online:netease:186016")
    assert cover == PIC
    assert hits == ["/api/v1/song/186016/info"], f"只应打一次 /info，实际 {hits}"

    hits.clear()
    assert await _online_cover_url(req, "online:netease:186016") == PIC
    assert hits == [], "第二次必须命中封面缓存"


@pytest.mark.anyio
async def test_online_cover_url_empty_is_not_cached(monkeypatch):
    hits = []
    req = _req_for(monkeypatch, _mb_request({"186016": {"id": 186016, "al": {}}}, hits))
    assert await _online_cover_url(req, "online:netease:186016") == ""
    assert "online:netease:186016" not in _ONLINE_COVER_CACHE
    await _online_cover_url(req, "online:netease:186016")
    assert len(hits) == 2, "空封面多为瞬时失败，不应缓存，应允许重试"


@pytest.mark.anyio
async def test_invalidate_online_info_cache_clears_both(monkeypatch):
    hits = []
    req = _req_for(monkeypatch, _mb_request({"186016": _SONG_RAW}, hits))
    await _online_info(req, "online:netease:186016")
    assert _ONLINE_INFO_CACHE and _ONLINE_COVER_CACHE
    n = invalidate_online_info_cache()
    assert n >= 2
    assert not _ONLINE_INFO_CACHE and not _ONLINE_COVER_CACHE


def test_cache_put_prune_bounds_size():
    store = {str(i): (float(i), {}) for i in range(10)}
    _cache_put_prune(store, 100)
    assert len(store) == 10, "未超上限不动"
    _cache_put_prune(store, 6)
    assert len(store) < 10, "超上限必须淘汰"
    assert "9" in store and "0" not in store, "应淘汰最旧的一半而不是最新的"


@pytest.mark.anyio
async def test_static_cover_streams_bytes_with_cache_header(monkeypatch):
    """封面由 NAS 代抓回传，而不是 302 让客户端直连网易云 CDN。

    302 依赖两件我们无法保证的事：客户端能直连 p1.music.126.net，且该 CDN 不校验
    Referer/Origin。任一不成立就表现为「列表里没有封面」。
    """
    hits = []
    app.state.musicbox_client = httpx.AsyncClient(
        transport=_mb_request({"186016": _SONG_RAW}, hits), base_url="http://127.0.0.1:8770")

    img = b"\xff\xd8\xff\xe0FAKEJPEGDATA"
    called = []

    async def fake_fetch(url):
        called.append(url)
        return img, "image/jpeg"

    monkeypatch.setattr(P, "_fetch_cover_bytes", fake_fetch)
    with TestClient(app) as c:
        r = c.get("/music/api/v1/static/cover",
                  params={"coverId": "online:netease:186016"}, follow_redirects=False)
    assert called == [PIC], "应按上游返回的 https picUrl 代抓"
    assert r.status_code == 200
    assert r.content == img
    assert r.headers["content-type"].startswith("image/")
    assert "max-age" in r.headers.get("cache-control", ""), "静态封面必须给客户端缓存指令"


@pytest.mark.anyio
async def test_static_cover_falls_back_to_redirect_when_fetch_fails(monkeypatch):
    """代抓失败时退回 302，至少保留原来那条能走通的路，别把封面彻底打死。"""
    hits = []
    app.state.musicbox_client = httpx.AsyncClient(
        transport=_mb_request({"186016": _SONG_RAW}, hits), base_url="http://127.0.0.1:8770")
    async def fail_fetch(url):
        return None

    monkeypatch.setattr(P, "_fetch_cover_bytes", fail_fetch)
    with TestClient(app) as c:
        r = c.get("/music/api/v1/static/cover",
                  params={"coverId": "online:netease:186016"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == PIC
