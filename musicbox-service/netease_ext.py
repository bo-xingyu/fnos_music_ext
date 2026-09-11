"""Optional NEMbox internals for batch detail / lyrics (NetEase-MusicBox)."""
from __future__ import annotations

import os
import threading
from typing import Any

_api_lock = threading.Lock()
_api_instance = None

# 未登录时是否降级为只播免费曲目（默认开；关掉则未登录直接不放行任何在线曲目）
FREE_ONLY_ON_LOGOUT = (
    os.environ.get("FNMUSIC_FREE_ONLY_ON_LOGOUT", "true").strip().lower()
    in ("true", "1", "yes", "on")
)


def _get_api():
    global _api_instance
    if _api_instance is None:
        with _api_lock:
            if _api_instance is None:
                from runner import ensure_xdg_dirs

                ensure_xdg_dirs()
                from NEMbox.api import NetEase

                _api_instance = NetEase()
    return _api_instance


def _map_song_detail(item: dict[str, Any]) -> dict[str, Any]:
    sid = item.get("id") or item.get("song_id")
    song_id = int(sid) if sid is not None else 0
    name = str(item.get("name") or "")
    ar_list = item.get("ar") or item.get("artists") or []
    if isinstance(ar_list, list):
        artist = " / ".join(
            str(a.get("name")) for a in ar_list if isinstance(a, dict) and a.get("name")
        )
    else:
        artist = ""
    al = item.get("al") or item.get("album") or {}
    if isinstance(al, dict):
        album_name = str(al.get("name") or "")
        album_pic_url = str(al.get("picUrl") or al.get("pic_url") or "")
    else:
        album_name = ""
        album_pic_url = ""
    duration_ms = int(item.get("dt") or item.get("duration") or 0)
    return {
        "song_id": song_id,
        "name": name,
        "artist": artist,
        "album_name": album_name,
        "album_pic_url": album_pic_url,
        "duration_ms": duration_ms,
        "has_sq": bool(item.get("sq")),
        "has_hr": bool(item.get("hr")),
    }


def check_is_logged_in() -> bool:
    try:
        api = _get_api()
        with _api_lock:
            info = api.get_account_info()
        return bool(info and (info.get("account") or info.get("profile")))
    except Exception:
        return False


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def auth_detail() -> dict[str, Any]:
    """登录态详情：是否登录、昵称、userId、VIP 类型与到期时间。

    只读取 NEMbox 已有的 get_account_info()，不额外发请求；任何字段缺失都
    以 None/0 返回，不编造。VIP 到期时间字段名在各版本网易返回里不完全一致，
    这里按常见几种命名依次尝试。
    """
    try:
        api = _get_api()
        with _api_lock:
            info = api.get_account_info() or {}
    except Exception as exc:  # noqa: BLE001 - 上游异常统一降级为未登录
        return {"logged_in": False, "error": str(exc)[:200]}

    if not isinstance(info, dict):
        return {"logged_in": False}

    profile = info.get("profile") if isinstance(info.get("profile"), dict) else {}
    account = info.get("account") if isinstance(info.get("account"), dict) else {}
    src: dict[str, Any] = profile or account or info

    nickname = _pick(src, "nickname", "userName", "nick_name", "name")
    user_id = _pick(src, "userId", "user_id", "id") or _pick(info, "userId", "user_id")
    vip_type = _pick(src, "vipType", "vip_type") or _pick(account, "vipType")
    vip_expire = _pick(src, "vipExpiryTime", "vipExpiry", "vip_expire") or _pick(
        account, "vipExpiryTime", "vip_expire"
    )
    logged_in = bool(profile or account or (nickname and user_id))

    try:
        vip_type_int = int(vip_type) if vip_type is not None else 0
    except (TypeError, ValueError):
        vip_type_int = 0
    try:
        vip_expire_int = int(vip_expire) if vip_expire is not None else 0
    except (TypeError, ValueError):
        vip_expire_int = 0

    return {
        "logged_in": logged_in,
        "nickname": str(nickname or ""),
        "user_id": str(user_id or ""),
        "vip_type": vip_type_int,
        "vip_expires_ms": vip_expire_int,
    }


def filter_playable_song_ids(ids: list[int]) -> set[int]:
    """根据真实可播放状态过滤歌曲 ID。

    音源只来自当前扫码登录的那个私人网易云账号，因此：

    - **已登录**：账号自身权益内的曲目（含 VIP / 无损 / 已购付费专辑）只要能拿到
      完整真实直链就放行；无权益的曲目仍然过滤。
    - **未登录**：降级为只播免费曲目（``FNMUSIC_FREE_ONLY_ON_LOGOUT``，默认开）。
    - 只能试听片段（带 freeTrialInfo）的曲目，无论是否登录都绝不当作可播返回。
    """
    if not ids:
        return set()
    api = _get_api()
    with _api_lock:
        try:
            urls_data = api.songs_url(ids)
        except Exception:
            return set()
    if not isinstance(urls_data, list):
        return set()

    logged_in = check_is_logged_in()
    playable_ids: set[int] = set()
    for item in urls_data:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        url = item.get("url")
        code = item.get("code")
        fee = item.get("fee", 0)
        free_trial = item.get("freeTrialInfo") or item.get("freeTrialPrivilege")

        # 核心铁律：拿不到真实直链（url 为空 / 404）一律过滤，试听片段同样过滤
        if not url or not str(url).strip() or code == 404 or free_trial:
            continue
        # 未登录时降级：只保留免费曲目（fee 0=免费，8=VIP 曲但未登录必然无 url，已被上面挡掉）
        if not logged_in and FREE_ONLY_ON_LOGOUT and fee not in (0, 8):
            continue

        playable_ids.add(sid_int)
    return playable_ids


def batch_song_details(ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    api = _get_api()
    with _api_lock:
        raw_items = api.songs_detail(ids)
    if not raw_items or not isinstance(raw_items, list):
        return []

    playable_ids = filter_playable_song_ids(ids)

    detail_map: dict[int, dict[str, Any]] = {}
    for item in raw_items:
        if isinstance(item, dict):
            mapped = _map_song_detail(item)
            if mapped["song_id"] in playable_ids:
                detail_map[mapped["song_id"]] = mapped
    return [detail_map[sid] for sid in ids if sid in detail_map]


def song_lyric_pair(song_id: int) -> dict[str, str]:
    api = _get_api()
    with _api_lock:
        raw_lyric = api.song_lyric(song_id)
        raw_tlyric = api.song_tlyric(song_id)
    lyric_str = "\n".join(str(line) for line in raw_lyric) if isinstance(raw_lyric, list) else ""
    tlyric_str = "\n".join(str(line) for line in raw_tlyric) if isinstance(raw_tlyric, list) else ""
    return {"lyric": lyric_str, "tlyric": tlyric_str}
