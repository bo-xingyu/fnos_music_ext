"""Optional NEMbox internals for batch detail / lyrics (NetEase-MusicBox)."""
from __future__ import annotations

import json
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


def _flag_of(d: Any, key: str) -> bool:
    v = d.get(key) if isinstance(d, dict) else None
    return v is True or str(v).lower() == "true"


def is_trial_snippet(item: dict[str, Any]) -> bool:
    """判定该曲目是否只是「试听片段」（不能真正播放）。

    .. warning:: 千万不要写成 ``item.get("freeTrialInfo") or item.get("freeTrialPrivilege")``。

    ``freeTrialPrivilege`` 是网易云**每条 song/url 响应都必带**的标准结构体，即使
    一切正常也存在，且是个非空 dict（＝真理值）。用它做真值判断会让**每一首歌**都被
    判成试听片段而剔除 —— 与是否登录、是否 VIP 完全无关，表现为「搜不到任何在线歌曲、
    每日推荐永远为空」。真正的试听信号在这个结构体**内部的布尔位**里：

    ``resConsumable`` / ``userConsumable`` 为 True 表示正在消耗试听额度。

    ``freeTrialInfo`` 则相反：它为 None 时表示无试听，**只有确实是试听曲目才带非空内容**
    （形如 ``{"st": 起始秒, "et": 结束秒}``），因此对它做存在性判断是安全的。

    两个字段的语义刚好相反，混在一起用真值判断正是本 bug 的成因。
    """
    priv = item.get("freeTrialPrivilege")
    if _flag_of(priv, "resConsumable") or _flag_of(priv, "userConsumable"):
        return True
    info = item.get("freeTrialInfo")
    return bool(info) and info is not None


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

        # 核心铁律：拿不到真实直链一律不放行，试听片段同样不放行
        if not url or not str(url).strip() or code == 404 or is_trial_snippet(item):
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


# ---------------------------------------------------------------------------
# 播放热路径的进程内实现
#
# 为什么必须有这两个函数：``/api/v1/song/{id}/url`` 与 ``/api/v1/song/{id}/info``
# 原先直接 exec CLI（``musicbox song url ...``）。它们位于**播放热路径**上——飞牛
# 播放器每首歌都要各调一次，一次搜索 50 首就是上百次调用。
#
# 实测（真实 NetEase-MusicBox 0.5.3）：
#   CLI 子进程**冷启动 47.37s**，稳态仍有 1.4s；进程内 eapi 取链仅 **0.05s**，
#   songs_detail 原始详情同样是进程内一次 HTTP。
# 而代理侧对这两个请求的 httpx 超时是 **10s** ⇒ 冷启动必然超时。更糟的是超时异常
# ``httpx.ReadTimeout('')`` 的 ``str()`` 是**空串**，日志只剩
# ``resolve_netease_url error for 94344 (quality=exhigh): ``，连异常类型都看不见。
# 用户侧现象即「搜索结果和歌单都出来了，但一直缓冲、无法播放」。
#
# 因此这里改为进程内直取；CLI 仅作为进程内失败时的兜底（保留 exec_musicbox 调用方）。
# ---------------------------------------------------------------------------

# 上游 api.songs_url 里 weapi 降级用的码率映射，原样照搬以免语义漂移
_LEVEL_RATE_MAP = {
    "exhigh": 320000,
    "higher": 192000,
    "standard": 128000,
    "lossless": 999000,
    "hires": 999000,
    "jymaster": 999000,
}


def _level_to_encode_type(level: str) -> str:
    """上游 ``level_to_encode_type`` 的薄封装。

    抽成模块级函数的唯一目的是让测试可以打桩——直接 ``from NEMbox.api import``
    写在函数体里的话，未安装真实 NetEase-MusicBox 的环境会直接 ImportError，
    这条纯逻辑分支就再也测不到。
    """
    from NEMbox.api import level_to_encode_type

    return level_to_encode_type(level)


def _quality_to_level(quality: str) -> str:
    """上游 ``music_quality_to_level`` 的薄封装，理由同上。"""
    from NEMbox.api import music_quality_to_level

    return music_quality_to_level(quality)


def _urls_for_level(api, ids: list[int], level: str) -> list[Any]:
    """按**指定** level 取直链，逐行对齐上游 ``api.songs_url`` 的实现。

    上游 ``songs_url(ids)`` 的 level 取自全局 ``Config().get("music_quality")``，
    **不接受参数**；而本服务的接口需要按请求音质（proxy 会依次试
    lossless → exhigh）取链。直接改全局 Config 会写坏用户配置文件，
    故在此按同样逻辑显式传 level。

    已实测校验：用 Config 里当前的 quality 走本函数，其返回与
    ``api.songs_url()`` **完全一致**，因此这是等价改写而非另起一套。
    """
    params = {
        "ids": json.dumps(ids, separators=(",", ":")),
        "level": level,
        "encodeType": _level_to_encode_type(level),
    }
    try:
        data = api.eapi_request("/api/song/enhance/player/url/v1", params).get("data", [])
    except Exception:  # noqa: BLE001 - eapi 不可用时照上游走 weapi 降级
        data = []
    if data:
        return data if isinstance(data, list) else []
    return (api.request("POST", "/weapi/song/enhance/player/url",
                        {"ids": ids, "br": _LEVEL_RATE_MAP.get(level, 320000)}).get("data") or [])


def _pick_by_id(items: Any, song_id: int) -> dict[str, Any] | None:
    """从返回列表里挑出指定 id 的那条；挑不到就退化为返回唯一一条。"""
    if isinstance(items, dict):
        return items or None
    if not isinstance(items, list):
        return None
    for it in items:
        if isinstance(it, dict):
            try:
                if int(it.get("id") or it.get("song_id") or 0) == song_id:
                    return it
            except (TypeError, ValueError):
                continue
    singles = [x for x in items if isinstance(x, dict)]
    return singles[0] if len(singles) == 1 else None


def song_url_info(song_id: int, quality: str = "exhigh") -> dict[str, Any]:
    """进程内取单曲直链信息（含 code / url / br / level / freeTrialPrivilege）。

    返回结构与 CLI ``musicbox song url <id> --json`` 的 ``data`` 字段一致，
    因此代理侧 ``resolve_netease_url`` 无需改动：它只认 ``code == 200 and url``。
    取不到时返回 ``{}``，由调用方决定是否降级到 CLI。
    """
    sid = int(song_id)
    level = _quality_to_level(quality)
    api = _get_api()
    with _api_lock:
        data = _urls_for_level(api, [sid], level)
    return _pick_by_id(data, sid) or {}


def song_raw_detail(song_id: int) -> dict[str, Any]:
    """进程内取单曲**原始**详情（含 ar / al / dt / sq / hr / h）。

    代理的 ``_online_info`` 期望的正是这个原始形状（它自己解析 ar/al/dt/sq/h），
    而不是 ``_map_song_detail`` 映射后的 song_name/album_pic_url 那套，
    所以这里直接返回 ``api.songs_detail([id])`` 的原始条目。
    """
    sid = int(song_id)
    api = _get_api()
    with _api_lock:
        raw = api.songs_detail([sid])
    return _pick_by_id(raw, sid) or {}


# ---------------------------------------------------------------------------
# 歌单 / 推荐口径 / 红心 / 最高品质下载
#
# 一律走进程内 NEMbox，不走 CLI：
#  - CLI 的 playlist show / album / download 都经 dig_info，任一首取不到直链就把
#    整个结果集清空（HTTP 仍 200），这是 2.1.3 之前的老毛病；
#  - CLI 每次 spawn 子进程，冷启动实测 47s，放在浏览/播放热路径上必然超时（2.1.6）。
# ---------------------------------------------------------------------------

# 逐档降级取"该账号能拿到的最高品质"。顺序即优先级：
# jymaster(臻品母带) > hires(高清无损) > lossless(无损) > exhigh(极高320k)
BEST_QUALITY_CHAIN = ("jymaster", "hires", "lossless", "exhigh")

# 上游返回的歌单封面是 http://（歌曲封面才是 https），必须升级协议：
# 飞牛 UI 跑在 https 下，http 图片会被浏览器按混合内容直接拦掉。
def _https_cover(url: Any) -> str:
    s = str(url or "").strip()
    if s.startswith("http://"):
        return "https://" + s[len("http://"):]
    return s


def _norm_playlist(raw: Any) -> dict[str, Any] | None:
    """把上游原始歌单 dict 归一化成本服务统一形状（容错缺字段）。

    上游 user_playlist / recommend_resource / top_playlists 都是原样透传网易云响应，
    NEMbox 源码只引用过 id/name/creator.nickname，因此 coverImgUrl、trackCount、
    subscribed 这些字段**源码层面无法证明一定存在**，全部按可选处理。
    """
    if not isinstance(raw, dict):
        return None
    pid = raw.get("id") or raw.get("playlistId")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    creator = raw.get("creator") if isinstance(raw.get("creator"), dict) else {}
    name = str(raw.get("name") or raw.get("title") or "").strip()
    if not name:
        name = f"歌单 {pid}"
    return {
        "playlist_id": pid,
        "name": name,
        "cover_url": _https_cover(raw.get("coverImgUrl") or raw.get("picUrl")
                                  or creator.get("backgroundUrl") or ""),
        "track_count": _to_int(raw.get("trackCount") or raw.get("track_count") or 0),
        "description": str(raw.get("description") or "")[:300],
        # 自建 vs 收藏：subscribed 为 True 表示是收藏的别人的歌单
        "subscribed": bool(raw.get("subscribed")),
        "creator": str(creator.get("nickname") or ""),
        "creator_id": _to_int(creator.get("userId") or raw.get("userId") or 0),
    }


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _playlist_list(raw: Any) -> list[dict[str, Any]]:
    out = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        n = _norm_playlist(it)
        if n:
            out.append(n)
    return out


def user_playlists(uid: int, offset: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    """账户歌单（自建 + 收藏），需登录。"""
    api = _get_api()
    with _api_lock:
        raw = api.user_playlist(int(uid), offset=offset, limit=limit)
    return _playlist_list(raw)


def recommend_playlists() -> list[dict[str, Any]]:
    """网易云按账号口味的推荐歌单列表（recommend_resource），需登录。"""
    api = _get_api()
    with _api_lock:
        raw = api.recommend_resource()
    return _playlist_list(raw)


def toplists() -> list[dict[str, Any]]:
    """排行榜清单：上游返回 [(榜单名, 榜单歌单id)]，无需登录。"""
    api = _get_api()
    with _api_lock:
        raw = api.fetch_toplists()
    out = []
    if isinstance(raw, (list, tuple)):
        for pair in raw:
            try:
                name, pid = pair[0], int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            out.append({"playlist_id": pid, "name": str(name), "cover_url": "",
                        "track_count": 0, "description": "", "subscribed": False,
                        "creator": "", "creator_id": 0})
    return out


def category_playlists(category: str = "华语", order: str = "hot",
                       limit: int = 20) -> list[dict[str, Any]]:
    """分类歌单（华语/欧美/场景/情感…），无需登录。"""
    api = _get_api()
    cat = str(category or "华语").strip()
    order_ = "new" if str(order or "hot").strip().lower() == "new" else "hot"
    with _api_lock:
        raw = api.top_playlists(cat, order_, 0, max(1, min(int(limit or 20), 50)))
    return _playlist_list(raw)


def playlist_categories() -> dict[str, list[str]]:
    """歌单分类目录 {大类名: [子类名]}；上游失败时用 NEMbox 内置常量兜底。"""
    api = _get_api()
    with _api_lock:
        raw = api.playlist_catelogs()
    parsed = api._parse_playlist_classes(raw) if isinstance(raw, dict) else {}
    if not parsed:
        try:
            parsed = dict(api._get_playlist_classes() or {})
        except Exception:  # noqa: BLE001
            parsed = {}
    return {str(k): [str(x) for x in v] for k, v in parsed.items() if v}


def new_albums(limit: int = 20) -> list[dict[str, Any]]:
    """新碟上架（专辑维度），无需登录。"""
    api = _get_api()
    with _api_lock:
        raw = api.new_albums(offset=0, limit=max(1, min(int(limit or 20), 50)))
    out = []
    if isinstance(raw, list):
        for a in raw:
            if not isinstance(a, dict):
                continue
            aid = _to_int(a.get("id"))
            if not aid:
                continue
            artist = a.get("artist") if isinstance(a.get("artist"), dict) else {}
            out.append({
                "album_id": aid,
                "name": str(a.get("name") or f"专辑 {aid}"),
                "cover_url": _https_cover(a.get("picUrl") or a.get("blurPicUrl") or ""),
                "artist": str(artist.get("name") or ""),
                "publish_time": _to_int(a.get("publishTime") or 0),
            })
    return out


def personal_fm() -> list[dict[str, Any]]:
    """私人FM / 漫游曲目，需登录。返回已按可播性过滤的 song_info 列表。"""
    api = _get_api()
    with _api_lock:
        raw = api.personal_fm()
    return _songs_from_raw_list(raw)


def playlist_track_ids(playlist_id: int, limit: int = 300) -> list[int]:
    """取歌单内曲目 id。

    ⚠️ 上游 ``playlist_songlist`` 返回的 ``trackIds`` **不是纯 id 列表**，而是
    ``[{"id": …, "v": …, "at": …}, …]`` 这种 dict 列表（实测确认），
    直接当 int 用会全线炸掉。同时兼容两种形态。
    """
    api = _get_api()
    with _api_lock:
        raw = api.playlist_songlist(int(playlist_id))
    ids: list[int] = []
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                sid = _to_int(it.get("id"))
            else:
                sid = _to_int(it)
            if sid:
                ids.append(sid)
            if len(ids) >= max(1, min(int(limit or 300), 1000)):
                break
    return ids


def album_songs(album_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """专辑内曲目。上游 ``album()`` 直接返回歌曲 dict 列表。"""
    api = _get_api()
    with _api_lock:
        raw = api.album(int(album_id))
    return _songs_from_raw_list(raw, limit=limit)


def _songs_from_raw_list(raw: Any, limit: int = 300) -> list[dict[str, Any]]:
    """把一批上游原始 song dict 逐首过滤后映射成 song_info。

    与 search_songs / daily_songs 同一套规则：坏数据只影响它自己那一首，
    绝不整表清空。
    """
    if not isinstance(raw, list) or not raw:
        return []
    ids = [_safe_sid(s) for s in raw]
    ids = [i for i in ids if i]
    if not ids:
        return []
    playable = playable_url_map(ids)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    cap = max(1, int(limit or 300))
    for song in raw:
        if not isinstance(song, dict):
            continue
        sid = _safe_sid(song)
        if not sid or sid in seen or sid not in playable:
            continue
        seen.add(sid)
        out.append(_song_info_from_raw(song, playable[sid]))
        if len(out) >= cap:
            break
    return out


def songs_by_ids(ids: list[int], limit: int = 300) -> list[dict[str, Any]]:
    """按 id 批量取可播曲目（歌单/排行榜内容都走这里）。"""
    clean = [int(i) for i in ids if _to_int(i)]
    if not clean:
        return []
    api = _get_api()
    with _api_lock:
        raw = api.songs_detail(clean[:1000])
    return _songs_from_raw_list(raw, limit=limit)


def best_url_info(song_id: int) -> dict[str, Any]:
    """按 jymaster → hires → lossless → exhigh 逐档试，取该账号能拿到的直链。

    ⚠️ 上游会按账号权益**自动降级**并以 ``code=200`` 返回：请求 jymaster 时，
    非臻品权益的账号拿回来的可能是 ``level=exhigh``（实测匿名账号对免费曲即如此）。
    所以 ``best_quality`` **必须取响应里的实际 ``level``**，而不是我们请求的档位，
    否则日志与归档 sidecar 都会谎称存了臻品母带，实际只有 320k mp3。
    ``requested_level`` 保留请求档位，便于对照"想要什么 vs 拿到什么"。

    一档都取不到返回 ``{}``。
    """
    sid = int(song_id)
    api = _get_api()
    for level in BEST_QUALITY_CHAIN:
        with _api_lock:
            try:
                data = _urls_for_level(api, [sid], level)
            except Exception:  # noqa: BLE001
                data = []
        item = _pick_by_id(data, sid)
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url and item.get("code") == 200 and not is_trial_snippet(item):
            item = dict(item)
            actual = str(item.get("level") or "").strip().lower()
            item["best_quality"] = actual or level
            item["requested_level"] = level
            # 上游降级了要留痕：这是"账号权益不够"而非"我们没试更高档"
            item["downgraded"] = bool(actual) and actual != level
            return item
    return {}


def song_like(song_id: int, like: bool = True) -> dict[str, Any]:
    """红心/取消红心（收藏同步回网易云），需登录。

    上游是 ``song_like(songid, like=True)`` → eapi ``/api/song/like``
    参数 ``{trackId, userid, like}``，返回 bool。
    注意：``like=False`` 这条分支在 NEMbox 源码里从未被调用过（CLI 只有加红心入口），
    属于未经验证路径，因此这里把上游返回值与异常都如实回传给调用方，不静默吞掉。
    """
    api = _get_api()
    try:
        with _api_lock:
            ok = api.song_like(int(song_id), like=bool(like))
        return {"ok": bool(ok), "song_id": int(song_id), "like": bool(like),
                "requires_login": bool(not ok)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "song_id": int(song_id), "like": bool(like),
                "error": f"{type(exc).__name__}: {exc}"[:200]}
