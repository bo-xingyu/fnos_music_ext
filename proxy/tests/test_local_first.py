"""v2.8 本地曲库优先：索引构建、匹配归一化、音质类约束、stream 集成。"""
from __future__ import annotations

import os
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import local_library as ll
from proxy import netease_auth
from proxy.app import app, CONF
from proxy import app as P


# ---------------------------------------------------------------------------
# 测试库：模拟飞牛 music.db（列名用常见命名，schema 容错扫描应能识别）
# ---------------------------------------------------------------------------


def _make_music_db(tmp_path, rows):
    db = str(tmp_path / "music.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE track (id INTEGER PRIMARY KEY, title TEXT, "
                "artist TEXT, path TEXT, duration INTEGER)")
    for i, (title, artist, path) in enumerate(rows):
        con.execute("INSERT INTO track (id, title, artist, path, duration) "
                    "VALUES (?, ?, ?, ?, 200)", (i + 1, title, artist, path))
    con.commit()
    con.close()
    return db


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    ll.reset_for_test()
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "library_dir", str(tmp_path / "library"))
    os.makedirs(CONF["library_dir"], exist_ok=True)
    yield
    ll.reset_for_test()


def _write_audio(name: str, size: int = 4096) -> str:
    path = os.path.join(CONF["library_dir"], name)
    with open(path, "wb") as f:
        f.write(b"A" * size)
    return path


# ---------------------------------------------------------------------------
# 索引与匹配
# ---------------------------------------------------------------------------


def test_build_index_and_match(tmp_path):
    flac = _write_audio("周杰伦 - 晴天.flac")
    db = _make_music_db(tmp_path, [
        ("晴天", "周杰伦", flac),
        ("晴天", "翻唱者", _write_audio("翻唱 - 晴天.mp3")),   # 同名不同人
        ("别的歌", "别人", _write_audio("别人 - 别的歌.mp3")),
        ("无路径歌", "谁", ""),                                # 脏数据
    ])
    hit = ll.find_local_match("晴天", "周杰伦", db)
    assert hit is not None
    assert hit["path"] == flac
    assert hit["klass"] == "lossless"

    # 同名不同艺术家：主艺术家不匹配 → 不命中（宁可不命中也不能放错歌）
    assert ll.find_local_match("晴天", "别人", db) is None
    # 艺术家一侧缺失视为匹配（排序里无损优先，翻唱是 mp3、原唱 flac → 取 flac）
    hit2 = ll.find_local_match("晴天", "", db)
    assert hit2 is not None and hit2["path"] == flac
    # 无同名 → 不命中
    assert ll.find_local_match("不存在的歌", "周杰伦", db) is None


def test_match_normalization_case_and_punctuation(tmp_path):
    flac = _write_audio("x - Song_Name.flac")
    db = _make_music_db(tmp_path, [("Song Name", "Some Artist", flac)])
    # 大小写、空格、下划线差异都应命中
    assert ll.find_local_match("song  name", "some artist", db) is not None
    assert ll.find_local_match("SONG-NAME", "SOME ARTIST / Guest", db) is not None


def test_index_cached_and_ttl(tmp_path, monkeypatch):
    flac = _write_audio("a - t.flac")
    db = _make_music_db(tmp_path, [("t", "a", flac)])
    assert ll.find_local_match("t", "a", db) is not None
    # TTL 内即使 db 被删也走缓存（索引已加载）
    os.remove(db)
    assert ll.find_local_match("t", "a", db) is not None
    # 过期后重建 → db 没了 → 不命中（且不抛异常）
    monkeypatch.setenv("FNMUSIC_LOCAL_INDEX_TTL", "0")
    _stale = str(tmp_path / "stale.db")
    ll._INDEX_CACHE[db] = (0.0, ll._INDEX_CACHE[db][1])
    assert ll.find_local_match("t", "a", db) is None


def test_db_missing_returns_none(tmp_path):
    assert ll.find_local_match("晴天", "周杰伦", str(tmp_path / "nope.db")) is None


def test_klass_helpers():
    assert ll.klass_of_ext("flac") == "lossless"
    assert ll.klass_of_ext(".wav") == "lossless"
    assert ll.klass_of_ext("mp3") == "lossy"
    assert ll.klass_of_level("jymaster") == "lossless"
    assert ll.klass_of_level("lossless") == "lossless"
    assert ll.klass_of_level("exhigh") == "lossy"
    assert ll.klass_of_level("standard") == "lossy"
    assert ll.serves_request({"ext": "flac"}, "lossless") is True
    assert ll.serves_request({"ext": "flac"}, "exhigh") is False   # 省流量不吃本地母带
    assert ll.serves_request({"ext": "mp3"}, "lossless") is False  # 320k 冒充不了无损
    assert ll.serves_request({"ext": "mp3"}, "exhigh") is True


# ---------------------------------------------------------------------------
# stream 集成：local-first 三种结局
# ---------------------------------------------------------------------------

_SONG_ID = "228908"


def _wire(monkeypatch, *, play_url=None, cdn=(200, b"NETEASE_AUDIO" * 300),
          info_title="晴天", info_artist="周杰伦"):
    def _musicbox(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/detail":
            return httpx.Response(200, json={"ok": True, "data": {"logged_in": True}})
        if path == f"/api/v1/song/{_SONG_ID}/info":
            return httpx.Response(200, json={"ok": True, "data": {
                "name": info_title,
                "ar": [{"name": info_artist}],
                "al": {"name": "叶惠美", "picUrl": ""},
                "dt": 269000,
                "sq": {"size": 28000000},
            }})
        if path == f"/api/v1/song/{_SONG_ID}/lyric":
            return httpx.Response(200, json={"ok": True, "data": {"lyric": "", "tlyric": ""}})
        if path == f"/api/v1/song/{_SONG_ID}/url":
            if not play_url:
                return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": None}})
            return httpx.Response(200, json={"ok": True,
                                             "data": {"code": 200, "url": play_url}})
        return httpx.Response(404, json={"ok": False})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_musicbox), base_url="http://127.0.0.1:8770")
    netease_auth.invalidate_state()

    if play_url:
        status, content = cdn
        orig_init = httpx.AsyncClient.__init__

        def _mock_init(self, *args, **kwargs):
            if "base_url" not in kwargs and not kwargs.get("transport"):
                kwargs["transport"] = httpx.MockTransport(
                    lambda r: httpx.Response(status, content=content,
                                             headers={"Content-Type": "audio/mpeg"}))
            orig_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _mock_init)


def test_stream_local_first_serves_local_when_class_matches(monkeypatch, tmp_path):
    """策略要 lossless、本地有无损同名曲 → 直接读本地，响应即本地文件字节。"""
    flac_bytes = b"LOCAL_FLAC_BYTES" * 300
    local_flac = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(local_flac, "wb") as f:
        f.write(flac_bytes)
    db = _make_music_db(tmp_path, [("晴天", "周杰伦", local_flac)])
    monkeypatch.setitem(CONF, "music_db", db)

    # 网易云侧故意全挂：无直链。本地命中 + 档位一致 → 仍应出声
    _wire(monkeypatch, play_url=None)
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid=online:netease:{_SONG_ID}",
                          headers={"Range": "bytes=0-"})
        assert resp.status_code == 206
        assert resp.content == flac_bytes
        assert resp.headers["content-type"] == "audio/flac"


def test_stream_local_first_skipped_when_policy_wants_lower(monkeypatch, tmp_path):
    """策略要 exhigh（省流量）、本地只有 Hi-Res/无损 → 不用本地，仍走网易云。"""
    local_flac = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(local_flac, "wb") as f:
        f.write(b"LOCAL_FLAC" * 300)
    db = _make_music_db(tmp_path, [("晴天", "周杰伦", local_flac)])
    monkeypatch.setitem(CONF, "music_db", db)
    monkeypatch.setenv("FNMUSIC_QUALITY_POLICY", "by_network")
    monkeypatch.setenv("FNMUSIC_QUALITY_WIFI", "exhigh")

    cdn_audio = b"NETEASE_320K" * 300
    _wire(monkeypatch, play_url="http://cdn.test/song.mp3", cdn=(200, cdn_audio))
    try:
        with TestClient(app) as client:
            resp = client.get(f"/music/api/v1/track/stream?guid=online:netease:{_SONG_ID}",
                              headers={"Range": "bytes=0-"})
            assert resp.status_code == 200
            assert resp.content.startswith(b"NETEASE_320K"), "省流量档必须走网易云 320k，不能喂本地无损"
    finally:
        from proxy import app as _pa
        _pa._URL_CACHE.clear()


def test_stream_local_first_disabled_by_env(monkeypatch, tmp_path):
    """FNMUSIC_LOCAL_FIRST=false 时完全不启用本地匹配。"""
    local_flac = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(local_flac, "wb") as f:
        f.write(b"LOCAL_FLAC" * 300)
    db = _make_music_db(tmp_path, [("晴天", "周杰伦", local_flac)])
    monkeypatch.setitem(CONF, "music_db", db)
    monkeypatch.setenv("FNMUSIC_LOCAL_FIRST", "false")

    _wire(monkeypatch, play_url=None)   # 网易云无直链
    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/track/stream?guid=online:netease:{_SONG_ID}")
        assert resp.status_code == 404, "关闭本地优先时无直链应如实 404"


# ---------------------------------------------------------------------------
# v2.8.1：music.db 自动定位
# ---------------------------------------------------------------------------


def test_resolve_music_db_prefers_existing_explicit(monkeypatch, tmp_path):
    db = _make_music_db(tmp_path, [("t", "a", "/x.flac")])
    monkeypatch.setitem(CONF, "music_db", db)
    P.reset_music_db_cache_for_test()
    assert P.resolve_music_db() == db


def test_resolve_music_db_falls_back_when_explicit_missing(monkeypatch, tmp_path):
    """显式配置的路径不存在时必须探测常见布局，而不是带着死路径静默失效。

    真机事故：默认路径 /usr/local/apps/... 在该机器上不存在（飞牛数据在
    /vol*/@appdata），诊断里「music.db 不存在」，本地曲库优先建立在空库上。
    """
    monkeypatch.setitem(CONF, "music_db", "/nonexistent/music.db")
    P.reset_music_db_cache_for_test()
    fake = str(tmp_path / "vol1_appdata.db")
    con = sqlite3.connect(fake)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    import glob as _glob
    orig_glob = _glob.glob
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(P.glob, "glob",
                   lambda pat, *a, **k: [fake] if "trim.music" in pat else orig_glob(pat))
        got = P.resolve_music_db()
    assert got == fake, "应探测到 /vol*/@appdata/trim.music/db/music.db 布局"


def test_resolve_music_db_uses_default_when_nothing_exists(monkeypatch):
    monkeypatch.setitem(CONF, "music_db", "/nonexistent/music.db")
    P.reset_music_db_cache_for_test()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(P.glob, "glob", lambda pat, *a, **k: [])
        assert P.resolve_music_db() == "/nonexistent/music.db"
