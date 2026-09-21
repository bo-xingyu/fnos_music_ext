"""平台扫码登录（best-effort）+ Cookie 登录说明。

Cookie 登录是主路径：从浏览器 DevTools 复制 Cookie 粘贴即可解锁会员曲目。
扫码登录各平台接口变动频繁，这里对酷狗/酷我给出可轮询的流程；失败时回落提示用 Cookie。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from . import auth_store

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

# 内存中的扫码会话：key -> {source, unikey, created, payload}
_QR_SESSIONS: dict[str, dict[str, Any]] = {}

COOKIE_HELP = {
    "qq": (
        "浏览器登录 y.qq.com → F12 → Network → 任选请求 → Request Headers 复制整段 Cookie。"
        "关键字段通常含 qqmusic_key / qm_keyst / uin / qm_keyst。"
    ),
    "kugou": (
        "浏览器登录 www.kugou.com → F12 复制 Cookie。"
        "关键字段通常含 kg_mid / kg_dfid / KugouCloudUserName / token 相关键。"
    ),
    "kuwo": (
        "浏览器登录 www.kuwo.cn → F12 复制 Cookie。"
        "关键字段通常含 kw_token / Hm_token / csrf 等。"
    ),
    "qishui": (
        "汽水登录态变动大：从 App/Web 抓包复制 Cookie 或 token，"
        "更推荐配置聚合网关 FNMUSIC_QISHUI_API_BASE。"
    ),
}


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA},
        timeout=12.0,
        follow_redirects=True,
    )


async def qr_start(source: str) -> dict[str, Any]:
    """发起扫码登录。支持 kugou / kuwo；其余返回 need_cookie。"""
    source = (source or "").strip().lower()
    if source not in ("kugou", "kuwo", "qq"):
        return {
            "ok": False,
            "error": f"{source} 暂不支持扫码，请使用 Cookie 登录",
            "help": COOKIE_HELP.get(source, ""),
            "mode": "cookie",
        }
    unikey = uuid.uuid4().hex
    now = time.time()
    try:
        if source == "kugou":
            payload = await _kugou_qr_create()
        elif source == "kuwo":
            payload = await _kuwo_qr_create()
        else:
            payload = await _qq_qr_create()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"扫码初始化失败: {exc}",
            "help": COOKIE_HELP.get(source, ""),
            "mode": "cookie",
        }
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": payload.get("error") or "扫码初始化失败",
            "help": COOKIE_HELP.get(source, ""),
            "mode": "cookie",
        }
    _QR_SESSIONS[unikey] = {
        "source": source,
        "unikey": unikey,
        "created": now,
        "payload": payload,
    }
    # 清理过期会话（10 分钟）
    for k in list(_QR_SESSIONS):
        if now - _QR_SESSIONS[k]["created"] > 600:
            _QR_SESSIONS.pop(k, None)
    return {
        "ok": True,
        "unikey": unikey,
        "source": source,
        "qr_content": payload.get("qr_content") or payload.get("url") or "",
        "qr_png": payload.get("qr_png") or "",
        "tip": payload.get("tip") or "请用对应 App 扫码登录",
        "mode": "qr",
    }


async def qr_check(unikey: str) -> dict[str, Any]:
    sess = _QR_SESSIONS.get(str(unikey or ""))
    if not sess:
        return {"ok": False, "code": 800, "error": "会话不存在或已过期"}
    source = sess["source"]
    payload = sess["payload"]
    try:
        if source == "kugou":
            result = await _kugou_qr_poll(payload)
        elif source == "kuwo":
            result = await _kuwo_qr_poll(payload)
        else:
            result = await _qq_qr_poll(payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": 0, "error": str(exc)}

    code = int(result.get("code") or 0)
    if code == 803 and result.get("cookie"):
        auth_store.set_cookie(source, str(result["cookie"]),
                              nickname=str(result.get("nickname") or ""))
        _QR_SESSIONS.pop(unikey, None)
        return {
            "ok": True,
            "code": 803,
            "nickname": result.get("nickname") or "",
            "source": source,
        }
    return {
        "ok": True,
        "code": code,
        "msg": result.get("msg") or "",
        "source": source,
        "help": "" if code != 800 else COOKIE_HELP.get(source, ""),
    }


async def _kugou_qr_create() -> dict[str, Any]:
    async with await _client() as c:
        ts = int(time.time() * 1000)
        r = await c.post(
            "https://login-user.kugou.com/v1/login/qrcode",
            json={
                "appid": 1005,
                "clientver": "1000",
                "clienttime": ts,
                "uuid": str(uuid.uuid4()),
                "dfid": "-",
                "mid": "fnmusic_ext",
                "platid": 4,
            },
            headers={"User-Agent": UA, "Content-Type": "application/json"},
        )
        data = r.json()
        inner = data.get("data") or {}
        qrcode = str(inner.get("qrcode") or inner.get("code") or "")
        url = str(inner.get("url") or inner.get("qrcode_url") or "")
        if not qrcode and not url:
            return {"ok": False, "error": f"酷狗未返回二维码: {str(data)[:200]}"}
        return {
            "ok": True,
            "qr_content": url or qrcode,
            "qr_png": "",
            "qrcode": qrcode or url,
            "tip": "请用酷狗音乐 App 扫码",
            "raw": data,
        }


async def _kugou_qr_poll(payload: dict) -> dict[str, Any]:
    qrcode = str(payload.get("qrcode") or "")
    async with await _client() as c:
        ts = int(time.time() * 1000)
        r = await c.get(
            "https://login-user.kugou.com/v1/login/qrcode",
            params={
                "appid": 1005,
                "clienttime": ts,
                "code": qrcode,
                "type": 1,
                "uuid": str(uuid.uuid4()),
            },
            headers={"User-Agent": UA},
        )
        data = r.json()
        status = int(data.get("status") or data.get("error_code") or 0)
        # 常见：0/800 过期，801 等待扫码，802 已扫待确认，803/1 成功
        if status in (0, 800) and not data.get("data"):
            return {"code": 800, "msg": "二维码过期"}
        if status in (801, 200):
            return {"code": 801, "msg": "等待扫码"}
        if status in (802, 201):
            return {"code": 802, "msg": "已扫码，请确认"}
        inner = data.get("data") or {}
        cookie_parts = []
        userid = str(inner.get("userid") or inner.get("uid") or "")
        token = str(inner.get("token") or inner.get("kguser_token") or "")
        mid = str(inner.get("mid") or "")
        if token:
            cookie_parts.append(f"KugouUserId={userid}" if userid else f"token={token}")
            cookie_parts.append(f"kguser_token={token}")
        if mid:
            cookie_parts.append(f"kg_mid={mid}")
        # 把返回里所有 string 字段也拼进 cookie（各版本字段名不一）
        for k, v in (inner.items() if isinstance(inner, dict) else []):
            if isinstance(v, (str, int)) and k.lower() in (
                "token", "userid", "uid", "mid", "dfid", "clienttime",
                "userhash", "musiclistversion",
            ):
                cookie_parts.append(f"{k}={v}")
        cookie = "; ".join(dict.fromkeys(cookie_parts))
        if not cookie and isinstance(inner, dict) and inner.get("cookie"):
            cookie = str(inner["cookie"])
        if cookie:
            return {
                "code": 803,
                "cookie": cookie,
                "nickname": str(inner.get("username") or inner.get("nickname") or userid),
            }
        return {"code": status or 0, "msg": str(data)[:200]}


async def _kuwo_qr_create() -> dict[str, Any]:
    # 酷我 H5 登录：先取登录页生成的 rid / 二维码内容
    async with await _client() as c:
        rid = uuid.uuid4().hex
        # 公开接口：返回二维码 URL 或登录 token
        r = await c.get(
            "https://newlogin.kuwo.cn/login",
            params={
                "uid": 0,
                "prod": "kwplayer_ar_9.3.0.3_qqbrowser_web_1",
                "verify": 0,
                "p2p": 1,
                "corp": "kuwo",
                "type": "login",
                "model": 1,
                "rid": rid,
            },
            headers={"User-Agent": UA, "Referer": "https://www.kuwo.cn/"},
        )
        text = (r.text or "").strip()
        data = None
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            pass
        qr = ""
        if isinstance(data, dict):
            qr = str(data.get("qr") or data.get("url") or data.get("data") or "")
        if not qr and text.startswith("http"):
            qr = text
        if not qr:
            # 降级：用登录页 URL 让用户手机打开扫码（不理想但可用）
            qr = "https://www.kuwo.cn/"
        return {
            "ok": True,
            "qr_content": qr,
            "rid": rid,
            "tip": "请用酷我音乐 App 扫码，或改用 Cookie 登录",
            "raw": data if isinstance(data, dict) else {"text": text[:300]},
        }


async def _kuwo_qr_poll(payload: dict) -> dict[str, Any]:
    rid = str(payload.get("rid") or "")
    async with await _client() as c:
        r = await c.get(
            "https://newlogin.kuwo.cn/login",
            params={
                "uid": 0,
                "prod": "kwplayer_ar_9.3.0.3_qqbrowser_web_1",
                "verify": 0,
                "p2p": 1,
                "corp": "kuwo",
                "type": "login",
                "model": 1,
                "rid": rid,
                "status": 1,
            },
            headers={"User-Agent": UA, "Referer": "https://www.kuwo.cn/"},
        )
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"text": (r.text or "")[:300]}
        if not isinstance(data, dict):
            return {"code": 0, "msg": "poll failed"}
        msg = str(data.get("msg") or data.get("message") or "")
        # 成功时通常带 uid / token / nick
        token = str(data.get("token") or data.get("Hm_token") or "")
        uid = str(data.get("uid") or data.get("userid") or "")
        nick = str(data.get("nick") or data.get("nickname") or "")
        if token or (uid and uid not in ("0", "") and "成功" in msg + str(data.get("code"))):
            cookie_parts = []
            if token:
                cookie_parts.append(f"Hm_token={token}")
                cookie_parts.append(f"kw_token={token}")
            if uid:
                cookie_parts.append(f"Hm_uid={uid}")
            if nick:
                cookie_parts.append(f"nick={nick}")
            return {
                "code": 803,
                "cookie": "; ".join(cookie_parts),
                "nickname": nick or uid,
            }
        if "扫码" in msg or data.get("code") in (801, "801"):
            return {"code": 801, "msg": msg or "等待扫码"}
        if "确认" in msg or data.get("code") in (802, "802"):
            return {"code": 802, "msg": msg or "已扫码，请确认"}
        return {"code": 800, "msg": msg or str(data)[:200]}


async def _qq_qr_create() -> dict[str, Any]:
    """QQ 扫码：返回 ptqrshow 内容；Cookie 落盘需要完整 ptlogin 链路，失败时引导 Cookie。"""
    appid = "716027609"
    async with await _client() as c:
        r = await c.get(
            "https://ssl.ptlogin2.qq.com/ptqrshow",
            params={
                "appid": appid,
                "e": "2",
                "e_l": "",
                "s": "3",
                "d": "72",
                "v": "4",
                "t": str(uuid.uuid4()),
                "daid": "73",
                "pt_3rd_aid": "100495085",
            },
            headers={"User-Agent": UA, "Referer": "https://y.qq.com/"},
        )
        if r.status_code != 200 or not r.content:
            return {"ok": False, "error": "QQ 二维码接口失败"}
        # 二进制二维码：前端可直接展示 data-url
        import base64

        b64 = base64.b64encode(r.content).decode("ascii")
        return {
            "ok": True,
            "qr_content": f"data:image/png;base64,{b64}",
            "qr_png": f"data:image/png;base64,{b64}",
            "appid": appid,
            "tip": "请用 QQ/手机 QQ 扫码登录 y.qq.com；若未自动成功请改用 Cookie",
        }


async def _qq_qr_poll(payload: dict) -> dict[str, Any]:
    # 完整 QQ 登录链路（ptqrlogin → 跳转 → music key）接口变更频繁，
    # 这里探测是否已在浏览器侧登录成功并不现实，统一引导 Cookie。
    return {
        "code": 801,
        "msg": "QQ 扫码需浏览器完成完整登录链路，请改用 Cookie 登录（见说明）",
    }


def help_text(source: str) -> str:
    return COOKIE_HELP.get((source or "").lower(), "从已登录的浏览器复制 Cookie 粘贴即可。")
