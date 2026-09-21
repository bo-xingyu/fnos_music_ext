#!/bin/bash
# ==============================================================================
# fnmusic-ext fpk：停止
#
# 顺序至关重要：
#   1. 先停代理进程（它正持有 /var/run/trim_music.socket 的监听）
#   2. 再把 socket 还原给飞牛官方后端 —— 这一步不可省略，否则飞牛音乐会直连失败
#   3. 最后停音源服务
#
# 幂等：任何一步「已经处于目标状态」都视为成功，绝不因重复执行而报错。
# ==============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./fnmusic-lib.sh
. "${SCRIPT_DIR}/fnmusic-lib.sh"

restore_socket() {
    # 复用仓库里已在生产验证过的还原逻辑（探测 socket 身份后安全复位，
    # 绝不误删官方活动 socket）。以 root 调用，其内部的 sudo 检查会直接通过。
    if [ -f "${RUN_DIR}/restore.sh" ]; then
        lib_log "调用 restore.sh 复位 Unix Socket..."
        if bash "${RUN_DIR}/restore.sh" >> "${LOG_DIR}/restore.log" 2>&1; then
            lib_log "socket 复位完成"
            return 0
        fi
        lib_warn "restore.sh 返回非零，改用内置最小复位逻辑兜底"
    else
        lib_warn "未找到 ${RUN_DIR}/restore.sh，使用内置最小复位逻辑兜底"
    fi

    # 兜底复位：与 restore.sh 同样的四态语义（proxy / trim-music / absent / unknown），
    # 但不依赖 sudo —— restore.sh 是为交互式非 root 用户写的，会强制要求 sudo 授权；
    # 在 sudo 不可用的环境（受限容器等）里它必然失败，这里必须能独立完成还原。
    # 铁律：身份不明时绝不动原路径上的 socket，宁可飞牛音乐短暂不可用，
    #       也不能误删官方正在监听的活动 socket。
    local identity="unknown"
    local resp
    if [ ! -S "${TARGET_SOCK}" ]; then
        identity="absent"
    else
        resp="$(curl -s --max-time 3 --unix-socket "${TARGET_SOCK}" \
            http://localhost/_ext/healthz 2>/dev/null || true)"
        if printf '%s' "${resp}" | grep -q '"upstream"'; then
            identity="proxy"
        else
            resp="$(curl -s --max-time 3 --unix-socket "${TARGET_SOCK}" \
                'http://localhost/music/api/v1/search/track?keyword=test' 2>/dev/null || true)"
            if printf '%s' "${resp}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
                identity="trim-music"
            fi
        fi
    fi
    lib_log "兜底复位：原路径身份=${identity}"

    case "${identity}" in
        proxy)
            rm -f "${TARGET_SOCK}" 2>/dev/null
            if [ -S "${UPSTREAM_SOCK}" ]; then
                mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}" 2>/dev/null
                chmod 666 "${TARGET_SOCK}" 2>/dev/null
                lib_log "兜底复位成功：upstream 已移回 ${TARGET_SOCK}"
            else
                lib_warn "兜底复位后未发现 upstream socket。若飞牛音乐无响应，请在应用中心重启「飞牛音乐」以重建官方 socket。"
            fi
            ;;
        trim-music)
            # 原路径已经是官方后端（可能 restart 过），只清理 upstream 残留
            [ -e "${UPSTREAM_SOCK}" ] && rm -f "${UPSTREAM_SOCK}" 2>/dev/null
            chmod 666 "${TARGET_SOCK}" 2>/dev/null
            lib_log "原路径已由官方后端监听，仅清理 upstream 残留"
            ;;
        absent)
            if [ -S "${UPSTREAM_SOCK}" ]; then
                mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}" 2>/dev/null
                chmod 666 "${TARGET_SOCK}" 2>/dev/null
                lib_log "兜底复位成功（原路径缺失，upstream 已移回）"
            else
                lib_warn "原路径与 upstream 均不存在。请在应用中心重启「飞牛音乐」。"
            fi
            ;;
        *)
            # 身份不明：保守处理，只在原路径确实没有 socket 时才搬回 upstream
            if [ -S "${UPSTREAM_SOCK}" ] && [ ! -S "${TARGET_SOCK}" ]; then
                mv "${UPSTREAM_SOCK}" "${TARGET_SOCK}" 2>/dev/null
                chmod 666 "${TARGET_SOCK}" 2>/dev/null
                lib_log "身份不明但原路径无 socket，已将 upstream 移回"
            else
                lib_warn "socket 身份不明，保守保留现状不做删除。若飞牛音乐异常，请在应用中心重启「飞牛音乐」。"
            fi
            ;;
    esac
    return 0
}

verify_official() {
    local resp
    resp="$(curl -s --max-time 5 --unix-socket "${TARGET_SOCK}" \
        "http://localhost/music/api/v1/search/track?keyword=test" 2>/dev/null || true)"
    if printf '%s' "${resp}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
        lib_log "官方 trim-music 直连验证成功"
        return 0
    fi
    lib_warn "官方直连验证未收到预期响应（可能需要重启飞牛音乐）: ${resp:-无响应}"
    return 0
}

main() {
    lib_log "=== stop 开始 ==="
    # 0. 先落「主动停机」标志并停看门狗：看门狗的职责是把死掉的服务拉起来，
    #    主动停机时必须让它先闭嘴，否则刚停完 30s 内又被原样拉回来。
    #    标志由下一次 start.sh 清除（看门狗发起的 start 见到标志会直接放弃）。
    lib_stopped_flag_mark
    lib_stop_pid "watchdog" "${WATCHDOG_PID}" 5
    # 0.5 再停管理页面：它可能会调用 restart_services.sh，先断掉这条路径，
    #     避免停机过程中页面又发起一次重启造成竞争
    lib_stop_pid "ui" "${UI_PID}" 10
    # pidfile 可能因启动竞态丢失，按 socket 路径精确兜底清理，杜绝孤儿进程
    lib_kill_stale_by_sock "${UI_SOCK}"
    rm -f "${UI_SOCK}" 2>/dev/null
    # 1. 停代理
    lib_stop_pid "proxy" "${PROXY_PID}" 15
    # 2. 复位 socket（关键步骤，失败也要继续清理音源服务）
    restore_socket
    verify_official
    # 3. 停音源服务
    lib_stop_pid "musicbox" "${MUSICBOX_PID}" 10
    lib_stop_pid "musicsource" "${MUSICSOURCE_PID}" 10
    lib_log "=== stop 完成 ==="
    return 0
}

main "$@"
