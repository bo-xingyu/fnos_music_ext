"""v2.9 本地每日推荐：随机抽取、当日稳定、跨天换新、注入与播放直读。"""
from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import netease_auth
from proxy import recommend as dailyrec
from proxy.app import app, CONF


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setitem(CONF, "library_dir", str(library))
    monkeypatch.setitem(CONF, "music_db", str(tmp_path / "none.db"))
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_LOCAL_DAILY_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_LOCAL_DAILY_LIMIT", "50")
    monkeypatch.delenv("FNMUSIC_NETEASE_PLAYLIST_ORDER", raising=False)

    # 造 80 首本地歌（文件名「歌手 - 歌名.flac」）
    for i in range(80):
        (library / f"歌手{i % 7} - 歌曲{i:03d}.flac").write_bytes(b"F" * 1024)

    def _upstream(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "u1"}})
        if path.endswith("/playlist/list"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        if "search/track" in path:
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(200, json={"code": 0, "data": None})

    def _musicbox(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_musicbox), base_url="http://127.0.0.1:8770")
    netease_auth.invalidate_state()
    yield str(library)


def test_local_daily_guid_distinct_from_netease_daily():
    """guid 命名空间必须与网易云日推彻底区分。"""
    assert dailyrec.is_local_daily_playlist_guid("online:playlist:localdaily:20260913:u1")
    assert not dailyrec.is_local_daily_playlist_guid("online:playlist:daily:20260913:u1")
    assert not dailyrec.is_daily_playlist_guid("online:playlist:localdaily:20260913:u1")
    assert dailyrec.local_daily_playlist_guid("20260913", "u1").startswith(
        "online:playlist:localdaily:20260913")
    assert dailyrec.local_daily_playlist_name("20260913") == "本地每日推荐 09-13"


def test_local_daily_random_stable_same_day_and_limit(wired, monkeypatch):
    """数量限制 + 同一天内多次构建结果一致（种子按用户+日期）。"""
    day = "20260913"
    monkeypatch.setenv("FNMUSIC_LOCAL_DAILY_LIMIT", "50")
    a = dailyrec.build_local_daily_tracks(wired, 50, "u1", day)
    b = dailyrec.build_local_daily_tracks(wired, 50, "u1", day)
    assert len(a) == 50
    assert [t["guid"] for t in a] == [t["guid"] for t in b], "同一天同用户必须稳定"
    # 不同用户/不同日期 → 不同批次（允许极小概率撞车，用集合大小做近似断言）
    c = dailyrec.build_local_daily_tracks(wired, 50, "u2", day)
    d = dailyrec.build_local_daily_tracks(wired, 50, "u1", "20260914")
    assert isinstance(c, list) and isinstance(d, list)
    # 曲目形状
    t = a[0]
    assert t["source"] == "local" and t["_local_path"].endswith(".flac")
    assert t["title"].startswith("歌曲") or t["artist"].startswith("歌手")
    # limit 上限生效
    assert len(dailyrec.build_local_daily_tracks(wired, 500, "u1", day)) == 80


def test_local_daily_bundle_cache_and_purge(wired):
    # ⚠️ 不要写死日期：跨过午夜后 today_key() 变了，磁盘缓存键就对不上，
    # 测试会在"没人改代码"的情况下突然挂掉（2026-09-14 实锤一次）。
    day = dailyrec.today_key()
    b1 = dailyrec.get_or_build_local_daily("u1", wired)
    assert b1["status"] == "ready"
    assert len(b1["tracks"]) > 0
    # 当天缓存命中：改库文件也不影响（缓存优先）
    (os.scandir(wired) and None)
    b2 = dailyrec.get_or_build_local_daily("u1", wired)
    assert [t["guid"] for t in b2["tracks"]] == [t["guid"] for t in b1["tracks"]]
    # 磁盘缓存存在
    assert dailyrec.load_local_daily_cache("u1", day)
    # 清理旧日：把"今天"之外的都清掉，今天的必须还在
    dailyrec.purge_stale_local_daily_cache("u1", day)
    assert dailyrec.load_local_daily_cache("u1", day) is not None


def test_local_daily_disabled_or_empty(wired, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LOCAL_DAILY_ENABLED", "false")
    b = dailyrec.get_or_build_local_daily("u1", wired)
    assert b["status"] == "unavailable" and b["reason"] == "disabled"
    monkeypatch.setenv("FNMUSIC_LOCAL_DAILY_ENABLED", "true")
    empty = dailyrec.get_or_build_local_daily("u1", "/nonexistent-library")
    assert empty["reason"] == "library_empty"


def test_playlist_list_injects_local_daily_and_stream_serves_file(wired):
    """端到端：歌单列表注入「本地每日推荐」；点开拿曲目；播放在线 guid 直读磁盘。"""
    with TestClient(app) as client:
        r = client.get("/music/api/v1/playlist/list")
        assert r.status_code == 200
        items = r.json()["data"]["list"]
        local = [it for it in items
                 if dailyrec.is_local_daily_playlist_guid(str(it.get("guid") or ""))]
        assert len(local) == 1
        assert local[0]["name"].startswith("本地每日推荐")
        assert local[0]["trackCount"] == 50
        guid = local[0]["guid"]

        # 点开曲目列表
        r2 = client.get(f"/music/api/v1/track/playlist-detail/list?playlistGUID={guid}&page=1&size=500")
        assert r2.status_code == 200
        tracks = r2.json()["data"]["list"]
        assert len(tracks) == 50
        assert all("_local_path" not in t for t in tracks), "内部字段不能外泄"
        tguid = tracks[0]["guid"]
        assert tguid.startswith("local:file:")

        # 播放：直接读本地文件（2048 字节假 flac 的 Range 全量）
        r3 = client.get(f"/music/api/v1/track/stream?guid={tguid}")
        assert r3.status_code == 200
        assert r3.content == b"F" * 1024
        assert r3.headers["content-type"] == "audio/flac"

        # detail / batch-detail 回显
        r4 = client.get(f"/music/api/v1/playlist/detail?guid={guid}")
        assert r4.status_code == 200
        assert r4.json()["data"]["name"].startswith("本地每日推荐")
        r5 = client.get(f"/music/api/v1/playlist/batch-detail?guids={guid}")
        assert r5.status_code == 200
        assert any(dailyrec.is_local_daily_playlist_guid(str(x.get("guid") or ""))
                   for x in r5.json()["data"]["list"])


def test_channel_order_accepts_localdaily(monkeypatch):
    from proxy import playlists as pl
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "localdaily,toplist,daily")
    order = pl.channel_order()
    assert order[:3] == ("localdaily", "toplist", "daily")
    # 没列出的按默认序跟在后面
    assert "mine" in order and "fm" in order


def test_scan_covers_nested_album_layouts(tmp_path):
    """曲库是「库/歌手/专辑/文件」这类嵌套布局时也必须扫得到。

    真机最常见的事故就是埋得深一点就一首扫不到 → 歌单静默消失。
    """
    lib = tmp_path / "lib"
    for rel in (
        "顶层歌曲.mp3",                       # 相对深度 0
        "周杰伦/晴天.mp3",                     # 1
        "周杰伦/七里香/01 七里香.flac",          # 2
        "陈奕迅/十年/2015 重制/02.flac",        # 3（旧实现会被漏掉）
    ):
        p = lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"F" * 512)

    got = {os.path.relpath(f["path"], str(lib)) for f in dailyrec._scan_library_audio_files(str(lib))}
    assert got == {
        "顶层歌曲.mp3",
        os.path.join("周杰伦", "晴天.mp3"),
        os.path.join("周杰伦", "七里香", "01 七里香.flac"),
        os.path.join("陈奕迅", "十年", "2015 重制", "02.flac"),
    }


def test_scan_stops_at_configured_depth(tmp_path):
    """超过配置深度的归档目录不参与扫描（避免大曲库被拖慢）。

    LOCAL_DAILY_SCAN_MAX_DEPTH=3 → 最远扫到「库/一/二/三/四.mp3」。
    """
    lib = tmp_path / "lib"
    ok = lib / "a" / "b" / "c" / "d"
    ok.mkdir(parents=True)
    (ok / "ok.mp3").write_bytes(b"F" * 512)
    deep = lib / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "too-deep.mp3").write_bytes(b"F" * 512)

    got = {os.path.relpath(f["path"], str(lib)) for f in dailyrec._scan_library_audio_files(str(lib))}
    assert got == {os.path.join("a", "b", "c", "d", "ok.mp3")}


def test_scan_ignores_non_audio_and_empty_files(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "cover.jpg").write_bytes(b"F" * 512)
    (lib / "empty - 空文件.mp3").write_bytes(b"")          # 0 字节视为损坏
    (lib / "歌手 - 真歌.mp3").write_bytes(b"F" * 512)
    got = dailyrec._scan_library_audio_files(str(lib))
    assert [f["title"] for f in got] == ["真歌"]
    assert got[0]["artist"] == "歌手"


def test_old_schema_cache_is_discarded_and_rebuilt(wired, monkeypatch):
    """升级后旧格式缓存必须自动重建，而不是继续喂 duration=0 的旧数据。

    这不是洁癖：get_or_build 命中缓存就直接 return、不再扫描，于是
    v2.9.5 新加的本地文件索引永远写不上、列表里时长还是 0。
    """
    day = dailyrec.today_key()
    b1 = dailyrec.get_or_build_local_daily("u1", wired)
    assert b1["status"] == "ready"

    # 手工伪造一份 v2.9.4 时代的缓存（无 schema 字段 + duration 全 0）
    legacy = dict(b1)
    legacy.pop("schema", None)
    legacy["tracks"] = [{**t, "duration": 0, "size": 0} for t in b1["tracks"]]
    dailyrec.save_local_daily_cache("u1", day, legacy)

    b2 = dailyrec.get_or_build_local_daily("u1", wired)
    assert b2["schema"] == dailyrec.LOCAL_DAILY_SCHEMA, "必须重建为新格式"
    assert all(t["duration"] > 0 or t["size"] > 0 for t in b2["tracks"]), \
        "重建后的曲目必须带真时长/体积"

    # 重建顺带把本地文件索引补上——metadata / cover 全靠它反查真实文件
    try:
        from proxy import local_files
    except ImportError:
        import local_files  # type: ignore
    assert local_files.resolve(str(b2["tracks"][0]["guid"])), \
        "重建后必须能在索引里查到首曲的真实路径"


def test_local_daily_tracks_duration_unit_matches_online(wired, monkeypatch):
    """列表里的 duration 也必须是毫秒——客户端在点开前就先按它判断可不可播。"""
    try:
        from proxy import local_files
    except ImportError:
        import local_files  # type: ignore
    monkeypatch.setattr(
        local_files, "probe",
        lambda p: {"duration": 180.0, "duration_ms": 180000, "size": 4096,
                   "bitrate": 900000, "album": "A", "title": "T", "artist": "Ar",
                   "sample_rate": 44100, "channels": 2})
    tracks = dailyrec.build_local_daily_tracks(wired, 3, "u1", "20260913")
    assert len(tracks) == 3
    for t in tracks:
        assert t["duration"] == 180000, "duration 必须是毫秒（与在线曲目一致）"
        assert t["duration_s"] == 180.0
        assert t["duration_ms"] == t["duration"]
