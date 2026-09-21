"""自定义音源：按用户配置的 HTTP 模板调用任意兼容接口。

配置文件：``{data_dir}/custom_sources.json``（JSON 数组）。
也可用环境变量 ``FNMUSIC_CUSTOM_SOURCES``（同一 JSON 字符串）覆盖合并。

每项字段：
  key          必填，字母数字下划线
  label        显示名
  enabled      默认 true
  cookie       可选 Cookie
  headers      可选附加请求头
  search_url   必填，支持 {keyword} {limit}
  search_method GET/POST，默认 GET
  search_body  POST 时的 JSON 模板（支持 {keyword} {limit}）
  list_path    搜索结果列表的点路径，如 data.songs；空则取 data 本身若是 list
  map          字段映射：id/title/artist/album/duration/cover/quality/ext
  url_url      取直链，支持 {id}；返回 JSON 里 url_path 指向的字符串
  url_path     直链字段点路径，默认 url
  lyric_url    歌词接口，支持 {id}
  lyric_path   歌词字段点路径，默认 lyric
  detail_url   可选详情
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .base import (
    SourceError,
    SourceSong,
    ensure_lrc_text,
    env_flag,
    guess_ext_from_url,
    parse_duration_ms,
)

KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]{0,31}$")

DEFAULT_MAP = {
    "id": "id",
    "title": "title",
    "artist": "artist",
    "album": "album",
    "duration": "duration",
    "cover": "cover",
    "quality": "quality",
    "ext": "ext",
}


def custom_config_path() -> Path:
    from . import auth_store

    p = auth_store.data_dir() / "custom_sources.json"
    return p


def _dig(obj: Any, path: str, default: Any = None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:  # noqa: BLE001
                return default
        else:
            return default
        if cur is None:
            return default
    return cur if cur is not None else default


def load_custom_configs() -> list[dict]:
    items: list[dict] = []
    # 环境变量优先（可整表覆盖）
    raw_env = (os.environ.get("FNMUSIC_CUSTOM_SOURCES") or "").strip()
    if raw_env:
        try:
            data = json.loads(raw_env)
            if isinstance(data, list):
                items.extend(x for x in data if isinstance(x, dict))
            elif isinstance(data, dict):
                items.extend(data.values())
        except Exception:  # noqa: BLE001
            pass
    path = custom_config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                # 文件补充：同 key 时 env 已有的优先
                env_keys = {str(x.get("key") or "").lower() for x in items}
                for x in data:
                    if isinstance(x, dict) and str(x.get("key") or "").lower() not in env_keys:
                        items.append(x)
        except Exception:  # noqa: BLE001
            pass
    cleaned = []
    for raw in items:
        key = str(raw.get("key") or "").strip()
        if not KEY_RE.match(key):
            continue
        cleaned.append(raw)
    return cleaned


def save_custom_configs(items: list[dict]) -> list[dict]:
    cleaned = []
    for raw in items:
        key = str(raw.get("key") or "").strip()
        if not KEY_RE.match(key):
            raise ValueError(f"自定义音源 key 非法: {key!r}（字母开头，字母数字_-）")
        if not str(raw.get("search_url") or "").strip():
            raise ValueError(f"自定义音源 {key} 缺少 search_url")
        cleaned.append(raw)
    path = custom_config_path()
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return cleaned


class CustomSource:
    """由配置驱动的通用音源适配器。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.key = str(cfg.get("key") or "").strip()
        self.label = str(cfg.get("label") or self.key)
        self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return str(self.cfg.get("enabled", True)).lower() not in ("false", "0", "no", "off")

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": str(self.cfg.get("user_agent")
                              or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
        }
        cookie = str(self.cfg.get("cookie") or "").strip()
        if not cookie:
            from . import auth_store

            try:
                cookie = auth_store.cookie_of(self.key)
            except Exception:  # noqa: BLE001
                cookie = ""
        if cookie:
            headers["Cookie"] = cookie
        extra = self.cfg.get("headers")
        if isinstance(extra, dict):
            for k, v in extra.items():
                headers[str(k)] = str(v)
        return headers

    def _fill(self, template: str, **kw: Any) -> str:
        s = str(template or "")
        for k, v in kw.items():
            s = s.replace("{" + k + "}", quote(str(v), safe="") if k in ("keyword",) else str(v))
            # keyword 也保留未编码形式，便于用户写已编码模板
            s = s.replace("{keyword_raw}", str(kw.get("keyword", "")))
        return s

    def _map_fields(self) -> dict[str, str]:
        m = dict(DEFAULT_MAP)
        user = self.cfg.get("map")
        if isinstance(user, dict):
            for k, v in user.items():
                if v:
                    m[str(k)] = str(v)
        return m

    async def healthz(self) -> bool:
        return True

    async def search(self, keyword: str, limit: int = 20) -> list[SourceSong]:
        if not self.enabled:
            return []
        url_t = str(self.cfg.get("search_url") or "")
        if not url_t:
            return []
        method = str(self.cfg.get("search_method") or "GET").upper()
        url = self._fill(url_t, keyword=keyword, limit=limit, q=keyword)
        headers = self._headers()
        try:
            if method == "POST":
                body_t = self.cfg.get("search_body")
                if isinstance(body_t, dict):
                    body = json.loads(self._fill(json.dumps(body_t, ensure_ascii=False),
                                                 keyword=keyword, limit=limit, q=keyword))
                else:
                    body = {"keyword": keyword, "limit": limit}
                r = await self._client.post(url, json=body, headers=headers)
            else:
                r = await self._client.get(url, headers=headers)
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"custom:{self.key} search failed: {exc}") from exc

        list_path = str(self.cfg.get("list_path") or "").strip()
        if list_path:
            raw_list = _dig(data, list_path, [])
        elif isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            raw_list = data.get("data") if isinstance(data.get("data"), list) else \
                data.get("songs") or data.get("list") or data.get("result") or []
        else:
            raw_list = []
        if not isinstance(raw_list, list):
            return []

        fmap = self._map_fields()
        items: list[SourceSong] = []
        for raw in raw_list[: max(1, int(limit))]:
            if not isinstance(raw, dict):
                continue
            sid = str(_dig(raw, fmap["id"], "") or "").strip()
            title = str(_dig(raw, fmap["title"], "") or "").strip()
            if not sid or not title:
                continue
            quality = str(_dig(raw, fmap["quality"], "") or "")
            ext = str(_dig(raw, fmap["ext"], "") or "") or guess_ext_from_url("", quality)
            cover = str(_dig(raw, fmap["cover"], "") or "")
            items.append(
                SourceSong(
                    id=sid,
                    source=self.key,
                    title=title,
                    artist=str(_dig(raw, fmap["artist"], "") or ""),
                    album=str(_dig(raw, fmap["album"], "") or ""),
                    duration_s=parse_duration_ms(_dig(raw, fmap["duration"], 0)),
                    ext=ext or "mp3",
                    cover_url=cover,
                    playable=True,
                    quality=quality,
                    raw=raw,
                )
            )
        return items

    async def resolve_url(self, song_id: str, quality: str = "") -> str:
        url_t = str(self.cfg.get("url_url") or "")
        if not url_t:
            raise SourceError(f"custom:{self.key} 未配置 url_url")
        url = self._fill(url_t, id=song_id, quality=quality)
        try:
            r = await self._client.get(url, headers=self._headers())
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"custom:{self.key} url failed: {exc}") from exc
        path = str(self.cfg.get("url_path") or "url")
        val = _dig(data, path, None)
        if isinstance(val, str) and val.startswith("http"):
            return val
        # 兜底常见字段
        for p in (path, "url", "data.url", "result.url", "play_url"):
            v = _dig(data, p, None)
            if isinstance(v, str) and v.startswith("http"):
                return v
        if isinstance(val, dict):
            for k in ("url", "play_url", "src"):
                if isinstance(val.get(k), str) and str(val[k]).startswith("http"):
                    return str(val[k])
        raise SourceError(f"custom:{self.key} url 返回中未找到直链（path={path}）")

    async def lyric(self, song_id: str) -> str:
        url_t = str(self.cfg.get("lyric_url") or "")
        if not url_t:
            return ""
        url = self._fill(url_t, id=song_id)
        try:
            r = await self._client.get(url, headers=self._headers())
            data = r.json()
            path = str(self.cfg.get("lyric_path") or "lyric")
            val = _dig(data, path, None)
            if val is None:
                for p in ("lyric", "lrc", "data.lyric", "data.lrc", "result.lrc"):
                    val = _dig(data, p, None)
                    if val:
                        break
            return ensure_lrc_text(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False) if val else "")
        except Exception:  # noqa: BLE001
            # 纯文本歌词
            try:
                r = await self._client.get(url, headers=self._headers())
                return ensure_lrc_text(r.text)
            except Exception:  # noqa: BLE001
                return ""

    async def detail(self, song_id: str) -> dict[str, Any]:
        return {}


def build_custom_sources() -> dict[str, CustomSource]:
    out: dict[str, CustomSource] = {}
    for cfg in load_custom_configs():
        src = CustomSource(cfg)
        if src.enabled and src.key:
            out[src.key] = src
    return out
