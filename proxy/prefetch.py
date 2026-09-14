"""下一首预热（T1）：播放当前曲目时，把「下一首」的直链与元数据提前取回来。

## 为什么需要它

真机实测（`proxy.log`）：

```
play-start netease online:netease:96100 519ms
```

一首没缓存的在线歌，从点击到出声要 519ms。这段时间的绝大部分花在
``asyncio.gather(resolve_netease_url, _online_info)``——两个 musicbox 往返
（`/song/{id}/url`、`/song/{id}/info` + `/lyric`），起步等的是两者的较慢者。

而客户端**完全不做预取**：播放开始后的十几秒里只请求 metadata / 封面 /
heartbeat / 上报，没有任何对下一首的动作（2026-09-14 真机日志实锤）。
所以要提速只能由代理主动做。

## 「下一首是谁」——没有上下文参数，只能推断

```
GET /music/api/v1/track/stream?guid=online%3Anetease%3A96100
```

请求里只有 guid，没有 playlistId / context。但**列表是我们下发的**：客户端播
之前必然拉过歌单曲目（`/track/playlist-detail/list`），那份列表的顺序我们
知道。于是：

* 把我们下发过的有序列表记进「最近上下文」环形缓冲；
* stream 到达时，用当前 guid 在最近上下文里定位 → 后一个就是下一首。

随机播放时这个推断会错——所以本模块只做**零音频流量**的预热（几个 KB 的
JSON），猜错不产生任何实质代价；整首预下载（T2）必须先解决这个不确定性。

## 设计约束

* 永不阻塞当前播放：全是后台任务，失败静默只记日志；
* 永不重复：按 guid 单飞（inflight 去重），已预热过的不再重复；
* 可关：`FNMUSIC_PREFETCH_NEXT=false`；
* 可证：统计调度/完成/失败次数与「预热命中率」，诊断页直接给出。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("fnmusic_proxy")

# 记住多少个「最近下发过的列表」。多于这个数的最久未用者被丢弃。
MAX_CONTEXTS = 8
# 单个列表最多记多少首（防止超大歌单把内存撑爆）
MAX_TRACKS = 3000
# 上下文多久算过期（秒）。太久没播放过说明用户早就不在这个列表里了。
CONTEXT_TTL = 3 * 3600.0

_CONTEXTS: list[dict] = []
_INFLIGHT: dict[str, float] = {}
# guid -> 预热完成时间戳。用于判断「这次播放的直链是不是我们提前取回来的」
_WARMED: dict[str, float] = {}
_RECENT: list[dict] = []
_RECENT_MAX = 20

_STATS: dict[str, float] = {
    "scheduled": 0,     # 调度次数
    "done": 0,          # 预热成功
    "failed": 0,        # 预热失败（静默，仅计数）
    "no_next": 0,       # 推断不出下一首（没有上下文 / 已是最后一首）
    "already": 0,       # 已经预热过或正在预热，跳过
    "hits": 0,          # 播放时命中预热成果
    "plays": 0,         # 在线播放总次数
    "cold_ms_sum": 0.0,  # 冷启动（未预热）gather 耗时累计
    "cold_ms_n": 0,
    "warm_ms_sum": 0.0,  # 命中预热后的 gather 耗时累计
    "warm_ms_n": 0,
}


def enabled() -> bool:
    return str(os.environ.get("FNMUSIC_PREFETCH_NEXT", "true") or "true") \
        .strip().lower() in ("true", "1", "yes", "on")


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------

def remember_context(context_guid: str, tracks: list[Any]) -> int:
    """记下我们下发过的一份有序曲目列表。返回记下的曲目数。"""
    guids: list[str] = []
    for t in tracks or []:
        if isinstance(t, dict):
            g = str(t.get("guid") or "").strip()
        else:
            g = str(t or "").strip()
        if g and g not in guids:
            guids.append(g)
        if len(guids) >= MAX_TRACKS:
            break
    if len(guids) < 2:
        return 0
    _CONTEXTS[:] = [c for c in _CONTEXTS if str(c.get("ctx") or "") != str(context_guid or "")]
    _CONTEXTS.append({"ts": time.time(), "ctx": str(context_guid or ""), "tracks": guids})
    if len(_CONTEXTS) > MAX_CONTEXTS:
        del _CONTEXTS[:-MAX_CONTEXTS]
    return len(guids)


def _prune() -> None:
    now = time.time()
    _CONTEXTS[:] = [c for c in _CONTEXTS
                    if (now - float(c.get("ts") or 0.0)) < CONTEXT_TTL]
    _CONTEXTS.sort(key=lambda c: float(c.get("ts") or 0.0))


def next_of(guid: str) -> tuple[str, str] | None:
    """当前 guid 的下一首。返回 (上下文 guid, 下一首 guid)。"""
    g = str(guid or "").strip()
    if not g:
        return None
    _prune()
    # 最近用过的上下文优先：用户很可能刚从这个列表里开始播
    for c in reversed(_CONTEXTS):
        tracks = list(c.get("tracks") or [])
        try:
            i = tracks.index(g)
        except ValueError:
            continue
        if i + 1 < len(tracks):
            return str(c.get("ctx") or ""), str(tracks[i + 1])
        return None       # 已是最后一首，没有下一首
    return None


def context_report() -> list[dict]:
    _prune()
    return [{"ctx": str(c.get("ctx") or ""), "tracks": len(c.get("tracks") or []),
             "age_s": int(time.time() - float(c.get("ts") or 0.0))} for c in _CONTEXTS]


# ---------------------------------------------------------------------------
# 单飞与统计
# ---------------------------------------------------------------------------

def claim(guid: str) -> bool:
    """抢占预热资格（同一个 guid 同时只跑一个）。"""
    g = str(guid or "").strip()
    if not g:
        return False
    now = time.time()
    last = float(_INFLIGHT.get(g) or 0.0)
    if last and (now - last) < 60.0:
        return False
    _INFLIGHT[g] = now
    if len(_INFLIGHT) > 200:
        for k in sorted(_INFLIGHT, key=lambda kk: _INFLIGHT[kk])[:100]:
            _INFLIGHT.pop(k, None)
    return True


def warming_seconds(guid: str) -> "float | None":
    """这个 guid 的直链是不是我们提前取回来的？是则返回预热至今的秒数。"""
    ts = float(_WARMED.get(str(guid or "").strip()) or 0.0)
    if ts <= 0:
        return None
    age = time.time() - ts
    return age if 0 <= age < 3600 else None


def note_result(guid: str, ok: bool, ms: float, detail: str = "") -> None:
    if ok:
        _STATS["done"] += 1
        _WARMED[str(guid or "").strip()] = time.time()
    else:
        _STATS["failed"] += 1
    _RECENT.append({"ts": time.time(), "guid": str(guid or ""), "ok": bool(ok),
                    "ms": round(float(ms), 1), "detail": detail[:120]})
    del _RECENT[:-_RECENT_MAX]
    logger.info("prefetch %s %s %.0fms %s", "ok" if ok else "fail", guid, ms, detail)


def note_play(guid: str, gather_ms: float, warm: bool) -> None:
    """一次在线播放的 gather 耗时记账：预热过的走 warm，否则走 cold。"""
    _STATS["plays"] += 1
    if warm:
        _STATS["hits"] += 1
        _STATS["warm_ms_sum"] += max(0.0, float(gather_ms))
        _STATS["warm_ms_n"] += 1
    else:
        _STATS["cold_ms_sum"] += max(0.0, float(gather_ms))
        _STATS["cold_ms_n"] += 1
    if warm:
        logger.info("prefetch hit: %s（预热于 %.1fs 前，本次 gather %.0fms）",
                    guid, float(warming_seconds(guid) or 0.0), gather_ms)


def bump(key: str, n: int = 1) -> None:
    _STATS[key] = float(_STATS.get(key, 0)) + n


def status() -> dict:
    def _avg(s: str, n: str) -> float:
        cnt = float(_STATS.get(n) or 0)
        return round(float(_STATS.get(s) or 0.0) / cnt, 1) if cnt else 0.0

    cold = _avg("cold_ms_sum", "cold_ms_n")
    warm = _avg("warm_ms_sum", "warm_ms_n")
    return {
        "enabled": enabled(),
        "scheduled": int(_STATS.get("scheduled") or 0),
        "done": int(_STATS.get("done") or 0),
        "failed": int(_STATS.get("failed") or 0),
        "no_next": int(_STATS.get("no_next") or 0),
        "already": int(_STATS.get("already") or 0),
        "plays": int(_STATS.get("plays") or 0),
        "hits": int(_STATS.get("hits") or 0),
        "hit_rate": (round(float(_STATS.get("hits") or 0) / float(_STATS["plays"]), 3)
                     if _STATS.get("plays") else 0.0),
        "cold_ms": cold,
        "warm_ms": warm,
        "saved_ms": round(max(0.0, cold - warm), 1) if (cold and _STATS.get("warm_ms_n")) else 0.0,
        "contexts": context_report(),
        "recent": [dict(r) for r in _RECENT[-10:]],
    }


def reset_for_test() -> None:
    _CONTEXTS.clear()
    _INFLIGHT.clear()
    _WARMED.clear()
    _RECENT.clear()
    for k in _STATS:
        _STATS[k] = 0.0
