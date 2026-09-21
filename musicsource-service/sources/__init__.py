"""多音源适配器：QQ / 酷狗 / 酷我 / 汽水。"""
from __future__ import annotations

from .base import SourceSong, SourceError, ensure_lrc_text
from .qq import QQSource
from .kugou import KugouSource
from .kuwo import KuwoSource
from .qishui import QishuiSource

SOURCE_CLASSES = {
    "qq": QQSource,
    "kugou": KugouSource,
    "kuwo": KuwoSource,
    "qishui": QishuiSource,
}

SOURCE_LABELS = {
    "qq": "QQ音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
    "qishui": "汽水音乐",
}


def build_registry(enabled: set[str] | None = None) -> dict:
    """构建启用中的音源实例字典。enabled=None 表示全部启用。"""
    registry = {}
    for key, cls in SOURCE_CLASSES.items():
        if enabled is not None and key not in enabled:
            continue
        registry[key] = cls()
    return registry


__all__ = [
    "SourceSong",
    "SourceError",
    "ensure_lrc_text",
    "QQSource",
    "KugouSource",
    "KuwoSource",
    "QishuiSource",
    "SOURCE_CLASSES",
    "SOURCE_LABELS",
    "build_registry",
]
