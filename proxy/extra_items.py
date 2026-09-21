"""扩展音源条目映射：musicsource-service 的 song → 代理内部统一条目。"""
from __future__ import annotations

import re
from typing import Any

BUILTIN_EXTRA_SOURCE_NAMES = ("qq", "kugou", "kuwo", "qishui")
# 运行时可含自定义音源 key；保持向后兼容别名
EXTRA_SOURCE_NAMES = BUILTIN_EXTRA_SOURCE_NAMES

SOURCE_LABELS = {
    "qq": "QQ音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
    "qishui": "汽水音乐",
}

CUSTOM_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]{0,31}$")

LOSSLESS_MARKERS = ("SQ", "HR", "LOSSLESS", "HIRES", "FLAC", "无损")


def is_extra_source(src: str | None) -> bool:
    """是否属于扩展音源（内置或自定义）。网易云不算。"""
    s = (src or "").strip().lower()
    if not s or s == "netease":
        return False
    if s in BUILTIN_EXTRA_SOURCE_NAMES:
        return True
    return bool(CUSTOM_KEY_RE.match(s))


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def map_extra_song(raw: dict) -> dict | None:
    """把 musicsource-service 返回的一条 song 映射成扩展内部条目。"""
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("source") or "").strip().lower()
    if not is_extra_source(source):
        return None
    sid = str(raw.get("id") or "").strip()
    if not sid:
        return None
    title = str(raw.get("title") or raw.get("name") or "").strip()
    if not title:
        return None
    if raw.get("playable") is False:
        return None

    duration = _to_float(raw.get("duration_s"))
    if duration > 10_000:
        duration /= 1000.0
    quality = str(raw.get("quality") or "").upper()
    lossless = any(m in quality for m in LOSSLESS_MARKERS)
    ext = str(raw.get("ext") or "").strip().lower()
    if lossless and ext in ("", "mp3"):
        ext = "flac"
    if not ext or ext not in ("mp3", "flac", "m4a", "wav", "ogg", "aac", "ape"):
        ext = "flac" if lossless else "mp3"

    return {
        "id": f"{source}:{sid}",
        "source": source,
        "title": title,
        "artist": str(raw.get("artist") or "").strip(),
        "album": str(raw.get("album") or "").strip(),
        "duration_s": duration,
        "ext": ext,
        "cover_url": str(raw.get("cover_url") or "").strip(),
        "lyric": "",
    }


def map_extra_search_payload(data: Any) -> list[dict]:
    """musicsource-service /api/v1/search 响应 → 统一条目列表。"""
    if isinstance(data, dict):
        raw_list = data.get("data")
    else:
        raw_list = data
    if not isinstance(raw_list, list):
        return []
    items: list[dict] = []
    for raw in raw_list:
        item = map_extra_song(raw)
        if item is not None:
            items.append(item)
    return items
