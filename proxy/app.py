"""fnmusic-ext 拦截代理 (FastAPI + httpx).

唯一在线音源是网易云（musicbox 服务），且只使用扫码登录的那个私人账号的权益：

    FNMUSIC_NETEASE_ENABLED=true          启用在线音源
    FNMUSIC_FREE_ONLY_ON_LOGOUT=true      未登录时降级为只播免费曲目
    FNMUSIC_DAILY_ENABLED=true            抓取网易云官方「每日推荐」歌单
    FNMUSIC_PUSHPLUS_TOKEN=...            掉线/VIP 临期时通过 PushPlus 推送提醒

功能：
1. 通用透传：所有非拦截路径原样转发到 trim-music unix socket
2. 搜索合并：GET /music/api/v1/search/track* （兼容 q/keyword，叠加网易云结果）
3. 在线播放：stream + HLS 兜底 + transcode 空操作 + tee 缓存回放（音频与歌词 sidecar）
4. 在线元数据/歌词/封面
5. 网易云官方每日推荐歌单注入（需登录）
6. GET /_ext/healthz
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Coroutine
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

try:
    from . import netease_auth
    from . import netease_items
    from . import local_library
    from . import playlists
    from . import download as downloader
    from . import quality
    from . import pushplus
    from . import recommend as dailyrec
    from . import trimgw
    from . import local_files
    from .version import get_version
except ImportError:  # uvicorn --app-dir proxy
    import netease_auth  # type: ignore
    import netease_items  # type: ignore
    import local_library  # type: ignore
    import playlists  # type: ignore
    import download as downloader  # type: ignore
    import quality  # type: ignore
    import pushplus  # type: ignore
    import recommend as dailyrec  # type: ignore
    import trimgw  # type: ignore
    import local_files  # type: ignore
    from version import get_version  # type: ignore

logger = logging.getLogger("fnmusic_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_HOME = dailyrec.home_dir()


def _flag(name: str, default: str) -> bool:
    return (os.environ.get(name) or default).strip().lower() in ("true", "1", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


# 播放链路（本地曲目 stream / HLS / 转码会话）转发到官方后端的读超时（秒）。
# 共享上游客户端是 30s；但官方后端处理 /track/transcode 要等 ffmpeg 产出
# 首个分片才应答，大文件 + 慢磁盘时 30s 不够，超时会把「能播」变成 504，
# 用户看到的就是「开转码后本地音乐全部播放失败」。
PLAYBACK_FORWARD_TIMEOUT_S = _float("FNMUSIC_PLAYBACK_FORWARD_TIMEOUT_S", 300.0)


CONF = {
    "musicbox_url": os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"),
    "netease_enabled": _flag("FNMUSIC_NETEASE_ENABLED", "true"),
    "netease_wait_s": _float("FNMUSIC_NETEASE_WAIT_S", 3.0),
    "netease_quality": os.environ.get("FNMUSIC_NETEASE_QUALITY", "lossless"),
    "netease_search_limit": _int("FNMUSIC_NETEASE_SEARCH_LIMIT", 50),
    # 未登录时降级为只播免费曲目；关掉则未登录完全不提供在线播放
    "free_only_on_logout": _flag("FNMUSIC_FREE_ONLY_ON_LOGOUT", "true"),
    "upstream_sock": os.environ.get("FNMUSIC_UPSTREAM_SOCK", "/var/run/trim_music_upstream.socket"),
    "online_limit": _int("FNMUSIC_ONLINE_LIMIT", 30),
    "search_list_path": os.environ.get("FNMUSIC_SEARCH_LIST_PATH", "data.list"),
    "cache_dir": os.environ.get("FNMUSIC_CACHE_DIR", os.path.join(_HOME, "cache")),
    # 空=从飞牛 shared_library.path 自动探测；测试可覆盖到临时目录
    "library_dir": os.environ.get("FNMUSIC_LIBRARY_DIR", ""),
    "music_db": os.environ.get(
        "FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db"
    ),
    "merge_suggest": _flag("FNMUSIC_MERGE_SUGGEST", "false"),
    "lyric_field": os.environ.get("FNMUSIC_LYRIC_FIELD", "data.lyric"),
    "search_timeout": _float("FNMUSIC_SEARCH_TIMEOUT", 15),
    "search_cache_ttl": _float("FNMUSIC_SEARCH_CACHE_TTL", 604800),
    # 空结果只用很短的 TTL。上游一次抖动/过滤全灭就会把 items=[] 写进缓存，
    # 若沿用 7 天 TTL，该关键词在整个周期内都只会返回本地结果——即使上游早已恢复。
    # 这条曾让"搜不到任何网易云歌曲"在修好根因后依然持续存在。
    "search_empty_ttl": _float("FNMUSIC_SEARCH_EMPTY_TTL", 60),
    "late_page_wait_s": _float("FNMUSIC_LATE_PAGE_WAIT_S", 5.0),
    "daily_enabled": _flag("FNMUSIC_DAILY_ENABLED", "true"),
    "daily_limit": _int("FNMUSIC_DAILY_LIMIT", dailyrec.PLAYLIST_SIZE),
    "pushplus_enabled": pushplus.enabled(),
    "fav_dir": os.environ.get(
        "FNMUSIC_FAV_DIR", os.path.join(_HOME, "online_favorites")
    ),
}

_REDACT_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password")

# 唯一在线音源标识，贯穿 guid（online:netease:<song_id>）与各拦截分支
NETEASE_SOURCE = netease_items.SOURCE_NAME

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

CACHE_EXTS = ("mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "ape", "wv", "dsf", "dff", "tta")

# 飞牛 Kl() 归一化：mpeg/mp3→mp3，wav/pcm→wav，m4a/aac/mp4→m4a，其余小写原样（flac/ogg/ape/wv…）
_FORMAT_ALIASES = {
    "mp3": "mp3",
    "mpeg": "mp3",
    "mpga": "mp3",
    "flac": "flac",
    "wav": "wav",
    "wave": "wav",
    "pcm": "wav",
    "lpcm": "wav",
    "ogg": "ogg",
    "vorbis": "ogg",
    "opus": "opus",
    "m4a": "m4a",
    "mp4": "m4a",
    "mp4a": "m4a",
    "aac": "m4a",
    "alac": "m4a",
    "ape": "ape",
    "wv": "wv",
    "wavpack": "wv",
    "dsf": "dsf",
    "dff": "dff",
    "dsd": "dsd",
    "tta": "tta",
    "tak": "tak",
    "wma": "wma",
    "aiff": "aiff",
    "aif": "aiff",
}


# 模块级搜索缓存
_SEARCH_CACHE: dict[str, dict] = {}


def _clean_search_cache() -> None:
    """清理过期缓存，若仍超过容量上限（2000条），按 ts 升序淘汰最旧的一半。"""
    now = time.time()
    ttl = CONF.get("search_cache_ttl", 604800.0)
    expired_keys = [k for k, v in _SEARCH_CACHE.items() if now - v.get("ts", 0) >= ttl]
    for k in expired_keys:
        _SEARCH_CACHE.pop(k, None)
    if len(_SEARCH_CACHE) > 2000:
        sorted_keys = sorted(_SEARCH_CACHE.keys(), key=lambda k: _SEARCH_CACHE[k].get("ts", 0))
        to_remove = sorted_keys[: len(sorted_keys) // 2]
        for k in to_remove:
            _SEARCH_CACHE.pop(k, None)


def _set_search_cache(keyword: str, entry: dict) -> None:
    _clean_search_cache()
    _SEARCH_CACHE[keyword] = entry


ONLINE_TRIAL_MARKERS = (
    "(试听)",
    "（试听）",
    "试听片段",
    "片段试听",
    "试听版",
    "[试听]",
    "【试听】",
    "- 试听",
    " - 试听",
)


def _has_trial_fragment(item: dict) -> bool:
    """条目是否只是「试听片段」。

    不能用 ``item.get("freeTrialInfo") or item.get("freeTrialPrivilege")`` 这种写法：
    ``freeTrialPrivilege`` 是网易云**每条 song/url 响应都必带**的标准结构体，
    正常曲目也一定存在且是非空 dict（真理值）。用它做真值判断会把**每一首歌**都判成
    试听而剔除，与是否登录、是否 VIP 完全无关，表现为「搜不到任何在线歌曲」。
    真正的信号在这个结构体**内部的布尔位**：``resConsumable`` / ``userConsumable``
    为 True 才表示正在消耗试听额度。``freeTrialInfo`` 语义正好相反：None 表示无试听，
    **只有确实是试听曲目才带非空内容**（形如 ``{"st": 起始秒, "et": 结束秒}``），
    因此对它做存在性判断是安全的。两者混在一起做真值判断正是本 bug 的成因。
    """
    def flag(d, k):
        v = d.get(k) if isinstance(d, dict) else None
        return v is True or str(v).lower() == "true"

    priv = item.get("freeTrialPrivilege")
    if flag(priv, "resConsumable") or flag(priv, "userConsumable"):
        return True
    return bool(item.get("freeTrialInfo"))


def is_playable_online_track(item: dict, require_id: bool = False) -> bool:
    """最终防线校验：过滤无音频流或试听标记的不可播曲目。"""
    if not isinstance(item, dict):
        return False

    title = str(item.get("title") or item.get("name") or item.get("song_name") or "").strip()
    if not title:
        return False

    if require_id:
        sid = str(item.get("id") or item.get("song_id") or item.get("guid") or "").strip()
        if not sid:
            return False

    # 1. 标题含试听标记
    if any(marker in title for marker in ONLINE_TRIAL_MARKERS):
        return False

    # 2. 字段试听标记
    if item.get("is_trial") is True or _has_trial_fragment(item):
        return False
    if int(item.get("is_free_part") or 0) != 0 or int(item.get("fail_process") or 0) == 4:
        return False

    # 3. 收费/VIP 拦截
    if int(item.get("pay_type") or 0) != 0:
        return False
    if int(item.get("pkg_price") or 0) != 0 or int(item.get("price") or 0) != 0:
        return False
    fee = item.get("fee")
    if fee is not None:
        try:
            if int(fee) not in (0, 8):
                return False
        except (ValueError, TypeError):
            pass

    # 4. 显式不可播/无流标记
    if item.get("unplayable") is True or item.get("playable") is False:
        return False
    if item.get("has_stream") is False:
        return False

    # 5. 音频流直链校验：若带有 download_url 或 url 键，则必须合法可用，绝不能是空串或 404
    if "download_url" in item:
        d_url = str(item.get("download_url") or "").strip()
        if not d_url or not d_url.startswith(("http://", "https://")) or "404/error.html" in d_url or "error.html" in d_url:
            return False
    if "url" in item:
        u = str(item.get("url") or "").strip()
        if not u or "404/error.html" in u or "error.html" in u:
            return False

    # 6. 片段时长校验（<=35s 且带有试听迹象）
    duration = item.get("duration_s") or (item.get("duration") or 0)
    try:
        duration_s = float(duration)
        if 0 < duration_s <= 35 and ("试听" in title or item.get("is_trial")):
            return False
    except (ValueError, TypeError):
        pass

    return True


def deduplicate_online_items(items: list[dict]) -> list[dict]:
    """在线条目合并去重：按 (title, artist) 小写，保留最先出现的（musicbox 优先）。同时做可播校验。"""
    seen = set()
    res = []
    for it in items:
        if not is_playable_online_track(it):
            continue
        t = str(it.get("title") or it.get("name") or "").strip().lower()
        a = str(it.get("artist") or "").strip().lower()
        if t and a:
            key = (t, a)
            if key in seen:
                continue
            seen.add(key)
        res.append(it)
    return res


def play_format_from_ext(ext: str | None) -> str:
    raw = (ext or "mp3").strip().lower().lstrip(".")
    if raw.startswith("audio/"):
        raw = raw.split("/", 1)[-1]
    return _FORMAT_ALIASES.get(raw, raw or "mp3")


def filter_headers(headers: Any, exclude_keys: set | None = None) -> dict:
    exclude = HOP_BY_HOP | {k.lower() for k in (exclude_keys or set())}
    return {k: v for k, v in headers.items() if k.lower() not in exclude}


def copy_incoming_headers(request: Request) -> dict:
    """透传鉴权 Cookie / Token。Starlette 头名为小写，需显式回填以免丢失 music-token。

    ``host`` 必须一并透传：官方后端在转码/HLS 链路里会用请求 Host 拼装
    绝对地址（m3u8 里的分片 URL 等）。此前把它剥掉后 httpx 会发
    ``Host: unix``，后端拼出来的地址客户端根本连不上——表现为
    「开转码后本地音乐播不了」（不开转码的 /track/stream 走相对路径，
    不受影响，所以平时看不出来）。
    """
    headers = filter_headers(request.headers, exclude_keys={"content-length"})
    headers["accept-encoding"] = "identity"
    for key in ("cookie", "authorization", "x-trim-music-temp-token", "host"):
        val = request.headers.get(key)
        if val:
            headers[key] = val
    return headers


def get_by_path(d: Any, path: str) -> Any:
    curr = d
    for p in path.split("."):
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return None
    return curr


def set_by_path(d: dict, path: str, val: Any):
    parts = path.split(".")
    curr = d
    for p in parts[:-1]:
        if p not in curr or not isinstance(curr[p], dict):
            curr[p] = {}
        curr = curr[p]
    curr[parts[-1]] = val


def extract_keyword(request: Request) -> str:
    """前端打包用 q，部分调用/验收用 keyword。"""
    params = request.query_params
    return (params.get("keyword") or params.get("q") or params.get("query") or "").strip()


def online_guid_from_item(item: dict) -> str:
    raw_id = str(item.get("id") or "")
    src = str(item.get("source") or "")
    if raw_id.startswith("online:"):
        return raw_id
    if ":" in raw_id:
        return f"online:{raw_id}"
    return f"online:{src}:{raw_id}"


def song_id_from_online_guid(guid: str) -> str:
    if guid.startswith("online:"):
        return guid[len("online:") :]
    return guid


def is_online_guid(guid: str) -> bool:
    return bool(guid) and guid.startswith("online:")


def source_from_online_guid(guid: str) -> str:
    parts = (guid or "").split(":")
    return parts[1] if len(parts) >= 3 else ""


def build_online_track(item: dict) -> dict:
    """对齐飞牛前端 ZQ 解构 / _h() 期望：artists、album 对象、genres 数组、audioSpec、duration 毫秒。"""
    guid = online_guid_from_item(item)
    src = str(item.get("source") or source_from_online_guid(guid) or "")
    title = str(item.get("title") or item.get("name") or "")
    artist = str(item.get("artist") or "")
    album = str(item.get("album") or "")
    duration_s = item.get("duration_s") or 0
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0
    duration_ms = int(duration_s * 1000)
    ext = str(item.get("ext") or "mp3") or "mp3"
    play_format = play_format_from_ext(ext)
    file_size = item.get("file_size") or 0
    try:
        file_size = int(file_size or 0)
    except (TypeError, ValueError):
        file_size = 0
    cover = str(item.get("cover_url") or "")
    # 路径带真实后缀，飞牛 ll() 用 path 解析 extension；封面走 guid 以便 /static/cover 拦截
    spec_path = f"online/{src}/{guid}.{play_format}"

    artists_list = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
    album_obj = {
        "name": album,
        "guid": f"{guid}:album",
        "artists": artists_list,
        "coverId": guid,
    }
    audio_spec = {
        "path": spec_path,
        "format": play_format,
        "codec": play_format,
        "container": play_format,
        "duration": duration_ms,
        "size": file_size,
        "channel": 2,
        "sampleRate": 44100,
        "bitDepth": 16 if play_format in ("wav", "flac", "aiff") else None,
        "bitrate": 1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000,
    }
    audio_spec = {k: v for k, v in audio_spec.items() if v is not None}

    return {
        "guid": guid,
        "id": guid,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artists_list,
        "album": album_obj,
        "albumName": album,
        "audioSpec": audio_spec,
        "duration": duration_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration_s": duration_s,
        "codec": play_format,
        "codecName": play_format,
        "format": play_format,
        "ext": ext,
        "size": file_size,
        "file_size": file_size,
        "coverId": guid,
        "cover_url": cover,
        "coverUrl": cover,
        "coverURL": cover,
        "source": src,
        "is_online": True,
        "isFavorite": False,
        "isCue": False,
        "hasLyric": bool(item.get("lyric")),
        "genres": [],
        "accessStatus": 0,
    }


def artist_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    a = item.get("artist") or item.get("singer") or item.get("singers") or ""
    if isinstance(a, list):
        names = []
        for x in a:
            if isinstance(x, dict):
                names.append(str(x.get("name") or ""))
            else:
                names.append(str(x))
        return " ".join(n for n in names if n).strip().lower()
    if isinstance(a, dict):
        return str(a.get("name") or "").strip().lower()
    return str(a).strip().lower()


def title_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("title") or item.get("name") or "").strip().lower()


def should_cache(range_header: str | None) -> bool:
    """完整拉取才落盘：无 Range，或 bytes=0-（开区间）。Safari bytes=0-1 探测不落盘。"""
    if not range_header:
        return True
    r = range_header.strip().lower()
    return bool(re.match(r"^bytes=0-$", r))


def is_range_from_zero_or_none(range_header: str | None) -> bool:
    return should_cache(range_header)


def cache_safe_guid(guid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)


def online_file_id(guid: str) -> str:
    """online:migu:600929… → 600929…，仅用于查找旧文件，不再写进文件名。"""
    return song_id_from_online_guid(guid).rsplit(":", 1)[-1]


def safe_basename_title(title: str) -> str:
    t = re.sub(r'[/\\:\0]', "_", (title or "").strip()) or "unknown"
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:120]


def library_basename(title: str, artist: str = "") -> str:
    """曲库文件名：歌手 - 歌名（无源站 id）。飞牛无标签时会用文件名当标题。"""
    title_s = safe_basename_title(title)
    artist_s = safe_basename_title(artist) if (artist or "").strip() else ""
    if artist_s and artist_s.lower() != title_s.lower() and artist_s != "unknown":
        return f"{artist_s} - {title_s}"
    return title_s


def media_ref_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.ref")


def _path_stem(path: str) -> str:
    root, ext = os.path.splitext(path)
    known = set(CACHE_EXTS) | {"lrc", "part"}
    if ext.lstrip(".").lower() in known:
        return root
    return path


def remember_media_path(guid: str, media_path: str) -> None:
    """记住曲库里的文件词干（不含扩展名），音频和 .lrc 共用。"""
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        with open(media_ref_path(guid), "w", encoding="utf-8") as f:
            f.write(_path_stem(media_path))
    except Exception as e:
        logger.warning("Failed to remember media path for %s: %s", guid, e)


def remember_archive_path(guid: str, media_path: str) -> None:
    """登记**归档**文件（收藏下载），与边播边存的 tee 缓存分用两个 ref 命名空间。

    必须分开：``remember_media_path`` 每个 guid 只有一个 ``.ref``，归档目录与曲库缓存
    目录是两个不同位置，复用同一个 ref 会互相覆盖，卸载时就漏删其中一边。
    存的是**词干**（不含扩展名），这样卸载脚本按 ``${stem}.{ext}`` 逐个精确删除的老
    逻辑对归档同样适用（含 ``.lrc``）。
    """
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        ref = os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.archive.ref")
        with open(ref, "w", encoding="utf-8") as f:
            f.write(_path_stem(media_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to remember archive path for %s: %s: %s",
                       guid, type(exc).__name__, exc)


def recalled_media_stem(guid: str) -> str | None:
    ref = media_ref_path(guid)
    if not os.path.exists(ref):
        return None
    try:
        with open(ref, encoding="utf-8") as f:
            stem = _path_stem(f.read().strip())
        if stem:
            return stem
    except Exception:
        return None
    return None


def recalled_media_path(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if not stem:
        return None
    for ext in CACHE_EXTS:
        path = f"{stem}.{ext}"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def unique_library_path(directory: str, basename: str, ext: str) -> str:
    dest = os.path.join(directory, f"{basename}.{ext}")
    if not os.path.exists(dest):
        return dest
    n = 2
    while os.path.exists(os.path.join(directory, f"{basename} ({n}).{ext}")):
        n += 1
    return os.path.join(directory, f"{basename} ({n}).{ext}")


def write_audio_tags(path: str, title: str, artist: str = "", album: str = "") -> None:
    """写入 title/artist/album，飞牛扫描后用标签而不是文件名显示。"""
    title, artist, album = (title or "").strip(), (artist or "").strip(), (album or "").strip()
    if not title and not artist:
        return
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        if getattr(audio, "tags", None) is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
    except Exception as e:
        logger.warning("Failed to write audio tags for %s: %s", path, e)


def _db_library_dirs() -> "list[str]":
    """从飞牛 music.db 的 shared_library 表读曲库目录（纯候选，不做任何回退）。"""
    db = resolve_music_db()
    if not db or not os.path.exists(db):
        logger.warning("曲库目录无法确定：music.db 不存在（%s），且未配置 FNMUSIC_LIBRARY_DIR", db)
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()
        finally:
            con.close()
    except Exception as e:
        logger.warning("Failed to read shared_library path: %s", e)
        return []
    out = [str(p) for p, in rows if p and os.path.isdir(str(p))]
    if not out:
        logger.warning("music.db 里没有可用的共享库路径（shared_library 行数=%d）: %s",
                       len(rows), db)
    return out


def _authorized_dirs() -> "list[str]":
    """飞牛**正式授权**给本应用的目录（开放网关 + 兼容环境变量）。

    这是 v2.9.4 的核心改动：以前靠 root 硬读 /vol1/...，既不合飞牛规范，
    也让管理员在应用设置里看不到「授权目录」入口、没法合规授权。现在改成
    先问网关「我被授权了哪些目录」，再用这些目录当曲库来源。
    """
    try:
        rep = trimgw.authorized_report()
    except Exception as exc:  # noqa: BLE001 - 查授权失败绝不能拖垮主流程
        logger.warning("查询飞牛授权目录失败: %s: %s", type(exc).__name__, exc)
        return []
    paths = rep.get("shared_paths") or []
    if not paths:
        logger.warning("飞牛尚未给 %s 授权任何目录：%s",
                       trimgw.app_name(), rep.get("hint") or rep.get("shared_error") or "")
    return list(paths)


def _strict_authorization() -> bool:
    """true = 只扫已授权目录，绝不依赖 root 直读未授权路径（合规最严档）。"""
    return (os.environ.get("FNMUSIC_STRICT_AUTHORIZATION") or "false").strip().lower() in (
        "true", "1", "yes", "on")


def library_authorization_state(path: str) -> dict:
    """判断一个曲库目录当前是否处在飞牛授权范围内（供诊断/日志使用）。"""
    try:
        rep = trimgw.authorized_report()
    except Exception as exc:  # noqa: BLE001
        return {"authorized": False, "paths": [], "hint": f"查询失败: {exc}"}
    paths = rep.get("shared_paths") or []
    covered = bool(path) and any(
        os.path.abspath(path) == os.path.abspath(a)
        or os.path.abspath(path).startswith(os.path.abspath(a).rstrip(os.sep) + os.sep)
        for a in paths
    )
    return {
        "authorized": covered,
        "paths": paths,
        "hint": rep.get("hint") or "",
        "error": rep.get("shared_error") or "",
    }


def detect_library_dir() -> str:
    """定位本地曲库目录。

    优先级：**管理页显式配置** → **飞牛正式授权目录**（v2.9.4 新增，合规范做法）
    → music.db 的 shared_library → 最后才回退到 cache_dir。

    ⚠️ 回退到 cache_dir 是「静默失效」的根源：那里一首歌都没有，于是本地曲库
    优先、本地每日推荐全都表现成「功能没开」而不是「路径错了」。所以每次回退
    都要把原因和探测到的东西写进日志。
    """
    explicit = str(CONF.get("library_dir") or "").strip()
    if explicit:
        if not os.path.isdir(explicit):
            logger.warning("FNMUSIC_LIBRARY_DIR 配置了但目录不存在: %s", explicit)
        else:
            st = library_authorization_state(explicit)
            if not st["authorized"]:
                logger.warning(
                    "曲库目录 %s 不在飞牛授权范围内（当前靠 root 直读）。"
                    "建议到「应用设置 → 授权目录」把它授权给本应用：%s",
                    explicit, st.get("hint") or "尚未授权任何目录",
                )
        return explicit

    authorized = _authorized_dirs()
    if authorized:
        db_dirs = _db_library_dirs()
        picked = trimgw.pick_library_from_authorized(db_dirs, authorized)
        if picked:
            logger.info("曲库目录取自飞牛已授权目录: %s", picked)
            return picked
        # music.db 没给可用路径，或它给的路径没被授权：直接用第一个授权目录。
        # 授权目录是管理员显式指定的音乐目录，比缓存目录可靠得多。
        logger.info("曲库目录使用飞牛授权目录（music.db 未提供可用路径）: %s", authorized[0])
        return authorized[0]

    db_dirs = _db_library_dirs()
    if db_dirs:
        if _strict_authorization():
            logger.warning("FNMUSIC_STRICT_AUTHORIZATION=true，拒绝使用未授权目录 %s", db_dirs[0])
        else:
            logger.warning("飞牛未授权任何目录，暂按 music.db 路径 %s 读取（建议到应用设置授权）",
                           db_dirs[0])
            return db_dirs[0]

    logger.warning("曲库目录无法确定：既没有飞牛授权目录，music.db 也不可用，"
                   "且未配置 FNMUSIC_LIBRARY_DIR；回退到 %s（空目录，本地每日推荐不会出歌）",
                   CONF["cache_dir"])
    return CONF["cache_dir"]


# ---------------------------------------------------------------------------
# music.db 自动定位（v2.8.1）
#
# 真机诊断铁证：「music.db 扫描 available=false / music.db 不存在或未能打开」——
# 默认路径 /usr/local/apps/@appdata/trim.music/db/music.db 在该机器上不存在
# （飞牛把应用数据放 /vol*/@appdata 下，不同版本布局不同）。后果远不止
# 「跟随飞牛」读不到偏好：**本地曲库优先的索引也建立在空库上**，功能静默失效
# ——又是那种"猜路径猜错不会报错、只会永远不生效"的坑。
#
# 解析顺序：FNMUSIC_MUSIC_DB 显式配置（存在才用）→ 常见布局探测。结果按
# 「显式值」为键缓存（测试会换 CONF["music_db"]，键变了自动重查）。
# ---------------------------------------------------------------------------

_MUSIC_DB_RESOLVED: dict[str, str] = {}


def _music_db_candidates() -> "list[str]":
    """music.db 的候选路径（按优先级，去重保序）。

    飞牛各版本把应用数据放在 /vol*/@appdata 下，目录层级并不统一（有的多一层
    data/、有的在 @appcenter）。只在 trim.music 自己的目录里递归找，不去扫
    音乐库那种大盘——递归 glob 落在 /vol*/@appdata/trim.music/ 下是安全的。
    """
    explicit = str(CONF.get("music_db") or "").strip()
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    cands.append("/usr/local/apps/@appdata/trim.music/db/music.db")
    for pat in (
        "/vol*/@appdata/trim.music/db/music.db",
        "/vol*/@appdata/trim.music/*/db/music.db",
        "/vol*/@appcenter/trim.music/db/music.db",
        "/vol*/@appdata/trim.music/**/music.db",
        "/usr/local/apps/@appdata/trim.music/**/music.db",
    ):
        try:
            cands.extend(sorted(glob.glob(pat, recursive=True)))
        except Exception:  # noqa: BLE001 - 单个 glob 表达式异常不该影响整体
            continue
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_music_db() -> str:
    explicit = str(CONF.get("music_db") or "").strip()
    cached = _MUSIC_DB_RESOLVED.get(explicit)
    if cached is not None:
        return cached

    candidates = _music_db_candidates()

    for cand in candidates:
        if cand and os.path.isfile(cand):
            _MUSIC_DB_RESOLVED[explicit] = cand
            if cand != explicit:
                logger.info("music.db 自动定位成功: %s（显式配置=%r）", cand, explicit or "未配置")
            return cand

    resolved = explicit or (candidates[1] if len(candidates) > 1 else "")
    _MUSIC_DB_RESOLVED[explicit] = resolved
    if not os.path.isfile(resolved):
        # 逐个列出候选与存在性：真机上「猜路径猜错」是本地曲库类功能静默失效的
        # 头号原因，没有这份清单就只能靠用户去 SSH 上 ls。
        logger.warning(
            "music.db 未找到（本地曲库优先/本地每日推荐/跟随飞牛 均不可用）。已探测: %s",
            "; ".join(f"{c}{'[存在]' if os.path.isfile(c) else '[缺失]'}" for c in candidates[:12]) or "无",
        )
    return resolved


def music_db_probe() -> dict:
    """供诊断页使用：把 music.db 的解析结果与探测明细摊开。"""
    explicit = str(CONF.get("music_db") or "").strip()
    cands = _music_db_candidates()
    resolved = resolve_music_db()
    rows: list[str] = []
    read_error = ""
    if os.path.isfile(resolved):
        try:
            con = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
            try:
                rows = [str(r[0]) for r in
                        con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()]
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            read_error = f"{type(exc).__name__}: {exc}"[:160]
    return {
        "explicit": explicit,
        "resolved": resolved,
        "exists": os.path.isfile(resolved),
        "read_error": read_error,
        "shared_library": rows[:10],
        "probed": [{"path": c, "exists": os.path.isfile(c)} for c in cands[:20]],
    }


def reset_music_db_cache_for_test() -> None:
    _MUSIC_DB_RESOLVED.clear()


def iter_media_dirs() -> list[str]:
    dirs: list[str] = []
    lib = detect_library_dir()
    for d in (lib, CONF["cache_dir"]):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def adopt_library_perms(path: str) -> None:
    try:
        parent = os.path.dirname(path) or "."
        st = os.stat(parent)
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o644)
    except Exception:
        pass


def find_cache_file(guid: str) -> str | None:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    file_id = online_file_id(guid)
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        for ext in CACHE_EXTS:
            exact = os.path.join(d, f"{safe}.{ext}")
            if os.path.exists(exact) and os.path.getsize(exact) > 0:
                return exact
            pattern = os.path.join(d, f"* - {glob.escape(file_id)}.{ext}")
            for path in glob.glob(pattern):
                if os.path.getsize(path) > 0:
                    return path
    return None


def promote_cache_hit(guid: str, audio_path: str) -> str:
    """旧 cache/ 音频：若曲库已有对应文件或歌词，则对齐过去。"""
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    lib = detect_library_dir()
    try:
        if os.path.abspath(os.path.dirname(audio_path)) == os.path.abspath(lib):
            remember_media_path(guid, audio_path)
            return audio_path
    except Exception:
        return audio_path
    file_id = online_file_id(guid)
    ext = os.path.splitext(audio_path)[1] or ".mp3"
    dest = None
    if os.path.isdir(lib):
        for lrc in glob.glob(os.path.join(lib, f"* - {glob.escape(file_id)}.lrc")):
            dest = os.path.splitext(lrc)[0] + ext
            break
    if not dest:
        return audio_path
    if not os.path.exists(dest):
        try:
            os.makedirs(lib, exist_ok=True)
            shutil.copy2(audio_path, dest)
            adopt_library_perms(dest)
        except Exception as e:
            logger.warning("Failed to promote cache audio into library: %s", e)
            return audio_path
    remember_media_path(guid, dest)
    return dest


def library_media_path(guid: str, title: str, ext: str, artist: str = "") -> str:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    stem = recalled_media_stem(guid)
    if stem:
        return f"{stem}.{ext}"
    lib = detect_library_dir()
    file_id = online_file_id(guid)
    if os.path.isdir(lib):
        for path in glob.glob(os.path.join(lib, f"* - {glob.escape(file_id)}.{ext}")):
            if os.path.getsize(path) > 0:
                return path
    os.makedirs(lib, exist_ok=True)
    return unique_library_path(lib, library_basename(title, artist), ext)


def find_lyric_file(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if stem:
        sibling = f"{stem}.lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    audio = find_cache_file(guid)
    if audio:
        sibling = os.path.splitext(audio)[0] + ".lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    file_id = online_file_id(guid)
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        exact = os.path.join(d, f"{safe}.lrc")
        if os.path.exists(exact) and os.path.getsize(exact) > 0:
            return exact
        pattern = os.path.join(d, f"* - {glob.escape(file_id)}.lrc")
        for path in glob.glob(pattern):
            if os.path.getsize(path) > 0:
                return path
    return None


def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
    found = find_lyric_file(guid)
    if found:
        return found
    audio = find_cache_file(guid)
    if audio:
        return os.path.splitext(audio)[0] + ".lrc"
    d = detect_library_dir()
    os.makedirs(d, exist_ok=True)
    if (title or "").strip() or (artist or "").strip():
        return os.path.join(d, f"{library_basename(title, artist)}.lrc")
    return os.path.join(d, f"{cache_safe_guid(guid)}.lrc")


def read_lyric_cache(guid: str) -> str:
    path = find_lyric_file(guid)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning("Failed to read lyric cache %s: %s", path, e)
        return ""


def write_lyric_cache(guid: str, text: str, title: str = "", artist: str = "") -> None:
    text = (text or "").strip()
    if not text:
        return
    if text == read_lyric_cache(guid):
        return
    path = lyric_cache_path(guid, title=title, artist=artist)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        with open(part_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_media_path(guid, path)
    except Exception as e:
        logger.warning("Failed to write lyric cache %s: %s", path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass


async def resolve_online_lyric(request: Request, guid: str) -> str:
    """本地 .lrc 优先；没有再向网易云要，拿到就落盘。"""
    cached = read_lyric_cache(guid)
    if cached:
        return cached

    musicbox_client = get_musicbox_client(request.app)
    song_id = song_id_from_online_guid(guid).split(":")[-1]
    if not song_id:
        return ""
    try:
        r = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
        if r.status_code == 200:
            res_data = r.json()
            if isinstance(res_data, dict) and res_data.get("ok") is not False:
                l_data = res_data.get("data")
                if isinstance(l_data, dict):
                    lyric_text = str(l_data.get("lyric") or "").strip()
                    if lyric_text:
                        info = await _online_info(request, guid)
                        write_lyric_cache(
                            guid,
                            lyric_text,
                            title=str((info or {}).get("title") or ""),
                            artist=str((info or {}).get("artist") or ""),
                        )
                        return lyric_text
    except Exception as e:
        logger.warning("musicbox lyric fetch failed for %s: %s", guid, e)
    return ""


def media_type_for_ext(ext: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ape": "audio/x-ape",
        "wv": "audio/x-wavpack",
        "dsf": "audio/x-dsd",
        "dff": "audio/x-dff",
        "tta": "audio/x-tta",
        "wma": "audio/x-ms-wma",
        "aiff": "audio/aiff",
    }.get(ext.lower(), "application/octet-stream")


def ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "flac" in ct:
        return "flac"
    if "wavpack" in ct or "x-wv" in ct:
        return "wv"
    if "wav" in ct or "wave" in ct:
        return "wav"
    if "opus" in ct:
        return "opus"
    if "ogg" in ct:
        return "ogg"
    if "ape" in ct:
        return "ape"
    if "aiff" in ct:
        return "aiff"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    if "aac" in ct:
        return "aac"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    return play_format_from_ext(ct.split("/")[-1] if "/" in ct else "mp3")


def parse_http_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip(), re.I)
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        suffix = int(end_s)
        start = max(file_size - suffix, 0)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    if start < 0 or start >= file_size or start > end:
        return None
    return start, end


def serve_file_with_range(path: str, range_header: str | None, media_type: str) -> Response:
    file_size = os.path.getsize(path)
    rng = parse_http_range(range_header, file_size)

    def iter_file(offset: int, length: int) -> AsyncGenerator[bytes, None]:
        async def gen() -> AsyncGenerator[bytes, None]:
            remaining = length
            with open(path, "rb") as fp:
                fp.seek(offset)
                while remaining > 0:
                    chunk = fp.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return gen()

    if rng is None:
        return StreamingResponse(
            iter_file(0, file_size),
            status_code=200,
            headers={
                "Content-Type": media_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        iter_file(start, length),
        status_code=206,
        headers={
            "Content-Type": media_type,
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
        },
    )


def get_upstream_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "upstream_client", None)
    if client is None:
        transport = httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"])
        client = httpx.AsyncClient(transport=transport, base_url="http://unix", timeout=30.0)
        fastapi_app.state.upstream_client = client
    return client


def get_musicbox_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicbox_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicbox_url"], timeout=20.0)
        fastapi_app.state.musicbox_client = client
    return client


def get_push_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "push_client", None)
    if client is None:
        client = httpx.AsyncClient(timeout=pushplus.REQUEST_TIMEOUT_S)
        fastapi_app.state.push_client = client
    return client


async def forward_to_upstream(
    request: Request,
    client: httpx.AsyncClient,
    timeout: float | None = None,
    label: str = "",
) -> Response:
    """把请求原样转给官方后端（流式）。

    - ``timeout`` 允许调用方为慢端点放宽读超时（默认沿用客户端的 30s）。
      转码链路必须放宽：官方后端收到 /track/transcode 后要等 ffmpeg 产出
      首个 HLS 分片才应答，NAS 磁盘慢或大文件（DSD/APE/FLAC）时 30s 根本
      不够——超时异常会把「能播」变成 500，用户看到的就是开了转码本地歌
      全部播放失败。
    - ``label`` 非空时把上游状态码与耗时记进日志（本地转码排障关键：
      客户端只请求一次 m3u8、失败后直接放弃，不留下任何线索）。
    - 上游未压缩（我们强制 accept-encoding: identity）时**保留 content-length**，
      让转发应答与官方直连逐字节等价，不给挑剔的播放器留差异。
    """
    started = time.monotonic()
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"

    headers = copy_incoming_headers(request)
    body = await request.body()

    extensions = None
    if timeout is not None:
        extensions = {"timeout": httpx.Timeout(connect=10.0, read=timeout, write=30.0,
                                               pool=30.0).as_dict()}
    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
        extensions=extensions,
    )
    try:
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        logger.warning("%supstream timeout %s %s: %s", f"[{label}] " if label else "",
                       request.method, request.url.path, type(exc).__name__)
        return JSONResponse(status_code=504, content={"code": 504, "msg": "upstream timeout",
                                                      "data": None})
    except httpx.HTTPError as exc:
        logger.warning("%supstream forward failed %s %s: %s: %s", f"[{label}] " if label else "",
                       request.method, request.url.path, type(exc).__name__, exc)
        return JSONResponse(status_code=502, content={"code": 502, "msg": "upstream unavailable",
                                                      "data": None})
    if label:
        ms = (time.monotonic() - started) * 1000.0
        line = ("%s %s %s -> %s %.0fms ct=%s",
                label, request.method, request.url.path, resp.status_code, ms,
                resp.headers.get("content-type") or "-")
        if resp.status_code >= 400:
            logger.warning(*line)
        else:
            logger.info(*line)
    exclude = {"content-encoding"}
    if resp.headers.get("content-encoding"):
        # 上游还是压缩了（罕见）：解压后长度必变，content-length 只能丢
        exclude.add("content-length")
    resp_headers = filter_headers(resp.headers, exclude_keys=exclude)

    async def body_stream() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=resp_headers,
    )


async def forward_buffered(
    request: Request,
    client: httpx.AsyncClient,
    timeout: float | None = None,
    label: str = "",
    body_sniff: int = 0,
) -> Response:
    """转发并**整包缓冲**应答（m3u8 / 转码会话这类小应答专用）。

    与流式转发的差别：
    1. 应答体在日志里留证（状态码、耗时、content-type，以及可选的响应体开头
       ``body_sniff`` 字节）——本地转码排障全靠它：客户端对 m3u8 只请求一次、
       失败即放弃，不留证就永远不知道官方后端到底回了什么。
    2. 原样保留 content-length 回给客户端，与官方直连逐字节等价。
    """
    started = time.monotonic()
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"

    headers = copy_incoming_headers(request)
    body = await request.body()

    extensions = None
    if timeout is not None:
        extensions = {"timeout": httpx.Timeout(connect=10.0, read=timeout, write=30.0,
                                               pool=30.0).as_dict()}
    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
        extensions=extensions,
    )
    try:
        resp = await client.send(req)
    except httpx.TimeoutException as exc:
        logger.warning("%supstream timeout %s %s: %s", f"[{label}] " if label else "",
                       request.method, request.url.path, type(exc).__name__)
        return JSONResponse(status_code=504, content={"code": 504, "msg": "upstream timeout",
                                                      "data": None})
    except httpx.HTTPError as exc:
        logger.warning("%supstream forward failed %s %s: %s: %s", f"[{label}] " if label else "",
                       request.method, request.url.path, type(exc).__name__, exc)
        return JSONResponse(status_code=502, content={"code": 502, "msg": "upstream unavailable",
                                                      "data": None})

    content = resp.content
    ms = (time.monotonic() - started) * 1000.0
    sniff = ""
    if body_sniff:
        sniff = " body[:%d]=%r" % (body_sniff, content[:body_sniff])
    log_args = ("%s %s %s -> %s %.0fms %dB ct=%s%s",
                label or "forward", request.method, request.url.path, resp.status_code, ms,
                len(content), resp.headers.get("content-type") or "-", sniff)
    if resp.status_code >= 400:
        logger.warning(*log_args)
    else:
        logger.info(*log_args)

    resp_headers = filter_headers(resp.headers, exclude_keys={"content-encoding", "content-type"})
    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp.headers.get("content-type"),
    )


async def fetch_upstream_envelope(request: Request, client: httpx.AsyncClient) -> Response | dict:
    """透传上游并解析 JSON 信封。失败时返回 Response，成功返回 dict。"""
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)
    body = await request.body()
    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    if not isinstance(payload, dict):
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    payload["_ext_headers"] = resp_headers
    return payload


async def fetch_netease_search(
    client: httpx.AsyncClient,
    keyword: str,
    limit: int,
    *,
    ids: "list[str] | None" = None,
    enrich: bool = True,
) -> list[dict] | None:
    """网易云聚合搜索（唯一在线音源）。

    先取搜索结果，再用 /api/v1/songs/detail 批量补齐封面与无损判定
    （``enrich=False`` 时跳过补齐，用于只要标题的联想词场景）。
    musicbox 服务端已按当前登录账号的真实可播状态过滤，这里只做可播标志复核。
    """
    if not keyword:
        return None
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": limit, "type": "song"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("data")
        if not isinstance(raw_list, list):
            return None

        items: list[dict] = []
        song_ids: list[str] = []
        for raw in raw_list:
            if not is_playable_online_track(raw):
                continue
            item = netease_items.map_netease_song(raw)
            if item is None:
                continue
            items.append(item)
            song_ids.append(netease_items.song_id_of(raw))

        if song_ids and ids is not None:
            ids.extend(song_ids)
        if items and enrich:
            await _enrich_netease_items(client, items)
        return [it for it in items if is_playable_online_track(it)]
    except Exception as e:
        logger.warning("Failed to fetch netease search: %s", e)
        return None


async def _enrich_netease_items(client: httpx.AsyncClient, items: list[dict]) -> None:
    """批量补齐封面 / 无损判定。失败只记日志，不影响搜索结果返回。"""
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
        logger.warning("Failed to fetch songs detail: %s: %s", type(e).__name__, e)
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
        detail = detail_map.get(str(item.get("id") or "").split(":", 1)[-1])
        if detail:
            netease_items.apply_song_detail(item, detail)



# ---------------------------------------------------------------------------
# 播放直链短缓存（v2.7）：网易云 CDN 直链自获取起约有 20 分钟有效期，
# 同一首歌短时间内重复播放（上一首/下一首切回、seek 重连、多端同曲）时
# 完全可以复用，省掉 resolve 的两次上游往返，也减少与歌单预热在
# musicbox 全局锁上的争抢。默认 600s，0 = 关闭。
# ---------------------------------------------------------------------------
_URL_CACHE_TTL = _float("FNMUSIC_URL_CACHE_TTL", 600.0)
_URL_CACHE_MAX = int(os.environ.get("FNMUSIC_URL_CACHE_MAX", "1000") or 1000)
_URL_CACHE: dict[str, tuple[float, str]] = {}


def _url_cache_get(song_id: str, level: str) -> str | None:
    if _URL_CACHE_TTL <= 0:
        return None
    hit = _URL_CACHE.get(f"{song_id}:{level}")
    if hit and (time.time() - hit[0]) < _URL_CACHE_TTL:
        return hit[1]
    return None


def _url_cache_put(song_id: str, level: str, url: str) -> None:
    if _URL_CACHE_TTL <= 0 or not url:
        return
    key = f"{song_id}:{level}"
    _URL_CACHE[key] = (time.time(), url)
    if len(_URL_CACHE) > _URL_CACHE_MAX:
        for k in sorted(_URL_CACHE, key=lambda kk: _URL_CACHE[kk][0])[: max(1, _URL_CACHE_MAX // 2)]:
            _URL_CACHE.pop(k, None)


def _url_cache_drop_song(song_id: str) -> None:
    prefix = f"{song_id}:"
    for k in [k for k in _URL_CACHE if k.startswith(prefix)]:
        _URL_CACHE.pop(k, None)


def invalidate_url_cache() -> int:
    n = len(_URL_CACHE)
    _URL_CACHE.clear()
    return n


async def resolve_netease_url(client: httpx.AsyncClient, song_id: str,
                              request: Request | None = None) -> str | None:
    """取播放直链。音质档位**按本次请求动态决定**（见 proxy/quality.py）。

    传入 request 是为了让 quality 看到客户端的网络类型线索；不传（同步场景或测试）时
    按策略的 WiFi 档处理，行为与旧版一致。选中的档位上游不给直链时仍会继续降到
    exhigh，不能因为策略选了高档就直接播放失败。
    """
    decision = quality.resolve(request, db_path=resolve_music_db())
    _log_quality_decision(song_id, decision)
    primary = decision.get("level") or str(CONF.get("netease_quality") or "lossless").strip()

    qualities = []
    if primary:
        qualities.append(primary)
    if "exhigh" not in qualities:
        qualities.append("exhigh")

    # 短缓存命中：直链还在有效期内，直接复用（省一次 musicbox + 网易云往返）
    for q in qualities:
        cached = _url_cache_get(song_id, q)
        if cached:
            return cached

    for q in qualities:
        try:
            r = await client.get(f"/api/v1/song/{song_id}/url", params={"quality": q}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        code = inner.get("code")
                        url = inner.get("url")
                        if code == 200 and url:
                            _url_cache_put(song_id, q, str(url))
                            return str(url)
        except Exception as e:
            logger.warning("resolve_netease_url error for %s (quality=%s): %s: %s",
                          song_id, q, type(e).__name__, e)
    return None


_LOGGED_QUALITY: set[tuple[str, str]] = set()


def _log_quality_decision(song_id: str, decision: dict) -> None:
    """每种 (档位, 判定来源) 组合只记一次日志。

    必须能把「跟随飞牛到底生效没有」查出来，否则那个策略可能是从未生效过的空话；
    但每次播放都记会把日志刷满（一首歌至少一次取链），所以按组合去重。
    """
    key = (str(decision.get("level")), str(decision.get("source")))
    if key in _LOGGED_QUALITY:
        return
    _LOGGED_QUALITY.add(key)
    logger.info("音质判定 level=%s source=%s policy=%s network=%s (song=%s)",
                decision.get("level"), decision.get("source"), decision.get("policy"),
                decision.get("network"), song_id)


def ensure_search_list(upstream_json: dict) -> list:
    """保证 data.list 存在，本地 0 条时仍能追加在线条目。"""
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {}
        upstream_json["data"] = data
    target = get_by_path(upstream_json, CONF["search_list_path"])
    if isinstance(target, list):
        return target
    for key in ("list", "items", "tracks", "records"):
        if isinstance(data.get(key), list):
            if key != "list":
                data["list"] = data[key]
            return data["list"]
    data["list"] = []
    if "total" not in data:
        data["total"] = 0
    return data["list"]


def merge_online_tracks(
    upstream_json: dict,
    online_data: list[dict] | dict | None,
    page: int = 1,
    size: int = 50,
) -> dict:
    target_list = ensure_search_list(upstream_json)
    if not online_data:
        return upstream_json

    if isinstance(online_data, dict):
        raw_items = online_data.get("items", [])
    elif isinstance(online_data, list):
        raw_items = online_data
    else:
        raw_items = []

    if not raw_items:
        return upstream_json

    existing_keys = set()
    for item in target_list:
        t = title_from_track(item)
        a = artist_from_track(item)
        if t and a:
            existing_keys.add((t, a))

    filtered_online = []
    for online_item in raw_items:
        if not is_playable_online_track(online_item, require_id=True):
            continue
        ot = str(online_item.get("title") or online_item.get("name") or "").strip().lower()
        oa = str(online_item.get("artist") or "").strip().lower()
        if ot and oa and (ot, oa) in existing_keys:
            continue
        filtered_online.append(online_item)

    online_limit = CONF["online_limit"]
    if page == 1:
        page_online = filtered_online[:online_limit]
    else:
        page_online = filtered_online[online_limit + (page - 2) * size : online_limit + (page - 1) * size]

    for it in page_online:
        target_list.append(build_online_track(it))

    parts = CONF["search_list_path"].split(".")
    parent = upstream_json
    for p in parts[:-1]:
        if isinstance(parent, dict) and p in parent:
            parent = parent[p]
    if isinstance(parent, dict):
        orig_total = parent.get("total")
        if not isinstance(orig_total, int):
            orig_total = len(target_list) - len(page_online)
        parent["total"] = orig_total + len(filtered_online)

    return upstream_json


def extract_guid(request: Request, path_guid: str | None = None) -> str:
    if path_guid:
        return path_guid
    return (
        request.query_params.get("guid")
        or request.query_params.get("trackGUID")
        or request.query_params.get("trackGuid")
        or request.query_params.get("coverId")
        or request.query_params.get("id")
        or request.query_params.get("trackId")
        or ""
    )


async def extract_guid_from_body(request: Request) -> str:
    guid = extract_guid(request)
    if guid:
        return guid
    try:
        body = await request.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return str(
            body.get("guid")
            or body.get("trackGUID")
            or body.get("trackGuid")
            or body.get("id")
            or body.get("trackId")
            or ""
        )
    return ""


def empty_ok() -> JSONResponse:
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {}})


def _online_unavailable(msg: str = "online source unavailable", code: int = 404) -> JSONResponse:
    """在线音源不可用时的统一响应（飞牛播放器据此跳过该曲目）。"""
    return JSONResponse(content={"code": code, "msg": msg, "data": None}, status_code=code)


def build_lyric_list_payload(guid: str, lyric_text: str) -> dict:
    """对齐飞牛 $n.lyric.list → xr(list, preferred)。

    每条需有非空 content；source=2 表示 EXTERNAL_LRC（非内嵌，不强制 offset）。
    """
    text = (lyric_text or "").strip()
    if not text:
        return {"code": 0, "msg": "ok", "data": {"list": [], "preferred": ""}}
    lyric_guid = f"{guid}:lyric"
    now = int(time.time())
    item = {
        "guid": lyric_guid,
        "content": text,
        "source": 2,
        "isLRC": True,
        "offset": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {"list": [item], "preferred": lyric_guid},
    }


def stub_online_info(guid: str) -> dict:
    song_id = song_id_from_online_guid(guid)
    return {
        "id": song_id,
        "source": source_from_online_guid(guid),
        "title": "",
        "artist": "",
        "album": "",
        "duration_s": 0,
        "ext": "mp3",
        "file_size": 0,
        "cover_url": "",
        "lyric": "",
    }


def build_metadata_payload(guid: str, data: dict | None) -> dict:
    """飞牛 resolveTrackPlayback._h() 会无防护读取 data.track.genres.join / album / artists。

    缺 genres 或 album 不是对象时直接抛错，播放器跳过且不会请求 stream。
    """
    info = dict(data or {})
    info.setdefault("id", song_id_from_online_guid(guid))
    info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(info)
    album_obj = vo["album"] if isinstance(vo.get("album"), dict) else {
        "name": str(vo.get("album") or ""),
        "guid": f"{guid}:album",
        "artists": vo.get("artists") or [],
        "coverId": guid,
    }
    track = {
        "guid": guid,
        "id": guid,
        "title": vo.get("title") or "",
        "artists": vo.get("artists") or [],
        "album": album_obj,
        "genres": list(vo.get("genres") or []),
        "duration": vo.get("duration") or 0,
        "coverId": guid,
        "coverUrl": vo.get("coverUrl") or "",
        "format": vo.get("format") or "mp3",
        "hasLyric": bool(vo.get("hasLyric") or info.get("lyric")),
        "isFavorite": False,
        "isCue": False,
        "accessStatus": 0,
        "audioSpec": vo["audioSpec"],
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **vo,
            "guid": guid,
            "id": guid,
            "album": album_obj,
            "audioSpec": vo["audioSpec"],
            "track": track,
        },
    }


def build_local_metadata_payload(guid: str, entry: dict) -> dict:
    """本地曲目（local:file:…）的 metadata 应答。

    形状对齐 ``build_metadata_payload``：飞牛客户端会无防护读 track.genres.join /
    album / artists，缺字段直接抛错 → 播放器跳过、连 stream 都不请求。
    duration 也必须给真值（早先恒为 0，客户端据此判定不可播）。
    """
    title = str(entry.get("title") or "") or _stem_of(entry.get("path"))
    artist = str(entry.get("artist") or "")
    album = str(entry.get("album") or "") or "本地曲库"
    duration_s = float(entry.get("duration") or 0)
    duration_ms = int(entry.get("duration_ms") or duration_s * 1000)
    size = int(entry.get("size") or 0)
    ext = str(entry.get("ext") or "mp3")
    play_format = play_format_from_ext(ext)
    artists = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
    album_obj = {
        "name": album,
        "guid": f"{guid}:album",
        "artists": artists,
        "coverId": guid,
    }
    # ⚠️ 两处必须与在线曲目（build_online_track）严格一致，否则客户端拒绝播放：
    #   1) duration 单位是**毫秒**（在线版 "duration": duration_ms）；
    #      早期这里填秒，客户端把 240 读成 240 毫秒 → 判定不可播。
    #   2) audioSpec.path 必须带真实后缀（"飞牛 ll() 用 path 解析 extension"）；
    #      早期这里整个 audioSpec 是另起炉灶的简版，缺 path 就拿不到容器格式。
    audio_spec = {
        "path": f"local/{guid.split('local:file:', 1)[-1]}.{play_format}",
        "format": play_format,
        "codec": play_format,
        "container": play_format,
        "duration": duration_ms,
        "size": size,
        "channel": int(entry.get("channels") or 2) or 2,
        "sampleRate": int(entry.get("sample_rate") or 44100) or 44100,
        "bitDepth": 16 if play_format in ("wav", "flac", "aiff") else None,
        "bitrate": int(entry.get("bitrate") or 0)
        or (1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000),
    }
    audio_spec = {k: v for k, v in audio_spec.items() if v is not None}
    track = {
        "guid": guid,
        "id": guid,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artists,
        "album": album_obj,
        "albumName": album,
        "audioSpec": audio_spec,
        "duration": duration_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration_s": duration_s,
        "codec": play_format,
        "codecName": play_format,
        "format": play_format,
        "ext": ext,
        "size": size,
        "file_size": size,
        "coverId": guid,
        "cover_url": "",
        "coverUrl": "",
        "coverURL": "",
        "source": "local",
        "is_online": False,
        "isFavorite": False,
        "isCue": False,
        "hasLyric": False,
        "genres": [],
        "accessStatus": 0,
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **track,
            "guid": guid,
            "id": guid,
            "album": album_obj,
            "audioSpec": audio_spec,
            "track": track,
        },
    }


def _stem_of(path: str | None) -> str:
    try:
        return os.path.splitext(os.path.basename(str(path or "")))[0].strip()
    except Exception:  # noqa: BLE001
        return ""


def _conf_log_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _REDACT_KEY_PARTS):
        return "***" if value else ""
    return value


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    logger.info("=== fnmusic-ext v%s configuration ===", get_version())
    for k, v in CONF.items():
        logger.info("  %s = %s", k, _conf_log_value(k, v))
    logger.info("  pushplus_enabled = %s", pushplus.enabled())
    logger.info("==================================")

    created_upstream = False
    created_musicbox = False
    created_push = False

    if getattr(fastapi_app.state, "upstream_client", None) is None:
        fastapi_app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"]),
            base_url="http://unix",
            timeout=30.0,
        )
        created_upstream = True

    if getattr(fastapi_app.state, "musicbox_client", None) is None:
        fastapi_app.state.musicbox_client = httpx.AsyncClient(
            base_url=CONF["musicbox_url"],
            timeout=20.0,
        )
        created_musicbox = True

    if getattr(fastapi_app.state, "push_client", None) is None:
        fastapi_app.state.push_client = httpx.AsyncClient(timeout=pushplus.REQUEST_TIMEOUT_S)
        created_push = True

    musicbox_client = fastapi_app.state.musicbox_client
    stop_event = asyncio.Event()

    # 后台巡检网易云登录态：掉线 / VIP 临期时走 PushPlus 提醒
    if CONF["netease_enabled"]:
        netease_auth.start_watch(musicbox_client, stop_event)

    # 每日定时刷新歌单曲目缓存（v2.6，管理页可配置时间，留空关闭）
    refresh_task: asyncio.Task | None = None
    if CONF["netease_enabled"]:
        refresh_task = asyncio.create_task(_playlist_refresh_loop(fastapi_app, stop_event))

    try:
        yield
    finally:
        stop_event.set()
        if refresh_task is not None:
            refresh_task.cancel()
        netease_auth.stop_watch()
        if created_upstream and getattr(fastapi_app.state, "upstream_client", None):
            await fastapi_app.state.upstream_client.aclose()
            fastapi_app.state.upstream_client = None
        if created_musicbox and getattr(fastapi_app.state, "musicbox_client", None):
            await fastapi_app.state.musicbox_client.aclose()
            fastapi_app.state.musicbox_client = None
        if created_push and getattr(fastapi_app.state, "push_client", None):
            await fastapi_app.state.push_client.aclose()
            fastapi_app.state.push_client = None


app = FastAPI(title="fnmusic-ext", lifespan=lifespan)


@app.get("/_ext/healthz")
async def ext_healthz(request: Request):
    upstream_client = get_upstream_client(request.app)
    upstream_status = "fail"
    try:
        r = await upstream_client.get("/music/api/v1/search/track?keyword=healthz_probe", timeout=2.0)
        if r.status_code < 500:
            upstream_status = "ok"
    except Exception as e:
        logger.debug("Upstream health check failed: %s", e)

    musicbox_client = get_musicbox_client(request.app)
    if not CONF["netease_enabled"]:
        musicbox_status = "disabled"
        login = netease_auth.LoginState(error="disabled")
    else:
        musicbox_status = "fail"
        try:
            r = await musicbox_client.get("/healthz", timeout=2.0)
            if r.status_code == 200:
                musicbox_status = "ok"
        except Exception as e:
            logger.debug("Musicbox health check failed: %s", e)
        login = await netease_auth.fetch_state(musicbox_client, force=True)

    daily_status = "disabled"
    if CONF["netease_enabled"] and CONF["daily_enabled"] and musicbox_status == "ok":
        daily_status = "ok" if login.logged_in else "need_login"

    # 在线音源可用 = 服务活着，且（已登录 或 允许未登录降级播免费曲）
    netease_usable = musicbox_status == "ok" and (
        login.logged_in or CONF["free_only_on_logout"]
    )

    return {
        "ok": upstream_status == "ok" and netease_usable,
        "version": get_version(),
        "upstream": upstream_status,
        "musicbox": musicbox_status,
        "netease": login.to_public_dict(),
        "daily": daily_status,
        "pushplus": "enabled" if pushplus.enabled() else "disabled",
    }


@app.post("/_ext/cache/invalidate")
async def ext_cache_invalidate():
    """清空代理侧缓存（搜索结果 + 登录态），供登录/登出后立即生效。

    代理与管理页面是两个独立进程：页面里扫码登录成功，只重置了页面进程自己的
    登录态。代理这边仍会拿旧的 `_SEARCH_CACHE`（可能全是空结果）和旧的
    `netease_auth` 登录态（TTL 默认 300s）继续服务几分钟，表现为
    "明明登录了，搜索还是只有本地歌曲"。所以登录成功后必须显式打这个端点。

    只做丢弃缓存这一件事：不改配置、不影响播放、可重复调用。
    """
    dropped_search = len(_SEARCH_CACHE)
    _SEARCH_CACHE.clear()
    daily_tasks = len(_DAILY_TASKS)
    for task in list(_DAILY_TASKS.values()):
        if task is not None and not task.done():
            task.cancel()
    _DAILY_TASKS.clear()
    # 已缓存的每日推荐歌单是按"未登录"或旧账号生成的，必须一并作废
    purged_daily = 0
    try:
        root = dailyrec.recommend_cache_dir()
        if os.path.isdir(root):
            for user_dir in os.listdir(root):
                sub = os.path.join(root, user_dir)
                if os.path.isdir(sub):
                    for name in os.listdir(sub):
                        if name.endswith(".json"):
                            try:
                                os.remove(os.path.join(sub, name))
                                purged_daily += 1
                            except OSError:
                                pass
    except OSError as exc:
        logger.warning("purge daily cache failed: %s", exc)

    netease_auth.invalidate_state()
    purged_info = invalidate_online_info_cache()
    purged_urls = invalidate_url_cache()
    purged_channel_lists = len(_channel_recs_cache)
    _channel_recs_cache.clear()
    for task in list(_channel_recs_refresh.values()):
        if task is not None and not task.done():
            task.cancel()
    _channel_recs_refresh.clear()
    logger.info(
        "cache invalidated: search=%d daily_tasks=%d daily_files=%d info=%d urls=%d channel_lists=%d login_state=reset",
        dropped_search, daily_tasks, purged_daily, purged_info, purged_urls,
        purged_channel_lists,
    )
    return {
        "ok": True,
        "cleared": {
            "search_entries": dropped_search,
            "daily_tasks": daily_tasks,
            "daily_cache_files": purged_daily,
            "online_info_entries": purged_info,
            "play_urls": purged_urls,
            "channel_lists": purged_channel_lists,
            "login_state": True,
        },
    }


@app.middleware("http")
async def _observe_quality_hints(request: Request, call_next):
    """被动记录客户端请求里的音质/网络线索，供「跟随飞牛」发现真实契约。

    代理就架在飞牛 socket 上，客户端每个请求都经过这里——这是搞清楚飞牛到底怎么表达
    音质偏好（哪个接口、哪个参数、哪种网络标识）的**唯一可靠途径**，比猜接口名去读
    诚实得多：猜错不会报错，只会让"跟随飞牛"从未生效过。

    只做记录，不改请求也不改响应；任何异常都吞掉，绝不允许影响播放。
    """
    try:
        if request.url.path.startswith("/music/api/"):
            quality.observe_request(request.method, request.url.path,
                                    request.query_params, request.headers)
    except Exception as exc:  # noqa: BLE001
        logger.debug("quality hint observation failed: %s: %s", type(exc).__name__, exc)
    return await call_next(request)


@app.get("/_ext/quality")
async def ext_quality_report():
    """音质策略与「跟随飞牛」的发现证据。只读，不改策略。

    观察记录只存在于**代理进程**内存里，管理页面是另一个进程，必须经 unix socket 取
    （与 /_ext/cache/invalidate 同一套做法）；直接 import 读到的永远是空。
    """
    try:
        return {"ok": True, "data": quality.report(db_path=resolve_music_db())}
    except Exception as exc:  # noqa: BLE001
        logger.warning("quality report failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.get("/_ext/localdaily")
async def ext_local_daily(request: Request):
    """本地每日推荐的排障快照：开关 / 曲库目录 / 扫到几首 / 为什么不注入。

    真机头号失效链条是：music.db 定位不到 → 曲库目录回退到空的 cache 目录 →
    一首歌扫不到 → 歌单**静默不出现**（界面上和「开关关着」一模一样）。
    这里把整条链路摊开，免得靠猜。只读，不写缓存、不触发构建。
    """
    try:
        db = music_db_probe()
        lib = detect_library_dir()
        lib_is_fallback = bool(lib) and os.path.abspath(lib) == os.path.abspath(
            str(CONF["cache_dir"]))
        files = dailyrec._scan_library_audio_files(lib) if lib else []
        # 访问权限排障：代理进程对曲库目录到底读不读得动。飞牛的「应用访问权限」
        # 是按应用账号授权的；代理以 root 运行通常不受限，但如果哪天改了运行身份，
        # 这里能第一时间看出「目录存在但读不了」。
        probe_error = ""
        if lib and os.path.isdir(lib):
            try:
                os.listdir(lib)
            except PermissionError as exc:
                probe_error = f"权限不足（应用访问权限未覆盖该目录）: {exc}"
            except OSError as exc:
                probe_error = f"{type(exc).__name__}: {exc}"
        day = dailyrec.today_key()
        cache_file = dailyrec.local_daily_cache_path("shared", day)
        return {
            "ok": True,
            "data": {
                "enabled": dailyrec.local_daily_enabled(),
                "limit": dailyrec.local_daily_limit(),
                "proxy_identity": f"uid={os.getuid()} gid={os.getgid()}",
                "library_dir": lib,
                "library_dir_exists": bool(lib) and os.path.isdir(lib),
                "library_readable": bool(lib) and os.access(lib, os.R_OK),
                "probe_error": probe_error,
                "library_is_cache_fallback": lib_is_fallback,
                "cache_dir": str(CONF["cache_dir"]),
                "scanned_files": len(files),
                "sample_files": [os.path.relpath(f["path"], lib) for f in files[:5]],
                "day": day,
                "cache_file": cache_file,
                "cache_exists": os.path.exists(cache_file),
                "music_db": db,
                "authorization": library_authorization_state(lib),
                "hint": (
                    "library_is_cache_fallback=true 表示没定位到曲库："
                    "到管理页填「本地曲库目录」，或到「应用设置 → 授权目录」授权你的音乐目录"
                    if lib_is_fallback else ""
                ),
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("local daily diag failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.get("/_ext/authorized")
async def ext_authorized(request: Request):
    """飞牛「应用授权目录」状态快照（v2.9.4）。

    以前本地曲库靠 root 硬读，管理员在应用设置里压根看不到「授权目录」入口。
    这里把「网关在不在 / token 有没有 / 授权了哪些目录 / 当前曲库是否覆盖」
    一次摊开，管理页据此提示去哪里点。
    """
    try:
        if request.query_params.get("refresh") in ("1", "true", "yes"):
            trimgw.invalidate_cache()
        rep = trimgw.authorized_report(force=request.query_params.get("refresh") in ("1", "true", "yes"))
        lib = detect_library_dir()
        is_fallback = bool(lib) and os.path.abspath(lib) == os.path.abspath(str(CONF["cache_dir"]))
        rep["library_dir"] = lib
        rep["library_is_cache_fallback"] = is_fallback
        rep["strict"] = _strict_authorization()
        rep["env_share_paths"] = trimgw.env_share_paths()
        return {"ok": True, "data": rep}
    except Exception as exc:  # noqa: BLE001
        logger.warning("authorized diag failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


@app.get("/_ext/playlists/preview")
async def ext_playlists_preview():
    """当前注入歌单清单与顺序预览（管理页「歌单顺序」卡片的数据源）。

    顺序与 ``playlist_list`` 实际注入**完全同源**（大类顺序 → 手动顺序覆盖），
    保证「网页上排什么序，飞牛里就是什么序」。只读：不盖展示时间戳；
    ``fetch_channel_records`` 顺带刷新注册表属于既有语义，无副作用。
    """
    client = get_musicbox_client(app)
    logged_in = await _netease_logged_in()

    items: list[dict] = []
    if CONF["netease_enabled"] and CONF["daily_enabled"] and logged_in:
        # 每日推荐的 guid 含日期与用户 id，预览用固定假 guid，手动顺序里以
        # token "daily" 与之匹配（apply_explicit_order 的 _token_matches）。
        items.append({
            "guid": playlists.DAILY_NS + "preview",
            "name": dailyrec.daily_playlist_name(dailyrec.today_key()),
            "channel": "daily",
            "track_count": 0,
        })
    try:
        channel_recs, _keep, _complete = await _channel_playlist_records(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("playlist preview: channel records failed: %s: %s",
                       type(exc).__name__, exc)
        channel_recs = []
    for r in channel_recs:
        items.append({
            "guid": str(r.get("guid") or ""),
            "name": str(r.get("name") or "网易云歌单"),
            "channel": str(r.get("channel") or ""),
            "track_count": int(r.get("track_count") or 0),
        })

    stamped_order = playlists.channel_order()
    items.sort(key=lambda it: stamped_order.index(it["channel"])
               if it["channel"] in stamped_order else len(stamped_order))
    items = playlists.apply_explicit_order(items)
    # 缓存统计只看**当前在列**的歌单（不含每日推荐，它的曲目按用户+日期另存）：
    # 旧的 cached 数的是注册表里「有缓存文件」的全部条目，注册表可能留着历史
    # 口径的死条目，出现过「缓存：59/34」这种分子大于分母的自相矛盾显示。
    # 注意此处 items 还没写 is_daily 键，按 channel 判断。
    current_items = [it for it in items if str(it.get("channel") or "") != "daily"]
    cached_count = sum(
        1 for it in current_items
        if playlists.load_cached_tracks(str(it.get("guid") or "")) is not None
    )
    return {
        "ok": True,
        "data": {
            "items": [{**it, "is_daily": it["channel"] == "daily"} for it in items],
            "logged_in": logged_in,
            "manual_order": list(playlists.explicit_order_tokens()),
            "cache": {
                "cached": cached_count,
                "total": len(current_items),
                "ttl_s": playlists.tracks_cache_ttl(),
                "refresh_at": playlists.refresh_time_of_day(),
                "warming": _PLAYLIST_WARMING,
            },
        },
    }


@app.post("/_ext/playlists/warm")
async def ext_playlists_warm():
    """立即后台预热当前在列歌单的曲目缓存（管理页按钮，不受冷却限制）。

    只预热**当前口径实际注入**的歌单——注册表里的历史死条目（旧分类/旧上限
    遗留）不再被全量拉取；清单完整时顺带 forget_stale 清理死条目及其缓存，
    让注册表、缓存计数与飞牛里看到的歌单一一对应。
    """
    client = get_musicbox_client(app)
    try:
        recs, keep, complete = await _channel_playlist_records(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("预热按钮：拉取当前歌单清单失败: %s: %s", type(exc).__name__, exc)
        recs, keep, complete = [], set(), False
    if complete and keep:
        playlists.forget_stale(keep)
    guids = [str(r.get("guid") or "") for r in recs if r.get("guid")]
    if not _schedule_playlist_warm(app, force=True, guids=guids):
        return {"ok": True, "data": {"started": False, "reason": "already_running"}}
    return {"ok": True, "data": {"started": True, "total": len(guids)}}


@app.get("/music/api/v1/search/track")
@app.get("/music/api/v1/search/track/{subpath:path}")
async def search_track(request: Request):
    upstream_client = get_upstream_client(request.app)
    musicbox_client = get_musicbox_client(request.app)
    keyword = extract_keyword(request)

    page_str = request.query_params.get("page")
    try:
        page = int(page_str) if page_str else 1
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    size_str = request.query_params.get("size")
    try:
        size = int(size_str) if size_str else 50
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    # 在线搜索与上游请求【并发】发起：网易云这一路本身要几秒（服务端要批量校验
    # 真实直链），串行排在 upstream 之后等于把这段时间白等掉，
    # 常常因此撞上首屏预算而只返回本地结果。
    online_allowed = await _online_search_allowed(musicbox_client) if keyword else False

    now = time.time()
    cached_entry = _SEARCH_CACHE.get(keyword) if keyword else None
    cache_ttl = CONF["search_cache_ttl"]
    if cached_entry is not None and not (cached_entry.get("items") or []):
        # 空结果按短 TTL 处理，让上游恢复后能自动自愈
        cache_ttl = min(cache_ttl, CONF["search_empty_ttl"])
    is_valid_cache = cached_entry is not None and (now - cached_entry.get("ts", 0) < cache_ttl)

    entry: dict[str, Any] | None = None
    search_task: asyncio.Task | None = None
    agg_task: asyncio.Task | None = None
    online_all: list[dict] = []

    if keyword and online_allowed and not is_valid_cache:
        entry = {"items": [], "ts": time.time(), "task": None}
        search_task = asyncio.create_task(
            fetch_netease_search(musicbox_client, keyword, CONF["netease_search_limit"])
        )
        agg_task = asyncio.create_task(_collect_search(entry, search_task))
        entry["task"] = agg_task
        _set_search_cache(keyword, entry)

    def _drop_pending() -> None:
        """上游失败时别把在线搜索任务留在事件循环里空跑。"""
        for t in (agg_task, search_task):
            if t is not None and not t.done():
                t.cancel()

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)

    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        _drop_pending()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        _drop_pending()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        _drop_pending()
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    if not keyword:
        _drop_pending()
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    if is_valid_cache and cached_entry is not None:
        task = cached_entry.get("task")
        if task and not task.done() and page >= 2:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=CONF["late_page_wait_s"])
            except Exception:
                pass
        online_all = cached_entry.get("items", []) if online_allowed else []
    elif entry is not None:
        if search_task is not None:
            if page == 1:
                # 阶段一：首屏等待预算（默认 3s）
                if not await _wait_task(search_task, float(CONF.get("netease_wait_s", 3.0))):
                    # 阶段二：仍未返回则再等兜底预算（默认 5s），一到就立刻采用
                    await _wait_task(search_task, float(CONF.get("late_page_wait_s", 5.0)))
                if search_task.done() and not agg_task.done():
                    try:
                        entry["items"] = deduplicate_online_items(search_task.result() or [])
                    except Exception as exc:
                        logger.warning("netease search failed (isolated): %s", exc)
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(agg_task), timeout=CONF["late_page_wait_s"])
                except Exception:
                    pass

        online_all = entry.get("items", [])

    merged = merge_online_tracks(upstream_json, online_all, page=page, size=size)
    return JSONResponse(content=merged, status_code=upstream_resp.status_code, headers=resp_headers)


async def _online_search_allowed(musicbox_client: httpx.AsyncClient) -> bool:
    """在线音源是否放行。

    单源 = 网易云。未登录时是否降级为「只播免费曲」由 FNMUSIC_FREE_ONLY_ON_LOGOUT 决定：
    开（默认）→ 继续搜索，musicbox 服务端已把结果过滤成免费可播曲目；
    关 → 未登录直接不提供任何在线结果。
    """
    if not CONF["netease_enabled"]:
        return False
    if CONF["free_only_on_logout"]:
        return True
    state = await netease_auth.fetch_state(musicbox_client)
    if not state.logged_in:
        logger.info("online search skipped: 网易云未登录且未开启免费曲降级")
        return False
    return True


async def _wait_task(task: asyncio.Task, timeout: float) -> bool:
    """等待任务完成；返回是否在预算内完成。超时/异常都算未完成。"""
    if timeout <= 0:
        return task.done()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return True
    except Exception:
        return task.done()


async def _collect_search(entry: dict, task: "asyncio.Task | None") -> None:
    """后台聚合：搜索任务完成后把结果写进缓存条目，供翻页复用。"""
    res = None
    if task is not None:
        try:
            res = await task
        except Exception as exc:
            logger.warning("netease search bg failed: %s", exc)
    entry["items"] = deduplicate_online_items(res if isinstance(res, list) else [])
    # 以"聚合完成"为缓存起点：空结果的短 TTL 窗口从此刻开始计时，
    # 而不是从发起请求那一刻（否则慢搜索会吃掉大部分自愈窗口）
    entry["ts"] = time.time()
    if not entry["items"]:
        logger.info(
            "online search returned no playable tracks; 该关键词按短 TTL(%ss) 缓存以便自愈",
            CONF.get("search_empty_ttl", 60),
        )


@app.get("/music/api/v1/search/suggest")
@app.get("/music/api/v1/search/suggest/{subpath:path}")
async def search_suggest(request: Request):
    if not CONF["merge_suggest"]:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    musicbox_client = get_musicbox_client(request.app)
    keyword = extract_keyword(request)

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    netease_task: asyncio.Task | None = None
    if keyword and await _online_search_allowed(musicbox_client):
        # 联想词只要标题，跳过详情补齐以省下一次往返
        netease_task = asyncio.create_task(
            fetch_netease_search(musicbox_client, keyword, 5, enrich=False)
        )

    def _drop_task() -> None:
        if netease_task is not None and not netease_task.done():
            netease_task.cancel()

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        _drop_task()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        _drop_task()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        _drop_task()
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    netease_rows = None
    if netease_task is not None:
        try:
            netease_rows = await asyncio.wait_for(asyncio.shield(netease_task), timeout=10.0)
        except Exception as e:
            logger.warning("Suggest netease error: %s", e)
            netease_task.cancel()

    data_field = upstream_json.get("data")
    if isinstance(data_field, list) and isinstance(netease_rows, list):
        for item in netease_rows[:5]:
            title = str(item.get("title") or "")
            if title and title not in data_field:
                data_field.append(title)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


def stream_tee_response(
    resp: httpx.Response,
    guid: str,
    range_header: str | None,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    client_to_close: httpx.AsyncClient | None = None,
    resolved_ext: str | None = None,
    pre_info: dict | None = None,
) -> Response:
    out_headers = {"Accept-Ranges": "bytes"}
    for k in ("content-type", "content-length", "content-range"):
        v = resp.headers.get(k)
        if v:
            out_headers[k] = v

    if resolved_ext:
        out_headers["content-type"] = media_type_for_ext(resolved_ext)

    status_code = resp.status_code
    content_length_str = resp.headers.get("content-length")
    content_length = (
        int(content_length_str) if content_length_str and content_length_str.isdigit() else None
    )

    ext = (resolved_ext or "").strip().lower() or ext_from_content_type(resp.headers.get("content-type") or "")
    store_dir = detect_library_dir()

    if should_cache(range_header):
        os.makedirs(store_dir, exist_ok=True)
        part_path = os.path.join(store_dir, f"{cache_safe_guid(guid)}.{uuid4().hex[:8]}.part")
        info_task: asyncio.Task | None = None
        if pre_info is None and coro_factory is not None:
            info_task = asyncio.create_task(coro_factory())

        # 背压缓冲（v2.7）：原先队列无界——NAS 从 CDN 下载通常远快于客户端消费，
        # 慢客户端一首无损能整个堆在内存里；快速切歌时多个被放弃的流各占
        # 一整首歌的内存，叠加后足以把代理进程推入 OOM（应用「异常退出」的
        # 候选根因之一）。现在入队满 64 块（约 4MB）就暂停拉取，TCP 背压自然
        # 传导给 CDN；客户端断开时转「只写盘」模式把文件下完（保住缓存），
        # 不再往一个没人消费的队列里堆数据。
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        client_gone = asyncio.Event()

        async def _enqueue(chunk: "bytes | None") -> bool:
            """入队；客户端已断开时返回 False（调用方应停止入队）。"""
            if client_gone.is_set():
                return False
            try:
                queue.put_nowait(chunk)
                return True
            except asyncio.QueueFull:
                put_fut: asyncio.Future = asyncio.ensure_future(queue.put(chunk))
                gone_fut: asyncio.Future = asyncio.ensure_future(client_gone.wait())
                done, _pending = await asyncio.wait(
                    {put_fut, gone_fut}, return_when=asyncio.FIRST_COMPLETED)
                if put_fut.done() and not put_fut.cancelled():
                    gone_fut.cancel()
                    return True
                put_fut.cancel()
                return False

        async def _downloader():
            written = 0
            part_file = None
            abandoned = False
            try:
                part_file = open(part_path, "wb")
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    part_file.write(chunk)
                    written += len(chunk)
                    if not abandoned and not await _enqueue(chunk):
                        abandoned = True
                        logger.info(
                            "tee client gone for %s, continuing disk-only download for cache",
                            guid)
            except Exception as e:
                logger.warning("tee download failed for %s: %s", guid, e)
            finally:
                if part_file:
                    try:
                        part_file.close()
                    except Exception:
                        pass
                await resp.aclose()
                if client_to_close:
                    await client_to_close.aclose()
                if not abandoned:
                    # 正常路径：通知消费端下载结束（客户端已断开时无须通知）
                    try:
                        await _enqueue(None)
                    except Exception:
                        pass
                info: dict | None = pre_info
                if info is None and info_task:
                    try:
                        info = await asyncio.wait_for(asyncio.shield(info_task), timeout=8.0)
                    except Exception as e:
                        logger.warning("info/lyric wait failed for %s: %s", guid, e)
                title = str((info or {}).get("title") or "")
                artist = str((info or {}).get("artist") or "")
                album = str((info or {}).get("album") or "")
                lyric_text = str((info or {}).get("lyric") or (info or {}).get("lrc") or "")
                complete = written >= 1024 and (content_length is None or written == content_length)
                if complete:
                    dest = library_media_path(guid, title, ext, artist=artist)
                    try:
                        os.replace(part_path, dest)
                        adopt_library_perms(dest)
                        remember_media_path(guid, dest)
                        write_audio_tags(dest, title=title, artist=artist, album=album)
                        if lyric_text.strip():
                            write_lyric_cache(guid, lyric_text, title=title, artist=artist)
                    except Exception as e:
                        logger.warning("Failed to rename cache file: %s", e)
                        if os.path.exists(part_path):
                            try:
                                os.remove(part_path)
                            except Exception:
                                pass
                elif os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass

        dl_task = asyncio.create_task(_downloader())

        async def stream_tee() -> AsyncGenerator[bytes, None]:
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    yield chunk
            finally:
                # 客户端断开/取消：通知下载端停止入队（它会转入只写盘模式）
                client_gone.set()

        return StreamingResponse(stream_tee(), status_code=status_code, headers=out_headers)

    async def stream_no_cache() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()
            if client_to_close:
                await client_to_close.aclose()

    return StreamingResponse(stream_no_cache(), status_code=status_code, headers=out_headers)


@app.get("/music/api/v1/track/stream")
@app.get("/music/api/v1/track/stream/{subpath:path}")
async def stream_track(request: Request):
    started = time.monotonic()
    guid = extract_guid(request)

    # 本地每日推荐的曲目（guid 形如 local:file:<sha1>）：直接从磁盘读文件。
    # 必须放在「非 online guid 直通官方后端」分支**之前**——local:file: 不带
    # online: 前缀，先判 alive 会被当成普通本地曲目转发官方后端（它按自己的
    # 库处理，最坏 404），永远到不了这里。
    # 路径在当天 bundle 里反查（guid 是路径指纹，无法逆向）。
    if str(guid or "").startswith("local:file:"):
        local_path = await _local_daily_path_of(request, guid) \
            or local_files.resolve(guid)
        if local_path and os.path.isfile(local_path):
            ext = os.path.splitext(local_path)[1].lstrip(".") or "mp3"
            logger.info("play-start local-daily %s %.0fms", guid,
                        (time.monotonic() - started) * 1000.0)
            return serve_file_with_range(local_path, request.headers.get("range"),
                                         media_type_for_ext(ext))
        return await forward_to_upstream(
            request, get_upstream_client(request.app),
            timeout=PLAYBACK_FORWARD_TIMEOUT_S, label="local-stream")

    if not is_online_guid(guid):
        # 本地曲目直通官方后端；播放链路放宽读超时（见 PLAYBACK_FORWARD_TIMEOUT_S）
        return await forward_to_upstream(
            request, get_upstream_client(request.app),
            timeout=PLAYBACK_FORWARD_TIMEOUT_S, label="local-stream")

    def _log_play(source: str) -> None:
        """播放起步留证：来源 + 总耗时。真机上「点开到出声几秒」从此可量化。"""
        logger.info("play-start %s %s %.0fms", source, guid,
                    (time.monotonic() - started) * 1000.0)

    range_header = request.headers.get("range")
    cached = find_cache_file(guid)
    if cached:
        cached = promote_cache_hit(guid, cached)
        ext = os.path.splitext(cached)[1].lstrip(".") or "mp3"
        _log_play("tee-cache")
        return serve_file_with_range(cached, range_header, media_type_for_ext(ext))

    src = source_from_online_guid(guid)
    if src and src != NETEASE_SOURCE:
        # 历史遗留的非网易云 guid（旧版多音源缓存/收藏），单源版无法解析
        logger.info("stream rejected: unsupported legacy online source %r (guid=%s)", src, guid)
        return _online_unavailable("unsupported online source")

    musicbox_client = get_musicbox_client(request.app)

    # 未登录且未开启免费曲降级时，不提供任何在线播放（本地缓存命中已在上方返回）
    if not await _online_search_allowed(musicbox_client):
        return _online_unavailable("netease login required")

    song_id = song_id_from_online_guid(guid).split(":")[-1]
    if not song_id:
        return _online_unavailable()

    play_url_res, info_res = await asyncio.gather(
        resolve_netease_url(musicbox_client, song_id, request),
        _online_info(request, guid),
        return_exceptions=True,
    )
    play_url = None if isinstance(play_url_res, Exception) else play_url_res
    info = None if isinstance(info_res, Exception) else info_res

    # ------------------------------------------------------------------
    # 本地曲库优先（v2.8）：NAS 上已有同一首歌时直接读本地文件——零外网、
    # 起步最快。是否可用本地受音质策略约束（见 local_library 模块头注）：
    # 策略要 lossless 且本地是无损 → 用本地；策略要 exhigh（省流量）而本地是
    # Hi-Res → 不用本地，仍按策略去网易云要 320k；本地 320k 而策略要 lossless
    # → 同样不用本地。网易云取链彻底失败时本地匹配（不论档位）作最后兜底。
    # ------------------------------------------------------------------
    local_hit: dict | None = None
    if local_library.local_first_enabled() and isinstance(info, dict) \
            and str(info.get("title") or "").strip():
        decision = quality.resolve(request, db_path=resolve_music_db())
        local_hit = local_library.find_local_match(
            str(info.get("title") or ""),
            str(info.get("artist") or ""),
            resolve_music_db(),
        )
        if local_hit and local_library.serves_request(local_hit, decision.get("level")):
            try:
                _log_play(f"local-first(class={local_hit.get('klass')},level={decision.get('level')})")
                logger.info("local-first hit: %s -> %s", guid, local_hit.get("path"))
                return serve_file_with_range(
                    local_hit["path"], range_header,
                    media_type_for_ext(local_hit.get("ext") or "mp3"))
            except Exception as exc:  # noqa: BLE001 - 本地文件异常则回落在线链路
                logger.warning("local-first serve failed for %s: %s: %s",
                               guid, type(exc).__name__, exc)
                local_hit = None

    def _serve_local_fallback(reason: str):
        _log_play(f"local-fallback({reason})")
        logger.info("local-first fallback (%s): %s -> %s", reason, guid,
                    (local_hit or {}).get("path"))
        return serve_file_with_range(
            local_hit["path"], range_header,
            media_type_for_ext(local_hit.get("ext") or "mp3"))

    if not play_url:
        if local_hit:
            try:
                return _serve_local_fallback("netease resolve failed")
            except Exception as exc:  # noqa: BLE001
                logger.warning("local fallback serve failed for %s: %s", guid, exc)
        state = netease_auth.current_state()
        if not state.logged_in:
            logger.info("stream 404 for %s: 网易云未登录，该曲目需要账号权益", guid)
        return _online_unavailable()

    resolved_ext = str(info.get("ext")) if (isinstance(info, dict) and info.get("ext")) else None

    req_headers = {}
    if range_header:
        req_headers["Range"] = range_header

    async def _open_cdn(url: str) -> tuple[httpx.Response | None, httpx.AsyncClient | None]:
        """连网易云 CDN 取流。失败返回 (None, None)（客户端已自行关闭）。"""
        stream_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        try:
            stream_req = stream_client.build_request("GET", url, headers=req_headers)
            resp = await stream_client.send(stream_req, stream=True)
            content_type = (resp.headers.get("content-type") or "").lower()
            if resp.status_code >= 400 or "text/html" in content_type:
                await resp.aclose()
                await stream_client.aclose()
                return None, None
            return resp, stream_client
        except Exception as e:
            logger.warning("Failed to stream netease url for %s: %s", guid, e)
            await stream_client.aclose()
            return None, None

    resp, stream_client = await _open_cdn(play_url)
    if resp is None:
        # 直链失效兜底：命中的可能是短缓存里的旧链（CDN 已过期/403）。
        # 丢弃该歌曲的全部缓存直链、强制重取一次；拿到**不同的**新链才重试。
        _url_cache_drop_song(song_id)
        fresh_url = await resolve_netease_url(musicbox_client, song_id, request)
        if fresh_url and fresh_url != play_url:
            logger.info("netease url stale for %s, retrying with fresh url", guid)
            play_url = fresh_url
            resp, stream_client = await _open_cdn(play_url)
        if resp is None and local_hit:
            try:
                return _serve_local_fallback("cdn unreachable")
            except Exception as exc:  # noqa: BLE001
                logger.warning("local fallback serve failed for %s: %s", guid, exc)
        if resp is None:
            return _online_unavailable()

    _log_play("netease")
    return stream_tee_response(
        resp,
        guid=guid,
        range_header=range_header,
        coro_factory=None if info is not None else (lambda: _online_info(request, guid)),
        client_to_close=stream_client,
        resolved_ext=resolved_ext,
        pre_info=info if isinstance(info, dict) else None,
    )


@app.get("/music/api/v1/track/hls/{guid}/preset.m3u8")
@app.get("/music/api/v1/track/hls/{guid}/{filename}")
async def track_hls(request: Request, guid: str, filename: str = "preset.m3u8"):
    # 本地曲目：官方后端不认 local:file:，转发过去只会失败。直接指回我们自己的
    # /track/stream（它已经能从索引反查真实文件并按 Range 输出）。
    if str(guid or "").startswith("local:file:"):
        entry = local_files.entry_with_probe(guid) or {}
        duration_s = int(entry.get("duration") or 0) or 240
        stream_url = f"/music/api/v1/track/stream?guid={quote(guid, safe='')}"
        playlist = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            f"#EXT-X-TARGETDURATION:{max(duration_s, 1)}\n"
            "#EXT-X-PLAYLIST-TYPE:VOD\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
            f"#EXTINF:{duration_s:.3f},\n"
            f"{stream_url}\n"
            "#EXT-X-ENDLIST\n"
        )
        return Response(content=playlist, media_type="application/vnd.apple.mpegurl")
    if not is_online_guid(guid):
        client = get_upstream_client(request.app)
        if str(filename).lower().endswith(".m3u8"):
            # m3u8 很小且是排障关键：整包透传 + 日志留证（见 forward_buffered）
            return await forward_buffered(
                request, client, timeout=PLAYBACK_FORWARD_TIMEOUT_S,
                label="hls-playlist", body_sniff=240)
        # 分片可能很大，流式转发 + 状态码留证
        return await forward_to_upstream(
            request, client, timeout=PLAYBACK_FORWARD_TIMEOUT_S, label="hls-segment")

    info = await _online_info(request, guid)
    duration_s = 0
    if info:
        try:
            duration_s = int(float(info.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration_s = 0
    if duration_s <= 0:
        duration_s = 240

    stream_url = f"/music/api/v1/track/stream?guid={quote(guid, safe='')}"
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{max(duration_s, 1)}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:{duration_s:.3f},\n"
        f"{stream_url}\n"
        "#EXT-X-ENDLIST\n"
    )
    return Response(content=playlist, media_type="application/vnd.apple.mpegurl")


@app.api_route("/music/api/v1/track/transcode/heartbeat", methods=["GET", "POST"])
@app.api_route("/music/api/v1/track/transcode/quit", methods=["GET", "POST"])
async def track_transcode_session(request: Request):
    guid = await extract_guid_from_body(request)
    # 本地曲目本来就是本地文件直读，不需要官方的转码会话；转发过去只会失败。
    if str(guid or "").startswith("local:file:"):
        return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid}})
    if not is_online_guid(guid):
        return await forward_buffered(
            request, get_upstream_client(request.app),
            timeout=PLAYBACK_FORWARD_TIMEOUT_S, label="transcode-session", body_sniff=160)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid}})


@app.api_route("/music/api/v1/track/transcode", methods=["GET", "POST"])
async def track_transcode(request: Request):
    guid = await extract_guid_from_body(request)
    if str(guid or "").startswith("local:file:"):
        # 本地文件直接给，不走官方转码（它没有这个曲目，必然失败）。
        return JSONResponse(content={
            "code": 0,
            "msg": "ok",
            "status": "success",
            "data": {"guid": guid, "status": "ready"},
        })
    if not is_online_guid(guid):
        # 本地转码启动要等 ffmpeg 就绪，30s 共享超时会把它打成 504。
        # 应答整包透传 + 留证：转码会话到底建没建起来，日志里必须看得见。
        return await forward_buffered(
            request, get_upstream_client(request.app),
            timeout=PLAYBACK_FORWARD_TIMEOUT_S, label="transcode-start", body_sniff=240)
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "status": "success",
            "data": {"guid": guid, "status": "ready"},
        }
    )


# ---------------------------------------------------------------------------
# 在线曲目元数据缓存
#
# 单曲的标题/艺术家/专辑/时长/封面/歌词是**静态**数据，不会变。原先每次
# /static/metadata、/lyric/list、/static/cover、播放路径都各自向 musicbox 发一次
# /api/v1/song/{id}/info + 一次 /api/v1/song/{id}/lyric（两个上游往返），
# 一首歌点开要重复好几轮 —— 实测这是「点击到出声约 4s」的主要构成
# （真正的流式转发改进后首字节仅 0.02~0.84s）。
#
# 只缓存**成功**结果：失败多半是上游瞬时抖动，缓存下来会把一次偶发失败
# 固化成一整段 TTL 里都无元数据，比多回源一次更糟。
# ---------------------------------------------------------------------------

_ONLINE_INFO_CACHE: dict[str, tuple[float, dict]] = {}
_ONLINE_INFO_TTL = float(os.environ.get("FNMUSIC_INFO_CACHE_TTL", "3600"))
_ONLINE_INFO_MAX = int(os.environ.get("FNMUSIC_INFO_CACHE_MAX", "2000"))

# 封面单独一份缓存：取封面只需要 /info 里的 al.picUrl，绝不该顺带去拉歌词
# （歌词是另一个上游往返，对一张缩略图毫无意义）。
_ONLINE_COVER_CACHE: dict[str, tuple[float, str]] = {}
_ONLINE_COVER_TTL = float(os.environ.get("FNMUSIC_COVER_CACHE_TTL", "86400"))


def _cache_put_prune(store: dict, max_entries: int) -> None:
    """超过上限时按写入时间淘汰最旧的一半，避免无界增长。"""
    if len(store) <= max_entries:
        return
    for k in sorted(store, key=lambda kk: store[kk][0])[: max(1, max_entries // 2)]:
        store.pop(k, None)


def invalidate_online_info_cache() -> int:
    """清空元数据与封面缓存，返回丢弃条目数。"""
    n = len(_ONLINE_INFO_CACHE) + len(_ONLINE_COVER_CACHE)
    _ONLINE_INFO_CACHE.clear()
    _ONLINE_COVER_CACHE.clear()
    return n


async def _fetch_cover_bytes(url: str) -> tuple[bytes, str] | None:
    """代抓封面图片字节。抽成函数是为了给测试留一个干净的接缝。

    返回 ``(图片字节, content-type)``；抓不到或不是图片则返回 ``None``。
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as pic_client:
            pic = await pic_client.get(url)
        if pic.status_code != 200 or not pic.content:
            return None
        ctype = (pic.headers.get("content-type") or "").split(";")[0].strip()
        if ctype and not ctype.startswith("image/"):
            return None
        return pic.content, ctype or "image/jpeg"
    except Exception as e:  # noqa: BLE001
        logger.warning("proxying cover failed for %s: %s: %s", url[:80], type(e).__name__, e)
        return None


# ---------------------------------------------------------------------------
# 封面字节磁盘缓存（v2.8）：同一 URL 只从网易云 CDN 抓一次，之后全部读本地。
#
# 此前只缓存了封面 URL（24h），字节每次都现抓——换台设备、客户端清了缓存、
# 或多端同时打开列表，NAS 就要对同一批图再跑一遍 CDN 往返（每张几百毫秒），
# 歌单列表的渲染时间被这些串行往返拖长。封面是静态资源，落盘没有任何
# 过期问题；超过上限按 mtime 淘汰最旧的。
# ---------------------------------------------------------------------------

_COVER_DISK_MAX_FILES = 800

# 封面压缩目标边长（像素）。网易云原图普遍几百 KB～1MB+，一个歌单列表首屏
# 三四十张就是几十 MB——移动网络下「列表打开卡顿」的主力（真机用户反馈定位）。
# 网易云 CDN 原生支持 ?param={N}y{N} 服务端缩图（p1.music.126.net 系域名），
# 300px 的封面约 20~50KB，体积缩到原图的几十分之一。0 = 不压缩。
# 动态读环境变量（.env 改完保存即生效，无需重启）。
def _cover_resize_px_config() -> int:
    return _int("FNMUSIC_COVER_RESIZE_PX", 300)


def cover_resize_px(requested_size: "int | None" = None) -> int:
    """决定本次封面用多大的缩图。客户端带 size 参数时优先，否则用配置默认。"""
    if requested_size:
        try:
            px = int(requested_size)
        except (TypeError, ValueError):
            px = 0
        if px >= 60:
            return min(px, 800)
    px = _cover_resize_px_config()
    if px <= 0:
        return 0
    return min(max(px, 60), 800)


def resized_cover_url(url: str, px: int) -> str:
    """给网易云 CDN 封面 URL 加 ?param={px}y{px}（服务端缩图）。

    只对 music.126.net 系域名生效（其他图床不认识这个参数，加了反而可能 404）。
    已有 query 的先剥掉（网易云该参数是唯一的尺寸控制方式）。
    """
    if px <= 0 or not url:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower()
        if not host.endswith("music.126.net"):
            return url
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           f"param={px}y{px}", ""))
    except Exception:  # noqa: BLE001 - URL 解析失败就原图原样
        return url


def _cover_cache_dir() -> str:
    return os.path.join(CONF["cache_dir"], "cover_cache")


def _cover_cache_paths(url: str) -> tuple[str, str]:
    import hashlib

    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    d = _cover_cache_dir()
    return os.path.join(d, f"{key}.bin"), os.path.join(d, f"{key}.json")


def _prune_cover_disk_cache() -> None:
    d = _cover_cache_dir()
    try:
        entries = []
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                entries.append((os.path.getmtime(p), p))
    except OSError:
        return
    excess = len(entries) - _COVER_DISK_MAX_FILES
    if excess <= 0:
        return
    for _mtime, path in sorted(entries)[:excess]:
        try:
            os.remove(path)
        except OSError:
            pass


async def _fetch_cover_bytes_cached(url: str) -> tuple[bytes, str] | None:
    """带磁盘缓存的封面代抓：命中直接读盘，未命中才出网（并落盘）。"""
    bin_path, meta_path = _cover_cache_paths(url)
    try:
        if os.path.isfile(bin_path) and os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                ct = str(json.load(f).get("ct") or "image/jpeg")
            with open(bin_path, "rb") as f:
                data = f.read()
            if data:
                return data, ct
    except Exception as exc:  # noqa: BLE001 - 缓存坏了就当没有
        logger.debug("cover disk cache read failed for %s: %s", url[:80], exc)

    fetched = await _fetch_cover_bytes(url)
    if fetched:
        content, ct = fetched
        try:
            os.makedirs(_cover_cache_dir(), exist_ok=True)
            tmp_bin = f"{bin_path}.{uuid4().hex[:8]}.part"
            with open(tmp_bin, "wb") as f:
                f.write(content)
            os.replace(tmp_bin, bin_path)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"ct": ct, "url": url[:200]}, f, ensure_ascii=False)
            _prune_cover_disk_cache()
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响本次应答
            logger.debug("cover disk cache write failed for %s: %s", url[:80], exc)
    return fetched


async def _online_cover_url(request: Request, guid: str) -> str:
    """只取封面地址，不拉歌词。命中缓存时零上游往返。"""
    now = time.time()
    hit = _ONLINE_COVER_CACHE.get(guid)
    if hit and now - hit[0] < _ONLINE_COVER_TTL:
        return hit[1]

    song_id = song_id_from_online_guid(guid).split(":")[-1]
    if not song_id:
        return ""
    src = source_from_online_guid(guid)
    if src and src != NETEASE_SOURCE:
        return ""

    cover = ""
    try:
        r = await get_musicbox_client(request.app).get(
            f"/api/v1/song/{song_id}/info", timeout=10.0
        )
        if r.status_code == 200:
            res = r.json()
            if isinstance(res, dict) and res.get("ok") is not False:
                data = res.get("data")
                if isinstance(data, dict):
                    al = data.get("al")
                    if not isinstance(al, dict):
                        al = {}
                    cover = str(al.get("picUrl") or al.get("pic_url") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("online cover fetch failed for %s: %s: %s",
                       guid, type(e).__name__, e)
        return ""

    # 空结果不缓存：那多半是上游瞬时失败，缓存会让封面长时间空白
    if not cover:
        return ""
    _ONLINE_COVER_CACHE[guid] = (now, cover)
    _cache_put_prune(_ONLINE_COVER_CACHE, _ONLINE_INFO_MAX)
    return cover


async def _online_info(request: Request, guid: str) -> dict | None:
    """在线曲目元数据：只走网易云（唯一音源）。结果按 TTL 缓存。"""
    now = time.time()
    hit = _ONLINE_INFO_CACHE.get(guid)
    if hit and now - hit[0] < _ONLINE_INFO_TTL:
        return hit[1]

    src = source_from_online_guid(guid)
    if src and src != NETEASE_SOURCE:
        return None

    song_id = song_id_from_online_guid(guid).split(":")[-1]
    if not song_id:
        return None

    musicbox_client = get_musicbox_client(request.app)
    try:
        # info 与 lyric 并发拉取（原先串行，首播一首歌要付两次跨洋往返的加和）
        r, lr = await asyncio.gather(
            musicbox_client.get(f"/api/v1/song/{song_id}/info", timeout=10.0),
            musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0),
            return_exceptions=True,
        )
        if isinstance(r, Exception):
            raise r
        if r.status_code != 200:
            return None
        res_data = r.json()
        if not isinstance(res_data, dict) or res_data.get("ok") is False:
            return None
        data = res_data.get("data")
        if not isinstance(data, dict):
            return None

        ar = data.get("ar") or data.get("artists") or []
        ar_names = []
        if isinstance(ar, list):
            for x in ar:
                if isinstance(x, dict) and x.get("name"):
                    ar_names.append(str(x["name"]))
                elif isinstance(x, str):
                    ar_names.append(x)
        artist = " / ".join(ar_names)

        al = data.get("al") or {}
        if not isinstance(al, dict):
            al = {}
        album_name = str(al.get("name") or "")
        cover_url = str(al.get("picUrl") or al.get("pic_url") or "")

        dt = data.get("dt") or data.get("duration") or 0
        try:
            dt_f = float(dt)
            duration_s = dt_f / 1000.0 if dt_f > 1000 else dt_f
        except (TypeError, ValueError):
            duration_s = 0.0

        sq = data.get("sq")
        hr = data.get("hr")
        h = data.get("h") or {}
        ext = "flac" if (sq or hr) else "mp3"
        size_obj = sq or hr or h or {}
        file_size = int(size_obj.get("size", 0) or 0) if isinstance(size_obj, dict) else 0

        lyric_text = ""
        if isinstance(lr, Exception):
            logger.warning("musicbox lyric fetch in _online_info failed for %s: %s: %s",
                           guid, type(lr).__name__, lr)
        else:
            try:
                if lr.status_code == 200:
                    l_res = lr.json()
                    if isinstance(l_res, dict) and l_res.get("ok") is not False:
                        l_data = l_res.get("data")
                        if isinstance(l_data, dict):
                            lyric_text = str(l_data.get("lyric") or "").strip()
            except Exception as l_err:
                logger.warning("musicbox lyric parse in _online_info failed for %s: %s: %s",
                               guid, type(l_err).__name__, l_err)

        record = {
            "id": f"{NETEASE_SOURCE}:{song_id}",
            "source": NETEASE_SOURCE,
            "title": str(data.get("name") or ""),
            "artist": artist,
            "album": album_name,
            "cover_url": cover_url,
            "duration_s": duration_s,
            "ext": ext,
            "file_size": file_size,
            "lyric": lyric_text,
        }
        if cover_url:
            # 顺带把封面缓存也填上：/static/cover 随后就能零上游往返命中
            _ONLINE_COVER_CACHE[guid] = (time.time(), cover_url)
            _cache_put_prune(_ONLINE_COVER_CACHE, _ONLINE_INFO_MAX)
        _ONLINE_INFO_CACHE[guid] = (time.time(), record)
        _cache_put_prune(_ONLINE_INFO_CACHE, _ONLINE_INFO_MAX)
        return record
    except Exception as e:
        logger.warning("musicbox /info failed for %s: %s: %s", guid, type(e).__name__, e)
        return None


@app.get("/music/api/v1/lyric/list")
@app.get("/music/api/v1/lyric/list/{subpath:path}")
async def lyric_list(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    return JSONResponse(content=build_lyric_list_payload(guid, lyric_text))


@app.get("/music/api/v1/track/lyrics")
@app.get("/music/api/v1/track/lyrics/{subpath:path}")
@app.get("/music/api/v1/detail/lyrics/{subpath:path}")
async def track_lyrics(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    if lyric_text:
        res = {"code": 0, "msg": "ok", "data": {"guid": guid, "lyric": lyric_text}}
        set_by_path(res, CONF["lyric_field"], lyric_text)
        return JSONResponse(content=res)
    return empty_ok()


@app.get("/music/api/v1/track/metadata")
@app.get("/music/api/v1/track/metadata/{subpath:path}")
@app.get("/music/api/v1/track/audio-info")
async def track_metadata(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    # 本地曲目（local:file:<sha1>）必须自己应答：官方后端不认识这个 guid，
    # 转发过去只回一堆空值，客户端就显示「有条目但没信息、点不开」。
    if str(guid or "").startswith("local:file:"):
        entry = local_files.entry_with_probe(guid)
        if not entry:
            # 索引没命中（清过缓存 / 换了运行目录）：再从当天歌单里反查一次路径，
            # 顺手补回索引，之后的请求就不必再走这条慢路。
            path = await _local_daily_path_of(request, guid)
            if path and os.path.isfile(path):
                stem = os.path.splitext(os.path.basename(path))[0]
                artist, title = "", stem
                if " - " in stem:
                    a, t = stem.split(" - ", 1)
                    if a.strip() and t.strip():
                        artist, title = a.strip(), t.strip()
                local_files.record_files([{
                    "path": path, "title": title, "artist": artist,
                    "ext": os.path.splitext(path)[1].lstrip(".").lower(),
                }])
                entry = local_files.entry_with_probe(guid)
        if entry:
            return JSONResponse(content=build_local_metadata_payload(guid, entry))
        logger.info("local metadata miss: %s（索引里没有或文件已不在）", guid)
        return JSONResponse(content=build_local_metadata_payload(guid, {}))
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    data = await _online_info(request, guid) or stub_online_info(guid)
    cached_lyric = read_lyric_cache(guid)
    if cached_lyric:
        data = {**data, "lyric": cached_lyric}
    elif data.get("lyric"):
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
    return JSONResponse(content=build_metadata_payload(guid, data))


# ---------------------------------------------------------------------------
# 本地每日推荐封面（现生成，零依赖）
# ---------------------------------------------------------------------------

_LOCAL_DAILY_COVER_CACHE: dict[int, bytes] = {}


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    import struct
    import zlib

    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _local_daily_cover_png(px: int = 300) -> bytes:
    """生成一张「唱片」图形封面：深蓝紫渐变底 + 白色唱片环。

    不引第三方库（PIL 在真机的 python312 环境里不保证有），直接按 PNG 规范
    用 zlib/struct 手搓；生成结果按尺寸内存缓存，一首歌单相一次。
    """
    import struct
    import zlib

    try:
        size = int(px)
    except Exception:  # noqa: BLE001
        size = 300
    size = max(96, min(512, size))
    hit = _LOCAL_DAILY_COVER_CACHE.get(size)
    if hit:
        return hit

    w = h = size
    cx = cy = (size - 1) / 2.0
    r_outer = size * 0.30
    r_inner = size * 0.115
    rows = bytearray()
    for y in range(h):
        rows.append(0)  # PNG filter type 0 (None)
        t = y / max(1, h - 1)
        base = (int(26 + 44 * t), int(30 + 28 * t), int(70 + 66 * t))
        for x in range(w):
            dx = x - cx
            dy = y - cy
            d = (dx * dx + dy * dy) ** 0.5
            if d <= r_inner:
                r, g, b = 248, 249, 252
            elif d <= r_outer:
                ring = (d - r_inner) / max(1e-6, r_outer - r_inner)
                k = 0.78 if int(ring * 6) % 2 else 0.42
                r = int(248 * k + base[0] * (1 - k))
                g = int(249 * k + base[1] * (1 - k))
                b = int(252 * k + base[2] * (1 - k))
            else:
                r, g, b = base
            rows += bytes((r, g, b))

    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += _png_chunk(b"IEND", b"")
    _LOCAL_DAILY_COVER_CACHE[size] = png
    return png


@app.api_route("/music/api/v1/static/cover", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/static/cover/{subpath:path}", methods=["GET", "HEAD"])
async def static_cover(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not guid and subpath.startswith("online:"):
        guid = subpath
    # 本地曲目封面：优先从音频文件里抽内嵌图（flac 的 picture 块 / ID3 APIC /
    # MP4 covr），没有内嵌图再找同目录的 cover.jpg 等，最后才用生成的唱片占位图。
    # 早期版本把 coverId=local:file:… 转发给官方后端，后端不认这个 guid 直接 400
    # ——整张歌单因此没封面，日志里就一行 400，很难联想到是转发造成的。
    if str(guid or "").startswith("local:file:"):
        found = local_files.cover(guid)
        if found:
            data, mime = found
            return Response(content=data, media_type=mime,
                            headers={"Cache-Control": "public, max-age=604800"})
        return Response(
            content=_local_daily_cover_png(cover_resize_px(request.query_params.get("size")) or 300),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    if dailyrec.is_local_daily_playlist_guid(guid):
        # 本地每日推荐封面：本地文件没有封面 URL，早期版本直接返回 404 让客户端用
        # 占位图。但真机上确实见过客户端因为歌单封面 404 而整条不渲染（日志里就是
        # 一行 404，界面上则是"歌单凭空消失"），所以这里干脆现生成一张 PNG 封面：
        # 零依赖（zlib+struct 手写 PNG）、按尺寸内存缓存，永远拿得到图。
        px = cover_resize_px(request.query_params.get("size")) or 300
        return Response(
            content=_local_daily_cover_png(px),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    if dailyrec.is_daily_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        cached = dailyrec.load_daily_cache(user_guid, dailyrec.today_key())
        tracks = (cached or {}).get("tracks") or []
        if tracks:
            first_guid = str(tracks[0].get("guid") or "")
            if is_online_guid(first_guid):
                guid = first_guid

    if playlists.is_channel_guid(guid):
        # ⚠️ 必须在下面的通用分支之前处理：像 online:playlist:ne:123 这种 guid 同样满足
        # is_online_guid()，而通用逻辑是 split(":")[-1] 取"歌曲 id"，会把 123 当成
        # song_id 去查 —— 结果是给歌单配上一首完全无关歌曲的封面。
        cover = await _channel_playlist_cover(request, guid)
    else:
        cover = ""
    if not cover and not playlists.is_channel_guid(guid):
        if not is_online_guid(guid):
            return await forward_to_upstream(request, get_upstream_client(request.app))
        cover = await _online_cover_url(request, guid)
    if not cover:
        # 退回元数据（可能是缓存里已有的整条记录），再取不到才 404
        data = None
        if is_online_guid(guid) and not playlists.is_channel_guid(guid):
            data = await _online_info(request, guid)
        cover = (data or {}).get("cover_url") or ""
    if not cover:
        # 无封面时返回 404，避免把 JSON 当成图片导致客户端裂图
        return Response(status_code=404)

    headers = {
        # 封面是静态资源，让客户端缓存住：列表滚动/来回切歌不再重复回源
        "Cache-Control": "public, max-age=86400",
    }
    # 由 NAS 代抓图片再回传，而不是 302 让浏览器直连网易云 CDN。
    # 302 依赖两件我们无法保证的事：客户端能直连 p1.music.126.net，且该 CDN
    # 不校验 Referer/Origin。任一不成立就表现为「列表里没有封面」。
    # 代抓失败时再退回 302，至少保留原来那条能走通的路。
    # v2.8.1：网易云系封面走 CDN 服务端缩图（?param=NyN）——原图几百 KB～1MB，
    # 一个列表首屏几十 MB 正是移动网络卡顿的主力；300px 缩图只有几十 KB。
    # 字节磁盘缓存按最终 URL 键控，不同尺寸各自缓存互不干扰。
    px = cover_resize_px(request.query_params.get("size"))
    fetch_url = resized_cover_url(cover, px)
    if fetch_url != cover:
        logger.info("cover resize %dx%d for %s", px, px, cover[:80])
    fetched = await _fetch_cover_bytes_cached(fetch_url)
    if fetched is None and fetch_url != cover:
        # 缩图 URL 失败（个别 CDN 节点不认参数）：再试一次原图
        fetched = await _fetch_cover_bytes_cached(cover)
    if fetched:
        content, ctype = fetched
        headers["Content-Type"] = ctype
        return Response(content=content, headers=headers)
    return RedirectResponse(cover, status_code=302, headers=headers)


# === online favorites ===

_FAV_LOCK = asyncio.Lock()


def sanitize_user_guid(guid: str | None) -> str:
    """过滤文件名合法字符 [A-Za-z0-9-_]，非法字符替换为 _；为空则返回 'shared'。"""
    raw = str(guid or "").strip()
    safe = re.sub(r"[^A-Za-z0-9\-_]", "_", raw)
    return safe or "shared"


def user_fav_path(user_guid: str) -> str:
    fav_dir = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
    safe_name = sanitize_user_guid(user_guid)
    return os.path.join(fav_dir, f"{safe_name}.json")


def load_online_favorites(user_guid: str) -> list[dict]:
    path = user_fav_path(user_guid)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning("Failed to load online favorites for %s from %s: %s", user_guid, path, e)
    return []


def save_online_favorites(user_guid: str, items: list[dict]) -> bool:
    path = user_fav_path(user_guid)
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
        return True
    except Exception as e:
        logger.warning("Failed to save online favorites for %s to %s: %s", user_guid, path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return False


def build_favorite_track_obj(guid: str, info: dict | None = None, created_at: int | None = None) -> dict:
    raw_info = dict(info or {})
    raw_info.setdefault("id", song_id_from_online_guid(guid))
    raw_info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(raw_info)

    now = int(time.time())
    ts = created_at or now

    artist_name = vo.get("artist") or ""
    artists_list = [
        {
            "guid": f"{guid}:artist",
            "name": artist_name,
            "coverId": guid,
            "createdAt": ts,
            "updatedAt": ts,
        }
    ] if artist_name else []

    album_name = vo.get("albumName") or (vo.get("album", {}).get("name") if isinstance(vo.get("album"), dict) else "") or ""
    album_obj = {
        "guid": f"{guid}:album",
        "name": album_name,
        "artists": artists_list,
        "coverId": guid,
        "releaseDate": 0,
        "barcode": "",
        "createdAt": ts,
        "updatedAt": ts,
    }

    audio_spec = vo.get("audioSpec") or {}

    return {
        "guid": guid,
        "title": vo.get("title") or "",
        "duration": vo.get("duration") or 0,
        "isFavorite": True,
        "isCue": False,
        "genres": [],
        "artists": artists_list,
        "album": album_obj,
        "audioSpec": audio_spec,
        "accessStatus": 0,
        "coverId": guid,
        "year": 0,
        "discNo": 1,
        "trackNo": 1,
        "isrc": "",
        "createdAt": ts,
        "updatedAt": ts,
    }


async def _probe_upstream_auth(request: Request, client: httpx.AsyncClient) -> tuple[bool, str, Response | None]:
    """向上游探测用户是否已登录。复用当前请求 headers。
    返回 (is_authed, user_guid, error_response)。
    """
    headers = copy_incoming_headers(request)
    try:
        probe_req = client.build_request("GET", "/music/api/v1/user/me", headers=headers)
        probe_resp = await client.send(probe_req)
        resp_headers = filter_headers(probe_resp.headers, exclude_keys={"content-length", "content-encoding"})

        if probe_resp.status_code == 401:
            return False, "", Response(
                content=probe_resp.content,
                status_code=401,
                headers=resp_headers,
                media_type=probe_resp.headers.get("content-type"),
            )

        if probe_resp.status_code == 200:
            try:
                probe_json = probe_resp.json()
                if isinstance(probe_json, dict) and probe_json.get("code") == 99999:
                    return False, "", JSONResponse(
                        content=probe_json,
                        status_code=200,
                        headers=resp_headers,
                    )
                if isinstance(probe_json, dict) and probe_json.get("code") == 0:
                    data = probe_json.get("data")
                    if isinstance(data, dict) and data.get("guid"):
                        return True, str(data["guid"]), None
                    logger.warning("user/me response missing data.guid, falling back to 'shared': %s", probe_json)
                    return True, "shared", None
            except Exception as e:
                logger.warning("Failed to parse user/me json response: %s", e)
                return True, "shared", None
            return True, "shared", None

        # 其他非 200/401 状态码，上游异常
        return True, "shared", None
    except Exception as e:
        logger.warning("Upstream auth probe failed: %s", e)
        # 探测异常时保守放行
        return True, "shared", None


@app.post("/music/api/v1/favorite-track/create")
async def favorite_track_create(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = str(body.get("trackGUID") or body.get("guid") or "").strip()

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    now = int(time.time())
    info = await _online_info(request, guid)
    if not info:
        cached_lyric = read_lyric_cache(guid)
        title = ""
        artist = ""
        cached_media = find_cache_file(guid)
        if cached_media:
            base = os.path.splitext(os.path.basename(cached_media))[0]
            if " - " in base:
                artist, title = base.split(" - ", 1)
            else:
                title = base
        info = {
            "id": song_id_from_online_guid(guid),
            "source": source_from_online_guid(guid),
            "title": title,
            "artist": artist,
            "lyric": cached_lyric,
        }

    track_obj = build_favorite_track_obj(guid, info, created_at=now)

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            # 查重
            idx = next((i for i, it in enumerate(items) if it.get("guid") == guid), None)
            if idx is not None:
                # 幂等更新
                items[idx]["track"] = track_obj
            else:
                items.append({
                    "guid": guid,
                    "createdAt": now,
                    "track": track_obj,
                })
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error updating online favorites for user %s: %s", user_guid, e)

    # 2) 收藏同步回网易云（加红心）+ 3) 归档下载。两者都只能尽力而为：
    #    本地收藏已经写成功了，任何一步失败都不该让这个接口报错——
    #    否则用户点一下收藏就看到红叉，反而把本来能用的功能弄坏。
    mb_client = get_musicbox_client(request.app)
    sid = _netease_song_id(guid)
    if sid:
        like_res = await _sync_netease_like(mb_client, sid, like=True)
        if not like_res.get("ok"):
            # 不静默：红心没加上必须留痕，否则用户以为同步了其实没有
            logger.warning("netease like not applied for song %s: %s", sid, like_res)
        if downloader.download_enabled():
            state = downloader.enqueue(
                mb_client, sid,
                {"title": (info or {}).get("title") or "",
                 "artist": (info or {}).get("artist") or "",
                 "album": (info or {}).get("album") or ""},
                ref_writer=lambda path: remember_archive_path(guid, path),
            )
            logger.info("archive enqueued for song %s (%s)", sid, state)
    # ⚠️ data 必须保持 None：这是飞牛官方接口的响应形状，既有客户端按它解析。
    #    归档/红心的执行结果走日志，不要为了"回传细节"去改线上协议。
    return JSONResponse(content={"code": 0, "msg": "", "data": None})


def _netease_song_id(guid: str) -> str:
    """从在线 guid 里取出**纯数字**的网易云歌曲 id；不是网易云来源则返回空。

    两个必须挡住的坑（都是既有测试当场抓到的）：
    1. ``song_id_from_online_guid`` 返回的是 ``netease:228908`` 这种带来源的串，
       直接 ``int()`` 会 ValueError —— 项目里其它地方一律再 ``.split(":")[-1]``。
    2. guid 也可能是 ``online:kuwo:123`` 这类**非网易云**来源，绝不能拿去加红心，
       那是往用户网易云账号里写一条不相干的歌。
    """
    g = str(guid or "")
    if not is_online_guid(g):
        return ""
    src = source_from_online_guid(g)
    if src and src != NETEASE_SOURCE:
        return ""
    sid = song_id_from_online_guid(g).split(":")[-1].strip()
    return sid if sid.isdigit() else ""


async def _sync_netease_like(client, song_id: str, like: bool) -> dict[str, Any]:
    """收藏 <-> 网易云红心。

    这是**对用户账号的写操作**，因此不静默失败：结果如实回传给接口与日志，
    管理页能据此看出到底是没登录、上游拒绝、还是取消红心那条未验证分支不通。
    """
    if not downloader.like_sync_enabled():
        return {"ok": False, "skipped": "disabled"}
    if not str(song_id).isdigit():
        return {"ok": False, "error": "invalid_song_id"}
    try:
        r = await client.post(f"/api/v1/song/{int(song_id)}/like",
                              params={"like": "true" if like else "false"}, timeout=15.0)
        if r.status_code != 200:
            return {"ok": False, "error": f"http_{r.status_code}"}
        body = r.json()
        if not isinstance(body, dict):
            return {"ok": False, "error": "bad_payload"}
        if body.get("ok") is not True:
            return {"ok": False, "error": str(body.get("error") or "upstream_rejected")}
        return {"ok": True, "like": like}
    except Exception as exc:  # noqa: BLE001
        logger.warning("netease like sync failed song=%s like=%s: %s: %s",
                       song_id, like, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


@app.post("/music/api/v1/favorite-track/delete")
async def favorite_track_delete(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = str(body.get("trackGUID") or body.get("guid") or "").strip()

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            items = [it for it in items if it.get("guid") != guid]
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error deleting from online favorites for user %s: %s", user_guid, e)

    # 取消收藏同步撤销网易云红心（用户选的口径是双向同步）。
    # 归档文件不删：那是用户主动要求下载到自定义目录的资产，取消收藏不等于要删文件。
    sid = _netease_song_id(guid)
    if sid:
        res = await _sync_netease_like(get_musicbox_client(request.app), sid, like=False)
        if not res.get("ok"):
            logger.warning("netease unlike not applied for song %s: %s", sid, res)
    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.get("/music/api/v1/favorite-track/list")
async def favorite_track_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    # 探测当前用户身份
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    # 成功获取官方列表，合并本地在线收藏
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        upstream_json["data"] = data

    official_list = data.get("list")
    if not isinstance(official_list, list):
        official_list = []
        data["list"] = official_list

    # 飞牛音乐前端收藏列表依赖 isFavorite=True 状态判断，遍历补齐官方列表中可能缺失的字段
    for item in official_list:
        if isinstance(item, dict):
            item["isFavorite"] = True

    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official_list)

    async with _FAV_LOCK:
        try:
            fav_items = load_online_favorites(user_guid)
        except Exception as e:
            logger.warning("Error reading online favorites for list for user %s: %s", user_guid, e)
            fav_items = []

    # 按 createdAt 倒序
    fav_items_sorted = sorted(fav_items, key=lambda x: x.get("createdAt", 0), reverse=True)
    online_tracks = []
    for it in fav_items_sorted:
        t = it.get("track")
        if isinstance(t, dict):
            # 确保关键属性为最新或格式完整
            t["isFavorite"] = True
            online_tracks.append(t)
        else:
            g = it.get("guid") or ""
            if g:
                online_tracks.append(build_favorite_track_obj(g, created_at=it.get("createdAt")))

    data["list"] = official_list + online_tracks
    data["total"] = official_total + len(online_tracks)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


# === daily recommend + play history ===

_HISTORY_LOCK = asyncio.Lock()
_DAILY_TASKS: dict[str, asyncio.Task] = {}


def _prune_stale_daily_tasks(day: str) -> None:
    suffix = f":{day}"
    stale = [k for k in list(_DAILY_TASKS) if not str(k).endswith(suffix)]
    for k in stale:
        old = _DAILY_TASKS.pop(k, None)
        if old is not None and not old.done():
            old.cancel()


async def _ensure_daily_task(request: Request, user_guid: str) -> asyncio.Task:
    """当天每日推荐歌单的构建任务（按 user_guid + 日期去重，单飞）。

    日推内容来自网易云官方接口，因此未登录时构建会立刻返回空 bundle；
    此时任务结果不缓存复用，等用户扫码登录后下次请求自然重建。
    """
    day = dailyrec.today_key()
    _prune_stale_daily_tasks(day)
    key = f"{user_guid}:{day}"
    task = _DAILY_TASKS.get(key)
    if task is not None and not task.done():
        return task
    if task is not None and task.done():
        try:
            if task.exception() is None:
                result = task.result()
                if isinstance(result, dict) and result.get("tracks"):
                    return task
        except (asyncio.CancelledError, Exception):
            pass

    task = asyncio.create_task(
        dailyrec.get_or_build_daily(
            user_guid=user_guid,
            musicbox_client=get_musicbox_client(request.app) if CONF["netease_enabled"] else None,
            build_track=build_online_track,
            limit=CONF["daily_limit"],
        )
    )
    _DAILY_TASKS[key] = task
    return task


async def _peek_daily_bundle(request: Request, user_guid: str) -> dict:
    """歌单列表用：有缓存立刻返回；否则后台生成，最多等 2s，超时仍返回占位歌单。"""
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached
    task = await _ensure_daily_task(request, user_guid)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except asyncio.TimeoutError:
        cached = dailyrec.load_daily_cache(user_guid, day)
        if cached and cached.get("tracks"):
            return cached
        return dailyrec.empty_daily_bundle(user_guid)
    except Exception as e:
        logger.warning("daily recommend peek failed: %s", e)
        return dailyrec.empty_daily_bundle(user_guid)


async def _load_daily_bundle(request: Request, user_guid: str) -> dict:
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached

    task = await _ensure_daily_task(request, user_guid)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=20.0)
    except asyncio.TimeoutError:
        cached = dailyrec.load_daily_cache(user_guid, day)
        if cached and cached.get("tracks"):
            return cached
        return dailyrec.empty_daily_bundle(user_guid)


def _load_local_daily_bundle(user_guid: str) -> dict:
    """本地每日推荐（同步，只扫本地磁盘，零网络）。失败/为空时 tracks=[]。"""
    try:
        return dailyrec.get_or_build_local_daily(user_guid, detect_library_dir())
    except Exception as exc:  # noqa: BLE001 - 本地日推失败不影响其它注入
        logger.warning("local daily bundle failed: %s: %s", type(exc).__name__, exc)
        return dailyrec.empty_local_daily_bundle(user_guid)


async def _local_daily_path_of(request: Request, guid: str) -> str | None:
    """在（当前用户的）本地日推 bundle 里反查曲目 guid 对应的磁盘路径。"""
    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, _resp = await _probe_upstream_auth(request, upstream_client)
    user = user_guid if is_authed else "shared"
    for _day_offset in range(2):   # 今天 + 昨天（跨零点后旧页仍在用昨天的 guid）
        day = dailyrec.today_key()
        if _day_offset == 1:
            import datetime as _dt
            day = (_dt.datetime.now() - _dt.timedelta(days=1)).strftime("%Y%m%d")
        for bundle in (
            dailyrec.load_local_daily_cache(user, day) or {},
            _load_local_daily_bundle(user),
        ):
            for t in bundle.get("tracks") or []:
                if isinstance(t, dict) and str(t.get("guid") or "") == guid:
                    return str(t.get("_local_path") or "") or None
            if dailyrec.load_local_daily_cache(user, day):
                break
    return None


async def _netease_logged_in() -> bool:
    """当前网易云账号是否已登录（走带 TTL 的登录态缓存，不额外打上游）。"""
    try:
        # ⚠️ 必须用 get_musicbox_client(app)：这里曾误写成不存在的 musicbox_client()，
        # NameError 被本函数自己的 except 吞掉后恒返回 False —— 代理侧于是永远
        # 认为「未登录」，我的歌单 / 推荐歌单 / 私人FM 这些需登录口径一颗都不会注入。
        return bool((await netease_auth.fetch_state(get_musicbox_client(app))).logged_in)
    except Exception as exc:  # noqa: BLE001
        logger.warning("login state probe failed: %s: %s", type(exc).__name__, exc)
        return False


# ---------------------------------------------------------------------------
# 口径清单短缓存（v2.7）：playlist_list / preview 共用。
#
# 每次打开飞牛歌单列表都要拉一遍全部启用口径（toplists / category / mine…），
# 各自一次跨洋往返，快慢取决于网易云当时的抖动——这正是「榜单打开速度不稳定」
# 的来源。清单内容（榜单名/封面/曲目数）本身变化极慢，缓存 + 过期后台刷新
# （stale-while-revalidate）后：TTL 内的打开零上游往返，过期后的打开也先用
# 上一份立即返回。0 = 关闭（回到每次实拉）。
# ---------------------------------------------------------------------------

_CHANNEL_LIST_CACHE_TTL = _float("FNMUSIC_CHANNEL_LIST_CACHE_TTL", 300.0)
_channel_recs_cache: dict[tuple, dict] = {}
_channel_recs_refresh: "dict[tuple, asyncio.Task]" = {}


def _channel_recs_cache_key() -> tuple:
    return (playlists.channels_enabled(), playlists.category_name(), playlists.channel_limit())


async def _collect_channel_records(client, key: tuple):
    logged_in = await _netease_logged_in()
    recs, keep, complete = await playlists.collect_records(client, logged_in)
    _channel_recs_cache[key] = {
        "ts": time.time(),
        "records": recs,
        "keep": keep,
        "complete": complete,
    }
    return recs, keep, complete


def _schedule_channel_recs_refresh(client, key: tuple) -> None:
    """后台单飞刷新某个 key 的口径清单（stale-while-revalidate 的后台半边）。

    ``client`` 是进程级共享的 musicbox client（app.state），后台任务用它很安全。
    """
    existing = _channel_recs_refresh.get(key)
    if existing is not None and not existing.done():
        return

    async def _job() -> None:
        try:
            await _collect_channel_records(client, key)
        except Exception as exc:  # noqa: BLE001 - 刷新失败保留旧缓存
            logger.warning("口径清单后台刷新失败: %s: %s", type(exc).__name__, exc)
        finally:
            _channel_recs_refresh.pop(key, None)

    _channel_recs_refresh[key] = asyncio.create_task(_job())


async def _channel_playlist_records(client) -> tuple[list[dict], set[str], bool]:
    """按管理页勾选的口径拉取要注入的伪歌单清单（带短缓存 + 后台刷新）。"""
    if _CHANNEL_LIST_CACHE_TTL <= 0:
        logged_in = await _netease_logged_in()
        return await playlists.collect_records(client, logged_in)

    key = _channel_recs_cache_key()
    hit = _channel_recs_cache.get(key)
    if hit is not None:
        if (time.time() - hit["ts"]) < _CHANNEL_LIST_CACHE_TTL:
            return hit["records"], hit["keep"], hit["complete"]
        # 过期：先返回旧值（列表页要快），后台单飞刷新
        _schedule_channel_recs_refresh(client, key)
        return hit["records"], hit["keep"], hit["complete"]
    return await _collect_channel_records(client, key)


# ---------------------------------------------------------------------------
# 歌单缓存预热（v2.6）
#
# 两条触发路径，共用同一把「正在预热」闸门：
#   1. 定时：每天 FNMUSIC_PLAYLIST_REFRESH_AT（默认 04:30）全量刷新；
#   2. 打开歌单列表后自动预热一次（限流：冷却期内不重复），让「点开每个
#      歌单都是秒开」在第一次打开列表后很快成立，而不要求用户先挨个点一遍。
# 管理页「预热歌单缓存」按钮走 /_ext/playlists/warm，不受冷却限制。
# ---------------------------------------------------------------------------

_PLAYLIST_WARMING = False
_LAST_AUTO_WARM_AT = 0.0


def _playlist_warm_cooldown() -> float:
    """自动预热的冷却时间：与缓存 TTL 对齐（TTL 内的缓存本来就新鲜，无需预热）。"""
    return float(playlists.tracks_cache_ttl())


async def _warm_playlist_caches(fastapi_app: FastAPI, guids: "list[str] | None" = None,
                                skip_fresh_s: float = 0.0) -> dict:
    """后台刷新歌单曲目缓存。串行 + 每个之间歇 1s，对上游友好。

    ``guids=None``（定时刷新）时**现场拉取当前口径的歌单清单**再预热，
    绝不能直接用注册表全部条目——注册表里可能留着历史口径/旧分类的死条目
    （complete=False 时按设计不清），每天把它们全量拉一遍纯属浪费上游配额
    （真机上出现过注册表 59 条、当前在列仅 34 条，07:15 定时刷新白刷 25 个）。
    顺带在清单完整时 forget_stale，把死条目连同其曲目缓存一起清掉。
    ``guids`` 由调用方给出（playlist_list 已拿到当前清单）时直接用，不重复拉。

    ``skip_fresh_s``（秒）：**跳过缓存仍新鲜的歌单**（v2.8.2）。定时刷新与
    自动预热传 入该阈值——15 分钟前刚刷过的缓存内容就是新的，再拉一遍上游
    纯属浪费（真机现象：07:00 手动预热 33/33，07:15 定时任务又把 33 个全部
    重刷）。手动按钮传 0（用户按了按钮就是要全量刷新）。
    """
    global _PLAYLIST_WARMING
    if _PLAYLIST_WARMING:
        return {"started": False, "reason": "already_running"}
    _PLAYLIST_WARMING = True
    try:
        client = get_musicbox_client(fastapi_app)
        if guids is None:
            try:
                # 走 _collect_channel_records 而不是裸 collect_records：
                # 顺带把口径清单 SWR 缓存也刷新成最新一届，次日早晨的列表页直接命中
                recs, keep, complete = await _collect_channel_records(
                    client, _channel_recs_cache_key())
            except Exception as exc:  # noqa: BLE001 - 清单拉不到就别预热了
                logger.warning("预热前拉取当前歌单清单失败: %s: %s", type(exc).__name__, exc)
                recs, keep, complete = [], set(), False
            if complete and keep:
                playlists.forget_stale(keep)
            guids = [str(r.get("guid") or "") for r in recs if r.get("guid")]

        refreshed = 0
        skipped_fresh = 0
        for guid in guids:
            if skip_fresh_s > 0:
                hit = playlists.load_cached_tracks(guid)
                if hit is not None and (time.time() - hit[0]) < skip_fresh_s:
                    skipped_fresh += 1
                    continue
            try:
                items = await _fetch_channel_tracks(client, guid)
                if items:
                    playlists.store_cached_tracks(guid, items)
                    refreshed += 1
            except Exception as exc:  # noqa: BLE001 - 单个失败不挡后面
                logger.warning("预热歌单 %s 失败: %s: %s", guid, type(exc).__name__, exc)
            await asyncio.sleep(1.0)
        logger.info("歌单缓存预热完成：刷新 %d、跳过（仍新鲜）%d、共 %d 个",
                    refreshed, skipped_fresh, len(guids))
        return {"started": True, "refreshed": refreshed,
                "skipped_fresh": skipped_fresh, "total": len(guids)}
    finally:
        _PLAYLIST_WARMING = False


def _warm_skip_fresh_s() -> float:
    """定时刷新/自动预热跳过「仍新鲜」缓存的阈值（秒）；0 = 一律刷新。

    默认 3600：一小时内刚刷过的缓存内容就是新的，重拉纯属浪费上游配额。
    手动按钮不受此限（按下按钮就是明确要求全量刷新）。
    """
    return max(0.0, _float("FNMUSIC_WARM_SKIP_FRESH_S", 3600.0))


def _schedule_playlist_warm(fastapi_app: FastAPI, *, force: bool = False,
                            guids: "list[str] | None" = None,
                            skip_fresh_s: float = 0.0) -> bool:
    """安排一次后台预热。返回是否真的安排了（已在跑/冷却期内则 False）。

    ``guids``：已知当前在列歌单时直接传入（省一次清单拉取）；
    缺省由预热任务自己现场拉当前清单（见 _warm_playlist_caches）。
    ``skip_fresh_s``：>0 时跳过缓存仍新鲜的歌单（定时刷新/自动预热用）。
    """
    global _LAST_AUTO_WARM_AT
    if _PLAYLIST_WARMING:
        return False
    if not force:
        if (time.time() - _LAST_AUTO_WARM_AT) < _playlist_warm_cooldown():
            return False
        _LAST_AUTO_WARM_AT = time.time()

    async def _job() -> None:
        await _warm_playlist_caches(fastapi_app, guids=guids, skip_fresh_s=skip_fresh_s)

    asyncio.create_task(_job())
    return True


async def _playlist_refresh_loop(fastapi_app: FastAPI, stop_event: asyncio.Event) -> None:
    """每日定时全量刷新歌单缓存（管理页可配置时间，留空关闭）。"""
    while not stop_event.is_set():
        delay = playlists.seconds_until_daily_refresh()
        if delay is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3600.0)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        logger.info("定时刷新歌单缓存开始（%s）", playlists.refresh_time_of_day())
        # 只补缺的/过新的：一小时内刚刷过的缓存直接跳过（见 _warm_skip_fresh_s）
        _schedule_playlist_warm(fastapi_app, force=True, skip_fresh_s=_warm_skip_fresh_s())
        # 等一小会儿让本轮跑起来；下轮循环会重新计算明天的触发时间
        await asyncio.sleep(5.0)


def _channel_public_fields(rec: dict) -> dict:
    """伪歌单 -> 飞牛歌单列表条目形状。

    与每日推荐的区别：``isDaily`` 必须为 False（飞牛会对 isDaily 的条目做特殊
    的"今日推荐"渲染），且封面走 coverId=guid 由 /static/cover 拦截。
    """
    now = int(time.time())
    return {
        "guid": rec.get("guid"),
        "name": rec.get("name") or "网易云歌单",
        "coverId": rec.get("guid"),
        "cover_url": rec.get("cover_url") or "",
        "coverUrl": rec.get("cover_url") or "",
        "createdAt": int(rec.get("createdAt") or now),
        "updatedAt": int(rec.get("updatedAt") or now),
        "trackCount": int(rec.get("track_count") or 0),
        "isDaily": False,
        "source": "netease",
        "channel": rec.get("channel") or "",
    }


async def _fetch_channel_tracks(client: httpx.AsyncClient, guid: str) -> list[dict]:
    """现场拉取伪歌单曲目（完整上游链路，慢——打开热路径别直接用它）。"""
    return await playlists.resolve_track_items(
        client, guid, netease_items.map_netease_song, _enrich_netease_items
    )


# 单飞刷新：同一 guid 的后台刷新任务全进程只允许一个在跑
_TRACK_REFRESH_TASKS: "dict[str, asyncio.Task]" = {}


def _schedule_track_refresh(fastapi_app: FastAPI, guid: str) -> None:
    """后台刷新某个歌单的曲目缓存（stale-while-revalidate 的 revalidate 半边）。

    刷新失败或拉到空列表时**保留旧缓存**：上游一次抖动不该把还能用的旧数据
    覆盖成空的。异常只记日志，绝不影响任何在线请求。
    """
    existing = _TRACK_REFRESH_TASKS.get(guid)
    if existing is not None and not existing.done():
        return

    async def _job() -> None:
        try:
            items = await _fetch_channel_tracks(get_musicbox_client(fastapi_app), guid)
            if items:
                playlists.store_cached_tracks(guid, items)
                logger.info("歌单缓存已后台刷新：%s（%d 首）", guid, len(items))
        except Exception as exc:  # noqa: BLE001
            logger.warning("歌单缓存后台刷新失败 %s: %s: %s",
                           guid, type(exc).__name__, exc)
        finally:
            _TRACK_REFRESH_TASKS.pop(guid, None)

    _TRACK_REFRESH_TASKS[guid] = asyncio.create_task(_job())


async def _channel_tracks_items(fastapi_app: FastAPI, guid: str) -> list[dict]:
    """伪歌单曲目（内部条目形态），带 stale-while-revalidate 缓存。

    - 命中且新鲜（TTL 内）→ 直接返回，零上游往返，**这就是打开变快的全部**；
    - 命中但过新鲜期 → 先返回旧值（打开永远快），后台单飞刷新；
    - 未命中（首次打开）→ 现场拉取并落盘，之后的打开都走缓存。
    """
    hit = playlists.load_cached_tracks(guid)
    if hit is not None:
        ts, items = hit
        if (time.time() - ts) > playlists.tracks_cache_ttl():
            _schedule_track_refresh(fastapi_app, guid)
        return items
    items = await _fetch_channel_tracks(get_musicbox_client(fastapi_app), guid)
    playlists.store_cached_tracks(guid, items)
    return items


async def _channel_tracks(request: Request, guid: str, limit: int = 0) -> list[dict]:
    """伪歌单 -> 飞牛 track 对象列表（已补封面、已按可播性过滤、走缓存）。"""
    items = await _channel_tracks_items(request.app, guid)
    if limit and limit > 0:
        items = items[:limit]
    return [build_online_track(it) for it in items]


async def _channel_playlist_cover(request: Request, guid: str) -> str:
    """伪歌单封面：注册表优先，缺失时回落到第一首歌的封面（并回填注册表）。

    排行榜口径上游**不给** coverImgUrl，只能借用榜单第一首歌的专辑封面。这一步
    要拉取曲目列表、代价不小，因此拿到后必须写回注册表，让它整个进程只发生一次。
    """
    reg = playlists.lookup(guid)
    cover = str(reg.get("cover_url") or "")
    if cover:
        return cover
    try:
        tracks = await _channel_tracks(request, guid, limit=1)
        first = str((tracks[0] or {}).get("coverUrl") or "") if tracks else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("channel cover fallback failed for %s: %s: %s",
                       guid, type(exc).__name__, exc)
        return ""
    if first:
        playlists.remember({"guid": guid, "name": reg.get("name", ""), "cover_url": first,
                            "track_count": reg.get("track_count", 0),
                            "channel": reg.get("channel", "")})
        playlists.save_registry()
    return first


def _playlist_public_fields(record: dict) -> dict:
    return {
        "guid": record.get("guid"),
        "name": record.get("name") or "每日推荐",
        "coverId": record.get("coverId") or record.get("guid"),
        "createdAt": int(record.get("createdAt") or time.time()),
        "updatedAt": int(record.get("updatedAt") or time.time()),
        "trackCount": int(record.get("trackCount") or 0),
        "isDaily": True,
    }


@app.get("/music/api/v1/playlist/list")
@app.get("/music/api/v1/playlist/list/{subpath:path}")
async def playlist_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    try:
        bundle = await _peek_daily_bundle(request, user_guid)
    except Exception as e:
        logger.warning("daily recommend list inject failed: %s", e)
        bundle = {}

    tracks = bundle.get("tracks") or []
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official

    # 先剔掉可能残留的旧伪歌单条目（例如取消了某个口径、昨天登录今天掉线）
    official = [
        it for it in official
        if not (isinstance(it, dict) and (
            dailyrec.is_daily_playlist_guid(str(it.get("guid") or ""))
            or dailyrec.is_local_daily_playlist_guid(str(it.get("guid") or ""))
            or playlists.is_channel_guid(str(it.get("guid") or ""))))
    ]

    # 更多口径的歌单（我的歌单/推荐歌单/排行榜/分类歌单/新碟/私人FM）
    try:
        channel_recs, keep, complete = await _channel_playlist_records(
            get_musicbox_client(request.app))
        # 清单不完整时（未登录 / 某口径抛异常）绝不能清注册表：否则用户只是掉线一次，
        # 所有歌单的名字与封面缓存就被抹掉了，恢复登录后全都要重新拉一遍。
        if complete:
            playlists.forget_stale(keep)
    except Exception as e:
        logger.warning("channel playlist inject failed: %s: %s", type(e).__name__, e)
        channel_recs = []

    # 组装注入头部：每日推荐 + 各口径伪歌单，按管理页配置的「大类顺序」排列。
    # daily 也参加排序（默认在最前）；随后统一盖上互不相同且递增的展示时间戳
    # （基准远早于本地歌单，客户端升序排序时注入条目整体在前、顺序=注入顺序）。
    stamped_order = playlists.channel_order()
    head_items: list[tuple[str, dict]] = []
    if tracks:
        rec = _playlist_public_fields(bundle.get("playlist") or {})
        rec["trackCount"] = len(tracks)
        head_items.append(("daily", rec))
    elif str(bundle.get("reason") or ""):
        logger.info("daily playlist not injected for %s: %s", user_guid[:8], bundle.get("reason"))

    # 本地每日推荐（v2.9）：与网易云日导独立，无需登录；失败时 tracks 为空自然不注入
    try:
        local_bundle = _load_local_daily_bundle(user_guid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("local daily inject failed: %s", exc)
        local_bundle = {}
    local_tracks = local_bundle.get("tracks") or []
    if local_tracks:
        local_rec = _playlist_public_fields(local_bundle.get("playlist") or {})
        local_rec["name"] = dailyrec.local_daily_playlist_name(local_bundle.get("day") or dailyrec.today_key())
        local_rec["trackCount"] = len(local_tracks)
        local_rec["isDaily"] = False
        local_rec["source"] = "local"
        head_items.append(("localdaily", local_rec))
    else:
        # 不注入的原因必须落日志：真机最常见的两类是「开关关着」和「曲库没扫到
        # 文件」，两者在界面上都表现为「歌单不出现」，没有日志就只能靠猜。
        logger.info("本地每日推荐未注入: reason=%s library=%s user=%s",
                    str(local_bundle.get("reason") or "unknown"),
                    detect_library_dir(), user_guid[:8])

    for r in channel_recs:
        ch = str(r.get("channel") or "")
        head_items.append((ch if ch in stamped_order else "category", _channel_public_fields(r)))

    # 稳定排序：同口径内部保持上游顺序（如「我的歌单」里自建在前、收藏在后）
    head_items.sort(key=lambda pair: stamped_order.index(pair[0])
                    if pair[0] in stamped_order else len(stamped_order))
    # 手动顺序（管理页「歌单顺序」卡片保存的 token 列表）整体覆盖大类顺序；
    # 没排到的新歌单按大类相对顺序跟在后面。实时读 .env，保存后立即生效。
    head = playlists.apply_explicit_order([it for _ch, it in head_items])
    # v2.9.8：本地每日推荐的 guid 天天变（online:playlist:localdaily:{日}:{用户}），
    # apply_explicit_order 认不出它，会当成"没排到的新歌单"统一甩到列表末尾——
    # 哪怕大类顺序里它明明排第一。这里做最后一道兜底，把它放回该在的位置。
    head = playlists.pin_local_daily_first(head)
    head = playlists.stamp_display_order(head)

    # 列表注入完成后安排一次后台预热（冷却期 = 缓存 TTL）：用户打开飞牛音乐
    # 看一眼歌单列表，几秒后所有歌单的曲目缓存就都在本地了——之后点开
    # 任何一个都是秒开，而不要求先挨个点一遍。清单用刚拿到的这一届，
    # 不再让预热任务重复拉一遍（也绝不会碰到注册表里的历史死条目）。
    if head:
        _schedule_playlist_warm(
            request.app, skip_fresh_s=_warm_skip_fresh_s(),
            guids=[str(r.get("guid") or "") for r in channel_recs if r.get("guid")])

    data["list"] = head + official
    data["total"] = len(head) + len(official)
    return JSONResponse(content=envelope, headers=headers)


@app.get("/music/api/v1/playlist/detail")
async def playlist_detail(request: Request):
    guid = str(request.query_params.get("guid") or "").strip()
    if playlists.is_channel_guid(guid):
        reg = playlists.lookup(guid)
        rec = {
            "guid": guid,
            "name": reg.get("name") or f"网易云歌单 {playlists._target_id(guid) or ''}".strip(),
            "cover_url": reg.get("cover_url") or "",
            "track_count": reg.get("track_count") or 0,
            "channel": reg.get("channel") or playlists.channel_of(guid),
            "createdAt": reg.get("ts") or int(time.time()),
            "updatedAt": reg.get("ts") or int(time.time()),
        }
        return JSONResponse(content={"code": 0, "msg": "ok", "data": _channel_public_fields(rec)})
    if dailyrec.is_local_daily_playlist_guid(guid):
        upstream_client = get_upstream_client(request.app)
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if not is_authed and auth_resp is not None:
            return auth_resp
        local_bundle = _load_local_daily_bundle(user_guid)
        rec = _playlist_public_fields(local_bundle.get("playlist") or {})
        rec["name"] = dailyrec.local_daily_playlist_name(local_bundle.get("day") or dailyrec.today_key())
        rec["trackCount"] = len(local_bundle.get("tracks") or [])
        reg_ts = playlists.lookup(guid).get("ts")
        if reg_ts:
            rec["createdAt"] = int(reg_ts)
            rec["updatedAt"] = int(reg_ts)
        return JSONResponse(content={"code": 0, "msg": "ok", "data": rec})
    if not dailyrec.is_daily_playlist_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid)
    rec = _playlist_public_fields(bundle.get("playlist") or {})
    rec["trackCount"] = len(bundle.get("tracks") or [])
    # 时间戳必须与列表页同一份（客户端按它排序）：列表页注入时会把每条的展示
    # 时间戳写进注册表，这里取注册表的值；取不到（还没请求过列表）才用 bundle 的。
    reg_ts = playlists.lookup(guid).get("ts")
    if reg_ts:
        rec["createdAt"] = int(reg_ts)
        rec["updatedAt"] = int(reg_ts)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": rec})


@app.get("/music/api/v1/playlist/batch-detail")
async def playlist_batch_detail(request: Request):
    raw = request.query_params.get("guids") or request.query_params.get("guid") or ""
    guids = [g.strip() for g in raw.split(",") if g.strip()]

    def _is_mine(g: str) -> bool:
        return (dailyrec.is_daily_playlist_guid(g)
                or dailyrec.is_local_daily_playlist_guid(g)
                or playlists.is_channel_guid(g))

    mine_ids = [g for g in guids if _is_mine(g)]
    if not mine_ids:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    rest = [g for g in guids if not _is_mine(g)]
    official_list: list = []
    if rest:
        headers = copy_incoming_headers(request)
        req = upstream_client.build_request(
            "GET",
            f"/music/api/v1/playlist/batch-detail?guids={quote(','.join(rest), safe=',')}",
            headers=headers,
        )
        resp = await upstream_client.send(req)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("code") == 0:
                    data = payload.get("data") or {}
                    if isinstance(data, dict) and isinstance(data.get("list"), list):
                        official_list = data["list"]
                    elif isinstance(data, list):
                        official_list = data
            except Exception:
                official_list = []

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    out: list[dict] = []
    daily_rec: dict | None = None
    local_daily_rec: dict | None = None
    for g in mine_ids:
        if playlists.is_channel_guid(g):
            reg = playlists.lookup(g)
            out.append(_channel_public_fields({
                "guid": g,
                "name": reg.get("name") or f"网易云歌单 {playlists._target_id(g)}".strip(),
                "cover_url": reg.get("cover_url") or "",
                "track_count": reg.get("track_count") or 0,
                "channel": reg.get("channel") or playlists.channel_of(g),
                "createdAt": reg.get("ts"), "updatedAt": reg.get("ts"),
            }))
        elif dailyrec.is_local_daily_playlist_guid(g):
            if local_daily_rec is None:
                local_bundle = _load_local_daily_bundle(user_guid)
                local_daily_rec = _playlist_public_fields(local_bundle.get("playlist") or {})
                local_daily_rec["name"] = dailyrec.local_daily_playlist_name(
                    local_bundle.get("day") or dailyrec.today_key())
                local_daily_rec["trackCount"] = len(local_bundle.get("tracks") or [])
                reg_ts = playlists.lookup(g).get("ts")
                if reg_ts:
                    local_daily_rec["createdAt"] = int(reg_ts)
                    local_daily_rec["updatedAt"] = int(reg_ts)
            out.append(local_daily_rec)
        elif daily_rec is None:
            # 每日推荐只解析一次；同一次批量请求里重复的日推 guid 复用结果
            bundle = await _load_daily_bundle(request, user_guid)
            daily_rec = _playlist_public_fields(bundle.get("playlist") or {})
            daily_rec["trackCount"] = len(bundle.get("tracks") or [])
            # 与列表页同一份展示时间戳（客户端按它排序），见 playlist_detail 同款注释
            reg_ts = playlists.lookup(g).get("ts")
            if reg_ts:
                daily_rec["createdAt"] = int(reg_ts)
                daily_rec["updatedAt"] = int(reg_ts)
            out.append(daily_rec)
        else:
            out.append(daily_rec)
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"list": out + official_list}})


@app.get("/music/api/v1/track/playlist-detail/list")
async def playlist_track_list(request: Request):
    guid = str(
        request.query_params.get("playlistGUID")
        or request.query_params.get("playlistGuid")
        or request.query_params.get("guid")
        or ""
    ).strip()
    is_channel = playlists.is_channel_guid(guid)
    is_local_daily = dailyrec.is_local_daily_playlist_guid(guid)
    if not is_channel and not is_local_daily and not dailyrec.is_daily_playlist_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    started = time.monotonic()
    cache_state = "hit"
    if is_local_daily:
        # 本地每日推荐：缓存即磁盘 bundle（当天稳定），构建只扫本地零网络
        local_bundle = _load_local_daily_bundle(user_guid)
        tracks = dailyrec.stamp_playlist_tracks(list(local_bundle.get("tracks") or []))
        # 内部字段（_local_path 等）不能进对外应答
        for t in tracks:
            t.pop("_local_path", None)
        cache_state = "local"
    elif is_channel:
        try:
            _hit = playlists.load_cached_tracks(guid)
            if _hit is None:
                cache_state = "miss"
            tracks = dailyrec.stamp_playlist_tracks(await _channel_tracks(request, guid))
        except Exception as e:
            logger.warning("channel playlist tracks failed for %s: %s: %s",
                           guid, type(e).__name__, e)
            return _online_unavailable("netease playlist unavailable", code=502)
    else:
        bundle = await _load_daily_bundle(request, user_guid)
        tracks = dailyrec.stamp_playlist_tracks(list(bundle.get("tracks") or []))
    # 打开耗时留证：真机上「歌单打开几秒」从此可量化（cache=miss 是上游链路，
    # cache=hit 仍慢则瓶颈在传输/客户端，两者排障方向完全不同）
    _ms = (time.monotonic() - started) * 1000.0
    if _ms > 1000:
        logger.info("playlist tracks slow open: %.0fms cache=%s %s", _ms, cache_state, guid)
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("size") or 50)
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50
    start = (page - 1) * size
    page_tracks = tracks[start:start + size] if size != -1 else tracks
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
        }
    )


@app.post("/music/api/v1/event/report")
async def event_report(request: Request):
    upstream_client = get_upstream_client(request.app)
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except Exception:
        body = {}
    events = body.get("events") if isinstance(body, dict) else None
    online_plays: list[str] = []
    other_events: list = []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("eventType") or ev.get("type") or "")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            guid = str(payload.get("trackGUID") or payload.get("guid") or "")
            if et in ("track_play", "TrackPlay") and is_online_guid(guid):
                online_plays.append(guid)
            else:
                other_events.append(ev)
    else:
        return await forward_to_upstream(request, upstream_client)

    if online_plays:
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if is_authed:
            async with _HISTORY_LOCK:
                for guid in online_plays:
                    info = stub_online_info(guid)
                    cached = find_cache_file(guid)
                    title = str(info.get("title") or "")
                    artist = str(info.get("artist") or "")
                    if cached:
                        base = os.path.splitext(os.path.basename(cached))[0]
                        if " - " in base:
                            artist, title = base.split(" - ", 1)
                        elif not title:
                            title = base
                    dailyrec.record_online_play(
                        user_guid,
                        guid,
                        {"guid": guid, "title": title, "artist": artist, "source": source_from_online_guid(guid)},
                    )
        elif auth_resp is not None and not other_events:
            return auth_resp

    if other_events:
        headers = copy_incoming_headers(request)
        fwd = dict(body)
        fwd["events"] = other_events
        req = upstream_client.build_request(
            "POST",
            "/music/api/v1/event/report",
            headers=headers,
            content=json.dumps(fwd).encode("utf-8"),
        )
        resp = await upstream_client.send(req)
        resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    return JSONResponse(content={"code": 0, "msg": "ok", "data": None})


@app.get("/music/api/v1/play-history/list")
async def play_history_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official

    async with _HISTORY_LOCK:
        online_items = dailyrec.load_online_play_history(user_guid)
    online_tracks = []
    for it in reversed(online_items):
        guid = str(it.get("guid") or "")
        if not guid:
            continue
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        obj = build_favorite_track_obj(guid, track, created_at=int(it.get("playedAt") or time.time()))
        obj["isFavorite"] = False
        online_tracks.append(obj)

    seen = {str(x.get("guid")) for x in official if isinstance(x, dict)}
    merged_online = [t for t in online_tracks if t.get("guid") not in seen]
    data["list"] = merged_online + official
    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official)
    data["total"] = official_total + len(merged_online)
    return JSONResponse(content=envelope, headers=headers)


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    return await forward_to_upstream(request, get_upstream_client(request.app))
