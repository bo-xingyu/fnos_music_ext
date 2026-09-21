"""扩展音源（QQ/酷狗/酷我/汽水）HTTP 客户端。"""
from __future__ import annotations

import os
from typing import Any

import httpx

try:
    from . import extra_items
except ImportError:  # uvicorn --app-dir proxy
    import extra_items  # type: ignore

DEFAULT_URL = "http://127.0.0.1:8771"


def _flag(name: str, default: str = "true") -> bool:
    return (os.environ.get(name) or default).strip().lower() in ("true", "1", "yes", "on", "")


def _int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def service_url() -> str:
    return (os.environ.get("FNMUSIC_MUSICSOURCE_URL") or DEFAULT_URL).strip().rstrip("/")


def enabled_sources() -> list[str]:
    raw = (os.environ.get("FNMUSIC_EXTRA_SOURCES") or "qq,kugou,kuwo,qishui").strip()
    listed = [p.strip().lower() for p in raw.split(",") if p.strip()]
    listed = [p for p in listed if p in extra_items.EXTRA_SOURCE_NAMES]
    singles = {
        "qq": _flag("FNMUSIC_QQ_ENABLED", "true"),
        "kugou": _flag("FNMUSIC_KUGOU_ENABLED", "true"),
        "kuwo": _flag("FNMUSIC_KUWO_ENABLED", "true"),
        "qishui": _flag("FNMUSIC_QISHUI_ENABLED", "true"),
    }
    # 若 EXTRA_SOURCES 明确列了清单，则取清单 ∩ 单源开关
    if listed:
        return [s for s in listed if singles.get(s, True)]
    return [s for s, on in singles.items() if on]


def any_enabled() -> bool:
    return bool(enabled_sources())


def get_client(fastapi_app) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicsource_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=service_url(), timeout=15.0)
        fastapi_app.state.musicsource_client = client
    return client


async def healthz(client: httpx.AsyncClient) -> str:
    try:
        r = await client.get("/healthz", timeout=2.0)
        return "ok" if r.status_code == 200 else "fail"
    except Exception:  # noqa: BLE001
        return "fail"


async def fetch_extra_search(
    client: httpx.AsyncClient,
    keyword: str,
    limit: int | None = None,
    sources: list[str] | None = None,
) -> list[dict] | None:
    """并发向扩展音源服务搜索；失败返回 None（调用方按空处理）。"""
    if not keyword:
        return None
    wanted = sources if sources is not None else enabled_sources()
    if not wanted:
        return []
    limit = limit if limit is not None else _int("FNMUSIC_EXTRA_SEARCH_LIMIT", 20)
    per = max(1, min(limit, 50))

    async def _one(source: str) -> list[dict]:
        try:
            r = await client.get(
                "/api/v1/search",
                params={"keyword": keyword, "limit": per, "source": source},
                timeout=12.0,
            )
            if r.status_code != 200:
                return []
            items = extra_items.map_extra_search_payload(r.json())
            # 服务端按 source 过滤；即便上游返回混音源，这里也只保留本音源条目
            return [it for it in items if str(it.get("source") or "").lower() == source]
        except Exception:  # noqa: BLE001
            return []

    import asyncio

    results = await asyncio.gather(*[_one(s) for s in wanted])
    items: list[dict] = []
    for batch in results:
        items.extend(batch)
    return items


async def resolve_url(
    client: httpx.AsyncClient,
    source: str,
    song_id: str,
    quality: str = "",
) -> str | None:
    source = (source or "").strip().lower()
    if source not in extra_items.EXTRA_SOURCE_NAMES:
        return None
    song_id = (song_id or "").strip()
    if not song_id:
        return None
    try:
        r = await client.get(
            f"/api/v1/song/{source}/{song_id}/url",
            params={"quality": quality} if quality else {},
            timeout=12.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        inner = data.get("data") or {}
        url = str(inner.get("url") or "") if isinstance(inner, dict) else ""
        return url or None
    except Exception:  # noqa: BLE001
        return None


async def fetch_lyric(
    client: httpx.AsyncClient,
    source: str,
    song_id: str,
) -> str:
    source = (source or "").strip().lower()
    if source not in extra_items.EXTRA_SOURCE_NAMES:
        return ""
    song_id = (song_id or "").strip()
    if not song_id:
        return ""
    try:
        r = await client.get(
            f"/api/v1/song/{source}/{song_id}/lyric",
            timeout=10.0,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return ""
        inner = data.get("data") or {}
        return str(inner.get("lyric") or "") if isinstance(inner, dict) else ""
    except Exception:  # noqa: BLE001
        return ""


async def fetch_detail(
    client: httpx.AsyncClient,
    source: str,
    song_id: str,
    fallback: dict | None = None,
) -> dict:
    """取详情；失败时回落搜索缓存里已有的条目字段。"""
    fallback = dict(fallback or {})
    source = (source or "").strip().lower()
    if source not in extra_items.EXTRA_SOURCE_NAMES:
        return fallback
    song_id = (song_id or "").strip()
    if not song_id:
        return fallback
    try:
        r = await client.get(
            f"/api/v1/song/{source}/{song_id}/detail",
            timeout=10.0,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("ok") is not False:
                inner = data.get("data")
                if isinstance(inner, dict) and (inner.get("title") or inner.get("name")):
                    item = extra_items.map_extra_song({**fallback, **inner, "source": source, "id": song_id})
                    if item:
                        return item
    except Exception:  # noqa: BLE001
        pass
    # 回落：至少保证 id/source 存在
    if not fallback.get("id"):
        fallback["id"] = f"{source}:{song_id}"
    fallback.setdefault("source", source)
    fallback.setdefault("title", "")
    fallback.setdefault("artist", "")
    fallback.setdefault("album", "")
    fallback.setdefault("duration_s", 0)
    fallback.setdefault("ext", "mp3")
    fallback.setdefault("cover_url", "")
    fallback.setdefault("lyric", "")
    return fallback


def lookup_cached_item(guid: str, cache_iter) -> dict | None:
    """从搜索缓存里按 guid 反查条目（用在线 info 缓存或搜索结果）。"""
    return None
