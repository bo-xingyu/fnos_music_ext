# fnmusic-ext 飞牛应用包（.fpk）

把本仓库打包成可在 fnOS「应用中心 → 手动安装」直接安装的 `.fpk`。

```bash
./build_fpk.sh                      # 版本取自仓库根 VERSION，产物 → dist/fnmusicext-<ver>.fpk
./build_fpk.sh --version 2.0.1      # 指定版本
./build_fpk.sh --fnpack             # 若探测到官方 fnpack，额外做一次结构交叉验证
./build_fpk.sh -h                   # 全部参数
```

依赖：`bash` `tar` `gzip` `md5sum` `python3`（图标生成需要 `Pillow`，可用 `--skip-icons` 复用已有图标）。

---

## 产物结构

`.fpk` 本质是 **tar.gz**（无签名、无加密），结构与官方 `fnpack` 1.2.3 产物逐项对齐：

```
fnmusicext-2.0.0.fpk  (tar.gz)
├── app.tgz          app/ 目录【内容】 + config/ 副本（注意：没有 app/ 前缀层级）
├── cmd/             9 个生命周期脚本
├── config/          privilege + resource（JSON）
├── wizard/          install / config / uninstall（JSON 数组表单）
├── manifest         key = value 文本，末行 checksum = MD5(app.tgz)
├── ICON.PNG         64×64
└── ICON_256.PNG     256×256
```

`app.tgz` 解开后是应用的实际内容，安装到 `/var/apps/fnmusicext/target/`：

```
proxy/                 拦截代理源码（app.py / admin_ui.py / netease_auth.py / pushplus.py /
                       netease_items.py / recommend.py / env_merge.py / version.py /
                       run_proxy.sh / requirements.txt）
musicbox-service/      网易云音源 HTTP 包装（app.py / runner.py / netease_ext.py）
bin/                   fpk 专属胶水层（见下）
ui/                    桌面入口声明 config + images/icon_{64,256}.png
restore.sh             从仓库根同步，socket 还原逻辑原样复用
netease_login.sh       从仓库根同步，终端扫码登录
VERSION
config/                privilege + resource 副本（fnpack 行为，一并打进 app.tgz）
```

**刻意不打包**：`install.sh` / `extend.sh` / `docker-compose.yml` / `ensure_base_image.sh` /
`fnmusic-ext.service` / `docs/` / `proxy/tests/` —— 这些属于 git 克隆安装路径，
fpk 走 `cmd/*` 生命周期，打包进去只会增加体积与审核困惑。

---

## `bin/` 胶水层

| 文件 | 作用 |
| :--- | :--- |
| `fnmusic-lib.sh` | 共享函数：路径常量、日志、`TRIM_TEMP_LOGFILE` 错误上报、PID 管理、HTTP 探活、降权执行、后台守护启动、停机标志 |
| `setup.sh` | 把载荷 stage 到 `$TRIM_PKGVAR/app`、建 venv 装依赖、按向导值生成 `.env`（0600）。幂等 |
| `start.sh` | 先起音源服务（降权），再接管 socket 起代理（root）；清理"活着但接管丢失"的僵尸代理；起看门狗 |
| `stop.sh` | 落主动停机标志 → 停看门狗 → 停管理页面 → 停代理 → 还原 socket → 停音源服务 |
| `status.sh` | 运行中 exit 0，未运行 exit 3 |
| `watchdog.sh` | 看门狗（v2.7）：代理死亡 / socket 接管丢失（官方后端重启会重绑 `trim_music.socket`）/ musicbox 死亡时自动调 `start.sh` 幂等恢复；`FNMUSIC_WATCHDOG_INTERVAL_S=0` 关闭 |
| `restart_services.sh` | 只重启代理与音源服务，**不动管理页面进程**（见下） |

### 为什么 stage 到 `$TRIM_PKGVAR/app` 而不是就地运行 `$TRIM_APPDEST`

`proxy/run_proxy.sh` 与 `restore.sh` 以「脚本所在目录的父目录」为 `BASE_DIR`，
并在其下寻找 `.venv-proxy` / `.env` / `cache` / `proxy`。把代码与数据放进同一个
`BASE_DIR`，就能**原样复用**那套已在生产验证过的 socket 接管与复位逻辑，
不必在 fpk 里另写一份高风险实现。

- `$TRIM_APPDEST`：只读载荷（升级时被整包替换）
- `$TRIM_PKGVAR/app`：运行目录。升级时只覆盖代码，**保留** `.env`、`musicbox-data/`
  （网易云登录凭证）、`cache/`、`online_favorites/`、`play_history/`、`.venv-*`

---

## 权限模型（审核相关，务必先读）

`config/privilege` 使用 `"run-as": "root"`。这偏离了官方「默认 `package` 用户」的建议，
原因是本应用的核心机制就是接管 `/var/run/trim_music.socket`：

1. 重命名官方 socket、在 `/var/run` 下新建同名 socket，需要对 `/var/run` **目录**的写权限；
   删除 socket 文件同样取决于目录写权限，而非文件权限。包用户没有该权限，无法完成接管。
2. `uvicorn --uds` 必须在接管点自己创建 bind socket，因此**代理进程也无法通过
   `runuser` 降权**——这是 socket 接管架构的固有代价。

在官方权限最小化要求允许的范围内已尽量降权：

| 进程 | 身份 | 理由 |
| :--- | :--- | :--- |
| 生命周期脚本 `cmd/*` | root | 官方文档允许：「只有生命周期脚本确实需要执行特权准备任务时才使用 Root 模式」 |
| 代理 `proxy/app.py` | root | 必须在 `/var/run` 下创建 bind socket，无法降权 |
| 管理页面 `proxy/admin_ui.py` | root | 要写入 root 代理读取的 `.env`，并调用需要 root 的重启脚本 |
| 看门狗 `bin/watchdog.sh` | root | 要调用 `start.sh`（其中的代理必须 root）；纯本地 sleep 循环，不监听任何端口 |
| 音源服务 `musicbox-service` | **`$TRIM_USERNAME`（专用包用户）** | 仅监听 unix/TCP 私有端点、读写自有数据目录，完全满足官方「长期运行且对外提供访问的进程应尽量非 root」 |

管理页面虽然以 root 跑，但**对外不开放任何端口**：它只监听 `${TRIM_APPDEST}/ui.sock`，
唯一入口是飞牛统一网关 `/app/fnmusicext`，网关会先校验 NAS 登录态、再注入身份 Header。
换句话说 root 权限只暴露给「已通过飞牛登录的管理员」。

降权通过 `fnmusic-lib.sh` 的 `lib_spawn` 实现：优先 `runuser`，退化到 `su`，
两者都不可用时（受限容器、包用户未创建）**如实告警后以当前身份继续**，
绝不静默假装降权成功。

`config/resource` 为 `{}`：不声明 `data-share`。本应用写入的是飞牛音乐自己的曲库目录
（由代理从官方配置 `shared_library.path` 自动探测），不是本应用的私有共享目录；
以 root 运行已获得写入能力，再声明共享目录只会扩大可见范围。

---

## 生命周期与 socket 安全

| 脚本 | 行为 |
| :--- | :--- |
| `install_init` | 纯预检，无副作用：检查飞牛音乐 socket / Python 运行时 / 必备命令 / 运行身份，把问题写进 `TRIM_TEMP_LOGFILE` |
| `install_callback` | 调 `setup.sh` 建运行环境。不启动服务（`ctl_stop=true` 由系统随后调用 `main start`）。在用户可见日志里写明「还需扫码登录」 |
| `main start` | 音源服务 → 等待 healthz（不就绪只告警，不致命）→ socket 接管 → 等待代理 healthz（最长 75s）→ `chmod 666` 接管点 |
| `main stop` | 停代理 → **还原 socket** → 停音源服务 → 验证官方直连恢复 |
| `main status` | 代理 PID 存活 **且** 原路径确实由本扩展接管才算 running；音源状态仅信息展示不参与判定 |
| `upgrade_init` | 完整 stop（含 socket 还原）+ 备份 `.env`。**升级窗口内飞牛音乐必须是可用的官方直连状态** |
| `upgrade_callback` | 重新 stage + 重建 venv（保留 `.env` / 登录凭证 / 缓存）→ start |
| `uninstall_init` | 完整 stop + 还原 socket，并**核实**原路径已不再是本扩展；仍是则报错要求重启飞牛音乐 |
| `uninstall_callback` | 按向导选择清理数据 |
| `config_init` | 纯校验向导值（音质枚举 / 模板枚举 / 监听地址 / 日推数量范围 / pip 源前缀 / token 长度），把非法值挡在落盘前 |
| `config_callback` | 只重写 `.env`（`REBUILD_VENV=false` 跳过装依赖）→ 重启生效 |

**socket 还原是最高风险环节**，因此做了双层保障：

1. 主路径调用仓库的 `restore.sh`（生产验证过的四态探测复位，绝不误删官方活动 socket）；
2. `restore.sh` 依赖 `sudo`（它为交互式非 root 用户而写），在 sudo 不可用的环境必然失败。
   因此 `stop.sh` 内置了一份**同等四态语义**（`proxy` / `trim-music` / `absent` / `unknown`）
   的兜底复位，不依赖 sudo。身份不明时保守保留原路径 socket 不做删除。

### PID 管理的一个坑（已修）

把「bash 函数调用」放到后台再取 `$!` 是错的：`$!` 拿到的是执行该函数的 bash 子壳，
子壳随后退出、真正的服务进程被 reparent 到 init，于是 stop 时 kill 的是一个早已死亡的
PID，服务进程永久泄漏。降权场景更糟——`runuser`/`su` 是中间父进程且默认不转发信号。

`lib_spawn` 的解法：让子进程**自己**把 `$$` 写进 pidfile，再 `exec` 成目标程序。
`bash -c` 里的 `$$` 与 exec 后的进程 PID 相同，所以 pidfile 永远指向服务真身，
中间层是否存活都不影响停机准确性。

---

## 桌面入口与管理页面

`manifest` 声明 `desktop_uidir = ui` + `desktop_applaunchname = fnmusicext.main`，
`app/ui/config` 按官方统一网关模型注册入口：

```json
{
  ".url": {
    "fnmusicext.main": {
      "title": "飞牛音乐扩展",
      "icon": "images/icon_{0}.png",
      "type": "iframe",
      "protocol": "",
      "gatewayPrefix": "/app/fnmusicext",
      "gatewaySocket": "ui.sock",
      "url": "/app/fnmusicext",
      "allUsers": false
    }
  }
}
```

于是飞牛桌面出现一个图标，点开即在 iframe 内加载 `/app/fnmusicext`；
飞牛**先校验 NAS 登录态**，再把请求转发到 `/var/apps/fnmusicext/target/ui.sock`，
并注入 `X-Trim-Userid` / `X-Trim-Isadmin` / `X-Trim-Username`。

`allUsers=false`：非管理员连图标都看不到，与后端的管理员限制一致。

### `proxy/admin_ui.py` 的设计要点

- **只信网关 Header**。缺少 `X-Trim-Userid`/`X-Trim-Username` 即判定「未经网关的裸 socket 访问」，
  页面返回 403 说明页、接口返回 403 JSON。官方明确要求「不要信任客户端传入的用户 ID」。
  `X-Trim-Userid` 还要过形状校验（`^[\w.\-@]{1,64}$`），可疑值直接拒绝并记日志。
- **登录与改配置一律要求 `X-Trim-Isadmin: true`**。扫码会决定整台 NAS 用哪个网易云账号做音源，
  token 属于凭据，都不该让家庭成员随手改。
- **前缀兼容**。网关可能带 `/app/fnmusicext` 前缀转发，也可能已剥离，中间件统一剥掉；
  页面内一律用相对路径请求，两种情况都能工作。
- **token 三处防泄漏**：`type=password` 输入不回显；`GET /api/config` 返回打码串；
  `/api/logs` 回显前用 `.env` 与进程环境变量**两处**真实值做脱敏（只读环境变量的话，
  用户刚保存、尚未被任何进程加载的新 token 就会原样出现在日志里）。
- **写入走白名单**。页面能改的键由 `CONFIG_FIELDS` 固定映射到 `FNMUSIC_*`，
  提交任何未知字段（含 `FNMUSIC_HOME`、`PATH` 这类 env 原名）直接 400，杜绝任意 `.env` 键注入。
  所有值按枚举/范围/URL/控制字符规则校验；写入复用 `env_merge`，先备份 `.env.bak`，
  再原子替换并保持 0600。
- **`留空 = 不改`**。password 字段不回显真实值，照原样写回就会把用户的 token 冲掉，
  因此「未提交该项」「提交空串」「提交打码串 `••••••••`」三种情况都判定为不修改。
- **日志接口防穿越**。`what` 走固定枚举白名单，解析后再用 `realpath` 前缀复核，
  `../../etc/passwd` 之类一律 400。
- **页面零外部依赖**。不引任何 CDN / 外链脚本 / 外链字体，内网与离线 NAS 可直接使用；
  自适应 `prefers-color-scheme`；用户名做 HTML 转义（Header 来的值不该被当成可信 HTML）。

### 为什么 `restart_services.sh` 刻意不动管理页面

保存配置后需要重启才能生效。如果重启流程把 ui 进程也停了，**发起重启的这个 HTTP 请求就会
把自己的服务杀掉**——响应永远回不来，用户只看到页面转圈卡死，还以为保存失败了。
所以拆出 `restart_services.sh`：只停/起 `proxy` 与 `musicbox`，ui 全程存活。

停机顺序也因此调整为「先停 ui，再停代理，再还原 socket，最后停音源」：
先断掉 ui 这条可能在停机途中又发起一次重启的路径，避免竞争。

### socket 权限与孤儿进程

- `ui.sock` 创建后 `chmod 666`：unix socket 的 **connect 权限取决于文件写位**，
  不设 666 网关进程可能连不上，表现为桌面图标点开 502。
- `stop.sh` 除按 pidfile 停进程外，还会用 `lib_kill_stale_by_sock` 按**本应用自己的 socket
  绝对路径**精确匹配清理孤儿。因为一旦启动竞态导致 pidfile 丢失，只 `rm` socket 文件是没用的——
  旧进程仍持有那个已删除的 inode，新进程绑到新 inode，两者并存且旧的再也回收不掉。
  匹配串限定为 `--uds <本应用 socket 路径>`，不会波及任何其它 uvicorn。
- `build_fpk.sh` 自检会核对 `ui/config` 的 `gatewaySocket` 与 `fnmusic-lib.sh` 里的
  `UI_SOCK` 文件名**完全一致**：不一致的话桌面图标点开就是 502，且极难排查。

## 配置向导

`wizard/` 三个表单收集的值由飞牛以**同名环境变量**注入生命周期脚本（无 `TRIM_` 前缀）。
`bin/setup.sh` 负责把它们转写成代理读取的 `FNMUSIC_*` 键并落到 `$RUN_DIR/.env`（0600）。

| 向导字段 | → 环境变量 | 说明 |
| :--- | :--- | :--- |
| `wizard_netease_quality` | `FNMUSIC_NETEASE_QUALITY` | lossless / exhigh / higher / standard |
| `wizard_free_only_on_logout` | `FNMUSIC_FREE_ONLY_ON_LOGOUT` | 未登录时降级为只播免费曲 |
| `wizard_daily_enabled` | `FNMUSIC_DAILY_ENABLED` | 网易云官方每日推荐 |
| `wizard_daily_limit` | `FNMUSIC_DAILY_LIMIT` | 日推曲目数 1..100 |
| `wizard_musicbox_bind` | `FNMUSIC_MUSICBOX_BIND` | `0.0.0.0` 可局域网浏览器扫码 / `127.0.0.1` 更安全 |
| `wizard_pushplus_enabled` | `FNMUSIC_PUSHPLUS_ENABLED` | 推送总开关 |
| `wizard_pushplus_token` | `FNMUSIC_PUSHPLUS_TOKEN` | `type=password`，不回显、不落日志 |
| `wizard_pushplus_topic` | `FNMUSIC_PUSHPLUS_TOPIC` | 群组编码，可空 |
| `wizard_pushplus_template` | `FNMUSIC_PUSHPLUS_TEMPLATE` | markdown / html / txt / json |
| `wizard_pip_index` | `FNMUSIC_PIP_INDEX` | 依赖安装镜像源 |
| `wizard_remove_data` | 仅 `uninstall_callback` 读取 | 是否删缓存与收藏 |
| `wizard_remove_library_cache` | 仅 `uninstall_callback` 读取 | 是否删写入曲库的在线音频 |

### 两个刻意的设计选择

- **token 留空 = 保持不变**。`wizard/config` 提交时若 token 为空，`setup.sh` 沿用
  `.env` 里已保存的值。否则用户在设置页改一下音质就会把自己之前填的 token 冲掉
  （password 字段不会回显当前值，用户无从察觉）。表单里也用 `tips` 明确写了这一点。
- **卸载时删除曲库文件只依据 `.ref` 记录逐条精确定位**。扩展每写一个音频文件都会在
  `cache/<safe_guid>.ref` 里记下文件词干，卸载时据此删 `<词干>.<各音频后缀>` 与同名 `.lrc`。
  **绝不使用通配符扫曲库目录**，避免误删用户自有音乐；同时对记录做绝对路径与 `..` 校验，
  拒绝任何越权路径。且该行为默认关闭，需用户在卸载向导里显式勾选。

---

## 图标

`ICON.PNG`(64×64) 与 `ICON_256.PNG`(256×256) 由 `tools/make_icons.py` 程序化绘制
（深靛蓝→品红对角渐变圆角方块 + 白色八分音符 + 声波曲线），两个尺寸由同一套矢量参数
按比例生成，保证 64px 下笔画不糊。重新生成：

```bash
python3 fpk/tools/make_icons.py            # 输出到 fpk/
python3 fpk/tools/make_icons.py --sizes 512 --out /tmp   # 额外尺寸
```

官方要求：PNG/JPG、sRGB、≤1024 KB、完整正方形画布、视觉主体为圆角矩形、64px 下清晰可辨。
脚本会对体积超限直接失败。上架还需单独提交截图（截图不入包）。

---

## 构建自检

`build_fpk.sh` 打包后会解包逐项核对，任一项不符立即非零退出：

1. 根层 7 项齐备（`app.tgz` `cmd` `config` `wizard` `manifest` `ICON.PNG` `ICON_256.PNG`）
2. `cmd/` 下 9 个生命周期脚本齐全且 `bash -n` 通过
3. `config/privilege`、`config/resource`、`wizard/*` 均为合法 JSON；向导每项有 `type`，
   非 `tips` 项有 `field`，且**不得**使用 `TRIM_` 前缀
4. `manifest.checksum == MD5(app.tgz)`
5. `manifest` 的 `version` / `appname` 与命令行一致
6. `app.tgz` 可解开，含 `config/` 副本与全部关键代码，且**不带** `app/` 前缀层级
7. 载荷内无 `test_*.py` / `__pycache__` / `.DS_Store` / `*.pyc` / `.env`
8. 载荷内无 v1.x 废弃配置键的**完整键名**读写
   （`env_merge.py` 里作为清理名单出现的裸前缀常量是刻意保留的，不算命中）
9. 图标尺寸与体积符合官方限制
10. 构建目录内检测到 `.env` / `*.pem` / `id_rsa*` 直接中止打包

`--fnpack` 会额外用官方 `fnpack build` 对同一份源树构建一次，比对顶层结构与 checksum 规则。

产物用 `--sort=name --mtime=@1700000000 --owner=0 --group=0` 打包，**同输入同哈希**，便于 CI 校验。

---

## 安装到真机

三选一（无需任何「开发者模式」开关）：

1. 应用中心 → 左下角「手动安装」→ 选择 `.fpk`
2. SSH 到设备：`appcenter-cli install-fpk fnmusicext-2.0.0.fpk`
   带向导值：`appcenter-cli install-fpk fnmusicext-2.0.0.fpk --env config.env`
   （`config.env` 为逐行 `key=value`，如 `wizard_pushplus_token=xxx`）
3. 项目目录已在设备上时：`cd /path/to/project && appcenter-cli install-local`

安装后仍需在终端扫码登录网易云（见仓库根 README），否则只有免费曲目、且没有每日推荐。

日志位置：

```
$TRIM_PKGVAR/logs/info.log       # 生命周期与启停全量日志
$TRIM_PKGVAR/logs/proxy.log      # 代理进程输出
$TRIM_PKGVAR/logs/musicbox.log   # 音源服务输出
$TRIM_PKGVAR/logs/setup.log      # venv 创建与 pip 安装输出
$TRIM_PKGVAR/logs/restore.log    # socket 还原过程
$TRIM_PKGVAR/logs/{upgrade,uninstall,config}.log
```

---

## 已知限制与待确认事项

- **审核政策未确认**：官方无公开审核细则，开发者后台尚未上线，上架走「先锋交流群」人工提交。
  涉及第三方音源/版权抓取的应用是否会被拒没有书面记录，建议直接向群内工作人员确认。
  本应用的立场是：音源完全来自使用者本人扫码登录的合法账号权益，不含免登录破解渠道。
- **`os_min_version=1.1.3100`** 取自官方 Native 案例（该版本引入统一网关）。本应用**不使用**
  统一网关，实际最低可用版本可能更低，未在真机逐版本验证。
- **未做真机验证**：以上结构与自检依据官方 `fnpack` 1.2.3 产物逆向 + 社区包交叉验证，
  并在沙箱的 mount namespace 里完整跑通了 `start/status/stop` 生命周期（含 socket 接管与还原、
  重复 stop 幂等、停机后无残留进程）。安装器是否强制校验 `checksum` 无法离线验证，
  但打包时始终写入正确值。
- **`platform=all`**：包内不含任何架构相关二进制（纯 Python + bash）。若将来引入编译产物需改为 `x86`/`arm` 分包。
- **不声明 `service_port`**：本应用没有对外的 Web 端口——管理页面走统一网关的 unix socket，
  8770 只是音源服务的内部端点且**默认只绑 `127.0.0.1`**（它的接口无鉴权，含"发起扫码登录"，
  对外暴露等于同网段任何人都能扫自己的号顶掉你的网易云登录）。故 `checkport=false`
  且不声明 `service_port`，飞牛也不会在启动前做端口占用检查。
  若确实要纳入飞牛远程访问的端口转发，社区有 `.sc` + `port-config` 的做法，
  但**官方文档无记载、字段语义未确认**，本包暂未使用。
- **管理页面的 root 身份**：`admin_ui.py` 以 root 运行，因为它要改 root 代理读取的 `.env`
  并调用需要 root 的重启脚本。它不监听任何端口、只经由校验 NAS 登录态的统一网关可达，
  且限管理员。若审核对此有异议，可行的收敛方向是把「写配置」与「重启」拆给一个由
  `config_callback` 驱动的最小特权辅助进程，但那是更大的改动。
