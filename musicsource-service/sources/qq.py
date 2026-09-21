"""QQ 音乐适配器。

走腾讯公开/半公开 Web 接口：搜索用 musicu.fcg 的 SearchCgi，
取链优先 vkey.GetVkeyServer，失败时尝试常见 CDN 直链模板。
不依赖登录 Cookie；仅覆盖免费/可匿名取链曲目。
"""
from __future__ import annotations

import json
import random
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .base import (
    SourceError,
    SourceSong,
    ensure_lrc_text,
    guess_ext_from_url,
    parse_duration_ms,
)
from . import auth_store

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": "https://y.qq.com/",
    "Origin": "https://y.qq.com",
    "Accept": "application/json, text/plain, */*",
}


def _auth_headers() -> dict:
    h = dict(HEADERS)
    h.update(auth_store.cookie_header("qq"))
    return h


class QQSource:
    key = "qq"
    label = "QQ音乐"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(headers=_auth_headers(), timeout=12.0, follow_redirects=True)

    def _headers(self) -> dict:
        return _auth_headers()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthz(self) -> bool:
        return True

    async def search(self, keyword: str, limit: int = 20) -> list[SourceSong]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        payload = {
            "comm": {
                "ct": 6,
                "cv": 80600,
                "guid": str(uuid.uuid4()).replace("-", ""),
                "format": "json",
                "platform": "yqq.json",
            },
            "music.search.SearchCgiService": {
                "method": "DoSearchForQQMusicDesktop",
                "module": "music.search.SearchCgiService",
                "param": {
                    "remoteplace": "txt.mqq.all",
                    "searchid": str(random.randint(10**15, 10**16 - 1)),
                    "search_type": 0,
                    "query": keyword,
                    "page_num": 1,
                    "num_per_page": max(1, min(int(limit), 50)),
                },
            },
        }
        try:
            r = await self._client.get(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                params={"data": json.dumps(payload, ensure_ascii=False)},
                headers=self._headers(),
            )
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"qq search failed: {exc}") from exc

        body = (data or {}).get("music.search.SearchCgiService") or {}
        inner = body.get("data") or {}
        body2 = inner.get("body") or {}
        song = body2.get("song") or {}
        raw_list = song.get("list") or []
        items: list[SourceSong] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            mid = str(raw.get("mid") or raw.get("songmid") or "").strip()
            if not mid:
                continue
            singers = raw.get("singer") or []
            artist = " / ".join(
                str(s.get("name") or "") for s in singers if isinstance(s, dict) and s.get("name")
            )
            album = raw.get("album") or {}
            album_name = str(album.get("name") or album.get("title") or "") if isinstance(album, dict) else str(album or "")
            album_mid = str(album.get("mid") or "") if isinstance(album, dict) else ""
            cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else ""
            file_info = raw.get("file") or {}
            size_hires = 0
            size_flac = 0
            if isinstance(file_info, dict):
                try:
                    size_hires = int(file_info.get("size_hires") or 0)
                    size_flac = int(file_info.get("size_flac") or 0)
                except (TypeError, ValueError):
                    size_hires = size_flac = 0
            lossless = bool(size_hires or size_flac)
            items.append(
                SourceSong(
                    id=mid,
                    source=self.key,
                    title=str(raw.get("title") or raw.get("songname") or ""),
                    artist=artist,
                    album=album_name,
                    duration_s=parse_duration_ms(raw.get("interval") or raw.get("time_public")),
                    ext="flac" if lossless else "mp3",
                    cover_url=cover,
                    playable=True,
                    quality="lossless" if lossless else "standard",
                    raw=raw,
                )
            )
        return items[:limit]

    async def resolve_url(self, song_id: str, quality: str = "") -> str:
        mid = (song_id or "").strip()
        if not mid:
            raise SourceError("empty qq song id")
        guid = str(uuid.uuid4()).replace("-", "")[:10] + str(int(time.time() * 1000) % 10**12)
        # 优先无损/高品质文件名，失败再降级
        candidates = [
            f"C400{mid}.m4a",
            f"M500{mid}.mp3",
            f"M800{mid}.mp3",
            f"F000{mid}.flac",
        ]
        payload = {
            "comm": {
                "ct": 24,
                "cv": 0,
                "guid": guid,
                "format": "json",
                "platform": "yqq.json",
            },
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "guid": guid,
                    "songmid": [mid],
                    "songtype": [0],
                    "uin": "0",
                    "loginflag": 1,
                    "platform": "20",
                },
            },
        }
        try:
            r = await self._client.get(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                params={"data": json.dumps(payload, ensure_ascii=False)},
                headers=self._headers(),
            )
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            data = None
            err = exc
        else:
            err = None

        url = ""
        if isinstance(data, dict):
            req0 = data.get("req_0") or {}
            inner = req0.get("data") or {}
            midurlinfo = inner.get("midurlinfo") or []
            sip = inner.get("sip") or []
            if midurlinfo and isinstance(midurlinfo, list):
                info = midurlinfo[0] or {}
                purl = str(info.get("purl") or "")
                filename = str(info.get("filename") or "")
                vkey = str(info.get("vkey") or "")
                if purl and sip:
                    base = str(sip[0] or "").rstrip("/")
                    if not purl.startswith("http"):
                        url = f"{base}/{purl}"
                elif filename and vkey and sip:
                    base = str(sip[0] or "").rstrip("/")
                    url = f"{base}/{filename}&vkey={vkey}"
                elif purl and purl.startswith("http"):
                    url = purl

        if not url:
            # 兜底：部分免费曲可走旧 CDN 模板（不带 vkey 多半 403，属预期）
            for fname in candidates:
                # 这里只尝试一种最常见的免费通路；真正取链仍以 vkey 为准
                trial = f"https://dl.stream.qqmusic.qq.com/{fname}?fromtag=0&guid={guid}"
                try:
                    probe = await self._client.head(trial, timeout=4.0)
                    if probe.status_code < 400:
                        url = trial
                        break
                except Exception:  # noqa: BLE001
                    continue

        if not url:
            if err is not None:
                raise SourceError(f"qq resolve failed: {err}")
            raise SourceError("qq resolve returned empty url")
        return url

    async def lyric(self, song_id: str) -> str:
        mid = (song_id or "").strip()
        if not mid:
            return ""
        try:
            r = await self._client.get(
                "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                params={
                    "songmid": mid,
                    "format": "json",
                    "nobase64": 1,
                    "g_tk": 5381,
                    "platform": "yqq.json",
                },
                headers={**self._headers(), "Referer": "https://y.qq.com/"},
            )
            data = r.json()
            text = str(data.get("lyric") or "")
            return ensure_lrc_text(text)
        except Exception:  # noqa: BLE001
            return ""

    async def detail(self, song_id: str) -> dict[str, Any]:
        """尽量补齐封面/时长；搜索结果里已有时可由调用方直接使用。"""
        mid = (song_id or "").strip()
        if not mid:
            return {}
        try:
            r = await self._client.get(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                params={
                    "data": json.dumps(
                        {
                            "comm": {"ct": 6, "cv": 80600, "format": "json"},
                            "song": {
                                "module": "music.pf_song_detail_svr",
                                "method": "get_song_detail_yqq",
                                "param": {"song_mid": mid},
                            },
                        },
                        ensure_ascii=False,
                    )
                },
            )
            data = r.json()
            info = ((data or {}).get("song") or {}).get("data") or {}
            track_info = info.get("track_info") or info or {}
            album = track_info.get("album") or {}
            album_mid = str(album.get("mid") or "") if isinstance(album, dict) else ""
            cover = (
                f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg"
                if album_mid
                else ""
            )
            singers = track_info.get("singer") or []
            artist = " / ".join(
                str(s.get("name") or "") for s in singers if isinstance(s, dict) and s.get("name")
            )
            return {
                "title": str(track_info.get("title") or track_info.get("name") or ""),
                "artist": artist,
                "album": str(album.get("name") or "") if isinstance(album, dict) else "",
                "cover_url": cover,
                "duration_s": parse_duration_ms(track_info.get("interval")),
            }
        except Exception:  # noqa: BLE001
            return {}
