# 人工安装与部署指南

适用环境：飞牛 NAS（fnOS）已安装并启动「飞牛音乐」官方应用。本项目采用无侵入接管设计，**完全不修改**飞牛官方 nginx 配置、不 Patch 官方 Go 二进制、不改动官方数据库。

> 💡 **自动化部署提示**：若使用 AI Agent（如 OpenCode、Claude Code、Cursor 等）进行全流程自动化部署与自检验收，请直接查阅 [Agent 安装提示词](AGENT_INSTALL.md)。

---

## 0. 前置准备

- **操作系统**：fnOS（Debian 12 基础系统）；
- **基础运行组件**：Python 3.11+ 及 `python3-venv` 虚拟环境模块，外加 `curl` 与 `jq`（`jq` 仅终端扫码登录脚本需要）；
  ```bash
  sudo apt-get update && sudo apt-get install -y python3 python3-venv git curl jq
  ```
- **管理员权限**：具备 `sudo` 执行权限的管理员账号；
- **官方音乐应用**：必须先在 fnOS「应用中心」安装并启动「飞牛音乐」（确保存在 `/var/run/trim_music.socket`）；
- **Docker 环境（若选 Docker 模式）**：必须先在 fnOS「应用中心」安装好 Docker，**脚本绝不会擅自安装 Docker 引擎**；

克隆项目并进入根目录赋予执行权限：
```bash
git clone https://github.com/gzywd/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext
chmod +x install.sh extend.sh restore.sh netease_login.sh ensure_base_image.sh proxy/run_proxy.sh
```

---

## 1. 安装模式透明说明（消除黑盒困惑）

为了让用户完全掌控系统的变动，下文对扩展代理的两种部署模式进行彻底、透明的直白说明：

### 共通的核心运行原则（必读）
> ⚠️ **无论选择「Docker 容器模式」还是「Host 宿主机本地服务模式」，核心代理服务（`fnmusic-ext`）都必须以宿主机 systemd 运行！**
> 
> **原因**：核心代理的核心任务是零侵入接管宿主机上的 Unix Domain Socket（`/var/run/trim_music.socket`），使官方 nginx 与飞牛原生后端透明桥接。若将代理塞入普通 Docker bridge 容器，将面临复杂的跨容器与宿主机 socket 权限穿透问题，因此核心代理始终由宿主机 systemd（`fnmusic-ext.service`，运行在独立的 `.venv-proxy` 虚拟环境中）原生管理。
> 
> **结论**：两种安装模式的**唯一区别**，仅在于**「网易云音源服务（musicbox）」以何种方式运行与隔离**。

---

### 方式 A：Docker 容器模式（推荐）

适合绝大多数已在 fnOS「应用中心」启用 Docker 的用户，享有最干净的环境隔离与省心的更新体验。

- **前置条件**：
  * 必须先在 fnOS「应用中心」安装好 Docker。**脚本不会擅自安装 Docker 引擎**；若未安装，向导会友好提醒并引导切换至 Host 模式。
- **会部署什么**：
  * 通过 `docker-compose.yml` 在本地构建并启动唯一的音源容器：
    1. **`fnmusic-musicbox`**：监听 **`0.0.0.0:8770`**（网易云；对外绑定是为了让局域网浏览器能打开二维码图片扫码登录）；
  * 若不需要浏览器扫码方式，可把 compose 里的端口改为 `127.0.0.1:8770:8000`，改用 `./netease_login.sh` 在终端扫码；
- **容器网络与权限**：
  * 容器内运行无特权（以非 root 的普通用户运行）；
  * 数据卷严格挂载并隔离在当前项目目录下的 `musicbox-data/` 目录中，不与系统其他目录发生交叉。

---

### 方式 B：Host 宿主机本地服务模式（纯净无 Docker）

适合未安装 Docker、不希望引入容器虚拟化，或追求极致轻量与低资源占用的机器。

- **适用场景**：
  * 未装 Docker 或 NAS 内存/CPU 资源极为宝贵的环境。
- **会部署什么**：
  * **独立的 Python 虚拟环境**：在当前项目目录下分别创建 `.venv-musicbox` 与 `.venv-proxy`。依赖严格限制在各自虚拟环境内，**绝不污染系统全局 Python 环境**；
  * **注册轻量 systemd 服务**：
    1. `fnmusic-musicbox.service`：`0.0.0.0:8770`（局域网扫码）；
- **数据与缓存管理**：
  * 所有运行时数据（音频缓存 `cache/`、用户收藏 `online_favorites/`、历史记录 `play_history/`、网易云配置与缓存 `musicbox-data/`）严格保存在当前项目根目录下，**绝对不会散落到系统其他地方**。

---

### 双模式透明对照表

| 对比维度 | 方式 A：Docker 容器模式（推荐） | 方式 B：Host 宿主机本地服务模式（纯净无 Docker） |
| :--- | :--- | :--- |
| **推荐指数** | ⭐⭐⭐⭐⭐（环境隔离最彻底） | ⭐⭐⭐⭐（免 Docker、极致轻量） |
| **适用群体** | 已安装 Docker，注重系统纯净度与隔离性 | 未装 Docker、低内存小主机、或追求直接运行 |
| **前置要求** | fnOS 应用中心安装 Docker（**脚本不擅自安装**） | 仅需宿主机具备 `python3` 及 `python3-venv` |
| **核心代理部署** | 宿主机 systemd 服务（`.venv-proxy` 独立虚拟环境） | 宿主机 systemd 服务（`.venv-proxy` 独立虚拟环境） |
| **音源运行形态** | Docker 容器（通过 `docker-compose` 编排管理） | 宿主机 systemd 服务（通过独立 Python venv 隔离） |
| **部署组件与端口** | • `fnmusic-musicbox`：`0.0.0.0:8770`（局域网可扫码） | • `fnmusic-musicbox.service`：`0.0.0.0:8770` |
| **Python 环境隔离** | 依赖封装在容器镜像内，宿主机零依赖污染 | 项目目录下 `.venv-musicbox`，不污染全局 |
| **权限与安全性** | 容器内无特权用户运行，隔离网络端口 | 独立 systemd 进程，仅监听本地回环网络 |
| **数据落盘路径** | 项目根目录 `cache/`、`online_favorites/`、`musicbox-data/` | 项目根目录 `cache/`、`online_favorites/`、`musicbox-data/` |
| **常规日常管理** | `docker logs -f fnmusic-musicbox`<br>`docker compose ps` | `journalctl -u fnmusic-musicbox -f`<br>`systemctl status fnmusic-musicbox` |
| **一键恢复直连** | 执行 `./restore.sh`（秒级切回原生直连，保留音源与数据） | 执行 `./restore.sh`（秒级切回原生直连，保留音源与数据） |
| **一键彻底卸载** | 执行 `./restore.sh --full`（自动停止并删除 Docker 容器） | 执行 `./restore.sh --full`（自动停止并注销 systemd 音源服务） |

---

### 两种模式的清理与卸载保障（零残留承诺）

不论您采用哪种模式安装，项目均提供了完善、清晰的还原与彻底卸载方案：

1. **日常无损还原**：
   ```bash
   ./restore.sh
   ```
   * 会立即复位 `/var/run/trim_music.socket`，停用代理服务，秒级恢复官方原生直连；
   * 音源服务与本地缓存数据完好保留，日后执行 `./extend.sh` 可秒级重新启用。
2. **彻底清理卸载**：
   ```bash
   ./restore.sh --full
   ```
   * **在 Docker 模式下**：自动停止并删除 `fnmusic-musicbox` 容器；
   * **在 Host 模式下**：自动停止并禁用 `fnmusic-musicbox.service`，移除 `/etc/systemd/system/fnmusic-ext.service`；
   * 两种模式下都会**额外清理 v1.x 遗留**的 `fnmusic-musicdl` / `fnmusic-lxmusic` 容器与 systemd unit；
   * 真正做到系统级服务干净利索、彻底无残留。

---

## 2. 一键安装与配置

### 交互向导安装（新手首选）

```bash
./install.sh
```

向导将自动执行环境安全预检，并提供直观的交互选择：
1. **安装模式**：输入 `1`（Docker 模式）或 `2`（Host 模式）；
2. **音质**：`lossless`（无损，默认）/ `exhigh`（极高）/ `higher` / `standard`。账号无对应权益时自动回退，不会因此播放失败；
3. **PushPlus 推送提醒（可选）**：是否启用、token（不回显输入）、群组编码。用于登录失效 / VIP 临期提醒；不使用直接回车跳过；
4. **降级策略**：未扫码登录时是否降级为只播免费曲目（默认开启，关掉则未登录完全不提供在线播放）；
5. **每日推荐**：是否抓取网易云官方「每日推荐」歌单（默认开启，需登录）；
6. **一键启用**：向导完成后直接确认即可调用 `./extend.sh` 自动接管上线，并进入扫码登录流程。

> ⚠️ v2.0 起已无「音源多选」环节。仍带 `--sources` / `--llm-*` 的老命令行**会被接受但忽略并告警**，
> 不会因此报错退出。

### 非交互静默部署示例（进阶运维 / 自动化脚本）

```bash
# 示例 1：推荐配置 —— Docker 模式 + 自动启用
./install.sh --non-interactive --mode docker --extend

# 示例 2：纯净轻量 —— Host 宿主机模式（独立 venv + systemd，免 Docker）
./install.sh --non-interactive --mode host --extend

# 示例 3：启用 PushPlus 提醒 + 指定音质（token 保存在项目本地 .env 中，权限为 600）
./install.sh --non-interactive --mode docker \
  --pushplus-token 'YOUR_PUSHPLUS_TOKEN' \
  --pushplus-topic 'optional-group-code' \
  --daily true \
  --free-only-on-logout true \
  --extend

# 示例 4：安装收尾直接扫码登录
./install.sh --non-interactive --mode docker --extend --qr
```

---

## 3. 启用与还原

```bash
# 1. 启用扩展接管（包含全链路健康与流式验收，失败自动秒级回滚）
./extend.sh

# 2. 还原官方原生直连（保留音源组件与本地缓存）
./restore.sh

# 3. 深度彻底还原（同时停止并删除音源 Docker 容器或宿主机 systemd 音源服务）
./restore.sh --full
```

---

## 4. 网易云登录扫码（关键步骤）

v2.0 起**在线音源完全来自你扫码登录的那一个私人网易云账号**。不登录时只能播免费曲目，
且不会有「每日推荐」，因此扫码登录是使用本扩展的核心前置动作。

**方式一：终端扫码（推荐）**

```bash
./netease_login.sh          # 等价于 ./install.sh --qr 或 ./extend.sh --qr
```

终端会绘制 ASCII 二维码，用【网易云音乐 App】扫码确认。脚本具备：
- 二维码约 3 分钟有效，**过期自动刷新**（总超时 15 分钟）；
- 每 3 秒轮询登录状态，区分「等待扫码 801 / 已扫码待确认 802 / 成功 803 / 已过期 800」；
- 已登录时直接跳过，不重复出码；
- 不想现在登录可按 `Ctrl+C` 跳过——扩展会以免费曲降级模式继续运行。

**方式二：局域网浏览器扫码（备选）**

```text
http://<飞牛NAS的IP地址>:8770/api/v1/auth/login/qr.png
```

> 该方式要求音源服务对外绑定 `0.0.0.0:8770`（默认如此）。若你已把 compose 端口收紧为
> `127.0.0.1:8770:8000`，则只能用方式一。

**查看登录状态**

```bash
curl -s http://127.0.0.1:8770/api/v1/auth/detail
```

```json
{"ok":true,"data":{"logged_in":true,"nickname":"某用户","user_id":"123","vip_type":11,"vip_expires_ms":1800000000000}}
```

登录凭证自动持久化在本地 `musicbox-data/` 目录中（Docker 模式为容器卷挂载，Host 模式为项目目录），
重装或升级都不需要重新扫码。

> 网易云公网 API 只接受中国区域访问。若 NAS 部署在海外，需在音源服务侧配置合适的出口网络，
> 否则会返回空结果。注意 `runner.py` 会**主动剥离代理环境变量**以保证国内直连，
> 海外场景需要另行处理。

---

## 5. 每日推荐工作机制

v2.0 起「每日推荐」**直接抓取网易云官方日推**，不再由大模型凭空生成——网易云本身就是推荐引擎，
它比任何本地口味模型都更了解你的听歌画像。用户登录飞牛音乐 Web 端或 App 后，左侧歌单列表顶部
将出现一张「每日推荐 MM-DD」歌单：

1. **拉取官方日推**：调用 musicbox 的 `/api/v1/recommend/daily`，内部走
   `musicbox recommend songs`（即网易云 `/weapi/v3/discovery/recommend/songs`）；
2. **可播性过滤**：与其他在线曲目一样，批量校验真实直链，拿不到直链或只能试听的曲目一律剔除；
3. **封面与音质补齐**：批量拉 `/api/v1/songs/detail` 补高清封面并按 SQ/HR 改判无损格式；
4. **按自然日缓存**：当天内稳定复用缓存；跨天自动重建并清理旧缓存文件；
5. **多用户隔离**：每个飞牛用户有独立的歌单 GUID 与独立缓存目录（底层网易云账号只有一个，
   因此内容一致，隔离是为了红心/收藏不串号）。

**未登录时不注入日推。** 没有登录态就拿不到账号个性化推荐，接口只会返回一份与网易云
「飙升榜热门填充」同质化的兜底列表——与其塞一个名不副实的歌单，不如让官方歌单列表保持原样。
此时 `healthz` 的 `daily` 字段为 `need_login`。

相关配置：`FNMUSIC_DAILY_ENABLED`（默认 `true`）、`FNMUSIC_DAILY_LIMIT`（默认 `20`）。

---

## 6. 健康检查与验收

在终端执行以下命令探测代理服务与上游各组件的连通状态：

```bash
# 探测代理端点健康状态
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz

# 运行本地自动化测试集
.venv-proxy/bin/python -m pytest proxy/tests -q
```

`healthz` 响应正常示例（已登录 + 已配推送）：
```json
{
  "ok": true,
  "version": "2.0.0",
  "upstream": "ok",
  "musicbox": "ok",
  "netease": {
    "logged_in": true, "nickname": "某用户", "vip": true, "vip_days_left": 126,
    "free_only": false, "checked_at": 1789000000, "age_s": 3
  },
  "daily": "ok",
  "pushplus": "enabled"
}
```

字段含义：
- `ok`：**上游官方后端连通正常 且 音源服务可运行 且（已登录 或 允许免费曲降级）**。
  因此「音源服务 ok 但未登录、且 `FNMUSIC_FREE_ONLY_ON_LOGOUT=false`」时 `ok` 为 `false`
  属预期行为，不是故障；
- `upstream`：官方 trim-music 后端是否连通；
- `musicbox`：网易云音源服务进程状态，`ok` / `fail` / `disabled`；
- `netease.logged_in`：当前是否已扫码登录；
- `netease.free_only`：正处于「只播免费曲」降级模式；
- `netease.vip` / `vip_days_left`：登录账号是否 VIP 及剩余天数（PushPlus 临期提醒据此触发）；
- `daily`：`ok`（可用）/ `need_login`（未登录）/ `disabled`（已关闭）；
- `pushplus`：`enabled` / `disabled`。

> v2.0 已移除 `musicdl` / `lxmusic` / `llm` 字段。若你的监控脚本还在断言这些键，需要同步更新。

## 7. 常见问题排查

### 容器日志刷 `PermissionError: [Errno 13] Permission denied: '/app/app.py'`

v1.2.1 及更早版本的已知问题：镜像内源码文件权限继承了仓库检出时的 umask。
若曾在 umask 077 的环境（root shell、`sudo git clone` 等）下检出仓库，`app.py` 为 600，
容器内非 root 的 `appuser` 无法读取，uvicorn 启动失败并随 `restart: unless-stopped` 无限重启。

v1.2.2 起已修复（镜像内文件统一 `--chown=appuser` 且权限 644，与宿主机文件权限解耦）。
升级方法：

```bash
git pull && ./install.sh
```

Dockerfile 的变更会使对应构建层缓存失效，重新安装时会自动重建镜像，无需 `--no-cache`。

### 构建时报 `failed to resolve source metadata for python:3.13-slim ... 401 Unauthorized` 或拉取超时

多为 fnOS 等系统在 Docker daemon 全局配置的镜像加速器（如 `docker.fnnas.com`）异常所致：
BuildKit 解析 `python:3.13-slim` 元数据时会先经过该加速器，失败后不会自动回退官方 Docker Hub，
`docker compose up --build` 随即失败。

v1.2.3 起安装脚本会在构建前自动探测可用源：**国内镜像优先**（完整镜像源引用直连，绕开 daemon
加速器，真实拉取验证），逐个尝试 docker.1ms.run / docker.m.daocloud.io / docker.1panel.live /
hub.rat.dev，全部失败再兜底官方源，结果缓存到 `.env` 的 `FNMUSIC_BASE_IMAGE`。
全程不修改系统 Docker 配置，仅本应用构建生效。

手动指定（例如自动探测全部失败、或偏好特定镜像源时）：

```bash
# 方式一：安装时通过环境变量指定
BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim ./install.sh

# 方式二：写入 .env（之后所有重建自动沿用）
# FNMUSIC_BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
```

自定义国内镜像候选列表：设置环境变量 `FNMUSIC_DOCKER_MIRRORS`（空格分隔，按序尝试）。

Dockerfile 的变更会使对应构建层缓存失效，重新安装时会自动重建镜像，无需 `--no-cache`。

---

### 搜索能出结果，但 VIP / 无损曲目播不了

这是**预期行为**，不是故障。v2.0 起全部在线音源来自你扫码登录的那一个私人网易云账号：

- 未登录 → 只有免费曲目可用（`FNMUSIC_FREE_ONLY_ON_LOGOUT=true` 时）；
- 已登录但账号非 VIP → 免费曲目可用，VIP 曲目会被服务端直接过滤掉，根本不出现在搜索结果里；
- 已登录且是 VIP → VIP / 无损 / 已购付费专辑曲目均可播放。

确认当前状态：

```bash
curl -s http://127.0.0.1:8770/api/v1/auth/detail
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
```

看 `netease.logged_in` 与 `netease.vip` 两个字段即可判断。若 `logged_in` 为 `false`，执行
`./netease_login.sh` 重新扫码。

### 登录态掉线（cookie 过期）

网易云的登录凭证会过期。表现是：原本能播的 VIP 曲目突然消失、`healthz` 里
`netease.logged_in` 变成 `false`、`netease.free_only` 变成 `true`、`daily` 变成 `need_login`。

代理内置后台巡检（默认每小时一次，`FNMUSIC_LOGIN_CHECK_INTERVAL` 可调），检测到登录态翻转时
会通过 PushPlus 推送提醒。处理办法就是重新扫码：

```bash
./netease_login.sh
```

已缓存到本地的音频文件不受影响，仍可离线播放。

### 「每日推荐」歌单没有出现

按顺序排查：

1. `curl -s http://127.0.0.1:8770/api/v1/auth/detail` → `logged_in` 必须为 `true`。**未登录时
   刻意不注入日推**，因为拿不到账号个性化推荐；
2. `curl -s 'http://127.0.0.1:8770/api/v1/recommend/daily?limit=5'` → 应返回
   `{"ok":true,"data":[...]}`。若返回 `{"ok":false,"error":"not_logged_in"}` 回到第 1 步；
3. 检查 `.env` 里 `FNMUSIC_DAILY_ENABLED` 是否为 `true`；
4. `healthz` 的 `daily` 字段会直接给出结论：`ok` / `need_login` / `disabled`。

飞牛音乐客户端可能缓存了歌单列表，重启 App 或刷新网页后再看。

### PushPlus 收不到推送

推送失败只会记日志，不影响播放，所以需要主动查：

```bash
journalctl -u fnmusic-ext -f | grep -i pushplus
```

| 现象 | 原因与处理 |
| :--- | :--- |
| 日志 `pushplus rejected code=903` | token 无效，到 pushplus.plus 个人中心重新复制 |
| 日志 `pushplus rejected code=905` | **账号未实名认证**。PushPlus 自 2024-08 起未实名用户无法调用发送接口 |
| 日志 `pushplus rejected code=900` | 触发免费档限流（每日 200 次 / 每分钟 5 次 / 同内容每小时 3 条） |
| 日志 `pushplus rejected code=888` | 积分不足 |
| `healthz` 里 `pushplus` 为 `disabled` | `FNMUSIC_PUSHPLUS_ENABLED=false`，或 token 为空（留空自动视为关闭） |
| 完全没有 pushplus 日志 | 配置未生效。`.env` 修改后需 `./extend.sh --force` 或 `systemctl restart fnmusic-ext` 重载 |

代码层已内置同内容 1 小时去重 + 全局最小间隔 12 秒的双重节流；token 无效或未实名属永久性配置
错误，会停止重试并打 `ERROR` 级日志，不会每分钟空转。

### 从 v1.x 升级后残留的旧音源服务

`install.sh` 会无条件清理 v1.x 遗留的 `fnmusic-musicdl` / `fnmusic-lxmusic` 容器与对应
systemd unit（它们会一直占着 8768 / 8772 端口并开机自启）。`.env` 里的 `FNMUSIC_MUSICDL_*` /
`FNMUSIC_LX_*` / `FNMUSIC_ONLINE_SOURCES` / `FNMUSIC_LLM_*` / `FNMUSIC_APT_MIRROR` /
`FNMUSIC_DEPLOY_MODE` 键由 `proxy/env_merge.py` 自动删除，其余自定义值一律保留
（合并前会自动备份 `.env`）。

手动确认：

```bash
docker ps -a | grep -E 'fnmusic-(musicdl|lxmusic)' || echo "无残留容器"
systemctl list-unit-files | grep -E 'fnmusic-(musicdl|lxmusic)' || echo "无残留 unit"
```
