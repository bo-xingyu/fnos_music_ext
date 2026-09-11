#!/bin/bash
# ==============================================================================
# fnmusic-ext fpk：只重启「代理 + 音源服务」，不动管理页面(ui)进程
#
# 由管理页面保存配置后调用。ui 进程必须保持存活，否则发起重启的那个 HTTP 请求
# 会把自己的服务一并杀掉、响应永远回不来，用户只能看到页面卡死。
#
# 顺序与 stop.sh/start.sh 一致，保证任何时刻 /var/run/trim_music.socket
# 要么指向官方后端、要么指向本扩展，绝不留空档让飞牛音乐连不上。
# ==============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./fnmusic-lib.sh
. "${SCRIPT_DIR}/fnmusic-lib.sh"

main() {
    lib_log "=== restart_services 开始（保留 ui 进程）==="

    # 1. 停代理：它持有 /var/run/trim_music.socket
    lib_stop_pid "proxy" "${PROXY_PID}" 15
    # 2. 立即还原 socket —— 重启窗口内飞牛音乐必须可用
    if [ -f "${RUN_DIR}/restore.sh" ]; then
        lib_log "重启中：先还原 socket 给官方后端"
        bash "${RUN_DIR}/restore.sh" >> "${LOG_DIR}/restore.log" 2>&1 \
            || lib_warn "restore.sh 返回非零，交由随后的 start.sh 重新接管（它会做幂等探测）"
    fi
    # 3. 停音源服务（配置里的监听地址/降级开关可能变了）
    lib_stop_pid "musicbox" "${MUSICBOX_PID}" 10

    # 4. 重新拉起。start.sh 自身幂等：ui 若已在运行会被跳过
    if bash "${SCRIPT_DIR}/start.sh" >> "${LOG_DIR}/info.log" 2>&1; then
        lib_log "=== restart_services 完成 ==="
        return 0
    fi
    lib_fail "重启后未能重新接管。飞牛音乐当前为官方直连状态（可正常使用），详见 ${LOG_DIR}"
    return 1
}

main "$@"
