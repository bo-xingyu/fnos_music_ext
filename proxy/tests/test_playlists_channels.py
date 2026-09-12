"""更多口径歌单、账户歌单与收藏归档（v2.2）的单元测试。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from proxy import download as dl
from proxy import playlists as pl


# ===========================================================================
# guid 规范与口径开关
# ===========================================================================


def test_guid_shapes_are_parsed():
    assert pl.is_channel_guid("online:playlist:ne:12345")
    assert pl.is_channel_guid("online:playlist:nealbum:999")
    assert pl.is_channel_guid(pl.NETEASE_FM_GUID)
    # 曲目 guid 与其它命名空间绝不能被误当成伪歌单
    assert not pl.is_channel_guid("online:netease:12345")
    assert not pl.is_channel_guid("online:playlist:daily:20260911")
    assert not pl.is_channel_guid("")
    assert not pl.is_channel_guid(None)

    assert pl.channel_of("online:playlist:nealbum:999") == "newalbum"
    assert pl.channel_of(pl.NETEASE_FM_GUID) == "fm"
    assert pl.channel_of("online:playlist:ne:7") == "playlist"
    assert pl._target_id("online:playlist:ne:7") == "7"
    assert pl._target_id("online:playlist:nealbum:88") == "88"
    assert pl._target_id(pl.NETEASE_FM_GUID) == ""


def test_channels_enabled_filters_and_dedupes(monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "toplist, mine,toplist,Bogus,,daily")
    # daily 由独立开关 daily_enabled 控制，不该混在这里；未知 key 必须丢弃
    assert pl.channels_enabled() == ("mine", "toplist")

    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "")
    assert pl.channels_enabled() == ("mine", "toplist", "category"), "未配置应有合理默认"

    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "totally-unknown")
    assert pl.channels_enabled() == ()


def test_channel_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "8")
    assert pl.channel_limit() == 8
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "9999")
    assert pl.channel_limit() == 50, "必须有硬上限，否则本地歌单会被淹掉"
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "0")
    assert pl.channel_limit() == 1
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "abc")
    assert pl.channel_limit() == 8


# ===========================================================================
# 封面协议与命名
# ===========================================================================


def test_cover_url_upgraded_to_https():
    """上游歌单 coverImgUrl 是 http://（歌曲 picUrl 才是 https）。

    飞牛 UI 跑在 https 下，http 图片会被浏览器按混合内容拦掉 —— 表现就是没封面。
    """
    assert pl._https("http://p1.music.126.net/a.jpg") == "https://p1.music.126.net/a.jpg"
    assert pl._https("https://p1.music.126.net/a.jpg") == "https://p1.music.126.net/a.jpg"
    assert pl._https("") == ""
    assert pl._https(None) == ""

    rec = pl.build_record("g", "n", "http://x/y.jpg", 3, "category")
    assert rec["cover_url"] == "https://x/y.jpg"


def test_display_name_prefixes():
    """账户歌单按用户要求加前缀区分本地歌单；收藏来的再标一次。"""
    assert pl._display_name("mine", "我的最爱") == "网易云·我的最爱"
    assert pl._display_name("mine", "别人的单", subscribed=True) == "网易云·收藏别人的单"
    assert pl._display_name("toplist", "飙升榜") == "榜｜飙升榜"
    assert pl._display_name("newalbum", "某专辑") == "新碟｜某专辑"
    assert pl._display_name("mine", "") == "网易云·未命名歌单"
    assert pl._display_name("mine", None) == "网易云·未命名歌单"


# ===========================================================================
# 注册表（必须落盘：飞牛只要 guid 来取封面，不重启就还能解析出名字）
# ===========================================================================


@pytest.fixture
def registry_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAYLIST_CACHE_DIR", str(tmp_path / "plc"))
    pl._registry_cache = None
    yield tmp_path / "plc"
    pl._registry_cache = None


def test_registry_roundtrip_survives_reload(registry_dir):
    rec = pl.build_record("online:playlist:ne:42", "榜｜热歌榜",
                          "http://p/x.jpg", 100, "toplist")
    pl.remember(rec)
    pl.save_registry()
    assert (registry_dir / "registry.json").exists()

    pl._registry_cache = None          # 模拟进程重启
    got = pl.lookup("online:playlist:ne:42")
    assert got["name"] == "榜｜热歌榜"
    assert got["cover_url"] == "https://p/x.jpg", "落盘前就应已升级协议"
    assert got["track_count"] == 100


def test_remember_keeps_existing_fields_on_partial_update(registry_dir):
    pl.remember(pl.build_record("online:playlist:ne:7", "网易云·我的单", "http://c", 0, "mine"))
    pl.save_registry()
    # 只更新曲目数，不能把名字与封面冲成空
    pl.remember({"guid": "online:playlist:ne:7", "track_count": 25})
    got = pl.lookup("online:playlist:ne:7")
    assert got["name"] == "网易云·我的单"
    assert got["cover_url"] == "http://c" or got["cover_url"] == "https://c"
    assert got["track_count"] == 25


def test_forget_stale_keeps_daily_and_current(registry_dir):
    pl.remember(pl.build_record("online:playlist:ne:1", "a", "", 1, "toplist"))
    pl.remember(pl.build_record("online:playlist:ne:2", "b", "", 1, "toplist"))
    pl.remember(pl.build_record("online:playlist:daily:20260911", "每日推荐", "", 20, "daily"))
    pl.save_registry()
    n = pl.forget_stale({"online:playlist:ne:1"})
    assert n == 1
    assert pl.lookup("online:playlist:ne:1")
    assert not pl.lookup("online:playlist:ne:2")
    assert pl.lookup("online:playlist:daily:20260911"), "每日推荐归 recommend.py 管，不能被清掉"


def test_registry_corruption_does_not_break_listing(registry_dir, monkeypatch):
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.json").write_text("{not json", encoding="utf-8")
    pl._registry_cache = None
    assert pl.load_registry() == {}, "注册表坏了就重建，不能让歌单列表整个挂掉"


# ===========================================================================
# 未登录时「需登录口径」必须消失，而不是塞一个点进去没内容的空歌单
# ===========================================================================


class _FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, path, params=None, timeout=None):
        self.calls.append((path, params))
        body = self.routes.get(path)
        if body is None:
            return _Resp(404, {})
        return _Resp(200, body)


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        return self._body


@pytest.mark.anyio
async def test_login_required_channels_absent_when_logged_out(registry_dir, monkeypatch):
    # 只启用需登录的口径：本用例要证明的正是「它们一个都不出现」
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine,nrec,fm")
    c = _FakeClient({"/api/v1/playlists/user": {"ok": False, "error": "not_logged_in", "data": []},
                     "/api/v1/playlists/recommend": {"ok": False, "error": "not_logged_in", "data": []},
                     "/api/v1/radio/fm": {"ok": False, "error": "not_logged_in", "data": []}})
    recs, keep, complete = await pl.collect_records(c, logged_in=False)
    assert recs == [], f"未登录时不该注入任何需登录口径：{[r['channel'] for r in recs]}"
    # 连上游都不该去打（明知未登录还去请求，纯属白跑一趟并拖慢列表）
    assert c.calls == []
    assert keep == set() and complete is False, "未登录时清单不完整，不得据此清理注册表"


@pytest.mark.anyio
async def test_public_channels_available_when_logged_out(registry_dir, monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "toplist,category,newalbum")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_LIMIT", "2")
    c = _FakeClient({
        "/api/v1/playlists/toplists": {"ok": True, "data": [
            {"playlist_id": 1, "name": "飙升榜", "cover_url": ""},
            {"playlist_id": 2, "name": "新歌榜", "cover_url": ""},
            {"playlist_id": 3, "name": "热歌榜", "cover_url": ""}]},
        "/api/v1/playlists/category": {"ok": True, "data": [
            {"playlist_id": 11, "name": "华语热单", "cover_url": "http://p/11.jpg", "track_count": 40},
            {"playlist_id": 12, "name": "欧美", "cover_url": "http://p/12.jpg", "track_count": 30}]},
        "/api/v1/playlists/newalbums": {"ok": True, "data": [
            {"album_id": 21, "name": "新专辑A", "artist": "甲", "cover_url": "http://p/21.jpg"},
            {"album_id": 22, "name": "新专辑B", "artist": "", "cover_url": ""}]},
    })
    recs, _, _c = await pl.collect_records(c, logged_in=False)
    chans = [r["channel"] for r in recs]
    assert chans == ["toplist", "toplist", "category", "category", "newalbum", "newalbum"]
    assert [r["guid"] for r in recs][:2] == ["online:playlist:ne:1", "online:playlist:ne:2"]
    assert recs[4]["guid"] == "online:playlist:nealbum:21", "专辑口径必须用自己的 guid 前缀"
    assert recs[2]["name"].startswith("华语｜")
    assert recs[4]["name"] == "新碟｜新专辑A - 甲"
    assert recs[5]["name"] == "新碟｜新专辑B", "没有艺术家时不该出现悬空的 ' - '"
    assert all(r["cover_url"].startswith("https://") or r["cover_url"] == "" for r in recs)


@pytest.mark.anyio
async def test_mine_playlists_mark_subscribed(registry_dir, monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine")
    c = _FakeClient({"/api/v1/playlists/user": {"ok": True, "data": [
        {"playlist_id": 5, "name": "自建单", "cover_url": "http://p/5.jpg", "track_count": 12,
         "subscribed": False},
        {"playlist_id": 6, "name": "收藏单", "cover_url": "", "track_count": 3, "subscribed": True},
    ]}})
    recs, _, _c = await pl.collect_records(c, logged_in=True)
    assert [r["name"] for r in recs] == ["网易云·自建单", "网易云·收藏收藏单"]
    assert pl.lookup("online:playlist:ne:6")["track_count"] == 3


@pytest.mark.anyio
async def test_channel_failure_is_isolated(registry_dir, monkeypatch):
    """一个口径挂了只影响它自己，不能整个歌单列表都出不来。"""
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "toplist,newalbum")

    class _Boom(_FakeClient):
        async def get(self, path, params=None, timeout=None):
            if path.endswith("toplists"):
                raise RuntimeError("upstream exploded")
            return await super().get(path, params, timeout)

    c = _Boom({"/api/v1/playlists/newalbums": {"ok": True, "data": [
        {"album_id": 31, "name": "还在的专辑", "artist": "", "cover_url": ""}]}})
    recs, _, _c = await pl.collect_records(c, logged_in=False)
    assert [r["name"] for r in recs] == ["新碟｜还在的专辑"]


# ===========================================================================
# 歌单内容解析
# ===========================================================================


def test_tracks_path_for_each_guid_kind():
    assert pl.tracks_path_for("online:playlist:ne:7") == ("/api/v1/playlist/7/tracks",
                                                          {"limit": pl.playlist_track_limit()})
    assert pl.tracks_path_for("online:playlist:nealbum:8")[0] == "/api/v1/album/8/tracks"
    assert pl.tracks_path_for(pl.NETEASE_FM_GUID)[0] == "/api/v1/radio/fm"
    assert pl.tracks_path_for("online:playlist:ne:not-a-number") is None
    assert pl.tracks_path_for("online:netease:5") is None
    assert pl.tracks_path_for(None) is None


@pytest.mark.anyio
async def test_resolve_track_items_dedupes_and_maps(registry_dir):
    rows = [{"song_id": 1, "song_name": "甲", "artist": "A", "album_name": "", "duration": 200,
             "quality": "HD 320k", "mp3_url": "http://u/1", "album_pic_url": ""},
            {"song_id": 1, "song_name": "甲重复", "artist": "A", "album_name": "", "duration": 200,
             "quality": "HD 320k", "mp3_url": "http://u/1", "album_pic_url": ""},
            {"song_id": 2, "song_name": "乙", "artist": "B", "album_name": "", "duration": 180,
             "quality": "HD 320k", "mp3_url": "http://u/2", "album_pic_url": ""}]
    c = _FakeClient({"/api/v1/playlist/7/tracks": {"ok": True, "data": rows}})
    enriched = []

    async def fake_enrich(client, items):
        enriched.append(len(items))

    from proxy import netease_items

    items = await pl.resolve_track_items(c, "online:playlist:ne:7",
                                        netease_items.map_netease_song, fake_enrich)
    assert [i["id"] for i in items] == ["netease:1", "netease:2"], "重复曲目必须去重"
    assert enriched == [2], "补齐只应跑一次批量请求"
    # 可播数量回填注册表，让列表上的曲目数是真实值
    assert pl.lookup("online:playlist:ne:7")["track_count"] == 2


@pytest.mark.anyio
async def test_resolve_track_items_tolerates_upstream_failure(registry_dir):
    from proxy import netease_items

    c = _FakeClient({"/api/v1/playlist/9/tracks": {"ok": False, "error": "boom", "data": []}})
    assert await pl.resolve_track_items(c, "online:playlist:ne:9",
                                       netease_items.map_netease_song, None) == []
    c2 = _FakeClient({})           # 端点直接 404
    assert await pl.resolve_track_items(c2, "online:playlist:ne:9",
                                       netease_items.map_netease_song, None) == []


# ===========================================================================
# v2.4：歌单大类顺序自定义 + 稳定时间戳
# ===========================================================================


def test_channel_order_custom_and_fallback(monkeypatch):
    # 自定义顺序：用户给的顺序原样生效，漏掉的按默认序追加
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "toplist, mine, daily")
    assert pl.channel_order()[:3] == ("toplist", "mine", "daily")
    assert pl.channel_order()[3:] == ("nrec", "category", "newalbum", "fm")

    # 未知 key 忽略，空值/坏值回落默认
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "")
    assert pl.channel_order() == ("daily", "mine", "nrec", "toplist", "category", "newalbum", "fm")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "bogus,,???")
    assert pl.channel_order() == ("daily", "mine", "nrec", "toplist", "category", "newalbum", "fm")

    assert pl.rank_of("toplist") == pl.channel_order().index("toplist")
    assert pl.rank_of("nonexistent") == len(pl.CHANNELS)


def test_channels_enabled_follows_custom_order(monkeypatch):
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine,toplist,category")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "category,toplist,mine")
    assert pl.channels_enabled() == ("category", "toplist", "mine"), (
        "勾选的口径要按大类顺序配置输出，否则飞牛列表里的顺序不受用户控制"
    )


def test_stamp_display_order_gives_distinct_descending_timestamps(registry_dir):
    """每条注入条目拿到互不相同、按位置递减的时间戳。

    原先所有条目共用 int(time.time())，客户端的非稳定排序每次刷新都会
    换一个顺序——这正是「歌单顺序不固定」的根因。
    """
    items = [
        {"guid": "online:playlist:daily:20260912", "name": "每日推荐"},
        {"guid": "online:playlist:ne:1", "name": "a", "channel": "mine"},
        {"guid": "online:playlist:ne:2", "name": "b", "channel": "mine"},
        {"guid": "online:playlist:ne:3", "name": "c", "channel": "toplist"},
    ]
    pl.remember(pl.build_record("online:playlist:ne:1", "a", "", 1, "mine"))
    out = pl.stamp_display_order(items)
    ts = [it["createdAt"] for it in out]
    assert ts == sorted(ts, reverse=True), "时间戳必须严格递减"
    assert len(set(ts)) == len(ts), "时间戳必须互不相同，否则非稳定排序仍会乱"
    assert all(it["createdAt"] == it["updatedAt"] for it in out)
    # 注册表 ts 同步：详情页回显要与列表页一致
    assert pl.lookup("online:playlist:ne:1")["ts"] == ts[1]
    # 降序排序（客户端常见行为）应还原注入顺序
    assert [it["name"] for it in sorted(out, key=lambda x: -x["updatedAt"])] == \
        ["每日推荐", "a", "b", "c"]


def test_stamp_display_order_syncs_registry_for_detail_pages(registry_dir):
    pl.remember(pl.build_record("online:playlist:ne:8", "y", "", 1, "category"))
    pl.remember(pl.build_record("online:playlist:ne:9", "x", "", 1, "category"))
    pl.save_registry()
    pl.stamp_display_order([
        {"guid": "online:playlist:ne:8", "name": "y"},
        {"guid": "online:playlist:ne:9", "name": "x"},
    ])
    reg = pl.lookup("online:playlist:ne:9")
    assert reg["ts"] == pl.lookup("online:playlist:ne:8")["ts"] - 1


# ===========================================================================
# v2.4：playlist_list 注入顺序 = 大类顺序配置，且时间戳严格有序
# ===========================================================================

import httpx
from fastapi.testclient import TestClient

from proxy import netease_auth
from proxy.app import app as proxy_app


def _mb_handler_logged_in():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/status":
            return httpx.Response(200, json={"ok": True,
                                             "data": {"logged_in": True, "nickname": "u"}})
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/playlists/user":
            return httpx.Response(200, json={"ok": True, "data": [
                {"playlist_id": 11, "name": "我的自建单", "cover_url": "",
                 "track_count": 5, "subscribed": False}]})
        if path == "/api/v1/playlists/toplists":
            return httpx.Response(200, json={"ok": True, "data": [
                {"playlist_id": 19723756, "name": "飙升榜", "cover_url": "",
                 "track_count": 100}]})
        if path == "/api/v1/recommend/daily":
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "d1", "song_name": "晴天", "artist": "周", "album_name": "叶",
                 "duration": 269, "quality": "SQ"}]})
        if path == "/api/v1/songs/detail":
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "d1", "album_pic_url": "http://p/1.jpg", "has_sq": True}]})
        return httpx.Response(404, json={"ok": False})
    return handler


def _upstream_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/user/me"):
        return httpx.Response(200, json={"code": 0, "data": {"guid": "user-ord"}})
    if path.endswith("/playlist/list"):
        return httpx.Response(200, json={"code": 0, "data": {
            "list": [{"guid": "localpl", "name": "本地单", "coverId": "c",
                      "createdAt": 1, "updatedAt": 1}], "total": 1}})
    if "search/track" in path:
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
    return httpx.Response(200, json={"code": 0, "data": None})


def test_playlist_list_follows_custom_channel_order(registry_dir, monkeypatch):
    """大类顺序配置必须左右注入顺序（含 daily 的位置），时间戳严格递减。"""
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNELS", "mine,toplist")
    monkeypatch.setenv("FNMUSIC_NETEASE_CHANNEL_ORDER", "toplist,daily,mine")
    monkeypatch.setenv("FNMUSIC_DAILY_ENABLED", "true")
    proxy_app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_handler), base_url="http://unix")
    proxy_app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_mb_handler_logged_in()),
        base_url="http://127.0.0.1:8770")
    netease_auth.invalidate_state()

    with TestClient(proxy_app) as client:
        body = client.get("/music/api/v1/playlist/list").json()

    lst = body["data"]["list"]
    names = [it["name"] for it in lst]
    # toplist → daily → mine → 官方本地歌单
    assert names[0].startswith("榜｜"), names
    from proxy import recommend as _rec
    assert _rec.is_daily_playlist_guid(lst[1]["guid"]), names
    assert names[2].startswith("网易云·"), names
    assert lst[-1]["guid"] == "localpl"
    # 头部时间戳严格递减：客户端怎么排序都得到同一顺序
    head_ts = [it["updatedAt"] for it in lst[:-1]]
    assert head_ts == sorted(head_ts, reverse=True), head_ts
    assert len(set(head_ts)) == len(head_ts), "时间戳必须互不相同"
