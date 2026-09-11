# fnmusic-ext 飞牛音乐扩展代理

`fnmusic-ext` 是专为 fnOS（飞牛私有云）自带音乐应用（`trim.music`）量身定制的无侵入式增强扩展。通过接管系统后端通信入口，在**完全不修改官方程序与数据库**的前提下，让原生飞牛音乐直接播放网易云曲库。

> **v2.0 起只有网易云一个在线音源**，且只使用你**扫码登录的那个私人账号**的权益。
> musicdl（酷我/咪咕聚合）与 lxmusic（洛雪免登录解析）两个音源已整体移除，详见 [CHANGELOG.md](CHANGELOG.md)。

### 🎵 核心带来什么功能？

- **网易云在线搜播**：直接在官方搜索框输入歌名/歌手，网易云曲库结果实时并入官方列表，即点即播；
- **使用你自己的会员权益**：扫码登录后，VIP / 无损 / 已购付费专辑曲目都能取到真实直链播放——音源完全来自你的私人账号，而不是任何第三方免登录解析；
- **未登录也不至于不可用**：掉线或未扫码时自动降级为只播免费曲目（可关闭），飞牛音乐的基础功能照常工作；
- **精准歌词与高清封面**：自动补齐在线歌曲的动态滚动 LRC 歌词与高清专辑封面，播放界面完整美观；
- **智能边播边存（无感离线）**：在线听歌时后台自动缓存音频文件，再次播放直接走本地，省外网流量且秒开；
- **网易云官方「每日推荐」**：直接抓取 `/weapi/v3/discovery/recommend/songs`，在飞牛歌单列表注入一份与网易云 App 同源的「每日推荐」，推荐逻辑交给网易云自己；
- **全平台原生无感适配**：飞牛网页端、官方手机 App、车载端开箱即用，无需安装任何客户端第三方插件；
- **多用户隔离收藏**：家庭多成员在 App 里点「红心」收藏在线歌曲，彼此数据独立隔离，与本地曲库完美融合；
- **PushPlus 掉线提醒**：登录态失效、首次检测到未登录、登录成功、VIP 临期时推送提醒，不必盯着 NAS 才发现会员已过期；
- **桌面网页管理**：装 `.fpk` 后飞牛桌面会出现「飞牛音乐扩展」图标，扫码登录、改配置、看日志全在一个页面里完成，**不用 SSH**。

在线音源实现基于 [darknessomi/musicbox](https://github.com/darknessomi/musicbox)（PyPI 包名 `NetEase-MusicBox`），本仓库的 `musicbox-service/` 是它的 HTTP 包装层。

---

## 两种安装方式

| | **A. 飞牛应用包 .fpk（推荐）** | **B. git 克隆 + 脚本** |
| :--- | :--- | :--- |
| 适合谁 | 想少碰命令行的普通用户 | 想读代码/改造/参与开发的用户 |
| 安装入口 | 应用中心 →「手动安装」→ 选 `.fpk` | `git clone` 后跑 `./install.sh` |
| 扫码登录 | **飞牛桌面图标里的网页**（也保留 `netease_login.sh`） | 终端 `./netease_login.sh` |
| 改配置 | **网页里改**（也保留应用设置的向导表单） | `./install.sh` 重新走向导，或改 `.env` |
| 看日志 | **网页里看** | `journalctl -u fnmusic-ext -f` |
| 启停 | 应用中心的启动/停止按钮 | `./extend.sh` / `./restore.sh` |
| 部署形态 | 飞牛原生进程管理（`cmd/main` + PID 文件，非 systemd、非 Docker） | systemd + Docker 容器，或 systemd + venv |

两种方式的音源与代理逻辑完全相同，只是宿主方式不同。下面**先讲 fpk**，再讲 git 方式。

### A. 飞牛应用包（.fpk）

```bash
# 在自己的构建机上打包（产物在 dist/）
./build_fpk.sh
```

或直接用 Release 里的成品，然后：

1. 打开飞牛**应用中心** → 左下角**「手动安装」** → 选择 `fnmusicext-<版本>.fpk`
2. 安装向导里按需勾选：音质、未登录是否降级、是否启用每日推荐、PushPlus token
3. 安装完成后在应用中心**启动**本应用
4. 回到飞牛**桌面**，点开「飞牛音乐扩展」图标 → 点「生成二维码」→ 用网易云 App 扫码

页面能力：

- **运行状态**：接管状态、官方后端连通性、音源服务、登录态与 VIP 剩余天数、日推与推送状态
- **扫码登录**：页面内出图，每 2.5s 轮询，已扫码会提示去手机确认，二维码过期自动换新的
- **配置**：音质 / 降级策略 / 每日推荐 / PushPlus / 搜索与缓存各项，保存后弹确认再重启
- **日志**：生命周期、代理、音源、socket 还原、安装依赖、本页，各看最近 120 行

该页面走飞牛**统一网关** `/app/fnmusicext`，由飞牛校验 NAS 登录态后才转发，
且**只认 `X-Trim-Isadmin: true`**——非管理员既看不到桌面图标，也调不动任何接口。
详见 [`fpk/README.md`](fpk/README.md)。

### B. git 克隆安装

见下方「快速开始」。

---

## 快速开始

### 前置准备

1. 已在 fnOS「应用中心」安装并启动官方 **【飞牛音乐】** 应用；
2. 宿主机已安装基础依赖（Python 3 及 venv）：
   ```bash
   sudo apt-get update && sudo apt-get install -y python3 python3-venv git jq curl
   ```
   > `jq` 仅 `netease_login.sh` 的终端扫码流程需要。

---

### 1. 拉取项目与赋予权限

```bash
# 1. 克隆仓库代码
git clone https://github.com/gzywd/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext

# 2. 赋予脚本执行权限
chmod +x install.sh extend.sh restore.sh netease_login.sh ensure_base_image.sh proxy/run_proxy.sh
```

---

### 2. 运行安装向导

执行交互式向导：

```bash
./install.sh
```

向导将引导您完成：

- **安装模式**：
  - `1) Docker 容器模式（推荐）`：音源服务容器化运行，隔离干净；
  - `2) Host 宿主机模式`：通过独立 Python venv 和 systemd 运行，免装 Docker。
- **音质**：`lossless` / `exhigh` / `higher` / `standard`（账号无对应权益时自动回退）；
- **PushPlus 推送（选填）**：是否启用、token（不回显输入）、群组编码；
- **降级策略**：未登录时是否只播免费曲目（默认开启）；
- **每日推荐**：是否抓取网易云官方日推（默认开启，需登录）。

> 💡 **进阶：非交互静默安装示例**（一行命令全自动完成并启用）：
> ```bash
> ./install.sh --non-interactive --mode docker --extend \
>   --pushplus-token 'YOUR_PUSHPLUS_TOKEN' --daily true --free-only-on-logout true
> ```

---

### 3. 扫码登录网易云（关键步骤）

音源完全来自你的私人账号，**必须扫码登录**才能解锁 VIP / 无损与每日推荐。

```bash
./netease_login.sh          # 或 ./install.sh --qr / ./extend.sh --qr
```

终端会展示 ASCII 二维码，用网易云音乐 App 扫码；二维码约 3 分钟有效，过期自动刷新，登录状态轮询到成功为止。不想现在登录可按 `Ctrl+C` 跳过——扩展会以「只播免费曲」的降级模式继续运行。

**fpk 安装用户**：装 `.fpk` 的话飞牛桌面上会有「飞牛音乐扩展」图标，点开就能在网页里扫码登录、
改全部配置、看日志，**不用 SSH**。该页面走飞牛统一网关 `/app/fnmusicext`，
由飞牛校验 NAS 登录态后转发，且限管理员访问。

**git 安装用户**：如需在别的设备上用浏览器直接取二维码图片，可把 `.env` 里的
`FNMUSIC_MUSICBOX_BIND` 改为 `0.0.0.0` 后重装，再访问
`http://<NAS_IP>:8770/api/v1/auth/login/qr.png`。
注意音源服务接口无鉴权，对外暴露等于同网段任何人都能扫自己的号顶掉你的登录，
因此**默认只绑 `127.0.0.1`**。

查看当前登录状态：

```bash
curl -s http://127.0.0.1:8770/api/v1/auth/detail
```

---

### 4. 启用与验证

若安装向导中未选择自动启用，可随时手动执行：

```bash
# 一键接管并启用扩展（含全链路自动化验收 + 登录态检查）
./extend.sh
```

- **在线听歌**：打开飞牛音乐 Web 端或手机 App，在搜索框输入歌曲名（如"晴天"），直接在线即点即播；
- **每日推荐**：登录后飞牛歌单列表会出现一张「每日推荐」，内容与网易云 App 同源；
- **健康检查**：在终端探测各组件连通状态与登录态：
  ```bash
  curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
  ```
  返回形如：
  ```json
  {
    "ok": true,
    "version": "2.0.0",
    "upstream": "ok",
    "musicbox": "ok",
    "netease": {
      "logged_in": true, "nickname": "某用户", "vip": true,
      "vip_days_left": 126, "free_only": false, "checked_at": 1789000000, "age_s": 3
    },
    "daily": "ok",
    "pushplus": "enabled"
  }
  ```
  `netease.logged_in` 为 `false` 且 `free_only` 为 `true` 时，说明当前处于免费曲降级模式；
  `daily` 为 `need_login` 时说明每日推荐因未登录而不可用。

---

### 5. 维护与一键还原

- **还原官方原生直连**（秒级切回，保留音源与缓存）：
  ```bash
  ./restore.sh
  ```
- **彻底卸载清理**（停止并删除容器/服务，清理配置残留与 v1.x 遗留音源）：
  ```bash
  ./restore.sh --full
  ```
- **多副本部署提示**：容器名（`fnmusic-musicbox`）与端口（8770）全局固定。
  从第二个副本（如测试目录）运行 `install.sh`/`extend.sh` 时，会自动移除并接管其他副本创建的同名容器；
  请避免多个副本同时执行安装/还原等运维操作。
- **从 v1.x 升级**：`install.sh` 会无条件清理旧版遗留的 `fnmusic-musicdl` / `fnmusic-lxmusic`
  容器与 systemd unit，并由 `proxy/env_merge.py` 自动从 `.env` 中删除已废弃的配置键。
  你的自定义路径、PushPlus token、音质偏好等非废弃配置一律保留。

---

## 环境变量配置

项目根目录的 `.env` 文件由安装向导自动生成与维护（权限 `0600`），主要配置项说明：

### 音源与搜索

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_MODE` | `docker` | 运行模式：`docker` 或 `host` |
| `FNMUSIC_NETEASE_ENABLED` | `true` | 是否启用网易云音源（关掉等于关闭全部在线功能） |
| `FNMUSIC_MUSICBOX_URL` | `http://127.0.0.1:8770` | 网易云音源服务地址 |
| `FNMUSIC_NETEASE_QUALITY` | `lossless` | 请求音质：`lossless`/`exhigh`/`higher`/`standard` |
| `FNMUSIC_NETEASE_SEARCH_LIMIT` | `50` | 单次搜索向网易云请求的曲目数上限 |
| `FNMUSIC_ONLINE_LIMIT` | `30` | 搜索结果并入飞牛列表的在线条目数上限 |
| `FNMUSIC_NETEASE_WAIT_S` | `3.0` | 搜索首屏等待预算（秒） |
| `FNMUSIC_LATE_PAGE_WAIT_S` | `5.0` | 首屏超时后的兜底等待预算（秒） |
| `FNMUSIC_SEARCH_CACHE_TTL` | `604800` | 搜索结果缓存有效期（默认 7 天） |

### 登录态与降级

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_FREE_ONLY_ON_LOGOUT` | `true` | 未登录时：`true`=降级只播免费曲，`false`=完全不提供在线播放 |
| `FNMUSIC_LOGIN_STATE_TTL` | `300` | 登录态缓存 TTL（秒） |
| `FNMUSIC_LOGIN_CHECK_INTERVAL` | `3600` | 后台巡检间隔（秒），用于发现掉线并推送 |
| `FNMUSIC_VIP_WARN_DAYS` | `7` | VIP 到期前几天开始推送提醒 |

### 每日推荐

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_DAILY_ENABLED` | `true` | 是否抓取网易云官方「每日推荐」（需登录） |
| `FNMUSIC_DAILY_LIMIT` | `20` | 每日推荐曲目数 |

### PushPlus 推送提醒

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_PUSHPLUS_ENABLED` | `true` | 推送总开关（未配 token 时自动视为关闭） |
| `FNMUSIC_PUSHPLUS_TOKEN` | *(空)* | [pushplus.plus](https://www.pushplus.plus) 用户 token |
| `FNMUSIC_PUSHPLUS_TOPIC` | *(空)* | 群组编码，留空只推送给自己 |
| `FNMUSIC_PUSHPLUS_TEMPLATE` | `markdown` | 消息模板：`markdown`/`html`/`txt`/`json` |
| `FNMUSIC_PUSHPLUS_URL` | `https://www.pushplus.plus/send` | 接口地址，可指向自建代理 |

> PushPlus 免费档限制：每日 200 次、每分钟 5 次、相同内容每小时 3 条，且**需实名认证**。
> 本扩展内置双重节流（同内容 1 小时去重 + 全局最小间隔 12 秒），token 无效或未实名时
> 停止重试并在日志明确报错。推送失败只记日志，绝不影响播放主链路。

完整清单见 [.env.example](.env.example)。

---

## 实现原理

### 1. Inode 接管与零侵入无缝串联

飞牛官方架构中，前端 Nginx 通过本地 Unix Domain Socket（`/var/run/trim_music.socket`）与官方 Go 编写的后端服务通信。

`fnmusic-ext` 巧妙利用 Linux 文件系统的 Socket Inode 机制：

1. 将官方套接字平滑重命名为 `trim_music_upstream.socket`；
2. 代理服务在原路径 `/var/run/trim_music.socket` 建立同名监听并赋予相同权限；
3. 官方 Nginx 与客户端对此完全无感知。

```text
[飞牛音乐客户端 (Web/App)]
          │
          ▼
    [飞牛 Nginx 代理]
          │ (通过 Unix Socket 请求)
          ▼
┌──────────────────────────────────────────────────────────────┐
│  fnmusic-ext 扩展代理 (/var/run/trim_music.socket)           │
│  ├─ 本地接口透传 ──► 官方后端 (trim_music_upstream.socket)   │
│  ├─ 在线搜索     ──► musicbox 音源服务 (127.0.0.1:8770)      │
│  ├─ 边播边落盘   ──► 网易云直链流式 Tee 写入曲库目录         │
│  ├─ 每日推荐歌单 ──► 网易云官方日推接口（需登录）            │
│  ├─ 登录态巡检   ──► 缓存 + 掉线/VIP 临期检测                │
│  └─ 推送提醒     ──► PushPlus                                │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
              [musicbox-service :8770]
              NetEase-MusicBox CLI/API 包装
                          │
                          ▼
                   [music.163.com]
```

### 2. 核心拦截与增强逻辑

- **搜索拦截 (`/music/api/v1/search/track`)**：
  透传请求给官方服务获取本地歌曲，同时向网易云发起在线搜索；按 `(title, artist)` 去重合并后渐进式返回。首屏预算（默认 3s）内返回即用，否则进入兜底预算（默认 5s），后台任务结果写入缓存供翻页复用。
- **流媒体播放与边播边存 (`/music/api/v1/track/stream`)**：
  拦截带有在线标识（`online:netease:<song_id>`）的 GUID，向 musicbox 解析真实直链后流式转发，并在后台通过独立的 Tee Task 异步将音频写入曲库目录（`歌手 - 歌名.ext`，同名 `.lrc` sidecar）。下次播放直接命中本地文件，秒开且省外网流量。
- **登录门控**：
  所有在线能力都以「当前登录的那个私人账号」的权益为上限。musicbox 服务端批量请求 `songs_url` 校验真实可播状态——拿不到直链、带试听片段（`freeTrialInfo`/`freeTrialPrivilege`）的曲目一律过滤；未登录时进一步只保留免费曲目（`fee in (0, 8)`）。
- **每日推荐 (`/music/api/v1/playlist/list`)**：
  登录后调用 musicbox 的 `/api/v1/recommend/daily`（内部走 `musicbox recommend songs`，即网易云 `/weapi/v3/discovery/recommend/songs`），把结果映射成飞牛歌单结构注入列表。**未登录时不注入**——与其塞一份网易云的热门填充列表，不如让官方歌单保持原样。按自然日缓存，跨天自动重建。
- **多用户隔离在线收藏 (`/music/api/v1/favorite-track/*`)**：
  用户点击红心时，拦截请求按当前登录用户的 GUID 独立记录在本地 `online_favorites/`，获取收藏列表时与官方本地收藏自动合并展示。
- **容灾与自动降级**：
  音源服务异常或超时时只返回本地结果，绝不阻塞搜索；若扩展代理进程异常退出，系统自动触发还原逻辑切回官方直连，绝不影响 NAS 原有音乐库的使用。

---

## 项目结构

```text
├── install.sh              # 安装向导（模式/音质/推送/降级/日推）
├── extend.sh               # socket 接管 + 全链路验收 + 登录态检查
├── restore.sh              # 一键还原官方直连（--full 彻底清理）
├── netease_login.sh        # 终端扫码登录（ASCII 二维码 + 过期刷新 + 状态轮询）
├── ensure_base_image.sh    # Docker 基础镜像源国内优先探测（docker 模式）
├── docker-compose.yml      # 单 service：musicbox
├── build_fpk.sh            # 打包飞牛应用包 → dist/<app>-<version>.fpk
├── fpk/                    # 应用包源：manifest / cmd / wizard / config / ui 入口 / 图标生成
├── proxy/                  # 拦截代理本体
│   ├── app.py              # FastAPI 代理，所有拦截端点
│   ├── admin_ui.py         # 桌面网页：扫码登录 + 配置 + 日志（统一网关鉴权）
│   ├── netease_auth.py     # 登录态探测/缓存 + 降级门控 + 推送触发
│   ├── netease_items.py    # 网易云 song_info → 统一条目映射
│   ├── pushplus.py         # PushPlus 推送客户端（含节流与脱敏）
│   ├── recommend.py        # 网易云官方每日推荐抓取与缓存
│   ├── env_merge.py        # .env 增量安全合并（保留用户值 + 清理废弃键）
│   ├── version.py          # 版本号读取
│   ├── run_proxy.sh        # 幂等 socket 接管 + uvicorn --uds 启动
│   └── tests/              # 288 个用例，pytest
└── musicbox-service/       # NetEase-MusicBox 的 HTTP 包装
    ├── app.py              # FastAPI：搜索/直链/详情/歌词/扫码登录/每日推荐
    ├── netease_ext.py      # NEMbox 内部 API：可播过滤、批量详情、登录详情
    └── runner.py           # musicbox CLI 执行器（剥离代理环境变量）
```

---

## 开发与测试

```bash
python3 -m pip install -r proxy/requirements.txt pytest pytest-asyncio pillow
python3 -m pytest proxy/tests -q          # 355 passed, 1 skipped(需 ffmpeg)

./build_fpk.sh                            # 打包 + 10 项自动自检
./build_fpk.sh --fnpack                   # 有官方 fnpack 时额外做结构交叉验证
```

参与贡献请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；安全问题请遵循 [SECURITY.md](SECURITY.md)。

---

## 免责与版权声明

### 1. 技术研究与非商业用途

- 本项目（`fnmusic-ext`）基于 **MIT 许可证** 开源发布，立项初衷仅为个人开发者探讨 Linux Unix Domain Socket 机制、透明反向代理技术、流式媒体传输与多协程并发架构的技术验证与学习交流。
- 本项目严格限定于**个人技术研究与非商业用途**。任何个人、团队或商业实体严禁将本项目、其衍生版本或相关工具用于任何形式的商业营利、付费订阅、软硬件捆绑销售或非法牟利行为。

### 2. 致谢上游开源项目与无侵权声明

- 本项目在线音源检索与元数据抓取能力依赖于社区优秀的开源组件：
  - [darknessomi/musicbox](https://github.com/darknessomi/musicbox)（PyPI：`NetEase-MusicBox`）
  在此向上游开源项目的原作者与贡献者致以崇高的敬意。
- 本项目仅在本地私有云环境充当**协议中继与数据适配胶水层**，本身不具备任何音源破解或版权规避逻辑。v2.0 起音源完全来自使用者本人扫码登录的合法账号权益，不涉及任何免登录第三方解析渠道，主观上绝无任何侵犯音乐平台、唱片公司或第三方知识产权的意图。

### 3. 音频及视听数据版权归属

- **音频及元数据版权全权归属各原始版权方**（包括但不限于各唱片公司、独立音乐人及在线音乐服务平台）。
- **零托管、零存储原则**：本项目服务器及开源代码仓库**不托管、不分发、不直接存储任何受版权保护的音频、视频、歌词或专辑封面文件**。所有音频流与图文元数据均系客户端发起请求时，由代理服务实时转发自使用者账号有权限访问的接口或源站 CDN。
- **本地缓存试听合规要求**：边播边落盘功能所生成的本地缓存文件，仅供个人离线收听、音频标签兼容性测试与学习评估。**使用者请在试听或测试后 24 小时内自行删除相关音频文件**。
- **倡导正版**：请大家支持正版数字音乐事业！如需长期收听、收藏或获得更高品质的音乐体验，请前往网易云音乐等官方平台开通正版会员并购买正版专辑。

### 4. 免责与使用者风险自担

- 使用者在下载、部署或运行本项目前，应充分知悉并自愿遵守所在国家/地区的法律法规，以及第三方服务平台的用户协议。
- **风险自担**：由于使用者滥用、恶意传播、商业化使用或不当配置本项目而导致的一切法律责任、版权纠纷、账号封禁、IP 拦截或连带经济损失，**概由使用者本人自行承担全部责任**，本项目发起人、维护者及社区贡献者不承担任何直接、间接或连带的法律责任。特别提示：使用本项目会以自己的网易云账号发起接口请求，请自行评估账号风险。
- **权利人联系通道**：若相关版权权利人认为本项目的代码实现或接口中继涉嫌侵犯其合法权益，请通过 GitHub Issue 或电子邮件向项目维护团队提交权属证明通知。我们将在收到通知并核实后的第一时间积极配合，并及时下架、修改或删除涉嫌侵权的代码与功能。
