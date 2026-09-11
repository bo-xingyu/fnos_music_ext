"""按飞牛「音质偏好 / 网络类型」动态决定向网易云要哪一档音质。

## 为什么不直接读飞牛的设置接口

飞牛的音质偏好（WiFi 用原始/标准、流量用原始/标准）落在哪个接口、哪个库表、哪个字段，
**我们没有可靠证据**：没有公开契约文档，而猜一个接口名或字段名去读，猜错时不会报错，
只会静默地一直走默认音质——用户以为"跟随飞牛"生效了，其实从来没生效过。这类静默失效
比明确不支持更难排查。

所以本模块采取「**可发现 + 可报告 + 有手动兜底**」：

1. **手动策略**（管理页）永远可用，是确定生效的那条路；
2. **被动发现**只依据我们真正观察到的东西——客户端请求里出现过的 query/header 键、
   ``music.db`` 里 schema 容错扫描出来的偏好行——**不预设任何接口名或字段名**；
3. 观察到的证据全部如实上报（``report()`` → 诊断页的 quality 段），因此「有没有读到、
   从哪儿读到的、原始值是什么」在真机上一眼可见，需要进一步适配时有据可依。

## 飞牛档位 → 网易云档位

飞牛只有「原始 / 标准」两档，网易云有 6 档，不是一一对应。映射刻意保守：
原始＝尽可能高（lossless），标准＝320k（exhigh）。把「标准」映射成 128k（standard）
会掉得太狠；映射成无损又失去省流量的意义。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger("fnmusic_proxy")

# 与 musicbox 侧 QUALITY_WHITELIST 一致，顺序由高到低
LEVELS = ("jymaster", "hires", "lossless", "exhigh", "higher", "standard")

LEVEL_HINTS = {
    "jymaster": ("臻品", "母带", "master", "jymaster"),
    "hires": ("hires", "hi-res", "高清无损"),
    "lossless": ("lossless", "无损", "flac", "原始", "original", "highest"),
    "exhigh": ("exhigh", "极高", "320", "高音质", "标准", "standard_quality"),
    "higher": ("higher", "较高", "192"),
    "standard": ("standard", "流畅", "128", "省流量", "low"),
}
# ⚠️ 这里有一处**真实的语义歧义**，取舍是刻意的：
# 飞牛的「标准音质」映射到 exhigh(320k)，而网易云自己也有一个叫 `standard` 的档位，
# 含义是 128k —— 二者字面相近却差两档。处理规则：
#   - **精确等于**网易云档位名时按网易云原义解析（``_norm_level`` 先查 LEVELS）：
#     来源若用的就是网易云词汇，照搬其含义最不容易出错；
#   - 只有中文标签「原始 / 标准」以及 original / 无损 / 省流量这类**飞牛语义**，
#     才按上面的映射表落到 lossless / exhigh。
# 飞牛设置页的实际文案是中文（原始音质 / 标准音质），走的是第二条，符合用户预期。

_CELLULAR = ("cellular", "cell", "4g", "5g", "mobile", "wwan", "lte", "流量", "蜂窝")
_WIFI = ("wifi", "wi-fi", "wlan", "wireless", "无线")

# 请求里可能承载"音质/网络"语义的键名子串。**不预设完整键名**，命中就记录原值，
# 用来在真机上发现飞牛到底传了什么。
_HINT_KEYS = ("quality", "bitrate", "network", "nettype", "net_type",
              "audiotype", "audio_type", "prefer", "transcode")

DEFAULT_POLICY = "follow_fnos"
POLICIES = ("follow_fnos", "fixed", "by_network")

# 进程内观察记录（用于诊断与自动策略）
_OBSERVED: dict[str, Any] = {
    "paths": {}, "hints": {}, "db": None, "db_path": "", "db_scanned_at": 0.0,
}
_DB_RESCAN_S = float(os.environ.get("FNMUSIC_QUALITY_DB_RESCAN", "300") or 300)


# ---------------------------------------------------------------------------
# 归一化与配置
# ---------------------------------------------------------------------------


def _norm_level(value: Any) -> str:
    """把各种写法归一化成网易云档位；认不出来返回空串（**不瞎猜**）。"""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if s in LEVELS:
        return s
    for level, hints in LEVEL_HINTS.items():
        if any(h in s for h in hints):
            return level
    return ""


def policy() -> str:
    p = str(os.environ.get("FNMUSIC_QUALITY_POLICY", DEFAULT_POLICY)
            or DEFAULT_POLICY).strip().lower()
    return p if p in POLICIES else DEFAULT_POLICY


def _configured(name: str, default: str = "") -> str:
    """读一个档位配置。

    环境变量缺失 → 用该字段默认值；值写了但**认不出来** → 返回空串，交给 ``resolve()``
    统一回落到既有 ``netease_quality``（再不行才是 lossless）。不在这里悄悄替换成默认档：
    那样用户填个错别字，页面显示与实际生效的就会对不上，而回落链会让 ``source``
    如实标注是回落来的。
    """
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default if default in LEVELS else ""
    return _norm_level(raw)


def fixed_level() -> str:
    return _configured("FNMUSIC_QUALITY_FIXED", "lossless")


def wifi_level() -> str:
    return _configured("FNMUSIC_QUALITY_WIFI", "lossless")


def cellular_level() -> str:
    """流量场景默认 320k，对应飞牛「标准音质」省流量的语义。"""
    return _configured("FNMUSIC_QUALITY_CELLULAR", "exhigh")


def _default_level() -> str:
    """跟随不了、也没配手动策略时的最后兜底：沿用既有 netease_quality。"""
    return _configured("FNMUSIC_NETEASE_QUALITY", "lossless") or "lossless"


# ---------------------------------------------------------------------------
# 被动发现之一：客户端请求线索
# ---------------------------------------------------------------------------


def _kv(obj: Any) -> list[tuple[str, Any]]:
    """把 QueryParams / Headers / dict / 任意怪东西统一成 (键, 值) 列表。

    这是旁路观察，任何入参都不能让它抛异常——否则会把正常请求搞挂。
    """
    if obj is None:
        return []
    for attr in ("multi_items", "items"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return [(str(k), v) for k, v in fn()]
            except Exception:  # noqa: BLE001
                break
    if isinstance(obj, (list, tuple)):
        out = []
        for pair in obj:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                out.append((str(pair[0]), pair[1]))
        return out
    return []


def observe_request(method: str, path: str, query: Any = None, headers: Any = None) -> None:
    """记录请求里出现的音质/网络线索。**只记录**，不推断、不改行为。

    这是发现"飞牛到底怎么表达音质偏好"的唯一可靠途径：我们的代理就架在飞牛 socket 上，
    客户端每个请求都经过这里。命中的键与值样例会进 report()，真机跑一会儿就能看到
    飞牛实际传了什么，比猜接口名诚实得多。
    """
    found: dict[str, str] = {}
    for source, items in (("query", _kv(query)), ("header", _kv(headers))):
        for key, val in items:
            low = str(key).lower()
            if not any(h in low for h in _HINT_KEYS):
                continue
            val_s = str(val)
            if len(val_s) > 120:
                val_s = val_s[:120] + "…"
            found[f"{source}.{key}"] = val_s
    if not found:
        return
    key = f"{str(method).upper()} {str(path).split('?')[0]}"
    _OBSERVED["paths"][key] = _OBSERVED["paths"].get(key, 0) + 1
    for name, val in found.items():
        rec = _OBSERVED["hints"].setdefault(name, {"samples": [], "count": 0, "last": 0.0})
        rec["count"] += 1
        rec["last"] = time.time()
        if val not in rec["samples"]:
            rec["samples"].insert(0, val)
            del rec["samples"][6:]      # 样例留几个够判断，不能无界增长


def network_of(request: Any) -> str:
    """判断本次播放走 WiFi 还是流量计费网络；判不出来返回 ``unknown``。

    只依据请求里真实出现的值（含中文「流量 / 无线」），不猜键名也不猜客户端行为。
    注意：中文只可能出现在 **query**（URL 解码后是 UTF-8），不可能出现在 header
    ——HTTP 头是 latin-1，Starlette 的 Headers 装非 latin-1 值会直接 UnicodeEncodeError。
    """
    parts: list[str] = []
    for items in (_kv(getattr(request, "query_params", None)),
                  _kv(getattr(request, "headers", None))):
        for key, val in items:
            low = str(key).lower()
            if any(h in low for h in ("network", "net", "cellular", "wifi", "conn")):
                parts.append(str(val).lower())
    text = " | ".join(parts)
    if not text:
        return "unknown"
    if any(k in text for k in _CELLULAR):
        return "cellular"
    if any(k in text for k in _WIFI):
        return "wifi"
    return "unknown"


# ---------------------------------------------------------------------------
# 被动发现之二：music.db 里的偏好行
# ---------------------------------------------------------------------------


def scan_music_db(db_path: str, force: bool = False) -> dict | None:
    """在飞牛 music.db 里做 **schema 容错**的偏好扫描。

    不预设表名/列名（那是猜测）。做法是枚举所有表，只扫"像 key-value 偏好"的表
    （列数 2..8 且含 key/name/setting/code/type/id 之类的列），再看整行文本是否命中
    音质/网络语义词；命中的整行原样记进证据。只读打开（``mode=ro``），扫描失败只记
    日志，绝不影响播放。默认缓存 5 分钟，避免每次播放都扫库。
    """
    now = time.time()
    cached = _OBSERVED.get("db")
    # 缓存必须**按路径**判断：只看时间戳的话，换一个 db 路径会直接拿回上一个库的
    # 扫描结果（真机上音乐库路径变化后，报告与自动判定都会是错的且无人察觉）。
    if (cached is not None and not force
            and _OBSERVED.get("db_path") == db_path
            and now - float(_OBSERVED.get("db_scanned_at") or 0) < _DB_RESCAN_S):
        return cached
    if not db_path or not os.path.exists(db_path):
        _OBSERVED["db"] = None
        _OBSERVED["db_path"] = db_path
        _OBSERVED["db_scanned_at"] = now
        return None

    hits: list[dict[str, Any]] = []
    err = ""
    sem = ("quality", "bitrate", "network", "wifi", "cellular", "transcode",
           "音质", "流量", "原始", "标准")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                if len(hits) >= 40:
                    break
                try:
                    cols = [c[1] for c in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
                except sqlite3.Error:
                    continue
                if not 2 <= len(cols) <= 8:
                    continue
                if not any(str(c).lower() in ("key", "name", "setting", "code", "type", "id")
                           for c in cols):
                    continue
                try:
                    rows = con.execute(f'SELECT * FROM "{table}" LIMIT 400').fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    text = " ".join(str(v) for v in row if v is not None).lower()
                    if not any(h in text for h in sem):
                        continue
                    hits.append({"table": str(table), "row": _jsonable(dict(zip(cols, row)))})
                    if len(hits) >= 40:
                        break
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 扫不到就如实说明，不能影响播放
        err = f"{type(exc).__name__}: {exc}"[:200]
        logger.info("music.db quality scan unavailable: %s", err)

    result = {"scanned_at": now, "path": db_path, "hits": hits, "error": err}
    _OBSERVED["db"] = result
    _OBSERVED["db_path"] = db_path
    _OBSERVED["db_scanned_at"] = now
    return result


def _jsonable(pairs: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in pairs.items():
        if isinstance(v, (bytes, bytearray)):
            v = f"<{len(v)} bytes>"
        out[str(k)] = v
    return out


def preference_from_db(db_path: str, network: str = "wifi") -> str:
    """从 db 扫描结果里尽力解析出一个网易云档位；解析不出来返回空串（不瞎猜）。

    优先取"既提到网络类型、又提到音质"的行，否则退而取任意提到音质的行。
    """
    scan = scan_music_db(db_path)
    if not scan or not scan.get("hits"):
        return ""
    want = _CELLULAR if network == "cellular" else _WIFI

    def text_of(hit: dict) -> str:
        row = hit.get("row") or {}
        return " ".join(str(v) for v in row.values() if v is not None).lower()

    for hit in scan["hits"]:
        text = text_of(hit)
        if any(k in text for k in want):
            level = _norm_level(text)
            if level:
                return level
    for hit in scan["hits"]:
        level = _norm_level(text_of(hit))
        if level:
            return level
    return ""


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------


def resolve(request: Any = None, db_path: str = "") -> dict[str, str]:
    """决定本次该向网易云要哪一档音质。返回 ``{level, network, policy, source}``。

    ``source`` 说明这个决定**是怎么来的**——「跟随飞牛」是否真的读到了偏好必须可查证，
    否则那个策略可能是从未生效过的空话。``auto:*`` = 读到了；``fallback:*`` = 没读到。
    """
    pol = policy()
    network = network_of(request) if request is not None else "unknown"
    fallback = _default_level()

    if pol == "fixed":
        return {"level": fixed_level() or fallback, "network": network,
                "policy": pol, "source": "manual:fixed"}
    if pol == "by_network":
        on_cellular = network == "cellular"
        level = (cellular_level() if on_cellular else wifi_level()) or fallback
        return {"level": level, "network": network, "policy": pol,
                "source": f"manual:{'cellular' if on_cellular else 'wifi'}"}

    # follow_fnos：先试自动发现，读不到就回落手动值（仍按网络类型选）
    auto = preference_from_db(db_path, network) if db_path else ""
    if auto:
        return {"level": auto, "network": network, "policy": pol, "source": "auto:music_db"}
    on_cellular = network == "cellular"
    manual = (cellular_level() if on_cellular else wifi_level()) or fallback
    return {"level": manual or fallback, "network": network, "policy": pol,
            "source": "fallback:manual_or_default"}


def report(db_path: str = "") -> dict[str, Any]:
    """诊断用：策略、已发现的证据、当前判定一次给全。

    「跟随飞牛」到底生效没有，不能靠感觉——这里直接给出判定来源，以及我们实际观察到
    的线索（客户端传了什么键、db 里扫到了什么行），需要进一步适配时这就是依据。
    """
    decision = resolve(None, db_path)
    scan = _OBSERVED.get("db")
    hints = _OBSERVED["hints"]
    return {
        "policy": decision["policy"],
        "levels": {"fixed": fixed_level(), "wifi": wifi_level(),
                   "cellular": cellular_level(), "default": _default_level()},
        "current": decision,
        "observed_client_hints": {
            k: {"count": v.get("count", 0), "samples": list(v.get("samples") or [])[:3]}
            for k, v in sorted(hints.items(), key=lambda kv: -(kv[1].get("count") or 0))[:12]
        },
        "observed_paths_with_hints": dict(sorted(_OBSERVED["paths"].items(),
                                                key=lambda kv: -kv[1])[:8]),
        "db_scan": ({
            "available": True,
            "hits": len(scan.get("hits") or []),
            "sample_hits": (scan.get("hits") or [])[:5],
            "error": scan.get("error", ""),
        } if scan else {"available": False, "hits": 0,
                        "note": "music.db 不存在或未能打开"}),
    }


def reset_for_test() -> None:
    """测试钩子：清空观察记录，避免用例之间互相污染。"""
    _OBSERVED["paths"].clear()
    _OBSERVED["hints"].clear()
    _OBSERVED["db"] = None
    _OBSERVED["db_path"] = ""
    _OBSERVED["db_scanned_at"] = 0.0
