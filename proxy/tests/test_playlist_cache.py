"""v2.6 回归：歌单曲目缓存——秒开（先读缓存）、stale-while-revalidate、预热。

真机痛点：点开一个伪歌单要现场跑完整上游链路（trackIds → songs_detail →
songs_url 逐首过滤 → 补封面），好几秒。缓存后：
  * 命中且新鲜 → 零上游往返；
  * 命中但过 TTL → 先返回旧值（打开永远快），后台单飞刷新；
  * 打开歌单列表 → 自动安排一次后台预热（冷却期 = TTL）；
  * /_ext/playlists/warm → 立即全量预热（管理页按钮）。
"""
from __future__ import annotations

import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import netease_auth, playlists as pl
from proxy.app import app


GUID = "online:playlist:ne:424242"

CALLS: dict = {}


def _mb_tracks(song_name: str) -> dict:
    return {"ok": True, "data": [
        {"song_id": 2706544264, "song_name": song_name, "artist": "周杰伦",
         "album_name": "叶惠美", "duration": 269, "quality": "LOSSLESS FLAC",
         "mp3_url": "http://m/1.flac"}], "engine": "in-process"}


def musicbox_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    CALLS[path] = CALLS.get(path, 0) + 1
    if path == "/api/v1/auth/status":
        return httpx.Response(200, json={"ok": True,
                                         "data": {"logged_in": True, "nickname": "u"}})
    if path == "/healthz":
        return httpx.Response(200, json={"ok": True})
    if path == f"/api/v1/playlist/424242/tracks":
        return httpx.Response(200, json=_mb_tracks(os.environ.get("_MB_SONG", "晴天")))
    if path == "/api/v1/songs/detail":
        return httpx.Response(200, json={"ok": True, "data": [
            {"song_id": "2706544264", "album_pic_url": "http://pic/1.jpg",
             "has_sq": True, "album_name": "叶惠美", "artist": "周杰伦"}]})
    if path.startswith("/api/v1/playlists/"):
        return httpx.Response(200, json={"ok": True, "data": []})
    return httpx.Response(404, json={"ok": False})


def upstream_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/user/me"):
        return httpx.Response(200, json={"code": 0, "data": {"guid": "user-cache"}})
    if path.endswith("/playlist/list"):
        return httpx.Response(200, json={"code": 0, "data": {
            "list": [{"guid": "localpl", "name": "本地单", "coverId": "c",
                      "createdAt": 1700000000, "updatedAt": 1700000000}], "total": 1}})
    if "search/track" in path:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
    return httpx.Response(200, json={"code": 0, "data": None})


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    CALLS.clear()
    monkeypatch.setenv("FNMUSIC_HOME", str(tmp_path))
    monkeypatch.setenv("FNMUSIC_PLAYLIST_TRACK_CACHE_DIR", str(tmp_path / "tc"))
    monkeypatch.setenv("FNMUSIC_PLAYLIST_TRACK_CACHE_TTL", "3600")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "toplist")
    monkeypatch.delenv("FNMUSIC_NETEASE_PLAYLIST_ORDER", raising=False)
    pl._reset_live_env_cache_for_test()
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770")
    netease_auth.invalidate_state()
    yield
    CALLS.clear()


def _open_playlist(client) -> dict:
    r = client.get(f"/music/api/v1/track/playlist-detail/list?playlistGUID={GUID}&page=1&size=50")
    assert r.status_code == 200
    return r.json()


def test_second_open_hits_cache_zero_upstream(wired):
    with TestClient(app) as client:
        first = _open_playlist(client)
        assert first["data"]["total"] == 1
        assert CALLS[f"/api/v1/playlist/424242/tracks"] == 1, "首次打开应现场拉取"

        second = _open_playlist(client)
        assert second["data"]["total"] == 1
        assert second["data"]["list"][0]["title"] == first["data"]["list"][0]["title"]
    assert CALLS[f"/api/v1/playlist/424242/tracks"] == 1, \
        "缓存命中时绝不能再打上游——这是「打开秒开」的全部"


def test_stale_cache_returns_old_then_refreshes_in_background(wired, monkeypatch):
    # 预热一份缓存（晴天），然后把它变陈旧、并让上游改返回「七里香」
    with TestClient(app) as client:
        _open_playlist(client)
    assert pl.load_cached_tracks(GUID) is not None

    path = pl._tracks_cache_path(GUID)
    with open(path, encoding="utf-8") as fh:
        body = json.loads(fh.read())
    body["ts"] = time.time() - 7200  # 超过 1h 的 TTL
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body))
    monkeypatch.setenv("_MB_SONG", "七里香")

    with TestClient(app) as client:
        got = _open_playlist(client)
        # 1. 先返回旧值：打开永远是快的，内容是上一次的
        assert got["data"]["list"][0]["title"] == "晴天", "陈旧缓存应先返回旧值"

        # 2. 后台单飞刷新最终落盘新值（轮询等待，上限 5s）
        deadline = time.time() + 5
        refreshed = None
        while time.time() < deadline:
            hit = pl.load_cached_tracks(GUID)
            if hit and hit[1] and hit[1][0].get("title") == "七里香":
                refreshed = True
                break
            time.sleep(0.1)
        assert refreshed, "后台刷新应把新内容写进缓存"

        # 3. 再打开：返回新值，且没有引发重复刷新
        got2 = _open_playlist(client)
        assert got2["data"]["list"][0]["title"] == "七里香"


def test_refresh_failure_keeps_old_cache(wired, monkeypatch):
    with TestClient(app) as client:
        _open_playlist(client)
    path = pl._tracks_cache_path(GUID)
    with open(path, encoding="utf-8") as fh:
        body = json.loads(fh.read())
    body["ts"] = time.time() - 7200
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body))

    def broken_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/playlist/424242/tracks":
            return httpx.Response(500)
        return musicbox_handler(request)

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(broken_handler), base_url="http://127.0.0.1:8770")
    with TestClient(app) as client:
        got = _open_playlist(client)
        assert got["data"]["total"] == 1, "上游炸了也得有旧缓存兜底"
    deadline = time.time() + 3
    while time.time() < deadline:
        if pl.load_cached_tracks(GUID) is not None:
            break
        time.sleep(0.1)
    assert pl.load_cached_tracks(GUID) is not None, "刷新失败绝不能清掉旧缓存"


def test_warm_endpoint_refreshes_all_registry_playlists(wired):
    pl.remember(pl.build_record(GUID, "榜｜测试榜", "", 1, "toplist"))
    pl.save_registry()
    with TestClient(app) as client:
        r = client.post("/_ext/playlists/warm")
        assert r.status_code == 200
        assert r.json()["data"]["started"] is True
        assert r.json()["data"]["total"] == 1

        deadline = time.time() + 5
        while time.time() < deadline:
            if pl.load_cached_tracks(GUID) is not None:
                break
            time.sleep(0.1)
    assert pl.load_cached_tracks(GUID) is not None, "预热应把注册表里的歌单都写进缓存"

    # 重复触发：已在跑/刚跑完时幂等，不炸
    with TestClient(app) as client:
        r = client.post("/_ext/playlists/warm")
        assert r.status_code == 200


def test_preview_reports_cache_stats(wired):
    with TestClient(app) as client:
        r = client.get("/_ext/playlists/preview")
        assert r.status_code == 200
        cache = r.json()["data"]["cache"]
        assert cache["ttl_s"] == 3600
        assert cache["refresh_at"] == "04:30"
        assert "cached" in cache and "total" in cache
