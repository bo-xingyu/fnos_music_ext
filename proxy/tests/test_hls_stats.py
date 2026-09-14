"""官方 HLS 实时转码的观测与「直出绕过」开关（v2.9.23）。

播飞牛本地曲库时官方后端把 FLAC 实时转码成 fMP4 分片，这条路径以前完全是黑盒：
只知道「慢」，分不清是**转码启动慢**（点下去要等）还是**播出后跟不上**（分片尖刺）。
两者解法完全不同，观测的价值就在把它们分开。
"""
from __future__ import annotations

import pytest

import proxy.app as P


@pytest.fixture(autouse=True)
def _reset_hls():
    P._HLS_STATS.update({"sessions": 0, "segments": 0, "seg_ms_sum": 0.0,
                         "seg_ms_max": 0.0, "first_ms_sum": 0.0,
                         "first_ms_max": 0.0, "first_n": 0, "bypassed": 0})
    P._HLS_PLAYLIST_AT.clear()
    yield
    P._HLS_STATS.update({"sessions": 0, "segments": 0, "seg_ms_sum": 0.0,
                         "seg_ms_max": 0.0, "first_ms_sum": 0.0,
                         "first_ms_max": 0.0, "first_n": 0, "bypassed": 0})
    P._HLS_PLAYLIST_AT.clear()


def test_segment_stats_average_and_spike():
    P._hls_note_segment("g1", "00010.m4s", 12.0)
    P._hls_note_segment("g1", "00011.m4s", 315.0)
    st = P.hls_stats()
    assert st["segments"] == 2
    assert st["seg_ms_avg"] == 163.5
    assert st["seg_ms_max"] == 315.0     # 抖动留证：转码跟不上就体现为尖刺


def test_first_segment_gap_is_measured_as_transcode_startup():
    """m3u8 → 首个分片的间隔 = 官方转码器初始化开销，也就是「点下去要等」的时间。

    只有把它单独量出来，才能判断该优化启动（提前建会话）还是该绕过转码。
    """
    P._HLS_PLAYLIST_AT["g1"] = P.time.time() - 1.5     # 1.5s 前拿到的 m3u8
    P._hls_note_segment("g1", "00000.m4s", 11.0)
    st = P.hls_stats()
    assert st["first_n"] == 1
    assert 1400 < st["first_ms_avg"] < 1700
    # 只记一次：第二个分片不该再算启动开销
    P._hls_note_segment("g1", "00001.m4s", 11.0)
    assert P.hls_stats()["first_n"] == 1


def test_non_numeric_segment_name_is_safe():
    P._hls_note_segment("g1", "preset.m3u8", 5.0)
    P._hls_note_segment("g1", "", 5.0)
    assert P.hls_stats()["first_n"] == 0
    assert P.hls_stats()["segments"] == 2


def test_stats_never_divide_by_zero():
    assert P.hls_stats()["seg_ms_avg"] == 0.0
    assert P.hls_stats()["first_ms_avg"] == 0.0


def test_bypass_defaults_off(monkeypatch):
    """默认关：客户端主动要 HLS 通常意味着它吃不下原始编码（本地多为 FLAC）。"""
    monkeypatch.delenv("FNMUSIC_HLS_LOCAL_BYPASS", raising=False)
    assert P.hls_local_bypass() is False
    assert P.hls_stats()["bypass_enabled"] is False


def test_bypass_can_be_enabled(monkeypatch):
    monkeypatch.setenv("FNMUSIC_HLS_LOCAL_BYPASS", "true")
    assert P.hls_local_bypass() is True


def test_direct_playlist_points_back_to_stream():
    resp = P._hls_direct_playlist("abc123", 240)
    body = resp.body.decode()
    assert "#EXT-X-ENDLIST" in body
    assert "/music/api/v1/track/stream?guid=abc123" in body
    assert "#EXTINF:240.000," in body


def test_ext_hls_endpoint_reports(monkeypatch):
    from fastapi.testclient import TestClient
    P._hls_note_segment("g1", "00002.m4s", 20.0)
    r = TestClient(P.app).get("/_ext/hls")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["data"]["segments"] == 1
