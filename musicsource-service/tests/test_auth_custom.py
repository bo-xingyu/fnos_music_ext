"""扩展音源登录与自定义音源：auth_store / custom 配置测试。"""
from __future__ import annotations

import json

import pytest

from sources import auth_store
from sources.custom import CustomSource, KEY_RE, load_custom_configs, save_custom_configs


def test_auth_store_cookie_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSICSOURCE_DATA", str(tmp_path))
    rec = auth_store.set_cookie("qq", "uin=1; qm_keyst=abc", nickname="tester")
    assert rec["logged_in"] is True
    st = auth_store.status("qq")
    assert st["has_cookie"] is True
    assert st["nickname"] == "tester"
    assert "qm_keyst" not in st["cookie_preview"] or "…" in st["cookie_preview"]
    assert auth_store.cookie_of("qq").startswith("uin=1")
    assert auth_store.clear("qq") is True
    assert auth_store.cookie_of("qq") == ""


def test_auth_store_rejects_bad_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSICSOURCE_DATA", str(tmp_path))
    with pytest.raises(ValueError):
        auth_store.set_cookie("../evil", "x=1")


def test_custom_source_key_re():
    assert KEY_RE.match("mymusic")
    assert KEY_RE.match("src-1")
    assert not KEY_RE.match("1bad")
    assert not KEY_RE.match("has space")


def test_custom_config_save_load(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSICSOURCE_DATA", str(tmp_path))
    items = [{
        "key": "mymusic",
        "label": "自建",
        "search_url": "http://api.test/search?q={keyword}",
        "list_path": "songs",
        "url_url": "http://api.test/url?id={id}",
        "url_path": "url",
    }]
    saved = save_custom_configs(items)
    assert saved[0]["key"] == "mymusic"
    loaded = load_custom_configs()
    assert any(c["key"] == "mymusic" for c in loaded)

    with pytest.raises(ValueError):
        save_custom_configs([{"key": "bad key", "search_url": "http://x"}])


@pytest.mark.asyncio
async def test_custom_source_search_and_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_MUSICSOURCE_DATA", str(tmp_path))
    import httpx
    from sources.custom import CustomSource

    def handler(request: httpx.Request) -> httpx.Response:
        if "search" in str(request.url):
            return httpx.Response(200, json={"songs": [
                {"id": "s1", "name": "晴天", "artist": "周杰伦", "duration": 269},
            ]})
        if "url" in str(request.url):
            return httpx.Response(200, json={"url": "http://cdn.test/s1.mp3"})
        return httpx.Response(404)

    cfg = {
        "key": "mymusic",
        "label": "自建",
        "search_url": "http://api.test/search?q={keyword}&limit={limit}",
        "list_path": "songs",
        "map": {"id": "id", "title": "name", "artist": "artist", "duration": "duration"},
        "url_url": "http://api.test/url?id={id}",
        "url_path": "url",
    }
    src = CustomSource(cfg)
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://api.test")
    items = await src.search("晴天", limit=5)
    assert items and items[0].title == "晴天"
    assert items[0].source == "mymusic"
    url = await src.resolve_url("s1")
    assert url == "http://cdn.test/s1.mp3"
