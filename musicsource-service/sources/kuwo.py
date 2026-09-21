"""酷我音乐适配器。

搜索优先走 search.kuwo.cn 的 r.s（encoding=utf8 返回 JSON）；
取链优先 antiserver 公开 convert_url，失败再试 www.kuwo.cn playUrl。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import httpx

from .base import SourceError, SourceSong, ensure_lrc_text, parse_duration_ms
from . import auth_store

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.kuwo.cn/",
}


class KuwoSource:
    key = "kuwo"
    label = "酷我音乐"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(headers=self._headers(), timeout=12.0, follow_redirects=True)
        self._kw_token = ""

    def _headers(self) -> dict:
        h = dict(HEADERS)
        cookie = auth_store.cookie_of("kuwo")
        token = ""
        if cookie:
            h["Cookie"] = cookie
            m = re.search(r"kw_token=([^;]+)", cookie)
            if m:
                token = m.group(1)
        if token:
            self._kw_token = token
            h["csrf"] = token
        return h

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthz(self) -> bool:
        return True

    async def _ensure_kw_token(self) -> str:
        if self._kw_token:
            return self._kw_token
        try:
            r = await self._client.get("https://www.kuwo.cn/", headers={"User-Agent": UA})
            # Set-Cookie: kw_token=XXXX
            token = ""
            for k, v in r.headers.multi_items():
                if k.lower() == "set-cookie" and "kw_token=" in v:
                    m = re.search(r"kw_token=([^;]+)", v)
                    if m:
                        token = m.group(1)
                        break
            self._kw_token = token
        except Exception:  # noqa: BLE001
            self._kw_token = ""
        return self._kw_token

    async def search(self, keyword: str, limit: int = 20) -> list[SourceSong]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        # 1) 老接口：encoding=utf8 时返回 JSON
        items = await self._search_r_s(keyword, limit)
        if items:
            return items
        # 2) 新接口：需要 kw_token
        return await self._search_www(keyword, limit)

    async def _search_r_s(self, keyword: str, limit: int) -> list[SourceSong]:
        params = {
            "all": keyword,
            "ft": "music",
            "rn": max(1, min(int(limit), 50)),
            "pn": 0,
            "cluster": 0,
            "ver": "kuwo",
            "vipver": "MUSIC_8.7.7.2-W4",
            "plat": "pc",
            "encoding": "utf8",
            "rformat": "json",
            "moession": "1",
            "vermerge": "1",
            "capt": 0,
            "newver": 1,
        }
        try:
            r = await self._client.get("https://search.kuwo.cn/r.s", params=params)
            text = r.text.strip()
            data = None
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                # 有些部署返回 NMAP 包装，尝试抽取 JSON
                m = re.search(r"=\s*(\{.*\})\s*$", text, re.S)
                if m:
                    data = json.loads(m.group(1))
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"kuwo search failed: {exc}") from exc
        return self._map_abslist(data)

    async def _search_www(self, keyword: str, limit: int) -> list[SourceSong]:
        token = await self._ensure_kw_token()
        headers = {
            **HEADERS,
            "Referer": f"https://www.kuwo.cn/searchList?key={keyword}",
            "Cookie": f"kw_token={token}" if token else "",
            "csrf": token or "",
            "Cross": "9e1e4a7f1d1f3f0e8b0c9d2a3e4f5a6b",
        }
        try:
            r = await self._client.get(
                "https://www.kuwo.cn/api/www/search/searchMusicBykeyWord",
                params={
                    "key": keyword,
                    "pn": 1,
                    "rn": max(1, min(int(limit), 50)),
                    "httpsStatus": 1,
                    "reqId": str(uuid.uuid4()),
                },
                headers=headers,
            )
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"kuwo www search failed: {exc}") from exc
        return self._map_www(data)

    @staticmethod
    def _map_abslist(data: Any) -> list[SourceSong]:
        if not isinstance(data, dict):
            return []
        abslist = data.get("abslist") or data.get("list") or []
        items: list[SourceSong] = []
        for raw in abslist or []:
            if not isinstance(raw, dict):
                continue
            rid = str(raw.get("DC_TARGETID") or raw.get("MUSICRID") or raw.get("rid") or "").strip()
            rid = rid.replace("MUSIC_", "").replace("music_", "")
            if not rid:
                continue
            artist = str(raw.get("ARTIST") or raw.get("artist") or "")
            title = str(raw.get("NAME") or raw.get("name") or raw.get("SONGNAME") or "")
            album = str(raw.get("ALBUM") or raw.get("album") or "")
            duration = raw.get("DURATION") or raw.get("duration") or 0
            cover = str(raw.get("web_albumpic_short") or raw.get("albumpic") or raw.get("pic") or "")
            if cover and cover.startswith("/"):
                cover = "https://img1.kuwo.cn" + cover
            items.append(
                SourceSong(
                    id=rid,
                    source="kuwo",
                    title=title,
                    artist=artist,
                    album=album,
                    duration_s=parse_duration_ms(duration),
                    ext="mp3",
                    cover_url=cover,
                    playable=True,
                    raw=raw,
                )
            )
        return items

    @staticmethod
    def _map_www(data: Any) -> list[SourceSong]:
        if not isinstance(data, dict):
            return []
        body = data.get("data") or {}
        lst = body.get("list") or body.get("musicList") or []
        items: list[SourceSong] = []
        for raw in lst or []:
            if not isinstance(raw, dict):
                continue
            rid = str(raw.get("rid") or raw.get("musicId") or raw.get("id") or "").strip()
            if not rid:
                continue
            cover = str(raw.get("pic") or raw.get("albumpic") or raw.get("pic120") or "")
            if cover.startswith("/"):
                cover = "https://img1.kuwo.cn" + cover
            items.append(
                SourceSong(
                    id=rid,
                    source="kuwo",
                    title=str(raw.get("name") or raw.get("songname") or ""),
                    artist=str(raw.get("artist") or raw.get("singer") or ""),
                    album=str(raw.get("album") or raw.get("album_name") or ""),
                    duration_s=parse_duration_ms(raw.get("duration") or raw.get("songTimeMinutes")),
                    ext="flac" if str(raw.get("hasLossless") or raw.get("lossless") or "").lower() in ("1", "true", "yes") else "mp3",
                    cover_url=cover,
                    playable=True,
                    raw=raw,
                )
            )
        return items

    async def resolve_url(self, song_id: str, quality: str = "") -> str:
        rid = (song_id or "").strip()
        if not rid:
            raise SourceError("empty kuwo song id")

        # 1) antiserver（公开，常能拿到 mp3/flac 直链）
        anti_params_list = [
            {"format": "mp3", "rid": rid, "response": "url", "type": "convert_url3", "br": "320kmp3"},
            {"format": "mp3", "rid": rid, "response": "url", "type": "convert_url"},
            {"format": "flac", "rid": rid, "response": "url", "type": "convert_url3", "br": "2000kflac"},
        ]
        for params in anti_params_list:
            try:
                r = await self._client.get("https://antiserver.kuwo.cn/anti.s", params=params)
                url = (r.text or "").strip()
                if url.startswith("http"):
                    return url
                # 有时返回 JSON
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        u = data.get("url") or data.get("data")
                        if isinstance(u, str) and u.startswith("http"):
                            return u
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                continue

        # 2) www API
        token = await self._ensure_kw_token()
        headers = {
            **HEADERS,
            "Cookie": f"kw_token={token}" if token else "",
            "csrf": token or "",
            "Referer": f"https://www.kuwo.cn/play_detail/{rid}",
        }
        try:
            r = await self._client.get(
                "https://www.kuwo.cn/api/v1/www/music/playUrl",
                params={
                    "mid": rid,
                    "type": "music",
                    "httpsStatus": 1,
                    "reqId": str(uuid.uuid4()),
                },
                headers=headers,
            )
            data = r.json()
            inner = data.get("data") or {}
            url = str(inner.get("url") or "")
            if url.startswith("http"):
                return url
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"kuwo resolve failed for {rid}: {exc}") from exc
        raise SourceError(f"kuwo resolve returned empty url for {rid}")

    async def lyric(self, song_id: str) -> str:
        rid = (song_id or "").strip()
        if not rid:
            return ""
        # m 端接口
        try:
            r = await self._client.get(
                "https://m.kuwo.cn/newh5/singles/songinfoandlrc",
                params={"musicId": rid},
            )
            data = r.json()
            inner = data.get("data") or {}
            items = inner.get("lrclist") or []
            if items and isinstance(items, list):
                lines = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    try:
                        t = float(it.get("time") or 0)
                    except (TypeError, ValueError):
                        t = 0.0
                    m, sec = divmod(t, 60)
                    lines.append(f"[{int(m):02d}:{sec:05.2f}]{it.get('lineLyric') or it.get('line_lyric') or ''}")
                if lines:
                    return "\n".join(lines)
        except Exception:  # noqa: BLE001
            pass
        try:
            r = await self._client.get("https://newlyric.kuwo.cn/newlyric.lrc", params={"rid": rid})
            text = r.text
            if text and text.lstrip().startswith("["):
                return ensure_lrc_text(text)
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def detail(self, song_id: str) -> dict[str, Any]:
        return {}
