#!/bin/bash
# ==============================================================================
# fnmusic-ext fpk：状态检查
#
# 官方约定：exit 0 = 正在运行，exit 3 = 未运行，其它非零 = 异常。
# 判据：代理进程存活 **且** 原路径 socket 确实由本扩展接管（healthz 可应答）。
# 只看 PID 不够 —— 接管可能因官方后端未就绪而失败，那种情况必须报未运行，
# 好让应用中心如实展示状态、用户才会去处理。
# ==============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./fnmusic-lib.sh
. "${SCRIPT_DIR}/fnmusic-lib.sh"

main() {
    # 重启窗口（管理页保存配置触发的 stop→start）不算停机：
    # 飞牛桌面看到「未运行」会立刻回收应用窗口，用户表现为「保存即闪退」。
    if lib_restart_in_progress; then
        echo "running: 配置重启进行中（最多 ${RESTART_MARKER_MAX_AGE}s），窗口保持可用" >&2
        exit 0
    fi

    if ! lib_pid_alive "${PROXY_PID}"; then
        rm -f "${PROXY_PID}" 2>/dev/null
        echo "not running: 代理进程不存在" >&2
        exit 3
    fi

    if ! lib_probe_proxy; then
        echo "not running: 代理进程存在但未接管 ${TARGET_SOCK}（官方后端可能重启过；看门狗会自动恢复）" >&2
        exit 3
    fi

    # 音源服务状态仅作信息展示，不参与 running 判定：
    # 音源挂了代理仍应透传本地曲库，飞牛音乐不至于不可用。
    local mb_state="down"
    if lib_pid_alive "${MUSICBOX_PID}"; then
        if curl -s --max-time 3 -f "${MUSICBOX_URL}/healthz" >/dev/null 2>&1; then
            mb_state="ok"
        else
            mb_state="degraded"
        fi
    fi

    local login="unknown"
    if [ "${mb_state}" != "down" ]; then
        login="$(curl -s --max-time 3 "${MUSICBOX_URL}/api/v1/auth/detail" 2>/dev/null \
            | jq -r 'if .data.logged_in == true then "logged_in" else "logged_out" end' 2>/dev/null || echo unknown)"
    fi

    local ui_state="down"
    if lib_pid_alive "${UI_PID}" && [ -S "${UI_SOCK}" ]; then
        ui_state="ok"
    fi

    echo "running: proxy pid=$(head -n 1 "${PROXY_PID}" | tr -d '[:space:]') musicbox=${mb_state} ui=${ui_state} netease=${login}"
    exit 0
}

main "$@"
