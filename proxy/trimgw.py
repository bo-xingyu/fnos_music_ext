"""飞牛开放能力网关客户端（trim open gateway / api-scope）。

背景（本模块的由来）
--------------------
之前本地曲库是「以 root 身份直接硬读 /vol1/... 目录」实现的。这在飞牛的开发
规范里是不合规的：应用访问用户存储空间中的文件夹前，必须**先获得授权**，由
系统把目标路径的 ACL 授予应用账号，之后应用才能实际访问。以 root 硬读虽在
多数机器上"碰巧能用"，但：

  * 管理员在应用设置里根本看不到「授权目录」入口（manifest 里
    disable_authorization_path=true 把它关掉了），无法合规地授权；
  * 一旦系统收紧或应用改用非 root 运行身份，读取直接 PermissionError，
    表现就是「本地每日推荐歌单不出现」且日志里查不到原因。

正确姿势（依据 https://developer.fnnas.com/api/overview/ ）
----------------------------------------------------------
1. 应用包 config/resource 声明需要的 api-scope：
     {"api-scope": ["trim.file.sharedAccess", "trim.file.userAccess"]}
2. 需要 JS SDK 时，manifest 声明 micro_app=true。
3. **后端**通过 Unix Socket /var/run/trim_open_gateway_apiscope.socket 调
   POST /api/v1/trimapp，Header 带 Authorization: Bearer <TRIM_API_TOKEN>。
   token 由系统在调用应用脚本时注入环境变量，**每次调用都要重新读**，
   绝不持久化（重装/重注册后会变）。
4. 管理员授权后，后端用 trim.file.getSharedAccessibleFolders 查询授权目录，
   用这些目录（而不是自己猜的路径）作为本地曲库来源。

设计约束
--------
* 零第三方依赖：标准库 socket 手写 HTTP/1.1（代理环境里没有 requests 的
  unix socket 适配器）。
* 永不抛异常：网关不存在/没授权/超时都只是"没有授权信息"，退化到既有逻辑，
  绝不能因为查授权把主流程搞挂。
* 结果短缓存（默认 60s）：授权目录不会频繁变，但改完设置希望能很快看到。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid

logger = logging.getLogger("app")

GATEWAY_SOCKET = "/var/run/trim_open_gateway_apiscope.socket"
GATEWAY_PATH = "/api/v1/trimapp"
TOKEN_ENV = "TRIM_API_TOKEN"
APP_NAME_ENV = "TRIM_APPNAME"

# 老版本 fnOS 没有 apiscope 网关，会把管理员授权的目录直接塞进这个环境变量
# （分号或冒号分隔）。一并读取，兼容 1.1.x / 1.2.x 两种形态。
SHARE_PATHS_ENV = "TRIM_DATA_SHARE_PATHS"

DEFAULT_TIMEOUT = 3.0

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 60.0


def app_name() -> str:
    """当前应用名（后端 API 请求体需要 appName）。"""
    return str(os.environ.get(APP_NAME_ENV) or os.environ.get("TRIM_APP_NAME") or "fnmusicext").strip() or "fnmusicext"


def _cached(key: str):
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _put(key: str, val: object) -> object:
    _CACHE[key] = (time.time(), val)
    return val


def invalidate_cache() -> None:
    """管理页点「刷新授权状态」时调用，强制下次重新查网关。"""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# 底层：Unix Socket 上的 HTTP/1.1
# ---------------------------------------------------------------------------

def _http_post(payload: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """向开放网关发一个 POST /api/v1/trimapp，返回解析后的 JSON。

    失败一律返回 {"code": <非0>, "msg": "..."} 而不是抛异常——调用方只需要
    关心「拿到了什么 / 没拿到」，不需要处理网络异常。
    """
    token = str(os.environ.get(TOKEN_ENV) or "").strip()
    if not token:
        return {"code": -1, "msg": f"环境变量 {TOKEN_ENV} 未注入（非由系统脚本启动？）"}
    if not os.path.exists(GATEWAY_SOCKET):
        return {"code": -2, "msg": f"开放网关不存在: {GATEWAY_SOCKET}"}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = (
        f"POST {GATEWAY_PATH} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("utf-8") + body

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(GATEWAY_SOCKET)
            sock.sendall(req)
            chunks: list[bytes] = []
            while True:
                try:
                    buf = sock.recv(65536)
                except socket.timeout:
                    break
                if not buf:
                    break
                chunks.append(buf)
    except FileNotFoundError:
        return {"code": -2, "msg": f"开放网关不存在: {GATEWAY_SOCKET}"}
    except PermissionError as exc:
        return {"code": -3, "msg": f"无权连接开放网关: {exc}"}
    except OSError as exc:
        return {"code": -4, "msg": f"{type(exc).__name__}: {exc}"}

    raw = b"".join(chunks)
    if not raw:
        return {"code": -5, "msg": "网关返回空响应"}
    try:
        head, _, tail = raw.partition(b"\r\n\r\n")
        text = tail.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"code": -6, "msg": f"响应解码失败: {exc}"}
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        snippet = text[:160].replace("\n", " ")
        return {"code": -7, "msg": f"响应不是 JSON: {snippet}"}


def call(req: str, data: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """调用一个后端能力。req 形如 trim.file.getSharedAccessibleFolders。"""
    payload = {
        "reqId": uuid.uuid4().hex[:16],
        "req": req,
        "appName": app_name(),
        "data": data or {},
    }
    return _http_post(payload, timeout=timeout)


# ---------------------------------------------------------------------------
# 授权目录查询
# ---------------------------------------------------------------------------

def _split_paths(raw: str) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").replace(";", ":").split(":"):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def env_share_paths() -> list[str]:
    """兼容老版本：TRIM_DATA_SHARE_PATHS 里的管理员授权目录。"""
    return _split_paths(os.environ.get(SHARE_PATHS_ENV) or "")


def shared_accessible_folders(force: bool = False) -> tuple[list[str], str]:
    """管理员为应用授权的共享目录（trim.file.getSharedAccessibleFolders）。

    返回 (路径列表, 错误信息)。列表为空 + 错误信息不空 = 查询失败/未授权。
    """
    key = "shared"
    if not force:
        hit = _cached(key)
        if hit is not None:
            return hit  # type: ignore[return-value]
    resp = call("trim.file.getSharedAccessibleFolders")
    if int(resp.get("code") or 0) != 0:
        res: tuple[list[str], str] = (env_share_paths(), str(resp.get("msg") or "未知错误"))
    else:
        data = resp.get("data") or {}
        paths = [str(p) for p in (data.get("paths") or []) if p]
        for p in env_share_paths():
            if p not in paths:
                paths.append(p)
        res = (paths, "")
    return _put(key, res)  # type: ignore[return-value]


def user_accessible_folders(uid: int, force: bool = False) -> tuple[list[str], str]:
    """指定用户授权给应用的目录（trim.file.getUserAccessibleFolders）。"""
    key = f"user:{uid}"
    if not force:
        hit = _cached(key)
        if hit is not None:
            return hit  # type: ignore[return-value]
    resp = call("trim.file.getUserAccessibleFolders", {"uid": int(uid)})
    if int(resp.get("code") or 0) != 0:
        res: tuple[list[str], str] = ([], str(resp.get("msg") or "未知错误"))
    else:
        data = resp.get("data") or {}
        res = ([str(p) for p in (data.get("paths") or []) if p], "")
    return _put(key, res)  # type: ignore[return-value]


def _existing(paths: list[str]) -> list[str]:
    return [p for p in paths if p and os.path.isdir(p)]


def authorized_report(force: bool = False) -> dict:
    """给管理页/诊断页用的一张授权状态快照。"""
    shared, shared_err = shared_accessible_folders(force=force)
    shared = _existing(shared)
    token_present = bool(str(os.environ.get(TOKEN_ENV) or "").strip())
    gateway_present = os.path.exists(GATEWAY_SOCKET)
    return {
        "gateway": {
            "socket": GATEWAY_SOCKET,
            "exists": gateway_present,
            "token_present": token_present,
            "app_name": app_name(),
        },
        "shared_paths": shared,
        "shared_error": shared_err,
        "env_paths": _existing(env_share_paths()),
        "authorized": bool(shared),
        "hint": _hint(shared, shared_err, gateway_present, token_present),
    }


def _hint(shared: list[str], err: str, gateway_present: bool, token_present: bool) -> str:
    if shared:
        return ""
    if not gateway_present:
        return "本机未发现飞牛开放网关（系统版本较低），请在应用设置→授权目录添加，或到管理页手动填写「本地曲库目录」。"
    if not token_present:
        return "进程环境里没有 TRIM_API_TOKEN（需由系统脚本启动），无法查询授权目录；可到管理页手动填写「本地曲库目录」。"
    if err and "仅管理员" in err:
        return "需管理员操作：在应用设置→授权目录里添加曲库目录后重试。"
    return "尚未授权任何目录。请到「应用设置 → 授权目录」添加你的音乐目录（或在管理页手动填写「本地曲库目录」）。"


def pick_library_from_authorized(candidates: list[str], authorized: list[str]) -> str:
    """在候选目录里挑一个**已被授权**的。

    规则：候选路径本身等于某个授权目录，或是某个授权目录的子路径。
    挑不到就返回空串——调用方据此回退并明确告警，绝不静默使用未授权路径。
    """
    auth = [os.path.abspath(p) for p in authorized if p]
    for cand in candidates:
        if not cand or not os.path.isdir(cand):
            continue
        ap = os.path.abspath(cand)
        for a in auth:
            if ap == a or ap.startswith(a.rstrip(os.sep) + os.sep):
                return cand
    return ""
