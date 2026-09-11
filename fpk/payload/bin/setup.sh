#!/bin/bash
# ==============================================================================
# fnmusic-ext fpk：把包载荷 stage 到运行目录、建虚拟环境、生成 .env
#
# 由 cmd/install_callback 与 cmd/upgrade_callback 调用。幂等，可反复执行。
#
# 为什么 stage 到 $TRIM_PKGVAR/app 而不是直接在 $TRIM_APPDEST 运行：
#   proxy/run_proxy.sh 与 restore.sh 以「脚本所在目录的父目录」为 BASE_DIR，
#   并在其下寻找 .venv-proxy / .env / cache / proxy。把它们连同数据放在同一个
#   BASE_DIR 下，就能**原样复用**那套已在生产验证过的 socket 接管/复位逻辑，
#   不必在 fpk 里重写一份高风险实现。代码是只读载荷，数据在 PKGVAR，
#   升级时用 APPDEST 覆盖代码、保留 .env / musicbox-data / cache。
# ==============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./fnmusic-lib.sh
. "${SCRIPT_DIR}/fnmusic-lib.sh"

# 由调用方（cmd/*）通过环境变量传入的向导值
WIZ_QUALITY="${wizard_netease_quality:-lossless}"
WIZ_FREE_ONLY="${wizard_free_only_on_logout:-true}"
WIZ_DAILY="${wizard_daily_enabled:-true}"
WIZ_DAILY_LIMIT="${wizard_daily_limit:-20}"
WIZ_BIND="${wizard_musicbox_bind:-127.0.0.1}"
WIZ_PUSH_ENABLED="${wizard_pushplus_enabled:-true}"
WIZ_PUSH_TOKEN="${wizard_pushplus_token:-}"
WIZ_PUSH_TOPIC="${wizard_pushplus_topic:-}"
WIZ_PUSH_TEMPLATE="${wizard_pushplus_template:-markdown}"
WIZ_PIP_INDEX="${wizard_pip_index:-https://pypi.tuna.tsinghua.edu.cn/simple}"
# 日志保留策略（不进向导，需要精调直接改 .env）
LOG_MAX_MB="${FNMUSIC_LOG_MAX_MB:-10}"
LOG_MAX_DAYS="${FNMUSIC_LOG_MAX_DAYS:-30}"

# 重建虚拟环境（升级时装新依赖）——设为 0 可跳过，节省升级时间
REBUILD_VENV="${FNMUSICEXT_REBUILD_VENV:-1}"

norm_bool() {
    case "$(echo "$1" | tr '[:upper:]' '[:lower:]')" in
        true|1|yes|on) echo "true" ;;
        *) echo "false" ;;
    esac
}

# 单引号 dotenv 转义：' -> '\''
dq() {
    local v="$1"
    printf "'%s'" "$(printf '%s' "${v}" | sed "s/'/'\\\\\\\\''/g")"
}

stage_code() {
    lib_log "stage 载荷: ${APPDEST} -> ${RUN_DIR}"
    mkdir -p "${RUN_DIR}" "${LOG_DIR}" || {
        lib_fail "无法创建运行目录 ${RUN_DIR}"
        return 1
    }
    # 代码与脚本载荷：逐项覆盖。绝不删除 RUN_DIR 下的 .env / cache /
    # online_favorites / play_history / recommend_cache / musicbox-data / .venv-*
    local item
    for item in proxy musicbox-service bin restore.sh netease_login.sh VERSION; do
        if [ -e "${APPDEST}/${item}" ]; then
            cp -a "${APPDEST}/${item}" "${RUN_DIR}/" || {
                lib_fail "复制 ${item} 到 ${RUN_DIR} 失败"
                return 1
            }
        fi
    done
    # 数据目录（首次创建，之后一律保留）
    mkdir -p "${RUN_DIR}/cache" "${RUN_DIR}/online_favorites" \
             "${RUN_DIR}/play_history" "${RUN_DIR}/recommend_cache" \
             "${RUN_DIR}/musicbox-data/cache/netease-musicbox" \
             "${RUN_DIR}/musicbox-data/config/netease-musicbox" \
             "${RUN_DIR}/musicbox-data/netease-musicbox" || true
    chmod +x "${RUN_DIR}/restore.sh" "${RUN_DIR}/netease_login.sh" \
             "${RUN_DIR}/proxy/run_proxy.sh" "${RUN_DIR}"/bin/*.sh 2>/dev/null || true
    # 网易云登录凭证必须归包用户所有，降权运行的 musicbox 才写得进去
    if [ -n "${TRIM_USERNAME:-}" ] && id "${TRIM_USERNAME}" >/dev/null 2>&1; then
        chown -R "${TRIM_USERNAME}:${TRIM_GROUPNAME:-${TRIM_USERNAME}}" \
            "${RUN_DIR}/musicbox-data" 2>/dev/null || true
    fi
    return 0
}

write_env_file() {
    lib_log "生成 ${ENV_FILE}"
    local token_line
    if [ -n "${WIZ_PUSH_TOKEN}" ]; then
        token_line="$(dq "${WIZ_PUSH_TOKEN}")"
    else
        # token 留空 = 保持已保存值不变（避免配置页误提交把 token 冲掉）
        local kept
        kept="$(lib_read_env_value FNMUSIC_PUSHPLUS_TOKEN "")"
        token_line="$(dq "${kept}")"
    fi

    local tmp="${ENV_FILE}.new.$$"
    {
        echo "# 由飞牛应用中心向导生成，请勿手工编辑后重新提交向导（会被覆盖）。"
        echo "# 手工精调可直接编辑本文件，然后重启应用。"
        echo "FNMUSIC_HOME=$(dq "${RUN_DIR}")"
        echo "FNMUSIC_VERSION=$(dq "$(cat "${RUN_DIR}/VERSION" 2>/dev/null || echo 2.0.0)")"
        echo "FNMUSIC_MODE=$(dq "host")"
        echo "# --- 网易云音源（唯一音源） ---"
        echo "FNMUSIC_NETEASE_ENABLED=$(dq "true")"
        echo "FNMUSIC_MUSICBOX_URL=$(dq "http://127.0.0.1:${MUSICBOX_PORT}")"
        echo "FNMUSIC_NETEASE_QUALITY=$(dq "${WIZ_QUALITY}")"
        echo "FNMUSIC_NETEASE_SEARCH_LIMIT=$(dq "50")"
        echo "FNMUSIC_ONLINE_LIMIT=$(dq "30")"
        echo "FNMUSIC_NETEASE_WAIT_S=$(dq "3.0")"
        echo "FNMUSIC_LATE_PAGE_WAIT_S=$(dq "5.0")"
        echo "FNMUSIC_SEARCH_TIMEOUT=$(dq "15")"
        echo "FNMUSIC_SEARCH_CACHE_TTL=$(dq "604800")"
        echo "FNMUSIC_MUSICBOX_BIND=$(dq "${WIZ_BIND}")"
        echo "# --- 登录态与降级 ---"
        echo "FNMUSIC_FREE_ONLY_ON_LOGOUT=$(dq "$(norm_bool "${WIZ_FREE_ONLY}")")"
        echo "FNMUSIC_LOGIN_STATE_TTL=$(dq "300")"
        echo "FNMUSIC_LOGIN_CHECK_INTERVAL=$(dq "3600")"
        echo "FNMUSIC_VIP_WARN_DAYS=$(dq "7")"
        echo "# --- 网易云官方每日推荐（需登录） ---"
        echo "FNMUSIC_DAILY_ENABLED=$(dq "$(norm_bool "${WIZ_DAILY}")")"
        echo "FNMUSIC_DAILY_LIMIT=$(dq "${WIZ_DAILY_LIMIT}")"
        echo "# --- PushPlus 推送提醒 ---"
        echo "FNMUSIC_PUSHPLUS_ENABLED=$(dq "$(norm_bool "${WIZ_PUSH_ENABLED}")")"
        echo "FNMUSIC_PUSHPLUS_TOKEN=${token_line}"
        echo "FNMUSIC_PUSHPLUS_TOPIC=$(dq "${WIZ_PUSH_TOPIC}")"
        echo "FNMUSIC_PUSHPLUS_TEMPLATE=$(dq "${WIZ_PUSH_TEMPLATE}")"
        echo "FNMUSIC_PUSHPLUS_URL=$(dq "https://www.pushplus.plus/send")"
        echo "# --- 运行时路径 ---"
        echo "FNMUSIC_CACHE_DIR=$(dq "${RUN_DIR}/cache")"
        echo "FNMUSIC_FAV_DIR=$(dq "${RUN_DIR}/online_favorites")"
        echo "FNMUSIC_PLAY_HISTORY_DIR=$(dq "${RUN_DIR}/play_history")"
        echo "FNMUSIC_RECOMMEND_DIR=$(dq "${RUN_DIR}/recommend_cache")"
        echo "FNMUSIC_UPSTREAM_SOCK=$(dq "${UPSTREAM_SOCK}")"
        echo "# 曲库目录留空 = 由代理自动探测飞牛 shared_library.path"
        echo "FNMUSIC_LIBRARY_DIR=$(dq "")"
        echo "FNMUSIC_MUSIC_DB=$(dq "/usr/local/apps/@appdata/trim.music/db/music.db")"
        echo "FNMUSIC_PIP_INDEX=$(dq "${WIZ_PIP_INDEX}")"
        echo "# --- 日志保留策略：超过 10MB 就地截断保留尾部，超过 30 天清理 ---"
        echo "FNMUSIC_LOG_MAX_MB=$(dq "${LOG_MAX_MB}")"
        echo "FNMUSIC_LOG_MAX_DAYS=$(dq "${LOG_MAX_DAYS}")"
        echo "FNMUSIC_LOG_SCAN_INTERVAL=$(dq "3600")"
    } > "${tmp}" || {
        lib_fail "写入 ${tmp} 失败"
        rm -f "${tmp}"
        return 1
    }
    chmod 600 "${tmp}" || true
    mv -f "${tmp}" "${ENV_FILE}" || {
        lib_fail "原子替换 ${ENV_FILE} 失败"
        rm -f "${tmp}"
        return 1
    }
    lib_log ".env 已写入（token 已脱敏，不落日志）"
    return 0
}

build_venvs() {
    [ "$(norm_bool "${REBUILD_VENV}")" = "true" ] || {
        lib_log "REBUILD_VENV=false，跳过虚拟环境重建"
        return 0
    }
    local py
    py="$(lib_check_python)" || return 1
    lib_log "使用 Python: ${py} ($(py_ver "${py}"))"

    local idx="${WIZ_PIP_INDEX}"
    local name req
    for name in proxy musicbox; do
        local venv="${RUN_DIR}/.venv-${name}"
        if [ "${name}" = "proxy" ]; then
            req="${RUN_DIR}/proxy/requirements.txt"
        else
            req="${RUN_DIR}/musicbox-service/requirements.txt"
        fi
        if [ ! -x "${venv}/bin/python" ]; then
            lib_log "创建虚拟环境 ${venv}"
            "${py}" -m venv "${venv}" >> "${LOG_DIR}/setup.log" 2>&1 || {
                lib_fail "创建虚拟环境 ${venv} 失败（详见 ${LOG_DIR}/setup.log）。请确认已安装 python3-venv 等价能力或 ${PYTHON_APP} 运行时包。"
                return 1
            }
        fi
        lib_log "安装依赖: ${req} (pip index=${idx})"
        "${venv}/bin/pip" install -q --disable-pip-version-check -U pip \
            -i "${idx}" >> "${LOG_DIR}/setup.log" 2>&1
        "${venv}/bin/pip" install -q --disable-pip-version-check -r "${req}" \
            -i "${idx}" >> "${LOG_DIR}/setup.log" 2>&1 || {
            lib_warn "使用镜像源 ${idx} 安装 ${name} 依赖失败，回退官方 PyPI 源重试"
            "${venv}/bin/pip" install -q --disable-pip-version-check -r "${req}" \
                -i "https://pypi.org/simple" >> "${LOG_DIR}/setup.log" 2>&1 || {
                lib_fail "安装 ${name} 依赖失败（镜像源与官方源均不可用）。详见 ${LOG_DIR}/setup.log"
                return 1
            }
        }
    done
    lib_log "虚拟环境就绪"
    return 0
}

main() {
    lib_log "=== setup 开始 (app=${TRIM_APPVER:-?}) ==="
    stage_code || return 1
    write_env_file || return 1
    build_venvs || return 1
    lib_log "=== setup 完成 ==="
    return 0
}

main "$@"
