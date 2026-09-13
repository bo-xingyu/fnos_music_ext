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


# ---------------------------------------------------------------------------
# music.db 索引（schema 容错）
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict[str, tuple[float, dict[str, list[dict]]]] = {}

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


def _get_index(db_path: str) -> dict[str, list[dict]]:
    now = time.time()
    hit = _INDEX_CACHE.get(db_path)
    if hit is not None and (now - hit[0]) < _index_ttl():
        return hit[1]
    index = build_index(db_path)
    _INDEX_CACHE[db_path] = (now, index)
    return index


def find_local_match(title: str, artist: str, db_path: str) -> dict | None:
    """找本地同名曲；命中返回 {path, ext, klass, …}，文件必须真实存在。

    同名多首（翻唱/伴奏等）时按「艺术家匹配 > 无损优先」排序取第一。
    """
    key = norm_title(title)
    if not key:
        return None
    entries = _get_index(db_path).get(key)
    if not entries:
        return None

    def _rank(e: dict) -> tuple[int, int]:
        la = primary_artist(e.get("artist"))
        ra = primary_artist(artist)
        artist_ok = (not la) or (not ra) or (la == ra)
        return (0 if artist_ok else 1, 0 if e.get("klass") == "lossless" else 1)

    for e in sorted(entries, key=_rank):
        if _rank(e)[0] != 0:
            break
        path = str(e.get("path") or "")
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return e
        except OSError:
            continue
    return None


def serves_request(entry: dict, level: str) -> bool:
    """本地文件的音质类是否与请求档位的音质类一致（一致才允许本地替代）。"""
    return klass_of_ext(entry.get("ext") or "") == klass_of_level(level)


def reset_for_test() -> None:
    _INDEX_CACHE.clear()
