"""扩展音源客户端与代理集成测试（mock musicsource-service）。"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import extra_sources
from proxy.app import app, CONF


EXTRA_SEARCH_PAYLOAD = {
    "ok": True,
    "data": [
        {
            "id": "003ABC",
            "source": "qq",
            "title": "夜曲",
            "artist": "周杰伦",
            "album": "十一月的萧邦",
            "duration_s": 225,
            "ext": "mp3",
            "cover_url": "https://img.qq/cover.jpg",
            "playable": True,
        },
        {
            "id": "KGHASH",
            "source": "kugou",
            "title": "夜曲",
            "artist": "周杰伦",
            "album": "",
            "duration_s": 225,
            "ext": "flac",
            "cover_url": "",
            "playable": True,
        },
    ],
}


def _ms_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/healthz":
        return httpx.Response(200, json={"status": "ok", "sources": {"qq": {"label": "QQ音乐"}}})
    if path == "/api/v1/search":
        return httpx.Response(200, json=EXTRA_SEARCH_PAYLOAD)
    if path.startswith("/api/v1/song/qq/003ABC/url"):
        return httpx.Response(200, json={
            "ok": True,
            "data": {"url": "http://cdn.test/qq/003ABC.mp3", "source": "qq", "id": "003ABC"},
        })
    if path.startswith("/api/v1/song/qq/003ABC/lyric"):
        return httpx.Response(200, json={
            "ok": True,
            "data": {"lyric": "[00:01.00]test", "source": "qq", "id": "003ABC"},
        })
    if path.startswith("/api/v1/song/qq/003ABC/detail"):
        return httpx.Response(200, json={
            "ok": True,
            "data": {
                "id": "qq:003ABC",
                "source": "qq",
                "title": "夜曲",
                "artist": "周杰伦",
                "album": "十一月的萧邦",
                "cover_url": "https://img.qq/cover.jpg",
                "duration_s": 225,
            },
        })
    return httpx.Response(404, json={"ok": False})


@pytest.fixture
def ms_client():
    transport = httpx.MockTransport(_ms_handler)
    return httpx.AsyncClient(transport=transport, base_url="http://musicsource.test")


@pytest.mark.asyncio
async def test_fetch_extra_search_maps_items(ms_client):
    items = await extra_sources.fetch_extra_search(ms_client, "夜曲", limit=10, sources=["qq", "kugou"])
    assert items is not None
    assert len(items) == 2
    assert items[0]["id"] == "qq:003ABC"
    assert items[1]["source"] == "kugou"
    assert items[1]["ext"] == "flac"


@pytest.mark.asyncio
async def test_resolve_url_ok(ms_client):
    url = await extra_sources.resolve_url(ms_client, "qq", "003ABC")
    assert url == "http://cdn.test/qq/003ABC.mp3"


@pytest.mark.asyncio
async def test_resolve_url_unknown_source(ms_client):
    assert await extra_sources.resolve_url(ms_client, "netease", "1") is None


@pytest.mark.asyncio
async def test_fetch_lyric_ok(ms_client):
    lyric = await extra_sources.fetch_lyric(ms_client, "qq", "003ABC")
    assert "test" in lyric


@pytest.mark.asyncio
async def test_fetch_detail(ms_client):
    info = await extra_sources.fetch_detail(ms_client, "qq", "003ABC")
    assert info["title"] == "夜曲"
    assert info["id"] == "qq:003ABC"


def test_enabled_sources_default(monkeypatch):
    monkeypatch.delenv("FNMUSIC_EXTRA_SOURCES", raising=False)
    monkeypatch.delenv("FNMUSIC_QQ_ENABLED", raising=False)
    monkeypatch.delenv("FNMUSIC_KUGOU_ENABLED", raising=False)
    monkeypatch.delenv("FNMUSIC_KUWO_ENABLED", raising=False)
    monkeypatch.delenv("FNMUSIC_QISHUI_ENABLED", raising=False)
    srcs = extra_sources.enabled_sources()
    assert set(srcs) >= {"qq", "kugou", "kuwo", "qishui"}


def test_enabled_sources_single_off(monkeypatch):
    monkeypatch.setenv("FNMUSIC_EXTRA_SOURCES", "qq,kugou,kuwo,qishui")
    monkeypatch.setenv("FNMUSIC_QISHUI_ENABLED", "false")
    srcs = extra_sources.enabled_sources()
    assert "qishui" not in srcs
    assert "qq" in srcs
    assert "kugou" in srcs


def test_healthz_includes_extra_sources(monkeypatch):
    """healthz 应包含 extra_sources 字段（服务不可达时 status=fail/disabled）。"""
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "extra_enabled", True)

    def _upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"list": []}})

    with TestClient(app) as client:
        app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream), base_url="http://unix"
        )
        app.state.musicbox_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://mb"
        )
        app.state.musicsource_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_ms_handler), base_url="http://ms"
        )
        resp = client.get("/_ext/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert "extra_sources" in body
        assert "status" in body["extra_sources"]
