"""play-start 日志去重（v2.9.24）。

真机上看到同一首歌 600ms 内刷出 10 行 play-start——它们不是「播了十次」，而是
同一次播放的十几个 Range 续传请求（ijk 起播探测 + 分片续传）。两个副作用：

1. 日志里「播了几首」被放大十倍，拿它评估预热命中率必然失真；
2. 本地直出（local-first / local-daily / tee-cache）压根没联网、也不吃预热，
   却被一律标成 cold，读起来像「因为没预热才慢」——而实际情况恰恰相反。

所以这个改动不是为了省几行日志，而是让 play-start 行重新能当证据用。
"""
from __future__ import annotations

import logging

import pytest

import proxy.app as P


@pytest.fixture(autouse=True)
def _reset_play_start():
    P._PLAY_START_AT.clear()
    P._PLAY_START_STATS.update({"logged": 0, "folded": 0})
    yield
    P._PLAY_START_AT.clear()
    P._PLAY_START_STATS.update({"logged": 0, "folded": 0})


def test_range_continuations_are_folded():
    """一次播放的十几个 Range 请求只算一次播放。"""
    assert P._play_start_fresh("g1") is True
    for _ in range(9):
        assert P._play_start_fresh("g1") is False
    st = P.play_start_stats()
    assert st["plays"] == 1
    assert st["folded"] == 9


def test_different_tracks_are_counted_separately():
    for i in range(5):
        assert P._play_start_fresh(f"g{i}") is True
    assert P.play_start_stats()["plays"] == 5


def test_window_expiry_counts_a_replay(monkeypatch):
    """过了窗口再播同一首 = 新的一次播放（不是续传）。"""
    monkeypatch.setenv("FNMUSIC_PLAY_START_DEDUPE_S", "0.05")
    assert P._play_start_fresh("g1", now=1000.0) is True
    assert P._play_start_fresh("g1", now=1000.01) is False   # 续传
    assert P._play_start_fresh("g1", now=1000.20) is True    # 隔了 200ms：重播


def test_window_zero_disables_dedupe(monkeypatch):
    monkeypatch.setenv("FNMUSIC_PLAY_START_DEDUPE_S", "0")
    for _ in range(5):
        assert P._play_start_fresh("g1") is True
    assert P.play_start_stats()["folded"] == 0


def test_peek_does_not_consume_the_slot():
    """旁路日志（local-first skip）跟着主行走，但不能抢走主行的去重额度。"""
    assert P._play_start_fresh("g1", consume=False) is True
    assert P._play_start_fresh("g1", consume=False) is True   # 不占位 → 仍 True
    assert P._play_start_fresh("g1") is True                  # 主行占位
    assert P._play_start_fresh("g1", consume=False) is False  # 已被占用
    st = P.play_start_stats()
    assert st["plays"] == 1
    assert st["folded"] == 0      # 窥探不该被算成「折叠掉的续传」


def test_requests_per_play_is_reported():
    P._play_start_fresh("g1")
    for _ in range(3):
        P._play_start_fresh("g1")
    assert P.play_start_stats()["requests_per_play"] == 4.0


def test_stats_are_safe_before_any_play():
    st = P.play_start_stats()
    assert st["plays"] == 0
    assert st["requests_per_play"] == 0.0     # 不能除零


def test_tracked_guids_are_bounded(monkeypatch):
    """听过的歌可能上千首，去重表不能跟着无限长。"""
    monkeypatch.setattr(P, "_PLAY_START_MAX", 8)
    for i in range(200):
        P._play_start_fresh(f"g{i}", now=float(i))
    assert len(P._PLAY_START_AT) <= P._PLAY_START_MAX + 1


def test_playstart_endpoint_reports():
    from fastapi.testclient import TestClient

    P._play_start_fresh("g1")
    P._play_start_fresh("g1")
    r = TestClient(P.app).get("/_ext/playstart")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["data"]["plays"] == 1
    assert r.json()["data"]["folded"] == 1


def test_local_daily_serves_one_play_start_line(monkeypatch, tmp_path, caplog):
    """端到端：本地文件直出重复请求只落一行，且标 local 而不是 cold。

    本地直出没有联网、也不吃预热，标 cold 会让人误读成「慢是因为没预热」。
    """
    f = tmp_path / "a.flac"
    f.write_bytes(b"fake-audio-bytes")

    async def _fake_path(request, guid):
        return str(f)

    monkeypatch.setattr(P, "_local_daily_path_of", _fake_path)
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        client = None
        try:
            from fastapi.testclient import TestClient

            client = TestClient(P.app)
            for _ in range(4):
                client.get("/music/api/v1/track/stream?guid=local:file:abc")
        finally:
            if client is not None:
                client.close()

    lines = [r.getMessage() for r in caplog.records if "play-start" in r.getMessage()]
    assert len(lines) == 1, f"4 次请求只该落 1 行 play-start，实际 {len(lines)}: {lines}"
    assert "local-daily" in lines[0]
    assert lines[0].endswith(" local")
