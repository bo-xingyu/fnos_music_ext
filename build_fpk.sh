#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext → 飞牛 fnOS 应用包 (.fpk) 构建脚本
#
# 产物结构依据官方 fnpack 1.2.3 的实际打包产物逆向确认（官方文档未记载容器格式）：
#
#   <appname>.fpk  (根级 tar.gz)
#   ├── app.tgz        app/ 目录【内容】 + config/ 副本（无 app/ 前缀层级）
#   ├── cmd/           9 个生命周期脚本
#   ├── config/        privilege + resource（合法 JSON）
#   ├── wizard/        install / config / uninstall（JSON 数组）
#   ├── manifest       key = value 文本；checksum = MD5(app.tgz) 由本脚本回填
#   ├── ICON.PNG       64x64
#   └── ICON_256.PNG   256x256
#
# 无签名、无加密；唯一完整性校验是 manifest 里的 checksum（MD5 of app.tgz）。
#
# 用法：
#   ./build_fpk.sh                     # 版本取仓库根 VERSION，产物进 dist/
#   ./build_fpk.sh --version 2.0.1
#   ./build_fpk.sh --outdir /tmp/out --keep
#   ./build_fpk.sh --fnpack            # 若探测到官方 fnpack 则用它打包（交叉验证）
#
# 中间产物 fpk/app/ 与 dist/ 不入库（见 .gitignore）。
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FPK_DIR="${BASE_DIR}/fpk"

APPNAME="fnmusicext"
VERSION=""
OUTDIR="${BASE_DIR}/dist"
KEEP=0
USE_FNPACK=0
FNPACK_BIN="${FNPACK_BIN:-fnpack}"
SKIP_ICONS=0

log_info() { printf '\033[32m[INFO]\033[0m %s\n' "$*"; }
log_warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*" >&2; }
log_err()  { printf '\033[31m[ERROR]\033[0m %s\n' "$*" >&2; }

usage() {
    cat <<USAGE
用法: $0 [选项]

  --appname NAME     应用唯一标识（默认 ${APPNAME}，纯字母数字最稳妥）
  --version X.Y.Z    包版本；默认读取仓库根 VERSION 文件
  --outdir DIR       产物输出目录（默认 dist/）
  --keep             保留中间构建目录（调试用）
  --fnpack           若探测到官方 fnpack 二进制则用它构建，并与内置实现交叉比对
  --skip-icons       跳过图标生成（复用已存在的 ICON.PNG / ICON_256.PNG）
  -h, --help         显示本帮助

环境依赖：bash、tar、gzip、md5sum、python3（图标生成需 Pillow，可用 --skip-icons 跳过）
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --appname) APPNAME="${2:?--appname 需要参数}"; shift 2 ;;
        --appname=*) APPNAME="${1#*=}"; shift ;;
        --version) VERSION="${2:?--version 需要参数}"; shift 2 ;;
        --version=*) VERSION="${1#*=}"; shift ;;
        --outdir) OUTDIR="${2:?--outdir 需要参数}"; shift 2 ;;
        --outdir=*) OUTDIR="${1#*=}"; shift ;;
        --keep) KEEP=1; shift ;;
        --fnpack) USE_FNPACK=1; shift ;;
        --skip-icons) SKIP_ICONS=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) log_err "未知参数: $1"; usage; exit 1 ;;
    esac
done

# ------------------------------------------------------------------------------
# 0. 版本与前置检查
# ------------------------------------------------------------------------------
if [ -z "${VERSION}" ]; then
    if [ -f "${BASE_DIR}/VERSION" ]; then
        VERSION="$(head -n 1 "${BASE_DIR}/VERSION" | tr -d '[:space:]')"
    fi
fi
if [ -z "${VERSION}" ]; then
    log_err "无法确定版本号：请用 --version 指定，或确保仓库根存在 VERSION 文件"
    exit 1
fi
if ! printf '%s' "${VERSION}" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$'; then
    log_err "版本号 '${VERSION}' 不是合法的语义化版本（形如 2.0.0 / 2.1.3-beta）"
    exit 1
fi

for tool in tar gzip md5sum python3; do
    command -v "${tool}" >/dev/null 2>&1 || { log_err "缺少必备命令: ${tool}"; exit 1; }
done

FPK_NAME="${APPNAME}-${VERSION}.fpk"
log_info "构建 ${FPK_NAME} (appname=${APPNAME}, version=${VERSION})"

# ------------------------------------------------------------------------------
# 1. 图标：缺失时自动生成（官方禁止占位图，所以必须真画一个）
# ------------------------------------------------------------------------------
if [ "${SKIP_ICONS}" -eq 1 ]; then
    log_info "--skip-icons：复用现有图标"
else
    if [ -x "${FPK_DIR}/tools/make_icons.py" ] || [ -f "${FPK_DIR}/tools/make_icons.py" ]; then
        if python3 "${FPK_DIR}/tools/make_icons.py" --out "${FPK_DIR}" >/dev/null 2>&1; then
            log_info "图标已生成/更新"
        else
            log_warn "图标生成失败（可能缺 Pillow）。若已存在图标则继续使用；否则请先 python3 -m pip install pillow"
        fi
    fi
fi
for icon in ICON.PNG ICON_256.PNG; do
    [ -f "${FPK_DIR}/${icon}" ] || { log_err "缺少图标 ${FPK_DIR}/${icon}（去掉 --skip-icons 或安装 Pillow 后重试）"; exit 1; }
done

# ------------------------------------------------------------------------------
# 2. 组装构建目录：manifest / config / cmd / wizard / app
# ------------------------------------------------------------------------------
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/fnmusic-fpk.XXXXXX")"
STAGE="${BUILD_ROOT}/pkg"
APP_SRC="${STAGE}/app"
cleanup() {
    if [ "${KEEP}" -eq 1 ]; then
        log_info "--keep：保留中间目录 ${BUILD_ROOT}"
    else
        rm -rf "${BUILD_ROOT}"
    fi
    rm -rf "${FPK_DIR}/app" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "${STAGE}/cmd" "${STAGE}/config" "${STAGE}/wizard" "${APP_SRC}"

log_info "组装生命周期脚本 cmd/ ..."
for script in install_init install_callback main upgrade_init upgrade_callback \
              uninstall_init uninstall_callback config_init config_callback; do
    src="${FPK_DIR}/cmd/${script}"
    [ -f "${src}" ] || { log_err "缺少生命周期脚本 ${src}"; exit 1; }
    bash -n "${src}" || { log_err "cmd/${script} 语法检查失败"; exit 1; }
    cp -a "${src}" "${STAGE}/cmd/${script}"
done

log_info "组装 config/ 与 wizard/ ..."
cp -a "${FPK_DIR}/config/privilege" "${STAGE}/config/privilege"
cp -a "${FPK_DIR}/config/resource"  "${STAGE}/config/resource"
for wiz in install config uninstall; do
    [ -f "${FPK_DIR}/wizard/${wiz}" ] || { log_err "缺少向导 ${FPK_DIR}/wizard/${wiz}"; exit 1; }
    cp -a "${FPK_DIR}/wizard/${wiz}" "${STAGE}/wizard/${wiz}"
done

# ---- app/ 载荷同步清单 ----
# 只同步 fpk 运行时真正需要的东西。docker-compose.yml / ensure_base_image.sh /
# install.sh / extend.sh / fnmusic-ext.service / docs / tests 都属于 git 安装
# 路径，fpk 走 cmd/* 生命周期，不该带进包里徒增体积与审核困惑。
log_info "同步 app/ 载荷 ..."
sync_into_app() {
    local rel="$1"
    local src="${BASE_DIR}/${rel}" dst="${APP_SRC}/${rel}"
    if [ -d "${src}" ]; then
        mkdir -p "${dst}"
        # -a 保留权限；排除项统一交给 prune 处理
        cp -a "${src}/." "${dst}/"
    elif [ -f "${src}" ]; then
        mkdir -p "$(dirname "${dst}")"
        cp -a "${src}" "${dst}"
    else
        log_err "载荷缺失: ${src}"
        exit 1
    fi
}

sync_into_app "proxy/app.py"
sync_into_app "proxy/recommend.py"
sync_into_app "proxy/netease_auth.py"
sync_into_app "proxy/pushplus.py"
sync_into_app "proxy/netease_items.py"
sync_into_app "proxy/extra_items.py"
sync_into_app "proxy/extra_sources.py"
sync_into_app "proxy/local_library.py"
sync_into_app "proxy/playlists.py"
sync_into_app "proxy/download.py"
sync_into_app "proxy/quality.py"
sync_into_app "proxy/admin_ui.py"
sync_into_app "proxy/loghouse.py"
sync_into_app "proxy/env_merge.py"
sync_into_app "proxy/version.py"
sync_into_app "proxy/trimgw.py"
sync_into_app "proxy/local_files.py"
sync_into_app "proxy/prefetch.py"
sync_into_app "proxy/__init__.py"
sync_into_app "proxy/run_proxy.sh"
sync_into_app "proxy/requirements.txt"
sync_into_app "musicbox-service/app.py"
sync_into_app "musicbox-service/runner.py"
sync_into_app "musicbox-service/netease_ext.py"
sync_into_app "musicbox-service/requirements.txt"
sync_into_app "musicbox-service/.dockerignore"
sync_into_app "restore.sh"
sync_into_app "netease_login.sh"
sync_into_app "VERSION"
# fpk 专属胶水层 + 桌面入口（ui/config 与 ui/images 由 payload/ 一起带进来）
cp -a "${FPK_DIR}/payload/." "${APP_SRC}/"

# 声明 desktop_uidir 时，官方 fnpack 要求 app/{desktop_uidir}/ 必须存在
DESKTOP_UIDIR="$(sed -n 's/^[[:space:]]*desktop_uidir[[:space:]]*=[[:space:]]*//p' "${FPK_DIR}/manifest" | tr -d '[:space:]')"
if [ -n "${DESKTOP_UIDIR}" ]; then
    [ -d "${APP_SRC}/${DESKTOP_UIDIR}" ] || { log_err "manifest 声明了 desktop_uidir=${DESKTOP_UIDIR}，但 app/${DESKTOP_UIDIR}/ 不存在"; exit 1; }
    [ -f "${APP_SRC}/${DESKTOP_UIDIR}/config" ] || { log_err "缺少桌面入口配置 app/${DESKTOP_UIDIR}/config"; exit 1; }
    log_info "桌面入口: app/${DESKTOP_UIDIR}/config 已就位"
fi

# 清理一切不该进包的东西
log_info "清理载荷中的构建垃圾 ..."
find "${APP_SRC}" \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.venv*' \
                    -o -name 'tests' -o -name '.mypy_cache' -o -name '.ruff_cache' \) \
     -prune -exec rm -rf {} + 2>/dev/null || true
find "${APP_SRC}" \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \
                    -o -name '*.part' -o -name '.env' -o -name '.env.*' \) \
     -type f -delete 2>/dev/null || true
# 运行时目录占位：install_callback 会 mkdir，这里预建空目录避免首次竞态
mkdir -p "${APP_SRC}/cache" "${APP_SRC}/musicbox-data"
# 绝不允许任何密钥进包：.env 已在上面删除，这里再做一次断言
SECRETS="$(find "${STAGE}" \( -name '.env' -o -name '*.pem' -o -name 'id_rsa*' \) -print 2>/dev/null || true)"
if [ -n "${SECRETS}" ]; then
    log_err "构建目录中检测到疑似密钥文件，已中止打包："
    printf '%s\n' "${SECRETS}"
    exit 1
fi

# ------------------------------------------------------------------------------
# 3. manifest：版本/appname 覆盖 + checksum 回填
# ------------------------------------------------------------------------------
log_info "生成 manifest ..."
[ -f "${FPK_DIR}/manifest" ] || { log_err "缺少 ${FPK_DIR}/manifest"; exit 1; }
# 去掉源 manifest 中已有的 checksum 行（由本脚本回填），并覆盖 appname/version
grep -v '^[[:space:]]*checksum[[:space:]]*=' "${FPK_DIR}/manifest" \
    | sed -E "s/^appname[[:space:]]*=.*/appname               = ${APPNAME}/" \
    | sed -E "s/^version[[:space:]]*=.*/version               = ${VERSION}/" \
    > "${STAGE}/manifest"

for field in appname version display_name desc source platform maintainer; do
    grep -Eq "^${field}[[:space:]]*=" "${STAGE}/manifest" \
        || { log_err "manifest 缺少必填字段: ${field}"; exit 1; }
done

# ------------------------------------------------------------------------------
# 4. app.tgz（= app/ 内容 + config/ 副本），随后 checksum 回填 manifest
# ------------------------------------------------------------------------------
log_info "打包 app.tgz ..."
cp -a "${STAGE}/config" "${APP_SRC}/config"
# --sort/--mtime/--owner 让产物可复现（同输入同哈希），便于 CI 校验
tar --sort=name --mtime="@1700000000" --owner=0 --group=0 --numeric-owner \
    -C "${APP_SRC}" -czf "${STAGE}/app.tgz" . 2>/dev/null \
  || tar -C "${APP_SRC}" -czf "${STAGE}/app.tgz" .
CHECKSUM="$(md5sum "${STAGE}/app.tgz" | cut -d' ' -f1)"
printf 'checksum              = %s\n' "${CHECKSUM}" >> "${STAGE}/manifest"
log_info "app.tgz checksum (MD5) = ${CHECKSUM}"

# ------------------------------------------------------------------------------
# 5. 顶层 fpk（根级 tar.gz）
# ------------------------------------------------------------------------------
mkdir -p "${OUTDIR}"
FPK_PATH="${OUTDIR}/${FPK_NAME}"
rm -f "${FPK_PATH}"
cp -a "${FPK_DIR}/ICON.PNG" "${FPK_DIR}/ICON_256.PNG" "${STAGE}/"
log_info "打包 ${FPK_PATH} ..."
tar --sort=name --mtime="@1700000000" --owner=0 --group=0 --numeric-owner \
    -C "${STAGE}" -czf "${FPK_PATH}" \
    app.tgz cmd config wizard manifest ICON.PNG ICON_256.PNG 2>/dev/null \
  || tar -C "${STAGE}" -czf "${FPK_PATH}" \
    app.tgz cmd config wizard manifest ICON.PNG ICON_256.PNG

log_info "产物: ${FPK_PATH} ($(du -h "${FPK_PATH}" | cut -f1))"

# ------------------------------------------------------------------------------
# 6. 自检：解包逐项核对，任一不符立即失败
# ------------------------------------------------------------------------------
log_info "自检构建产物 ..."
VERIFY="${BUILD_ROOT}/verify"
mkdir -p "${VERIFY}"
tar xzf "${FPK_PATH}" -C "${VERIFY}"

fail_verify() { log_err "自检失败: $*"; exit 1; }

for required in app.tgz cmd config wizard manifest ICON.PNG ICON_256.PNG; do
    [ -e "${VERIFY}/${required}" ] || fail_verify "fpk 根缺少 ${required}"
done

# 6.1 生命周期脚本齐全
for script in install_init install_callback main upgrade_init upgrade_callback \
              uninstall_init uninstall_callback config_init config_callback; do
    [ -f "${VERIFY}/cmd/${script}" ] || fail_verify "cmd/ 缺少 ${script}"
    bash -n "${VERIFY}/cmd/${script}" || fail_verify "cmd/${script} 语法错误"
done

# 6.2 JSON 合法性
python3 - "$VERIFY" <<'PYEOF' || fail_verify "config/wizard JSON 校验未通过"
import json, os, sys
root = sys.argv[1]
for rel in ("config/privilege", "config/resource"):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        json.load(f)
for rel in ("wizard/install", "wizard/config", "wizard/uninstall"):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list) and data, f"{rel} 必须是非空 JSON 数组"
    for step in data:
        assert isinstance(step, dict), f"{rel} 步骤必须是对象"
        assert "items" in step, f"{rel} 步骤缺少 items"
        for item in step["items"]:
            assert item.get("type"), f"{rel} 存在没有 type 的表单项"
            if item["type"] != "tips":
                assert item.get("field"), f"{rel} 存在没有 field 的表单项"
                assert not item["field"].startswith("TRIM_"), "禁止使用 TRIM_ 前缀字段"
print("config/wizard JSON 校验通过")
PYEOF

# 6.3 checksum 必须等于实际 MD5(app.tgz)
ACTUAL="$(md5sum "${VERIFY}/app.tgz" | cut -d' ' -f1)"
DECLARED="$(sed -n 's/^[[:space:]]*checksum[[:space:]]*=[[:space:]]*//p' "${VERIFY}/manifest" | tr -d '[:space:]')"
[ -n "${DECLARED}" ] || fail_verify "manifest 缺少 checksum 字段"
[ "${ACTUAL}" = "${DECLARED}" ] || fail_verify "checksum 不匹配：manifest=${DECLARED} 实际=${ACTUAL}"

# 6.4 manifest 版本/appname
grep -Eq "^version[[:space:]]*=[[:space:]]*${VERSION}\$" "${VERIFY}/manifest" \
    || fail_verify "manifest version 与 ${VERSION} 不一致"
grep -Eq "^appname[[:space:]]*=[[:space:]]*${APPNAME}\$" "${VERIFY}/manifest" \
    || fail_verify "manifest appname 与 ${APPNAME} 不一致"

# 6.5 app.tgz 能解开，且包含 config/ 副本与关键代码
APPV="${BUILD_ROOT}/appv"
mkdir -p "${APPV}"
tar xzf "${VERIFY}/app.tgz" -C "${APPV}"
for required in config/privilege config/resource \
                proxy/app.py proxy/netease_auth.py proxy/pushplus.py \
                proxy/netease_items.py proxy/extra_items.py proxy/extra_sources.py \
                proxy/playlists.py proxy/download.py proxy/quality.py \
                proxy/admin_ui.py proxy/loghouse.py \
                proxy/recommend.py proxy/env_merge.py proxy/trimgw.py proxy/local_files.py proxy/prefetch.py proxy/run_proxy.sh proxy/requirements.txt \
                musicbox-service/app.py musicbox-service/requirements.txt \
                bin/fnmusic-lib.sh bin/setup.sh bin/start.sh bin/stop.sh bin/status.sh \
                bin/restart_services.sh \
                restore.sh netease_login.sh VERSION; do
    [ -e "${APPV}/${required}" ] || fail_verify "app.tgz 缺少 ${required}"
done
# app.tgz 内不应带 app/ 前缀层级
[ ! -d "${APPV}/app" ] || fail_verify "app.tgz 内不应存在 app/ 前缀目录层级"

# 6.5b 桌面入口：ui/config 合法，且 gatewaySocket 与生命周期脚本创建的 socket 名一致
if [ -n "${DESKTOP_UIDIR}" ]; then
    [ -f "${APPV}/${DESKTOP_UIDIR}/config" ] || fail_verify "app.tgz 缺少 ${DESKTOP_UIDIR}/config"
    GATEWAY_SOCK="$(python3 - "${APPV}/${DESKTOP_UIDIR}/config" <<'PYEOF'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
urls = cfg.get(".url") or {}
assert urls, "ui/config 的 .url 不能为空"
socks = {v.get("gatewaySocket") for v in urls.values()}
prefixes = {v.get("gatewayPrefix") for v in urls.values()}
assert len(socks) == 1 and all(socks), "所有入口必须声明同一个 gatewaySocket"
assert len(prefixes) == 1 and all(prefixes), "所有入口必须声明 gatewayPrefix"
print(list(socks)[0])
PYEOF
)" || fail_verify "${DESKTOP_UIDIR}/config 校验失败"
    [ -n "${GATEWAY_SOCK}" ] || fail_verify "无法从 ui/config 解析 gatewaySocket"
    grep -q "UI_SOCK=\"\${APPDEST}/${GATEWAY_SOCK}\"" "${APPV}/bin/fnmusic-lib.sh" \
        || fail_verify "生命周期脚本创建的 socket 名与 ui/config 的 gatewaySocket(${GATEWAY_SOCK}) 不一致"
    [ -d "${APPV}/${DESKTOP_UIDIR}/images" ] || log_warn "${DESKTOP_UIDIR}/images 不存在，桌面图标将缺失"
    log_info "桌面入口校验通过: gatewaySocket=${GATEWAY_SOCK}"
fi

# 6.6 载荷中不得混入已删除的多音源残留与测试/构建垃圾
JUNK="$(find "${APPV}" \( -name 'test_*.py' -o -name '__pycache__' \
        -o -name '.DS_Store' -o -name '*.pyc' -o -name '.env' \) -print 2>/dev/null || true)"
if [ -n "${JUNK}" ]; then
    log_err "app.tgz 内含不该打包的文件："
    printf '%s\n' "${JUNK}"
    exit 1
fi
# 只匹配【完整】的废弃键名（FNMUSIC_LX_ENABLED 之类）。
# 裸前缀字符串 "FNMUSIC_LX_" 在 proxy/env_merge.py 里是刻意保留的清理名单，不是活跃使用。
OBSOLETE_RE='FNMUSIC_(MUSICDL|LX|LLM)_[A-Z]'
if grep -rIl -E "${OBSOLETE_RE}" "${APPV}" >/dev/null 2>&1; then
    log_err "app.tgz 内仍在读写 v1.x 废弃配置键："
    grep -rIn -E "${OBSOLETE_RE}" "${APPV}" | head -20
    exit 1
fi

# 6.7 图标尺寸与体积
python3 - "$VERIFY" <<'PYEOF' || fail_verify "图标校验未通过"
import os, sys, struct
root = sys.argv[1]
def png_size(path):
    with open(path, "rb") as f:
        head = f.read(33)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path} 不是 PNG"
    w, h = struct.unpack(">II", head[16:24])
    return w, h
for name, want in (("ICON.PNG", 64), ("ICON_256.PNG", 256)):
    p = os.path.join(root, name)
    w, h = png_size(p)
    assert (w, h) == (want, want), f"{name} 应为 {want}x{want}，实际 {w}x{h}"
    kb = os.path.getsize(p) / 1024
    assert kb <= 1024, f"{name} 超过 1024 KB 限制 ({kb:.1f} KB)"
    print(f"{name}: {w}x{h}, {kb:.1f} KB  OK")
PYEOF

log_info "自检全部通过：结构 / JSON / checksum / manifest / 载荷 / 图标"

# ------------------------------------------------------------------------------
# 7. 可选：用官方 fnpack 交叉验证
# ------------------------------------------------------------------------------
if [ "${USE_FNPACK}" -eq 1 ]; then
    if command -v "${FNPACK_BIN}" >/dev/null 2>&1; then
        log_info "检测到官方 fnpack，开始交叉验证 ..."
        REF="${BUILD_ROOT}/fnpack-src"
        mkdir -p "${REF}"
        cp -a "${STAGE}/manifest" "${STAGE}/cmd" "${STAGE}/config" "${STAGE}/wizard" "${REF}/"
        cp -a "${VERIFY}/ICON.PNG" "${VERIFY}/ICON_256.PNG" "${REF}/"
        cp -a "${APP_SRC}" "${REF}/app"
        ( cd "${REF}" && "${FNPACK_BIN}" build >/dev/null 2>&1 ) || {
            log_warn "官方 fnpack build 失败（不影响内置实现产物）"
        }
        if [ -f "${REF}/${APPNAME}.fpk" ]; then
            REFX="${BUILD_ROOT}/fnpack-out"
            mkdir -p "${REFX}"
            tar xzf "${REF}/${APPNAME}.fpk" -C "${REFX}"
            log_info "官方 fnpack 产物顶层条目: $(cd "${REFX}" && ls -A | tr '\n' ' ')"
            log_info "本脚本产物顶层条目:      $(cd "${VERIFY}" && ls -A | tr '\n' ' ')"
            diff <(cd "${REFX}" && ls -A | sort) <(cd "${VERIFY}" && ls -A | sort) \
                && log_info "顶层结构与官方 fnpack 完全一致" \
                || log_warn "顶层结构与官方 fnpack 存在差异（见上方 diff）"
            REF_CK="$(sed -n 's/^[[:space:]]*checksum[[:space:]]*=[[:space:]]*//p' "${REFX}/manifest" | tr -d '[:space:]')"
            REF_ACTUAL="$(md5sum "${REFX}/app.tgz" | cut -d' ' -f1)"
            [ "${REF_CK}" = "${REF_ACTUAL}" ] \
                && log_info "官方 fnpack 亦满足 checksum = MD5(app.tgz)，与本实现一致" \
                || log_warn "官方 fnpack 产物 checksum(${REF_CK}) != MD5(app.tgz)(${REF_ACTUAL})"
        fi
    else
        log_warn "--fnpack: 未找到 ${FNPACK_BIN}，跳过交叉验证（可设 FNPACK_BIN=/path/to/fnpack）"
    fi
fi

log_info "============================================================"
log_info "构建完成: ${FPK_PATH}"
log_info "安装到真机（三选一）:"
log_info "  1. 应用中心 → 左下角「手动安装」→ 选择该 .fpk"
log_info "  2. appcenter-cli install-fpk ${FPK_NAME}"
log_info "  3. 把项目目录放到 NAS 上后 cd 进去执行 appcenter-cli install-local"
log_info "============================================================"
