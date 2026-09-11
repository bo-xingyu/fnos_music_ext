#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 一键扩展脚本 (Unix Socket 接管架构)
# 功能：接管 /var/run/trim_music.socket，实现零侵入扩展（严禁修改 nginx 配置）
# 具备幂等性与自动回滚能力
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FNMUSIC_VERSION="$(head -n 1 "${BASE_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
FNMUSIC_VERSION="${FNMUSIC_VERSION:-0.0.0}"
TARGET_SOCK="/var/run/trim_music.socket"
UPSTREAM_SOCK="/var/run/trim_music_upstream.socket"
MUSICBOX_URL="http://127.0.0.1:8770"
FORCE_RELOAD=0

for arg in "$@"; do
    case "${arg}" in
        --qr)
            bash "${BASE_DIR}/netease_login.sh"
            exit 0
            ;;
        --force)
            FORCE_RELOAD=1
            ;;
        -h|--help)
            echo "用法: $0 [--force] [--qr]"
            echo "  --force  强制重写 unit 并重启代理（安装改配置后使用）"
            echo "  --qr     启动终端网易云扫码登录流程"
            exit 0
            ;;
        *)
            if [ "${arg}" != "" ]; then
                echo "未知参数: ${arg}"
                exit 1
            fi
            ;;
    esac
done

log_info() {
    echo -e "\033[32m[INFO]\033[0m $*"
}

log_warn() {
    echo -e "\033[33m[WARN]\033[0m $*"
}

log_err() {
    echo -e "\033[31m[ERROR]\033[0m $*" >&2
}

is_enabled() {
    case "$(printf '%s' "${1:-true}" | tr '[:upper:]' '[:lower:]')" in
        false|0|no|off) return 1 ;;
        *) return 0 ;;
    esac
}

# ------------------------------------------------------------------------------
# 获取 fnOS 网关 http/https 端口 (读取失败时默认 5666/5667)
# 输出: "<http_port> <https_port>"
# ------------------------------------------------------------------------------
get_fnos_gateway_ports() {
    cat /usr/trim/etc/network_gateway_setting.conf 2>/dev/null | python3 -c '
import sys, json, re
text = sys.stdin.read()
http_port, https_port = "5666", "5667"
def scan(obj):
    global http_port, https_port
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, (int, str)) and str(v).isdigit():
                if "https" in kl and "port" in kl:
                    https_port = str(v)
                elif "http" in kl and "port" in kl:
                    http_port = str(v)
            else:
                scan(v)
    elif isinstance(obj, list):
        for it in obj:
            scan(it)
if text.strip():
    try:
        scan(json.loads(text))
    except Exception:
        pass
    if http_port == "5666":
        m = re.search(r"\"?http_port\"?\s*[:=]\s*(\d+)", text)
        if m:
            http_port = m.group(1)
    if https_port == "5667":
        m = re.search(r"\"?https_port\"?\s*[:=]\s*(\d+)", text)
        if m:
            https_port = m.group(1)
print(http_port, https_port)
' 2>/dev/null || echo "5666 5667"
}

if [ ! -f "${BASE_DIR}/.env" ]; then
    if [ -t 0 ]; then
        log_warn "检测到尚未完成初次安装配置（未找到 .env 配置文件）。"
        read -r -p "检测到尚未完成初次安装配置，是否现在启动安装向导 (./install.sh)？[Y/n] " prompt_ans || true
        case "${prompt_ans:-y}" in
            y|Y|yes|YES|"")
                log_info "正在启动安装向导 (./install.sh)..."
                exec "${BASE_DIR}/install.sh"
                ;;
            *)
                log_err "请先执行 ./install.sh 完成音源与配置安装。"
                exit 1
                ;;
        esac
    else
        log_err "请先执行 ./install.sh 完成音源与配置安装。"
        exit 1
    fi
fi

set -a
# shellcheck disable=SC1091
source "${BASE_DIR}/.env"
set +a
MUSICBOX_URL="${FNMUSIC_MUSICBOX_URL:-${MUSICBOX_URL}}"
# v2.0 单源化：部署形态读 FNMUSIC_MODE（docker|host）
SRC_MODE="${FNMUSIC_MODE:-}"
# 网易云 musicbox 是唯一在线音源
ENABLE_MUSICBOX=0
is_enabled "${FNMUSIC_NETEASE_ENABLED:-true}" && ENABLE_MUSICBOX=1
if [ "${ENABLE_MUSICBOX}" -eq 0 ]; then
    log_err "FNMUSIC_NETEASE_ENABLED=false：v2.0 起网易云（musicbox）是唯一在线音源，"
    log_err "关闭它会导致本扩展没有任何可用在线音源、失去意义。请在 .env 中置为 true，"
    log_err "或执行 ./restore.sh 一键还原为官方直连。"
    exit 1
fi

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

# ------------------------------------------------------------------------------
# 回滚函数 (restore 逻辑)
# ------------------------------------------------------------------------------
rollback() {
    log_err "执行遇到错误或验收失败，正在执行自动回滚..."
    sudo systemctl disable --now fnmusic-ext.service 2>/dev/null || true

    # 探测 socket 状态
    local health_resp
    health_resp="$(curl -s --max-time 2 --unix-socket "${TARGET_SOCK}" http://localhost/_ext/healthz 2>/dev/null || true)"
    if echo "${health_resp}" | grep -q '"upstream"'; then
        # 当前原路径仍是代理 socket 残留，清理并恢复 upstream
        log_info "正在清理代理 socket 并恢复官方 trim-music socket..."
        sudo rm -f "${TARGET_SOCK}"
        if [ -S "${UPSTREAM_SOCK}" ]; then
            sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
            sudo chmod 666 "${TARGET_SOCK}"
        fi
    elif [ -S "${UPSTREAM_SOCK}" ] && [ ! -S "${TARGET_SOCK}" ]; then
        log_info "正在将 upstream socket 恢复为原路径..."
        sudo mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}"
        sudo chmod 666 "${TARGET_SOCK}"
    fi

    log_err "回滚完成。扩展未能成功启用。"
    exit 1
}

# ------------------------------------------------------------------------------
# 验收测试函数
# ------------------------------------------------------------------------------
verify_acceptance() {
    log_info "==> 执行链路与功能验收..."

    local GW_HTTP_PORT GW_HTTPS_PORT
    read -r GW_HTTP_PORT GW_HTTPS_PORT <<< "$(get_fnos_gateway_ports)"
    log_info "fnOS 网关端口: http=${GW_HTTP_PORT} https=${GW_HTTPS_PORT}"

    # 6a. 401 快速路径响应与时延测试 (< 3s)
    log_info "验收 6a: 验证未登录 401/99999 快速路径透传 (耗时必须 < 3s)..."
    local url_https="https://127.0.0.1:${GW_HTTPS_PORT}/music/api/v1/search/track?keyword=test"
    local url_443="https://127.0.0.1/music/api/v1/search/track?keyword=test"
    local resp_file
    resp_file="$(mktemp)"
    local time_total=""
    local resp_content=""

    # 优先通过 Unix socket 探测
    time_total="$(curl -s --max-time 8 --unix-socket "${TARGET_SOCK}" -w "%{time_total}" -o "${resp_file}" "http://localhost/music/api/v1/search/track?keyword=test" 2>/dev/null || echo "")"
    resp_content="$(cat "${resp_file}" 2>/dev/null || true)"

    if [ -z "${time_total}" ] || [ ! -s "${resp_file}" ]; then
        log_warn "socket 探测异常，尝试 fallback 访问网关 https 端口 (${GW_HTTPS_PORT})..."
        time_total="$(curl -sk --max-time 8 -w "%{time_total}" -o "${resp_file}" "${url_https}" 2>/dev/null || echo "")"
        if [ -z "${time_total}" ] || [ ! -s "${resp_file}" ]; then
            log_warn "网关 https 端口连接异常，尝试 fallback 访问 443 端口 (302 跳转)..."
            time_total="$(curl -skL --max-time 8 -w "%{time_total}" -o "${resp_file}" "${url_443}" 2>/dev/null || echo "99")"
        fi
        resp_content="$(cat "${resp_file}" 2>/dev/null || true)"
    fi
    rm -f "${resp_file}"

    if ! echo "${resp_content}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
        log_err "验收 6a 失败：未收到预期的 INVALID TOKEN 响应。实际响应: ${resp_content}"
        return 1
    fi

    local is_fast
    is_fast="$(awk -v t="${time_total}" 'BEGIN{print (t < 3.0) ? "1" : "0"}')"
    if [ "${is_fast}" != "1" ]; then
        log_err "验收 6a 失败：401 请求耗时过长 (${time_total}s >= 3s)，快速路径可能被阻塞！"
        return 1
    fi
    log_info "验收 6a 通过：INVALID TOKEN 正确透传，耗时 ${time_total}s (< 3s)。"

    # 6b. 网易云单源直链验收：搜索 -> 首个 song_id -> /api/v1/song/{id}/url -> HTTP 探活
    #     (Range: bytes=0-1024 -> 200/206 且 Content-Type 不是 text/html)
    log_info "验收 6b: 验证网易云 musicbox 搜索与直链探活 (Range bytes=0-1024 -> 200/206)..."

    local probe_keywords=("晴天" "海阔天空" "稻香")
    local search_any_result=0
    local stream_ok=0
    local quality="${FNMUSIC_NETEASE_QUALITY:-lossless}"

    # 搜索网易云，输出首个可播 song_id（musicbox 已按账号真实可播权益过滤；可能为空）
    netease_search_first_id() {
        local keyword="$1" encoded
        encoded="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "${keyword}" 2>/dev/null || true)"
        [ -z "${encoded}" ] && return 0
        curl -s --max-time 20 "${MUSICBOX_URL}/api/v1/search?keyword=${encoded}&limit=3&type=song" 2>/dev/null | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows=d.get('data') if isinstance(d,dict) else None
if not isinstance(rows,list):
    rows=d.get('songs') if isinstance(d,dict) else None
for it in (rows or []):
    it=it if isinstance(it,dict) else {}
    sid=str(it.get('song_id') or it.get('id') or '')
    if sid:
        print(sid); break" 2>/dev/null || true
    }

    # 解析指定 song_id 在 given quality 下的直链（/api/v1/song/{id}/url）；输出 URL（可能为空）
    resolve_netease_direct_url() {
        local sid="$1" q="$2"
        curl -s --max-time 20 "${MUSICBOX_URL}/api/v1/song/${sid}/url?quality=${q}" 2>/dev/null | python3 -c "import sys,json
def find_url(o):
    if isinstance(o,dict):
        if o.get('url'):
            return str(o['url'])
        for v in o.values():
            r=find_url(v)
            if r: return r
    elif isinstance(o,list):
        for v in o:
            r=find_url(v)
            if r: return r
    return None
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
u=find_url(d)
if u: print(u)" 2>/dev/null || true
    }

    # 直链 HTTP 探活：Range bytes=0-1024，期待 200/206 且 Content-Type 不是 text/html（防劫持到网页）
    probe_direct_url() {
        local url="$1" code ctype
        read -r code ctype <<< "$(curl -s -o /dev/null -w '%{http_code} %{content_type}' \
            -H 'Range: bytes=0-1024' --max-time 20 -L "${url}" 2>/dev/null || echo '000 -')"
        case "${code}" in
            200|206)
                case "${ctype}" in
                    text/html*)
                        log_warn "直链返回 text/html（疑似被重定向到登录/错误网页）：HTTP=${code}"
                        return 1 ;;
                    *)
                        log_info "直链探活成功：HTTP=${code} Content-Type=${ctype:-未知}"
                        return 0 ;;
                esac ;;
        esac
        log_warn "直链探活失败：HTTP=${code} Content-Type=${ctype:-未知}"
        return 1
    }

    local kw sid url q
    for kw in "${probe_keywords[@]}"; do
        sid="$(netease_search_first_id "${kw}")"
        [ -z "${sid}" ] && continue
        search_any_result=1
        # 无损需对应权益，失败时按 exhigh -> standard 依次降级重试直链
        url=""
        for q in "${quality}" exhigh standard; do
            url="$(resolve_netease_direct_url "${sid}" "${q}")"
            [ -n "${url}" ] && break
        done
        if [ -n "${url}" ] && probe_direct_url "${url}"; then
            log_info "验收 6b 通过：网易云搜索 -> 直链 -> HTTP 探活成功（关键词=${kw}, song_id=${sid}）。"
            stream_ok=1
            break
        fi
        log_warn "关键词=${kw}（song_id=${sid}）未能完成直链探活，尝试下一候选..."
    done

    # 外部音源网络波动不阻断部署：仅输出警告，绝不触发 return 1 / rollback
    if [ "${stream_ok}" -eq 0 ]; then
        if [ "${search_any_result}" -eq 0 ]; then
            log_warn "网易云 musicbox 搜索结果均为空：可能是外部网络异常、尚未扫码登录或网易限流。"
        else
            log_warn "所有候选歌曲均未能完成直链探活（网易云网络波动或登录权益受限，不阻断部署）。"
        fi
        log_warn "跳过在线播放自动验收，建议稍后在飞牛音乐 Web 端手动搜索试播验证。"
    fi

    # 6c. 登录态验收与提示（v2.0 单源：全部权益来自扫码登录的网易云账号）
    log_info "验收 6c: 校验网易云登录态与官方每日推荐..."
    verify_login_state
    return 0
}

# ------------------------------------------------------------------------------
# 登录态验收（网易云单源）：读取 /api/v1/auth/detail
#   - 未登录：醒目告警「免费曲目降级」模式（VIP/无损不可播、每日推荐不出），提示扫码登录
#   - 已登录：打印昵称与 VIP 剩余天数，并附带验收 /api/v1/recommend/daily（仅告警不判失败）
# ------------------------------------------------------------------------------
verify_login_state() {
    local detail_json parsed nickname vip_days warn_days daily_json daily_count
    detail_json="$(curl -s --max-time 10 "${MUSICBOX_URL}/api/v1/auth/detail" 2>/dev/null || true)"
    if [ -z "${detail_json}" ]; then
        log_warn "无法访问 ${MUSICBOX_URL}/api/v1/auth/detail，跳过登录态验收（未能确认网易云登录状态）。"
        return 0
    fi
    parsed="$(printf '%s' "${detail_json}" | python3 -c "
import sys,json,time
try:
    d=json.load(sys.stdin)
except Exception:
    print('ERR'); sys.exit(0)
data=d.get('data') if isinstance(d,dict) else None
if not isinstance(data,dict):
    print('ERR'); sys.exit(0)
if not data.get('logged_in'):
    print('OUT'); sys.exit(0)
nick=str(data.get('nickname') or data.get('user_id') or '')
ms=data.get('vip_expires_ms') or 0
try:
    ms=int(ms)
except Exception:
    ms=0
days=''
if ms>0:
    days=str(int((ms/1000.0 - time.time())/86400))
print('IN::'+nick+'::'+days)
" 2>/dev/null || echo ERR)"
    case "${parsed}" in
        OUT)
            log_warn "============================================================"
            log_warn "【网易云未登录】当前处于「免费曲目降级」模式："
            log_warn "  - VIP / 无损曲目无法在线播放，仅提供免费曲目；"
            log_warn "  - 网易云官方「每日推荐」不会出现在飞牛音乐中。"
            log_warn "  扫码登录（推荐）：./extend.sh --qr 或 ./netease_login.sh"
            log_warn "  浏览器图片扫码：http://<NAS_IP>:8770/api/v1/auth/login/qr.png"
            log_warn "  登录后重跑 ./extend.sh --force，即可恢复 VIP/无损与每日推荐。"
            log_warn "============================================================"
            ;;
        IN::*)
            nickname="${parsed#IN::}"; nickname="${nickname%%::*}"
            vip_days="${parsed##*::}"
            warn_days="${FNMUSIC_VIP_WARN_DAYS:-7}"
            if [ -n "${vip_days}" ]; then
                log_info "网易云已登录：昵称=${nickname:-未知}，VIP 剩余约 ${vip_days} 天。"
                if [ "${vip_days}" -le "${warn_days}" ] 2>/dev/null; then
                    log_warn "VIP 临期（≤${warn_days} 天）；如已配置 PushPlus 将收到微信提醒。"
                fi
            else
                log_info "网易云已登录：昵称=${nickname:-未知}（当前非 VIP 或无到期信息）。"
            fi
            # 顺带验收官方每日推荐能否拿到数据（拿不到只 warn 不 fail）
            daily_json="$(curl -s --max-time 40 "${MUSICBOX_URL}/api/v1/recommend/daily?limit=5" 2>/dev/null || true)"
            daily_count="$(printf '%s' "${daily_json}" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    print('ERR'); sys.exit(0)
if isinstance(d,dict) and d.get('ok') and isinstance(d.get('data'),list):
    print(len(d['data']))
else:
    print(str(d.get('error')) if isinstance(d,dict) and d.get('error') else 'ERR')
" 2>/dev/null || echo ERR)"
            case "${daily_count}" in
                ''|*[!0-9]*)
                    if [ "${daily_count}" = "not_logged_in" ]; then
                        log_warn "每日推荐接口返回未登录（not_logged_in），与 auth/detail 不一致；建议重新扫码登录后重试。"
                    else
                        log_warn "每日推荐未能拿到数据（返回：${daily_count:-空}）；不影响部署，可稍后自查 /api/v1/recommend/daily?limit=5。"
                    fi
                    ;;
                *)
                    if [ "${daily_count}" -gt 0 ]; then
                        log_info "每日推荐验收通过：/api/v1/recommend/daily 返回 ${daily_count} 首。"
                    else
                        log_warn "每日推荐返回 0 首（可能今日暂无推荐或网易限流），不影响部署。"
                    fi
                    ;;
            esac
            ;;
        *)
            log_warn "无法解析网易云登录态（auth/detail 返回格式异常），跳过登录验收。"
            ;;
    esac
    return 0
}

# ------------------------------------------------------------------------------
# 1. 预检
# ------------------------------------------------------------------------------
log_info "==> 步骤 1/5: 环境预检... (fnmusic-ext v${FNMUSIC_VERSION})"

# 1.1 检查 Python 3 与 venv 模块
if ! command -v python3 >/dev/null 2>&1; then
    log_err "【缺少基础依赖】系统未检测到 python3。"
    log_err "请先执行以下命令安装基础组件：sudo apt-get update && sudo apt-get install -y python3 python3-venv"
    exit 1
fi

if ! python3 -c "import venv" >/dev/null 2>&1; then
    log_err "【缺少基础依赖】系统 Python 缺少 venv 模块。"
    log_err "请先执行以下命令安装基础组件：sudo apt-get update && sudo apt-get install -y python3-venv"
    exit 1
fi

# 1.2 sudo 权限检查
if ! sudo -n true 2>/dev/null; then
    if [ -t 0 ]; then
        log_warn "需要管理员权限执行扩展配置，正在请求 sudo 授权..."
        sudo -v || {
            log_err "管理员权限获取失败，请确认当前用户具备 sudo 权限。"
            exit 1
        }
    else
        log_err "当前用户无法进行无密码 sudo 授权，请确认当前用户具备 sudo 权限。"
        exit 1
    fi
fi

# 1.3 检查飞牛音乐 socket 文件
if [ ! -S "${TARGET_SOCK}" ] && [ ! -S "${UPSTREAM_SOCK}" ]; then
    log_err "【前置条件未满足】未检测到飞牛音乐运行套接字。"
    log_err "请先在 fnOS 管理界面 -> 应用中心，安装并启动【飞牛音乐】应用后，再运行本脚本。"
    exit 1
fi

# 1.4 检查 / 自动拉起网易云 musicbox 音源（按 FNMUSIC_MODE 只走 docker 或 host，避免双轨冲突）
ensure_source() {
    local name="$1" url="$2" compose_svc="$3" unit="$4"
    if curl -sf --max-time 5 "${url}/healthz" >/dev/null 2>&1; then
        log_info "${name} 已就绪 (${url}/healthz)。"
        return 0
    fi
    log_warn "${name} 未就绪，正在拉起..."
    local mode="${SRC_MODE}"
    if [ -z "${mode}" ]; then
        if systemctl list-unit-files "${unit}" 2>/dev/null | grep -q "${unit}"; then
            mode="host"
        elif command -v docker >/dev/null 2>&1; then
            mode="docker"
        else
            mode="host"
        fi
    fi
    if [ "${mode}" = "docker" ]; then
        # 基础镜像源保障（国内镜像优先/官方兜底，见 ensure_base_image.sh），整次运行只执行一次
        if [ "${BASE_IMAGE_ENSURED:-0}" -ne 1 ]; then
            if bash "${BASE_DIR}/ensure_base_image.sh"; then
                BASE_IMAGE_ENSURED=1
            else
                log_err "基础镜像源探测失败，无法构建容器。"
                return 1
            fi
        fi
        reclaim_container "fnmusic-${name}" || return 1
        run_docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build "${compose_svc}" || true
    else
        if systemctl list-unit-files "${unit}" >/dev/null 2>&1; then
            sudo systemctl start "${unit}" 2>/dev/null || true
        fi
    fi
    local i
    for i in $(seq 1 60); do
        if curl -sf --max-time 3 "${url}/healthz" >/dev/null 2>&1; then
            log_info "${name} 已就绪。"
            return 0
        fi
        sleep 2
    done
    log_err "等待 ${name} healthz 超时 (${url}/healthz)。"
    return 1
}

if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    mkdir -p "${BASE_DIR}/musicbox-data/cache/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/config/netease-musicbox" \
        "${BASE_DIR}/musicbox-data/netease-musicbox"
    chmod -R 777 "${BASE_DIR}/musicbox-data" 2>/dev/null || true
    if ! ensure_source "musicbox" "${MUSICBOX_URL}" "musicbox" "fnmusic-musicbox.service"; then
        exit 1
    fi
fi

# 1.5 检查 Python 虚拟环境与依赖
if [ ! -f "${BASE_DIR}/.venv-proxy/bin/python" ]; then
    log_info "创建 .venv-proxy 虚拟环境..."
    python3 -m venv "${BASE_DIR}/.venv-proxy"
    "${BASE_DIR}/.venv-proxy/bin/pip" install -r "${BASE_DIR}/proxy/requirements.txt" -i "${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
fi

# 1.6 编译与语法检查
python3 -m py_compile "${BASE_DIR}/proxy/app.py" "${BASE_DIR}/proxy/recommend.py"
bash -n "${BASE_DIR}/proxy/run_proxy.sh"

# ------------------------------------------------------------------------------
# 2. 幂等性检查
# ------------------------------------------------------------------------------
log_info "==> 步骤 2/5: 幂等性检查..."
HEALTH_CHECK="$(curl -s --max-time 3 --unix-socket "${TARGET_SOCK}" http://localhost/_ext/healthz 2>/dev/null || true)"

if [ "${FORCE_RELOAD}" -eq 1 ]; then
    log_info "已指定 --force：跳过幂等提前退出，将重写 unit 并重启代理以加载最新 .env。"
elif echo "${HEALTH_CHECK}" | grep -q '"upstream":[[:space:]]*"ok"'; then
    log_info "检测到代理服务已在运行且上游健康 (处于扩展接管态)。"
    log_info "直接运行验收测试确认状态..."
    if verify_acceptance; then
        log_info "============================================================"
        log_info "fnmusic-ext 当前已处于扩展态且运行正常，无需重复操作！"
        log_info "============================================================"
        exit 0
    else
        log_warn "现有扩展态验收未通过，将重启服务重新接管..."
    fi
fi

# ------------------------------------------------------------------------------
# 3. 安装并启动 systemd unit（按当前目录生成，禁止写死个人路径）
# ------------------------------------------------------------------------------
log_info "==> 步骤 3/5: 安装 systemd 服务并启动接管..."
UNIT_TMP="$(mktemp)"
cat > "${UNIT_TMP}" <<EOF
[Unit]
Description=fnmusic-ext Proxy Service (Socket Takeover)
After=network.target docker.service

[Service]
Type=simple
User=root
WorkingDirectory=${BASE_DIR}
ExecStart=${BASE_DIR}/proxy/run_proxy.sh
ExecStartPost=/bin/sh -c 'for i in \$(seq 1 65); do [ -S /var/run/trim_music.socket ] && chmod 666 /var/run/trim_music.socket && exit 0; sleep 1; done; exit 1'
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=FNMUSIC_HOME=${BASE_DIR}
EnvironmentFile=-${BASE_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF
sudo cp "${UNIT_TMP}" /etc/systemd/system/fnmusic-ext.service
rm -f "${UNIT_TMP}"
sudo systemctl daemon-reload

if systemctl is-active --quiet fnmusic-ext.service 2>/dev/null; then
    log_info "重启 fnmusic-ext 服务..."
    sudo systemctl restart fnmusic-ext.service
else
    log_info "启用并启动 fnmusic-ext 服务..."
    sudo systemctl enable --now fnmusic-ext.service
fi

# ------------------------------------------------------------------------------
# 4. 等待接管完成与健康检查
# ------------------------------------------------------------------------------
log_info "==> 步骤 4/5: 等待代理服务接管完成并就绪..."
READY=0
for i in $(seq 1 30); do
    STATUS_JSON="$(curl -s --max-time 2 --unix-socket "${TARGET_SOCK}" http://localhost/_ext/healthz 2>/dev/null || true)"
    # v2.0 单源：接管就绪 = upstream 透传 ok 且 musicbox（网易云）ok；healthz 已按单源重构，仅保留 musicbox 字段。
    # 不以聚合 "ok":true 作门槛——未登录且 free_only=false 时 ok 会为 false，但接管本身仍可用（登录态由 6c 另行告警，不阻断）。
    if echo "${STATUS_JSON}" | grep -q '"upstream":[[:space:]]*"ok"' \
        && echo "${STATUS_JSON}" | grep -q '"musicbox":[[:space:]]*"ok"'; then
        READY=1
        break
    fi
    sleep 1
done

if [ "${READY}" -ne 1 ]; then
    log_err "等待接管超时 (30s) 或 healthz 未通过。当前探测响应: ${STATUS_JSON:-无响应}"
    rollback
fi

log_info "Unix socket 接管成功且健康探测通过。"

# ------------------------------------------------------------------------------
# 5. 验收测试与自动回滚
# ------------------------------------------------------------------------------
log_info "==> 步骤 5/5: 验收链路连通性..."
if ! verify_acceptance; then
    rollback
fi

log_info "============================================================"
log_info "fnmusic-ext v${FNMUSIC_VERSION} 扩展已成功部署并生效！"
log_info "架构：Unix Socket 接管 (零侵入，不修改 nginx 配置)"
log_info "网易云在线音源搜索合并、在线播放与元数据代理已就绪。"
log_info "------------------------------------------------------------"
log_info "【后续验证与使用指引】"
log_info "1. 验证搜索与播放："
log_info "   打开飞牛音乐 Web 端或手机 App，搜索歌曲（如“晴天”或“周杰伦”），"
log_info "   点击在线源歌曲试听，确认可以流畅播放并显示歌词与封面。"
log_info "2. 网易云扫码登录（强烈推荐）："
log_info "   v2.0 起所有在线曲目都来自扫码登录的私人网易云账号权益，未登录仅播免费曲目："
log_info "   • 命令行扫码登录（推荐）: ./extend.sh --qr 或 ./netease_login.sh"
log_info "     （自动展示二维码、轮询登录状态、过期自动刷新，支持随时 Ctrl+C 跳过）"
log_info "   • 浏览器图片扫码（备选）: http://<NAS_IP>:8770/api/v1/auth/login/qr.png"
log_info "   • 查询登录与 VIP 状态: curl -s ${MUSICBOX_URL}/api/v1/auth/detail"
log_info "3. 健康检查与运维："
log_info "   • 探测状态: curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz"
log_info "   • 查看日志: sudo journalctl -u fnmusic-ext -f"
log_info "   • 一键还原: ./restore.sh (一键无损切回官方原生直连)"
log_info "============================================================"
