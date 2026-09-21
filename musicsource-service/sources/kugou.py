"""酷狗音乐适配器。

搜索用 mobilecdn 公开接口；取链优先 wwwapi play/getdata（免费曲常见可用），
失败时尝试 trackercdn 与 m.kugou 的 playInfo。
"""
from __future__ import annotations

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

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.kugou.com/",
}


class KugouSource:
    key = "kugou"
    label = "酷狗音乐"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(headers=HEADERS, timeout=12.0, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthz(self) -> bool:
        return True

    async def search(self, keyword: str, limit: int = 20) -> list[SourceSong]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        url = "https://mobilecdn.kugou.com/api/v3/search/song"
        params = {
            "format": "json",
            "keyword": keyword,
            "page": 1,
            "pagesize": max(1, min(int(limit), 50)),
            "showtype": 1,
            "userid": 0,
            "clientver": "11539",
            "platform": "Android",
            "iscorrection": 1,
            "privilege_filter": 0,
        }
        try:
            r = await self._client.get(url, params=params)
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"kugou search failed: {exc}") from exc

        info = ((data or {}).get("data") or {}).get("info") or []
        items: list[SourceSong] = []
        for raw in info:
            if not isinstance(raw, dict):
                continue
            hash_ = str(raw.get("hash") or raw.get("FileHash") or "").strip()
            song_id = str(raw.get("audio_id") or raw.get("songid") or hash_).strip()
            if not song_id:
                song_id = hash_
            if not hash_ and not song_id:
                continue
            # id 优先 hash（取链更稳），无 hash 时用 audio_id
            stable_id = hash_ or song_id
            album_id = str(raw.get("album_id") or raw.get("albumid") or "")
            # 把 album_id 编进 raw，取链时用
            quality = str(raw.get("remark") or raw.get("quality") or "")
            items.append(
                SourceSong(
                    id=stable_id,
                    source=self.key,
                    title=str(raw.get("songname") or raw.get("song_name") or raw.get("name") or ""),
                    artist=str(raw.get("singername") or raw.get("singer") or ""),
                    album=str(raw.get("album_name") or raw.get("albumname") or ""),
                    duration_s=parse_duration_ms(raw.get("duration") or raw.get("timelength")),
                    ext="flac" if any(x in quality.upper() for x in ("SQ", "FLAC", "VIP", "无损")) else "mp3",
                    cover_url=str(raw.get("image") or raw.get("cover") or raw.get("album_img") or "").replace("{size}", "300"),
                    playable=True,
                    quality=quality,
                    raw={"hash": hash_, "album_id": album_id, "audio_id": song_id},
                )
            )
        return items[:limit]

    async def resolve_url(self, song_id: str, quality: str = "") -> str:
        """song_id 期望是 hash；也兼容 `hash|album_id` 形式。"""
        hash_, album_id = self._split_id(song_id)
        if not hash_:
            raise SourceError("empty kugou song id")

        headers = {
            **HEADERS,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        }
        attempts: list[tuple[str, dict]] = [
            (
                "https://wwwapi.kugou.com/yy/index.php",
                {
                    "r": "play/getdata",
                    "hash": hash_,
                    "album_id": album_id or "0",
                    "dfid": "-",
                    "mid": "fnmusic_ext",
                    "platid": "4",
                },
            ),
            (
                "http://trackercdn.kugou.com/i/v2/",
                {
                    "cmd": 23,
                    "pid": 1,
                    "behavior": "play",
                    "hash": hash_,
                },
            ),
            (
                "https://m.kugou.com/app/i/getSongInfo.php",
                {
                    "cmd": "playInfo",
                    "hash": hash_,
                },
            ),
        ]
        last_err: Exception | None = None
        for url, params in attempts:
            try:
                r = await self._client.get(url, params=params, headers=headers)
                data = r.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
            play_url = self._extract_url(data)
            if play_url:
                return play_url
        raise SourceError(f"kugou resolve failed for {hash_}: {last_err or 'empty url'}")

    @staticmethod
    def _split_id(song_id: str) -> tuple[str, str]:
        raw = (song_id or "").strip()
        if "|" in raw:
            h, _, a = raw.partition("|")
            return h.strip(), a.strip()
        return raw, ""

    @staticmethod
    def _extract_url(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        # wwwapi play/getdata
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("play_url", "url", "play_url_320", "play_url_128", "trans_param"):
                val = inner.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return val
            if isinstance(inner.get("trans_param"), dict):
                url = (inner["trans_param"] or {}).get("musicpack_advance") or ""
            else:
                url = ""
            if isinstance(url, str) and url.startswith("http"):
                return url
        # trackercdn
        if isinstance(data.get("url"), str) and str(data["url"]).startswith("http"):
            return str(data["url"])
        # m.kugou playInfo
        for key in ("url", "url_backup", "playUrl"):
            val = data.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
        return ""

    async def lyric(self, song_id: str) -> str:
        hash_, _ = self._split_id(song_id)
        if not hash_:
            return ""
        try:
            r = await self._client.get(
                "https://m.kugou.com/app/i/krc.php",
                params={"cmd": 100, "timelength": 999999, "hash": hash_},
                headers={**HEADERS, "Accept": "*/*"},
            )
            # krc 接口有时返回纯文本 LRC，有时是加密 krc（不可读）；仅透传可识别文本
            text = r.text
            if text and text.lstrip().startswith("["):
                return ensure_lrc_text(text)
            # 再试公开歌词接口
            r2 = await self._client.get(
                "https://krcs.kugou.com/search",
                params={"ver": 1, "man": "yes", "client": "mobi", "keyword": "", "duration": 999999, "hash": hash_},
            )
            data = r2.json()
            candidates = (data or {}).get("candidates") or []
            if candidates and isinstance(candidates[0], dict):
                accesskey = candidates[0].get("accesskey")
                id_ = candidates[0].get("id")
                r3 = await self._client.get(
                    "https://lyrics.kugou.com/download",
                    params={"ver": 1, "client": "pc", "id": id_, "accesskey": accesskey, "fmt": "lrc"},
                )
                d3 = r3.json()
                content = d3.get("content") or ""
                if content:
                    import base64

                    try:
                        return ensure_lrc_text(base64.b64decode(content).decode("utf-8", "ignore"))
                    except Exception:  # noqa: BLE001
                        return ensure_lrc_text(content)
            return ensure_lrc_text(text)
        except Exception:  # noqa: BLE001
            return ""

    async def detail(self, song_id: str) -> dict[str, Any]:
        return {}
