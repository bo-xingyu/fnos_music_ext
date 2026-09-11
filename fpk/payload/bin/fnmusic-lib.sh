#!/bin/bash
# ==============================================================================
# fnmusic-ext fpk 共享函数库
#
# 设计要点：
#   1. 生命周期脚本（cmd/*）以 root 运行，因为「接管 /var/run/trim_music.socket」
#      必须在 /var/run 下重命名与新建 socket 文件，包用户没有该目录写权限。
#   2. 网易云音源服务（musicbox）不对外暴露接管能力，按官方要求降权到
#      $TRIM_USERNAME（专用包用户）运行。
#   3. 代理进程无法降权：uvicorn 需要在 /var/run 下创建 bind socket，非 root
#      缺少目录写权限。这是 socket 接管架构的固有代价，已在 README / fpk
#      文档中明示。
#   4. 全部运行时代码 stage 到 $RUN_DIR（= $TRIM_PKGVAR/app），使其目录布局
#      与仓库根完全一致，从而**原样复用** proxy/run_proxy.sh 与 restore.sh 中
#      已经过生产验证的 socket 接管 / 复位逻辑，不在 fpk 里另写一份。
#
# 依赖：仅 bash / coreutils / curl / jq（jq 缺失时扫码登录脚本会自行降级）
# ==============================================================================

APP_NAME="fnmusicext"
PYTHON_APP="python312"
PYTHON_BIN="/var/apps/${PYTHON_APP}/target/bin"

APPDEST="${TRIM_APPDEST:-/var/apps/${APP_NAME}/target}"
# 官方框架：var -> /vol{n}/@appdata/{appname}，对应 TRIM_PKGVAR
# （仅在环境变量缺失时兜底；正常由飞牛注入，不要依赖这个默认值）
PKGVAR="${TRIM_PKGVAR:-$(ls -d /vol*/@appdata/${APP_NAME} 2>/dev/null | head -n 1)}"
PKGVAR="${PKGVAR:-/vol1/@appdata/${APP_NAME}}"
RUN_DIR="${PKGVAR}/app"
LOG_DIR="${PKGVAR}/logs"
ENV_FILE="${RUN_DIR}/.env"

PROXY_PID="${PKGVAR}/proxy.pid"
MUSICBOX_PID="${PKGVAR}/musicbox.pid"
UI_PID="${PKGVAR}/ui.pid"
PROXY_LOG="${LOG_DIR}/proxy.log"
MUSICBOX_LOG="${LOG_DIR}/musicbox.log"
UI_LOG="${LOG_DIR}/ui.log"
INFO_LOG="${LOG_DIR}/info.log"

# 管理页面的 unix socket。官方要求 gatewaySocket 放在已安装应用的 target 目录下，
# 因此这里用 APPDEST 而不是 PKGVAR；网关按 ui/config 里的 gatewaySocket 文件名找它。
UI_SOCK="${APPDEST}/ui.sock"

TARGET_SOCK="/var/run/trim_music.socket"
UPSTREAM_SOCK="/var/run/trim_music_upstream.socket"
MUSICBOX_PORT="${FNMUSIC_MUSICBOX_PORT:-8770}"
MUSICBOX_URL="http://127.0.0.1:${MUSICBOX_PORT}"

lib_log() {
    mkdir -p "${LOG_DIR}" 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$$] $*" >> "${INFO_LOG}" 2>/dev/null
}

# 生命周期脚本失败时，用户可见错误必须写入 TRIM_TEMP_LOGFILE（官方约定）
lib_fail() {
    local msg="$1"
    lib_log "ERROR: ${msg}"
    if [ -n "${TRIM_TEMP_LOGFILE:-}" ]; then
        echo "${msg}" >> "${TRIM_TEMP_LOGFILE}" 2>/dev/null
    fi
    echo "[fnmusic-ext] ${msg}" >&2
}

lib_warn() {
    lib_log "WARN: $*"
    if [ -n "${TRIM_TEMP_LOGFILE:-}" ]; then
        echo "警告: $*" >> "${TRIM_TEMP_LOGFILE}" 2>/dev/null
    fi
    echo "[fnmusic-ext] 警告: $*" >&2
}

lib_python() {
    # 官方运行时包路径优先；缺失时回落到系统 python3（便于开发机自测）
    if [ -x "${PYTHON_BIN}/python3" ]; then
        echo "${PYTHON_BIN}/python3"
    else
        command -v python3 || echo ""
    fi
}

lib_check_python() {
    local py
    py="$(lib_python)"
    if [ -z "${py}" ]; then
        lib_fail "找不到 Python 运行时。请确认已在应用中心安装依赖包 ${PYTHON_APP}，或系统存在 python3。"
        return 1
    fi
    if [ ! -x "${PYTHON_BIN}/python3" ]; then
        lib_warn "未检测到 ${PYTHON_BIN}/python3，改用系统 python3（$(py_ver "${py}")）。生产环境建议安装 ${PYTHON_APP} 依赖包。"
    fi
    echo "${py}"
    return 0
}

py_ver() {
    "$1" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null || echo "unknown"
}

lib_pid_alive() {
    local file="$1"
    [ -r "${file}" ] || return 1
    local pid
    pid="$(head -n 1 "${file}" | tr -d '[:space:]')"
    [ -n "${pid}" ] || return 1
    kill -0 "${pid}" 2>/dev/null
}

# TERM -> 等待 -> KILL 两段式停止（官方 native 案例同款模式）
lib_stop_pid() {
    local name="$1" file="$2" wait_s="${3:-15}"
    if ! lib_pid_alive "${file}"; then
        rm -f "${file}" 2>/dev/null
        lib_log "${name} 未在运行，跳过停止"
        return 0
    fi
    local pid count=0
    pid="$(head -n 1 "${file}" | tr -d '[:space:]')"
    lib_log "停止 ${name} (pid=${pid})，发送 TERM..."
    kill -TERM "${pid}" 2>/dev/null || true
    while kill -0 "${pid}" 2>/dev/null && [ "${count}" -lt "${wait_s}" ]; do
        sleep 1
        count=$((count + 1))
    done
    if kill -0 "${pid}" 2>/dev/null; then
        lib_log "${name} ${wait_s}s 内未退出，发送 KILL"
        kill -KILL "${pid}" 2>/dev/null || true
        sleep 1
    fi
    rm -f "${file}" 2>/dev/null
    lib_log "${name} 已停止"
    return 0
}

lib_wait_http() {
    local url="$1" tries="${2:-30}" interval="${3:-1}" i=0
    while [ "${i}" -lt "${tries}" ]; do
        if curl -s --max-time 3 -f "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep "${interval}"
        i=$((i + 1))
    done
    return 1
}

lib_read_env_value() {
    local key="$1" default="${2:-}"
    if [ -r "${ENV_FILE}" ]; then
        local v
        v="$(sed -n "s/^${key}='\{0,1\}\([^']*\)'\{0,1\}\s*$/\1/p" "${ENV_FILE}" 2>/dev/null | tail -n 1)"
        if [ -n "${v}" ]; then
            echo "${v}"
            return 0
        fi
    fi
    echo "${default}"
}

# ------------------------------------------------------------------------------
# 降级运行：把不需要 root 的进程切到专用包用户
# ------------------------------------------------------------------------------

# 探测当前是否【能够】降权到目标用户。用一条空命令试探，避免把子命令自身的
# 失败误判为"降权不可用"。结果缓存，避免每个进程都试一次。
_DROP_CAPABLE=""
lib_can_drop_to() {
    local target="$1"
    if [ -n "${_DROP_CAPABLE}" ]; then
        # 已探测过：no 表示不可降权，其余（yes / yes:su）表示可降权
        [ "${_DROP_CAPABLE}" != "no" ]
        return $?
    fi
    _DROP_CAPABLE="no"
    if [ "$(id -un)" = "root" ] && id "${target}" >/dev/null 2>&1; then
        if command -v runuser >/dev/null 2>&1 && runuser -u "${target}" -- /bin/true >/dev/null 2>&1; then
            _DROP_CAPABLE="yes"
        elif command -v su >/dev/null 2>&1 && su -s /bin/bash "${target}" -c '/bin/true' >/dev/null 2>&1; then
            _DROP_CAPABLE="yes:su"
        fi
    fi
    [ "${_DROP_CAPABLE}" != "no" ]
}

lib_as_app_user() {
    # 用法：lib_as_app_user <cmd> [args...]
    #
    # 官方要求「长期运行并对外提供访问的进程应尽可能以非 root 用户运行」，
    # 因此音源服务尽量降权。降权确实不可用时（受限容器 / 包用户未创建）
    # 如实告警后继续以当前身份运行，而不是让整个应用启动失败。
    local target="${TRIM_USERNAME:-}"
    if [ -z "${target}" ] || [ "$(id -un)" = "${target}" ]; then
        # 没有可降权的目标，或本来就是该用户 —— 无需降权
        "$@"
        return $?
    fi

    # 先调用探测（会设置全局 _DROP_CAPABLE），再读结果——
    # 不能放进 $( ) 里，子 shell 会把缓存丢掉。
    local can=1
    lib_can_drop_to "${target}" && can=0

    if [ "${can}" -eq 0 ]; then
        case "${_DROP_CAPABLE}" in
            yes)
                runuser -u "${target}" -- "$@"
                return $?
                ;;
            yes:su)
                su -s /bin/bash "${target}" -c "$(printf '%q ' "$@")"
                return $?
                ;;
        esac
    fi

    lib_warn "无法降权到用户 ${target}（受限环境或该用户不存在），改以 $(id -un) 运行：$(basename "$1")"
    "$@"
}

# ------------------------------------------------------------------------------
# 后台守护进程启动：pidfile 必须指向真身
# ------------------------------------------------------------------------------
#
# 直接把「函数调用」放到后台再取 $! 是错的：$! 拿到的是执行该函数的 bash 子壳，
# 子壳随后退出、真正的服务进程被 reparent 到 init，于是 stop 时 kill 的是一个
# 早已死亡的 PID，服务进程永久泄漏。降权场景更糟——runuser/su 是中间父进程，
# 且默认不转发信号。
#
# 解法：让子进程【自己】把 $$ 写进 pidfile，然后 exec 成目标程序。
# bash -c 里的 $$ 与 exec 后的进程 PID 相同，所以 pidfile 永远指向服务真身，
# 中间的 runuser/su 是否存活都不影响停机准确性。
lib_spawn() {
    # 用法: lib_spawn <pidfile> <logfile> [--as-root] <cmd> [args...]
    #   --as-root  显式要求以当前(root)身份运行，不降权。
    #              只用于必须在 /var/run 下创建 bind socket 的代理进程。
    local pidfile="$1" logfile="$2"
    shift 2
    local force_root=0
    if [ "${1:-}" = "--as-root" ]; then
        force_root=1
        shift
    fi
    mkdir -p "$(dirname "${logfile}")" 2>/dev/null

    local inner script
    inner="$(printf '%q ' "$@")"
    script="printf '%s' \$\$ > $(printf '%q' "${pidfile}"); exec ${inner}"

    local target="${TRIM_USERNAME:-}"
    if [ "${force_root}" -eq 0 ] && [ -n "${target}" ] && [ "$(id -un)" != "${target}" ]; then
        local can=1
        lib_can_drop_to "${target}" && can=0
        if [ "${can}" -eq 0 ] && [ "${_DROP_CAPABLE}" = "yes" ]; then
            runuser -u "${target}" -- bash -c "${script}" >> "${logfile}" 2>&1 &
            return 0
        elif [ "${can}" -eq 0 ]; then
            su -s /bin/bash "${target}" -c "$(printf '%q ' bash -c "${script}")" \
                >> "${logfile}" 2>&1 &
            return 0
        fi
        lib_warn "无法降权到 ${target}，以 $(id -un) 启动：$(basename "$1")"
    fi
    bash -c "${script}" >> "${logfile}" 2>&1 &
    return 0
}

lib_wait_pidfile() {
    # pidfile 由子进程自己写入，启动瞬间可能还没落盘，最多等 5s
    local file="$1" i=0
    while [ "${i}" -lt 50 ]; do
        if [ -s "${file}" ] && lib_pid_alive "${file}"; then
            return 0
        fi
        sleep 0.1
        i=$((i + 1))
    done
    [ -s "${file}" ] && lib_pid_alive "${file}"
}

lib_kill_stale_by_sock() {
    # 兜底清理：pidfile 丢失（例如启动竞态下被误删）但进程仍在监听我们的 socket 时，
    # 按【本应用自己的 socket 绝对路径】精确匹配并终止，绝不波及任何其它 uvicorn。
    #
    # 没有这道网，一次启动竞态就会留下一个永久孤儿进程：它占着 ui.sock，
    # 下次启动又因 socket 已存在而行为诡异，用户只能重启整机。
    local sock="$1"
    [ -n "${sock}" ] || return 0
    [ -S "${sock}" ] || return 0

    local needles=("uds ${sock}" "uds=${sock}")
    local pids="" line pid args needle hit
    while IFS= read -r line; do
        pid="${line%% *}"
        args="${line#* }"
        hit=0
        for needle in "${needles[@]}"; do
            case "${args}" in *"${needle}"*) hit=1; break ;; esac
        done
        [ "${hit}" -eq 1 ] || continue
        case "${pid}" in ''|*[!0-9]*) continue ;; esac
        pids="${pids} ${pid}"
    done < <(ps -eo pid=,args= 2>/dev/null)

    pids="$(echo "${pids}" | tr -s ' ')"
    [ -n "${pids// /}" ] || return 0

    lib_log "发现绑定 ${sock} 的孤儿进程:${pids}，执行清理"
    # 先 TERM 再 KILL，并跳过自己与父进程，避免自杀
    local self=$$ parent=${PPID:-0}
    for pid in ${pids}; do
        [ "${pid}" = "${self}" ] && continue
        [ "${pid}" = "${parent}" ] && continue
        kill -TERM "${pid}" 2>/dev/null || true
    done
    sleep 2
    for pid in ${pids}; do
        [ "${pid}" = "${self}" ] && continue
        [ "${pid}" = "${parent}" ] && continue
        kill -0 "${pid}" 2>/dev/null && {
            lib_log "孤儿进程 ${pid} 未响应 TERM，发送 KILL"
            kill -KILL "${pid}" 2>/dev/null || true
        }
    done
    rm -f "${sock}" 2>/dev/null
    return 0
}

lib_rotate_logs() {
    # 日志保留策略：单文件超上限就轮转成 .1 备份，超龄文件直接删。
    # 默认 10MB / 30 天，可由 .env 的 FNMUSIC_LOG_MAX_MB / FNMUSIC_LOG_MAX_DAYS 覆盖。
    # 只在【启动前】调用（此时没有进程持有日志 fd，rename 安全）；
    # 运行期由管理页面进程用 loghouse.scan() 就地截断，避免 inode 被带走。
    local max_mb max_days keep
    max_mb="$(lib_read_env_value FNMUSIC_LOG_MAX_MB 10)"
    max_days="$(lib_read_env_value FNMUSIC_LOG_MAX_DAYS 30)"
    case "${max_mb}" in ''|*[!0-9.]*) max_mb=10 ;; esac
    case "${max_days}" in ''|*[!0-9.]*) max_days=30 ;; esac
    keep=1

    [ -d "${LOG_DIR}" ] || return 0

    local max_bytes
    max_bytes="$(awk -v m="${max_mb}" 'BEGIN{printf "%d", m*1048576}')"
    local cutoff
    cutoff="$(awk -v d="${max_days}" 'BEGIN{printf "%d", systime() - d*86400}')" 2>/dev/null || cutoff=0

    local f name size mtime freed=0 acted=0
    for f in "${LOG_DIR}"/*.log "${LOG_DIR}"/*.log.*; do
        [ -f "${f}" ] || continue
        name="$(basename "${f}")"
        size="$(stat -c %s "${f}" 2>/dev/null || echo 0)"
        mtime="$(stat -c %Y "${f}" 2>/dev/null || echo 0)"

        # 超龄：备份直接删，活跃日志清空（可能仍有进程持有 fd，不能 unlink）
        if [ "${max_days}" != "0" ] && [ "${cutoff}" -gt 0 ] && [ "${mtime}" -lt "${cutoff}" ]; then
            case "${name}" in
                *.log.[0-9]*)
                    rm -f "${f}" 2>/dev/null && { freed=$((freed + size)); acted=$((acted+1)); }
                    lib_log "清理超龄备份日志 ${name}（$((size / 1024)) KB）"
                    continue ;;
                *)
                    : > "${f}" 2>/dev/null && { freed=$((freed + size)); acted=$((acted+1)); }
                    lib_log "清空超龄日志 ${name}（$((size / 1024)) KB）"
                    continue ;;
            esac
        fi

        # 超大：活跃日志轮转成 .1（启动前无写入端持有 fd，rename 安全）
        if [ "${max_mb}" != "0" ] && [ "${size}" -gt "${max_bytes}" ]; then
            case "${name}" in
                *.log)
                    rm -f "${f}.${keep}" 2>/dev/null
                    for ((n = keep; n > 1; n--)); do
                        [ -f "${f}.$((n - 1))" ] && mv -f "${f}.$((n - 1))" "${f}.${n}" 2>/dev/null
                    done
                    mv -f "${f}" "${f}.1" 2>/dev/null || continue
                    : > "${f}" 2>/dev/null
                    chmod 644 "${f}" 2>/dev/null
                    freed=$((freed + size)); acted=$((acted + 1))
                    lib_log "轮转超大日志 ${name}（$((size / 1048576)) MB > ${max_mb} MB）"
                    ;;
                *)
                    rm -f "${f}" 2>/dev/null && { freed=$((freed + size)); acted=$((acted+1)); }
                    ;;
            esac
        fi
    done
    [ "${acted}" -gt 0 ] && lib_log "日志清理完成：${acted} 个文件，回收 $((freed / 1048576)) MB"
    return 0
}

lib_probe_proxy() {
    # 代理自身健康端点
    local resp
    resp="$(curl -s --max-time 3 --unix-socket "${TARGET_SOCK}" http://localhost/_ext/healthz 2>/dev/null || true)"
    case "${resp}" in
        *'"upstream"'*) return 0 ;;
        *) return 1 ;;
    esac
}
