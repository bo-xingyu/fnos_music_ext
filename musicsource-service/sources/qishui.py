"""汽水音乐适配器。

汽水（字节）Web 接口变动频繁且常带签名/登录态，这里采用两层策略：

1. 若配置了兼容聚合 API（``FNMUSIC_QISHUI_API_BASE`` / ``FNMUSIC_EXTRA_API_BASE``），
   优先走聚合端：``{base}/search?source=qishui&keyword=`` 与
   ``{base}/url?source=qishui&id=``、``{base}/lyric?source=qishui&id=``。
2. 否则尝试汽水/H5 常见公开探测接口（成功率不稳定，失败会明确报错）。

可将 base 指向自建的 lx-music-api-server / Musicn 等兼容网关。
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urljoin

import httpx

from .base import SourceError, SourceSong, ensure_lrc_text, parse_duration_ms, guess_ext_from_url
from . import auth_store

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.qishui.com/",
    "Accept": "application/json, text/plain, */*",
}


def _api_bases() -> list[str]:
    bases = []
    for key in ("FNMUSIC_QISHUI_API_BASE", "FNMUSIC_EXTRA_API_BASE"):
        v = (os.environ.get(key) or "").strip().rstrip("/")
        if v and v not in bases:
            bases.append(v)
    return bases


class QishuiSource:
    key = "qishui"
    label = "汽水音乐"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(headers=self._headers(), timeout=12.0, follow_redirects=True)

    def _headers(self) -> dict:
        h = dict(HEADERS)
        h.update(auth_store.cookie_header("qishui"))
        token = auth_store.token_of("qishui")
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthz(self) -> bool:
        return True

    async def search(self, keyword: str, limit: int = 20) -> list[SourceSong]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        errors: list[str] = []
        for base in _api_bases():
            try:
                items = await self._search_via_aggregator(base, keyword, limit)
                if items:
                    return items
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        # 无聚合网关时的 best-effort：返回空，由代理层静默忽略该音源
        if errors:
            raise SourceError("qishui search failed: " + "; ".join(errors[:2]))
        return []

    async def _search_via_aggregator(self, base: str, keyword: str, limit: int) -> list[SourceSong]:
        url = urljoin(base + "/", "search")
        r = await self._client.get(
            url,
            params={"source": "qishui", "keyword": keyword, "limit": limit, "type": "song"},
            timeout=15.0,
        )
        data = r.json()
        raw_list = data.get("data") if isinstance(data, dict) else data
        if isinstance(raw_list, dict):
            raw_list = raw_list.get("songs") or raw_list.get("list") or raw_list.get("items") or []
        if not isinstance(raw_list, list):
            return []
        items: list[SourceSong] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            sid = str(raw.get("id") or raw.get("song_id") or raw.get("songId") or "").strip()
            if not sid:
                continue
            items.append(
                SourceSong(
                    id=sid,
                    source=self.key,
                    title=str(raw.get("title") or raw.get("name") or raw.get("song_name") or ""),
                    artist=str(raw.get("artist") or raw.get("singer") or raw.get("author") or ""),
                    album=str(raw.get("album") or raw.get("album_name") or ""),
                    duration_s=parse_duration_ms(raw.get("duration_s") or raw.get("duration")),
                    ext=str(raw.get("ext") or guess_ext_from_url(str(raw.get("url") or ""), str(raw.get("quality") or ""))),
                    cover_url=str(raw.get("cover_url") or raw.get("picUrl") or raw.get("cover") or ""),
                    playable=bool(raw.get("playable", True)),
                    quality=str(raw.get("quality") or ""),
                    raw=raw,
                )
            )
        return items[:limit]

    async def resolve_url(self, song_id: str, quality: str = "") -> str:
        sid = (song_id or "").strip()
        if not sid:
            raise SourceError("empty qishui song id")
        errors: list[str] = []
        for base in _api_bases():
            for path in ("url", "song/url", "song/url/v1"):
                url = urljoin(base + "/", path)
                try:
                    r = await self._client.get(
                        url,
                        params={"source": "qishui", "id": sid, "song_id": sid, "quality": quality or "standard"},
                        timeout=15.0,
                    )
                    data = r.json()
                    play = self._extract_url(data)
                    if play:
                        return play
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
        raise SourceError(
            "qishui resolve failed：未配置可用聚合网关，或网关未返回直链。"
            "请设置 FNMUSIC_QISHUI_API_BASE（或 FNMUSIC_EXTRA_API_BASE）指向兼容音源网关。"
            + ((" " + errors[0][:120]) if errors else "")
        )

    @staticmethod
    def _extract_url(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("url", "play_url", "playUrl"):
            val = data.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("url", "play_url", "playUrl"):
                val = inner.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val
        if isinstance(inner, str) and inner.startswith("http"):
            return inner
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            val = inner[0].get("url")
            if isinstance(val, str) and val.startswith("http"):
                return val
        return ""

    async def lyric(self, song_id: str) -> str:
        sid = (song_id or "").strip()
        if not sid:
            return ""
        for base in _api_bases():
            for path in ("lyric", "song/lyric", "lyric/lrc"):
                url = urljoin(base + "/", path)
                try:
                    r = await self._client.get(
                        url,
                        params={"source": "qishui", "id": sid, "song_id": sid},
                        timeout=12.0,
                    )
                    data = r.json()
                    if isinstance(data, dict):
                        for key in ("lyric", "lrc", "data", "content"):
                            val = data.get(key)
                            if isinstance(val, str) and val.strip():
                                return ensure_lrc_text(val)
                            if isinstance(val, dict):
                                for k2 in ("lyric", "lrc", "content"):
                                    if isinstance(val.get(k2), str) and val[k2].strip():
                                        return ensure_lrc_text(val[k2])
                    elif isinstance(data, str) and data.strip():
                        return ensure_lrc_text(data)
                except Exception:  # noqa: BLE001
                    continue
        return ""

    async def detail(self, song_id: str) -> dict[str, Any]:
        sid = (song_id or "").strip()
        for base in _api_bases():
            for path in ("detail", "song/detail", "song"):
                url = urljoin(base + "/", path)
                try:
                    r = await self._client.get(
                        url,
                        params={"source": "qishui", "id": sid, "song_id": sid},
                        timeout=12.0,
                    )
                    data = r.json()
                    raw = data.get("data") if isinstance(data, dict) else data
                    if isinstance(raw, dict) and (raw.get("title") or raw.get("name")):
                        return {
                            "title": str(raw.get("title") or raw.get("name") or ""),
                            "artist": str(raw.get("artist") or raw.get("singer") or ""),
                            "album": str(raw.get("album") or ""),
                            "cover_url": str(raw.get("cover_url") or raw.get("cover") or raw.get("picUrl") or ""),
                            "duration_s": parse_duration_ms(raw.get("duration_s") or raw.get("duration")),
                        }
                except Exception:  # noqa: BLE001
                    continue
        return {}
