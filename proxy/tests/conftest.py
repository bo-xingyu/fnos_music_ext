"""共享测试夹具。

netease_auth 与 pushplus 都是模块级单例状态（登录态 TTL 缓存 / 推送去重窗口），
测试之间必须复位，否则互相污染。
"""
from __future__ import annotations

import os
import sys

import pytest

# 保证可以直接 `from proxy.app import ...`（从仓库根运行 pytest 时已由 rootdir 提供）
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from proxy import netease_auth, pushplus  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_singleton_state():
    """每个用例前后都清空登录态缓存、推送节流窗口与后台巡检任务。"""
    netease_auth.reset_for_test()
    pushplus.reset_throttle()
    _reset_online_info_caches()
    try:
        yield
    finally:
        netease_auth.reset_for_test()
        pushplus.reset_throttle()
        _reset_online_info_caches()


def _reset_online_info_caches():
    """清空在线元数据/封面缓存。

    这两份缓存是 2.1.7 为消掉重复上游往返而加的进程级 dict，不清会让
    「同 guid 第二次调用」静默命中上一个用例的数据，排查起来极其迷惑。
    """
    from proxy.app import _ONLINE_COVER_CACHE, _ONLINE_INFO_CACHE

    _ONLINE_INFO_CACHE.clear()
    _ONLINE_COVER_CACHE.clear()


@pytest.fixture
def logged_in(monkeypatch):
    """把网易云登录态固定为「已登录 VIP」。"""

    def _apply(nickname: str = "测试账号", vip_days_left: int = 30):
        import time

        now_ms = int((time.time() + vip_days_left * 86400) * 1000)
        state = netease_auth.LoginState(
            logged_in=True,
            nickname=nickname,
            user_id="10086",
            vip_type=11,
            vip_expires_ms=now_ms,
        )
        monkeypatch.setattr(netease_auth, "current_state", lambda: state)

        async def _fetch(client, *, force=False):
            return state

        monkeypatch.setattr(netease_auth, "fetch_state", _fetch)
        return state

    return _apply


@pytest.fixture
def logged_out(monkeypatch):
    """把网易云登录态固定为「未登录」。"""

    def _apply(error: str = ""):
        state = netease_auth.LoginState(logged_in=False, error=error)
        monkeypatch.setattr(netease_auth, "current_state", lambda: state)

        async def _fetch(client, *, force=False):
            return state

        monkeypatch.setattr(netease_auth, "fetch_state", _fetch)
        return state

    return _apply
