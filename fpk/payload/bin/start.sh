#!/bin/bash
# ==============================================================================
# fnmusic-ext fpk：启动
#   1. 网易云音源服务（降权到专用包用户，监听 8770）
#   2. Unix Socket 接管 + 代理服务（必须 root，见 fnmusic-lib.sh 头部说明）
#
# 幂等：已在运行则直接返回成功。
# ==============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./fnmusic-lib.sh
. "${SCRIPT_DIR}/fnmusic-lib.sh"

start_musicbox() {
    if lib_pid_alive "${MUSICBOX_PID}"; then
        lib_log "musicbox 已在运行 (pid=$(head -n 1 "${MUSICBOX_PID}"))"
        return 0
    fi
    local venv="${RUN_DIR}/.venv-musicbox"
    if [ ! -x "${venv}/bin/uvicorn" ]; then
        lib_fail "未找到 ${venv}/bin/uvicorn，音源服务无法启动。请在应用中心重新安装本应用以重建虚拟环境。"
        return 1
    fi
    local bind
    bind="$(lib_read_env_value FNMUSIC_MUSICBOX_BIND "0.0.0.0")"
    lib_log "启动 musicbox 音源服务 ${bind}:${MUSICBOX_PORT}（降权到 ${TRIM_USERNAME:-当前用户}）"

    # 音源服务不需要 root：只监听 TCP 端口、读写自己数据目录，
    # 按官方「长期运行并对外提供访问的进程应尽可能以非 root 运行」要求降权。
    # PID 由子进程自己写入 pidfile（见 lib_spawn 说明），确保 stop 能杀到真身。
    lib_spawn "${MUSICBOX_PID}" "${MUSICBOX_LOG}" env \
        PATH="${PYTHON_BIN}:${PATH}" \
        PYTHONUNBUFFERED=1 \
        XDG_DATA_HOME="${MUSICBOX_DATA_DIR}" \
        XDG_CACHE_HOME="${MUSICBOX_DATA_DIR}/cache" \
        XDG_CONFIG_HOME="${MUSICBOX_DATA_DIR}/config" \
        FNMUSIC_FREE_ONLY_ON_LOGOUT="$(lib_read_env_value FNMUSIC_FREE_ONLY_ON_LOGOUT true)" \
        FNMUSIC_LOG_QUIET="$(lib_read_env_value FNMUSIC_LOG_QUIET true)" \
        "${venv}/bin/uvicorn" app:app \
            --app-dir "${RUN_DIR}/musicbox-service" \
            --host "${bind}" \
            --port "${MUSICBOX_PORT}"

    if ! lib_wait_pidfile "${MUSICBOX_PID}"; then
        lib_fail "musicbox 启动后未能写入 PID 或立刻退出。日志: ${MUSICBOX_LOG}"
        tail -n 20 "${MUSICBOX_LOG}" >> "${TRIM_TEMP_LOGFILE:-/dev/null}" 2>/dev/null
        return 1
    fi

    if lib_wait_http "${MUSICBOX_URL}/healthz" 30 1; then
        lib_log "musicbox 就绪 ${MUSICBOX_URL}/healthz (pid=$(head -n 1 "${MUSICBOX_PID}"))"
        return 0
    fi
    # 音源未就绪不致命：代理仍可接管并透传本地曲库，但在线功能会不可用
    if lib_pid_alive "${MUSICBOX_PID}"; then
        lib_warn "musicbox 30s 内未通过 healthz，仍在运行中；在线音源可能暂不可用。日志: ${MUSICBOX_LOG}"
        return 0
    fi
    lib_fail "musicbox 启动后立刻退出。日志: ${MUSICBOX_LOG}"
    tail -n 20 "${MUSICBOX_LOG}" >> "${TRIM_TEMP_LOGFILE:-/dev/null}" 2>/dev/null
    return 1
}

start_musicsource() {
    # 扩展音源（QQ/酷狗/酷我/汽水）。开关关闭或未打包装载荷时静默跳过。
    local extra_on
    extra_on="$(lib_read_env_value FNMUSIC_EXTRA_ENABLED true)"
    case "$(echo "${extra_on}" | tr '[:upper:]' '[:lower:]')" in
        true|1|yes|on) ;;
        *)
            lib_log "扩展音源已关闭（FNMUSIC_EXTRA_ENABLED=${extra_on}），跳过 musicsource"
            return 0
            ;;
    esac
    if [ ! -d "${RUN_DIR}/musicsource-service" ]; then
        lib_log "未找到 musicsource-service 载荷，跳过扩展音源"
        return 0
    fi
    if lib_pid_alive "${MUSICSOURCE_PID}"; then
        lib_log "musicsource 已在运行 (pid=$(head -n 1 "${MUSICSOURCE_PID}"))"
        return 0
    fi
    local venv="${RUN_DIR}/.venv-musicsource"
    if [ ! -x "${venv}/bin/uvicorn" ]; then
        lib_warn "未找到 ${venv}/bin/uvicorn，扩展音源暂不可用（网易云音源不受影响）"
        return 0
    fi
    local bind
    bind="$(lib_read_env_value FNMUSIC_MUSICSOURCE_BIND 127.0.0.1)"
    lib_log "启动 musicsource 扩展音源 ${bind}:${MUSICSOURCE_PORT}（降权）"
    lib_spawn "${MUSICSOURCE_PID}" "${MUSICSOURCE_LOG}" env \
        PATH="${PYTHON_BIN}:${PATH}" \
        PYTHONUNBUFFERED=1 \
        FNMUSIC_MUSICSOURCE_DATA="${MUSICSOURCE_DATA_DIR}" \
        FNMUSIC_EXTRA_SOURCES="$(lib_read_env_value FNMUSIC_EXTRA_SOURCES qq,kugou,kuwo,qishui)" \
        FNMUSIC_QQ_ENABLED="$(lib_read_env_value FNMUSIC_QQ_ENABLED true)" \
        FNMUSIC_KUGOU_ENABLED="$(lib_read_env_value FNMUSIC_KUGOU_ENABLED true)" \
        FNMUSIC_KUWO_ENABLED="$(lib_read_env_value FNMUSIC_KUWO_ENABLED true)" \
        FNMUSIC_QISHUI_ENABLED="$(lib_read_env_value FNMUSIC_QISHUI_ENABLED true)" \
        FNMUSIC_QISHUI_API_BASE="$(lib_read_env_value FNMUSIC_QISHUI_API_BASE "")" \
        FNMUSIC_EXTRA_API_BASE="$(lib_read_env_value FNMUSIC_EXTRA_API_BASE "")" \
        FNMUSIC_LOG_QUIET="$(lib_read_env_value FNMUSIC_LOG_QUIET true)" \
        "${venv}/bin/uvicorn" app:app \
            --app-dir "${RUN_DIR}/musicsource-service" \
            --host "${bind}" \
            --port "${MUSICSOURCE_PORT}"

    if ! lib_wait_pidfile "${MUSICSOURCE_PID}"; then
        lib_warn "musicsource 启动失败，扩展音源暂不可用。日志: ${MUSICSOURCE_LOG}"
        return 0
    fi
    if lib_wait_http "${MUSICSOURCE_URL}/healthz" 20 1; then
        lib_log "musicsource 就绪 ${MUSICSOURCE_URL}/healthz (pid=$(head -n 1 "${MUSICSOURCE_PID}"))"
        return 0
    fi
    if lib_pid_alive "${MUSICSOURCE_PID}"; then
        lib_warn "musicsource 20s 内未通过 healthz，仍在运行中。日志: ${MUSICSOURCE_LOG}"
        return 0
    fi
    lib_warn "musicsource 启动后立刻退出。日志: ${MUSICSOURCE_LOG}"
    return 0
}

start_proxy() {
    if lib_pid_alive "${PROXY_PID}" && lib_probe_proxy; then
        lib_log "代理已在运行且接管正常 (pid=$(head -n 1 "${PROXY_PID}"))"
        return 0
    fi
    if lib_pid_alive "${PROXY_PID}"; then
        # 进程活着但接管丢失：官方 trim-music 重启时重绑了 trim_music.socket，
        # 原路径已不是我们的监听（2026-09-12「异常退出」事故的形态）。
        # 必须先停掉这个"僵尸代理"再重新接管，否则它会永远占着
        # trim_music_upstream.socket，新的代理也接不上。
        lib_warn "代理进程存活但 socket 接管已丢失，先停止旧代理再重新接管"
        lib_stop_pid "proxy" "${PROXY_PID}" 15
    fi
    # pidfile 丢失但进程仍在监听我们的 socket 时，按路径精确清理，杜绝孤儿
    lib_kill_stale_by_sock "${TARGET_SOCK}"
    # 一律用 bash 显式解释执行：tar 包内脚本是 644（无执行位），且社区实测部分
    # 文件系统上 chmod +x 可能不生效，靠 -x 判断会把可运行的脚本误判为缺失。
    if [ ! -f "${RUN_DIR}/proxy/run_proxy.sh" ]; then
        lib_fail "未找到 ${RUN_DIR}/proxy/run_proxy.sh，无法接管 socket。"
        return 1
    fi
    if [ ! -f "${RUN_DIR}/.venv-proxy/bin/uvicorn" ]; then
        lib_fail "未找到 ${RUN_DIR}/.venv-proxy/bin/uvicorn，请在应用中心重新安装本应用以重建虚拟环境。"
        return 1
    fi
    if [ ! -S "${TARGET_SOCK}" ] && [ ! -S "${UPSTREAM_SOCK}" ]; then
        lib_fail "未探测到飞牛音乐套接字 ${TARGET_SOCK}。请先在应用中心安装并启动「飞牛音乐」，然后重新启动本应用。"
        return 1
    fi

    lib_log "启动代理（socket 接管）..."
    # run_proxy.sh 内部实现了幂等接管：探测 trim/proxy/stale 三态、
    # 平滑 mv 官方 socket 到 upstream、再以 --uds 绑定原路径。
    # 必须 --as-root：uvicorn 要在 /var/run 下创建 bind socket，包用户没有该目录写权限。
    lib_spawn "${PROXY_PID}" "${PROXY_LOG}" --as-root \
        env PATH="${PYTHON_BIN}:${PATH}" bash "${RUN_DIR}/proxy/run_proxy.sh"

    # run_proxy.sh 最多等 60s 探测官方 socket，这里给足启动窗口
    local i=0
    while [ "${i}" -lt 75 ]; do
        if lib_probe_proxy; then
            # 官方 nginx 需要能连上该 socket
            chmod 666 "${TARGET_SOCK}" 2>/dev/null || true
            lib_log "代理接管成功 ${TARGET_SOCK} (pid=$(head -n 1 "${PROXY_PID}"))"
            log_login_hint
            return 0
        fi
        if ! lib_pid_alive "${PROXY_PID}"; then
            break
        fi
        sleep 1
        i=$((i + 1))
    done

    lib_fail "代理未能完成 socket 接管（75s 超时或进程提前退出）。日志: ${PROXY_LOG}"
    tail -n 25 "${PROXY_LOG}" >> "${TRIM_TEMP_LOGFILE:-/dev/null}" 2>/dev/null
    rm -f "${PROXY_PID}" 2>/dev/null
    return 1
}

log_login_hint() {
    # 只在日志里给提示，绝不因为未登录就判定启动失败
    local detail
    detail="$(curl -s --max-time 5 "${MUSICBOX_URL}/api/v1/auth/detail" 2>/dev/null || true)"
    if [ -z "${detail}" ]; then
        return 0
    fi
    local logged_in
    logged_in="$(printf '%s' "${detail}" | jq -r '.data.logged_in // false' 2>/dev/null || echo false)"
    if [ "${logged_in}" = "true" ]; then
        local nick vip
        nick="$(printf '%s' "${detail}" | jq -r '.data.nickname // "已登录用户"' 2>/dev/null || echo '已登录用户')"
        vip="$(printf '%s' "${detail}" | jq -r '.data.vip_type // 0' 2>/dev/null || echo 0)"
        lib_log "网易云登录态: 已登录 (${nick}), vip_type=${vip}"
    else
        lib_warn "网易云尚未扫码登录 → 当前只能播放免费曲目，「每日推荐」不可用。请在飞牛桌面打开「${APP_NAME}」图标扫码登录（或 SSH 执行 bash ${RUN_DIR}/netease_login.sh）"
    fi
}

start_ui() {
    # 管理页面：扫码登录 + 全部配置，经飞牛统一网关暴露在 /app/${APP_NAME}
    # 网关会先校验 NAS 登录态再转发，并注入 X-Trim-Userid / X-Trim-Isadmin。
    # 必须以 root 运行：它要写入 root 代理读取的 .env，并调用需要 root 的重启脚本。
    if lib_pid_alive "${UI_PID}" && [ -S "${UI_SOCK}" ]; then
        lib_log "管理页面已在运行 (pid=$(head -n 1 "${UI_PID}"))"
        return 0
    fi
    # 先清掉可能残留的孤儿：只 rm 掉 socket 文件是不够的——旧进程仍持有那个
    # 已删除的 inode，新进程会绑到一个新 inode 上，两者并存且旧进程再也无法回收。
    lib_kill_stale_by_sock "${UI_SOCK}"
    rm -f "${UI_SOCK}" 2>/dev/null
    if [ ! -f "${RUN_DIR}/.venv-proxy/bin/uvicorn" ] || [ ! -f "${RUN_DIR}/proxy/admin_ui.py" ]; then
        lib_warn "管理页面组件缺失（uvicorn 或 proxy/admin_ui.py），跳过启动。扩展主功能不受影响。"
        return 0
    fi
    if [ ! -d "$(dirname "${UI_SOCK}")" ]; then
        lib_warn "网关 socket 目录 $(dirname "${UI_SOCK}") 不存在，跳过管理页面启动"
        return 0
    fi

    lib_log "启动管理页面 unix socket ${UI_SOCK}"
    lib_spawn "${UI_PID}" "${UI_LOG}" --as-root env \
        PATH="${PYTHON_BIN}:${PATH}" \
        PYTHONUNBUFFERED=1 \
        FNMUSIC_HOME="${RUN_DIR}" \
        FNMUSIC_MUSICBOX_URL="$(lib_read_env_value FNMUSIC_MUSICBOX_URL "${MUSICBOX_URL}")" \
        FNMUSIC_UPSTREAM_SOCK="${UPSTREAM_SOCK}" \
        FNMUSIC_ADMIN_ENV_FILE="${RUN_DIR}/.env" \
        FNMUSIC_ADMIN_RESTART_SCRIPT="${RUN_DIR}/bin/restart_services.sh" \
        FNMUSIC_ADMIN_LOG_DIR="${LOG_DIR}" \
        FNMUSIC_ADMIN_VAR_DIR="${PKGVAR}" \
        FNMUSIC_ADMIN_PREFIX="/app/${APP_NAME}" \
        FNMUSIC_ADMIN_UI_SOCK="${UI_SOCK}" \
        FNMUSIC_LOG_MAX_MB="$(lib_read_env_value FNMUSIC_LOG_MAX_MB 10)" \
        FNMUSIC_LOG_MAX_DAYS="$(lib_read_env_value FNMUSIC_LOG_MAX_DAYS 30)" \
        FNMUSIC_LOG_SCAN_INTERVAL="$(lib_read_env_value FNMUSIC_LOG_SCAN_INTERVAL 3600)" \
        "${RUN_DIR}/.venv-proxy/bin/uvicorn" admin_ui:app \
            --app-dir "${RUN_DIR}/proxy" \
            --uds "${UI_SOCK}"

    # pidfile 由子进程自己写入，存在竞态窗口；先给宽限再判活，
    # 否则会在 uvicorn 还没落 pidfile 时就误判"进程已退出"而 break。
    if ! lib_wait_pidfile "${UI_PID}"; then
        lib_warn "管理页面未能启动（进程退出或未写入 PID）。扫码登录请改用 SSH: bash ${RUN_DIR}/netease_login.sh。日志: ${UI_LOG}"
        tail -n 15 "${UI_LOG}" 2>/dev/null | sed 's/^/  ui: /' >&2 || true
        rm -f "${UI_PID}" 2>/dev/null
        # 进程可能其实起来了、只是 pidfile 没及时落盘；按 socket 路径收尾，
        # 否则就会留下一个占着 ui.sock 的永久孤儿
        lib_kill_stale_by_sock "${UI_SOCK}"
        return 0
    fi

    local i=0
    while [ "${i}" -lt 30 ]; do
        if [ -S "${UI_SOCK}" ]; then
            # 网关进程需要能 connect：unix socket 的连接权限取决于文件写位
            chmod 666 "${UI_SOCK}" 2>/dev/null || true
            lib_log "管理页面就绪 socket=${UI_SOCK} (pid=$(head -n 1 "${UI_PID}" 2>/dev/null))"
            lib_log "访问入口：飞牛桌面「飞牛音乐扩展」图标，或 https://<NAS>/app/${APP_NAME}"
            return 0
        fi
        if ! lib_pid_alive "${UI_PID}"; then
            break
        fi
        sleep 1
        i=$((i + 1))
    done
    # 管理页面起不来不影响在线播放，只降级为「需 SSH 扫码」
    lib_warn "管理页面未能启动（30s 超时或进程退出）。扫码登录请改用 SSH: bash ${RUN_DIR}/netease_login.sh。日志: ${UI_LOG}"
    tail -n 15 "${UI_LOG}" 2>/dev/null | sed 's/^/  ui: /' >&2 || true
    rm -f "${UI_PID}" 2>/dev/null
    return 0
}

start_watchdog() {
    # 看门狗（v2.7）：代理死亡 / 接管丢失时自动恢复。详见 watchdog.sh 头注。
    # 必须以 root 运行（它要调 start.sh，其中的代理以 root 起）。
    if [ "$(lib_watchdog_interval)" -le 0 ]; then
        lib_log "看门狗已按 FNMUSIC_WATCHDOG_INTERVAL_S=0 关闭"
        return 0
    fi
    if lib_pid_alive "${WATCHDOG_PID}"; then
        return 0
    fi
    lib_log "启动看门狗（interval=$(lib_watchdog_interval)s）"
    lib_spawn "${WATCHDOG_PID}" "${LOG_DIR}/watchdog.log" --as-root \
        env PATH="${PYTHON_BIN}:${PATH}" bash "${SCRIPT_DIR}/watchdog.sh"
    return 0
}

main() {
    lib_log "=== start 开始 ==="

    # 主动停机标志：生命周期入口（应用中心启动/安装/升级/配置保存）到达这里时
    # 一律清除；只有看门狗发起的恢复（FNMUSICEXT_WATCHDOG=1）在标志存在时
    # 必须立刻放弃——那是用户刚点了「停止」，绝不能被起死回生。
    if lib_stopped_flag_set; then
        if [ "${FNMUSICEXT_WATCHDOG:-0}" = "1" ]; then
            lib_log "start 由看门狗发起，但应用处于主动停机状态，放弃启动"
            return 0
        fi
        lib_log "清除主动停机标志（应用中心启动）"
        lib_stopped_flag_clear
    fi

    mkdir -p "${LOG_DIR}" "${PKGVAR}" 2>/dev/null

    # 启动前轮转一次日志：此时还没有进程持有日志 fd，rename 是安全的
    lib_rotate_logs

    start_musicbox || return 1
    # 扩展音源失败不阻断：网易云与本地曲库仍可工作
    start_musicsource
    start_proxy || {
        # 代理起不来就别留着音源服务空转
        lib_stop_pid "musicbox" "${MUSICBOX_PID}" 10
        lib_stop_pid "musicsource" "${MUSICSOURCE_PID}" 10
        return 1
    }
    # 管理页面失败不致命：主功能（在线播放）不依赖它
    start_ui
    # 看门狗失败同样不致命：它只影响故障自愈速度
    start_watchdog
    lib_log "=== start 完成 ==="
    return 0
}

main "$@"
