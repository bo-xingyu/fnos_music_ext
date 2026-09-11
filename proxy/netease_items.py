"""网易云 song_info → 扩展统一条目的映射。

musicbox 服务把搜索结果（``/api/v1/search``）与每日推荐（``/api/v1/recommend/daily``）
都归一化成 NEMbox 的 song_info 结构，字段名一致，因此这里只写一份映射，
被 proxy/app.py（搜索聚合）与 proxy/recommend.py（每日推荐）共用。
"""
from __future__ import annotations

from typing import Any

SOURCE_NAME = "netease"
LOSSLESS_QUALITY_MARKERS = ("SQ", "HR", "无损")


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def song_id_of(raw: dict) -> str:
    return str(raw.get("song_id") or raw.get("id") or "")


def has_lossless(raw: dict) -> bool:
    quality = str(raw.get("quality") or "").upper()
    return any(marker in quality for marker in LOSSLESS_QUALITY_MARKERS)


def map_netease_song(raw: dict) -> dict | None:
    """把一条 musicbox song_info 转成扩展内部条目；缺 id 时返回 None。"""
    if not isinstance(raw, dict):
        return None
    sid = song_id_of(raw)
    if not sid:
        return None

    duration = _to_float(raw.get("duration"))
    # NEMbox 的 duration 是秒，但个别上游版本会直接给毫秒，超过 1 万秒的一律按毫秒还原
    if duration > 10_000:
        duration /= 1000.0

    return {
        "id": f"{SOURCE_NAME}:{sid}",
        "source": SOURCE_NAME,
        "title": str(raw.get("song_name") or raw.get("title") or raw.get("name") or ""),
        "artist": str(raw.get("artist") or ""),
        "album": str(raw.get("album_name") or raw.get("album") or ""),
        "duration_s": duration,
        "ext": "flac" if has_lossless(raw) else "mp3",
        "cover_url": "",
        "lyric": "",
    }


def apply_song_detail(item: dict, detail: dict) -> dict:
    """用 /api/v1/songs/detail 的结果补齐封面与音质判定（原地更新并返回）。"""
    if not isinstance(detail, dict):
        return item
    cover = str(detail.get("album_pic_url") or "")
    if cover:
        item["cover_url"] = cover
    if detail.get("has_sq") or detail.get("has_hr"):
        item["ext"] = "flac"
    if not item.get("album"):
        item["album"] = str(detail.get("album_name") or "")
    if not item.get("artist"):
        item["artist"] = str(detail.get("artist") or "")
    return item
