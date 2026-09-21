"""多音源适配器：QQ / 酷狗 / 酷我 / 汽水 + 自定义音源。"""
from __future__ import annotations

from .base import SourceSong, SourceError, ensure_lrc_text
from .qq import QQSource
from .kugou import KugouSource
from .kuwo import KuwoSource
from .qishui import QishuiSource
from .custom import CustomSource, build_custom_sources, load_custom_configs, save_custom_configs
from . import auth_store, login as login_mod

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

BUILTIN_KEYS = tuple(SOURCE_LABELS)


def build_registry(enabled: set[str] | None = None, include_custom: bool = True) -> dict:
    """构建启用中的音源实例字典。enabled=None 表示内置音源全部启用。"""
    registry = {}
    for key, cls in SOURCE_CLASSES.items():
        if enabled is not None and key not in enabled:
            continue
        registry[key] = cls()
    if include_custom:
        for key, src in build_custom_sources().items():
            if key not in registry:
                registry[key] = src
    return registry


def label_of(key: str) -> str:
    if key in SOURCE_LABELS:
        return SOURCE_LABELS[key]
    for cfg in load_custom_configs():
        if str(cfg.get("key")) == key:
            return str(cfg.get("label") or key)
    return key


__all__ = [
    "SourceSong",
    "SourceError",
    "ensure_lrc_text",
    "QQSource",
    "KugouSource",
    "KuwoSource",
    "QishuiSource",
    "CustomSource",
    "SOURCE_CLASSES",
    "SOURCE_LABELS",
    "BUILTIN_KEYS",
    "build_registry",
    "build_custom_sources",
    "load_custom_configs",
    "save_custom_configs",
    "auth_store",
    "login_mod",
    "label_of",
]
