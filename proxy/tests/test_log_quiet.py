"""日志降噪（v2.9.29）的单元测试。

真机 proxy.log 10MB 就截断，而其中一大半是封面图、5 秒保活心跳、客户端状态轮询
这类高频且成功与否都不影响播放的访问行——真正有排障价值的 fnmusic_proxy 行反而
留不住（「播不出来时日志里一行都没有」，一半就是这个原因）。

核心不变量：
1. **默认开，可一键关**（关掉要能真的看到全部原始日志，否则排障时无从下手）；
2. **只掐得掉噪音，掐不掉业务日志**——播放、取链失败、慢分片这些必须一字不减；
   降噪把「没日志」变成「有日志」只是手段，把它变成「更少的日志」就是倒退；
3. **用 filter 而不是 setLevel**——uvicorn 启动时会用 dictConfig 重配
   uvicorn.access（重置 level、清空 handlers），但不会清掉 logger 上的 filter；
   写成 setLevel 会在启动时被悄悄覆盖回去，功能等于没做；
4. **可重复调用**（幂等），否则 filter 会叠加，一次访问跑 N 个过滤器。
"""
from __future__ import annotations

import logging

import pytest

import proxy.app as P


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """还原被 install_log_quiet() 改动的全局日志状态。

    app.py 在 import 时就调用过一次 install_log_quiet()，所以这些状态在函数里
    被改过之后必须还原，否则会顺着进程影响到后面的用例。
    """
    acc = logging.getLogger("uvicorn.access")
    saved_filters = list(acc.filters)
    saved_httpx = logging.getLogger("httpx").level
    saved_core = logging.getLogger("httpcore").level
    monkeypatch.delenv("FNMUSIC_LOG_QUIET", raising=False)
    yield
    acc.filters[:] = saved_filters
    logging.getLogger("httpx").setLevel(saved_httpx)
    logging.getLogger("httpcore").setLevel(saved_core)


def _rec(msg, args=None) -> logging.LogRecord:
    """构造一条 uvicorn.access 风格的访问日志记录。"""
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             msg, args, None)


def test_default_enabled():
    """默认开启：不动配置的用户自动受益。"""
    assert P.log_quiet_enabled() is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "FALSE"])
def test_disable_via_env(monkeypatch, raw):
    """关掉之后要真的能看到全部原始日志——这是排障时的后路。"""
    monkeypatch.setenv("FNMUSIC_LOG_QUIET", raw)
    assert P.log_quiet_enabled() is False


def test_install_disabled_leaves_levels_untouched(monkeypatch):
    monkeypatch.setenv("FNMUSIC_LOG_QUIET", "false")
    logging.getLogger("httpx").setLevel(logging.INFO)
    assert P.install_log_quiet() is False
    assert logging.getLogger("httpx").level == logging.INFO


def test_install_lowers_httpx_noise():
    assert P.install_log_quiet() is True
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_install_is_idempotent():
    """重复调用不能叠加 filter（filter 是与关系，叠加只会白白变慢）。"""
    for _ in range(5):
        P.install_log_quiet()
    acc = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, P._QuietAccessFilter) for f in acc.filters) == 1


def test_noise_paths_are_dropped():
    flt = P._QuietAccessFilter(P.LOG_QUIET_ACCESS_PATHS)
    for path in P.LOG_QUIET_ACCESS_PATHS:
        line = '- "GET %s?x=1 HTTP/1.0" 200 OK' % path
        assert flt.filter(_rec(line)) is False, path


def test_playback_and_failure_logs_survive():
    """降噪的底线：这些行一条都不能少，少了就比不做还糟。"""
    flt = P._QuietAccessFilter(P.LOG_QUIET_ACCESS_PATHS)
    keep = [
        '- "GET /music/api/v1/track/stream?guid=online%3Anetease%3A1 HTTP/1.0" 302 Found',
        '- "GET /music/api/v1/track/stream?guid=online%3Anetease%3A1 HTTP/1.0" 404 Not Found',
        '- "GET /music/api/v1/track/hls/online%3Anetease%3A1/preset.m3u8 HTTP/1.0" 200 OK',
        '- "GET /music/api/v1/song/list?id=1 HTTP/1.0" 200 OK',
        '- "GET /music/api/v1/lyric/list?trackGUID=x HTTP/1.0" 200 OK',
        '- "GET /music/api/v1/search/track?keyword=x HTTP/1.0" 200 OK',
        '- "GET /music/api/v1/track/metadata?guid=x HTTP/1.0" 200 OK',
        '- "GET /_ext/diag HTTP/1.1" 200 OK',
        '- "POST /music/api/v1/track/transcode HTTP/1.0" 200 OK',
    ]
    for line in keep:
        assert flt.filter(_rec(line)) is True, line


def test_filter_fails_open_on_unformattable_record():
    """拿不到消息就放行——宁可多记，也不能把故障日志一起吞掉。"""
    flt = P._QuietAccessFilter(P.LOG_QUIET_ACCESS_PATHS)
    assert flt.filter(_rec(None, (1,))) is True


def test_noise_paths_cover_known_hot_paths():
    """真机日志里占比最高的那几类，必须都在名单里，否则降噪等于没做。"""
    for path in ("/music/api/v1/static/cover",
                 "/music/api/v1/task/list",
                 "/music/api/v1/event/report",
                 "/music/api/v1/track/transcode/heartbeat",
                 "/_ext/healthz"):
        assert path in P.LOG_QUIET_ACCESS_PATHS, path
