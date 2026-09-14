"""本地曲库优先播放（v2.8）：在线曲目先匹配本地音乐库，命中且音质档位
符合策略时直接读本地文件。

## 背景

播放网易云歌单里的歌，即使 NAS 上早就有同一首歌（用户自己抓轨/下载的），
原先也一定要走「取直链 → CDN 下载」的完整链路：一次 musicbox 往返 +
CDN 首字节，远程访问（移动数据）时再加一整段窄管道传输。若本地有同名
歌曲，直接读本地文件起步最快，且完全不出外网。

## 匹配规则

- 数据源：飞牛官方 ``music.db``（schema 容错扫描——不预设表名列名，与
  quality.scan_music_db 同一套哲学：猜错不会报错，只会静默失效，所以只信
  真实观察到的列）。索引按 (归一化标题) 分组、内存缓存、TTL 过期重建。
- 标题：去空白与标点后**全等**（大小写不敏感）。保守匹配，宁可不命中
  也不能放错歌。
- 艺术家：任一侧缺失视为匹配；双侧都有时**主艺术家**（第一个，按
  ``/ ; ; , &`` 切）归一化后相等才算。

## 音质档位约束（用户要求：仍按已选音质策略决定是否降码率播放）

本地文件与请求档位各归为一个「音质类」：

- lossless 类：flac / wav / ape / wv / dsf / dff / aiff / alac 文件，
  或 jymaster / hires / lossless 档位；
- lossy 类：mp3 / m4a / aac / ogg / opus / wma 等文件，
  或 exhigh / higher / standard 档位。

**仅当两边同类时才用本地文件**：

- 策略要 lossless、本地是 Hi-Res/无损 → 读本地（本地比请求只高不低，可接受）；
- 策略要 exhigh（省流量）、本地是 Hi-Res → **不用本地**，仍按策略去网易云
  要 320k——这正是「根据音质策略决定是否降码率」：不能因为在本地摸到了
  母带就把省流量的意图顶掉；
- 策略要 lossless、本地只有 320k mp3 → 不用本地（拿不到想要的质量）；
- 网易云取链失败/无权益时，若本地有匹配（不论档位类）→ 兜底读本地，
  「能播」优先于「档位精确」。

想要「只要本地有就播、不管档位」，把 ``FNMUSIC_LOCAL_FIRST_ANY_CLASS=true``。

## 索引来源（v2.9.14 修正）

v2.8~v2.9.13 只从飞牛 ``music.db`` 建索引。真机上**这个库里未必有曲目表**——
诊断里能读到的只有 ``shared_library``（一排曲库目录），根本没有
(title, artist, path) 这样的行。于是索引恒为空、本地优先一次都不会命中，
而界面上没有任何迹象，看起来就是「这功能没做」。

现在改为 **music.db + 曲库目录文件系统扫描** 双来源合并：

- 文件系统扫描复用「本地每日推荐」已验证可行的做法（目录深度/文件数上限、
  扩展名白名单），标题/艺术家从文件名 ``歌手 - 歌名.ext`` 解析，
  文件名里没有分隔符时退用**父目录名**当艺术家（``/许嵩/庐州月.flac``）；
- 两边都有时按 path 去重，music.db 的条目优先（它带真实标签）。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Any

logger = logging.getLogger("fnmusic_proxy")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def local_first_enabled() -> bool:
    return str(os.environ.get("FNMUSIC_LOCAL_FIRST", "true") or "true") \
        .strip().lower() in ("true", "1", "yes", "on")


def any_class_allowed() -> bool:
    """true = 只要本地有同名曲就播，不看音质档位是否同类。"""
    return str(os.environ.get("FNMUSIC_LOCAL_FIRST_ANY_CLASS", "false") or "false") \
        .strip().lower() in ("true", "1", "yes", "on")


def _fs_scan_max_files() -> int:
    try:
        return max(0, int(float(os.environ.get("FNMUSIC_LOCAL_FS_MAX_FILES", "20000"))))
    except (TypeError, ValueError):
        return 20000


def _fs_scan_max_depth() -> int:
    try:
        return max(0, int(float(os.environ.get("FNMUSIC_LOCAL_FS_MAX_DEPTH", "6"))))
    except (TypeError, ValueError):
        return 6


def _index_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("FNMUSIC_LOCAL_INDEX_TTL", "300") or 300))
    except (TypeError, ValueError):
        return 300.0


# ---------------------------------------------------------------------------
# 音质类
# ---------------------------------------------------------------------------

LOSSLESS_EXTS = {"flac", "wav", "ape", "wv", "dsf", "dff", "aiff", "aif", "alac"}
LOSSLESS_LEVELS = {"jymaster", "hires", "lossless"}

# 只索引这些扩展名的文件（cue/歌词/封面等一律忽略）
AUDIO_EXTS = LOSSLESS_EXTS | {"mp3", "m4a", "aac", "ogg", "opus", "wma", "tta", "tak"}


def klass_of_ext(ext: str) -> str:
    return "lossless" if str(ext or "").strip().lstrip(".").lower() in LOSSLESS_EXTS else "lossy"


def klass_of_level(level: str) -> str:
    return "lossless" if str(level or "").strip().lower() in LOSSLESS_LEVELS else "lossy"


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

_STRIP_RE = re.compile(r"[\s\-–—·・_.,，、。.!！?？:：;；'\"`~@#$%^&*()（）\[\]【】{}<>《》/\\|+…]+")
_ARTIST_SPLIT_RE = re.compile(r"\s*[/;,，、&\+]\s*")


def _norm_text(s: Any) -> str:
    return _STRIP_RE.sub("", str(s or "").strip().lower())


def norm_title(s: Any) -> str:
    return _norm_text(s)


def primary_artist(s: Any) -> str:
    parts = [p for p in _ARTIST_SPLIT_RE.split(str(s or "").strip()) if p.strip()]
    return _norm_text(parts[0]) if parts else ""


def artist_compatible(local: Any, remote: Any) -> bool:
    """艺术家是否算「同一批人」。

    比全等宽松一点，但仍是保守方向：飞牛/网易云两侧的艺术家写法差异极大
    （``许嵩 _ 何曼婷`` vs ``许嵩 / 何曼婷``、``周杰伦`` vs ``周杰伦&xxx``），
    标题已经全等了，再要求艺术家字符串逐字相同会把大量真命中挡掉——这在真机
    上的表现就是「本地明明有这首歌，却还是走了网易云」。

    规则：任一侧缺失 → 兼容；归一化后相等 → 兼容；**一侧包含另一侧** → 兼容
    （``许嵩何曼婷`` 含 ``许嵩``）。都不满足才判不兼容。
    """
    a = primary_artist(local)
    b = primary_artist(remote)
    if not a or not b:
        return True
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 2 and short in long_


# ---------------------------------------------------------------------------
# music.db 索引（schema 容错）
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict[str, tuple[float, dict[str, list[dict]]]] = {}
# 索引的来源构成（music.db 多少首 / 目录扫描多少首），诊断页要靠它回答
# 「本地优先到底有没有歌可匹配」——v2.9.13 之前这个问题完全无法查证。
_INDEX_META: dict[str, dict] = {}
# 最近若干次匹配尝试（命中/未命中 + 原因）。功能「看起来没生效」时，
# 这就是第一现场：是没索引、还是索引里有但标题没对上。
_LOOKUP_LOG: list[dict] = []
_LOOKUP_LOG_MAX = 30

_TITLE_COLS = ("title", "song_name", "name", "track_title", "songtitle")
_ARTIST_COLS = ("artist", "artists", "singer", "singers", "artist_name", "author")
_PATH_COLS = ("path", "file_path", "filepath", "url", "file", "location", "filename")


def _pick_col(cols: list[str], candidates: tuple[str, ...]) -> str | None:
    low = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in low:
            return low[cand]
    for c in cols:
        cl = c.lower()
        for cand in candidates:
            if cand in cl:
                return c
    return None


def build_index(db_path: str) -> dict[str, list[dict]]:
    """扫描 music.db，构建 {归一化标题: [条目…]}。

    任何失败都返回空索引（缓存住，TTL 后重试），绝不抛异常影响播放。
    """
    index: dict[str, list[dict]] = {}
    if not db_path or not os.path.exists(db_path):
        return index
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                try:
                    cols = [c[1] for c in con.execute(
                        f'PRAGMA table_info("{table}")').fetchall()]
                except sqlite3.Error:
                    continue
                title_col = _pick_col(cols, _TITLE_COLS)
                artist_col = _pick_col(cols, _ARTIST_COLS)
                path_col = _pick_col(cols, _PATH_COLS)
                if not (title_col and path_col):
                    continue
                try:
                    rows = con.execute(
                        f'SELECT "{title_col}", "{artist_col}", "{path_col}" '
                        f'FROM "{table}" LIMIT 200000').fetchall()
                except sqlite3.Error:
                    continue
                for title, artist, path in rows:
                    path = str(path or "").strip()
                    if not path or len(path) < 3:
                        continue
                    ext = os.path.splitext(path)[1].lstrip(".").lower()
                    if ext not in AUDIO_EXTS:
                        continue
                    key = norm_title(title)
                    if not key:
                        continue
                    index.setdefault(key, []).append({
                        "title": str(title or ""),
                        "artist": str(artist or ""),
                        "path": path,
                        "ext": ext,
                        "klass": klass_of_ext(ext),
                    })
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 索引失败不影响播放，只是不启用本地优先
        logger.info("local library index unavailable (%s): %s: %s",
                    db_path, type(exc).__name__, exc)
        return {}
    if index:
        logger.info("本地曲库索引就绪：%d 首（db=%s）", sum(len(v) for v in index.values()), db_path)
    else:
        # 索引为空 = 本地优先将永远不命中。必须留痕说明是「库没扫到东西」，
        # 否则用户只看到功能没生效、却不知道该往哪儿查（music.db 路径不对？
        # 列名没匹配上？）。真机排障时这是第一现场。
        logger.info("本地曲库索引为空（db=%s）——music.db 可能路径不对或表结构未匹配，"
                    "本地曲库优先不会生效；请把 music.db 的 .schema 发给开发者适配", db_path)
    return index


def _split_name(stem: str, fallback_artist: str = "") -> tuple[str, str]:
    """``歌手 - 歌名`` → (artist, title)；没有分隔符就整段当标题。

    真机曲库里既有 ``许嵩 - 庐州月`` 也有 ``许嵩 _ 何曼婷 - 素颜``（分隔符前后
    带空格），还有直接以父目录名当歌手的 ``/许嵩/庐州月.flac``。
    """
    for sep in (" - ", " – ", " — ", " _ ", "-"):
        if sep in stem:
            a, t = stem.split(sep, 1)
            if a.strip() and t.strip():
                return a.strip(), t.strip()
    return fallback_artist, stem.strip()


def build_fs_index(library_dir: str) -> dict[str, list[dict]]:
    """扫曲库目录建 {归一化标题: [条目…]}。

    与「本地每日推荐」的扫描同源同参数（目录深度 / 文件数 / 扩展名），那边能
    扫出歌、这边就一定能建出索引。任何异常都返回已收集到的部分，绝不抛。
    """
    index: dict[str, list[dict]] = {}
    root = str(library_dir or "").strip()
    if not root or not os.path.isdir(root):
        return index
    max_files = _fs_scan_max_files()
    max_depth = _fs_scan_max_depth()
    try:
        for base, dirs, files in os.walk(root):
            depth = os.path.relpath(base, root).count(os.sep)
            if depth > max_depth:
                dirs[:] = []
                continue
            # 目录名当兜底艺术家：/曲库/许嵩/庐州月.flac → 许嵩
            fallback = "" if depth == 0 else os.path.basename(base)
            for name in files:
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                if ext not in AUDIO_EXTS:
                    continue
                path = os.path.join(base, name)
                try:
                    if os.path.getsize(path) <= 0:
                        continue
                except OSError:
                    continue
                artist, title = _split_name(os.path.splitext(name)[0].strip(), fallback)
                key = norm_title(title)
                if not key:
                    continue
                index.setdefault(key, []).append({
                    "title": title,
                    "artist": artist,
                    "path": path,
                    "ext": ext,
                    "klass": klass_of_ext(ext),
                    "src": "fs",
                })
                if sum(len(v) for v in index.values()) >= max_files:
                    return index
    except Exception as exc:  # noqa: BLE001 - 扫不动就只用 music.db 的部分
        logger.warning("本地曲库目录扫描失败（%s）：%s: %s", root, type(exc).__name__, exc)
    return index


def _get_index(db_path: str, library_dir: str = "") -> dict[str, list[dict]]:
    key = f"{db_path}|{library_dir}" if library_dir else str(db_path)
    now = time.time()
    hit = _INDEX_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _index_ttl():
        return hit[1]

    index = build_index(db_path)
    for e in index.values():
        for item in e:
            item.setdefault("src", "db")
    fs_n = 0
    if library_dir:
        seen = {str(i.get("path") or "") for e in index.values() for i in e}
        for k, entries in build_fs_index(library_dir).items():
            for e in entries:
                if str(e.get("path") or "") in seen:
                    continue
                index.setdefault(k, []).append(e)
                fs_n += 1
    db_n = sum(1 for e in index.values() for i in e if i.get("src") == "db")
    _INDEX_META[key] = {
        "db": db_n, "fs": fs_n, "dir": library_dir,
        "ts": now, "key": key,
        "empty_reason": ("" if (db_n or fs_n) else
                         ("music.db 没有可索引的曲目表" if not library_dir else
                          "music.db 无曲目表且曲库目录未扫到音频文件")),
    }
    if db_n or fs_n:
        logger.info("本地曲库索引：music.db %d 首 + 目录扫描 %d 首（dir=%s）",
                    db_n, fs_n, library_dir or "-")
    _INDEX_CACHE[key] = (now, index)
    return index


def find_local_match(title: str, artist: str, db_path: str,
                     library_dir: str = "") -> dict | None:
    """找本地同名曲；命中返回 {path, ext, klass, …}，文件必须真实存在。

    同名多首（翻唱/伴奏等）时按「艺术家匹配 > 无损优先」排序取第一。
    ``library_dir`` 给了就把曲库目录的文件系统索引并进来一起匹配——真机的
    music.db 常常根本没有曲目表，**只靠它会永远命中不了**。
    """
    key = norm_title(title)
    if not key:
        return None
    entries = _get_index(db_path, library_dir).get(key)
    hit: dict | None = None
    reason = "title-not-in-index"
    if entries:
        def _rank(e: dict) -> tuple[int, int]:
            return (0 if artist_compatible(e.get("artist"), artist) else 1,
                    0 if e.get("klass") == "lossless" else 1)

        for e in sorted(entries, key=_rank):
            if _rank(e)[0] != 0:
                reason = "artist-mismatch"
                break
            path = str(e.get("path") or "")
            try:
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    hit = e
                    reason = "hit"
                    break
            except OSError:
                continue
            reason = "file-missing"
    _LOOKUP_LOG.append({
        "ts": time.time(), "title": str(title or ""), "artist": str(artist or ""),
        "hit": hit is not None, "reason": reason,
        "path": str((hit or {}).get("path") or ""),
    })
    del _LOOKUP_LOG[:-_LOOKUP_LOG_MAX]
    if hit is None:
        logger.debug("local-first miss (%s): title=%r artist=%r", reason, title, artist)
    return hit


def serves_request(entry: dict, level: str) -> bool:
    """本地文件的音质类是否满足请求档位。

    默认要求**同类**：策略要 lossless 就只吃本地无损；策略要 320k 就不喂本地
    母带（那会把「省流量」的意图顶掉）。``FNMUSIC_LOCAL_FIRST_ANY_CLASS=true``
    时放开——只要本地有这首就播，不看档位。
    """
    if any_class_allowed():
        return True
    return klass_of_ext(entry.get("ext") or "") == klass_of_level(level)


def status(db_path: str, library_dir: str = "") -> dict:
    """诊断页用的本地优先状态快照。

    这个模块之前最大的问题不是逻辑错，而是**无法自证**：索引空了、匹配没命中，
    谁也说不清。这里把「索引有多少首、从哪来的、最近查了什么、结果如何」
    全部摊开，功能没生效时能一眼看出卡在哪一环。
    """
    idx = _get_index(db_path, library_dir)
    meta = dict(_INDEX_META.get(
        f"{db_path}|{library_dir}" if library_dir else str(db_path), {}) or {})
    selftest: dict = {}
    for _k, entries in list(idx.items())[:1]:
        for e in entries[:1]:
            found = find_local_match(str(e.get("title") or ""),
                                     str(e.get("artist") or ""),
                                     db_path, library_dir)
            selftest = {"title": str(e.get("title") or ""),
                        "artist": str(e.get("artist") or ""),
                        "ok": found is not None,
                        "path": str((found or {}).get("path") or "")}
    return {
        "enabled": local_first_enabled(),
        "any_class": any_class_allowed(),
        "db_path": db_path,
        "library_dir": library_dir,
        "titles": len(idx),
        "entries": sum(len(v) for v in idx.values()),
        "from_db": int(meta.get("db") or 0),
        "from_fs": int(meta.get("fs") or 0),
        "built_at": float(meta.get("ts") or 0.0),
        "empty_reason": str(meta.get("empty_reason") or ""),
        "lookups": len(_LOOKUP_LOG),
        "lookup_hits": sum(1 for r in _LOOKUP_LOG if r.get("hit")),
        "recent": [dict(r) for r in _LOOKUP_LOG[-10:]],
        "selftest": selftest,
    }


def reset_for_test() -> None:
    _INDEX_CACHE.clear()
    _INDEX_META.clear()
    _LOOKUP_LOG.clear()
