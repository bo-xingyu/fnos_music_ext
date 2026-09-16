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

# 向导值不再在此预读成 WIZ_*：一律经 pick_env 按「向导 > 旧值 > 默认」
# （或保留模式下的「旧值 > 向导 > 默认」）取值，防止把用户已保存的设置冲掉。
# 日志保留策略（不进向导，需要精调直接改 .env）
LOG_MAX_MB="${FNMUSIC_LOG_MAX_MB:-10}"
LOG_MAX_DAYS="${FNMUSIC_LOG_MAX_DAYS:-30}"

# 重建虚拟环境（升级时装新依赖）——设为 0 可跳过，节省升级时间
REBUILD_VENV="${FNMUSICEXT_REBUILD_VENV:-1}"

# 保留模式（升级专用，由 cmd/upgrade_callback 设置）：
# .env 里已有的用户设置一律原样保留，向导值只用于补齐缺失键。
# 背景事故：v2.3 之前 write_env_file 每次都整表重写、只特殊照顾 token，
# 用户在管理页保存过的歌单口径 / 收藏归档目录等设置在每次升级或
# 「应用设置」保存后全部被冲回默认值。
PRESERVE_ENV="${FNMUSICEXT_PRESERVE_ENV:-false}"

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

# ---------------------------------------------------------------------------
# 旧 .env 读取：现 .env 优先，其次「卸载+重装」前的快照 .env.preserved
# ---------------------------------------------------------------------------
ENV_SRC=""
if [ -r "${ENV_FILE}" ]; then
    ENV_SRC="${ENV_FILE}"
elif [ -r "${PKGVAR}/.env.preserved" ]; then
    ENV_SRC="${PKGVAR}/.env.preserved"
fi

OLD_ENV_KEYS=""
if [ -n "${ENV_SRC}" ]; then
    OLD_ENV_KEYS="$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "${ENV_SRC}" 2>/dev/null | sort -u)"
    if [ "${ENV_SRC}" = "${PKGVAR}/.env.preserved" ]; then
        lib_log "检测到重装场景：从 .env.preserved 恢复全部既有设置"
    fi
fi

has_old() {
    [ -n "${OLD_ENV_KEYS}" ] || return 1
    printf '%s\n' "${OLD_ENV_KEYS}" | grep -qx -- "$1"
}

old_val() {
    [ -n "${ENV_SRC}" ] || return 0
    sed -n "s/^$1='\{0,1\}\([^']*\)'\{0,1\}\s*$/\1/p" "${ENV_SRC}" 2>/dev/null | tail -n 1
}

# pick_env <KEY> <向导变量名或空> <默认值>
#   普通模式（安装 / 应用设置保存）：向导值 > 旧值 > 默认
#   保留模式（升级）：旧值 > 向导值 > 默认
pick_env() {
    local key="$1" wiz_var="$2" default="$3" wiz="" old=""
    [ -n "${wiz_var}" ] && wiz="${!wiz_var:-}"
    if has_old "${key}"; then
        old="$(old_val "${key}")"
    fi
    if [ "$(norm_bool "${PRESERVE_ENV}")" = "true" ]; then
        if has_old "${key}"; then printf '%s' "${old}"
        elif [ -n "${wiz}" ]; then printf '%s' "${wiz}"
        else printf '%s' "${default}"; fi
    else
        if [ -n "${wiz}" ]; then printf '%s' "${wiz}"
        elif has_old "${key}"; then printf '%s' "${old}"
        else printf '%s' "${default}"; fi
    fi
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
             "${RUN_DIR}/play_history" "${RUN_DIR}/recommend_cache" || true
    chmod +x "${RUN_DIR}/restore.sh" "${RUN_DIR}/netease_login.sh" \
             "${RUN_DIR}/proxy/run_proxy.sh" "${RUN_DIR}"/bin/*.sh 2>/dev/null || true
    # 网易云登录凭证放在 ${PKGVAR}/musicbox-data（RUN_DIR 之外），跨安装/重装持久；
    # 先把旧版本的凭证迁过来，再确保目录结构与属主正确。
    lib_migrate_musicbox_data
    lib_ensure_musicbox_data_dirs
    return 0
}

write_env_file() {
    lib_log "生成 ${ENV_FILE}（已有用户设置原样保留，模式 preserve=${PRESERVE_ENV}）"
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
        echo "FNMUSIC_NETEASE_QUALITY=$(dq "$(pick_env FNMUSIC_NETEASE_QUALITY wizard_netease_quality lossless)")"
        echo "FNMUSIC_NETEASE_SEARCH_LIMIT=$(dq "$(pick_env FNMUSIC_NETEASE_SEARCH_LIMIT "" 50)")"
        echo "FNMUSIC_ONLINE_LIMIT=$(dq "$(pick_env FNMUSIC_ONLINE_LIMIT "" 30)")"
        echo "FNMUSIC_NETEASE_WAIT_S=$(dq "$(pick_env FNMUSIC_NETEASE_WAIT_S "" 3.0)")"
        echo "FNMUSIC_LATE_PAGE_WAIT_S=$(dq "$(pick_env FNMUSIC_LATE_PAGE_WAIT_S "" 5.0)")"
        echo "FNMUSIC_SEARCH_TIMEOUT=$(dq "$(pick_env FNMUSIC_SEARCH_TIMEOUT "" 15)")"
        echo "FNMUSIC_SEARCH_CACHE_TTL=$(dq "$(pick_env FNMUSIC_SEARCH_CACHE_TTL "" 604800)")"
        echo "FNMUSIC_SEARCH_EMPTY_TTL=$(dq "$(pick_env FNMUSIC_SEARCH_EMPTY_TTL "" 60)")"
        echo "FNMUSIC_LOGIN_CACHE_TTL=$(dq "$(pick_env FNMUSIC_LOGIN_CACHE_TTL "" 300)")"
        echo "FNMUSIC_MUSICBOX_BIND=$(dq "$(pick_env FNMUSIC_MUSICBOX_BIND wizard_musicbox_bind 127.0.0.1)")"
        echo "# --- 登录态与降级 ---"
        echo "FNMUSIC_FREE_ONLY_ON_LOGOUT=$(dq "$(norm_bool "$(pick_env FNMUSIC_FREE_ONLY_ON_LOGOUT wizard_free_only_on_logout true)")")"
        echo "FNMUSIC_LOGIN_STATE_TTL=$(dq "$(pick_env FNMUSIC_LOGIN_STATE_TTL "" 300)")"
        echo "FNMUSIC_LOGIN_CHECK_INTERVAL=$(dq "$(pick_env FNMUSIC_LOGIN_CHECK_INTERVAL "" 3600)")"
        echo "# --- 网易云官方每日推荐（需登录） ---"
        echo "FNMUSIC_DAILY_ENABLED=$(dq "$(norm_bool "$(pick_env FNMUSIC_DAILY_ENABLED wizard_daily_enabled true)")")"
        echo "FNMUSIC_DAILY_LIMIT=$(dq "$(pick_env FNMUSIC_DAILY_LIMIT wizard_daily_limit 20)")"
        # 本地每日推荐（v2.9）：每天从本地曲库随机抽 N 首（与网易云日推独立）
        echo "FNMUSIC_LOCAL_DAILY_ENABLED=$(dq "$(norm_bool "$(pick_env FNMUSIC_LOCAL_DAILY_ENABLED "" true)")")"
        echo "FNMUSIC_LOCAL_DAILY_LIMIT=$(dq "$(pick_env FNMUSIC_LOCAL_DAILY_LIMIT "" 50)")"
        echo "# --- 更多口径歌单 / 账户歌单 ---"
        echo "FNMUSIC_NETEASE_CHANNELS=$(dq "$(pick_env FNMUSIC_NETEASE_CHANNELS "" mine,toplist,category)")"
        echo "FNMUSIC_NETEASE_CHANNEL_LIMIT=$(dq "$(pick_env FNMUSIC_NETEASE_CHANNEL_LIMIT "" 8)")"
        echo "FNMUSIC_NETEASE_CATEGORY=$(dq "$(pick_env FNMUSIC_NETEASE_CATEGORY "" 华语)")"
        echo "# 歌单口径展示顺序（大类固定排序，管理页可改）"
        # v2.9.5 迁移：大类顺序的旧默认值（日推在前）整体换成新默认值
        # （本地每日推荐排第一）。只认"完全等于旧默认值"的情况——用户自己调过
        # 顺序的话原样保留，绝不覆盖。
        _cho="$(pick_env FNMUSIC_NETEASE_CHANNEL_ORDER "" localdaily,daily,mine,nrec,toplist,category,newalbum,fm)"
        # v2.9.8 迁移：把 localdaily 提到第一位，其余口径的相对顺序原样保留。
        #
        # v2.9.5 那版只认「完全等于旧默认值」才迁移，太脆弱：用户只要在管理页
        # 保存过一次配置（哪怕只是勾掉一个 fm），值就再也不等于旧默认值，于是
        # 本地每日推荐永远卡在第二位。这里改成幂等的"提到最前"，怎么改过都能
        # 收敛到正确顺序；用户之后在管理页再调仍然以管理页为准。
        case "${_cho}" in
            localdaily,*|localdaily) : ;;   # 已经在第一位，不动
            *)
                _rest="$(printf '%s' "${_cho}" | tr ',' '\n' \
                         | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                         | grep -vx 'localdaily' | paste -sd, -)"
                if [ -n "${_rest}" ]; then
                    _cho="localdaily,${_rest}"
                else
                    _cho="localdaily"
                fi
                ;;
        esac
        echo "FNMUSIC_NETEASE_CHANNEL_ORDER=$(dq "${_cho}")"
        # 手动歌单顺序（v2.5）：管理页「歌单顺序」卡片保存的 token 列表；空=按大类
        echo "FNMUSIC_NETEASE_PLAYLIST_ORDER=$(dq "$(pick_env FNMUSIC_NETEASE_PLAYLIST_ORDER "" "")")"
        echo "FNMUSIC_PLAYLIST_TRACK_LIMIT=$(dq "$(pick_env FNMUSIC_PLAYLIST_TRACK_LIMIT "" 300)")"
        # 歌单曲目缓存（v2.6）：TTL 秒 + 每日定时刷新时间
        echo "FNMUSIC_PLAYLIST_TRACK_CACHE_TTL=$(dq "$(pick_env FNMUSIC_PLAYLIST_TRACK_CACHE_TTL "" 21600)")"
        echo "FNMUSIC_PLAYLIST_REFRESH_AT=$(dq "$(pick_env FNMUSIC_PLAYLIST_REFRESH_AT "" 04:30)")"
        # 定时刷新/自动预热跳过「仍新鲜」缓存的阈值（v2.8.2，秒）：1 小时内刚刷过的不重拉；
        # 手动「预热歌单缓存」按钮不受此限。0 = 一律刷新
        echo "FNMUSIC_WARM_SKIP_FRESH_S=$(dq "$(pick_env FNMUSIC_WARM_SKIP_FRESH_S "" 3600)")"
        # 看门狗（v2.7）：代理死亡/接管丢失时的自动恢复轮询间隔（秒），0=关闭。
        # 2026-09-12 事故：官方后端重启重绑 socket 后，应用报「异常退出」且无人自愈。
        echo "FNMUSIC_WATCHDOG_INTERVAL_S=$(dq "$(pick_env FNMUSIC_WATCHDOG_INTERVAL_S "" 30)")"
        # 播放直链短缓存（v2.7）：复用有效期内直链，省去重复取链的跨洋往返；0=关闭
        echo "FNMUSIC_URL_CACHE_TTL=$(dq "$(pick_env FNMUSIC_URL_CACHE_TTL "" 600)")"
        # 口径清单短缓存（v2.7）：歌单列表页 5 分钟内零上游往返，过期后台刷新；0=关闭
        echo "FNMUSIC_CHANNEL_LIST_CACHE_TTL=$(dq "$(pick_env FNMUSIC_CHANNEL_LIST_CACHE_TTL "" 300)")"
        # 放 PKGVAR 而不是 RUN_DIR：RUN_DIR 在「卸载+重装」时整个被删，
        # 注册表存着歌单名字与封面，丢了就会退化成"网易云歌单 12345"+无封面。
        echo "FNMUSIC_PLAYLIST_CACHE_DIR=$(dq "${PKGVAR}/playlist_cache")"
        echo "# --- 收藏归档与红心同步 ---"
        echo "FNMUSIC_DOWNLOAD_DIR=$(dq "$(pick_env FNMUSIC_DOWNLOAD_DIR "" "")")"
        echo "FNMUSIC_DOWNLOAD_ON_FAVORITE=$(dq "$(norm_bool "$(pick_env FNMUSIC_DOWNLOAD_ON_FAVORITE "" true)")")"
        echo "# --- 音质策略（跟随飞牛 / 按网络 / 固定）---"
        echo "FNMUSIC_QUALITY_WIFI=$(dq "$(pick_env FNMUSIC_QUALITY_WIFI "" lossless)")"
        echo "FNMUSIC_QUALITY_CELLULAR=$(dq "$(pick_env FNMUSIC_QUALITY_CELLULAR "" exhigh)")"
        echo "FNMUSIC_QUALITY_DB_RESCAN=$(dq "$(pick_env FNMUSIC_QUALITY_DB_RESCAN "" 300)")"
        # 远程访问识别（v2.8）：公网客户端 IP 视同流量场景（走省流档）；false=关闭
        echo "FNMUSIC_REMOTE_AS_CELLULAR=$(dq "$(norm_bool "$(pick_env FNMUSIC_REMOTE_AS_CELLULAR "" true)")")"
        # 本地曲库优先（v2.8）：在线曲目先匹配本地同名文件，命中且音质类与策略一致时直接读本地
        echo "FNMUSIC_LOCAL_FIRST=$(dq "$(norm_bool "$(pick_env FNMUSIC_LOCAL_FIRST "" true)")")"
        echo "FNMUSIC_LOCAL_INDEX_TTL=$(dq "$(pick_env FNMUSIC_LOCAL_INDEX_TTL "" 300)")"
        # 封面缩图边长（v2.8.1）：网易云 CDN 服务端缩图（?param=NyN），300px 约 20~50KB，
        # 原图几百 KB~1MB 是移动网络下列表卡顿的主力；0=不压缩
        echo "FNMUSIC_COVER_RESIZE_PX=$(dq "$(pick_env FNMUSIC_COVER_RESIZE_PX "" 300)")"
        echo "# --- PushPlus 推送提醒 ---"
        echo "FNMUSIC_PUSHPLUS_ENABLED=$(dq "$(norm_bool "$(pick_env FNMUSIC_PUSHPLUS_ENABLED wizard_pushplus_enabled true)")")"
        echo "FNMUSIC_PUSHPLUS_TOKEN=$(dq "$(pick_env FNMUSIC_PUSHPLUS_TOKEN wizard_pushplus_token "")")"
        echo "FNMUSIC_PUSHPLUS_TOPIC=$(dq "$(pick_env FNMUSIC_PUSHPLUS_TOPIC wizard_pushplus_topic "")")"
        echo "FNMUSIC_PUSHPLUS_TEMPLATE=$(dq "$(pick_env FNMUSIC_PUSHPLUS_TEMPLATE wizard_pushplus_template markdown)")"
        echo "FNMUSIC_PUSHPLUS_URL=$(dq "$(pick_env FNMUSIC_PUSHPLUS_URL "" https://www.pushplus.plus/send)")"
        echo "# --- 运行时路径 ---"
        echo "FNMUSIC_CACHE_DIR=$(dq "${RUN_DIR}/cache")"
        echo "FNMUSIC_FAV_DIR=$(dq "${RUN_DIR}/online_favorites")"
        echo "FNMUSIC_PLAY_HISTORY_DIR=$(dq "${RUN_DIR}/play_history")"
        echo "FNMUSIC_RECOMMEND_DIR=$(dq "${RUN_DIR}/recommend_cache")"
        echo "FNMUSIC_UPSTREAM_SOCK=$(dq "${UPSTREAM_SOCK}")"
        echo "# 曲库目录留空 = 由代理自动探测飞牛 shared_library.path"
        echo "FNMUSIC_LIBRARY_DIR=$(dq "$(pick_env FNMUSIC_LIBRARY_DIR "" "")")"
        echo "FNMUSIC_MUSIC_DB=$(dq "$(pick_env FNMUSIC_MUSIC_DB "" /usr/local/apps/@appdata/trim.music/db/music.db)")"
        echo "FNMUSIC_PIP_INDEX=$(dq "$(pick_env FNMUSIC_PIP_INDEX wizard_pip_index https://pypi.tuna.tsinghua.edu.cn/simple)")"
        echo "# --- 日志保留策略：超过 10MB 就地截断保留尾部，超过 30 天清理 ---"
        echo "FNMUSIC_LOG_MAX_MB=$(dq "$(pick_env FNMUSIC_LOG_MAX_MB "" "${LOG_MAX_MB}")")"
        echo "FNMUSIC_LOG_MAX_DAYS=$(dq "$(pick_env FNMUSIC_LOG_MAX_DAYS "" "${LOG_MAX_DAYS}")")"
        echo "FNMUSIC_LOG_SCAN_INTERVAL=$(dq "$(pick_env FNMUSIC_LOG_SCAN_INTERVAL "" 3600)")"
        # v2.9.29：日志降噪（默认开）。关掉可记录全部访问行，用于抓原始日志排障。
        echo "FNMUSIC_LOG_QUIET=$(dq "$(pick_env FNMUSIC_LOG_QUIET "" true)")"
        # v2.9.28：非局域网直连 CDN（音频不经 NAS 中转）与取链硬超时（秒）
        echo "FNMUSIC_CDN_REDIRECT=$(dq "$(pick_env FNMUSIC_CDN_REDIRECT "" true)")"
        echo "FNMUSIC_PLAY_RESOLVE_TIMEOUT_S=$(dq "$(pick_env FNMUSIC_PLAY_RESOLVE_TIMEOUT_S "" 5)")"
        # 用户手工加的、不属于本应用托管清单的自定义键：原样保留在尾部
        if [ -n "${ENV_SRC}" ] && [ -n "${OLD_ENV_KEYS}" ]; then
            echo "# --- 以下为用户自定义键（自动保留） ---"
            local k v
            while IFS= read -r k; do
                [ -n "${k}" ] || continue
                case "${k}" in
                    FNMUSIC_HOME|FNMUSIC_VERSION|FNMUSIC_MODE|FNMUSIC_NETEASE_ENABLED|\
                    FNMUSIC_MUSICBOX_URL|FNMUSIC_NETEASE_QUALITY|FNMUSIC_NETEASE_SEARCH_LIMIT|\
                    FNMUSIC_ONLINE_LIMIT|FNMUSIC_NETEASE_WAIT_S|FNMUSIC_LATE_PAGE_WAIT_S|\
                    FNMUSIC_SEARCH_TIMEOUT|FNMUSIC_SEARCH_CACHE_TTL|FNMUSIC_SEARCH_EMPTY_TTL|\
                    FNMUSIC_LOGIN_CACHE_TTL|FNMUSIC_MUSICBOX_BIND|FNMUSIC_FREE_ONLY_ON_LOGOUT|\
                    FNMUSIC_LOGIN_STATE_TTL|FNMUSIC_LOGIN_CHECK_INTERVAL|\
                    FNMUSIC_DAILY_ENABLED|FNMUSIC_DAILY_LIMIT|FNMUSIC_LOCAL_DAILY_ENABLED|FNMUSIC_LOCAL_DAILY_LIMIT|FNMUSIC_NETEASE_CHANNELS|\
                    FNMUSIC_NETEASE_CHANNEL_LIMIT|FNMUSIC_NETEASE_CATEGORY|FNMUSIC_NETEASE_CHANNEL_ORDER|\
                    FNMUSIC_NETEASE_PLAYLIST_ORDER|FNMUSIC_PLAYLIST_TRACK_CACHE_TTL|FNMUSIC_PLAYLIST_REFRESH_AT|\
                    FNMUSIC_WARM_SKIP_FRESH_S|\
                    FNMUSIC_WATCHDOG_INTERVAL_S|FNMUSIC_URL_CACHE_TTL|FNMUSIC_CHANNEL_LIST_CACHE_TTL|\
                    FNMUSIC_PLAYLIST_TRACK_LIMIT|FNMUSIC_PLAYLIST_CACHE_DIR|FNMUSIC_DOWNLOAD_DIR|\
                    FNMUSIC_DOWNLOAD_ON_FAVORITE|\
                    FNMUSIC_QUALITY_WIFI|FNMUSIC_QUALITY_CELLULAR|\
                    FNMUSIC_QUALITY_DB_RESCAN|FNMUSIC_REMOTE_AS_CELLULAR|FNMUSIC_LOCAL_FIRST|\
                    FNMUSIC_LOCAL_INDEX_TTL|FNMUSIC_COVER_RESIZE_PX|FNMUSIC_PUSHPLUS_ENABLED|FNMUSIC_PUSHPLUS_TOKEN|\
                    FNMUSIC_PUSHPLUS_TOPIC|FNMUSIC_PUSHPLUS_TEMPLATE|FNMUSIC_PUSHPLUS_URL|\
                    FNMUSIC_CACHE_DIR|FNMUSIC_FAV_DIR|FNMUSIC_PLAY_HISTORY_DIR|FNMUSIC_RECOMMEND_DIR|\
                    FNMUSIC_UPSTREAM_SOCK|FNMUSIC_LIBRARY_DIR|FNMUSIC_MUSIC_DB|FNMUSIC_PIP_INDEX|\
                    FNMUSIC_LOG_MAX_MB|FNMUSIC_LOG_MAX_DAYS|FNMUSIC_LOG_SCAN_INTERVAL|\
                    FNMUSIC_LOG_QUIET|FNMUSIC_CDN_REDIRECT|FNMUSIC_PLAY_RESOLVE_TIMEOUT_S)
                        continue ;;
                    FNMUSIC_MUSICDL_*|FNMUSIC_LX_*|FNMUSIC_LLM_*)
                        continue ;;  # 已废弃的 v1.x 遗留键不再保留
                esac
                v="$(old_val "${k}")"
                echo "${k}=$(dq "${v}")"
            done <<< "${OLD_ENV_KEYS}"
        fi
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
    lib_log ".env 已写入（全部既有设置已保留，token 不落日志）"
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

    # pip 镜像源以【已合并的 .env】为准（可能是用户保留的旧值）
    local idx
    idx="$(lib_read_env_value FNMUSIC_PIP_INDEX "https://pypi.tuna.tsinghua.edu.cn/simple")"
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
