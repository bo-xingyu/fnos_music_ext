"""扩展音源扫码登录（QQ / 酷狗 / 酷我）+ Cookie 登录。

扫码流程对齐网易云：生成二维码 → 轮询 → 成功写 Cookie。
各平台接口会变，失败时前端回落到 Cookie 粘贴。
"""
from __future__ import annotations

import base64
import hashlib
import http.cookies
import io
import json
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import auth_store

UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
UA_APP = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

_QR_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_TTL = 600

QR_STEPS = {
    "qq": [
        "打开手机上的 QQ 或 QQ 音乐 App",
        "扫一扫左侧二维码",
        "在手机上确认登录 y.qq.com",
    ],
    "kugou": [
        "打开手机上的酷狗音乐 App",
        "首页扫一扫左侧二维码",
        "在手机上确认登录",
    ],
    "kuwo": [
        "打开手机上的酷我音乐 App",
        "使用扫一扫扫描左侧二维码",
        "在手机上确认登录（若 App 无扫码，请改用 Cookie）",
    ],
    "qishui": [
        "汽水暂无稳定扫码接口",
        "请用 Cookie 登录，或配置聚合网关",
    ],
}

COOKIE_HELP = {
    "qq": (
        "浏览器登录 y.qq.com → F12 → Network → 任选请求 → "
        "Request Headers 复制整段 Cookie（常含 qqmusic_key / qm_keyst / uin）。"
    ),
    "kugou": (
        "浏览器登录 www.kugou.com → F12 复制 Cookie"
        "（常含 kg_mid / kg_dfid / token 相关键）。"
    ),
    "kuwo": (
        "浏览器登录 www.kuwo.cn → F12 复制 Cookie"
        "（常含 kw_token / Hm_token）。"
    ),
    "qishui": (
        "从汽水 App/Web 抓包复制 Cookie 或 token；"
        "更推荐在配置里填写聚合网关 FNMUSIC_QISHUI_API_BASE。"
    ),
}

SOURCE_LABELS = {
    "qq": "QQ音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
    "qishui": "汽水音乐",
}


def _hash33(s: str) -> int:
    e = 0
    for ch in s:
        e += (e << 5) + ord(ch)
    return e & 0x7FFFFFFF


def _qr_png_dataurl(content: str) -> str:
    """把任意字符串编成二维码 PNG data URL。失败返回空串。"""
    if not content:
        return ""
    if content.startswith("data:image/"):
        return content
    try:
        import qrcode  # type: ignore

        img = qrcode.make(content)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001
        # 无 qrcode 库时退回第三方生成器（仅当 content 是 URL）
        if content.startswith("http"):
            return (
                "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data="
                + content
            )
        return ""


def _cookie_header_from_jar(jar: httpx.Cookies) -> str:
    parts = []
    for k, v in jar.items():
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def _merge_set_cookie(existing: dict[str, str], response: httpx.Response) -> None:
    for k, v in response.cookies.items():
        existing[k] = v
    # 解析多个 Set-Cookie（httpx.cookies 可能合并不全）
    raw = response.headers.get_list("set-cookie") if hasattr(response.headers, "get_list") else []
    if not raw:
        single = response.headers.get("set-cookie")
        if single:
            raw = [single]
    for item in raw:
        try:
            c = http.cookies.SimpleCookie()
            c.load(item)
            for k, morsel in c.items():
                existing[k] = morsel.value
        except Exception:  # noqa: BLE001
            continue


def help_text(source: str) -> str:
    return COOKIE_HELP.get((source or "").lower(), "从已登录的浏览器复制 Cookie 粘贴即可。")


def qr_steps(source: str) -> list[str]:
    return list(QR_STEPS.get((source or "").lower(), ["使用对应 App 扫码", "手机上确认登录"]))


def _client(cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA_WEB},
        timeout=15.0,
        follow_redirects=True,
        cookies=cookies or {},
    )


def _store_session(source: str, payload: dict[str, Any]) -> str:
    unikey = uuid.uuid4().hex
    now = time.time()
    for k in list(_QR_SESSIONS):
        if now - _QR_SESSIONS[k].get("created", 0) > _SESSION_TTL:
            _QR_SESSIONS.pop(k, None)
    _QR_SESSIONS[unikey] = {
        "source": source,
        "unikey": unikey,
        "created": now,
        "payload": payload,
        "cookies": dict(payload.get("cookies") or {}),
    }
    return unikey


# ---------------------------------------------------------------------- QQ ----

async def _qq_create() -> dict[str, Any]:
    appid = "716027609"
    daid = "73"
    pt_3rd_aid = "100495085"
    cookies: dict[str, str] = {}
    async with _client() as c:
        r = await c.get(
            "https://ssl.ptlogin2.qq.com/ptqrshow",
            params={
                "appid": appid,
                "e": "2",
                "e_l": "",
                "s": "3",
                "d": "72",
                "v": "4",
                "t": f"{uuid.uuid4()}",
                "daid": daid,
                "pt_3rd_aid": pt_3rd_aid,
            },
            headers={
                "User-Agent": UA_WEB,
                "Referer": "https://y.qq.com/",
                "Origin": "https://y.qq.com",
            },
        )
        if r.status_code != 200 or not r.content:
            raise RuntimeError(f"ptqrshow HTTP {r.status_code}")
        _merge_set_cookie(cookies, r)
        qrsig = cookies.get("qrsig") or ""
        if not qrsig:
            # 有时在 set-cookie 合并后仍缺失，从 jar 兜底
            qrsig = r.cookies.get("qrsig") or ""
        png = "data:image/png;base64," + base64.b64encode(r.content).decode("ascii")
    return {
        "ok": True,
        "qr_png": png,
        "qr_content": "",
        "cookies": cookies,
        "qrsig": qrsig,
        "appid": appid,
        "daid": daid,
        "pt_3rd_aid": pt_3rd_aid,
        "tip": "请用 QQ / QQ 音乐 App 扫码登录",
    }


def _parse_ptui(text: str) -> tuple[int, str, str]:
    """解析 ptuiCB('66','0','','0','...','...'); 返回 (code, msg, href)。"""
    m = re.search(r"ptuiCB\((.*)\)", text or "")
    if not m:
        return 0, text[:200], ""
    args = re.findall(r"'([^']*)'", m.group(1))
    try:
        code = int(args[0]) if args else 0
    except ValueError:
        code = 0
    msg = args[4] if len(args) > 4 else (args[1] if len(args) > 1 else "")
    href = args[2] if len(args) > 2 else ""
    return code, msg, href


async def _qq_poll(payload: dict, cookies: dict[str, str]) -> dict[str, Any]:
    qrsig = str(payload.get("qrsig") or cookies.get("qrsig") or "")
    if not qrsig:
        return {"code": 800, "msg": "缺少 qrsig，请重新生成二维码"}
    ptqrtoken = _hash33(qrsig)
    appid = str(payload.get("appid") or "716027609")
    params = {
        "u1": "https://y.qq.com/into/proxy/qqmusic_jump.html",
        "ptqrtoken": ptqrtoken,
        "ptredirect": "0",
        "h": "1",
        "t": "1",
        "g": "1",
        "from_ui": "1",
        "ptlang": "2052",
        "action": f"0-0-{int(time.time() * 1000)}",
        "js_ver": "23070709",
        "js_type": "1",
        "login_sig": "",
        "pt_uistyle": "40",
        "aid": appid,
        "daid": str(payload.get("daid") or "73"),
        "pt_3rd_aid": str(payload.get("pt_3rd_aid") or "100495085"),
        "has_onekey": "1",
    }
    hdrs = {
        "User-Agent": UA_WEB,
        "Referer": "https://y.qq.com/",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v),
    }
    async with _client(cookies) as c:
        r = await c.get(
            "https://ssl.ptlogin2.qq.com/ptqrlogin",
            params=params,
            headers=hdrs,
        )
        _merge_set_cookie(cookies, r)
        code, msg, href = _parse_ptui(r.text or "")

        if code == 0 and href:
            # 跟随跳转收集 p_skey / pt4_token 等
            r2 = await c.get(href, headers={**hdrs, "Referer": "https://y.qq.com/"})
            _merge_set_cookie(cookies, r2)
            # 再访问 y.qq.com 补全音乐站 Cookie
            r3 = await c.get(
                "https://y.qq.com/",
                headers={"User-Agent": UA_WEB, "Referer": "https://y.qq.com/"},
            )
            _merge_set_cookie(cookies, r3)
            # 尝试取 QQ 音乐登录信息
            nickname = ""
            try:
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                g_tk = _hash33(cookies.get("p_skey") or cookies.get("skey") or "")
                body = {
                    "comm": {
                        "ct": 19,
                        "cv": 1859,
                        "g_tk": g_tk,
                        "uin": cookies.get("uin") or cookies.get("p_uin") or "0",
                    },
                    "req_1": {
                        "module": "music.login.LoginServer",
                        "method": "Login",
                        "param": {"loginType": 1},
                    },
                }
                r4 = await c.get(
                    "https://u.y.qq.com/cgi-bin/musicu.fcg",
                    params={"data": json.dumps(body, ensure_ascii=False)},
                    headers={
                        "User-Agent": UA_WEB,
                        "Referer": "https://y.qq.com/",
                        "Cookie": cookie_str,
                    },
                )
                try:
                    j = r4.json()
                    nickname = str(
                        ((j.get("req_1") or {}).get("data") or {}).get("nick")
                        or cookies.get("nickname")
                        or ""
                    )
                except Exception:  # noqa: BLE001
                    nickname = ""
            except Exception:  # noqa: BLE001
                nickname = ""
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
            if len(cookie_str) < 20:
                return {"code": 0, "msg": "登录跳转成功但未拿到 Cookie，请改用 Cookie 登录"}
            return {
                "code": 803,
                "cookie": cookie_str,
                "nickname": nickname or str(cookies.get("nickname") or "QQ用户"),
            }

        # 66=未扫码 67=已扫待确认 65=二维码过期
        if code in (65, 501, 0) and not href:
            return {"code": 800, "msg": msg or "二维码已过期"}
        if code == 67:
            return {"code": 802, "msg": msg or "已扫码，请在手机上确认"}
        if code == 66:
            return {"code": 801, "msg": msg or "等待扫码"}
        return {"code": code or 0, "msg": msg or f"qq poll code={code}"}


# ------------------------------------------------------------------ 酷狗 ----

async def _kugou_create() -> dict[str, Any]:
    uuid_v = str(uuid.uuid4())
    mid = uuid_v
    cookies: dict[str, str] = {}
    async with _client() as c:
        r = await c.post(
            "https://login-user.kugou.com/v1/login/qrcode",
            json={
                "appid": 1014,
                "clientver": "1000",
                "clienttime": int(time.time() * 1000),
                "uuid": uuid_v,
                "dfid": "-",
                "mid": mid,
                "platid": 4,
            },
            headers={
                "User-Agent": UA_APP,
                "Content-Type": "application/json",
                "Referer": "https://www.kugou.com/",
            },
        )
        data = r.json()
        _merge_set_cookie(cookies, r)
        inner = data.get("data") or {}
        qrcode = str(inner.get("qrcode") or inner.get("code") or "")
        url = str(inner.get("url") or inner.get("qrcode_url") or "")
        if not qrcode and not url:
            # 部分版本 status=0 且 data 直接是 code
            status = data.get("status") or data.get("error_code") or 0
            if isinstance(inner, str) and inner:
                qrcode = inner
        if not qrcode and not url:
            raise RuntimeError(f"酷狗未返回二维码: {str(data)[:240]}")
        content = url or qrcode
        return {
            "ok": True,
            "qr_content": content,
            "qr_png": _qr_png_dataurl(content),
            "qrcode": qrcode or url,
            "uuid": uuid_v,
            "mid": mid,
            "cookies": cookies,
            "tip": "请用酷狗音乐 App 扫码",
        }


async def _kugou_poll(payload: dict, cookies: dict[str, str]) -> dict[str, Any]:
    qrcode = str(payload.get("qrcode") or "")
    uuid_v = str(payload.get("uuid") or "")
    mid = str(payload.get("mid") or "")
    params_list = [
        {
            "appid": 1014,
            "clientver": "1000",
            "clienttime": int(time.time() * 1000),
            "code": qrcode,
            "type": 1,
            "uuid": uuid_v,
            "platid": 4,
            "dfid": "-",
            "mid": mid,
        },
        {
            "appid": 1005,
            "clienttime": int(time.time() * 1000),
            "code": qrcode,
            "type": 1,
            "uuid": uuid_v,
        },
    ]
    async with _client(cookies) as c:
        last_text = ""
        for params in params_list:
            try:
                r = await c.get(
                    "https://login-user.kugou.com/v1/login/qrcode",
                    params=params,
                    headers={
                        "User-Agent": UA_APP,
                        "Referer": "https://www.kugou.com/",
                    },
                )
                data = r.json()
                _merge_set_cookie(cookies, r)
            except Exception as exc:  # noqa: BLE001
                last_text = str(exc)
                continue
            last_text = str(data)[:240]
            status = data.get("status")
            err = data.get("error_code")
            code_val = status if status is not None else err
            try:
                code_val = int(code_val or 0)
            except (TypeError, ValueError):
                code_val = 0
            inner = data.get("data") or {}
            # 常见约定（不同版本混用）：
            # 0 成功 / 7102 等待扫码 / 7103 已扫待确认 / 7101 过期
            # 另有：1 等待，2 已扫，3 成功
            if code_val in (0, 3) or (isinstance(inner, dict) and (inner.get("token") or inner.get("userid") or inner.get("uid"))):
                if isinstance(inner, dict) and (inner.get("token") or inner.get("userid") or inner.get("uid") or inner.get("cookie")):
                    cookie = str(inner.get("cookie") or "")
                    if not cookie:
                        parts = []
                        for k in (
                            "token", "userid", "uid", "mid", "dfid",
                            "userhash", "KugouUserId", "kguser_token",
                        ):
                            if inner.get(k) not in (None, ""):
                                parts.append(f"{k}={inner.get(k)}")
                        if inner.get("userid") or inner.get("uid"):
                            uid = inner.get("userid") or inner.get("uid")
                            parts.append(f"KugouUserId={uid}")
                        if inner.get("token"):
                            parts.append(f"kguser_token={inner.get('token')}")
                        cookie = "; ".join(dict.fromkeys(parts))
                    if cookie:
                        return {
                            "code": 803,
                            "cookie": cookie,
                            "nickname": str(inner.get("username") or inner.get("nickname") or ""),
                        }
            if code_val in (7101, 65, 501):
                return {"code": 800, "msg": "二维码已过期"}
            if code_val in (7103, 2, 201):
                return {"code": 802, "msg": "已扫码，请在手机上确认"}
            if code_val in (7102, 1, 200):
                return {"code": 801, "msg": "等待扫码"}
            # 继续试下一组参数
        return {"code": 0, "msg": last_text or "酷狗轮询无结果"}


# ------------------------------------------------------------------ 酷我 ----

async def _kuwo_create() -> dict[str, Any]:
    rid = uuid.uuid4().hex
    cookies: dict[str, str] = {}
    qr_content = ""
    async with _client() as c:
        # 1) 尝试 H5 登录接口拿二维码内容
        try:
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
                    "https://www.kuwo.cn/": "",
                },
                headers={
                    "User-Agent": UA_WEB,
                    "Referer": "https://www.kuwo.cn/",
                },
            )
            _merge_set_cookie(cookies, r)
            text = (r.text or "").strip()
            try:
                data = r.json()
                if isinstance(data, dict):
                    qr_content = str(
                        data.get("qr")
                        or data.get("qrcode")
                        or data.get("url")
                        or data.get("data")
                        or ""
                    )
            except Exception:  # noqa: BLE001
                data = None
            if not qr_content and text.startswith("http"):
                qr_content = text
        except Exception:  # noqa: BLE001
            data = None
        # 2) 回退：生成指向「登录说明页」的二维码，引导 Cookie
        if not qr_content:
            qr_content = "https://www.kuwo.cn/"
        return {
            "ok": True,
            "qr_content": qr_content,
            "qr_png": _qr_png_dataurl(qr_content),
            "rid": rid,
            "cookies": cookies,
            "tip": "请用酷我音乐 App 扫码；若 App 无扫码入口请改用 Cookie 登录",
        }


async def _kuwo_poll(payload: dict, cookies: dict[str, str]) -> dict[str, Any]:
    rid = str(payload.get("rid") or "")
    async with _client(cookies) as c:
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
            headers={
                "User-Agent": UA_WEB,
                "Referer": "https://www.kuwo.cn/",
            },
        )
        _merge_set_cookie(cookies, r)
        try:
            data = r.json() if (r.text or "").strip().startswith("{") else {"text": (r.text or "")[:300]}
        except Exception:  # noqa: BLE001
            data = {"text": (r.text or "")[:300]}
        if not isinstance(data, dict):
            return {"code": 0, "msg": "kuwo poll failed"}
        msg = str(data.get("msg") or data.get("message") or "")
        token = str(data.get("token") or data.get("Hm_token") or data.get("kw_token") or "")
        uid = str(data.get("uid") or data.get("userid") or "")
        nick = str(data.get("nick") or data.get("nickname") or "")
        code_raw = data.get("code")
        try:
            code_n = int(code_raw) if code_raw is not None else -1
        except (TypeError, ValueError):
            code_n = -1
        if token or uid not in ("", "0") and code_n in (0, 200, 803):
            parts = []
            if token:
                parts += [f"Hm_token={token}", f"kw_token={token}"]
            if uid:
                parts.append(f"Hm_uid={uid}")
            if nick:
                parts.append(f"nick={nick}")
            # 合并已有 cookie
            for k, v in cookies.items():
                if k not in {p.split("=", 1)[0] for p in parts}:
                    parts.append(f"{k}={v}")
            return {
                "code": 803,
                "cookie": "; ".join(parts),
                "nickname": nick or uid,
            }
        if code_n in (802, 201) or "确认" in msg:
            return {"code": 802, "msg": msg or "已扫码，请确认"}
        if code_n in (801, 200) or "扫码" in msg or "等待" in msg:
            return {"code": 801, "msg": msg or "等待扫码"}
        if code_n in (800, 65) or "过期" in msg:
            return {"code": 800, "msg": msg or "二维码已过期"}
        return {"code": 801 if not msg else 0, "msg": msg or str(data)[:200]}


# ------------------------------------------------------------------ 汽水 ----

async def _qishui_create() -> dict[str, Any]:
    # 无稳定公开扫码接口：返回引导型会话，UI 提示用 Cookie
    content = "https://www.qishui.com/"
    return {
        "ok": True,
        "qr_content": content,
        "qr_png": _qr_png_dataurl(content),
        "tip": "汽水暂无稳定扫码接口，请改用 Cookie 登录或配置聚合网关",
        "mode": "cookie_fallback",
    }


async def _qishui_poll(payload: dict, cookies: dict[str, str]) -> dict[str, Any]:
    return {
        "code": 800,
        "msg": "汽水请使用 Cookie 登录，或配置 FNMUSIC_QISHUI_API_BASE",
    }


_CREATORS = {
    "qq": _qq_create,
    "kugou": _kugou_create,
    "kuwo": _kuwo_create,
    "qishui": _qishui_create,
}
_POLLS = {
    "qq": _qq_poll,
    "kugou": _kugou_poll,
    "kuwo": _kuwo_poll,
    "qishui": _qishui_poll,
}


async def qr_start(source: str) -> dict[str, Any]:
    source = (source or "").strip().lower()
    if source not in _CREATORS:
        return {
            "ok": False,
            "error": f"不支持音源 {source}",
            "help": help_text(source),
            "mode": "cookie",
        }
    try:
        payload = await _CREATORS[source]()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"扫码初始化失败: {exc}",
            "help": help_text(source),
            "mode": "cookie",
        }
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": payload.get("error") or "扫码初始化失败",
            "help": help_text(source),
            "mode": "cookie",
        }
    unikey = _store_session(source, payload)
    return {
        "ok": True,
        "unikey": unikey,
        "source": source,
        "label": SOURCE_LABELS.get(source, source),
        "qr_png": payload.get("qr_png") or _qr_png_dataurl(str(payload.get("qr_content") or "")),
        "qr_content": payload.get("qr_content") or "",
        "tip": payload.get("tip") or "",
        "steps": qr_steps(source),
        "mode": payload.get("mode") or "qr",
        "cookie_help": help_text(source),
    }


async def qr_check(unikey: str) -> dict[str, Any]:
    sess = _QR_SESSIONS.get(str(unikey or ""))
    if not sess:
        return {"ok": False, "code": 800, "error": "会话不存在或已过期，请重新生成二维码"}
    source = sess["source"]
    payload = sess["payload"]
    cookies = sess.setdefault("cookies", dict(payload.get("cookies") or {}))
    poller = _POLLS.get(source)
    if not poller:
        return {"ok": False, "code": 0, "error": f"不支持 {source}"}
    try:
        result = await poller(payload, cookies)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": 0, "error": str(exc)}

    code = int(result.get("code") or 0)
    if code == 803 and result.get("cookie"):
        auth_store.set_cookie(
            source,
            str(result["cookie"]),
            nickname=str(result.get("nickname") or ""),
        )
        _QR_SESSIONS.pop(unikey, None)
        return {
            "ok": True,
            "code": 803,
            "source": source,
            "label": SOURCE_LABELS.get(source, source),
            "nickname": result.get("nickname") or "",
        }
    out = {
        "ok": True,
        "code": code,
        "source": source,
        "label": SOURCE_LABELS.get(source, source),
        "msg": result.get("msg") or "",
    }
    if code == 800:
        out["help"] = help_text(source)
        out["steps"] = qr_steps(source)
    return out
