#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键安装 / 配置
# - v2.0 起只保留网易云一个在线音源（单源，无多选）：
#     musicbox https://github.com/darknessomi/musicbox   (:8770 网易云)
#   所有在线曲目都来自扫码登录的那个私人网易云账号的权益。
# - 每日推荐抓取网易云官方「每日推荐」歌单（需登录，不再依赖 LLM）
# - 可选 PushPlus 推送提醒（登录态失效 / VIP 临期）
# - 不修改飞牛 nginx / 官方二进制 / 官方数据库写入
# 用法:
#   ./install.sh                         # 交互
#   ./install.sh --mode host
#   ./install.sh --mode docker --daily true --free-only-on-logout true
#   ./install.sh --non-interactive --mode docker --pushplus-token '***' --pushplus-topic '***'
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FNMUSIC_VERSION="$(head -n 1 "${BASE_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
FNMUSIC_VERSION="${FNMUSIC_VERSION:-0.0.0}"
MODE=""
NON_INTERACTIVE=0
RUN_EXTEND=0
# PushPlus 推送（token/topic 预取自环境变量；ENABLED 空哨兵=本次未明确选择）
PUSHPLUS_ENABLED=""
PUSHPLUS_TOKEN="${FNMUSIC_PUSHPLUS_TOKEN:-}"
PUSHPLUS_TOPIC="${FNMUSIC_PUSHPLUS_TOPIC:-}"
# 登录降级 / 每日推荐开关（--free-only-on-logout / --daily 控制，空哨兵=沿用默认值）
FREE_ONLY_ON_LOGOUT=""
FREE_ONLY_FROM_CLI=0
DAILY_ENABLED=""
DAILY_FROM_CLI=0
# 网易云音源参数（缺省与 .env.example 一致，可用同名环境变量覆盖）
MUSICBOX_URL="${FNMUSIC_MUSICBOX_URL:-http://127.0.0.1:8770}"
NETEASE_QUALITY="${FNMUSIC_NETEASE_QUALITY:-lossless}"
NETEASE_SEARCH_LIMIT="${FNMUSIC_NETEASE_SEARCH_LIMIT:-50}"
ONLINE_LIMIT="${FNMUSIC_ONLINE_LIMIT:-30}"
NETEASE_WAIT_S="${FNMUSIC_NETEASE_WAIT_S:-3.0}"
LATE_PAGE_WAIT_S="${FNMUSIC_LATE_PAGE_WAIT_S:-5.0}"
SEARCH_TIMEOUT="${FNMUSIC_SEARCH_TIMEOUT:-15}"
SEARCH_CACHE_TTL="${FNMUSIC_SEARCH_CACHE_TTL:-604800}"
LOGIN_STATE_TTL="${FNMUSIC_LOGIN_STATE_TTL:-300}"
LOGIN_CHECK_INTERVAL="${FNMUSIC_LOGIN_CHECK_INTERVAL:-3600}"
VIP_WARN_DAYS="${FNMUSIC_VIP_WARN_DAYS:-7}"
DAILY_LIMIT="${FNMUSIC_DAILY_LIMIT:-20}"
PUSHPLUS_TEMPLATE="${FNMUSIC_PUSHPLUS_TEMPLATE:-markdown}"
PUSHPLUS_URL="${FNMUSIC_PUSHPLUS_URL:-https://www.pushplus.plus/send}"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
MUSICBOX_REPO="${MUSICBOX_REPO:-https://github.com/darknessomi/musicbox}"
BASE_IMAGE="${BASE_IMAGE:-}"
DOCKER_IMAGE_MIRRORS="${DOCKER_IMAGE_MIRRORS:-docker.1ms.run docker.m.daocloud.io docker.1panel.live hub.rat.dev}"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

usage() {
    cat <<'EOF'
用法: ./install.sh [选项]

  --mode host|docker            安装模式（host=宿主机 venv；docker=musicbox 容器）
  --non-interactive             无交互，缺省值：mode=docker，开启每日推荐，未登录只播免费曲目
  --pushplus-token TOKEN        PushPlus 推送 token（不回显；用于登录失效/VIP 临期提醒）
  --pushplus-topic TOPIC        PushPlus 群组编码（可选，留空只推送给自己）
  --free-only-on-logout BOOL    未登录时是否降级为只播免费曲目（true|false，缺省 true）
  --daily BOOL                  是否开启网易云官方「每日推荐」歌单（true|false，缺省 true）
  --extend                      安装完成后立即执行 ./extend.sh
  --qr                          启动终端网易云扫码登录流程
  -h, --help                    显示帮助

v2.0 起仅保留网易云单一音源（musicbox），所有在线曲目来自扫码登录的私人账号权益。
PushPlus token 只写入仓库根目录 .env（chmod 600），不会进入 systemd 文件或日志。
EOF
}

# 归一化布尔入参：接受 true/false/1/0/yes/no/on/off，输出规范 true|false；非法值直接退出
normalize_bool() {
    local name="$1" raw="${2:-}"
    case "$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]')" in
        true|1|yes|on) printf 'true' ;;
        false|0|no|off) printf 'false' ;;
        *)
            log_err "${name} 只接受 true|false（收到: ${raw:-空}）"
            exit 1
            ;;
    esac
}

# 旧多音源参数（--sources / --llm-* / --enable-recommend 等）已废弃：接受但忽略并告警
warn_deprecated() {
    log_warn "参数 $1 自 v2.0 起已废弃：仅保留网易云单一音源，该参数被忽略。"
}

wait_http() {
    local url="$1" tries="${2:-60}" delay="${3:-2}"
    local i
    for i in $(seq 1 "${tries}"); do
        if curl -sf --max-time 3 "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep "${delay}"
    done
    return 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)
            [ $# -ge 2 ] || { log_err "--mode 需要参数 host|docker"; exit 1; }
            MODE="${2}"; shift 2 ;;
        --mode=*) MODE="${1#*=}"; shift ;;
        # --- v2.0 新增：PushPlus / 登录降级 / 每日推荐 ---
        --pushplus-token)
            [ $# -ge 2 ] || { log_err "--pushplus-token 需要 TOKEN 参数"; exit 1; }
            PUSHPLUS_TOKEN="${2}"; shift 2 ;;
        --pushplus-token=*) PUSHPLUS_TOKEN="${1#*=}"; shift ;;
        --pushplus-topic)
            [ $# -ge 2 ] || { log_err "--pushplus-topic 需要 TOPIC 参数"; exit 1; }
            PUSHPLUS_TOPIC="${2}"; shift 2 ;;
        --pushplus-topic=*) PUSHPLUS_TOPIC="${1#*=}"; shift ;;
        --free-only-on-logout)
            [ $# -ge 2 ] || { log_err "--free-only-on-logout 需要 true|false 参数"; exit 1; }
            FREE_ONLY_ON_LOGOUT="$(normalize_bool "--free-only-on-logout" "${2}")"
            FREE_ONLY_FROM_CLI=1; shift 2 ;;
        --free-only-on-logout=*)
            FREE_ONLY_ON_LOGOUT="$(normalize_bool "--free-only-on-logout" "${1#*=}")"
            FREE_ONLY_FROM_CLI=1; shift ;;
        --daily)
            [ $# -ge 2 ] || { log_err "--daily 需要 true|false 参数"; exit 1; }
            DAILY_ENABLED="$(normalize_bool "--daily" "${2}")"
            DAILY_FROM_CLI=1; shift 2 ;;
        --daily=*)
            DAILY_ENABLED="$(normalize_bool "--daily" "${1#*=}")"
            DAILY_FROM_CLI=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --extend) RUN_EXTEND=1; shift ;;
        --qr)
            bash "${BASE_DIR}/netease_login.sh"
            exit 0
            ;;
        # --- 以下为 v1.x 旧参数：仍接受但忽略，避免老命令行直接报错退出 ---
        --sources)
            # 旧「音源多选」已废弃；v2.0 只有网易云单源（吞掉随后的取值参数）
            warn_deprecated "--sources"
            [ $# -ge 2 ] && shift 2 || shift ;;
        --sources=*) warn_deprecated "--sources"; shift ;;
        --enable-recommend|--disable-recommend)
            # 每日推荐已改为抓取网易云官方日推，不再依赖 LLM；此开关映射到 --daily
            log_warn "参数 $1 语义已变更：每日推荐现抓取网易云官方歌单（等同 --daily true|false），不再使用 LLM。"
            if [ "$1" = "--disable-recommend" ]; then
                DAILY_ENABLED="false"; DAILY_FROM_CLI=1
            else
                DAILY_ENABLED="true"; DAILY_FROM_CLI=1
            fi
            shift ;;
        --llm-base-url|--llm-api-key|--llm-model)
            warn_deprecated "$1"
            [ $# -ge 2 ] && shift 2 || shift ;;
        --llm-base-url=*|--llm-api-key=*|--llm-model=*)
            warn_deprecated "${1%%=*}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) log_err "未知参数: $1"; usage; exit 1 ;;
    esac
done

run_docker() {
    if docker info >/dev/null 2>&1; then
        docker "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        sudo docker "$@"
    else
        return 1
    fi
}

# 容器名全局固定（fnmusic-*）；若被其他副本/并发任务的容器占用，移除后由当前目录接管
reclaim_container() {
    local name="$1" owner=""
    if ! run_docker container inspect "${name}" >/dev/null 2>&1; then
        return 0
    fi
    owner="$(run_docker container inspect "${name}" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
    log_info "检测到同名容器 ${name}（来自 ${owner:-未知目录}），移除后由当前目录接管..."
    if ! run_docker rm -f "${name}"; then
        log_err "无法移除同名容器 ${name}，请手动执行: docker rm -f ${name}"
        return 1
    fi
}

precheck_environment() {
    log_info "==> 开始安装环境预检..."
    local precheck_failed=0

    # 0. curl（健康探测 / 验收 / 二维码均依赖）
    if ! command -v curl >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统未检测到 curl。"
        log_err "请先执行：sudo apt-get update && sudo apt-get install -y curl"
        precheck_failed=1
    else
        log_info "curl 已就绪。"
    fi

    # 1. 检查 Python 3 与 venv 模块
    if ! command -v python3 >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统未检测到 python3。"
        log_err "请先执行命令安装：sudo apt-get update && sudo apt-get install -y python3 python3-venv"
        precheck_failed=1
    elif ! python3 -c "import venv" >/dev/null 2>&1; then
        log_err "【缺少基础组件】系统 Python 缺少 venv 模块。"
        log_err "请先执行命令安装：sudo apt-get update && sudo apt-get install -y python3-venv"
        precheck_failed=1
    else
        log_info "Python 3 与 venv 模块已就绪。"
    fi

    # 2. 检查 sudo 权限
    if ! sudo -n true 2>/dev/null; then
        if [ -t 0 ]; then
            log_warn "检测到当前操作需要管理员权限，正在请求 sudo 授权..."
            if ! sudo -v; then
                log_err "【权限不足】当前用户无法获取管理员 (sudo) 权限，安装无法继续。"
                precheck_failed=1
            fi
        else
            log_err "【权限不足】非交互模式下需要免密 sudo 权限（sudo -n true 失败）。"
            precheck_failed=1
        fi
    else
        log_info "管理员 (sudo) 权限已就绪。"
    fi

    # 3. 检查飞牛音乐运行套接字
    local target_sock="/var/run/trim_music.socket"
    local upstream_sock="/var/run/trim_music_upstream.socket"
    if [ ! -S "${target_sock}" ] && [ ! -S "${upstream_sock}" ]; then
        log_warn "【前置提醒】未检测到飞牛音乐运行套接字 (${target_sock} 不存在)。"
        log_warn "请确认已在 fnOS 管理界面 ->「应用中心」，安装并启动【飞牛音乐】应用。"
        log_warn "（安装向导仍可继续准备音源依赖与配置，但在最后执行 ./extend.sh 启用扩展前必须先启动飞牛音乐）"
    else
        log_info "飞牛音乐运行套接字检测正常。"
    fi

    # 4. 检查 Docker 环境
    if command -v docker >/dev/null 2>&1; then
        if run_docker info >/dev/null 2>&1; then
            log_info "Docker 容器环境已就绪。"
        else
            log_warn "检测到 docker 命令，但当前用户无法连通 Docker daemon。"
            if [ "${MODE}" = "docker" ]; then
                log_err "【权限不足】Docker 模式需要可用的 docker（或 sudo docker）。"
                precheck_failed=1
            fi
        fi
    else
        log_warn "【提示】系统未检测到 Docker 环境。"
        if [ "${MODE}" = "docker" ]; then
            log_err "【缺少组件】当前指定了 Docker 模式，但系统未安装 Docker。"
            log_err "请先在 fnOS 应用中心安装 Docker，或改用 --mode host 模式。"
            precheck_failed=1
        else
            log_warn "若计划使用 Docker 容器模式运行音源，请先在 fnOS 应用中心安装 Docker；"
            log_warn "您也可以在向导中选择宿主机 (host) 模式直接通过 Python 虚拟环境运行。"
        fi
    fi

    if [ "${precheck_failed}" -ne 0 ]; then
        log_err "环境预检未通过，请处理上述问题后再试。"
        exit 1
    fi
    log_info "环境预检全部通过。"
}

ensure_docker_ready() {
    if ! command -v docker >/dev/null 2>&1 || ! run_docker info >/dev/null 2>&1; then
        log_err "【缺少组件】已选择 Docker 模式，但 Docker 不可用。"
        log_err "请先在 fnOS 应用中心安装 Docker，或改用 --mode host。"
        exit 1
    fi
}

precheck_environment

dotenv_escape() {
    printf "%s" "$1" | sed "s/'/'\\\\''/g"
}

prompt() {
    local msg="$1" def="${2:-}"
    local ans=""
    if [ -n "$def" ]; then
        read -r -p "$msg [$def]: " ans || true
        echo "${ans:-$def}"
    else
        read -r -p "$msg: " ans || true
        echo "$ans"
    fi
}

if [ "${NON_INTERACTIVE}" -eq 0 ]; then
    echo "============================================================"
    echo " fnmusic-ext 安装配置向导  v${FNMUSIC_VERSION}"
    echo " 唯一在线音源: 网易云 musicbox — ${MUSICBOX_REPO}"
    echo " (v2.0 起不再提供多音源选择；所有在线曲目均来自"
    echo "  扫码登录的私人网易云账号权益)"
    echo "============================================================"
    if [ -z "${MODE}" ]; then
        echo "【安装模式说明】"
        echo "  无论选哪种模式，核心代理（fnmusic-ext）均以宿主机 systemd 运行接管 Socket。"
        echo "  两种模式区别仅在于网易云音源服务（musicbox）的部署运行形态："
        if command -v docker >/dev/null 2>&1; then
            echo "  1) docker  — [推荐] Docker 容器模式："
            echo "               通过 compose 运行轻量容器（端口 8770，无特权，数据隔离在 musicbox-data/）"
            echo "  2) host    — Host 宿主机本地服务模式（纯净无 Docker）："
            echo "               创建独立 Python venv 并注册为 systemd 服务（监听 8770 端口，不污染全局环境）"
            local_choice="$(prompt "请选择安装模式 (输入 1 或 2)" "1")"
        else
            echo "  1) docker  — Docker 容器模式（未检测到 Docker，若选此项请先在 fnOS「应用中心」安装 Docker）"
            echo "  2) host    — [推荐当前环境] Host 宿主机本地服务模式："
            echo "               纯净无 Docker，通过项目内独立 Python venv 运行并注册为 systemd 服务"
            local_choice="$(prompt "请选择安装模式 (输入 1 或 2)" "2")"
        fi
        case "${local_choice}" in
            2|host) MODE="host" ;;
            *) MODE="docker" ;;
        esac
    fi
    # PushPlus 推送提醒（替代 v1.x 的 LLM 每日推荐配置环节）
    if [ -z "${PUSHPLUS_ENABLED}" ]; then
        echo "PushPlus 推送提醒（可选）:"
        echo "  网易云登录态失效、VIP 临期等事件将通过 PushPlus（https://www.pushplus.plus）"
        echo "  推送到微信，免费注册后在个人中心获取 token。"
        pp_choice="$(prompt "是否启用 PushPlus 推送提醒? [y/N]" "N")"
        case "${pp_choice}" in
            y|Y|yes|YES) PUSHPLUS_ENABLED="true" ;;
            *) PUSHPLUS_ENABLED="false" ;;
        esac
    fi
    if [ "${PUSHPLUS_ENABLED}" = "true" ]; then
        if [ -z "${PUSHPLUS_TOKEN}" ]; then
            read -r -s -p "PushPlus token（输入不回显；留空则暂不实际推送）: " PUSHPLUS_TOKEN || true
            echo
        fi
        if [ -z "${PUSHPLUS_TOPIC}" ]; then
            PUSHPLUS_TOPIC="$(prompt "PushPlus 群组编码（可选，留空只推送给自己）")"
        fi
        if [ -n "${PUSHPLUS_TOKEN}" ]; then
            log_info "PushPlus 已启用（token: ${PUSHPLUS_TOKEN:0:4}****，完整值仅写入 .env，不出现在日志）。"
        else
            log_warn "PushPlus token 为空，暂不会实际推送；后续可在 .env 填写 FNMUSIC_PUSHPLUS_TOKEN 并重启 fnmusic-ext。"
        fi
    fi
    ext_choice="$(prompt "安装配置完成，是否立即执行 extend.sh 启用扩展? [Y/n]" "Y")"
    case "${ext_choice}" in
        n|N|no|NO) RUN_EXTEND=0 ;;
        *) RUN_EXTEND=1 ;;
    esac
else
    MODE="${MODE:-docker}"
    # 非交互：PushPlus 取 --pushplus-token/--pushplus-topic 或 FNMUSIC_PUSHPLUS_* 环境变量；
    # 缺省则 token 留空（pushplus.enabled() 按 token 门控，即不启用推送）。
    # 提供了 token 即视为希望启用推送。
    if [ -n "${PUSHPLUS_TOKEN}" ] && [ -z "${PUSHPLUS_ENABLED}" ]; then
        PUSHPLUS_ENABLED="true"
    fi
fi

MODE="${MODE:-docker}"
if [ "${MODE}" != "host" ] && [ "${MODE}" != "docker" ]; then
    log_err "mode 必须是 host 或 docker"
    exit 1
fi
if [ "${MODE}" = "docker" ]; then
    ensure_docker_ready
fi

# 开关最终值（未在命令行/交互中明确指定时回落到 .env.example 同款默认值）
FREE_ONLY_VAL="${FREE_ONLY_ON_LOGOUT:-true}"
# 音源服务监听地址：默认只绑回环（接口无鉴权，对外暴露等于谁都能顶掉你的网易云登录）
MUSICBOX_BIND="${MUSICBOX_BIND:-127.0.0.1}"
DAILY_VAL="${DAILY_ENABLED:-true}"
PUSHPLUS_ENABLED_VAL="${PUSHPLUS_ENABLED:-true}"

log_info "fnmusic-ext v${FNMUSIC_VERSION}"
log_info "安装模式: ${MODE}"
log_info "音源: musicbox — 网易云 [8770]（v2.0 起唯一在线音源）"
log_info "每日推荐: ${DAILY_VAL}（抓取网易云官方日推，需扫码登录）"
log_info "未登录降级只播免费曲目: ${FREE_ONLY_VAL}"
log_info "PushPlus 推送: $([ "${PUSHPLUS_ENABLED_VAL}" = "true" ] && [ -n "${PUSHPLUS_TOKEN}" ] && echo "已启用 (token: ${PUSHPLUS_TOKEN:0:4}****)" || echo "未启用（不会发送任何推送）")"
log_info "项目目录: ${BASE_DIR}"

mkdir -p "${BASE_DIR}/cache" "${BASE_DIR}/online_favorites" "${BASE_DIR}/play_history" "${BASE_DIR}/recommend_cache" \
    "${BASE_DIR}/musicbox-data/cache/netease-musicbox" \
    "${BASE_DIR}/musicbox-data/config/netease-musicbox" \
    "${BASE_DIR}/musicbox-data/netease-musicbox"
chmod -R 777 "${BASE_DIR}/musicbox-data" 2>/dev/null || true

# 归一化服务源码权限：umask 077 环境检出的文件为 600，会导致镜像内 appuser 读不到 app.py
chmod 0644 \
    "${BASE_DIR}/musicbox-service/app.py" "${BASE_DIR}/musicbox-service/runner.py" \
    "${BASE_DIR}/musicbox-service/netease_ext.py" \
    2>/dev/null || true

# --- 写 .env（防覆盖：安全增量合并，脱敏：不打印 token） ---
# desired 键集与 .env.example 对齐；敏感值（PushPlus token）沿用 dotenv_escape 单引号转义。
ENV_PATH="${BASE_DIR}/.env"
umask 077
ENV_DESIRED="$(mktemp)"
{
    echo "FNMUSIC_HOME='$(dotenv_escape "${BASE_DIR}")'"
    echo "FNMUSIC_CACHE_DIR='$(dotenv_escape "${BASE_DIR}/cache")'"
    echo "FNMUSIC_FAV_DIR='$(dotenv_escape "${BASE_DIR}/online_favorites")'"
    echo "FNMUSIC_PLAY_HISTORY_DIR='$(dotenv_escape "${BASE_DIR}/play_history")'"
    echo "FNMUSIC_RECOMMEND_DIR='$(dotenv_escape "${BASE_DIR}/recommend_cache")'"
    # --- 网易云单源（v2.0 唯一在线音源，恒为启用） ---
    echo "FNMUSIC_NETEASE_ENABLED='true'"
    echo "FNMUSIC_MUSICBOX_URL='$(dotenv_escape "${MUSICBOX_URL}")'"
    echo "FNMUSIC_NETEASE_QUALITY='$(dotenv_escape "${NETEASE_QUALITY}")'"
    echo "FNMUSIC_NETEASE_SEARCH_LIMIT='${NETEASE_SEARCH_LIMIT}'"
    echo "FNMUSIC_ONLINE_LIMIT='${ONLINE_LIMIT}'"
    echo "FNMUSIC_NETEASE_WAIT_S='${NETEASE_WAIT_S}'"
    echo "FNMUSIC_LATE_PAGE_WAIT_S='${LATE_PAGE_WAIT_S}'"
    echo "FNMUSIC_SEARCH_TIMEOUT='${SEARCH_TIMEOUT}'"
    echo "FNMUSIC_SEARCH_CACHE_TTL='${SEARCH_CACHE_TTL}'"
    # --- 登录态与降级 ---
    echo "FNMUSIC_FREE_ONLY_ON_LOGOUT='${FREE_ONLY_VAL}'"
    echo "FNMUSIC_LOGIN_STATE_TTL='${LOGIN_STATE_TTL}'"
    echo "FNMUSIC_LOGIN_CHECK_INTERVAL='${LOGIN_CHECK_INTERVAL}'"
    echo "FNMUSIC_VIP_WARN_DAYS='${VIP_WARN_DAYS}'"
    # --- 网易云官方「每日推荐」歌单（需登录） ---
    echo "FNMUSIC_DAILY_ENABLED='${DAILY_VAL}'"
    echo "FNMUSIC_DAILY_LIMIT='${DAILY_LIMIT}'"
    # --- PushPlus 推送提醒（token 单引号转义，绝不打印到日志） ---
    echo "FNMUSIC_PUSHPLUS_ENABLED='${PUSHPLUS_ENABLED_VAL}'"
    echo "FNMUSIC_PUSHPLUS_TOKEN='$(dotenv_escape "${PUSHPLUS_TOKEN}")'"
    echo "FNMUSIC_PUSHPLUS_TOPIC='$(dotenv_escape "${PUSHPLUS_TOPIC}")'"
    echo "FNMUSIC_PUSHPLUS_TEMPLATE='$(dotenv_escape "${PUSHPLUS_TEMPLATE}")'"
    echo "FNMUSIC_PUSHPLUS_URL='$(dotenv_escape "${PUSHPLUS_URL}")'"
    # --- 运行形态（BASE_IMAGE 留空，由 ensure_base_image.sh 探测后写入） ---
    echo "FNMUSIC_MODE='${MODE}'"
    echo "FNMUSIC_BASE_IMAGE='$(dotenv_escape "${BASE_IMAGE}")'"
    echo "FNMUSIC_PIP_INDEX='$(dotenv_escape "${PIP_INDEX}")'"
    echo "FNMUSIC_DOCKER_MIRRORS='$(dotenv_escape "${DOCKER_IMAGE_MIRRORS}")'"
    echo "FNMUSIC_VERSION='${FNMUSIC_VERSION}'"
} > "${ENV_DESIRED}"

# 用户本次明确提供了新值的键（版本/单源开关/运行模式为安装部署选项，始终采用新值）
ENV_EXPLICIT="FNMUSIC_VERSION,FNMUSIC_NETEASE_ENABLED,FNMUSIC_MODE"
[ "${FREE_ONLY_FROM_CLI}" -eq 1 ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_FREE_ONLY_ON_LOGOUT"
[ "${DAILY_FROM_CLI}" -eq 1 ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_DAILY_ENABLED"
# PushPlus：仅当本次确实拿到值（交互/命令行/环境变量）才显式写入，避免升级时覆盖/清空既有 token
[ -n "${PUSHPLUS_ENABLED}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_PUSHPLUS_ENABLED"
[ -n "${PUSHPLUS_TOKEN}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_PUSHPLUS_TOKEN"
[ -n "${PUSHPLUS_TOPIC}" ] && ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_PUSHPLUS_TOPIC"

if [ -f "${ENV_PATH}" ]; then
    PREV_VERSION="$(grep -E "^\s*(export\s+)?FNMUSIC_VERSION=" "${ENV_PATH}" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]" || true)"
    PREV_VERSION="${PREV_VERSION:-}"
    ENV_BACKUP="${ENV_PATH}.bak.$(date +%Y%m%d%H%M%S)"
    cp -p "${ENV_PATH}" "${ENV_BACKUP}"
    if [ -n "${PREV_VERSION}" ] && [ "${PREV_VERSION}" = "${FNMUSIC_VERSION}" ]; then
        log_warn "检测到同版本 (v${FNMUSIC_VERSION}) 重复安装：现有配置将被保护，"
        log_warn "仅补齐缺失配置项；token/自定义路径/网易云参数等沿用已有值（备份: ${ENV_BACKUP}）。"
    else
        log_info "检测到已有配置（v${PREV_VERSION:-未知} -> v${FNMUSIC_VERSION}）平滑升级："
        log_info "保留用户自定义配置与 token，仅安全补齐新增/缺失配置项（备份: ${ENV_BACKUP}）。"
    fi
    MERGE_SUMMARY="$(python3 "${BASE_DIR}/proxy/env_merge.py" \
        --existing "${ENV_PATH}" --desired "${ENV_DESIRED}" \
        --output "${ENV_PATH}" --explicit "${ENV_EXPLICIT}" 2>&1)" || {
        log_err "配置合并失败，已保留原配置不动: ${ENV_PATH}"
        rm -f "${ENV_DESIRED}"
        exit 1
    }
    log_info "配置合并完成 (v${FNMUSIC_VERSION})："
    while IFS= read -r line; do
        [ -n "${line}" ] && log_info "  ${line}"
    done <<< "${MERGE_SUMMARY}"
else
    python3 "${BASE_DIR}/proxy/env_merge.py" \
        --existing /dev/null --desired "${ENV_DESIRED}" \
        --output "${ENV_PATH}" --explicit "${ENV_EXPLICIT}" --quiet
    log_info "已生成初始配置 ${ENV_PATH} (chmod 600)。PushPlus token 不会出现在日志中。"
fi
rm -f "${ENV_DESIRED}"
chmod 600 "${ENV_PATH}"

# --- 代理 Python 环境 ---
if ! command -v python3 >/dev/null 2>&1; then
    log_err "需要 python3"
    exit 1
fi
if [ ! -x "${BASE_DIR}/.venv-proxy/bin/python" ]; then
    log_info "创建 .venv-proxy ..."
    python3 -m venv "${BASE_DIR}/.venv-proxy"
fi
log_info "安装代理依赖..."
"${BASE_DIR}/.venv-proxy/bin/pip" install -q -U pip -i "${PIP_INDEX}"
"${BASE_DIR}/.venv-proxy/bin/pip" install -q -r "${BASE_DIR}/proxy/requirements.txt" -i "${PIP_INDEX}"

install_unit() {
    local src="$1" dest="$2"
    if ! sudo -n true 2>/dev/null; then
        log_warn "无免密 sudo，请手动安装 unit: ${src}"
        log_warn "或稍后用 sudo cp 该文件到 ${dest}"
        return 1
    fi
    sudo cp "${src}" "${dest}"
    rm -f "${src}"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$(basename "${dest}")"
    return 0
}

# --- musicbox（v2.0 唯一在线音源：网易云） ---
install_musicbox_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        log_err "未找到 docker，无法使用 docker 模式。请安装 Docker 或改用 --mode host"
        return 1
    fi
    mkdir -p "${BASE_DIR}/musicbox-data/cache/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/config/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/netease-musicbox"
    chmod -R 777 "${BASE_DIR}/musicbox-data" 2>/dev/null || true
    log_info "构建并启动 musicbox 容器（基于 ${MUSICBOX_REPO}）..."
    reclaim_container fnmusic-musicbox || return 1
    run_docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build musicbox
    if wait_http "http://127.0.0.1:8770/healthz" 60 2; then
        log_info "musicbox 已就绪 http://127.0.0.1:8770/healthz"
        return 0
    fi
    log_err "等待 musicbox healthz 超时"
    return 1
}

install_musicbox_host() {
    log_info "宿主机安装 musicbox 服务（pip 包来自 ${MUSICBOX_REPO}）..."
    mkdir -p "${BASE_DIR}/musicbox-data/cache/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/config/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/netease-musicbox"
    chmod -R 777 "${BASE_DIR}/musicbox-data" 2>/dev/null || true
    if [ ! -x "${BASE_DIR}/.venv-musicbox/bin/python" ]; then
        python3 -m venv "${BASE_DIR}/.venv-musicbox"
    fi
    "${BASE_DIR}/.venv-musicbox/bin/pip" install -q -U pip -i "${PIP_INDEX}"
    "${BASE_DIR}/.venv-musicbox/bin/pip" install -q -r "${BASE_DIR}/musicbox-service/requirements.txt" -i "${PIP_INDEX}"
    local unit
    unit="$(mktemp)"
    cat > "${unit}" <<EOF
[Unit]
Description=fnmusic-ext musicbox source (${MUSICBOX_REPO})
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${BASE_DIR}/musicbox-service
Environment=PYTHONUNBUFFERED=1
Environment=XDG_DATA_HOME=${BASE_DIR}/musicbox-data
Environment=XDG_CACHE_HOME=${BASE_DIR}/musicbox-data/cache
Environment=XDG_CONFIG_HOME=${BASE_DIR}/musicbox-data/config
Environment=FNMUSIC_FREE_ONLY_ON_LOGOUT=${FREE_ONLY_VAL:-true}
ExecStart=${BASE_DIR}/.venv-musicbox/bin/uvicorn app:app --host ${MUSICBOX_BIND} --port 8770
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    if ! install_unit "${unit}" /etc/systemd/system/fnmusic-musicbox.service; then
        return 0
    fi
    if wait_http "http://127.0.0.1:8770/healthz" 30 1; then
        log_info "宿主机 musicbox 已就绪"
        return 0
    fi
    log_warn "musicbox systemd 已启动，但 healthz 尚未就绪，请检查 journalctl -u fnmusic-musicbox"
}

clear_opposite_mode() {
    # musicbox 部署形态切换时清理对侧，避免 8770 端口被 docker 与 host 同时占用
    if [ "${MODE}" = "docker" ]; then
        log_info "Docker 模式：停用宿主机 musicbox systemd unit（若存在）..."
        sudo systemctl disable --now fnmusic-musicbox.service 2>/dev/null || true
    else
        log_info "Host 模式：停止 Docker musicbox 容器（若存在）..."
        run_docker rm -f fnmusic-musicbox 2>/dev/null || true
    fi
}

cleanup_legacy_sources() {
    # v2.0 单源化：无条件清理 v1.x 多音源（musicdl / lxmusic）遗留的容器与 systemd unit，
    # 让从旧版升级上来的用户不残留占用 8768/8772 端口的僵尸服务与开机自启项。
    log_info "清理 v1.x 历史音源残留（musicdl / lxmusic 容器与 systemd unit）..."
    run_docker rm -f fnmusic-musicdl fnmusic-lxmusic 2>/dev/null || true
    sudo systemctl disable --now fnmusic-musicdl.service fnmusic-lxmusic.service 2>/dev/null || true
    local unit
    for unit in fnmusic-musicdl fnmusic-lxmusic; do
        if [ -f "/etc/systemd/system/${unit}.service" ]; then
            sudo rm -f "/etc/systemd/system/${unit}.service" 2>/dev/null || true
        fi
    done
    sudo systemctl daemon-reload 2>/dev/null || true
}

# Docker 模式：先探测可用基础镜像源（国内镜像优先直连、官方源兜底），
# 结果写入 .env 的 FNMUSIC_BASE_IMAGE 供 compose build.args 使用；失败直接退出，不动现有部署
if [ "${MODE}" = "docker" ]; then
    if ! BASE_IMAGE="${BASE_IMAGE}" FNMUSIC_DOCKER_MIRRORS="${DOCKER_IMAGE_MIRRORS}" \
        bash "${BASE_DIR}/ensure_base_image.sh"; then
        log_err "基础镜像源探测失败。可设置 BASE_IMAGE 环境变量手动指定可用镜像源，或改用 --mode host。"
        exit 1
    fi
fi

cleanup_legacy_sources
clear_opposite_mode
if [ "${MODE}" = "docker" ]; then
    install_musicbox_docker
else
    install_musicbox_host
fi

python3 -m py_compile "${BASE_DIR}/proxy/app.py" "${BASE_DIR}/proxy/recommend.py"
bash -n "${BASE_DIR}/extend.sh" "${BASE_DIR}/restore.sh" "${BASE_DIR}/proxy/run_proxy.sh" "${BASE_DIR}/netease_login.sh" "${BASE_DIR}/ensure_base_image.sh"

log_info "============================================================"
log_info "🎉 fnmusic-ext v${FNMUSIC_VERSION} 安装配置完成！"
log_info "在线音源（安装模式: ${MODE}）：musicbox — 网易云 [8770]（单一音源）"
log_info "------------------------------------------------------------"
log_info "【音源服务状态】"
log_info "  • musicbox  [8770] 网易云音源     ${MUSICBOX_URL}/healthz"
log_info "------------------------------------------------------------"
log_info "【后续验证与使用指引】"
if [ "${RUN_EXTEND}" -eq 1 ]; then
    log_info "即将自动执行 ./extend.sh 进行 Unix Socket 接管与链路自检验收..."
else
    log_info "1. 一键启用扩展："
    log_info "   请在终端运行: ./extend.sh"
    log_info "   （脚本将自动接管 Unix Socket 并进行链路自检验收，安全零侵入）"
fi
log_info "2. 验证搜索与试听："
log_info "   打开飞牛音乐 Web 端或手机 App，在搜索框中搜索歌曲（例如“晴天”或“周杰伦”），"
log_info "   点击在线源歌曲试听，确认可以流畅播放并显示歌词与封面。"
log_info "3. 网易云扫码登录（强烈推荐）："
log_info "   v2.0 起所有在线曲目都来自扫码登录的私人网易云账号权益；未登录时仅提供免费曲目降级播放。"
log_info "   • 命令行扫码登录（推荐）: ./install.sh --qr 或 ./netease_login.sh"
log_info "     （自动展示二维码、轮询登录状态、过期自动刷新，支持随时 Ctrl+C 跳过）"
log_info "   • 局域网浏览器图片（备选）: http://<NAS_IP>:8770/api/v1/auth/login/qr.png"
log_info "   • 检查登录与 VIP 状态: curl -s ${MUSICBOX_URL}/api/v1/auth/detail"
if [ "${DAILY_VAL}" = "true" ]; then
    log_info "4. 每日推荐（网易云官方日推）："
    log_info "   登录网易云后，飞牛音乐左侧歌单列表顶部会自动出现官方「每日推荐」（不再依赖 LLM）。"
fi
if [ "${PUSHPLUS_ENABLED_VAL}" = "true" ] && [ -n "${PUSHPLUS_TOKEN}" ]; then
    log_info "5. PushPlus 推送提醒：已启用 — 网易云登录态失效 / VIP 临期将推送到微信。"
else
    log_info "5. PushPlus 推送提醒：未启用。可在 .env 配置 FNMUSIC_PUSHPLUS_TOKEN 获取登录/VIP 临期微信提醒。"
fi
log_info "6. 状态探测与一键还原："
log_info "   • 探测健康状态: curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz"
log_info "   • 随时一键还原: ./restore.sh (立即恢复官方出厂直连状态)"
log_info "============================================================"

if [ "${NON_INTERACTIVE}" -eq 0 ]; then
    log_info ""
    log_info "==> 网易云是唯一在线音源，即将进入扫码登录流程（支持随时 Ctrl+C 跳过）..."
    bash "${BASE_DIR}/netease_login.sh" || true
fi

if [ "${RUN_EXTEND}" -eq 1 ]; then
    exec "${BASE_DIR}/extend.sh" --force
fi
