"""网易云「更多口径歌单」与「账户歌单」注入飞牛歌单列表。

飞牛的歌单列表来自官方接口 ``/music/api/v1/playlist/list``，本模块在那份列表**头部**
追加若干由网易云内容构成的伪歌单。每日推荐（``online:playlist:daily:``）由
``recommend.py`` 单独负责，本模块不重叠；其余口径都在这里。

guid 规范（都在 ``online:playlist:`` 命名空间下，与曲目 guid ``online:netease:<sid>``
区分开）：

    online:playlist:ne:{playlist_id}     真实网易云歌单（账户歌单/推荐歌单/排行榜/分类歌单）
    online:playlist:nealbum:{album_id}   新碟上架里的专辑（当作一个歌单看待）
    online:playlist:nefm                 私人FM 无限流（做成会滚动更新的伪歌单）

为什么账户歌单、排行榜、分类歌单共用 ``ne:{id}`` 这一种 guid：它们最终都是「网易云
歌单 id → 曲目 id 列表」，取内容的路径完全一致，没必要为每个口径造一套 guid；
口径差别只体现在**注入时给它起的名字与封面**上，这些存在注册表里。

登录要求：账户歌单/推荐歌单/私人FM 需登录（上游语义就是账号绑定的），排行榜/分类
歌单/新碟无需登录。未登录时对应口径**不出现在列表里**，而不是塞一个空歌单——
空歌单点进去没内容，比不出现更容易让人误判成"坏了"。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("fnmusic_proxy")

NETEASE_PLAYLIST_PREFIX = "online:playlist:ne:"
NETEASE_ALBUM_PREFIX = "online:playlist:nealbum:"
NETEASE_FM_GUID = "online:playlist:nefm"
CHANNEL_NS = "online:playlist:"

# 口径 -> (是否需要登录, 展示名前缀)
CHANNELS: dict[str, dict[str, Any]] = {
    "daily":    {"needs_login": True,  "label": "每日推荐"},     # 由 recommend.py 负责
    "mine":     {"needs_login": True,  "label": "我的歌单"},     # 自建 + 收藏
    "nrec":     {"needs_login": True,  "label": "推荐歌单"},     # recommend_resource
    "toplist":  {"needs_login": False, "label": "排行榜"},
    "category": {"needs_login": False, "label": "分类歌单"},
    "newalbum": {"needs_login": False, "label": "新碟上架"},
    "fm":       {"needs_login": True,  "label": "私人FM"},
}
DEFAULT_CHANNELS = "mine,toplist,category"

# 大类展示顺序（v2.4）：飞牛歌单列表里各口径的先后由它决定，管理页可改。
# 默认值同时是兜底序：未列出的口径按此顺序追加在末尾。
DEFAULT_CHANNEL_ORDER = "daily,mine,nrec,toplist,category,newalbum,fm"

_PREFIX_BY_CHANNEL = {"mine": "", "nrec": "推荐", "toplist": "榜",
                      "category": "", "newalbum": "新碟", "fm": "电台"}


def _flag(name: str, default: str) -> bool:
    return str(os.environ.get(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


def channel_order() -> tuple[str, ...]:
    """全部口径的展示顺序（含 daily）。

    解析 ``FNMUSIC_NETEASE_CHANNEL_ORDER``（逗号分隔的口径 key）：
    按用户给的顺序排，漏掉的口径按默认序追加在末尾，未知 key 忽略。
    任何解析异常都回落到默认序——顺序配置坏了不能让歌单列表整个消失。
    """
    raw = (os.environ.get("FNMUSIC_NETEASE_CHANNEL_ORDER") or "").strip()
    ordered: list[str] = []
    if raw:
        for part in raw.replace(";", ",").split(","):
            key = part.strip().lower()
            if key in CHANNELS and key not in ordered:
                ordered.append(key)
    for key in DEFAULT_CHANNEL_ORDER.split(","):
        if key not in ordered:
            ordered.append(key)
    return tuple(ordered)


def rank_of(channel: str) -> int:
    try:
        return channel_order().index(channel)
    except ValueError:
        return len(CHANNELS)


def channels_enabled() -> tuple[str, ...]:
    """管理页勾选的口径。除 daily 外都在这里生效；daily 由 recommend.py 单独控制。

    输出按 ``channel_order()`` 的**用户自定义顺序**排列（默认即规范顺序）——
    飞牛歌单列表里的顺序由此决定，且必须稳定：不能因为用户先勾了排行榜
    就跑到我的歌单前面去。
    """
    raw = (os.environ.get("FNMUSIC_NETEASE_CHANNELS") or "").strip()
    if not raw:
        # 未配置、或被手工编辑成空串，一律按默认口径。
        # 管理页的 _as_channels 校验器本就不允许保存空值（会提示"至少勾选一个"），
        # 所以空值只可能是手工改出来的；此时回落到默认比让所有歌单凭空消失更合理。
        raw = DEFAULT_CHANNELS
    picked = {str(part).strip().lower() for part in raw.split(",")}
    return tuple(k for k in channel_order() if k != "daily" and k in picked)


def channel_limit() -> int:
    """每个口径最多注入几个歌单。

    排行榜上游有 63 个、分类歌单一次能取 50 个，全塞进飞牛歌单列表会把用户自己的
    本地歌单淹掉，所以必须有上限（默认 8，可调）。
    """
    try:
        return max(1, min(int(os.environ.get("FNMUSIC_NETEASE_CHANNEL_LIMIT", "8") or 8), 50))
    except ValueError:
        return 8


def category_name() -> str:
    return str(os.environ.get("FNMUSIC_NETEASE_CATEGORY", "华语") or "华语").strip()


def playlist_track_limit() -> int:
    try:
        return max(1, min(int(os.environ.get("FNMUSIC_PLAYLIST_TRACK_LIMIT", "300") or 300), 1000))
    except ValueError:
        return 300


def is_channel_guid(guid: str | None) -> bool:
    """是否本模块负责的伪歌单 guid（不含每日推荐）。"""
    g = str(guid or "")
    return g.startswith(NETEASE_PLAYLIST_PREFIX) or g.startswith(NETEASE_ALBUM_PREFIX) \
        or g == NETEASE_FM_GUID


def channel_of(guid: str | None) -> str:
    g = str(guid or "")
    if g.startswith(NETEASE_ALBUM_PREFIX):
        return "newalbum"
    if g == NETEASE_FM_GUID:
        return "fm"
    return "playlist"


def _target_id(guid: str | None) -> str:
    """从 guid 里取出网易云歌单 id / 专辑 id。"""
    g = str(guid or "")
    for pref in (NETEASE_ALBUM_PREFIX, NETEASE_PLAYLIST_PREFIX):
        if g.startswith(pref):
            return str(g[len(pref):]).strip()
    return ""


# ---------------------------------------------------------------------------
# 注册表：guid -> {name, cover_url, track_count, channel}
#
# 必须落盘。飞牛点开歌单封面时只会带 guid 过来，不会带名字与封面；若只存在内存里，
# 服务重启后 /static/cover 与 playlist/detail 就只能显示"网易云歌单 12345"且无封面。
# 而列表刷新可能间隔很久，不能指望届时还在内存中。
# ---------------------------------------------------------------------------


def registry_path() -> str:
    base = os.environ.get("FNMUSIC_PLAYLIST_CACHE_DIR") or os.path.join(
        os.environ.get("FNMUSIC_HOME") or os.path.expanduser("~"), "playlist_cache")
    return os.path.join(base, "registry.json")


_registry_cache: dict[str, dict] | None = None


def load_registry() -> dict[str, dict]:
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    path = registry_path()
    data: dict[str, dict] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - 注册表坏了就重建，不能让歌单列表挂掉
        logger.warning("playlist registry load failed (%s): %s: %s",
                       path, type(exc).__name__, exc)
    _registry_cache = data
    return data


def save_registry() -> None:
    global _registry_cache
    reg = load_registry()
    path = registry_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("playlist registry save failed: %s: %s", type(exc).__name__, exc)


def remember(record: dict) -> None:
    """把一条伪歌单的名字/封面记进注册表。"""
    guid = str(record.get("guid") or "")
    if not guid:
        return
    reg = load_registry()
    old = reg.get(guid) or {}
    entry = {
        "name": str(record.get("name") or old.get("name") or ""),
        "cover_url": str(record.get("cover_url") or old.get("cover_url") or ""),
        "track_count": int(record.get("track_count") or old.get("track_count") or 0),
        "channel": str(record.get("channel") or old.get("channel") or ""),
        "ts": int(time.time()),
    }
    reg[guid] = entry
    _registry_cache = reg


def lookup(guid: str | None) -> dict:
    reg = load_registry()
    return reg.get(str(guid or "")) or {}


def forget_stale(keep_guids: set[str]) -> int:
    """清掉本轮列表里已经不再出现的条目（例如取消了某个口径、或歌单被删除）。"""
    reg = load_registry()
    doomed = [g for g in reg if str(g).startswith(CHANNEL_NS) and g not in keep_guids
              and not str(g).startswith("online:playlist:daily:")]
    for g in doomed:
        reg.pop(g, None)
    if doomed:
        save_registry()
    return len(doomed)


# ---------------------------------------------------------------------------
# 名称/封面归一化
# ---------------------------------------------------------------------------


def _display_name(channel: str, raw_name: str, subscribed: bool = False) -> str:
    """给伪歌单起名。

    账户歌单按用户要求加「网易云·」前缀，与本地歌单区分；收藏来的歌单再标一次，
    免得把别人的歌单当成自己的。前缀必须短——飞牛列表一行放不下长名字。
    """
    name = str(raw_name or "").strip() or "未命名歌单"
    prefix = _PREFIX_BY_CHANNEL.get(channel, "")
    if channel == "mine":
        tag = "网易云·收藏" if subscribed else "网易云·"
        return f"{tag}{name}"
    if prefix:
        return f"{prefix}｜{name}"
    return name


def build_record(guid: str, name: str, cover_url: str, track_count: int, channel: str) -> dict:
    return {
        "guid": guid,
        "name": name,
        "cover_url": _https(cover_url),
        "track_count": int(track_count or 0),
        "channel": channel,
        "createdAt": int(time.time()),
        "updatedAt": int(time.time()),
    }


def _https(url: Any) -> str:
    """歌单/专辑封面上游给的是 http://，必须升级协议。

    飞牛 UI 跑在 https 下，http 图片会被浏览器按混合内容拦掉 —— 表现就是裂图/无封面。
    （歌曲封面 picUrl 上游本来就是 https，只有歌单 coverImgUrl 是 http）
    """
    s = str(url or "").strip()
    if s.startswith("http://"):
        return "https://" + s[len("http://"):]
    return s


# ---------------------------------------------------------------------------
# 拉取各口径的歌单清单
# ---------------------------------------------------------------------------


async def _get_json(client, path: str, params: dict | None = None) -> dict:
    try:
        r = await client.get(path, params=params or {}, timeout=20.0)
        if r.status_code != 200:
            return {}
        body = r.json()
        return body if isinstance(body, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 单个口径失败不能拖垮整个歌单列表
        logger.warning("musicbox %s failed: %s: %s", path, type(exc).__name__, exc)
        return {}


async def fetch_channel_records(client, channel: str, logged_in: bool) -> list[dict]:
    """取某个口径要注入的伪歌单清单。失败/未登录返回空列表（宁可不出现）。"""
    limit = channel_limit()
    spec = CHANNELS.get(channel) or {}
    if spec.get("needs_login") and not logged_in:
        return []

    if channel == "fm":
        # 私人FM 是无限流，做成一个固定的伪歌单；曲目数按上次抓到的量报
        rows = (await _get_json(client, "/api/v1/radio/fm", {"limit": 10})).get("data") or []
        if not isinstance(rows, list) or not rows:
            return []
        rec = build_record(NETEASE_FM_GUID, "私人FM｜网易云电台", "", len(rows), "fm")
        remember(rec)
        return [rec]

    if channel == "newalbum":
        body = await _get_json(client, "/api/v1/playlists/newalbums", {"limit": min(limit, 50)})
        rows = body.get("data") or []
        out = []
        for a in rows if isinstance(rows, list) else []:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("album_id") or "")
            if not aid:
                continue
            artist = str(a.get("artist") or "").strip()
            title = str(a.get("name") or f"专辑 {aid}")
            name = f"新碟｜{title}" + (f" - {artist}" if artist else "")
            rec = build_record(f"{NETEASE_ALBUM_PREFIX}{aid}", name,
                               a.get("cover_url"), 0, "newalbum")
            remember(rec)
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    if channel == "category":
        path, params = ("/api/v1/playlists/category",
                        {"cat": category_name(), "order": "hot", "limit": min(limit, 50)})
        body = await _get_json(client, path, params)
        kind, rows = "category", body.get("data") or []
    elif channel == "toplist":
        body = await _get_json(client, "/api/v1/playlists/toplists")
        kind, rows = "toplist", body.get("data") or []
    elif channel == "mine":
        body = await _get_json(client, "/api/v1/playlists/user", {"limit": 200})
        kind, rows = "mine", body.get("data") or []
        if body.get("error"):
            logger.info("mine playlists unavailable: %s", body.get("error"))
            return []
    elif channel == "nrec":
        body = await _get_json(client, "/api/v1/playlists/recommend")
        kind, rows = "nrec", body.get("data") or []
    else:
        return []

    out = []
    for p in rows if isinstance(rows, list) else []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("playlist_id") or "")
        if not pid:
            continue
        name = _display_name(kind, p.get("name"), bool(p.get("subscribed")))
        if kind == "category":
            name = f"{category_name()}｜{p.get('name')}"
        rec = build_record(f"{NETEASE_PLAYLIST_PREFIX}{pid}", name, p.get("cover_url"),
                           p.get("track_count"), kind)
        remember(rec)
        out.append(rec)
        if len(out) >= limit:
            break
    if out:
        save_registry()
    return out


async def collect_records(client, logged_in: bool) -> tuple[list[dict], set[str], bool]:
    """按勾选顺序汇总全部口径。任一口径异常只影响它自己。

    返回 ``(清单, guid 集合, complete)``。``complete`` 表示「这份清单可信到足以据此
    清理注册表」：未登录或有口径抛异常时为 False —— 那种情况下清单必然不完整，
    若仍拿它去做 forget_stale，会把暂时没取到的条目（连同名字与封面）一并抹掉，
    用户只是掉线一次就得重新等所有歌单刷新。
    """
    records: list[dict] = []
    complete = True
    for ch in channels_enabled():
        spec = CHANNELS.get(ch) or {}
        if spec.get("needs_login") and not logged_in:
            # 需登录的口径缺席是**正常**的，不代表清单不可信
            continue
        try:
            records.extend(await fetch_channel_records(client, ch, logged_in))
        except Exception as exc:  # noqa: BLE001
            logger.warning("channel %s failed: %s: %s", ch, type(exc).__name__, exc)
            complete = False
    # guid 去重（不同口径可能给出同一个网易云歌单，例如账户歌单同时也在推荐里）
    seen: set[str] = set()
    uniq: list[dict] = []
    for r in records:
        g = str(r.get("guid") or "")
        if not g or g in seen:
            continue
        seen.add(g)
        uniq.append(r)
    if not logged_in:
        complete = False
    return uniq, seen, complete


def stamp_display_order(items: list[dict]) -> list[dict]:
    """给最终注入顺序里的条目盖上**互不相同且单调递减**的 createdAt/updatedAt。

    飞牛客户端会按 updatedAt 对歌单列表排序，而原先每条记录的时间都是
    ``int(time.time())`` —— 同一秒内的一堆完全相同的时间戳，遇上客户端的
    非稳定排序就是每次刷新都换一个顺序（用户看到的「顺序不固定」）。
    现在按注入位置依次减一秒：客户端无论按 updatedAt 升序还是降序排，
    得到的都是**确定**的顺序（降序=注入序，升序=严格反序），不再随机。

    就地修改并返回同一列表。注册表里的 ts 同步更新：playlist/detail 与
    batch-detail 回显的 createdAt/updatedAt 取的就是它，两处必须一致，
    否则详情页与列表页的顺序语义打架。
    """
    now = int(time.time())
    reg = load_registry()
    dirty = False
    for i, it in enumerate(items):
        ts = now - i
        it["createdAt"] = ts
        it["updatedAt"] = ts
        guid = str(it.get("guid") or "")
        entry = reg.get(guid)
        if guid and isinstance(entry, dict) and entry.get("ts") != ts:
            entry["ts"] = ts
            dirty = True
    if dirty:
        global _registry_cache
        _registry_cache = reg
        save_registry()
    return items


# ---------------------------------------------------------------------------
# 解析歌单内容
# ---------------------------------------------------------------------------


def tracks_path_for(guid: str | None) -> tuple[str, dict] | None:
    """guid -> (musicbox 端点, query)。"""
    g = str(guid or "")
    limit = playlist_track_limit()
    if g == NETEASE_FM_GUID:
        return "/api/v1/radio/fm", {"limit": min(limit, 20)}
    tid = _target_id(g)
    if not tid or not tid.isdigit():
        return None
    if g.startswith(NETEASE_ALBUM_PREFIX):
        return f"/api/v1/album/{tid}/tracks", {"limit": limit}
    return f"/api/v1/playlist/{tid}/tracks", {"limit": limit}


async def resolve_track_items(
    client,
    guid: str | None,
    map_song: Callable[[dict], dict | None],
    enrich: Callable[[Any, list[dict]], Awaitable[None]] | None = None,
) -> list[dict]:
    """把伪歌单解析成扩展内部条目列表（已补封面、已去重）。

    ``map_song`` / ``enrich`` 由调用方注入（app.py 的 ``netease_items.map_netease_song``
    与 ``_enrich_netease_items``），避免本模块与 app 形成循环导入。
    """
    route = tracks_path_for(guid)
    if route is None:
        return []
    path, params = route
    body = await _get_json(client, path, params)
    if body.get("ok") is False:
        logger.info("playlist tracks unavailable guid=%s: %s", guid, body.get("error"))
        return []
    raw_rows = body.get("data") or []
    if not isinstance(raw_rows, list):
        return []

    items: list[dict] = []
    song_ids: list[str] = []
    seen: set[str] = set()
    for raw in raw_rows:
        item = map_song(raw)
        if item is None:
            continue
        sid = str(item.get("id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        items.append(item)
        song_ids.append(sid)
    if items and enrich is not None:
        await enrich(client, items)
        items = [it for it in items if str(it.get("id") or "") in set(song_ids)]

    # 实际可播数量回填注册表，让列表上的曲目数是真实值而不是上游的 trackCount
    reg = lookup(guid)
    if items and (not reg or reg.get("track_count") != len(items)):
        remember({"guid": str(guid), "name": reg.get("name", ""),
                  "cover_url": reg.get("cover_url", ""),
                  "track_count": len(items), "channel": reg.get("channel", "")})
        save_registry()
    return items
