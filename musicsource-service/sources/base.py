"""音源适配器公共类型与工具。"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Any


class SourceError(Exception):
    """音源上游失败（网络/解析/不可播）。"""


@dataclass
class SourceSong:
    """统一曲目结构，id 形如 `qq:003aAYrm3GE0Ac`（不含 source 前缀时由 map 补全）。"""

    id: str
    source: str
    title: str
    artist: str
    album: str = ""
    duration_s: float = 0.0
    ext: str = "mp3"
    cover_url: str = ""
    playable: bool = True
    quality: str = ""
    raw: dict | None = None

    def to_public(self) -> dict:
        data = asdict(self)
        data.pop("raw", None)
        return data


def ensure_lrc_text(text: str | None) -> str:
    """尽量把上游歌词整理成 LRC 文本；拿不到就返回空串。"""
    if not text:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""
    # 汽水/部分上游可能给 JSON 包装
    if s.startswith("{") or s.startswith("["):
        try:
            import json

            obj = json.loads(s)
            if isinstance(obj, dict):
                for key in ("lrc", "lyric", "lyrics", "content", "data"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        return ensure_lrc_text(val)
                    if isinstance(val, dict):
                        for k2 in ("lyric", "lrc", "content"):
                            if isinstance(val.get(k2), str) and val[k2].strip():
                                return ensure_lrc_text(val[k2])
            elif isinstance(obj, list):
                lines = []
                for item in obj:
                    if isinstance(item, dict):
                        t = item.get("time") or item.get("t") or 0
                        w = item.get("word") or item.get("w") or item.get("lyric") or ""
                        try:
                            ms = float(t or 0)
                        except (TypeError, ValueError):
                            ms = 0
                        m, sec = divmod(ms / 1000.0, 60)
                        lines.append(f"[{int(m):02d}:{sec:05.2f}]{w}")
                    elif isinstance(item, str):
                        lines.append(item)
                if lines:
                    return "\n".join(lines)
        except Exception:  # noqa: BLE001
            return s
    return s


def env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def env_flag(name: str, default: str = "true") -> bool:
    return (os.environ.get(name) or default).strip().lower() in ("true", "1", "yes", "on", "")


def parse_duration_ms(value: Any) -> float:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if v > 10_000:  # 毫秒
        v /= 1000.0
    return v


def guess_ext_from_url(url: str, quality: str = "") -> str:
    u = (url or "").lower()
    q = (quality or "").upper()
    if any(x in q for x in ("FLAC", "SQ", "LOSSLESS", "HIRES", "无损")):
        return "flac"
    if ".flac" in u:
        return "flac"
    if ".m4a" in u or ".aac" in u:
        return "m4a"
    return "mp3"
