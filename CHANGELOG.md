# 更新日志 (Changelog)

本项目所有显著变更均记录于此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [2.1.0] - 2026-09-11

### 新增

- **飞牛桌面内管理页面**（`proxy/admin_ui.py`）：装 `.fpk` 后飞牛桌面会出现「飞牛音乐扩展」
  图标，点开即可在网页里完成**扫码登录、全部配置、日志查看**，全程不需要 SSH。
  - 通过**统一网关** `/app/fnmusicext` 暴露（`fpk/payload/ui/config` 注册），服务监听
    `${TRIM_APPDEST}/ui.sock`；飞牛在转发前校验 NAS 登录态并注入
    `X-Trim-Userid` / `X-Trim-Isadmin` / `X-Trim-Username`。
  - 鉴权只信任这三个 Header，缺失即拒绝（视为未经网关的裸 socket 访问）；
    登录与改配置一律要求 `X-Trim-Isadmin: true`。桌面入口 `allUsers=false`，
    非管理员看不到图标。
  - 页面单文件、零外部依赖（不引任何 CDN），内网/离线环境可直接使用，自适应深浅色。
  - 扫码流程：页面内出图 → 每 2.5s 轮询 → 802 提示手机确认 → 803 成功并刷新状态 →
    800 过期自动换码（170s 兜底刷新）。
  - 配置保存前弹确认，明确告知「重启期间飞牛音乐会短暂回到官方直连、正在播放的在线曲目
    可能中断一次」。
  - 内置日志查看（生命周期/代理/音源/socket 还原/安装依赖/本页），带路径穿越防护与
    token 脱敏。
- **musicbox 二维码接口支持指定 unikey**：`GET /api/v1/auth/login/qr.png?unikey=`。
  此前每次取 PNG 都会新发起一次登录，"展示的码"与"轮询的 unikey"会错位，扫了也登不上。
- **`fpk/payload/bin/restart_services.sh`**：只重启代理与音源服务，**刻意不动 ui 进程**——
  否则页面发起重启会杀掉自己，响应永远回不来、用户只看到页面卡死。
- 管理页面写配置复用 `proxy/env_merge.py`，顺带清理废弃键，`.env` 写入前自动备份为
  `.env.bak`，保持 0600 与原子替换。

### 变更

- **音源服务默认只监听 `127.0.0.1`**（原为 `0.0.0.0:8770`）。
  musicbox 的接口全部无鉴权，其中就包含"发起扫码登录"，对外暴露意味着同网段任何人都能扫
  自己的号顶掉你的网易云登录。扫码改由桌面页面或 `netease_login.sh` 完成，不再需要对外开端口。
  - fpk：向导「音源服务监听地址」默认值改为 `127.0.0.1`，仍可改回 `0.0.0.0`；
  - git 安装：`docker-compose.yml` 改为 `${FNMUSIC_MUSICBOX_BIND:-127.0.0.1}:8770:8000`，
    `install.sh` host 模式的 systemd unit 改用 `${MUSICBOX_BIND}`（默认回环）；
  - 新增 `.env` 键 `FNMUSIC_MUSICBOX_BIND`。

### 修复

- **管理页面保存任意配置会冲掉 PushPlus token**：合并逻辑误用了"对客户端打码后的视图"，
  导致 `pushplus_token` / `pushplus_topic` 被写成 `••••••••`。改为内部合并一律走
  **真实值**视图 `_to_editable()`，脱敏只发生在返回客户端那一步；「未提交该项」与
  「提交了打码串/空串」都判定为不修改并保留原值。
- `fnmusic-lib.sh` 里 `PKGVAR` 的兜底默认值写成了 `@appvar`，官方框架实际是
  `/vol{n}/@appdata/{appname}`。改为按真实布局探测后回退。
- `build_fpk.sh` 新增自检：声明 `desktop_uidir` 时校验 `app/{uidir}/config` 存在且为合法
  JSON，并核对 `gatewaySocket` 与生命周期脚本创建的 socket 文件名**完全一致**
  （不一致会导致桌面图标点开 502，且很难排查）。

## [2.0.0] - 2026-09-11

大版本重构：**在线音源收敛为网易云单一渠道，且只使用扫码登录的那个私人账号的权益。**

### 移除

- **musicdl 聚合音源**（酷我 / 咪咕，端口 8768）：删除 `musicdl-service/` 整个目录、
  docker-compose service、systemd unit、安装向导多选项与相关验收分支。
- **lxmusic 洛雪免登录解析音源**（酷狗 kg / 网易 wy / 咪咕 mg，端口 8772）：删除
  `lxmusic-service/` 整个目录及其全部代理层解析分支（`fetch_lx_search`、`resolve_lx_url`）。
- **LLM 每日推荐**：删除 `FNMUSIC_LLM_BASE_URL` / `FNMUSIC_LLM_API_KEY` / `FNMUSIC_LLM_MODEL`
  配置、安装向导的模型列表拉取与选择流程、`proxy/recommend.py` 里的
  提示词构造 / 响应解析 / 本地收听记录种子推断 / 语种识别 / 兜底曲库整套逻辑。
- 配置项 `FNMUSIC_MUSICDL_*`、`FNMUSIC_LX_*`、`FNMUSIC_ONLINE_SOURCES`、`FNMUSIC_APT_MIRROR`、
  `FNMUSIC_DEPLOY_MODE` 一律废弃。**升级时由 `proxy/env_merge.py` 自动从 `.env` 清理**，
  用户自定义键与其余偏好值不受影响。
- 音源多选参数 `--sources` 与 `--llm-*` 参数不再具有语义；为不破坏老命令行，仍接受但忽略并告警。

### 新增

- **`proxy/netease_auth.py` 登录态门控**：探测并缓存（默认 300s TTL）网易云登录状态、
  昵称与 VIP 到期时间；后台每小时巡检一次（`FNMUSIC_LOGIN_CHECK_INTERVAL`）。
- **未登录降级策略 `FNMUSIC_FREE_ONLY_ON_LOGOUT`**（默认 `true`）：未扫码或 cookie 过期时，
  降级为只播免费曲目以保证飞牛音乐基础可用；置 `false` 则未登录完全不提供在线播放。
  已登录时使用账号自身权益，VIP / 无损 / 已购付费专辑曲目均可取到真实直链。
- **`proxy/pushplus.py` 推送提醒**：登录态失效、首次检测到未登录、登录成功、VIP 临期
  （`FNMUSIC_VIP_WARN_DAYS`，默认 7 天）时通过 PushPlus 推送。token 由
  `FNMUSIC_PUSHPLUS_TOKEN` 配置（安装向导不回显读取，`.env` 权限 0600），
  支持自定义 `FNMUSIC_PUSHPLUS_URL` / `TOPIC` / `TEMPLATE`。内置双重节流
  （同内容 1 小时去重 + 全局最小间隔 12s）以适配 PushPlus 免费档「每日 200 次 /
  每分钟 5 次 / 同内容每小时 3 条」的限制；token 无效或未实名时停止重试并明确报错。
  推送全链路失败只记日志，绝不影响播放。
- **`proxy/netease_items.py` 共享映射层**：网易云 song_info → 扩展统一条目的映射
  收敛为一份，消除搜索链路与日推链路的重复实现。
- **musicbox 服务新增接口**：
  - `GET /api/v1/auth/detail` —— 登录态详情（含 `vip_type` / `vip_expires_ms`）；
  - `GET /api/v1/recommend/daily?limit=N` —— 网易云官方「每日推荐」，复用
    `musicbox recommend songs` 子命令（退出码 3 判定未登录）。
- **安装向导 PushPlus 配置环节**：`--pushplus-token` / `--pushplus-topic` /
  `--free-only-on-logout` / `--daily` 命令行参数，交互式下 token 不回显、打印时脱敏。
- **`extend.sh` 登录态验收**：接管完成后读取 `/api/v1/auth/detail`，未登录给出醒目的
  降级说明与扫码指引；已登录打印昵称与 VIP 剩余天数，并顺带验收日推接口可用性
  （失败只告警不阻断）。
- **v1.x 残留自动清理**：`install.sh` 无条件移除旧版遗留的 `fnmusic-musicdl` /
  `fnmusic-lxmusic` 容器与 systemd unit；`restore.sh --full` 同步清理。

### 变更

- **「每日推荐」歌单来源**：由大模型凭空生成改为直接抓取网易云官方日推
  （`/weapi/v3/discovery/recommend/songs`），推荐内容与用户真实听歌画像一致。
  代价是**必须登录**——未登录时不再注入一份内容不对的歌单，`playlist/list`
  保持官方列表原样（此前会注入一个空壳「每日推荐」）。
- **搜索链路**：三源并发竞速 + 首响兜底合并逻辑简化为单源两段式等待
  （首屏预算 `FNMUSIC_NETEASE_WAIT_S` 3s → 兜底预算 `FNMUSIC_LATE_PAGE_WAIT_S` 5s），
  行为对多源时代等价但代码量大幅下降。
- **代理层健康检查 `/_ext/healthz` 返回体**：去掉 `musicdl` / `lxmusic` / `llm` 字段，
  改为 `netease`（登录态子对象）+ `daily` + `pushplus`；`ok` 语义收紧为
  「上游可用 且 音源服务可用 且（已登录 或 允许免费曲降级）」。
- **可播性过滤**：`netease_ext.filter_playable_song_ids` 的「未登录只留免费曲」逻辑
  改为受 `FNMUSIC_FREE_ONLY_ON_LOGOUT` 控制，并显式过滤 `freeTrialPrivilege` 试听片段、
  移除原先冗余的 `fee != 0 and fee not in (0, 8)` 判断。
- **版本号**：按 `proxy/version.py` 约定，破坏性大版本重构 → `2.0.0`。

### 修复

- `test_version_env.py` 的 VERSION 断言此前写死 `1.1.2`，与仓库真实版本 `1.2.3`
  不符（长期处于失败状态）。改为校验语义化版本格式，并新增
  **VERSION 与 CHANGELOG 最新条目必须一致** 的用例，防止发版只改一处。

## [1.2.3] - 2026-09-08

### 新增

- **Docker 构建基础镜像源自动探测回退**：fnOS 等系统在 Docker daemon 全局配置的镜像加速器
  （如 `docker.fnnas.com`）异常（401/超时）时，BuildKit 解析 `python:3.13-slim` 元数据失败且不会
  回退官方源，`docker compose up --build` 随即失败（`failed to resolve source metadata ... 401 Unauthorized`）。
  新增 `ensure_base_image.sh`：**国内镜像优先**（完整镜像源引用直连对应仓库，绕开只拦截
  docker.io 短引用的 daemon 加速器，以真实 `docker pull` 验证），逐个尝试
  docker.1ms.run / docker.m.daocloud.io / docker.1panel.live / hub.rat.dev（`FNMUSIC_DOCKER_MIRRORS`
  可覆盖），全部失败再兜底官方 `python:3.13-slim`（daemon 加速器链路在国内网络下常慢/不稳），
  结果缓存到 `.env` 的 `FNMUSIC_BASE_IMAGE` 并由 compose `build.args` 自动读取；install.sh 安装、
  extend.sh 自愈重建、手动 `docker compose up -d --build` 三条路径全部生效，后续运行先验证缓存、
  失效自动重新探测。全程不修改系统 Docker 配置，仅本应用构建生效；也可通过 `BASE_IMAGE` 环境变量
  或直接编辑 `.env` 手动指定。

### 变更

- 三个音源镜像构建内 `pip install` 默认接入清华 PyPI 源（`.env` 的 `FNMUSIC_PIP_INDEX` 可覆盖），
  与宿主机模式安装惯例对齐。
- musicdl 镜像 apt 层默认接入清华镜像（`FNMUSIC_APT_MIRROR` 可覆盖）：仅在构建层内临时替换
  `deb.debian.org`，apt update 失败（15s 超时快速判定）自动回退官方源重试，安装完成后恢复
  官方源——最终镜像与宿主机 apt 配置不受影响；国内网络下 ffmpeg 及其依赖不再长时间卡在
  deb.debian.org 慢速下载。

## [1.2.2] - 2026-09-08

### 修复

- **Docker 安装在受限 umask 环境下启动失败的严重问题**：三个音源镜像此前直接继承仓库检出文件的权限位，
  在 umask 077 环境（root shell、`sudo git clone` 等）下检出的 `app.py` 为 600，进镜像后为
  `root:root 0600`，容器内非 root 的 `appuser` 无法读取，uvicorn 启动即抛
  `PermissionError: [Errno 13] Permission denied: '/app/app.py'` 并随 `restart: unless-stopped` 无限重启。
  现镜像内源码统一 `--chown=appuser:appuser` 且权限 0644，与宿主机文件权限完全解耦（`COPY --chmod`
  仅 BuildKit 支持，故采用兼容新旧构建器的 `--chown` + `RUN chmod` 方案）。
- 修正 musicbox Dockerfile 中 `chown` 早于 `COPY` 执行而对源码文件不生效的问题。

### 变更

- `install.sh` 构建前对服务源码做权限归一化（非致命兜底），避免受限 umask/属主影响构建上下文。
- `docs/INSTALL.md` 新增「常见问题排查」章节，含上述报错的说明与升级方法。

## [1.2.1] - 2026-09-08

### 修复

- 移除生产安装中多余的 pytest 测试依赖。

## [1.2.0] - 2026-09-07

### 新增

- 安装收尾集成网易云终端扫码登录流程（ASCII 二维码过期自动刷新 + 登录状态轮询）。

## [1.1.2] - 2026-09-07

### 新增

- 全音源严格可播过滤与直链探活防线校验升级。

### 优化

- 多音源搜索 3s 首屏与 5s 首响兜底机制，缓存延长至 7 天。

## [1.1.1] - 2026-09-07

### 修复

- 过滤收费不可播歌曲，重构酷狗直链解析。
- 安装/还原流程加固与多音源搜索容错增强。

## [1.1.0] - 2026-09-07

### 新增

- 第三音源：洛雪音乐源（lxmusic，酷狗/网易/咪咕免登录解析）。

## [1.0.1] - 2026-09-07

### 新增

- 项目版本管理与安装配置增量合并机制，音源超时与自适应降级。

### 修复

- 彻底修复网易云 XDG 目录缺失导致子进程崩溃，支持命令行终端直接显示登录二维码。
- 重构验收试播逻辑，支持多音源平等遍历与多关键词重试。
- extend 与端口 5667 解耦，通过 UDS 检查安全判定启用状态。

## [1.0.0] - 2026-09-04

### 新增

- fnmusic-ext 首个发布版本：musicdl / musicbox 双音源，Docker 与宿主机双模式部署，一键安装向导与 fnOS 代理扩展接管。
