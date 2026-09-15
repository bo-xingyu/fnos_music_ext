#!/bin/bash
# ==============================================================================
# fnmusic-ext fpk：看门狗（v2.7）
#
# 背景（2026-09-12 真机事故）：官方 trim-music 后端重启时会重绑
# /var/run/trim_music.socket（先 unlink 再 bind），把本扩展代理的监听 socket
# 文件"抢走"。代理进程本身毫发无损（诊断里启动时间很长），但 status.sh 探测
# 不到接管 → 应用中心报「异常退出」，而扩展自己毫无察觉，在线音源从此失效，
# 直到用户手动重启。另一种形态是代理/音源进程因故死亡（OOM 等），同样无人拉起。
#
# 职责（每 FNMUSIC_WATCHDOG_INTERVAL_S 秒，默认 30，.env 里设 0 关闭）：
#   1. 代理进程不存在，或存在但 socket 接管丢失 → 调 start.sh 幂等恢复；
#   2. musicbox 音源进程不存在 → 同样走 start.sh（幂等，只补缺失的部分）；
#   3. 主动停机（stop.sh 落 stopped.flag）→ 立即退出，绝不起死回生；
#   4. 配置重启窗口（restart.inprogress）→ 跳过本轮，避免与正常重启打架。
#
# 恢复连续失败时指数退避（interval → 2× → 4× … 封顶 300s），避免在
# 官方后端长时间不可用时每 30s 空转一次冷启动。
# ==============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./fnmusic-lib.sh
. "${SCRIPT_DIR}/fnmusic-lib.sh"

# start.sh 会以 FNMUSICEXT_WATCHDOG=1 识别本次是看门狗发起的恢复：
# 若此刻 stopped.flag 已存在（用户刚点了停止），它必须立刻放弃。
export FNMUSICEXT_WATCHDOG=1

main() {
    local interval backoff
    interval="$(lib_watchdog_interval)"
    if [ "${interval}" -le 0 ]; then
        lib_log "watchdog：FNMUSIC_WATCHDOG_INTERVAL_S<=0，已按配置关闭"
        exit 0
    fi
    backoff="${interval}"
    lib_log "watchdog 启动 (pid=$$, interval=${interval}s)"

    local need reason fails=0
    while true; do
        sleep "${backoff}"

        # 主动停机：用户/系统明确要求停止，看门狗使命结束
        if lib_stopped_flag_set; then
            lib_log "watchdog：检测到主动停机标志，退出"
            exit 0
        fi
        # pidfile 被换成别人（新一轮 start 起了新看门狗）：旧实例自觉退出
        if [ -r "${WATCHDOG_PID}" ]; then
            local cur
            cur="$(head -n 1 "${WATCHDOG_PID}" | tr -d '[:space:]')"
            [ -n "${cur}" ] && [ "${cur}" != "$$" ] && exit 0
        fi
        # 配置重启窗口内不插手（restart_services.sh 正在停→起）
        if lib_restart_in_progress; then
            fails=0
            backoff="${interval}"
            continue
        fi

        need=0
        reason=""
        if ! lib_pid_alive "${PROXY_PID}"; then
            need=1
            reason="代理进程不存在"
        elif ! lib_probe_proxy; then
            need=1
            reason="socket 接管丢失（官方后端可能重启过）"
        elif ! lib_pid_alive "${MUSICBOX_PID}"; then
            need=1
            reason="musicbox 音源进程不存在"
        fi
        if [ "${need}" -eq 0 ]; then
            fails=0
            backoff="${interval}"
            continue
        fi

        # ------------------------------------------------------------------
        # 防抖：只在「接管丢失」时复测一次。
        #
        # lib_probe_proxy 有 3s 超时，而 healthz 内部要连上游 + musicbox 三次；
        # 上游偶尔慢一下就会超时 → 误判成接管丢失 → 走进 start.sh 的「先停后
        # 起」分支，**把健康的代理杀掉**，代价是 1~2 分钟服务中断（真机 01:51
        # 那次报丢失、3 秒后就正常了，纯属误报）。
        # 进程不存在的分支不复测——那没有歧义，越早恢复越好。
        # ------------------------------------------------------------------
        if [ "${need}" -eq 1 ] && [ "${reason}" = "socket 接管丢失（官方后端可能重启过）" ]; then
            sleep 5
            if lib_pid_alive "${PROXY_PID}" && lib_probe_proxy; then
                lib_log "watchdog：复测 socket 接管正常，判定为瞬时抖动，不干预"
                fails=0
                backoff="${interval}"
                continue
            fi
        fi

        # 行动前最后再确认一次停机标志（stop.sh 可能刚好落地）
        if lib_stopped_flag_set; then
            exit 0
        fi

        lib_warn "watchdog：${reason}，尝试自动恢复..."
        if bash "${SCRIPT_DIR}/start.sh" >> "${LOG_DIR}/watchdog.log" 2>&1; then
            lib_log "watchdog：恢复完成（${reason}）"
            fails=0
            backoff="${interval}"
        else
            fails=$(( fails + 1 ))
            backoff=$(( interval * 2 ** fails ))
            [ "${backoff}" -gt 300 ] && backoff=300
            lib_warn "watchdog：恢复失败（第 ${fails} 次），${backoff}s 后重试"
        fi

        # 恢复期间若有停机请求插进来（stopped.flag 已落盘），立即补一次停机，
        # 保证「用户点停止」永远是最终状态，而不是被恢复动作覆盖。
        if lib_stopped_flag_set; then
            lib_log "watchdog：恢复过程中收到停机请求，执行停机以保持最终状态一致"
            bash "${SCRIPT_DIR}/stop.sh" >> "${LOG_DIR}/watchdog.log" 2>&1 || true
            exit 0
        fi
    done
}

main "$@"
