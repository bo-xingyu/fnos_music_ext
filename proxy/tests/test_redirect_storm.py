"""302 重试风暴自动回退（v2.9.30）的单元测试。

背景：非局域网 302 让客户端直连 CDN，代价是**客户端与 CDN 之间这一程我们完全
看不见**。真机出现过同一首歌 2.5 秒里被请求 9 次、然后被切到下一首——正常播放时
客户端拿一次直链就该自己去拉很久（对比另一首：全程 1 次请求，之后 31 秒无动静）。

核心不变量：
1. **判据是客户端的行为，不是我们的猜测**。我们看不见 CDN 那一程，但看得见
   客户端是不是在反复回来要同一首歌；反复回来 = 上一次直连没取到音频。
2. **回退的路必须是已知安全的**：中转是 2.9.28 之前一直在用的老路，不是新逻辑。
   所以「判定失败 → 走老路」，出问题时最多退化到旧版本行为，不会更糟。
3. **窗口外的旧请求必须失效**，否则一首歌播过几次之后会被永久误判成风暴，
   从此再也不走直连——那就等于把开关关了，还关得很隐蔽。
4. 告警每首只报一次，否则一次播放刷出十几行，等于用新问题换旧问题。
"""
from __future__ import annotations

import logging
import time
from collections import deque

import pytest

import proxy.app as P


@pytest.fixture(autouse=True)
def _clean():
    saved_hits = P._STREAM_HITS.copy()
    saved_logged = set(P._REDIRECT_STORM_LOGGED)
    P._STREAM_HITS.clear()
    P._REDIRECT_STORM_LOGGED.clear()
    yield
    P._STREAM_HITS.clear()
    P._STREAM_HITS.update(saved_hits)
    P._REDIRECT_STORM_LOGGED.clear()
    P._REDIRECT_STORM_LOGGED.update(saved_logged)


def test_first_requests_are_not_storm():
    """正常播放：拿一次直链就自己去拉，不该被误判。"""
    assert P.redirect_storm("g") is False
    assert P.redirect_storm("g") is False


def test_third_request_in_window_is_storm():
    assert P.redirect_storm("g") is False
    assert P.redirect_storm("g") is False
    assert P.redirect_storm("g") is True
    assert P.redirect_storm("g") is True


def test_old_requests_fall_out_of_window():
    """窗口外的旧请求必须失效，否则一首歌会被永久误判、从此再不走直连。"""
    now = time.monotonic()
    P._STREAM_HITS["g"] = deque([now - 100.0, now - 90.0, now - 80.0], maxlen=8)
    assert P.redirect_storm("g") is False


def test_storm_is_per_guid():
    for _ in range(3):
        P.redirect_storm("a")
    assert P.redirect_storm("b") is False


def test_tracked_guids_are_bounded():
    """不能无上限增长——长时间播放会累积很多曲目。"""
    limit = P._REDIRECT_STORM_MAX_GUID
    for i in range(limit + 40):
        P.redirect_storm("g%d" % i)
    assert len(P._STREAM_HITS) <= limit


def test_note_redirect_storm_logs_once(caplog):
    with caplog.at_level(logging.WARNING, logger="fnmusic_proxy"):
        P._note_redirect_storm("g", 3)
        P._note_redirect_storm("g", 4)
        P._note_redirect_storm("g", 5)
    hits = [r for r in caplog.records if "放弃直连" in r.getMessage()]
    assert len(hits) == 1
    assert "中转" in hits[0].getMessage()


def test_note_redirect_storm_leaves_evidence():
    """风暴要能被诊断页看到——否则跟 2.9.27 之前一样，失败不留痕。"""
    P._note_redirect_storm("storm-guid", 9)
    items = P.stream_failures().get("items") or []
    assert any("redirect-storm" == str(it.get("stage")) for it in items)


def test_stream_mode_stats_exposes_storm():
    P._note_redirect_storm("g", 3)
    st = P.stream_mode_stats()
    assert st["storm"] >= 1
    assert st["storm_n"] == P._REDIRECT_STORM_N
    assert st["storm_window_s"] == P._REDIRECT_STORM_WINDOW_S


@pytest.mark.parametrize("url,expect", [
    ("https://m10.music.126.net/20260916/abc.mp3", "https://m10.music.126.net"),
    ("http://m701.music.126.net/x/y.mp3", "http://m701.music.126.net"),
    ("https://p2.music.126.net:443/a.flac", "https://p2.music.126.net:443"),
])
def test_url_scheme_host(url, expect):
    """scheme 必须留：明文 http 是「302 出去却取不到」最可能的原因之一。
    路径必须丢：那是直链令牌，不该进日志。"""
    assert P._url_scheme_host(url) == expect


def test_url_scheme_host_never_raises():
    for bad in ("", "not a url", None, 123):
        assert P._url_scheme_host(bad) == "?"
