"""HTTP wrapper for multi-source music adapters (QQ / 酷狗 / 酷我 / 汽水 + 自定义).

API:
  GET  /healthz
  GET  /api/v1/sources
  GET  /api/v1/search?keyword=&limit=&source=all|<key>
  GET  /api/v1/song/{source}/{id}/url?quality=
  GET  /api/v1/song/{source}/{id}/lyric
  GET  /api/v1/song/{source}/{id}/detail
  GET  /api/v1/auth                         各音源登录状态
  GET  /api/v1/auth/{source}/status
  GET  /api/v1/auth/{source}/help           Cookie 获取说明
  POST /api/v1/auth/{source}/cookie         body: {"cookie":"...","token":"..."}
  POST /api/v1/auth/{source}/logout
  POST /api/v1/auth/{source}/qr             发起扫码
  GET  /api/v1/auth/{source}/qr/check?unikey=
  GET  /api/v1/custom                       自定义音源列表
  POST /api/v1/custom                       body: {"items":[...]} 整表覆盖
  POST /api/v1/custom/item                  body: 单条 upsert
  POST /api/v1/custom/{key}/delete
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from sources import (
    BUILTIN_KEYS,
    SOURCE_LABELS,
    SourceError,
    auth_store,
    build_registry,
    label_of,
    load_custom_configs,
    login_mod,
    save_custom_configs,
)
from sources.base import env_flag
from sources.custom import CustomSource, build_custom_sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fnmusic_musicsource")

app = FastAPI(title="fnmusic-musicsource", version="2.0.0")

REGISTRY: dict[str, Any] = {}


def _builtin_enabled() -> set[str]:
    raw = (os.environ.get("FNMUSIC_EXTRA_SOURCES") or "qq,kugou,kuwo,qishui").strip()
    listed = {p.strip().lower() for p in raw.split(",") if p.strip()} if raw else set()
    mapping = {
        "qq": env_flag("FNMUSIC_QQ_ENABLED", "true"),
        "kugou": env_flag("FNMUSIC_KUGOU_ENABLED", "true"),
        "kuwo": env_flag("FNMUSIC_KUWO_ENABLED", "true"),
        "qishui": env_flag("FNMUSIC_QISHUI_ENABLED", "true"),
    }
    if listed:
        return {k for k in listed if k in mapping and mapping.get(k, True)}
    return {k for k, v in mapping.items() if v}


def reload_registry() -> dict:
    global REGISTRY
    REGISTRY = build_registry(_builtin_enabled(), include_custom=True)
    return REGISTRY


reload_registry()


def _get_source(key: str):
    src = REGISTRY.get(key)
    if src is None:
        # 自定义音源可能刚写入配置
        if key not in BUILTIN_KEYS:
            for k, s in build_custom_sources().items():
                if k == key:
                    REGISTRY[k] = s
                    return s
        raise HTTPException(status_code=404, detail=f"source {key!r} not enabled")
    return src


def _valid_source_key(key: str) -> bool:
    k = (key or "").strip().lower()
    return k in REGISTRY or k in BUILTIN_KEYS or any(
        str(c.get("key")).lower() == k for c in load_custom_configs()
    )


@app.get("/healthz")
def healthz():
    sources = {}
    for k, v in REGISTRY.items():
        custom = isinstance(v, CustomSource)
        sources[k] = {
            "label": getattr(v, "label", label_of(k)),
            "custom": custom,
            "logged_in": (auth_store.status(k) or {}).get("logged_in", False)
            if k in BUILTIN_KEYS else bool(str(getattr(v, "cfg", {}).get("cookie") or "") or
                                           (auth_store.cookie_of(k) if not custom else "")),
        }
    return {
        "status": "ok",
        "sources": sources,
        "count": len(REGISTRY),
        "auth_dir": str(auth_store.data_dir() / "auth"),
    }


@app.get("/api/v1/sources")
def list_sources():
    data = []
    for k, src in REGISTRY.items():
        st = {}
        try:
            st = auth_store.status(k)
        except Exception:  # noqa: BLE001
            st = {}
        data.append({
            "key": k,
            "label": getattr(src, "label", label_of(k)),
            "enabled": True,
            "builtin": k in BUILTIN_KEYS,
            "logged_in": bool(st.get("logged_in")),
        })
    return {"ok": True, "data": data, "builtin": list(BUILTIN_KEYS)}


@app.get("/api/v1/search")
async def search(
    keyword: str = Query(""),
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
    for key, items in results:
        for it in items:
            data.append(it)
    return {"ok": True, "data": data, "sources": [k for k, _ in results]}


@app.get("/api/v1/song/{source}/{song_id}/url")
async def song_url(source: str = Path(...), song_id: str = Path(...), quality: str = Query("")):
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
async def song_lyric(source: str = Path(...), song_id: str = Path(...)):
    src = _get_source(source)
    try:
        lyric = await src.lyric(song_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("lyric %s/%s failed: %s: %s", source, song_id, type(exc).__name__, exc)
        lyric = ""
    return {"ok": True, "data": {"lyric": lyric, "source": source, "id": song_id}}


@app.get("/api/v1/song/{source}/{song_id}/detail")
async def song_detail(source: str = Path(...), song_id: str = Path(...)):
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


# ---------------------------------------------------------------------- auth ----

@app.get("/api/v1/auth")
def auth_all():
    keys = list(REGISTRY.keys())
    for c in load_custom_configs():
        k = str(c.get("key") or "")
        if k and k not in keys:
            keys.append(k)
    return {"ok": True, "data": auth_store.all_status(keys)}


@app.get("/api/v1/auth/{source}/status")
def auth_status(source: str = Path(...)):
    if not _valid_source_key(source) and source not in BUILTIN_KEYS:
        # 仍允许查询任意合法 key
        try:
            return {"ok": True, "data": auth_store.status(source)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": auth_store.status(source)}


@app.get("/api/v1/auth/{source}/help")
def auth_help(source: str = Path(...)):
    s = source.strip().lower()
    try:
        from sources import login as login_mod
        steps = login_mod.qr_steps(s)
        help_txt = login_mod.help_text(s)
        label = login_mod.SOURCE_LABELS.get(s, s)
    except Exception:  # noqa: BLE001
        steps, help_txt, label = [], "", s
    return {
        "ok": True,
        "data": {
            "source": s,
            "label": label,
            "cookie_help": help_txt,
            "supports_qr": s in ("qq", "kugou", "kuwo"),
            "steps": steps,
            "qr_note": "扫码接口各平台会变；失败时请改用 Cookie。",
        },
    }


@app.post("/api/v1/auth/{source}/cookie")
async def auth_set_cookie(request: Request, source: str = Path(...)):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    cookie = str((body or {}).get("cookie") or "").strip()
    token = str((body or {}).get("token") or "").strip()
    nickname = str((body or {}).get("nickname") or "").strip()
    if not cookie and not token:
        raise HTTPException(status_code=422, detail="cookie 或 token 至少填一个")
    if len(cookie) > 8000 or len(token) > 2000:
        raise HTTPException(status_code=422, detail="凭据过长")
    try:
        if cookie:
            auth_store.set_cookie(source, cookie, nickname=nickname)
        if token:
            auth_store.set_token(source, token, nickname=nickname)
        st = auth_store.status(source)
        # 自定义音源配置里同步一份 cookie
        try:
            configs = load_custom_configs()
            key = source.strip().lower()
            for cfg in configs:
                if str(cfg.get("key")).lower() == key:
                    if cookie:
                        cfg["cookie"] = cookie
                    save_custom_configs(configs)
                    reload_registry()
                    break
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "data": st}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/auth/{source}/logout")
def auth_logout(source: str = Path(...)):
    try:
        cleared = auth_store.clear(source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        configs = load_custom_configs()
        key = source.strip().lower()
        changed = False
        for cfg in configs:
            if str(cfg.get("key")).lower() == key and cfg.get("cookie"):
                cfg["cookie"] = ""
                changed = True
        if changed:
            save_custom_configs(configs)
            reload_registry()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "cleared": cleared, "data": auth_store.status(source)}


@app.post("/api/v1/auth/{source}/qr")
async def auth_qr_start(source: str = Path(...)):
    result = await login_mod.qr_start(source)
    return result


@app.get("/api/v1/auth/{source}/qr/check")
async def auth_qr_check(source: str = Path(...), unikey: str = Query("")):
    result = await login_mod.qr_check(unikey)
    result.setdefault("source", source)
    return result


@app.get("/api/v1/auth/qr/check")
async def auth_qr_check_alias(unikey: str = Query("")):
    return await login_mod.qr_check(unikey)


# -------------------------------------------------------------------- custom ----

@app.get("/api/v1/custom")
def custom_list():
    items = load_custom_configs()
    # 脱敏：cookie 只给 preview
    out = []
    for c in items:
        row = dict(c)
        cookie = str(row.get("cookie") or "")
        row["cookie_preview"] = (cookie[:10] + "…") if len(cookie) > 10 else ("已设置" if cookie else "")
        if cookie:
            row["cookie"] = ""
        out.append(row)
    return {"ok": True, "data": out, "count": len(out)}


@app.post("/api/v1/custom")
async def custom_save_all(request: Request):
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"无效 JSON: {exc}") from exc
    items = (body or {}).get("items")
    if items is None and isinstance(body, list):
        items = body
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="需要 {\"items\": [...]}")
    try:
        saved = save_custom_configs(items)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    reload_registry()
    return {"ok": True, "data": saved, "count": len(saved)}


@app.post("/api/v1/custom/item")
async def custom_upsert(request: Request):
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"无效 JSON: {exc}") from exc
    if not isinstance(body, dict) or not body.get("key"):
        raise HTTPException(status_code=422, detail="需要单条对象且含 key")
    key = str(body.get("key"))
    items = load_custom_configs()
    # 保留未提交的 cookie（列表接口会清空 cookie 字段）
    prev_cookie = ""
    replaced = False
    new_items = []
    for c in items:
        if str(c.get("key")).lower() == key.lower():
            prev_cookie = str(c.get("cookie") or "")
            merged = dict(c)
            merged.update(body)
            if not body.get("cookie") and prev_cookie:
                merged["cookie"] = prev_cookie
            new_items.append(merged)
            replaced = True
        else:
            new_items.append(c)
    if not replaced:
        new_items.append(body)
    try:
        saved = save_custom_configs(new_items)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    reload_registry()
    return {"ok": True, "data": saved, "replaced": replaced}


@app.post("/api/v1/custom/{key}/delete")
def custom_delete(key: str = Path(...)):
    items = load_custom_configs()
    key_l = key.strip().lower()
    kept = [c for c in items if str(c.get("key")).lower() != key_l]
    if len(kept) == len(items):
        return {"ok": True, "deleted": False}
    try:
        save_custom_configs(kept)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        auth_store.clear(key_l)
    except Exception:  # noqa: BLE001
        pass
    REGISTRY.pop(key_l, None)
    reload_registry()
    return {"ok": True, "deleted": True}
