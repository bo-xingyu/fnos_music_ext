"""每日推荐：抓取网易云官方「每日推荐」歌单。

推荐内容直接来自扫码登录的那个私人网易云账号
（``/weapi/v3/discovery/recommend/songs``，由 musicbox 服务代理），
不再由大模型凭空生成，也不再按收听记录检索兜底——网易云自己就是推荐引擎。

因此：
- **未登录时不出日推**。没有登录态就拿不到账号个性化推荐，硬塞一份热门填充
  列表只会让飞牛音乐里出现一个内容不对的歌单。此时返回空 bundle，
  代理层不注入「每日推荐」，登录状态由 netease_auth 负责推送提醒。
- 日推按自然日缓存，当天内稳定；跨天自动重建并清理旧缓存。
- 每个飞牛用户有独立缓存与独立 guid（红心隔离），但底层网易云账号只有一个。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

import httpx

logger = logging.getLogger("fnmusic_proxy.recommend")

try:  # 作为包导入（proxy.app）
    from . import netease_items, netease_auth
except ImportError:  # uvicorn --app-dir proxy 扁平导入
    import netease_items  # type: ignore
    import netease_auth  # type: ignore

DAILY_GUID_PREFIX = "online:playlist:daily:"
PLAYLIST_SIZE = int(os.environ.get("FNMUSIC_DAILY_LIMIT") or 20)
DAILY_FETCH_TIMEOUT_S = float(os.environ.get("FNMUSIC_DAILY_TIMEOUT", "25"))
BUILD_BUDGET_S = float(os.environ.get("FNMUSIC_DAILY_BUDGET", "30"))


# === 路径与配置 ===


def home_dir() -> str:
    env = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if env:
        return env
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def recommend_cache_dir() -> str:
    return os.environ.get("FNMUSIC_RECOMMEND_DIR") or os.path.join(home_dir(), "recommend_cache")


def play_history_dir() -> str:
    return os.environ.get("FNMUSIC_PLAY_HISTORY_DIR") or os.path.join(home_dir(), "play_history")


def today_key(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d")


def daily_playlist_guid(day: str | None = None, user_guid: str = "") -> str:
    day = day or today_key()
    suffix = re.sub(r"[^A-Za-z0-9]", "", user_guid)[:12]
    if suffix:
        return f"{DAILY_GUID_PREFIX}{day}:{suffix}"
    return f"{DAILY_GUID_PREFIX}{day}"


def is_daily_playlist_guid(guid: str | None) -> bool:
    return str(guid or "").startswith(DAILY_GUID_PREFIX)


def _safe_user_name(user_guid: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9\-_]", "_", str(user_guid or "").strip())
    return safe or "shared"


def _atomic_write_json(path: str, payload: Any) -> bool:
    parent = os.path.dirname(path) or "."
    part = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(part, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("failed to write %s: %s", path, e)
        if os.path.exists(part):
            try:
                os.remove(part)
            except Exception:
                pass
        return False


# === 在线播放历史（飞牛多用户隔离） ===


def load_online_play_history(user_guid: str) -> list[dict]:
    path = os.path.join(play_history_dir(), f"{_safe_user_name(user_guid)}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [x for x in data["items"] if isinstance(x, dict)]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception as e:
        logger.warning("failed to load play history for %s: %s", user_guid, e)
    return []


def save_online_play_history(user_guid: str, items: list[dict]) -> bool:
    path = os.path.join(play_history_dir(), f"{_safe_user_name(user_guid)}.json")
    return _atomic_write_json(path, {"items": items[-500:]})


def record_online_play(user_guid: str, guid: str, track: dict | None = None) -> None:
    if not guid:
        return
    now = int(time.time())
    items = load_online_play_history(user_guid)
    items = [it for it in items if it.get("guid") != guid]
    snapshot = dict(track or {})
    snapshot.setdefault("guid", guid)
    items.append({"guid": guid, "playedAt": now, "track": snapshot})
    save_online_play_history(user_guid, items)


# === 网易云每日推荐抓取 ===


async def fetch_daily_songs(
    client: httpx.AsyncClient | None, limit: int = PLAYLIST_SIZE
) -> tuple[list[dict], str]:
    """拉取网易云官方每日推荐。返回 (原始 song_info 列表, 错误码)。

    错误码为空串表示成功；其余取值：not_logged_in / unreachable / http_XXX /
    upstream_error / empty / no_client。
    """
    if client is None:
        return [], "no_client"
    try:
        r = await client.get(
            "/api/v1/recommend/daily",
            params={"limit": limit},
            timeout=DAILY_FETCH_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning("netease daily fetch failed: %s", e)
        return [], "unreachable"

    if r.status_code != 200:
        return [], f"http_{r.status_code}"
    try:
        payload = r.json()
    except Exception:
        return [], "bad_json"
    if not isinstance(payload, dict):
        return [], "bad_json"

    if payload.get("ok") is False:
        err = str(payload.get("error") or "upstream_error")
        logger.info("netease daily unavailable: %s", err)
        return [], err

    raw = payload.get("data")
    if not isinstance(raw, list):
        return [], "empty"
    songs = [x for x in raw if isinstance(x, dict)]
    if not songs:
        return [], "empty"
    return songs[:limit], ""


async def build_daily_items(
    client: httpx.AsyncClient | None, limit: int = PLAYLIST_SIZE
) -> tuple[list[dict], str]:
    """把每日推荐映射成扩展统一条目，并批量补齐封面与音质。"""
    songs, err = await fetch_daily_songs(client, limit)
    if err:
        return [], err

    items: list[dict] = []
    seen_ids: set[str] = set()
    for raw in songs:
        item = netease_items.map_netease_song(raw)
        if item is None:
            continue
        sid = netease_items.song_id_of(raw)
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        items.append(item)

    if not items:
        return [], "empty"

    await enrich_items(client, items)
    return items, ""


async def enrich_items(client: httpx.AsyncClient | None, items: list[dict]) -> None:
    """批量拉 /api/v1/songs/detail 补封面并修正音质判定；失败不影响主流程。"""
    if client is None or not items:
        return
    ids = [str(it["id"]).split(":", 1)[-1] for it in items if it.get("id")]
    if not ids:
        return
    try:
        r = await client.get(
            "/api/v1/songs/detail", params={"ids": ",".join(ids[:100])}, timeout=15.0
        )
        if r.status_code != 200:
            return
        payload = r.json()
    except Exception as e:
        logger.debug("daily detail enrich failed: %s", e)
        return

    if not isinstance(payload, dict) or payload.get("ok") is False:
        return
    detail_list = payload.get("data")
    if not isinstance(detail_list, list):
        return

    detail_map: dict[str, dict] = {}
    for d in detail_list:
        if not isinstance(d, dict):
            continue
        dsid = str(d.get("song_id") or d.get("id") or "")
        if dsid:
            detail_map[dsid] = d

    for item in items:
        sid = str(item.get("id") or "").split(":", 1)[-1]
        detail = detail_map.get(sid)
        if detail:
            netease_items.apply_song_detail(item, detail)


# === 缓存 ===


def cache_path(user_guid: str, day: str) -> str:
    return os.path.join(recommend_cache_dir(), _safe_user_name(user_guid), f"{day}.json")


def load_daily_cache(user_guid: str, day: str) -> dict | None:
    path = cache_path(user_guid, day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if str(data.get("day") or "") != day:
            return None
        if data.get("status") not in ("ready", "partial"):
            return None
        tracks = data.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            return None
        return data
    except Exception as e:
        logger.warning("failed to load recommend cache: %s", e)
    return None


def save_daily_cache(user_guid: str, day: str, payload: dict) -> None:
    _atomic_write_json(cache_path(user_guid, day), payload)


def purge_stale_daily_cache(user_guid: str, keep_day: str) -> None:
    folder = os.path.join(recommend_cache_dir(), _safe_user_name(user_guid))
    if not os.path.isdir(folder):
        return
    keep = f"{keep_day}.json"
    for name in os.listdir(folder):
        if not name.endswith(".json") or name == keep:
            continue
        path = os.path.join(folder, name)
        try:
            os.remove(path)
            logger.info("purged stale daily recommend %s", path)
        except Exception as e:
            logger.warning("failed to purge %s: %s", path, e)


# === 本地每日推荐（v2.9）：每天从本地曲库随机抽 N 首 ===
#
# 与网易云「每日推荐」（online:playlist:daily:）严格区分：
#   - guid 命名空间 online:playlist:localdaily:{日}:{用户}
#   - 注册表 channel 标 localdaily（大类顺序里排在 daily 之后，可调）
#   - 缓存目录独立（recommend_cache/<user>/local-<day>.json）
#   - 曲目是**本地文件**（guid 用本地路径指纹生成，播放走官方后端直读文件，
#     不经过网易云取链），无需登录、零外网
# 随机性：以「用户+日期」为随机种子——同一天内多次打开结果一致（不会刷新
# 一次换一批），跨天自动换新。

LOCAL_DAILY_GUID_PREFIX = "online:playlist:localdaily:"
LOCAL_DAILY_DEFAULT_LIMIT = 50
# 扫描深度：以曲库根为基准的目录层数（相对路径里的分隔符个数）。
# 3 层 = 能覆盖「库/歌手/专辑/CD 分碟/文件.mp3」这种偏深的归档布局；
# 再深的大概率不是音乐库的组织方式，继续递归只会拖慢大曲库。
LOCAL_DAILY_SCAN_MAX_DEPTH = 3
# 超大曲库的兜底：扫到这个数就停，避免几万首歌把列表请求拖住。
LOCAL_DAILY_SCAN_MAX_FILES = 20000


def local_daily_enabled() -> bool:
    return (os.environ.get("FNMUSIC_LOCAL_DAILY_ENABLED") or "true").strip().lower() \
        in ("true", "1", "yes", "on")


def local_daily_limit() -> int:
    try:
        return max(1, min(int(os.environ.get("FNMUSIC_LOCAL_DAILY_LIMIT")
                              or LOCAL_DAILY_DEFAULT_LIMIT), 500))
    except (TypeError, ValueError):
        return LOCAL_DAILY_DEFAULT_LIMIT


def local_daily_playlist_guid(day: str | None = None, user_guid: str = "") -> str:
    day = day or today_key()
    suffix = re.sub(r"[^A-Za-z0-9]", "", user_guid)[:12]
    if suffix:
        return f"{LOCAL_DAILY_GUID_PREFIX}{day}:{suffix}"
    return f"{LOCAL_DAILY_GUID_PREFIX}{day}"


def is_local_daily_playlist_guid(guid: str | None) -> bool:
    return str(guid or "").startswith(LOCAL_DAILY_GUID_PREFIX)


def local_daily_playlist_name(day: str) -> str:
    return f"本地每日推荐 {day[4:6]}-{day[6:8]}"


def _local_track_guid(path: str) -> str:
    """本地文件 → 稳定 guid。用绝对路径指纹，避免盘符/卷变化时漂移。"""
    import hashlib

    return "local:file:" + hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()


def _scan_library_audio_files(library_dir: str) -> "list[dict]":
    """扫曲库目录里的音频文件，返回 {path,title,artist,ext}。

    artist/title 尽力从文件名「歌手 - 歌名.ext」解析；没有分隔符就整名当标题。
    扫描失败/目录不存在返回空列表（不抛异常）。
    """
    out: list[dict] = []
    root = str(library_dir or "").strip()
    if not root or not os.path.isdir(root):
        return out
    # ⚠️ 必须带 ImportError 兜底：真机以 `uvicorn app:app --app-dir proxy` 运行，
    # recommend 是顶层模块、没有父包，裸的 `from . import x` 必抛
    # ImportError——这行在下面的 try 之外，一抛整个扫描就废，而且表现成
    # 「歌单静默不出现」（v2.9.2 真机实锤，与 admin_ui._as_path 的注释同一类坑）。
    try:  # 作为包导入（proxy.recommend）
        from . import local_library
    except ImportError:  # 扁平导入
        import local_library  # type: ignore

    audio_exts = local_library.AUDIO_EXTS
    try:
        for base, dirs, files in os.walk(root):
            # 只扫有限层：飞牛曲库普遍「库/歌手/专辑/文件」结构，更深的是用户
            # 自建归档，扫了也大概率不是音乐库的组织方式
            depth = os.path.relpath(base, root).count(os.sep)
            if depth > LOCAL_DAILY_SCAN_MAX_DEPTH:
                dirs[:] = []   # 剪掉这棵子树，别在大曲库上做无谓递归
                continue
            for name in files:
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                if ext not in audio_exts:
                    continue
                path = os.path.join(base, name)
                try:
                    if os.path.getsize(path) <= 0:
                        continue
                except OSError:
                    continue
                stem = os.path.splitext(name)[0].strip()
                artist, title = "", stem
                if " - " in stem:
                    a, t = stem.split(" - ", 1)
                    if a.strip() and t.strip():
                        artist, title = a.strip(), t.strip()
                out.append({"path": path, "title": title, "artist": artist, "ext": ext})
                if len(out) >= LOCAL_DAILY_SCAN_MAX_FILES:
                    return out
    except Exception as e:  # noqa: BLE001 - 扫描失败 = 不出本地日推，不影响其它功能
        logger.warning("scan library for local daily failed (%s): %s: %s",
                       root, type(e).__name__, e)
        return []
    return out


def build_local_daily_tracks(library_dir: str, limit: int, user_guid: str,
                             day: str | None = None) -> "list[dict]":
    """从本地曲库随机抽 limit 首，构造成飞牛 track 对象列表。"""
    import random

    day = day or today_key()
    files = _scan_library_audio_files(library_dir)
    if not files:
        logger.info("local daily: 曲库里没扫到音频文件 library=%s（检查曲库目录是否正确、"
                    "音频是否埋得太深）", library_dir)
        return []

    rng = random.Random(f"{user_guid}:{day}")
    pool = files[:]
    rng.shuffle(pool)

    tracks: list[dict] = []
    for f in pool[:limit]:
        guid = _local_track_guid(f["path"])
        media_type = None
        title = f["title"]
        artist = f["artist"]
        play_format = f["ext"]
        # 复用 app.py 的形状构造太重，这里直接按飞牛 track 形状组装
        artists_list = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
        album_obj = {
            "name": "本地曲库",
            "guid": f"{guid}:album",
            "artists": artists_list,
            "coverId": guid,
        }
        tracks.append({
            "guid": guid,
            "id": guid,
            "title": title,
            "name": title,
            "artist": artist,
            "artists": artists_list,
            "album": album_obj,
            "albumName": "本地曲库",
            "duration": 0,
            "duration_ms": 0,
            "durationMs": 0,
            "codec": play_format,
            "format": play_format,
            "ext": play_format,
            "size": 0,
            "file_size": 0,
            "coverId": guid,
            "cover_url": "",
            "coverUrl": "",
            "source": "local",
            "is_online": False,
            "isFavorite": False,
            "isCue": False,
            "genres": [],
            "accessStatus": 0,
            "_local_path": f["path"],   # 代理内部用；组装应答时剥掉
        })
    return tracks


def local_daily_cache_path(user_guid: str, day: str) -> str:
    return os.path.join(recommend_cache_dir(), _safe_user_name(user_guid), f"local-{day}.json")


def load_local_daily_cache(user_guid: str, day: str) -> dict | None:
    path = local_daily_cache_path(user_guid, day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if str(data.get("day") or "") != day:
            return None
        if not isinstance(data.get("tracks"), list) or not data.get("tracks"):
            return None
        return data
    except Exception as e:
        logger.warning("failed to load local daily cache: %s", e)
    return None


def save_local_daily_cache(user_guid: str, day: str, payload: dict) -> None:
    _atomic_write_json(local_daily_cache_path(user_guid, day), payload)


def purge_stale_local_daily_cache(user_guid: str, keep_day: str) -> None:
    folder = os.path.join(recommend_cache_dir(), _safe_user_name(user_guid))
    if not os.path.isdir(folder):
        return
    keep = f"local-{keep_day}.json"
    for name in os.listdir(folder):
        if name.startswith("local-") and name.endswith(".json") and name != keep:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


def empty_local_daily_bundle(user_guid: str, reason: str = "") -> dict:
    day = today_key()
    guid = local_daily_playlist_guid(day, user_guid)
    return {
        "day": day,
        "guid": guid,
        "status": "unavailable",
        "reason": reason,
        "playlist": build_playlist_record(
            guid=guid, name=local_daily_playlist_name(day),
            cover_id=guid, track_count=0,
        ),
        "tracks": [],
        "builtAt": int(time.time()),
    }


def get_or_build_local_daily(user_guid: str, library_dir: str) -> dict:
    """返回当天的本地每日推荐 bundle（同步——只扫本地磁盘，零网络）。"""
    day = today_key()
    guid = local_daily_playlist_guid(day, user_guid)
    purge_stale_local_daily_cache(user_guid, day)

    cached = load_local_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached

    if not local_daily_enabled():
        logger.info("local daily: 开关关闭（FNMUSIC_LOCAL_DAILY_ENABLED=%s），不注入",
                    os.environ.get("FNMUSIC_LOCAL_DAILY_ENABLED"))
        return empty_local_daily_bundle(user_guid, "disabled")

    limit = local_daily_limit()
    try:
        tracks = build_local_daily_tracks(library_dir, limit, user_guid, day)
    except Exception as e:  # noqa: BLE001
        logger.warning("local daily build failed: %s: %s", type(e).__name__, e)
        return empty_local_daily_bundle(user_guid, "scan_failed")

    if not tracks:
        return empty_local_daily_bundle(user_guid, "library_empty")

    tracks = stamp_playlist_tracks(tracks)
    payload = {
        "day": day,
        "guid": guid,
        "status": "ready",
        "reason": "",
        "playlist": build_playlist_record(
            guid=guid, name=local_daily_playlist_name(day),
            cover_id=guid, track_count=len(tracks),
        ),
        "tracks": tracks,
        "source": "local_daily",
        "builtAt": int(time.time()),
    }
    save_local_daily_cache(user_guid, day, payload)
    logger.info("local daily recommend %s tracks=%d (library=%s)",
                guid, len(tracks), library_dir)
    return payload


# === 歌单装配 ===


def stamp_playlist_tracks(tracks: list[dict], now: int | None = None) -> list[dict]:
    ts = int(now or time.time())
    out: list[dict] = []
    for t in tracks:
        item = dict(t)
        item.setdefault("createdAt", ts)
        item.setdefault("updatedAt", ts)
        item.setdefault("isFavorite", False)
        item.setdefault("isCue", False)
        item.setdefault("accessStatus", 0)
        item.setdefault("year", None)
        item.setdefault("discNo", None)
        item.setdefault("trackNo", None)
        item.setdefault("isrc", "")
        artists = item.get("artists")
        if isinstance(artists, list):
            shaped = []
            for a in artists:
                if not isinstance(a, dict):
                    continue
                aa = dict(a)
                aa.setdefault("guid", aa.get("guid") or f"{item.get('guid')}:artist")
                aa.setdefault("coverId", aa.get("guid"))
                aa.setdefault("createdAt", ts)
                aa.setdefault("updatedAt", ts)
                shaped.append(aa)
            item["artists"] = shaped
        album = item.get("album")
        if isinstance(album, dict):
            alb = dict(album)
            alb.setdefault("createdAt", ts)
            alb.setdefault("updatedAt", ts)
            alb.setdefault("releaseDate", "")
            alb.setdefault("barcode", "")
            item["album"] = alb
        out.append(item)
    return out


def build_playlist_record(
    guid: str,
    name: str,
    cover_id: str | None,
    track_count: int,
    created_at: int | None = None,
) -> dict:
    ts = created_at or int(time.time())
    return {
        "guid": guid,
        "name": name,
        "coverId": cover_id or guid,
        "createdAt": ts,
        "updatedAt": ts,
        "trackCount": track_count,
        "isDaily": True,
    }


def daily_playlist_name(day: str) -> str:
    return f"每日推荐 {day[4:6]}-{day[6:8]}"


def empty_daily_bundle(user_guid: str, reason: str = "") -> dict:
    """不出日推时返回的空壳；代理层看到 tracks 为空就不注入歌单。"""
    day = today_key()
    guid = daily_playlist_guid(day, user_guid)
    return {
        "day": day,
        "guid": guid,
        "status": "unavailable",
        "reason": reason,
        "playlist": build_playlist_record(
            guid=guid, name=daily_playlist_name(day), cover_id=guid, track_count=0
        ),
        "tracks": [],
        "builtAt": int(time.time()),
    }


async def get_or_build_daily(
    user_guid: str,
    musicbox_client: httpx.AsyncClient | None,
    build_track: Callable[[dict], dict],
    *,
    limit: int = PLAYLIST_SIZE,
    force: bool = False,
) -> dict:
    """返回当天的每日推荐 bundle（含 playlist 记录与 tracks）。

    未登录 / 抓取失败时返回 empty_daily_bundle(reason=...)，不写缓存，
    以便下次请求重试（缓存只存成功结果）。
    """
    day = today_key()
    guid = daily_playlist_guid(day, user_guid)
    purge_stale_daily_cache(user_guid, day)

    if not force:
        cached = load_daily_cache(user_guid, day)
        if cached and len(cached.get("tracks") or []) > 0:
            return cached

    if musicbox_client is None or not netease_auth.daily_enabled():
        return empty_daily_bundle(user_guid, "disabled")

    logged_in = await netease_auth.require_login(musicbox_client)
    if not logged_in:
        return empty_daily_bundle(user_guid, "not_logged_in")

    try:
        items, err = await asyncio.wait_for(
            build_daily_items(musicbox_client, limit), timeout=BUILD_BUDGET_S
        )
    except asyncio.TimeoutError:
        logger.warning("daily recommend build timed out for %s", guid)
        return empty_daily_bundle(user_guid, "timeout")

    if err or not items:
        return empty_daily_bundle(user_guid, err or "empty")

    tracks = [build_track(it) for it in items if isinstance(it, dict)]
    tracks = [t for t in tracks if isinstance(t, dict) and t.get("guid")]
    tracks = stamp_playlist_tracks(tracks[:limit])

    if not tracks:
        return empty_daily_bundle(user_guid, "unplayable")

    cover_id = str(tracks[0].get("coverId") or tracks[0].get("guid") or guid)
    payload = {
        "day": day,
        "guid": guid,
        "status": "ready" if len(tracks) >= limit else "partial",
        "reason": "",
        "playlist": build_playlist_record(
            guid=guid,
            name=daily_playlist_name(day),
            cover_id=cover_id,
            track_count=len(tracks),
        ),
        "tracks": tracks,
        "source": "netease_daily",
        "builtAt": int(time.time()),
    }
    save_daily_cache(user_guid, day, payload)
    logger.info("daily recommend %s tracks=%s status=%s", guid, len(tracks), payload["status"])
    return payload
