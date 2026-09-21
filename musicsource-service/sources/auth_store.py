"""扩展音源登录凭据存储（Cookie / Token）。

凭据落在数据目录 ``data/auth/{source}.json``，权限 0600。
调用方只通过本模块读写，适配器不直接碰磁盘。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    raw = (os.environ.get("FNMUSIC_MUSICSOURCE_DATA") or "").strip()
    if raw:
        base = Path(raw)
    else:
        base = Path(__file__).resolve().parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    auth = base / "auth"
    auth.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _auth_dir() -> Path:
    d = data_dir() / "auth"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_key(source: str) -> str:
    s = (source or "").strip().lower()
    if not s or not all(c.isalnum() or c in "_-" for c in s):
        raise ValueError(f"非法音源标识: {source!r}")
    return s


def _path(source: str) -> Path:
    return _auth_dir() / f"{_safe_key(source)}.json"


def load(source: str) -> dict[str, Any]:
    p = _path(source)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save(source: str, cookie: str = "", token: str = "",
         nickname: str = "", extra: dict | None = None) -> dict[str, Any]:
    key = _safe_key(source)
    cookie = (cookie or "").strip()
    token = (token or "").strip()
    record = {
        "source": key,
        "cookie": cookie,
        "token": token,
        "nickname": (nickname or "").strip(),
        "updated_at": time.time(),
        "logged_in": bool(cookie or token),
    }
    if extra:
        for k, v in extra.items():
            if k not in record:
                record[k] = v
    p = _path(key)
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return record


def set_cookie(source: str, cookie: str, nickname: str = "") -> dict[str, Any]:
    prev = load(source)
    return save(source, cookie=cookie, token=prev.get("token") or "",
                nickname=nickname or prev.get("nickname") or "")


def set_token(source: str, token: str, nickname: str = "") -> dict[str, Any]:
    prev = load(source)
    return save(source, cookie=prev.get("cookie") or "", token=token,
                nickname=nickname or prev.get("nickname") or "")


def clear(source: str) -> bool:
    p = _path(source)
    if p.is_file():
        p.unlink()
        return True
    return False


def cookie_of(source: str) -> str:
    return str(load(source).get("cookie") or "")


def token_of(source: str) -> str:
    return str(load(source).get("token") or "")


def status(source: str) -> dict[str, Any]:
    rec = load(source)
    cookie = str(rec.get("cookie") or "")
    token = str(rec.get("token") or "")
    return {
        "source": _safe_key(source),
        "logged_in": bool(cookie or token),
        "has_cookie": bool(cookie),
        "has_token": bool(token),
        "nickname": str(rec.get("nickname") or ""),
        "updated_at": rec.get("updated_at"),
        # 不回显完整 cookie/token
        "cookie_preview": (cookie[:12] + "…") if len(cookie) > 12 else ("已设置" if cookie else ""),
        "token_preview": (token[:8] + "…") if len(token) > 8 else ("已设置" if token else ""),
    }


def all_status(sources: list[str]) -> list[dict[str, Any]]:
    out = []
    for s in sources:
        try:
            out.append(status(s))
        except ValueError:
            continue
    return out


def cookie_header(source: str) -> dict[str, str]:
    """适配器用：有 cookie 时返回 Cookie 头。"""
    c = cookie_of(source)
    return {"Cookie": c} if c else {}
