"""「播不出来」的留证（v2.9.27）。

真机反馈：「一首歌等 7~8 秒，然后自动跳过；手动切几首后彻底放不出声，飞牛音乐
闪退」。翻日志时最难堪的不是失败本身，而是**失败没有留下任何痕迹**——
`_online_unavailable()` 直接返回 404 就结束了，既不知道卡在哪一步，也不知道耗时
多久。诊断页那行「取链耗时 100ms」的均值再好看，也盖不住尾部那几次几秒的。

这组测试锁住两件事：
1. 每个失败出口都必须留证，且带**阶段**（resolve / open-cdn / slow-start）——
   阶段决定了排查方向，缺了它就只剩「跳过」这个现象；
2. HLS 慢分片必须能定位到曲目。诊断里「最大 30106.9ms / 平均 193.3ms」这样的
   155 倍尖刺，如果只有一个孤零零的 max，等于知道出事了却不知道是谁。
"""
from __future__ import annotations

import os

import pytest

import proxy.app as P
import proxy.prefetch as pf


@pytest.fixture(autouse=True)
def _clean():
    P._STREAM_FAILS.clear()
    yield
    P._STREAM_FAILS.clear()


# ---------------------------------------------------------------------------
# ① 失败留证：每次失败都要能查到「哪一步、多久」
# ---------------------------------------------------------------------------

def test_note_stream_fail_records_stage_and_ms():
    P._note_stream_fail("online:netease:123", "musicbox 超时", 7300.0, "resolve")
    data = P.stream_failures()
    assert data["count"] == 1
    item = data["items"][-1]
    assert item["guid"] == "online:netease:123"
    assert item["stage"] == "resolve"
    assert item["reason"] == "musicbox 超时"
    assert item["ms"] == 7300.0
    assert item["ts"]  # 有时间才能跟用户说的「刚才」对上


def test_failures_keep_most_recent_last():
    for i in range(5):
        P._note_stream_fail(f"g{i}", f"r{i}", float(i), "resolve")
    data = P.stream_failures()
    assert data["count"] == 5
    assert [it["guid"] for it in data["items"]] == [f"g{i}" for i in range(5)]


def test_failures_buffer_is_bounded():
    """环形缓冲必须有上限：代理是长驻进程，无界就是内存泄漏。"""
    for i in range(500):
        P._note_stream_fail(f"g{i}", "x", 1.0, "open-cdn")
    data = P.stream_failures()
    assert data["count"] == 40
    assert data["kept"] == 40
    assert data["items"][-1]["guid"] == "g499"


def test_note_stream_fail_never_raises():
    """留证本身绝不能把播放再搞挂一次。"""
    P._note_stream_fail(None, None, None, None)  # 全空也不许抛
    assert P.stream_failures()["count"] == 1


def test_ms_optional():
    P._note_stream_fail("g", "无耗时可用")
    item = P.stream_failures()["items"][-1]
    assert item["ms"] is None
    assert item["stage"] == ""


# ---------------------------------------------------------------------------
# ② HLS 慢分片：看得到「有一片 30 秒」，还得知道是哪首歌
# ---------------------------------------------------------------------------

def test_hls_slow_ms_default_and_env(monkeypatch):
    monkeypatch.delenv("FNMUSIC_HLS_SLOW_SEG_MS", raising=False)
    assert P._hls_slow_ms() == 5000.0
    monkeypatch.setenv("FNMUSIC_HLS_SLOW_SEG_MS", "2000")
    assert P._hls_slow_ms() == 2000.0
    # 阈值太低会把正常分片全算成慢的，那样这个计数就没有意义了
    monkeypatch.setenv("FNMUSIC_HLS_SLOW_SEG_MS", "10")
    assert P._hls_slow_ms() == 1000.0


def test_hls_slow_segment_is_counted_and_located(monkeypatch):
    monkeypatch.setenv("FNMUSIC_HLS_SLOW_SEG_MS", "5000")
    st = P._HLS_STATS
    saved = dict(st)
    try:
        st.clear()
        st.update({"segments": 0, "seg_ms_sum": 0.0, "seg_ms_max": 0.0, "seg_slow": 0,
                   "seg_ms_max_guid": "", "seg_ms_max_seg": "", "sessions": 0,
                   "first_ms_sum": 0.0, "first_ms_max": 0.0, "first_n": 0, "bypassed": 0})
        P._hls_note_segment("188dde79", "00003.m4s", 193.3)      # 正常
        P._hls_note_segment("188dde79", "00004.m4s", 30106.9)    # 尖刺
        P._hls_note_segment("188dde79", "00005.m4s", 8200.0)     # 也慢

        stats = P.hls_stats()
        assert stats["seg_slow"] == 2
        assert stats["seg_ms_max"] == 30106.9
        # 定位不到曲目的 max 等于白记：诊断里就只能写「有一片 30 秒」
        assert stats["seg_ms_max_guid"] == "188dde79"
        assert stats["seg_ms_max_seg"] == "00004.m4s"
        assert stats["seg_slow_ms"] == 5000.0
    finally:
        st.clear()
        st.update(saved)


def test_hls_slow_threshold_not_counted_below(monkeypatch):
    monkeypatch.setenv("FNMUSIC_HLS_SLOW_SEG_MS", "5000")
    st = P._HLS_STATS
    saved = dict(st)
    try:
        st.clear()
        st.update({"segments": 0, "seg_ms_sum": 0.0, "seg_ms_max": 0.0, "seg_slow": 0,
                   "seg_ms_max_guid": "", "seg_ms_max_seg": ""})
        P._hls_note_segment("g", "00001.m4s", 4999.0)
        assert P.hls_stats()["seg_slow"] == 0
    finally:
        st.clear()
        st.update(saved)


# ---------------------------------------------------------------------------
# ③ 预热 lookahead 收敛：真实机 LOOKAHEAD=5 + max_queue=2 的空转
# ---------------------------------------------------------------------------

def test_lookahead_clamped_to_three(monkeypatch):
    """真机 .env 里的 5（升级不覆盖用户值）配 max_queue=2，结果是 6 次被队列拒、
    11 次撞上「已预热过」——预热全在空转却占着单进程的 musicbox。"""
    monkeypatch.setenv("FNMUSIC_PREFETCH_LOOKAHEAD", "5")
    assert pf._lookahead() == 3
    # 用户写的值要原样暴露出来，诊断页才能明确告诉他「已收敛」
    assert pf.lookahead_requested() == 5


def test_lookahead_floor_is_one(monkeypatch):
    monkeypatch.setenv("FNMUSIC_PREFETCH_LOOKAHEAD", "0")
    assert pf._lookahead() == 1
    monkeypatch.setenv("FNMUSIC_PREFETCH_LOOKAHEAD", "2")
    assert pf._lookahead() == 2


def test_status_exposes_requested_lookahead(monkeypatch):
    monkeypatch.setenv("FNMUSIC_PREFETCH_LOOKAHEAD", "5")
    st = pf.status()
    assert st["lookahead"] == 3
    assert st["lookahead_requested"] == 5
