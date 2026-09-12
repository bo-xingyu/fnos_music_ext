"""v2.4 回归：本地曲目转码链路（/track/transcode + /track/hls）必须完全透明。

真机现象：启用扩展后，客户端开启转码播放本地音乐全部失败；关闭转码
（直接 /track/stream 原始流）则一切正常。两者经过代理的差别只有两处：

1. ``Host`` 头被剥掉。官方后端在转码/HLS 链路里按请求 Host 拼绝对地址
   （m3u8 分片 URL 等），代理转发时 httpx 发的是 ``Host: unix``，后端拼出的
   地址客户端连不上。非转码路径用相对 URL，所以平时看不出来。
2. 共享上游客户端 30s 读超时。/track/transcode 要等 ffmpeg 产出首分片，
   慢盘大文件时 30s 不够，超时异常让「能播」变 504。

这里用 mock 官方后端把整条本地转码链路（start → m3u8 → 分片 → 心跳）
过一遍，并断言 Host 头透传、播放链路放宽超时、超时返回 504 而不是 500。
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import app as proxy_app
from proxy.app import app, forward_to_upstream, copy_incoming_headers, PLAYBACK_FORWARD_TIMEOUT_S


SEEN: dict = {}


def upstream_handler(request: httpx.Request) -> httpx.Response:
    SEEN.setdefault("requests", []).append({
        "method": request.method,
        "path": request.url.path,
        "host": request.headers.get("host", ""),
        "body": (request.read().decode("utf-8", "replace") if request.method == "POST" else ""),
    })
    path = request.url.path
    if path == "/music/api/v1/track/transcode":
        return httpx.Response(200, json={
            "code": 0, "msg": "ok",
            "data": {"guid": "local-1", "status": "ready",
                     "url": "/music/api/v1/track/hls/local-1/preset.m3u8"},
        })
    if path == "/music/api/v1/track/transcode/heartbeat":
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})
    if path == "/music/api/v1/track/hls/local-1/preset.m3u8":
        return httpx.Response(200, content=(
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:9.0,\nseg-0.ts\n#EXT-X-ENDLIST\n"
        ).encode(), headers={"content-type": "application/vnd.apple.mpegurl"})
    if path == "/music/api/v1/track/hls/local-1/seg-0.ts":
        return httpx.Response(200, content=b"TS" * 1024,
                              headers={"content-type": "video/mp2t"})
    return httpx.Response(404, text=f"unexpected {path}")


@pytest.fixture()
def wired():
    SEEN.clear()
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix")
    app.state.musicbox_client = httpx.AsyncClient(base_url="http://127.0.0.1:8770")
    yield
    SEEN.clear()


def test_local_transcode_chain_is_transparent(wired):
    """本地 guid 的 转码启动/心跳/清单/分片 全部原样到达官方后端。"""
    with TestClient(app) as client:
        r = client.post("/music/api/v1/track/transcode",
                        json={"guid": "local-1", "preset": "aac"},
                        headers={"Host": "nas.example.com:5667"})
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "ready"

        hb = client.post("/music/api/v1/track/transcode/heartbeat",
                         json={"guid": "local-1"},
                         headers={"Host": "nas.example.com:5667"})
        assert hb.status_code == 200

        m3u8 = client.get("/music/api/v1/track/hls/local-1/preset.m3u8",
                          headers={"Host": "nas.example.com:5667"})
        assert m3u8.status_code == 200
        assert "#EXTM3U" in m3u8.text

        seg = client.get("/music/api/v1/track/hls/local-1/seg-0.ts",
                         headers={"Host": "nas.example.com:5667"})
        assert seg.status_code == 200
        assert len(seg.content) == 2048

    reqs = {r["path"]: r for r in SEEN["requests"]}
    assert "/music/api/v1/track/transcode" in reqs
    assert "/music/api/v1/track/transcode/heartbeat" in reqs
    assert "/music/api/v1/track/hls/local-1/preset.m3u8" in reqs
    assert "/music/api/v1/track/hls/local-1/seg-0.ts" in reqs
    # 请求体必须原样转发（guid 不能被代理吃掉）
    assert '"guid"' in reqs["/music/api/v1/track/transcode"]["body"]


def test_host_header_is_forwarded_to_upstream(wired):
    """Host 头必须透传：官方后端按它拼转码 m3u8 的绝对地址。"""
    with TestClient(app) as client:
        client.post("/music/api/v1/track/transcode", json={"guid": "local-1"},
                    headers={"Host": "nas.example.com:5667"})
    reqs = [r for r in SEEN["requests"] if r["path"] == "/music/api/v1/track/transcode"]
    assert reqs, "请求必须真的到达官方后端"
    assert reqs[0]["host"] == "nas.example.com:5667", (
        "Host 必须原样透传；发成 unix 会让后端拼出客户端连不上的绝对地址"
    )


def test_copy_incoming_headers_keeps_host():
    class _H:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=None):
            return self._d.get(k, default)

        def items(self):
            return self._d.items()

    class _R:
        headers = _H({"host": "nas.example.com:5667", "cookie": "a=b",
                      "content-length": "5", "accept-encoding": "gzip"})

    headers = copy_incoming_headers(_R())
    assert headers.get("host") == "nas.example.com:5667"
    assert headers.get("cookie") == "a=b"
    assert "content-length" not in headers
    assert headers.get("accept-encoding") == "identity"


@pytest.mark.anyio
async def test_forward_timeout_extension_and_504(wired):
    """播放链路放宽读超时；真超时时返回 504 而不是裸 500。"""
    assert PLAYBACK_FORWARD_TIMEOUT_S >= 120, "播放链路读超时必须显著大于共享 30s"

    def slow_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ffmpeg 还没出片")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(slow_handler), base_url="http://unix")

    class _FakeReq:
        method = "POST"
        url = type("U", (), {"path": "/music/api/v1/track/transcode", "query": ""})()
        headers = httpx.Headers({"host": "nas"})

        async def body(self):
            return b'{"guid":"local-1"}'

    resp = await forward_to_upstream(_FakeReq(), app.state.upstream_client,
                                     timeout=PLAYBACK_FORWARD_TIMEOUT_S)
    assert resp.status_code == 504, "上游超时应答 504，别让异常炸成 500"


def test_per_request_timeout_reaches_extension_dict():
    """build_request 的 timeout 必须放进 extensions，否则 httpx 不认。"""
    client = httpx.AsyncClient(base_url="http://unix")
    req = client.build_request(
        "GET", "/x",
        extensions={"timeout": httpx.Timeout(connect=10.0, read=300.0,
                                             write=30.0, pool=30.0).as_dict()})
    assert req.extensions["timeout"]["read"] == 300.0
