"""Optional NEMbox internals for batch detail / lyrics (NetEase-MusicBox)."""
from __future__ import annotations

import os
import threading
import time
from typing import Any

_api_lock = threading.Lock()          # 保护单次 API 调用（NEMbox 非线程安全）
_api_init_lock = threading.Lock()     # 单独一把锁管理实例生命周期，避免与上面嵌套死锁
_api_instance = None
_api_cookie_stamp: tuple | None = None

# 未登录时是否降级为只播免费曲目（默认开；关掉则未登录直接不放行任何在线曲目）
FREE_ONLY_ON_LOGOUT = (
    os.environ.get("FNMUSIC_FREE_ONLY_ON_LOGOUT", "true").strip().lower()
    in ("true", "1", "yes", "on")
)


def _cookie_stamp(api) -> tuple:
    """cookie 文件的 (mtime_ns, size) 指纹。取不到就用空指纹（视为"无 cookie"）。"""
    try:
        path = getattr(getattr(api, "storage", None), "cookie_path", "") or ""
        if path and os.path.exists(path):
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    return ()


def _build_api():
    from runner import ensure_xdg_dirs

    ensure_xdg_dirs()
    from NEMbox.api import NetEase

    return NetEase()


def reset_api_instance() -> None:
    """丢弃进程内的 NetEase 实例与登录态缓存，强制下次重建。

    NEMbox 的 cookie 只在 ``NetEase.__init__`` 里 ``cookie_jar.load()`` 一次，之后**永不重读**。
    而扫码登录是由 **musicbox CLI 子进程**完成并写盘的（`netease_login.sh`、
    页面的扫码流程都是），子进程写完 cookie 后，父进程里那个长命单例仍握着登录前的
    旧 cookie —— 于是"CLI 说已登录、进程内说未登录"，可播性过滤按未登录处理，
    VIP/付费曲目被全部剔除，表现为**登录后搜索与每日推荐依然为空**。

    登录/登出后必须调用本函数；此外 _get_api() 也会按 cookie 文件指纹自动重建，
    以覆盖登录发生在别的进程、或 cookie 被外部改写/过期的情况。
    """
    global _api_instance, _api_cookie_stamp
    with _api_init_lock:
        _api_instance = None
        _api_cookie_stamp = None
    invalidate_login_cache()


def _get_api():
    """取 NetEase 实例；cookie 文件发生变化时自动重建，避免长期持有过期登录态。"""
    global _api_instance, _api_cookie_stamp

    # 快路径：已有实例且 cookie 指纹未变
    if _api_instance is not None and _cookie_stamp(_api_instance) == _api_cookie_stamp:
        return _api_instance

    with _api_init_lock:
        # 双检：等锁期间可能已被重建
        if _api_instance is not None:
            stamp = _cookie_stamp(_api_instance)
            if stamp == _api_cookie_stamp:
                return _api_instance
            # cookie 变了：旧实例的 cookie_jar 不会再重读，必须整个重建
            try:
                _api_instance = _build_api()
                _api_cookie_stamp = _cookie_stamp(_api_instance)
                invalidate_login_cache()
            except Exception:  # noqa: BLE001 - 重建失败时继续用旧实例，总比直接崩好
                pass
            return _api_instance

        _api_instance = _build_api()
        _api_cookie_stamp = _cookie_stamp(_api_instance)
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


_LOGIN_CACHE: tuple[float, bool] = (0.0, False)
# 登录态缓存 TTL。check_is_logged_in() 每次都要向网易云发一次
# /weapi/nuser/account/get，而搜索与日推每轮都会调它，白占一次跨洋往返。
_LOGIN_CACHE_TTL = float(os.environ.get("FNMUSIC_LOGIN_CACHE_TTL", "300"))


def check_is_logged_in(force: bool = False) -> bool:
    """当前账号是否已登录（带 TTL 缓存）。

    这个函数在每次搜索/日推时可播性过滤里都会被调用，直连网易云一次约几百毫秒，
    缓存掉能显著缩短首屏耗时。登录态变化由 invalidate_login_cache() 主动清除。
    """
    global _LOGIN_CACHE
    now = time.monotonic()
    if not force and _LOGIN_CACHE[0] and (now - _LOGIN_CACHE[0]) < _LOGIN_CACHE_TTL:
        return _LOGIN_CACHE[1]
    value = False
    try:
        api = _get_api()
        with _api_lock:
            info = api.get_account_info()
        value = bool(info and (info.get("account") or info.get("profile")))
    except Exception:
        # 探测失败不缓存为"未登录太久"，否则一次网络抖动会让账号在 TTL 内
        # 一直被当成未登录，VIP 曲目全被过滤掉
        if _LOGIN_CACHE[0]:
            return _LOGIN_CACHE[1]
        return False
    _LOGIN_CACHE = (now, value)
    return value


def invalidate_login_cache() -> None:
    """登录态变化（扫码登录/登出）后调用。"""
    global _LOGIN_CACHE
    _LOGIN_CACHE = (0.0, False)


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


_VIP_EXPIRY_KEYS = (
    "vipExpiryTime", "vipExpiry", "vipExpires", "vip_expire", "vipExpireTime",
    "expireTime", "expiredTime", "endTime", "validTime",
)


def _looks_like_future_ms(value: Any) -> bool:
    """只接受"看起来像未来的毫秒时间戳"的值，绝不把别的字段硬凑成到期时间。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False
    if n <= 0:
        return False
    import time as _t

    now_ms = _t.time() * 1000
    # 合理区间：当前时间之后、且不超过 50 年
    return now_ms < n < now_ms + 50 * 365 * 86400 * 1000


def _find_vip_expiry(*sources: dict[str, Any]) -> int:
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in _VIP_EXPIRY_KEYS:
            v = src.get(key)
            if _looks_like_future_ms(v):
                return int(v)
    return 0


def auth_detail() -> dict[str, Any]:
    """登录态详情：是否登录、昵称、userId、VIP 类型与到期时间。

    只读取 NEMbox 已有的 get_account_info()，不额外发请求；任何字段缺失都
    以 None/0 返回，不编造。

    注意 VIP 到期时间：NEMbox 上游（0.5.3）**没有任何提供到期时间的接口**，
    get_account_info 只有 vipType。这里对若干候选字段名做尽力探测，探不到就
    如实返回 0，由调用方显示"上游未提供"——绝不臆造一个天数。
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
    vip_type = _pick(src, "vipType", "vip_type") or _pick(account, "vipType", "vip_type")
    logged_in = bool(profile or account or (nickname and user_id))

    try:
        vip_type_int = int(vip_type) if vip_type is not None else 0
    except (TypeError, ValueError):
        vip_type_int = 0

    vip_expire_int = _find_vip_expiry(src, account, profile, info)
    if not vip_expire_int and vip_type_int > 0:
        # 尽力再问一次用户详情接口；失败或字段缺失都静默放弃，不编造
        try:
            uid = int(user_id) if user_id else 0
        except (TypeError, ValueError):
            uid = 0
        if uid:
            try:
                with _api_lock:
                    detail = api.request("POST", f"/weapi/v1/user/detail/{uid}") or {}
            except Exception:  # noqa: BLE001
                detail = {}
            if isinstance(detail, dict):
                dp = detail.get("profile") if isinstance(detail.get("profile"), dict) else {}
                vip_expire_int = _find_vip_expiry(dp, detail)

    return {
        "logged_in": logged_in,
        "nickname": str(nickname or ""),
        "user_id": str(user_id or ""),
        "vip_type": vip_type_int,
        "vip_expires_ms": vip_expire_int,
        # 上游没提供到期时间时明确标记，让前端显示"未知"而不是"剩余 0 天"
        "vip_expires_known": vip_expire_int > 0,
    }


def filter_playable_song_ids(ids: list[int]) -> set[int]:
    """根据真实可播放状态过滤歌曲 ID（集合形式，保留给既有调用方）。"""
    return set(playable_url_map(ids).keys())


def playable_url_map(ids: list[int]) -> dict[int, dict[str, Any]]:
    """返回 {song_id: 直链信息} ——只包含当前账号**真实可播**的曲目。

    与上游 NEMbox 的 ``dig_info`` 有本质区别：dig_info 在**任意一首**歌取不到 url 时
    会 ``return []``，把整个列表清空（api.py 里注释自承"可能因网络波动"）。
    那是本项目「搜索/日推返回 200 但结果为空」的根因，因此这里改为**逐首判定**：
    坏数据只影响它自己那一首。

    过滤规则：
    - 已登录：账号自身权益内、能拿到完整真实直链的曲目放行（含 VIP / 无损 / 已购）；
    - 未登录：降级为只播免费曲目（``FNMUSIC_FREE_ONLY_ON_LOGOUT``，默认开）；
    - 任何情况下，url 为空 / code 404 / 带试听片段标记的曲目都不放行。
    """
    if not ids:
        return {}
    api = _get_api()
    try:
        with _api_lock:
            urls_data = api.songs_url(ids)
    except Exception:
        return {}
    if not isinstance(urls_data, list):
        return {}

    logged_in = check_is_logged_in()
    out: dict[int, dict[str, Any]] = {}
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

        # 核心铁律：拿不到真实直链一律不放行，试听片段同样不放行
        if not url or not str(url).strip() or code == 404 or free_trial:
            continue
        # 未登录时降级：只保留免费曲目（fee 0=免费，8=VIP 但未登录必然无 url，已被上面挡掉）
        if not logged_in and FREE_ONLY_ON_LOGOUT and fee not in (0, 8):
            continue

        out[sid_int] = item
    return out


def quality_of(url_info: dict[str, Any]) -> str:
    """按上游 Parse.song_url 的同款逻辑判定音质字符串。

    刻意与 NEMbox 保持一致（LOSSLESS / HIRES / JYMASTER / FLAC / "HD 320k" …），
    这样代理层的无损判定和 UI 展示都拿的是同一套词汇。
    """
    level = str(url_info.get("level") or "").upper()
    stype = str(url_info.get("type") or "").upper()
    try:
        br = int(url_info.get("br") or 0)
    except (TypeError, ValueError):
        br = 0
    if level in ("LOSSLESS", "HIRES", "JYMASTER") and stype:
        return f"{level} {stype}"
    if stype == "FLAC" and level:
        return f"{level} FLAC"
    if stype == "FLAC":
        return "LOSSLESS FLAC"
    if br >= 999000:
        return "LOSSLESS"
    if br >= 320000:
        return f"HD {br // 1000}k"
    if br >= 192000:
        return f"MD {br // 1000}k"
    if br:
        return f"LD {br // 1000}k"
    return "LD 128k"


def _song_info_from_raw(raw: dict[str, Any], url_info: dict[str, Any] | None) -> dict[str, Any]:
    """把网易云原始 song dict 映射成与 CLI song_info 兼容的结构。

    额外带上 album_pic_url / has_sq / has_hr，让代理层不必再为补封面
    多发一次 /api/v1/songs/detail。
    """
    mapped = _map_song_detail(raw)
    info = {
        "song_id": mapped["song_id"],
        "song_name": mapped["name"],
        "artist": mapped["artist"],
        "album_name": mapped["album_name"],
        "album_id": (raw.get("al") or {}).get("id", "") if isinstance(raw.get("al"), dict) else "",
        "album_pic_url": mapped["album_pic_url"],
        "duration": int(round((mapped["duration_ms"] or 0) / 1000)),
        "has_sq": mapped["has_sq"],
        "has_hr": mapped["has_hr"],
        "quality": quality_of(url_info or {}),
        "mp3_url": str((url_info or {}).get("url") or ""),
    }
    return info


def search_songs(keyword: str, limit: int = 50) -> list[dict[str, Any]]:
    """进程内搜索，逐首过滤可播性。

    不走 ``musicbox search`` CLI —— 它内部调 dig_info，任何一首取不到直链就会
    让整个结果集变成空列表（HTTP 仍为 200），用户表现为"搜不到任何在线歌曲"。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    api = _get_api()
    try:
        with _api_lock:
            result = api.search(kw, limit=max(1, min(int(limit or 50), 200)))
    except Exception:
        return []
    if not isinstance(result, dict):
        return []
    raw_songs = result.get("songs")
    if not isinstance(raw_songs, list):
        return []

    ids = [_safe_sid(s) for s in raw_songs]
    ids = [i for i in ids if i]
    if not ids:
        return []

    playable = playable_url_map(ids)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in raw_songs:
        if not isinstance(raw, dict):
            continue
        sid = _safe_sid(raw)
        if not sid or sid in seen or sid not in playable:
            continue
        seen.add(sid)
        out.append(_song_info_from_raw(raw, playable[sid]))
    return out[: max(1, int(limit or 50))]


def daily_songs(limit: int = 20) -> list[dict[str, Any]]:
    """网易云官方每日推荐，进程内实现 + 逐首过滤。

    同样不走 ``musicbox recommend songs`` CLI：它经 dig_info，一首坏数据就会
    把整份日推清空，表现为"每日推荐歌单不出现"。这里逐首判定，
    只有真正拿不到直链的那几首被剔除。
    """
    api = _get_api()
    try:
        with _api_lock:
            raw = api.recommend_playlist(limit=max(1, min(int(limit or 20), 200)))
    except Exception:
        return []
    if not isinstance(raw, list) or not raw:
        return []

    ids = [_safe_sid(s) for s in raw]
    ids = [i for i in ids if i]
    if not ids:
        return []

    playable = playable_url_map(ids)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for song in raw:
        if not isinstance(song, dict):
            continue
        sid = _safe_sid(song)
        if not sid or sid in seen or sid not in playable:
            continue
        seen.add(sid)
        out.append(_song_info_from_raw(song, playable[sid]))
        if len(out) >= max(1, int(limit or 20)):
            break
    return out


def _safe_sid(raw: Any) -> int:
    if not isinstance(raw, dict):
        return 0
    sid = raw.get("id") or raw.get("song_id")
    try:
        return int(sid) if sid is not None else 0
    except (TypeError, ValueError):
        return 0


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
