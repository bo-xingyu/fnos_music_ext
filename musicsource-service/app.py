"""HTTP wrapper for multi-source music adapters (QQ / 酷狗 / 酷我 / 汽水).

API contract mirrors musicbox-service:
  GET /healthz
  GET /api/v1/sources
  GET /api/v1/search?keyword=&limit=&source=all|qq|kugou|kuwo|qishui
  GET /api/v1/song/{source}/{song_id}/url?quality=
  GET /api/v1/song/{source}/{song_id}/lyric
  GET /api/v1/song/{source}/{song_id}/detail
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from sources import SOURCE_LABELS, SourceError, build_registry
from sources.base import env_flag

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fnmusic_musicsource")

VALID_SOURCES = set(SOURCE_LABELS)


def _enabled_sources() -> set[str]:
    raw = (os.environ.get("FNMUSIC_EXTRA_SOURCES") or "qq,kugou,kuwo,qishui").strip()
    if not raw:
        return set()
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    # 单独开关
    mapping = {
        "qq": env_flag("FNMUSIC_QQ_ENABLED", "true"),
        "kugou": env_flag("FNMUSIC_KUGOU_ENABLED", "true"),
        "kuwo": env_flag("FNMUSIC_KUWO_ENABLED", "true"),
        "qishui": env_flag("FNMUSIC_QISHUI_ENABLED", "true"),
    }
    enabled = {p for p in parts if p in VALID_SOURCES and mapping.get(p, True)}
    # 若用户只改了单源开关而没写 EXTRA_SOURCES，也以单源开关为准
    if raw in ("qq,kugou,kuwo,qishui", ""):
        enabled = {k for k, v in mapping.items() if v}
        env_list = (os.environ.get("FNMUSIC_EXTRA_SOURCES") or "").strip()
        if env_list:
            listed = {p.strip().lower() for p in env_list.split(",") if p.strip()}
            if listed & VALID_SOURCES:
                enabled &= listed or enabled
    return enabled


REGISTRY = build_registry(_enabled_sources())
app = FastAPI(title="fnmusic-musicsource", version="1.0.0")


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "sources": {k: {"label": v.label} for k, v in REGISTRY.items()},
        "count": len(REGISTRY),
    }


@app.get("/api/v1/sources")
def list_sources():
    return {
        "ok": True,
        "data": [
            {"key": k, "label": src.label, "enabled": True}
            for k, src in REGISTRY.items()
        ],
    }


def _get_source(key: str):
    src = REGISTRY.get(key)
    if src is None:
        raise HTTPException(status_code=404, detail=f"source {key!r} not enabled")
    return src


@app.get("/api/v1/search")
async def search(
    keyword: str = Query("", alias="keyword"),
    q: str = Query(""),
    limit: int = Query(20, ge=1, le=50),
    source: str = Query("all"),
):
    kw = (keyword or q or "").strip()
    if not kw:
        raise HTTPException(status_code=422, detail="keyword is required")

    source = (source or "all").strip().lower()
    if source not in ("all", "") and source not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"source {source!r} not enabled")

    targets = list(REGISTRY.items()) if source in ("all", "") else [(source, REGISTRY[source])]

    async def _one(key: str, src):
        try:
            items = await src.search(kw, limit=limit)
            return key, [it.to_public() for it in items]
        except Exception as exc:  # noqa: BLE001
            logger.warning("search %s failed: %s: %s", key, type(exc).__name__, exc)
            return key, []

    results = await asyncio.gather(*[_one(k, s) for k, s in targets])
    data: list[dict] = []
    # 保持音源顺序合并
    for key, items in results:
        for it in items:
            data.append(it)
    return {"ok": True, "data": data, "sources": [k for k, _ in results]}


@app.get("/api/v1/song/{source}/{song_id}/url")
async def song_url(
    source: str = Path(...),
    song_id: str = Path(...),
    quality: str = Query(""),
):
    src = _get_source(source)
    try:
        url = await src.resolve_url(song_id, quality=quality)
    except SourceError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("url %s/%s failed: %s: %s", source, song_id, type(exc).__name__, exc)
        return JSONResponse(status_code=502, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    if not url:
        return JSONResponse(status_code=404, content={"ok": False, "error": "empty url"})
    return {"ok": True, "data": {"url": url, "source": source, "id": song_id}}


@app.get("/api/v1/song/{source}/{song_id}/lyric")
async def song_lyric(
    source: str = Path(...),
    song_id: str = Path(...),
):
    src = _get_source(source)
    try:
        lyric = await src.lyric(song_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("lyric %s/%s failed: %s: %s", source, song_id, type(exc).__name__, exc)
        lyric = ""
    return {"ok": True, "data": {"lyric": lyric, "source": source, "id": song_id}}


@app.get("/api/v1/song/{source}/{song_id}/detail")
async def song_detail(
    source: str = Path(...),
    song_id: str = Path(...),
):
    src = _get_source(source)
    try:
        info = await src.detail(song_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("detail %s/%s failed: %s: %s", source, song_id, type(exc).__name__, exc)
        info = {}
    if not isinstance(info, dict):
        info = {}
    info.setdefault("id", f"{source}:{song_id}")
    info.setdefault("source", source)
    return {"ok": True, "data": info}
