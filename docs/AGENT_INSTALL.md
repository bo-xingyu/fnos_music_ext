# Agent 安装提示词

本提示词专为 AI CLI Agent（如 OpenCode、Claude Code、Cursor 等）自动化部署与运维设计。
人工详细安装步骤、部署模式差异与背景说明见：[人工安装与部署指南](INSTALL.md)。

把下面代码块内的整段内容复制给 Agent。要求：只改本仓库与本机配置，禁止改飞牛系统文件，禁止把密钥写入 git。

```text
你在一台已安装飞牛 NAS（fnOS）和「飞牛音乐」(trim.music) 的机器上工作。
仓库是 fnmusic-ext v2.0：无侵入 Unix Socket 代理，用网易云单一音源扩展官方 App 的
在线搜索/播放/歌词/封面/边听边存/每日推荐能力。

【v2.0 单源化要点（与 1.x 的关键差异，先读懂再动手）】
- 只有网易云一个在线音源（musicbox）。musicdl（酷我/咪咕）与 lxmusic（洛雪）已整体移除；
  仓库里不再有 musicdl-service/ 与 lxmusic-service/ 目录，端口 8768 / 8772 不再使用。
- 音源完全来自使用者「扫码登录的那一个私人网易云账号」的权益。未登录时按
  FNMUSIC_FREE_ONLY_ON_LOGOUT（默认 true）降级为只播免费曲目；置 false 则未登录不提供在线播放。
- 每日推荐改为抓取网易云官方日推（/weapi/v3/discovery/recommend/songs），不再使用 LLM。
  因此未登录时不会注入「每日推荐」歌单；FNMUSIC_LLM_* 配置项已废弃。
- 旧版命令行参数 --sources / --llm-base-url / --llm-api-key / --llm-model 仍被接受但已忽略并告警，
  不要因为它们还能解析就以为多音源仍然存在。
- install.sh 会无条件清理 v1.x 遗留的 fnmusic-musicdl / fnmusic-lxmusic 容器与 systemd unit。

【核心运行原则与硬约束（违反即失败）】
1. 禁止修改 /usr/trim/nginx 以及任何 nginx 配置；飞牛系统更新或配置重载会回写覆盖。
2. 禁止 patch trim-music 官方二进制，禁止写入官方 music.db。
3. 禁止把 PushPlus token、密码、cookie 写进源码、测试、README、commit、issue，或 echo 打印到
   终端日志。所有密钥仅保存在仓库根目录 .env（文件权限 chmod 600）。读取 token 必须用不回显方式，
   展示时必须脱敏（如只显示前 4 位 + ****）。
4. 绝对禁止擅自安装 Docker 引擎：fnOS 的 Docker 必须在「应用中心」由系统管理员安装。若环境未安装
   Docker，必须选用 Host 宿主机模式，严禁执行 apt-get install docker 等命令。
5. 核心代理运行原则：无论 Docker 还是 Host 模式，核心代理服务（fnmusic-ext proxy）都必须由宿主机
   systemd（运行在项目根目录 .venv-proxy 独立虚拟环境中）原生管理，负责无侵入接管
   /var/run/trim_music.socket。两种模式的区别仅在于「网易云音源服务（musicbox）」以何种方式运行与隔离。
6. 一键扩展 ./extend.sh 与一键还原 ./restore.sh（含彻底清理 ./restore.sh --full）必须始终保持可用；
   扩展失败必须安全秒级回滚到官方直连。
7. 音源组件与端口规划：
   - musicbox: https://github.com/darknessomi/musicbox（PyPI 包名 NetEase-MusicBox）。
     Docker / Host 均映射 0.0.0.0:8770，便于局域网扫码
     http://<NAS-IP>:8770/api/v1/auth/login/qr.png；登录凭证持久化在 musicbox-data/。
   - 容器名固定 fnmusic-musicbox，宿主机模式 systemd unit 固定 fnmusic-musicbox.service。
   - FNMUSIC_NETEASE_ENABLED 为 false 时在线功能全部关闭，等于扩展失去意义，应向用户确认而非静默继续。

【自动化部署执行步骤】

步骤 1：准备脚本权限
在仓库根目录执行：
  chmod +x install.sh extend.sh restore.sh netease_login.sh ensure_base_image.sh proxy/run_proxy.sh

步骤 2：环境预检（Preflight Checks）与模式决策
在执行安装前进行环境检测判断（install.sh 会自动执行安全预检，Agent 应先行确认或理解预检逻辑）：
  1. Python 环境：检查系统具备 python3 (>=3.11) 以及 python3-venv 模块。若缺失，需先安装：
     sudo apt-get update && sudo apt-get install -y python3 python3-venv。
  2. 其它依赖：git、curl、jq（jq 仅终端扫码流程 netease_login.sh 需要）。
  3. 管理员权限：确认当前执行用户具备 sudo 权限（非交互脚本需免密 sudo）。
  4. 飞牛音乐运行套接字：确认官方套接字 /var/run/trim_music.socket 存在。若不存在，提示用户必须
     先在 fnOS「应用中心」安装并启动「飞牛音乐」。
  5. Docker / Host 模式决策：
     - 运行 command -v docker 检测 Docker 环境：
       * 若已安装 Docker：采用 --mode docker（推荐，音源在 compose 轻量无特权容器中运行，隔离最彻底）。
       * 若未安装 Docker：采用 --mode host（使用项目内独立的 .venv-musicbox 虚拟环境运行并注册为
         systemd 服务，纯净轻量，零全局依赖污染；切勿擅自安装 Docker）。

步骤 3：执行安装与一步到位启用（--extend）
推荐使用 --extend 参数，让 install.sh 在依赖就绪、服务配置完成后自动调用 ./extend.sh 进行
Unix Domain Socket 接管与端到端验收自检，实现安装+接管一步到位：
  - Docker 模式（推荐）：
    ./install.sh --non-interactive --mode docker --extend
  - Host 模式（宿主机独立 venv）：
    ./install.sh --non-interactive --mode host --extend
  - 若用户提供了 PushPlus token，追加推送参数（token 仅写入 .env，禁止 echo 到日志）：
    ./install.sh --non-interactive --mode docker \
      --pushplus-token '<用户提供的 TOKEN>' \
      --pushplus-topic '<可选群组编码，无则省略>' \
      --extend
  - 其它可调参数：
      --daily true|false                 是否抓取网易云官方每日推荐（默认 true，需登录）
      --free-only-on-logout true|false   未登录时是否降级只播免费曲（默认 true）
      --qr                               安装收尾直接进入终端扫码登录
  - 若用户未提供 PushPlus token，切勿自行编造或启用推送（token 为空时自动视为关闭）。
  - 若安装时未加 --extend，则需在安装完成后显式执行 ./extend.sh。

步骤 4：扫码登录网易云（在线功能的前置条件）
音源取决于登录账号的权益，未登录只能播免费曲且没有每日推荐。
  - 终端扫码（推荐，ASCII 二维码 + 过期自动刷新 + 状态轮询）：
    ./netease_login.sh        # 或 ./extend.sh --qr
  - 局域网浏览器扫码（备选）：http://<NAS-IP>:8770/api/v1/auth/login/qr.png
  - 查询登录状态：curl -s http://127.0.0.1:8770/api/v1/auth/detail
    返回 data.logged_in / data.nickname / data.vip_type / data.vip_expires_ms。
  - 若用户暂时不扫码，如实告知当前处于免费曲降级模式即可，不要反复重试扫码。

步骤 5：端到端健康检查与验收
部署完成后执行以下验证命令：
  1. 探测代理接管与组件健康端点：
     curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
     期望 "upstream": "ok" 且 "musicbox": "ok"。
     注意 v2.0 的返回体已变化：不再有 musicdl / lxmusic / llm 字段，改为：
       netease: {logged_in, nickname, vip, vip_days_left, free_only, checked_at, age_s}
       daily:   "ok" | "need_login" | "disabled"
       pushplus:"enabled" | "disabled"
     顶层 "ok" 的新语义是「上游可用 且 音源服务可用 且（已登录 或 允许免费曲降级）」。
     因此「音源服务 ok 但未登录且 free_only_on_logout=false」时 ok 会是 false，属预期而非故障。
  2. 音源服务自检：
     curl -s http://127.0.0.1:8770/healthz
     curl -s 'http://127.0.0.1:8770/api/v1/search?keyword=晴天&type=song&limit=3'
     curl -s 'http://127.0.0.1:8770/api/v1/recommend/daily?limit=5'   # 未登录返回 error=not_logged_in
  3. Shell 语法检查：
     bash -n install.sh extend.sh restore.sh netease_login.sh ensure_base_image.sh proxy/run_proxy.sh
  4. Python 代码语法检查：
     .venv-proxy/bin/python -m py_compile proxy/app.py proxy/recommend.py proxy/netease_auth.py \
       proxy/pushplus.py proxy/netease_items.py proxy/env_merge.py
     （可选运行自动化测试集：.venv-proxy/bin/python -m pytest proxy/tests -q）

步骤 6：还原机制与彻底卸载规范（知悉与必要时使用）
  - 日常无损还原（秒级恢复官方原生直连）：
    ./restore.sh
    复位 Unix Socket，停用 fnmusic-ext 代理服务，秒级恢复官方直连；保留音源容器/服务、登录凭证
    及本地缓存数据，日后执行 ./extend.sh 可秒级重新上线。
  - 彻底清理卸载（系统级服务零残留）：
    ./restore.sh --full
    在复位 Socket 并停用核心代理的同时，自动停止并删除音源 Docker 容器（fnmusic-musicbox）或注销
    宿主机 systemd 音源服务（fnmusic-musicbox.service），并一并清理 v1.x 遗留的
    fnmusic-musicdl / fnmusic-lxmusic 容器与 unit。

【完成汇报规范】
任务完成后用简短中文输出总结，内容包含：
1. 部署模式判断（Docker 模式或 Host 模式，及环境预检依据）；
2. 音源与端口说明（musicbox: 0.0.0.0:8770）；
3. 网易云登录状态（logged_in / nickname / 是否 VIP / VIP 剩余天数）——严禁复述 cookie 或 token；
4. 每日推荐与降级策略是否开启（daily_enabled / free_only_on_logout）；
5. PushPlus 是否启用（只说 enabled/disabled 与是否配了群组，严禁输出 token 原文）；
6. healthz 接口探测响应 JSON；
7. extend 链路接管与验收状态。
```
