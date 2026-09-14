"""fnmusic-ext .env 安全合并工具（防覆盖 / 平滑升级）.

install.sh 在写入 .env 前先收集“本次安装期望的配置”，再调用本模块与
已有 .env 做增量合并：

- 已存在的配置项一律保留用户现有值（密钥、自定义路径、ONLINE_SOURCES 等），
  除非该键出现在 explicit（用户本次明确提供了新值）列表中；
- 新版本引入的新配置项 / 缺失配置项自动安全补齐；
- 用户手工添加的自定义键原样保留；
- 合并结果原子写入并保持 0600 权限，合并前由调用方负责备份。

CLI:
    python3 proxy/env_merge.py --existing .env --desired desired.env \
        --output .env --explicit KEY1,KEY2 [--quiet]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# v2.0 单源化后新增的配置项：合并时自动识别并安全补齐（不覆盖用户已有值）
NEW_KEYS_COMMENT = "网易云单源 + PushPlus 提醒配置（v2.0 新增，缺失时自动补齐）"
# 与 proxy/pushplus.py 的 DEFAULT_URL 保持一致；此处不 import pushplus，
# 因为 env_merge 由 install.sh 用系统 python3 直接调用，不能依赖 httpx。
DEFAULT_PUSHPLUS_URL = "https://www.pushplus.plus/send"
NEW_DEFAULTS: "list[tuple[str, str]]" = [
    ("FNMUSIC_FREE_ONLY_ON_LOGOUT", "true"),
    ("FNMUSIC_DAILY_ENABLED", "true"),
    ("FNMUSIC_DAILY_LIMIT", "20"),
    # 本地每日推荐（v2.9）：每天从本地曲库随机抽 N 首，与网易云日推独立
    ("FNMUSIC_LOCAL_DAILY_ENABLED", "true"),
    ("FNMUSIC_LOCAL_DAILY_LIMIT", "50"),
    # 本地曲库优先（v2.8 引入 / v2.9.14 修好）与下一首预热（v2.9.14）
    ("FNMUSIC_LOCAL_FIRST", "true"),
    ("FNMUSIC_LOCAL_FIRST_ANY_CLASS", "true"),
    ("FNMUSIC_PREFETCH_NEXT", "true"),
    # --- 更多口径歌单 / 账户歌单（v2.2 新增）---
    ("FNMUSIC_NETEASE_CHANNELS", "mine,toplist,category"),
    ("FNMUSIC_NETEASE_CHANNEL_LIMIT", "8"),
    ("FNMUSIC_NETEASE_CATEGORY", "华语"),
    # 歌单大类展示顺序（v2.4）：列表里各口径的先后，管理页可改
    # ⚠️ v2.9.8：本地每日推荐默认排第一，必须与 playlists.DEFAULT_CHANNEL_ORDER、
    # admin_ui 默认值、setup.sh 四处保持完全一致，否则用户改一次配置顺序就乱。
    ("FNMUSIC_NETEASE_CHANNEL_ORDER", "localdaily,daily,mine,nrec,toplist,category,newalbum,fm"),
    # 手动歌单顺序（v2.5）：管理页逐个拖排的 token 列表；空=按大类顺序
    ("FNMUSIC_NETEASE_PLAYLIST_ORDER", ""),
    # 歌单曲目缓存（v2.6）：stale-while-revalidate + 每日定时刷新
    ("FNMUSIC_PLAYLIST_TRACK_CACHE_TTL", "21600"),
    ("FNMUSIC_PLAYLIST_REFRESH_AT", "04:30"),
    # 定时刷新/自动预热跳过「仍新鲜」缓存的阈值（秒，v2.8.2）：刚刷过的不
    # 重拉；手动预热按钮不受此限。0 = 一律刷新
    ("FNMUSIC_WARM_SKIP_FRESH_S", "3600"),
    ("FNMUSIC_PLAYLIST_TRACK_LIMIT", "300"),
    ("FNMUSIC_PLAYLIST_CACHE_DIR", ""),
    # --- 稳定性与提速（v2.7）---
    # 看门狗轮询间隔（秒）：代理死亡 / 接管丢失（官方后端重启抢走 socket）时
    # 自动恢复；0 = 关闭。2026-09-12「应用异常退出」事故的自愈手段。
    ("FNMUSIC_WATCHDOG_INTERVAL_S", "30"),
    # 播放直链短缓存（秒）：网易云 CDN 直链约有 20 分钟有效期，复用可省去
    # 重复取链的跨洋往返；0 = 关闭
    ("FNMUSIC_URL_CACHE_TTL", "600"),
    # 口径清单短缓存（秒）：歌单列表页在 TTL 内零上游往返，过期后先返回旧值
    # 并后台刷新（stale-while-revalidate）；0 = 关闭
    ("FNMUSIC_CHANNEL_LIST_CACHE_TTL", "300"),
    # --- 远程访问识别与本地曲库优先（v2.8）---
    # 公网客户端 IP（X-Forwarded-For）视同流量场景走省流档；false = 关闭。
    # 飞牛客户端从不发送网络类型键，不识别的话「流量档」永远不会触发。
    ("FNMUSIC_REMOTE_AS_CELLULAR", "true"),
    # 在线曲目先匹配本地 music.db 同名文件，命中且音质类与策略一致时直接读本地
    ("FNMUSIC_LOCAL_FIRST", "true"),
    ("FNMUSIC_LOCAL_FIRST_ANY_CLASS", "true"),
    # 本地曲库索引的内存缓存时长（秒）
    ("FNMUSIC_LOCAL_INDEX_TTL", "300"),
    # 封面缩图边长（像素）：网易云 CDN 服务端缩图（?param=NyN）。原图几百 KB
    # ~1MB，一个歌单列表首屏几十 MB 正是移动网络卡顿的主力；300px 约 20~50KB。
    # 0 = 不压缩
    ("FNMUSIC_COVER_RESIZE_PX", "300"),
    # --- 收藏归档与红心同步（v2.2 新增）---
    # 归档目录默认留空 = 关闭自动下载：这是往用户自己的磁盘写文件，
    # 绝不能替他决定写到哪儿，必须他在管理页里显式填。
    ("FNMUSIC_DOWNLOAD_DIR", ""),
    ("FNMUSIC_DOWNLOAD_ON_FAVORITE", "true"),
    # --- 音质策略（v2.3 新增）---
    ("FNMUSIC_QUALITY_POLICY", "follow_fnos"),
    ("FNMUSIC_QUALITY_FIXED", "lossless"),
    ("FNMUSIC_QUALITY_WIFI", "lossless"),
    ("FNMUSIC_QUALITY_CELLULAR", "exhigh"),
    ("FNMUSIC_QUALITY_DB_RESCAN", "300"),
    ("FNMUSIC_FAV_SYNC_LIKE", "true"),
    ("FNMUSIC_LOGIN_STATE_TTL", "300"),
    ("FNMUSIC_LOGIN_CHECK_INTERVAL", "3600"),
    ("FNMUSIC_VIP_WARN_DAYS", "7"),
    ("FNMUSIC_PUSHPLUS_ENABLED", "true"),
    ("FNMUSIC_PUSHPLUS_TOKEN", ""),
    ("FNMUSIC_PUSHPLUS_TOPIC", ""),
    ("FNMUSIC_PUSHPLUS_TEMPLATE", "markdown"),
    ("FNMUSIC_PUSHPLUS_URL", DEFAULT_PUSHPLUS_URL),
    # 日志保留策略：单文件超 MB 就地截断保留尾部，超龄文件按类型清理
    ("FNMUSIC_LOG_MAX_MB", "10"),
    ("FNMUSIC_LOG_MAX_DAYS", "30"),
    ("FNMUSIC_LOG_SCAN_INTERVAL", "3600"),
    # 在线搜索空结果的短 TTL（秒）：上游一次抖动导致结果为空时，
    # 若沿用 7 天的正常 TTL，该关键词会在整个周期内只返回本地结果。
    ("FNMUSIC_SEARCH_EMPTY_TTL", "60"),
    # musicbox 侧登录态缓存 TTL（秒）：可播性过滤每轮都要查账号信息，
    # 缓存掉可省一次跨洋往返，显著缩短搜索首屏。
    ("FNMUSIC_LOGIN_CACHE_TTL", "300"),
]
NEW_PREFIXES = ("FNMUSIC_FREE_ONLY", "FNMUSIC_DAILY", "FNMUSIC_LOCAL_DAILY", "FNMUSIC_LOGIN_",
                "FNMUSIC_VIP_", "FNMUSIC_PUSHPLUS_", "FNMUSIC_LOG_",
                "FNMUSIC_SEARCH_",
                # v2.2 新增：更多口径歌单、账户歌单、收藏归档与红心同步。
                # 加前缀只是允许这些键被自动补齐，键本身仍必须在 NEW_DEFAULTS 里列出，
                # 因此不会凭空给老用户的 .env 塞进没定义的项。
                "FNMUSIC_NETEASE_CHANNEL", "FNMUSIC_NETEASE_CATEGOR",
                "FNMUSIC_NETEASE_PLAYLIST_", "FNMUSIC_PLAYLIST_", "FNMUSIC_WARM_",
                "FNMUSIC_DOWNLOAD_", "FNMUSIC_FAV_",
                "FNMUSIC_QUALITY_", "FNMUSIC_WATCHDOG_", "FNMUSIC_URL_CACHE_",
                "FNMUSIC_CHANNEL_LIST_", "FNMUSIC_REMOTE_AS_", "FNMUSIC_LOCAL_",
                "FNMUSIC_PREFETCH_", "FNMUSIC_COVER_")

# v2.0 已废弃的配置项：升级合并时从 .env 中清理，避免残留误导。
# 只删「确定已无代码读取」的键；FNMUSIC_MODE / BASE_IMAGE / PIP_INDEX 等 docker 相关项保留。
OBSOLETE_EXACT = {
    "FNMUSIC_ONLINE_SOURCES",
    "FNMUSIC_APT_MIRROR",
    "FNMUSIC_DEPLOY_MODE",
}
OBSOLETE_PREFIXES = (
    "FNMUSIC_MUSICDL_",
    "FNMUSIC_LX_",
    "FNMUSIC_LLM_",
)


def escape_single_quoted(value: str) -> str:
    """转义 .env 单引号包裹值中的单引号（与 install.sh dotenv_escape 一致）。"""
    return value.replace("'", "'\\''")


def unquote_env_value(raw: str) -> str:
    """去掉 KEY=VALUE 行 VALUE 部分的引号并还原转义的单引号。"""
    v = raw.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        inner = v[1:-1]
        return inner.replace("'\\''", "'")
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        inner = v[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return v


def quote_env_value(value: str) -> str:
    return "'" + escape_single_quoted(value) + "'"


def parse_env_file(path: str | Path) -> "tuple[list[tuple[str, str]], list[str]]":
    """解析 .env 文件。

    返回 (kv_list, other_lines)：kv_list 为按出现顺序的 (key, value) 元组，
    other_lines 为注释/空行等非赋值行（仅记录内容，位置信息不保留）。
    """
    kv: list[tuple[str, str]] = []
    others: list[str] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return kv, others
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            kv.append((m.group(1), unquote_env_value(m.group(2))))
        elif line.strip():
            others.append(line.rstrip())
    return kv, others


def merge_env(
    existing: "list[tuple[str, str]]",
    desired: "list[tuple[str, str]]",
    explicit: "set[str] | None" = None,
) -> "tuple[list[tuple[str, str]], dict[str, list[str]]":
    """增量合并配置。

    规则：
    1. desired 中存在、existing 中不存在的键 -> 安全补齐（added）；
    2. explicit 中的键 -> 采用 desired 新值（updated，用户本次明确提供）；
    3. 其余已有键 -> 保留用户现有值（preserved）；
    4. existing 中多出的自定义键 -> 原样保留（custom_kept）。
    """
    explicit = set(explicit or ())
    existing_map = dict(existing)
    desired_map = dict(desired)
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    summary = {"added": [], "updated": [], "preserved": [], "custom_kept": []}

    for key, _val in existing:
        if key in seen:
            continue
        seen.add(key)
        if key in desired_map:
            if key in explicit:
                result.append((key, desired_map[key]))
                summary["updated"].append(key)
            else:
                result.append((key, existing_map[key]))
                summary["preserved"].append(key)
        else:
            result.append((key, existing_map[key]))
            summary["custom_kept"].append(key)

    for key, val in desired:
        if key in seen:
            continue
        seen.add(key)
        result.append((key, val))
        summary["added"].append(key)

    return result, summary


def is_obsolete_key(key: str) -> bool:
    """v2.0 起已无代码读取的配置项（musicdl / lxmusic / LLM 每日推荐等）。"""
    if key in OBSOLETE_EXACT:
        return True
    return any(key.startswith(p) for p in OBSOLETE_PREFIXES)


def drop_obsolete(kv: "list[tuple[str, str]]") -> "tuple[list[tuple[str, str]], list[str]]":
    """清理废弃配置项；返回 (保留列表, 被删除的键列表)。"""
    kept: "list[tuple[str, str]]" = []
    removed: "list[str]" = []
    for key, val in kv:
        if is_obsolete_key(key):
            removed.append(key)
            continue
        kept.append((key, val))
    return kept, removed


def ensure_prefix_defaults(
    kv: "list[tuple[str, str]]",
    defaults: "list[tuple[str, str]] | None" = None,
    prefixes: "tuple[str, ...]" = NEW_PREFIXES,
) -> "tuple[list[tuple[str, str]], list[str]]":
    """自动识别并补齐指定前缀的缺失配置项。

    已存在的键一律不动（保留用户现有值，包括空 token / false），
    仅追加缺失键；返回 (新列表, 追加的键列表)。
    """
    if defaults is None:
        defaults = NEW_DEFAULTS
    known = {k for k, _ in kv}
    out = list(kv)
    added: list[str] = []
    for key, val in defaults:
        if key in known:
            continue
        if prefixes and not key.startswith(prefixes):
            continue
        out.append((key, val))
        added.append(key)
    return out, added


def render_env(
    kv: "list[tuple[str, str]]",
    header: str = "",
    comments: "dict[str, str] | None" = None,
) -> str:
    lines = []
    if header:
        lines.append(f"# {header}")
    for key, val in kv:
        if comments and key in comments:
            lines.append(f"# {comments[key]}")
        lines.append(f"{key}={quote_env_value(val)}")
    return "\n".join(lines) + "\n"


def write_env_atomic(path: str | Path, content: str, mode: int = 0o600) -> None:
    path = Path(path)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent or "."), prefix=".env.merge.")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(path))
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_installed_version(env_path: str | Path) -> str:
    """读取现有 .env 中记录的已安装版本（FNMUSIC_VERSION）。"""
    kv, _ = parse_env_file(env_path)
    return dict(kv).get("FNMUSIC_VERSION", "")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="fnmusic-ext .env safe merge")
    parser.add_argument("--existing", required=True, help="现有 .env 路径（可不存在）")
    parser.add_argument("--desired", required=True, help="本次安装期望的 .env 内容")
    parser.add_argument("--output", required=True, help="合并结果输出路径")
    parser.add_argument("--explicit", default="", help="用户本次明确提供新值的键，逗号分隔")
    parser.add_argument("--header", default="generated by install.sh — do not commit")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    existing_kv, _others = parse_env_file(args.existing)
    desired_kv, _ = parse_env_file(args.desired)
    explicit = {k.strip() for k in args.explicit.split(",") if k.strip()}
    merged, summary = merge_env(existing_kv, desired_kv, explicit)

    # v2.0 单源化：清理已无代码读取的废弃键（musicdl / lxmusic / LLM）
    merged, obsolete_removed = drop_obsolete(merged)
    if obsolete_removed:
        summary["removed"] = obsolete_removed
        dropped = set(obsolete_removed)
        summary["custom_kept"] = [k for k in summary.get("custom_kept", []) if k not in dropped]

    # 新配置项自动识别：缺失时安全补齐
    merged, new_added = ensure_prefix_defaults(merged)
    summary["added"].extend(new_added)

    write_env_atomic(
        args.output,
        render_env(
            merged,
            args.header,
            comments={NEW_DEFAULTS[0][0]: NEW_KEYS_COMMENT},
        ),
    )
    if not args.quiet:
        for action in ("removed", "added", "updated", "preserved", "custom_kept"):
            keys = summary.get(action) or []
            if keys:
                print(f"{action}: {','.join(sorted(keys))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
