"""版本号读取与 .env 安全合并（防覆盖/平滑升级/废弃键清理）单元测试。"""
import re
import stat
from pathlib import Path

from proxy import env_merge
from proxy.version import FALLBACK_VERSION, get_version

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def _changelog_top_version() -> str:
    """CHANGELOG.md 里第一个 `## [x.y.z]` 条目的版本号。"""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        m = re.match(r"^##\s+\[([^\]]+)\]", line.strip())
        if m:
            return m.group(1).strip()
    return ""


# ---------------------------------------------------------------- version ----

def test_version_file_exists_with_semver():
    version_file = REPO_ROOT / "VERSION"
    assert version_file.is_file(), "仓库根目录必须存在 VERSION 文件"
    ver = version_file.read_text(encoding="utf-8").strip()
    assert SEMVER_RE.match(ver), f"VERSION 必须是语义化版本号，实际为 {ver!r}"


def test_get_version_reads_file(monkeypatch):
    monkeypatch.delenv("FNMUSIC_VERSION", raising=False)
    expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert get_version() == expected


def test_version_matches_changelog_head(monkeypatch):
    """VERSION 与 CHANGELOG 最新条目必须同步（防止发版只改了一处）。"""
    monkeypatch.delenv("FNMUSIC_VERSION", raising=False)
    top = _changelog_top_version()
    assert top, "CHANGELOG.md 里找不到任何版本条目"
    assert get_version() == top


def test_get_version_env_override(monkeypatch):
    monkeypatch.setenv("FNMUSIC_VERSION", "2.3.4")
    assert get_version() == "2.3.4"


def test_get_version_fallback(monkeypatch):
    monkeypatch.delenv("FNMUSIC_VERSION", raising=False)
    import proxy.version as v

    monkeypatch.setattr(v, "_read_version_file", lambda p: "")
    assert v.get_version() == FALLBACK_VERSION


# ------------------------------------------------------------- env parsing ---

def test_parse_and_roundtrip_quoting(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n"
        "A='hello'\n"
        'B="world"\n'
        "C=bare\n"
        "D='it'\\''s'\n"
        "export E='exported'\n",
        encoding="utf-8",
    )
    kv, others = env_merge.parse_env_file(p)
    assert kv == [
        ("A", "hello"),
        ("B", "world"),
        ("C", "bare"),
        ("D", "it's"),
        ("E", "exported"),
    ]
    assert others == ["# comment"]
    rendered = env_merge.render_env(kv)
    out = tmp_path / "roundtrip.env"
    out.write_text(rendered, encoding="utf-8")
    kv3, _ = env_merge.parse_env_file(out)
    assert kv3 == kv


# ---------------------------------------------------------------- merging ----

# 一个「从 v1.x 升级上来」的旧 .env：含已废弃的多音源/LLM 键、用户自定义值
EXISTING = [
    ("FNMUSIC_HOME", "/custom/home"),
    ("FNMUSIC_MUSICDL_ENABLED", "true"),
    ("FNMUSIC_MUSICDL_URL", "http://127.0.0.1:8768"),
    ("FNMUSIC_LX_ENABLED", "true"),
    ("FNMUSIC_LX_URL", "http://127.0.0.1:8772"),
    ("FNMUSIC_ONLINE_SOURCES", "UserCustomClient"),
    ("FNMUSIC_LLM_API_KEY", "sk-user-secret"),
    ("FNMUSIC_LLM_BASE_URL", "https://user.example.com/v1"),
    ("FNMUSIC_LLM_MODEL", "deepseek-chat"),
    ("FNMUSIC_APT_MIRROR", "https://mirrors.tuna.tsinghua.edu.cn"),
    ("FNMUSIC_DEPLOY_MODE", "docker"),
    ("FNMUSIC_MUSICBOX_URL", "http://nas:9999"),
    ("FNMUSIC_PUSHPLUS_TOKEN", "old-push-token"),
    ("FNMUSIC_NETEASE_QUALITY", "exhigh"),
    ("FNMUSIC_MY_EXTRA", "keep-me"),
]

DESIRED = [
    ("FNMUSIC_HOME", "/default/home"),
    ("FNMUSIC_CACHE_DIR", "/default/home/cache"),
    ("FNMUSIC_NETEASE_ENABLED", "false"),
    ("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"),
    ("FNMUSIC_VERSION", "2.0.0"),
]


def test_merge_preserves_user_config_on_upgrade():
    merged, summary = env_merge.merge_env(EXISTING, DESIRED, explicit={"FNMUSIC_VERSION"})
    m = dict(merged)
    # 用户自定义路径 / 自定义 token / 音质偏好必须原样保留
    assert m["FNMUSIC_HOME"] == "/custom/home"
    assert m["FNMUSIC_MUSICBOX_URL"] == "http://nas:9999"
    assert m["FNMUSIC_PUSHPLUS_TOKEN"] == "old-push-token"
    assert m["FNMUSIC_NETEASE_QUALITY"] == "exhigh"
    assert m["FNMUSIC_MY_EXTRA"] == "keep-me"
    # 新增配置项被补齐
    assert m["FNMUSIC_CACHE_DIR"] == "/default/home/cache"
    assert m["FNMUSIC_VERSION"] == "2.0.0"
    assert "FNMUSIC_CACHE_DIR" in summary["added"]
    # DESIRED 里没有的既有键：自定义键归 custom_kept，废弃键也先原样保留，
    # 真正的清理发生在 CLI 的 drop_obsolete 阶段（见下方专项用例）
    assert "FNMUSIC_MY_EXTRA" in summary["custom_kept"]
    assert "FNMUSIC_PUSHPLUS_TOKEN" in summary["custom_kept"]
    # DESIRED 与 EXISTING 都有的键，用户值胜出并计入 preserved
    assert "FNMUSIC_MUSICBOX_URL" in summary["preserved"]
    assert "FNMUSIC_HOME" in summary["preserved"]


def test_merge_explicit_override_only_when_user_provides():
    merged, _ = env_merge.merge_env(
        EXISTING, DESIRED, explicit={"FNMUSIC_VERSION", "FNMUSIC_MUSICBOX_URL"}
    )
    m = dict(merged)
    assert m["FNMUSIC_MUSICBOX_URL"] == "http://127.0.0.1:8770"  # 用户明确提供新值才覆盖
    assert m["FNMUSIC_HOME"] == "/custom/home"


def test_merge_same_version_reinstall_keeps_everything():
    existing = EXISTING + [("FNMUSIC_VERSION", "2.0.0")]
    merged, _ = env_merge.merge_env(existing, DESIRED, explicit={"FNMUSIC_VERSION"})
    m = dict(merged)
    assert m["FNMUSIC_VERSION"] == "2.0.0"
    assert m["FNMUSIC_PUSHPLUS_TOKEN"] == "old-push-token"
    assert m["FNMUSIC_HOME"] == "/custom/home"


def test_write_env_atomic_permissions_and_content(tmp_path):
    out = tmp_path / ".env"
    env_merge.write_env_atomic(out, env_merge.render_env(DESIRED))
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    kv, _ = env_merge.parse_env_file(out)
    assert dict(kv)["FNMUSIC_VERSION"] == "2.0.0"


def test_read_installed_version(tmp_path):
    p = tmp_path / ".env"
    env_merge.write_env_atomic(p, env_merge.render_env(DESIRED))
    assert env_merge.read_installed_version(p) == "2.0.0"
    assert env_merge.read_installed_version(tmp_path / "missing.env") == ""


# ------------------------------------------------------- obsolete key drops ---

OBSOLETE_KEYS = [
    "FNMUSIC_MUSICDL_ENABLED",
    "FNMUSIC_MUSICDL_URL",
    "FNMUSIC_LX_ENABLED",
    "FNMUSIC_LX_URL",
    "FNMUSIC_LLM_API_KEY",
    "FNMUSIC_LLM_BASE_URL",
    "FNMUSIC_LLM_MODEL",
    "FNMUSIC_ONLINE_SOURCES",
    "FNMUSIC_APT_MIRROR",
    "FNMUSIC_DEPLOY_MODE",
]


def test_is_obsolete_key():
    for k in OBSOLETE_KEYS:
        assert env_merge.is_obsolete_key(k), f"{k} 应被识别为废弃键"
    # 仍在使用的键绝不能被误判为废弃
    for k in (
        "FNMUSIC_NETEASE_ENABLED",
        "FNMUSIC_MUSICBOX_URL",
        "FNMUSIC_PUSHPLUS_TOKEN",
        "FNMUSIC_MODE",
        "FNMUSIC_BASE_IMAGE",
        "FNMUSIC_PIP_INDEX",
        "FNMUSIC_DOCKER_MIRRORS",
        "FNMUSIC_FREE_ONLY_ON_LOGOUT",
        "FNMUSIC_MY_EXTRA",
    ):
        assert not env_merge.is_obsolete_key(k), f"{k} 不应被判定为废弃"


def test_drop_obsolete_removes_legacy_and_keeps_custom():
    kept, removed = env_merge.drop_obsolete(EXISTING)
    m = dict(kept)
    assert set(removed) == {k for k in OBSOLETE_KEYS if k in dict(EXISTING)}
    for k in removed:
        assert k not in m
    # 非废弃键全部原样保留，顺序也不被打乱
    assert m["FNMUSIC_HOME"] == "/custom/home"
    assert m["FNMUSIC_MY_EXTRA"] == "keep-me"
    assert m["FNMUSIC_MUSICBOX_URL"] == "http://nas:9999"
    assert len(kept) == len(EXISTING) - len(removed)


# ------------------------------------------------------------ new key merge ---

def test_ensure_prefix_defaults_adds_missing_new_keys():
    kv, added = env_merge.ensure_prefix_defaults([("FNMUSIC_HOME", "/x")])
    m = dict(kv)
    # 单源 + 降级 + 日推 + 推送四组新键都要补齐
    for key in (
        "FNMUSIC_FREE_ONLY_ON_LOGOUT",
        "FNMUSIC_DAILY_ENABLED",
        "FNMUSIC_DAILY_LIMIT",
        "FNMUSIC_PUSHPLUS_ENABLED",
        "FNMUSIC_PUSHPLUS_TOKEN",
        "FNMUSIC_PUSHPLUS_URL",
        "FNMUSIC_LOGIN_STATE_TTL",
        "FNMUSIC_VIP_WARN_DAYS",
    ):
        assert key in m, f"{key} 应被自动补齐"
    for key, val in env_merge.NEW_DEFAULTS:
        assert m[key] == val
    assert added == [k for k, _ in env_merge.NEW_DEFAULTS]


def test_ensure_prefix_defaults_preserves_existing_values():
    """用户已填的 token 与已关掉的开关绝不能被默认值覆盖。"""
    existing = [
        ("FNMUSIC_PUSHPLUS_TOKEN", "my-real-token"),
        ("FNMUSIC_FREE_ONLY_ON_LOGOUT", "false"),
        ("FNMUSIC_DAILY_ENABLED", "false"),
        ("FNMUSIC_DAILY_LIMIT", "5"),
    ]
    kv, added = env_merge.ensure_prefix_defaults(existing)
    m = dict(kv)
    assert added == [
        k for k, _ in env_merge.NEW_DEFAULTS if k not in dict(existing)
    ]
    assert m["FNMUSIC_PUSHPLUS_TOKEN"] == "my-real-token"
    assert m["FNMUSIC_FREE_ONLY_ON_LOGOUT"] == "false"
    assert m["FNMUSIC_DAILY_ENABLED"] == "false"
    assert m["FNMUSIC_DAILY_LIMIT"] == "5"


def test_every_new_default_matches_a_prefix():
    """不变量：NEW_DEFAULTS 里的每个键都必须能被 NEW_PREFIXES 匹配，
    否则升级时它永远不会被补齐——这个坑已经踩过一次（FNMUSIC_SEARCH_EMPTY_TTL）。"""
    missing = [
        k for k, _ in env_merge.NEW_DEFAULTS
        if not any(k.startswith(pfx) for pfx in env_merge.NEW_PREFIXES)
    ]
    assert not missing, f"以下新键没有对应前缀，升级时不会被补齐：{missing}"


def test_new_defaults_are_actually_backfilled():
    """端到端确认：从一个只有 HOME 的 .env 出发，所有新键都会被补齐。"""
    kv, added = env_merge.ensure_prefix_defaults([("FNMUSIC_HOME", "/x")])
    got = dict(kv)
    for key, val in env_merge.NEW_DEFAULTS:
        assert got.get(key) == val, f"{key} 未被补齐（期望 {val!r}，实际 {got.get(key)!r}）"
    assert len(added) == len(env_merge.NEW_DEFAULTS)


def test_ensure_prefix_defaults_does_not_invent_unrelated_keys():
    """只补 NEW_DEFAULTS 里、且前缀匹配的键。"""
    kv, added = env_merge.ensure_prefix_defaults(
        [("FNMUSIC_HOME", "/x")],
        defaults=[("FNMUSIC_UNRELATED", "1"), ("OTHER_PREFIX_KEY", "2")],
        prefixes=("FNMUSIC_UNRELATED",),
    )
    assert added == ["FNMUSIC_UNRELATED"]
    assert "OTHER_PREFIX_KEY" not in dict(kv)


def test_render_env_includes_new_keys_comment():
    text = env_merge.render_env(
        env_merge.NEW_DEFAULTS,
        comments={env_merge.NEW_DEFAULTS[0][0]: env_merge.NEW_KEYS_COMMENT},
    )
    assert env_merge.NEW_KEYS_COMMENT in text
    # render -> parse 往返，验证注释不干扰解析
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write(text)
        path = f.name
    kv2, _ = env_merge.parse_env_file(Path(path))
    m = dict(kv2)
    assert m["FNMUSIC_PUSHPLUS_URL"] == env_merge.DEFAULT_PUSHPLUS_URL
    assert m["FNMUSIC_FREE_ONLY_ON_LOGOUT"] == "true"


def test_cli_merge_adds_new_and_drops_obsolete(tmp_path):
    """CLI 端到端：补齐新键 + 清理废弃键 + 保留用户值。"""
    existing = tmp_path / ".env"
    existing.write_text(
        "FNMUSIC_FREE_ONLY_ON_LOGOUT='false'\n"
        "FNMUSIC_MUSICDL_ENABLED='true'\n"
        "FNMUSIC_LX_URL='http://127.0.0.1:8772'\n"
        "FNMUSIC_LLM_API_KEY='sk-old'\n"
        "FNMUSIC_HOME='/custom'\n"
        "FNMUSIC_MY_EXTRA='x'\n",
        encoding="utf-8",
    )
    desired = tmp_path / "desired.env"
    env_merge.write_env_atomic(desired, env_merge.render_env(DESIRED))
    rc = env_merge.main(
        [
            "--existing", str(existing),
            "--desired", str(desired),
            "--output", str(existing),
            "--explicit", "FNMUSIC_VERSION",
        ]
    )
    assert rc == 0
    text = existing.read_text(encoding="utf-8")
    m = dict(env_merge.parse_env_file(existing)[0])

    # 用户已显式关闭降级 -> 不被默认值覆盖
    assert m["FNMUSIC_FREE_ONLY_ON_LOGOUT"] == "false"
    # 缺失的新键被补齐，且带注释
    assert m["FNMUSIC_DAILY_ENABLED"] == "true"
    assert m["FNMUSIC_PUSHPLUS_TOKEN"] == ""
    assert env_merge.NEW_KEYS_COMMENT in text
    # 废弃键被彻底清理
    for k in ("FNMUSIC_MUSICDL_ENABLED", "FNMUSIC_LX_URL", "FNMUSIC_LLM_API_KEY"):
        assert k not in m, f"{k} 应在升级合并时被清理"
    # 用户自定义键与自定义值保留
    assert m["FNMUSIC_HOME"] == "/custom"
    assert m["FNMUSIC_MY_EXTRA"] == "x"


def test_cli_end_to_end_merge(tmp_path, capsys):
    existing = tmp_path / ".env"
    existing.write_text(
        "FNMUSIC_PUSHPLUS_TOKEN='sk-old'\n"
        "FNMUSIC_HOME='/custom'\n"
        "FNMUSIC_MY_EXTRA='x'\n",
        encoding="utf-8",
    )
    desired = tmp_path / "desired.env"
    env_merge.write_env_atomic(desired, env_merge.render_env(DESIRED))
    rc = env_merge.main(
        [
            "--existing", str(existing),
            "--desired", str(desired),
            "--output", str(existing),
            "--explicit", "FNMUSIC_VERSION",
        ]
    )
    assert rc == 0
    m = dict(env_merge.parse_env_file(existing)[0])
    assert m["FNMUSIC_PUSHPLUS_TOKEN"] == "sk-old"
    assert m["FNMUSIC_HOME"] == "/custom"
    assert m["FNMUSIC_MY_EXTRA"] == "x"
    assert m["FNMUSIC_CACHE_DIR"] == "/default/home/cache"
    assert m["FNMUSIC_VERSION"] == "2.0.0"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    summary = capsys.readouterr().err
    assert "preserved" in summary and "added" in summary


def test_cli_summary_does_not_report_removed_keys_as_custom_kept(tmp_path, capsys):
    """被清理的废弃键不应再出现在 custom_kept 汇总里（否则日志自相矛盾）。"""
    existing = tmp_path / ".env"
    existing.write_text(
        "FNMUSIC_MUSICDL_ENABLED='true'\nFNMUSIC_MY_EXTRA='x'\n", encoding="utf-8"
    )
    desired = tmp_path / "desired.env"
    env_merge.write_env_atomic(desired, env_merge.render_env(DESIRED))
    rc = env_merge.main(
        [
            "--existing", str(existing),
            "--desired", str(desired),
            "--output", str(existing),
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "removed" in err and "FNMUSIC_MUSICDL_ENABLED" in err
    custom_line = [ln for ln in err.splitlines() if ln.startswith("custom_kept:")]
    if custom_line:
        assert "FNMUSIC_MUSICDL_ENABLED" not in custom_line[0]
        assert "FNMUSIC_MY_EXTRA" in custom_line[0]


# ---------------------------------------------------------------------------
# 打包清单完整性（真实事故防护）
#
# build_fpk.sh 用的是**显式文件清单**（sync_into_app 逐个列出 + 自检再列一遍）来
# 决定哪些文件进 payload。2.2.0 新增 proxy/playlists.py 与 proxy/download.py 时忘了
# 登记，包能构建成功、自检也"全部通过"，但 payload 里没有这两个模块 —— 装上去
# import 就直接 ImportError。校验 payload 时才发现。
# 这类"新文件忘了登记"的错不会有任何提示，只能用测试钉住。
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def test_every_proxy_module_is_in_fpk_manifest():
    """proxy/ 下每个运行期 .py 都必须被 sync_into_app 登记，否则不会被打包。"""
    root = _repo_root()
    script = (root / "build_fpk.sh").read_text(encoding="utf-8")
    modules = sorted(
        p.name for p in (root / "proxy").glob("*.py")
        if p.name != "__init__.py"          # __init__.py 单独登记
    )
    missing = [m for m in modules if f'proxy/{m}' not in script]
    assert not missing, (
        f"这些模块没被 build_fpk.sh 登记，装上去会 ImportError: {missing}"
    )


def test_fpk_payload_selfcheck_covers_every_proxy_module():
    """自检那段清单也必须覆盖，否则"结构/载荷校验通过"是假的。"""
    root = _repo_root()
    script = (root / "build_fpk.sh").read_text(encoding="utf-8")
    modules = sorted(
        p.name for p in (root / "proxy").glob("*.py")
        if p.name not in ("__init__.py", "version.py")
    )
    # 自检清单里既可能写 proxy/x.py 也可能只写文件名，这里两种都接受
    missing = [m for m in modules if m not in script]
    assert not missing, f"payload 自检清单漏了: {missing}"
