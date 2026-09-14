# 更新日志 (Changelog)

本项目所有显著变更均记录于此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [2.9.23] - 2026-09-14

**播飞牛本地曲库慢：这次不是网易云，是官方 HLS 实时转码。**

日志里 HLS 的 guid 是 `188dde79…` 这种 32 位十六进制——**飞牛官方曲库的 guid**，
既不是我们的 `online:netease:` 也不是 `local:file:`。而这三种 guid 在 `/track/hls`
里的待遇完全不同：

| guid 类型 | HLS 处理 |
|---|---|
| `local:file:` | 伪 HLS 单分片指回 `/track/stream`（v2.9.7 起，**绕过转码**） |
| `online:netease:` | 伪 HLS 单分片指回 `/track/stream`（**绕过转码**） |
| **官方本地曲目（32 位 hex）** | **原样转发 → 官方后端实时转码成 fMP4 分片** ❌ |

也就是说，播本地歌走的是官方**实时转码**路径，而且这条路径以前**完全没有观测**——
只知道「慢」，分不清是**转码启动慢**（点下去要等）还是**播出后跟不上**（分片尖刺）。
两者解法完全不同，没有这个区分只能瞎猜。

新增诊断段「官方 HLS 实时转码」：

- **转码启动开销** = 客户端拿到 m3u8 到取回第一个分片的间隔（就是"点下去要等"的时间）
- **分片转发耗时** = 平均值看常态、最大值看抖动（最大远大于平均＝转码吞吐跟不上）

同时新增 `FNMUSIC_HLS_LOCAL_BYPASS`（**默认 false**）：打开后用伪 HLS 直出原始流，
跳过官方转码。**默认关是刻意的**——客户端主动要求 HLS 转码通常意味着它吃不下原始
编码（本地多为 FLAC），直出有可能直接放不出来。而且直出是原始码率（无损 30MB 起），
数据网络下并不省流量。装完先看诊断数据，确认是启动慢再决定要不要开。

顺带：`music.db` 失效记录过滤生效了（已忽略 96 条，索引从 917 降到 824）。

## [2.9.22] - 2026-09-14

**新增音质策略「局域网听无损 / 其他一律 320k」（`by_lan`）。**

表面上看这跟已有的「按网络分别设置」很像，差别**只在判不出网络的那一档**：

| 策略 | 判出 lan | 判出 cellular | **判不出（unknown）** |
|---|---|---|---|
| `by_network` | WiFi 档 | 流量档 | **WiFi 档（照发母带）** ❌ |
| `by_lan` | 无损 | 320k | **320k** ✅ |

`by_network` 里只有 `network == "cellular"` 才降档，于是 unknown 走的是 WiFi 档——
这正是前面几轮「数据网络卡顿」的成因（诊断页显示 `network=unknown` + musicbox
`quality=jymaster` 实锤）。`by_lan` 反过来定义：**只有真的看见私网 IP 才给无损**，
其余（含判不出的、远程公网的）一律 320k。

宁可在判不出时少给一档音质（多半听不出来），也不能在窄管道上灌母带（立刻感觉到卡）——
两种错误的代价不对等。

配套改动：本地优先放行无损改用**同一套局域网口径**（`quality.on_lan`）。两边各写一份
判断必然漂移，漂移的结果就是「在线降到 320k、本地却照灌 30MB FLAC」这种自相矛盾，
正是 2.9.21 修掉的「越修越慢」。现在 `unknown` 也按非局域网处理，不传网络线索时同样
按非局域网处理（fail-safe，宁可不放行）。

已知代价：若家里一次 XFF 都没透传，WiFi 也会停在 320k。诊断页「网络判定计数」里
`lan` 有计数就说明透传正常（你的机器实测 16 次全判对）。

## [2.9.21] - 2026-09-14

**「比之前还慢、播放《最后一首情歌》要等 7~8 秒」——本地优先修好之后反而变慢的悖论。**

诊断里本地优先 14 查 8 中，看着是好事，但**命中的全是 `.flac`**：

```
[命中] 最后一首情歌 - 苏琛  → 苏琛 - 最后一首情歌.flac
[命中] 盗将行 - 花粥/马雨阳  → 花粥,马雨阳 - 盗将行.flac
[命中] 遗失的心跳 - 萧亚轩   → 萧亚轩 - 遗失的心跳.flac
[命中] 易燃易爆炸（Cover 陈粒）→ 阿冷 - 易燃易爆炸（Cover 陈粒）.flac
```

数据都要从 NAS 传到手机，走的是**同一条窄管道**，所以决定快慢的是体积不是来源：

| 来源 | 单曲体积 | 数据网络 |
|---|---|---|
| 本地 FLAC | **30~40 MB** | 7~8 秒起步 |
| 在线 exhigh 320k | ~9 MB | 快 3~4 倍 |

2.9.17 时本地优先几乎不命中（30/1），全都走在线 320k；**命中率修好之后反而开始灌
30MB 的本地无损**——这就是「越修越慢」的成因。`FNMUSIC_LOCAL_FIRST_ANY_CLASS=true`
（2.9.16 为解决「本地 MP3 被档位卡死」改的默认值）在数据网络下把事情推向了反面。

新增 `FNMUSIC_LOCAL_FIRST_CELLULAR_LOSSY_ONLY`（默认 true）：**流量网络下本地优先只吃
有损文件**，本地无损让给在线省流档。有损（mp3/m4a）体积与在线 320k 相当，本地仍更快
（省掉外网往返），所以只在本地是无损时才让。WiFi / 局域网行为完全不变。

诊断页新增「流量网络：本地无损让给在线省流档（已让 N 次）」——不记次数就只能靠用户
描述快慢，没法确认规则到底生效了几次。

## [2.9.20] - 2026-09-14

**2.9.19 回测确认：降档已生效，另修掉 `file-missing` 的真凶。**

先结论：2.9.19 真机诊断第一次把「WiFi 还是数据」答清楚了——
`局域网=0 / 远程(公网)=12 / 本机转发(回环)=0`，musicbox 日志**全程 `quality=exhigh`、
无一条 jymaster**，对比 2.9.17 的 `quality=jymaster`，降档确实落地了（我上一版猜的
"回环中继伪装成局域网"被证伪，`relay=0`，XFF 透传是好的）。

本次修的是诊断里这两行：

```
[未中] やわらかな光 - やまだ豊 （file-missing）
   索引里最接近: やまだ豊 - やわらかな光 / やわらかな光
```

标题明明匹配上了，却打不开文件。原因在 `music.db` 里的**失效记录**（文件已被搬走
或删除）：它有两个坏处，第二个是隐性的——

1. 匹配上了但 `os.path.isfile()` 失败 → 就是这句看不懂的 `file-missing`；
2. 更糟：`_get_index` 按 **path 相等**去重，失效记录的 path 会把**目录扫描到的同名
   真文件**误判成「已在库里」而跳过。于是「目录扫描新增」恒为 0，坏记录永远没人顶替。

现在建索引时先按可达性过滤一次：失效记录直接剔除（不再挡住 fs 的真文件），并在诊断页
标出「已忽略 music.db 失效记录 N 条」。另外 `file-missing` 现在会带上**失败的完整路径**
——不写出来，它就是一句没有主语的废话（到底路径拼错了还是文件真没了，查不下去）。

## [2.9.19] - 2026-09-14

**回答「现在是 WiFi 还是数据」——以前答不了，因为关键证据被自己丢了。**

诊断页原来只有一行 `客户端 IP 线索(XFF): 局域网=823 次 远程(公网)=21 次 例: xxx`，
三个问题让它无法回答任何问题：

1. **样例混存**：局域网/远程样例挤在同一个最多 6 条的列表里，先到先得。823:21 的比例下
   远程样例必然被挤掉——而远程恰恰是唯一需要看清的那一类。现在按类分开各留 4 条。
2. **没有时间戳**：只有累计次数，"我刚才明明用数据播了"这句话和证据对不上。
   现在分别给出 `最近一次 Xs 前`。
3. **回环地址被当成局域网**（本次真正的 bug）：XFF 是 `127.0.0.1` / `::1` 说明请求是
   **本机 nginx 或飞牛远程中继（fnConnect）转发过来的**——这个 IP 证明的是"转发者在
   本机"，不是"客户端在局域网"。以前一律判成 `lan`，于是走 WiFi 档发 jymaster 母带。
   这很可能就是「数据网络下卡、诊断页却显示局域网」对不上的原因。现在这类请求单独计
   为 `relay`，并按"来源不明"处理（沿用粘性结论，可配 `FNMUSIC_UNKNOWN_AS_CELLULAR`
   一律按流量档）。

诊断页现在直接给出读法：远程样例是真公网 IP 且 `remote_last` 很小 = 正在远程/数据访问；
局域网样例是 192.168/10.x 且 `lan_last` 很小 = 正在家里 WiFi；`relay` 次数很大 = 源 IP
证明不了客户端在哪。

## [2.9.18] - 2026-09-14

**「WiFi 还行、数据网络卡顿」的元凶是音质档位没降下来，不是延迟。**

诊断页这行是关键线索：

```
当前判定 : level=jymaster  network=unknown  source=manual:wifi
```

`by_network` 策略里**只有** `network == "cellular"` 才会走省流档；而 `network_of()`
在请求里既没有网络类型提示、又没有 `X-Forwarded-For` 时返回 `unknown`——**unknown
不等于 cellular，于是照旧发 jymaster（Hi-Res 母带）**。jymaster 实测约 1 MB/s，
320k 只有 40 KB/s，差 25 倍；在移动数据 + 远程中继的窄管道上，前者必然断断续续。
日志里 `GET /api/v1/song/167705/url?quality=jymaster` 就是实锤。

本次改动：

1. **网络判定带「粘性」，且有效期刻意不对称。** 明确判出过 cellular/lan 就记下来，
   后续判不出的请求沿用；但**「上次是流量」记 30 分钟，「上次是局域网」只记 5 分钟**。
   两种误判的代价完全不对等——误判成流量只是音质低一档，你可能根本听不出来；误判成
   局域网就是在窄管道上照发母带，直接卡成幻灯片。所以宁可偏保守。
2. **判定结果按类计数并显示在诊断页**（新增「网络判定计数」与「粘性结论」两行）。
   以前 unknown 是隐形的：它既不落进 `client_ips.lan` 也不落进 `remote`，于是「到底
   有多少请求压根判不出网络」永远没有答案，只能靠猜。现在数字直接摆出来。
3. **新增 `FNMUSIC_UNKNOWN_AS_CELLULAR`**（默认 false）：一条线索都没有时一律按流量
   档处理。若诊断页显示 unknown 占大头，把它打开即可立刻见效。默认关是因为家里若
   恰好一次 XFF 都没透传，开着会让 WiFi 也长期停在省流档。
4. **诊断页没有 request 时不再写死 unknown**（`resolve(None)` 以前硬编码，于是页面
   永远显示 WiFi 档 + jymaster，而实际播放已经在降档——看起来像策略没生效）。

## [2.9.17] - 2026-09-14

**本地优先几乎从不命中的根因：music.db 的 `title` 字段存的是完整文件名，不是标题。**

诊断里这行一直摆在那儿，但直到现在才读出它的意思：

```
自测: 能匹配上自己（Beyond - 光辉岁月.flac → /vol1/.../Beyond - 光辉岁月.flac）
[命中] Beyond - 光辉岁月.flac - None
```

`title = "Beyond - 光辉岁月.flac"`、`artist = None`。索引直接拿它归一化成
`beyond光辉岁月flac`，而查询用的是网易云给的纯标题 `光辉岁月`——**两个键永远对不上**。
索引看着有 915 首，实际一首都匹配不了。自测之所以显示"能匹配上自己"，是因为它
拿索引里自己的 title 去查自己，当然中。

现在一行记录会登记多组 `(标题, 艺术家)` 候选：

1. title 去掉音频扩展名后的原样；
2. 它再按 `歌手 - 歌名` 拆开（artist 也由此补上真实值，**艺术家匹配终于有东西可比**）；
3. `path` 的文件名——最可靠，它一定是真实文件名；
4. 文件名同样拆开。

顺带修掉两处会误导人的显示：

* `entries` 改为按**唯一 path** 统计（一首歌现在登记多个键，直接数列长会翻几倍，
  看着像索引爆炸）；
* 「索引里最接近的标题」不再只用 difflib——中文歌名普遍很短（「出山」两个字
  根本过不了 0.5 的相似度门槛），改为**先用包含关系捞、再用 difflib 补**。

## [2.9.16] - 2026-09-14

**「本地明明有这首歌，为什么还走网易云」——找到了：音质档位把本地文件卡死了。**

### ① 本地优先默认不再挑音质档位

真机的组合是：`level=jymaster`（无损类）+ 本地是 MP3。严格同类规则会把本地 MP3
**一刀切拒掉**，转头去网易云要无损——而你自己的 proxy.log 写着：

```
CDN 回的是 audio/mpeg，曲目却声明无损（online:netease:306877）
```

**绕一圈出了外网、多花几百毫秒，拿回来的还是 MP3。**

所以 `FNMUSIC_LOCAL_FIRST_ANY_CLASS` 默认改为 `true`：本地只要有同名曲就播，零外网、
起步最快。同名多首时仍然**优先取无损那条**（排序没变）。坚持「非无损不播」的把
管理页「本地优先：不挑音质档位」取消勾选即可。

### ② 预热不再把自己堵死

真机回测：单次预热从 400ms 涨到 **1100ms**，命中率却是 0%。原因是我们自己加的
「列表下发即预热」——后台批量刷新十几个歌单时，每个歌单都触发预热，请求全堆到
单进程的 musicbox 上，**连正在播的那首都跟着变慢**。

* `FNMUSIC_PREFETCH_ON_LIST` 默认改为 `false`（只在播放时预热下一首）；
* 预热加并发闸门 `FNMUSIC_PREFETCH_CONCURRENCY`（默认 1）与超时
  `FNMUSIC_PREFETCH_TIMEOUT`（默认 6s）——宁可这次不预热，也不拖慢播放。

### ③ 未命中时给出「索引里最接近的标题」

`title-not-in-index` 一行其实有两种可能：本地真没这首歌，或者只是名字写法不同。
以前只能靠猜。现在诊断页直接列出索引里最接近的几个真实标题：

```
[未中] 遗失的心跳 - 萧亚轩 （title-not-in-index）
   索引里最接近: 遗失的美好 / 类似爱情
```

一眼分清该去补文件，还是该改匹配规则。

## [2.9.15] - 2026-09-14

**按 2.9.14 真机回测校准：预热统计口径修正、一次预热多首、本地优先标题匹配放宽。**

### ① 统计口径错了：「78 次播放」其实是十几首歌

一次流式播放客户端会连发好几个 Range 请求，每次都算一次「播放」，于是命中率与
冷/热耗时均值全被重复请求稀释。现在**同一首歌 10 分钟内的续传请求折叠成一次播放**
（单独计 `repeat_plays`），冷/热对比只取每首歌的首次请求。

### ② 一次预热 N 首（默认 3）+ 列表下发即预热头首

`FNMUSIC_PREFETCH_LOOKAHEAD`（1–5，管理页可调）。只取 JSON 不下音频，多预热几首
几乎不额外耗流量。另加 `FNMUSIC_PREFETCH_ON_LIST`：歌单列表一下发就预热它的第一首，
用户点开时直链已经是热的（只预热 1 首——真机 musicbox 会同时收到十几个歌单的列表
请求，预热多了反而把它自己堵住）。

### ③ play-start 日志直接标 cold / warm

以前要评估预热效果得去对日志。现在每行自带标记，grep 一下就能把两组分开比：

```
play-start netease online:netease:96100 519ms cold
play-start netease online:netease:96101 41ms warm
```

### ④ 本地优先：标题括号尾巴

网易云的「晴天 (Live)」「演员（伴奏）」在本地库里往往就叫「晴天」「演员」。
归一化前先剥掉括号尾巴，建索引与查询两端用同一套键——真机 30 次查询只中 1 次，
这类标题差异是主因之一。

### ⑤ 诊断页：目录扫描的真实数字

「目录扫描新增 0」常常不是没扫到，而是扫到的全都已在 music.db 里。
现在分开显示「扫到 N 个音频文件 / 其中 M 首是补充」，不再让人误判成扫描坏了。

## [2.9.14] - 2026-09-14

**本地曲库优先终于真的能命中了（之前索引恒为空）；新增「下一首预热」把起步网络往返提前做完；修掉「.flac 装 MP3」的坏缓存文件。**

### ① 本地曲库优先：不是没做，是索引从来就是空的

真机 `music.db` 里**根本没有曲目表**——诊断能读到的只有 `shared_library`（一排曲库目录）。
而 v2.8~v2.9.13 的索引只从 `music.db` 建，于是它永远为空、一次都匹配不上，界面上却没有任何迹象，
看起来就是「这功能没实现」。

现在改成 **music.db + 曲库目录文件系统扫描** 双来源合并：

| 环节 | 之前 | 现在 |
| --- | --- | --- |
| 索引来源 | 只有 music.db（真机上无曲目表 → 恒空） | music.db + 目录扫描，按 path 去重，db 条目优先（带真实标签） |
| 匹配时机 | 直链取回来之后才判 → 519ms 网络往返照付 | **联网之前**先用 metadata 缓存的标题判一次，命中直接出流 |
| 艺术家匹配 | 归一化后全等，写法一变就失配 | 任一侧缺失 / 相等 / 一侧包含另一侧（长度≥2）都算兼容 |
| 可观测性 | 无 | 诊断页给出「db 多少首 / 目录多少首 / 索引多少首 / 最近 10 次查询及原因」，索引为空时明确写出原因 |

新增 `FNMUSIC_LOCAL_FIRST_ANY_CLASS=true` 可放开音质档位约束（只要本地有就播），
`FNMUSIC_LOCAL_FS_MAX_FILES` / `FNMUSIC_LOCAL_FS_MAX_DEPTH` 控制扫描规模。

### ② 下一首预热（T1）

真机 `play-start netease online:netease:96100 519ms` —— 一首没缓存的在线歌，从点击到出声
要 519ms，绝大部分花在 `gather(解析直链, 取信息)` 两个 musicbox 往返上。而客户端**完全不预取**
（播放后十几秒只请求 metadata / 封面 / heartbeat）。

`stream` 请求里只有 guid、没有歌单上下文，但**列表是我们下发的**：把下发过的有序列表记进环形缓冲，
播放时用它推断下一首，后台提前把直链与元数据取回来。**只取几 KB 的 JSON，不下载任何音频字节**——
随机播放时推断会错，但错了的代价只是几个 KB。

* 按 guid 单飞去重，60s 内不重复；永不阻塞当前播放，失败静默只记日志；
* 可关：`FNMUSIC_PREFETCH_NEXT=false`；
* 可证：诊断页给出「调度 / 完成 / 失败 / 推断不出 / 已预热 / 命中率 / 冷启动 ms / 预热后 ms / 省下 ms」。

### ③ 扩展名 bug：`.flac` 装的其实是 MP3

以前落盘扩展名只信曲目声明（`sq/hr` → flac）。真机 CDN 实际给的是 MP3，于是落出
「.flac 装 MP3」的坏文件：写标签报 `is not a valid FLAC file`，本地曲库还把它当无损匹配。

现在**按文件头嗅探真实格式**（fLaC / ID3 / OggS / RIFF-WAVE / wvpk / MAC / AIFF / ftyp / MPEG 帧同步）
后再定扩展名，声明与实际不一致时记一条日志。响应 Content-Type 行为保持不变。

### 其他

* 管理页「应用设置」新增「本地曲库优先」「下一首预热」两个开关；
* 诊断页新增「本地曲库优先」「下一首预热（T1）」两个区块；
* 新增诊断接口 `/_ext/localfirst`、`/_ext/prefetch`。

## [2.9.13] - 2026-09-14

**授权其实一直是成功的——是我们把官方下发的结果当成了「降级」。顺带修掉诊断页一条假警报。**

### ① 系统早就把授权结果给出来了，我们却还在打那个 500 的网关

2.9.12 的诊断第一次把系统注入的变量值摊开，答案就在里面：

```
TRIM_DATA_ACCESSIBLE_PATHS = /vol1/1000/存储空间1/汇总音乐
TRIM_DATA_SHARE_PATHS      = （空）
```

这个值和你在「应用设置 → 授权目录」里勾选的完全一致 —— **系统已经把该路径的 ACL 授予了本应用，并把结果直接写进了进程环境变量**。
这就是飞牛官方授权的权威结果。可我们之前只把它当成"网关查不到时的兜底"，照旧每次去打
`trim.file.getSharedAccessibleFolders`，拿到稳定的 `HTTP/1.1 500 / 200006 Internal Error`，
然后给授权卡片打上「config（降级）」——明明是已授权，界面上却像出了故障。

本次改动：

| 之前 | 现在 |
| --- | --- |
| 无条件查网关，失败后回退 env/config，标记 `source=config`、`degraded=true` | env 有值 → **直接用，跳过网关**，`source=env`、`degraded=false` |
| 诊断页常驻一行「网关返回 : Internal Error」 | 显示「网关查询 : 已跳过 — 系统已通过 TRIM_DATA_ACCESSIBLE_PATHS 下发授权目录」 |
| 只认 `trim.file.getSharedAccessibleFolders` 一个 req 名 | 依次试 `getSharedAccessibleFolders` / `sharedAccess` / `getSharedFolders`，并试一次带 `uid` 的调用 |

点「刷新状态」时仍会主动探一次网关，但结果单独列为**「网关主动探测（仅供参考）」**，
不参与来源判定——不会再让一次 500 把已授权显示成故障。

### ② 诊断页那句「music.db 不存在或未能打开」是假警报

同一份诊断里，音质策略区块说 `music.db 扫描 available=false`，本地曲库区块却说
`music.db 存在=true` 并读出了 `shared_library`。两者都对，因为是**同一页的两次判断**：

```python
def report(db_path=""):
    decision = resolve(None, db_path)     # by_network/fixed 策略下根本不读 db
    scan = _OBSERVED.get("db")            # → 永远是 None
```

`resolve()` 里只有 `follow_fnos` 策略才会走到 `preference_from_db`，你的策略是
`by_network`，直接就返回了，`_OBSERVED["db"]` 从头到尾没被填过。于是 note 打出了
"不存在或未能打开"——库就在那儿，只是没去查。

现在 `report()` 自己扫一遍（有 5 分钟缓存，不会每次都扫库），并把两种情况分开说：
没传路径 → "未传入 music.db 路径（无法扫描）"；传了但文件不在 → 才说文件不存在。

### 测试

- `test_trimgw.py`：`test_shared_folders_uses_env_paths_as_official_authorization`
  （env 有值时**不得**再去调网关，否则直接断言失败；`source=env` 且 `degraded=False`）、
  `test_shared_folders_falls_back_to_config_when_no_env`、`test_probe_shared_tries_req_aliases`。
- `test_quality_policy.py`：`test_report_scans_db_even_when_policy_does_not_need_it`。

## [2.9.12] - 2026-09-14

**两处体验优化：歌单名去掉日期、文件位置显示真实路径。**

### ① 本地每日推荐歌单名不带日期了

之前的歌单名叫 `本地每日推荐 09-14`，每天一变。副作用是：客户端侧每天像多了一张新歌单，旧的几张看着又像「昨天过期的」，
而用户其实只想每天打开同一张、内容自动换。现在固定为 **本地音乐每日推荐**。

内容照样每天换：随机种子仍然是 `random.Random(f"{user_guid}:{day}")`，guid 仍带日期
（`online:playlist:localdaily:{day}:{user}`），跨天自动重新抽签，无需任何手动操作。

### ② 曲目「文件位置」显示真实路径

之前点开曲目详情，文件位置是：

```
local/835b6c756ffb7c6b3fde7851883519206e458ab1.mp3
```

这是我们内部按绝对路径 sha1 造的假路径——后缀是真的（客户端靠它判断容器格式），路径是假的。
现在改为写入**真实绝对路径**，并同时给出顶层 `path` / `filePath`，客户端无论读哪个字段都能拿到真实位置：

```python
real_path = str(entry.get("path") or "")
spec_path = real_path or f"local/{_sha}.{play_format}"   # 索引里没存路径时回退
audio_spec = {"path": spec_path, ...}
track["path"]     = spec_path
track["filePath"] = real_path or spec_path
```

播放链路不受影响：`/track/stream` 一直是按 guid 反查真实路径的，不依赖 audioSpec.path。
本地日推缓存 schema 从 3 升到 4，旧缓存自动重建（缓存里的曲目也补上了 `path` / `filePath`）。

### 测试

- `test_local_daily.py`：歌单名断言改为 `本地音乐每日推荐`，并确认不同日期仍产出不同内容（名字不变、内容变）。
- `test_local_files.py`：新增 `test_local_metadata_path_is_real_absolute_path`、
  `test_local_metadata_path_falls_back_when_index_has_no_path`。

## [2.9.11] - 2026-09-14

**修掉本地每日推荐「封面不是真实图」——真正的病根不在封面，在字段形状。**

### 起因

2.9.10 的诊断数据把真相摆到了面前：

```
封面探测 : 曲目=66  已查=20  有索引=20  内嵌图=17  同目录图=0  可用=17
```

**17 首有内嵌封面，图是有的。** 可 proxy.log 里：

```
GET /music/api/v1/static/cover?coverId=online%3Aplaylist%3Ane%3A6900072061
GET /music/api/v1/static/cover?coverId=online%3Aplaylist%3Ane%3A8443086236
... （ne:* 每个都请求了）
（唯独没有 coverId=online:playlist:localdaily:... 的请求）
```

客户端**压根没来要**本地日推的封面。也就是说：2.9.9 改的"用真实歌曲封面"
根本没机会生效，我 2.9.4 生成的那张 PNG 也从来没被用上 —— 用户看到的一直是
**飞牛客户端自带的默认占位图**。

对比网易云伪歌单的字段形状，差异一目了然：

| 字段 | 网易云伪歌单（`_channel_public_fields`） | 本地日推 |
|---|---|---|
| `coverId` | = guid | = guid ✅ |
| `coverUrl` / `cover_url` | **有** | **无** ❌ |
| `source` | `"netease"` | **`"local"`** ❌ |

两个嫌疑：缺 `coverUrl`，以及我在 `playlist/list` 里标的 `source: "local"`。
后者尤其可疑——飞牛的本地歌单本来就用 `source` 区分来源，标成 `local` 会让
客户端转去 music.db 找封面，自然不会来请求 `static/cover`。

### 改动

与其继续猜客户端的封面策略，不如**绕开它**：

1. 新增 `_local_daily_cover_track()`：找出歌单里第一张**确实有内嵌封面**的曲目。
2. 新增 `_apply_local_daily_cover()`：把歌单条目的 `coverId` **直接指向那首歌**
   （`local:file:<sha1>`），并补齐 `coverUrl` / `cover_url`。
   `local:file:` 这条封面路径已被真机验证能出图（日志里 200 OK）。
   取不到任何封面时保持原 `coverId` 不变。
3. **去掉 `source: "local"`**，改成 `"localdaily"`（客户端不认识的值，不会触发
   本地歌单的封面逻辑）。
4. 三个出口全部应用：`playlist/list`、`playlist/detail`、`playlist/batch-detail`。
5. 顺带：`trimgw` 增加读取 `TRIM_DATA_ACCESSIBLE_PATHS`（真机环境变量清单里
   确实有这个键），作为网关查不动时的授权目录兜底来源。

### 回归测试

* `test_apply_local_daily_cover_points_at_real_track` —— `coverId` 必须指向有图的
  曲目、`coverUrl` 指向 `static/cover`，且 **`source` 绝不能是 `"local"`**。
* `test_apply_local_daily_cover_keeps_guid_when_no_cover` —— 取不到图时不改 `coverId`。

## [2.9.10] - 2026-09-14

**封面与网关两件事都补齐「可观测性」——不再靠猜。**

### 起因

2.9.9 装完，排序已经对了，但还剩两个说不清的问题：

**① 封面不是真实图。** 可能原因至少三种，光看结果完全分不出来：

* 本地文件索引没建 → `lookup()` 查不到路径；
* 文件里确实没内嵌封面、同目录也没有 `cover.jpg`；
* 只扫了前 12 首，恰好前 12 首都没图（整张歌单明明有封面，却被误判）。

第 3 种是我自己埋的：为了不让封面请求变慢，只试前 12 首。曲库里前十几首
若是没内嵌图的无损文件，就会直接回退占位图——用户看到的就是"封面不是真实图"。

**② 网关 `Internal Error` 仍然无解。** 2.9.9 的诊断给出了关键新信息：

```
应用名候选     : fnmusicext          ← 只有一个候选
网关原始响应   : HTTP/1.1 500 Internal Server Error
                 {"reqId":"aab1c81f...","code":200006,"msg":"Internal Error","data":null}
```

**候选只有一个**，说明 `TRIM_PKGVAR` / `TRIM_PKGMETA` / `TRIM_APPNAME` 这些
**一个都没注入**，只能退回硬编码。而 token 明明是注入了的——说明系统确实在
按应用脚本的方式拉起进程，但没给应用名相关变量。要确定系统登记的应用名到底
叫什么，就必须先看清楚系统**实际**注入了哪些 `TRIM_*`。

顺带一提，HTTP 状态码是 **500**（不是 200），`data` 为 `null`——这是服务端
处理请求时真的出了错，不是"没授权"那类业务拒绝。

### 改动

1. **封面选取范围 12 → 60**：先看前 12 首（快），都没有再扩大到 60 首，
   避免"前十几首恰好没图"造成的误判。
2. **封面探测诊断** `_local_daily_cover_probe()`：管理页诊断里直接给出
   `曲目数 / 已查 / 有索引 / 内嵌图 / 同目录图 / 可用`，并**用一句话指出卡在哪**
   （索引没建 / 文件里没图 / 曲目为空），不再让用户猜。
3. **网关变量诊断** `trim_env_report()`：列出系统实际注入的全部 `TRIM_*`
   变量名，值按 `TOKEN/SECRET/PASS/KEY/CRED` 脱敏；管理页诊断额外打印
   `TRIM_PKGVAR` 等能反推应用名的那几个。
4. 封面命中/落空都写日志（命中记曲目 guid 与字节数，落空记探测结论）。

### 回归测试

* `test_local_daily_cover_probe_tells_where_it_is_stuck` —— 有索引但没图时，
  诊断必须明确指出是"文件里没内嵌封面"。
* `test_local_daily_cover_probe_reports_no_index` —— 索引没建要单独说，
  和"文件没图"是两种不同的病。
* `test_trim_env_report_lists_names_but_hides_secrets` —— 变量要列全，token 不能露。

## [2.9.9] - 2026-09-14

**本地日推封面改用真实歌曲封面；开放网关 `Internal Error` 的排错能力补齐。**

### 起因

2.9.7 之后本地每日推荐**可以播放**了（日志里 `track/stream`、`track/transcode/heartbeat`
都正常）。剩下三件事：

**① 歌单封面太丑。** 2.9.4 为了防止客户端因为封面 404 而整条不渲染，给本地日推
现生成了一张「唱片」PNG（zlib+struct 手搓，零依赖）。图能出来，但它是画的，
跟歌单内容毫无关系。用户反馈"太丑了，随便找一张歌单里的歌曲封面"——合理，
曲目本来就有封面（内嵌图或同目录 `cover.jpg`），没理由不用。

**② 授权卡片一直显示"尚未授权任何目录"，网关返回 `Internal Error`。**
按飞牛《错误码》文档，`Internal Error` 是 `code 200006`「具体业务模块内部错误」——
一个笼统到无法定位的错误。而《调用方式》文档确认我们的请求格式是对的：

```json
{"reqId":"1","req":"trim.file.getSharedAccessibleFolders","appName":"...","data":{}}
```

排下来最可疑的是 `appName`。而 `app_name()` 只从 `TRIM_APPNAME` / `TRIM_APP_NAME`
读，**这两个环境变量在真机上常常根本不注入**，于是回退到硬编码 `"fnmusicext"`。
系统按 appName 查授权记录，名字对不上就 200006。

更糟的是以前 `_http_post()` 把 **HTTP 状态行直接丢了**，只留 `code`/`msg`，
等于把最有价值的排错信息扔掉了。

### 改动

1. **封面**：新增 `_local_daily_playlist_cover()`，从歌单曲目里取第一张能拿到的
   真实封面（`local_files.cover()`：内嵌图 → 同目录 `cover.jpg`），命中过的曲目
   guid 记在 `_LOCAL_DAILY_COVER_SRC` 里复用，不必每次请求翻一遍歌单；
   一首都取不到才回退到生成的占位图（保证永远有图，客户端不会因 404 不渲染）。
2. **appName 候选**：`candidate_app_names()` 按可信度排序——系统注入的应用数据
   目录末段（`/vol1/@appdata/<appname>`，最权威）→ `TRIM_APPNAME` 等环境变量 →
   去前缀写法 → 硬编码 `fnmusicext`；`call()` 遇到 200006 就换下一个候选重试，
   参数错 / Forbidden / Unauthorized / NotFound 这几类换了也没用，不重试。
3. **原始响应留档**：`_http_post()` 记下 HTTP 状态行 + 响应体片段
   （`last_raw_response()`），并附在每个响应的 `http_status` 字段上。
4. **管理页**：授权卡片补显示「应用名候选 / 网关原始响应 / 说明」，
   用户把这段贴出来就能一眼判断是 appName 不对、scope 没生效还是系统版本问题。

### 回归测试

* `test_candidate_app_names_derives_from_system_pkgvar` —— 权威应用名取自系统目录末段。
* `test_call_retries_next_app_name_on_internal_error` —— 200006 时换候选再试。
* `test_call_does_not_retry_on_scope_or_token_errors` —— Forbidden 不重试。
* `test_http_post_records_status_line_and_raw_body` —— 状态行与原文必须留档。
* `test_local_daily_playlist_cover_uses_real_track_cover` —— 取到的是歌曲真实封面
  （断言字节是 JPEG 而不是生成的 PNG），并记住命中的曲目。
* `test_local_daily_playlist_cover_falls_back_when_no_track_has_cover` —— 回退到 None。

## [2.9.8] - 2026-09-14

**修掉本地每日推荐「排不到歌单列表第一位」。**

### 起因

用户反馈：「你生成的歌单排序也不在第一」。2.9.5 明明已经把默认顺序改成了
`localdaily,daily,...`，为什么还是沉底？顺着 `playlist/list` 的组装链路往回查，
发现歌单最终顺序由**三处互斥逻辑**依次决定，任意一处漏掉都会前功尽弃：

1. `channel_order()` 的大类顺序 —— 这里 `localdaily` 确实在第一 ✅
2. `apply_explicit_order()` 的手动顺序 —— **对大类顺序是「整体覆盖」** ❌
3. `stamp_display_order()` 按最终顺序盖时间戳

问题出在第 2 步。看 `_key()`：

```python
def _key(it: dict) -> tuple[int, int]:
    guid = str(it.get("guid") or "")
    for idx, tok in enumerate(tokens):
        if _token_matches(tok, guid):
            return (0, idx)
    return (1, 0)          # ← 匹配不上的一律丢到这里
```

匹配不上的条目全部拿到 `(1, 0)`，**排在所有已匹配条目之后**。而 `_token_matches`：

```python
def _token_matches(token: str, guid: str) -> bool:
    if token == "daily":
        return guid.startswith(DAILY_NS)
    return token == guid          # ← localdaily 走的是这条
```

`daily` 有前缀特判（它的 guid 带日期，写死会失配），**`localdaily` 没有**。
可本地日推的 guid 是 `online:playlist:localdaily:{日}:{用户}` —— **每天、每个用户
都变**。于是：

* 管理页「歌单顺序」里存的是**昨天**那个 guid → 今天匹配不上；
* 或者用户压根没排过、token 列表里没有它 → 同样匹配不上；
* 两种情况下它都被当成「没排到的新歌单」甩到列表末尾。

这就解释了为什么「改了默认值也没用」：大类顺序算得再对，也会被手动顺序整段覆盖。

顺带还发现第 4 处不一致：`env_merge.NEW_DEFAULTS` 里
`FNMUSIC_NETEASE_CHANNEL_ORDER` 的默认值**还是 2.9.5 之前的 `daily,localdaily,...`**
（改漏了），新装或补齐配置的用户会拿到 daily 在前。

另外 2.9.5 的 setup.sh 迁移只认「值完全等于旧默认值」才改：用户只要在管理页
保存过一次配置（哪怕只是勾掉一个 `fm`），值就再也不相等，迁移永远不触发。

### 改动

1. `_token_matches` 给 `localdaily` 加前缀特判（`LOCAL_DAILY_NS`），与 `daily` 对齐。
2. 新增 `pin_local_daily_first()`：在 `apply_explicit_order()` 之后做最后兜底，
   按大类顺序把本地日推放回它该在的位置；默认第一位，若用户在大类顺序里
   **显式**给了位置（`local_daily_pinned_index()`）则以配置为准。
3. `app.py` 列表组装处接入该兜底（在 `stamp_display_order` 之前）。
4. `setup.sh` 迁移放宽为幂等的「把 `localdaily` 提到最前，其余相对顺序不变」，
   不再依赖「完全等于旧默认值」。
5. 补掉 `env_merge` 漏改的默认值，四处（`playlists.DEFAULT_CHANNEL_ORDER` /
   `admin_ui` / `env_merge` / `setup.sh`）现在完全一致。

### 回归测试

* `test_local_daily_token_matches_by_prefix` —— token 按前缀认领带日期的 guid。
* `test_local_daily_survives_explicit_order_without_its_token` —— **先断言问题确实
  存在**（手动顺序下它沉到最后），再断言兜底后回到第一、其余顺序不变。
* `test_pin_respects_explicit_channel_order_position` —— 显式配置的位置被尊重。
* `test_channel_order_default_puts_local_daily_first` —— 四处默认值一致性。

## [2.9.7] - 2026-09-14

**修掉本地每日推荐「有清单、能进播放器、但点下去不出声」的最后一环：曲目时长单位与 `audioSpec`。**

### 起因

2.9.6 之后封面已经能出来（`static/cover` 返回 200），说明客户端确实走进了播放器，
但日志里**一条 `track/stream` 请求都没有**——客户端卡在「解析可播信息」这一步就放弃了。

把本地曲目和在线曲目逐字段对齐后，差距非常明确：

| 字段 | 在线（网易云注入，可播） | 本地（2.9.6 及以前） |
|---|---|---|
| `duration` | **毫秒**，例如 `233000` | **秒**，例如 `233` |
| `durationMs` | 毫秒 | 写的是秒 |
| `audioSpec.path` | `https://.../x.flac` | **缺失** |
| `audioSpec.format/codec/container` | 有 | **缺失** |
| `audioSpec.bitrate` / `sampleRate` / `bitDepth` | 有 | **缺失** |

飞牛全链路的 `duration` 一律是**毫秒**这件事，可以在 `build_online_track()` 里直接看到：

```python
"duration": duration_ms,
```

而我本地这条路径是从 mutagen 的 `info.length`（秒）一路原样透传的，于是：
客户端拿 `233` 当 `233 毫秒` 用 —— 一首歌只有 0.23 秒，直接被判为无效音源。

同时 `audioSpec.path` 为空，飞牛的 `ll()` 正是**用 path 的后缀去解析 extension**，
没有 path 就没有容器类型，转码/HLS 也不知道该按什么格式处理。

再叠加一个连带问题：即便客户端真发起了播放，2.9.6 的 `track/hls` 与 `track/transcode`
对 `local:file:` 这个它不认识的 guid **直接转发给官方后端**，官方后端当然不认识，
于是还没走到取流就已经 4xx 了。

### 改动

1. `recommend.py`：歌单列表里的本地曲目，`duration` / `durationMs` / `duration_ms`
   统一改为 **毫秒**（`duration * 1000`），另保留 `duration_s` 存秒，避免任何一处误读。
2. `app.py` `build_local_metadata_payload()`：重写成与在线曲目**同形状**——
   `duration` 毫秒、`duration_s` 秒，并补全完整 `audioSpec`：
   `path`（`local/<sha1>.<format>`，带真实后缀）/ `format` / `codec` / `container` /
   `duration` / `size` / `channel` / `sampleRate` / `bitDepth` / `bitrate`
   （缺失时按格式兜底：flac 1411kbps、其余 320kbps），另加 `codecName`、`coverURL`。
3. `app.py`：`track/hls`、`track/transcode_session`、`track/transcode` 现在**拦截
   `local:file:` 并自行应答**，不再转发给官方后端。
4. `LOCAL_DAILY_SCHEMA` 提到 3，旧缓存自动判废重建，升级后无需手动清缓存。

### 回归测试

* `test_local_duration_uses_same_unit_as_online` —— 直接拿 `app.build_online_track()`
  的输出当基准，断言本地曲目的 `duration` 单位与在线一致。
* `test_local_audiospec_carries_path_with_extension` —— 断言 `audioSpec.path` 非空且带后缀。
* `test_local_daily_tracks_duration_unit_matches_online` —— 歌单列表层的单位一致性。

## [2.9.6] - 2026-09-14

**本地每日推荐缓存加版本号：旧格式自动判废重建，升级后不需要任何手动操作。**

### 起因

v2.9.5 的说明里我让用户「装完点一次预热歌单缓存」——**这是错的**，那个按钮只服务于
网易云在线歌单的曲目缓存，跟本地日推毫无关系。被指出后顺着查，发现底下压着一个
真实的升级隐患：

```python
def get_or_build_local_daily(...):
    cached = load_local_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached          # ← 命中就返回，根本不扫描
    ...
```

用户当天（2026-09-14）的 `local-20260914.json` 是 v2.9.4 时代生成的。升级到 v2.9.5 后
第一次拉歌单列表会**直接命中这份旧缓存**，于是：

* 不重新扫描 → v2.9.5 新加的本地文件索引永远写不上；
* 列表里显示的是缓存里的 `duration = 0` / `size = 0` → 客户端仍可能判定不可播。

我原本指望 metadata 请求的「慢路兜底」（从歌单反查路径再补索引）来救，但那只修好了
封面和元数据，**列表项本身的 duration 还是 0**。

### 改动

1. 缓存 payload 增加 `schema` 字段（`LOCAL_DAILY_SCHEMA = 2`）；加载时版本落后即
   判废返回 `None`，触发静默重建。旧缓存没有该字段，按 1 处理。
2. 缓存命中时也顺手补一次本地文件索引（`local_files.record_tracks`，已存在则跳过，
   几乎零成本）——清过缓存目录或换过运行目录后不必等慢路兜底。
3. 新增回归测试 `test_old_schema_cache_is_discarded_and_rebuilt`：伪造一份 v2.9.4
   格式的缓存，断言会被重建且首曲能在索引里查到真实路径。

## [2.9.5] - 2026-09-14

**修复本地每日推荐「有清单、没内容、点不开」：曲目是我们造的，元数据却指望官方后端给。**

### 根因：合成 guid 被转发给了不认识它的官方后端

本地曲目的 guid 是我们按路径指纹造的 `local:file:<sha1>`（官方后端没有这个概念）。
但三个关键接口都没对它做处理，直接 `forward_to_upstream`：

| 请求 | 官方后端的反应 | 界面表现 |
|---|---|---|
| `/static/cover?coverId=local:file:…` | 不认这个 guid → **400 Bad Request** | 整张歌单没封面 |
| `/track/metadata?guid=local:file:…` | 返回一堆空值 | 有条目但没信息、像占位数据 |
| 列表里的 `duration` / `size` | 我们恒填 0 | 客户端判定「不可播」，点了没反应 |

日志里只有一行 400，很难联想到是转发造成的——这正是它一直没被发现的原因。

### 改动

1. **新增 `proxy/local_files.py`**：本地曲库文件索引（sha1 → path，落盘，扫描时写入）
   + 标签探测 + 内嵌封面提取。既然文件就在本地磁盘上，标题/艺术家/专辑/时长/
   体积/码率/封面全都自己从文件里读，一次都不用转发。
2. **metadata 自应答**：`/track/metadata` 遇到 `local:file:` 直接返回按飞牛形状组装的
   完整信息（对齐 `build_metadata_payload`，含 genres/album/artists——客户端会无防护
   读这些字段，缺了就跳过播放）。
3. **封面三级取图**：音频内嵌图（FLAC picture 块 / ID3 APIC / MP4 covr / ASF 图片）
   → 同目录 `cover.jpg` 等 → 生成的唱片占位图。抽出的图按 sha1 落盘缓存。
4. **列表里给真时长/体积**：入选曲目在构建时读一次标签，`duration` / `size` /
   `bitrate` 全部为真值，标题艺术家优先用标签里的（比文件名解析准）。
5. **播放**：`/track/stream` 的路径反查在当天 bundle 之外，再兜一层持久索引。

### 本地每日推荐排到歌单列表第一位

默认大类顺序改为 `localdaily,daily,mine,…`；配置里**没写** `localdaily` 时直接插到
最前（写了则以配置为准，仍可自己把它排后面）。setup.sh 里加了迁移：保存值正好等于
旧默认值时自动换成新默认值，用户自己调过顺序的一律保留。

### 授权状态不再误报故障

真机实锤：部分版本不会向应用进程注入 `TRIM_API_TOKEN`（系统版本低于 1.2.0401，
或需重装应用以注册 api-scope），网关查不到授权目录。此时页面原本直接报红字错误。
现在改为：退到应用配置目录下的 `share_paths` 文件（`TRIM_PKGVAR` / `TRIM_PKGETC`
及 `/vol*/@appdata/<app>/share_paths`）读取授权目录，能读到就标为「降级但正常」；
只在确实一个授权目录都没有时才给指引。

### 测试

`test_local_daily_bundle_cache_and_purge` 原先把日期写死成 `20260913`，跨过午夜后
`today_key()` 变了、磁盘缓存键对不上，会在「没人改代码」的情况下突然挂掉
（2026-09-14 实锤一次）。已改为取 `today_key()`。

## [2.9.4] - 2026-09-13

**按飞牛开发规范改用「应用授权目录」访问本地曲库，不再依赖 root 硬读用户存储空间。**

### 问题：绕过了飞牛的应用访问权限体系

之前本地曲库的实现是「代理以 root 运行 → 直接 `os.listdir('/vol1/...')`」。这在飞牛
的开发规范里不合规：应用访问用户存储空间中的文件夹前必须先获得授权，由系统把
目标路径的 ACL 授予应用账号。副作用很明显——管理员在应用设置里**看不到「授权
目录」入口**（manifest 里 `disable_authorization_path = true` 把它关掉了），想合规
授权都没地方点；一旦系统收紧权限或改用非 root 运行身份，读取直接
`PermissionError`，表现就是「本地每日推荐歌单不出现」且日志里查不到原因。

### 改动

1. **声明能力**：`fpk/config/resource` 写入
   `{"api-scope": ["trim.file.sharedAccess"]}`；manifest 改
   `disable_authorization_path = false`（打开授权入口）、新增 `micro_app = true`
   （JS SDK 需要）。
2. **新增 `proxy/trimgw.py`**：飞牛开放网关客户端。通过 Unix Socket
   `/var/run/trim_open_gateway_apiscope.socket` 调 `POST /api/v1/trimapp`，
   `Authorization: Bearer <TRIM_API_TOKEN>`，用
   `trim.file.getSharedAccessibleFolders` 查询管理员已授权目录。token **每次调用
   现读环境变量**，绝不落盘（重装/重注册后会变）。零第三方依赖，失败永不抛异常。
   同时兼容旧版 `TRIM_DATA_SHARE_PATHS` 环境变量。
3. **曲库目录定位改优先级**：管理页显式配置 → **已授权目录** → music.db 的
   `shared_library` → 缓存目录。未授权时明确 WARNING 并给出指引；可用
   `FNMUSIC_STRICT_AUTHORIZATION=true` 强制只扫已授权目录（合规最严档，默认关）。
4. **管理页新增「飞牛授权目录」卡片**：一键唤起飞牛目录选择器（JS SDK 可用时），
   SDK 不可用时降级为「去应用设置授权 + 本页刷新状态」的引导。
5. **诊断页新增授权状态区块**：网关是否存在、token 是否注入、已授权目录清单、
   当前曲库是否被授权覆盖。

### 附带修复：本地每日推荐歌单封面 404

日志里 `static/cover?coverId=online:playlist:localdaily:...` 返回 404。早期实现是
「本地文件没有封面 URL，返回 404 让客户端用占位图」，但个别客户端会因为歌单封
面 404 而整条不渲染——日志只有一行 404，界面则是「歌单凭空消失」。现改为**现生成
零依赖 PNG 唱片封面**（zlib + struct 手写 PNG，不依赖 PIL；按尺寸内存缓存）。

## [2.9.3] - 2026-09-13

**修复：真机上本地每日推荐必然失效的决定性 bug（v2.9.0 起一直存在）。**

### 根因：扁平运行形态下的裸相对导入

真机的代理是这么启动的（`run_proxy.sh`）：

```
uvicorn app:app --app-dir proxy --uds ...
```

此时 `app` / `recommend` / `local_library` 全是**没有父包的顶层模块**。而
`_scan_library_audio_files()` 里有一句函数内延迟导入：

```python
from . import local_library   # ← 顶层模块形态下必抛 ImportError
```

这行在扫描函数的 `try` **之外**，一抛整个扫描报废 → `get_or_build_local_daily`
捕获后返回 `scan_failed` 空歌单 → **歌单静默不出现**。曲库目录对不对都一样，
v2.9.0/2.9.1/2.9.2 全部中招。

更阴的是：**测试全是绿的**——pytest 以 `proxy` 包形态导入（有父包），相对导入
正常工作，只有真机的扁平形态会炸。这正是 skill 里 admin_ui `_as_path` 注释记过
的同款坑，这次栽在 recommend 里。

v2.9.2 新增的排障接口反把它暴露了出来：诊断页取快照时报
`ImportError: attempted relative import with no known parent package`。

### 修复

- 补上 `try/except ImportError` 兜底，扁平/包两种形态都能工作；
  修复后已用真机同款方式（`cd proxy && python3 -c "import recommend"`）实测扫描正常；
- **新增 AST 级静态检查**（`test_import_guards.py`）：遍历 `proxy/*.py` 的语法树，
  任何不在 `except ImportError` 兜底块内的相对导入直接测试失败——这类
  「测试全绿、真机必炸」的导入问题从此无法混入。

### 关于飞牛「应用访问权限」

有用户反馈第三方应用拿不到像官方音乐那样的目录授权入口。说明两点：

- 代理进程以 **root** 运行（`run_proxy.sh` 由 systemd 以 root 调用），读取文件系统
  一般不受应用账号 ACL 限制；受限于访问权限的是降权运行的音源服务
  （`fnmusicext_user`），那影响的是网易云取链，不影响本地曲库扫描。
- 为防万一，排障接口现在会显示**代理运行身份**（uid/gid）、曲库目录**可读性**，
  目录探测失败时给出具体错误（含「权限不足（应用访问权限未覆盖该目录）」）。

### 测试

- 新增：相对导入兜底静态检查；合计 805 passed。

## [2.9.2] - 2026-09-13


**修复：本地每日推荐「页面显示保存成功，但没有歌单生成」。**

### 根因：曲库目录定位失败后**静默回退到空目录**

`detect_library_dir()` 的解析顺序是：

1. `FNMUSIC_LIBRARY_DIR` 显式配置 → 2. 读飞牛 `music.db` 的 `shared_library.path`
   → 3. **回退到代理自己的 `cache/` 目录**

第 3 步是罪魁祸首。`cache/` 里一首歌都没有，于是扫描结果恒为 0、歌单不注入——
而界面表现跟「开关关着」**一模一样**，并且不报任何错。真机诊断页已经点破了：

```
music.db 扫描   : available=false  命中行=0  music.db 不存在或未能打开
```

只要这一行是 `available=false`，本地每日推荐就不可能出歌，与开关、
与网易云登录态都无关。

### 修复

- **扩充 music.db 候选路径**：新增 `@appcenter` 布局，并在 `/vol*/@appdata/trim.music/`
  下递归查找 `music.db`（只在这个目录里递归，不去扫音乐库那种大盘）。
  探测失败时日志改为 `WARNING`，并**逐个列出候选路径及其存在性**，不再让人猜。
- **管理页新增「本地曲库目录」配置项**（`FNMUSIC_LIBRARY_DIR`，留空 = 自动探测）。
  飞牛各版本目录布局不统一，自动探测注定不可能 100% 命中，必须有手动入口。
  校验规则：留空放行；填了就必须是**已存在的绝对路径**——只要求存在、**不要求可写**
  （曲库是只读的），填错在保存时就直接报错，不会静默失败。
- **诊断页新增「本地每日推荐（排障）」区块**，一次给出：开关状态、曲库目录、
  **是否回落到空目录**（关键）、扫到多少首音频（含样例）、music.db 解析结果、
  `shared_library` 里的路径、以及全部已探测候选路径的存在性。
- `detect_library_dir` 在每次回退时打 `WARNING`，写明原因；`music.db` 里
  `shared_library` 为空时也单独提示。

### 现在怎么定位

装完 2.9.2 打开「诊断」，看新增的这一块：

```
-- 本地每日推荐（排障）--
  开关           : enabled=true   数量上限=50
  曲库目录       : /vol1/@appdata/fnmusicext/cache  存在=true
  是否回落到空目录: true  ★ 这就是歌单不出现的原因
  扫到音频文件数 : 0
  music.db       : /usr/local/apps/@appdata/trim.music/db/music.db  存在=false
  已探测候选路径 :
     [缺失] /usr/local/apps/@appdata/trim.music/db/music.db
     [缺失] /vol1/@appdata/trim.music/db/music.db
     ...
```

**最省事的解法**：不管上面显示什么，直接到管理页「本地曲库目录」里填上真实
曲库路径（例如 `/vol1/1000/music`），保存并重启即可。

### 测试

- 新增：曲库目录校验（留空放行 / 相对路径报错 / 不存在的目录报错）、
  目录配置往返写盘；
- 合计 804 passed。

## [2.9.1] - 2026-09-13


**修复：本地每日推荐「保存后仍显示未启用 / 飞牛音乐里不显示歌单」。**

### 根因 ①：启用开关没登记进前端开关表（决定性）

管理页 JS 里有一张开关白名单 `BOOLS`，只有登记在里面的键才会：

- 加载配置时走 `el.checked = (v[k] === "true")`；
- 提交配置时按 `el.checked` 取值。

`local_daily_enabled` **漏登记**了，于是两个后果叠加：

| 环节 | 实际发生 | 用户看到 |
| :--- | :--- | :--- |
| 加载 | 走 `else` 分支 `el.value = "true"`——给 checkbox 赋 value 不改变勾选外观 | 刷新后永远显示**未启用** |
| 提交 | 被 `el.type === "checkbox"` 分支整个跳过，该字段根本不进 `values` | 勾了也不生效，后端按「页面没提交」保留原值 |

这类 bug 特别阴——后端、配置读写、注入逻辑全是对的，只有前端这一处漏登记，
而且不报任何错。已加回归测试：扫描页面里所有具名 checkbox，凡不在 `BOOLS`
里的直接测试失败，以后新增开关不可能再漏。

### 根因 ②：大类顺序默认值漏了 localdaily

管理页 `DEFAULTS["netease_channel_order"]` 仍是 v2.8 的旧值（没有 `localdaily`），
与 `setup.sh` 的默认不一致。只要用户在页面上保存过一次配置，大类顺序里就没有
本地每日推荐了。已对齐为 `daily,localdaily,mine,nrec,toplist,category,newalbum,fm`。

### 根因 ③：曲库扫描深度偏浅

`_scan_library_audio_files` 只扫到相对深度 2（3 层子目录）。曲库若是
「库/歌手/专辑/分碟/文件」这类布局，会整库扫空 → 歌单静默不出现（不报错）。
已放宽到相对深度 3（4 层子目录），并加了 20000 首的扫描上限兜底，避免超大
曲库拖慢列表请求；超过深度的子树直接剪掉，不再无谓递归。

### 诊断增强

本地每日推荐**没注入**时，现在一定会往日志里写一行原因，带上曲库目录：

```
本地每日推荐未注入: reason=disabled library=/vol1/1000/music user=1a2b3c4d
本地每日推荐未注入: reason=library_empty library=/vol1/1000/music user=1a2b3c4d
```

- `disabled` → 开关关着（检查管理页开关是否已保存并重启）；
- `library_empty` → 曲库目录里没扫到音频文件（检查目录对不对、歌是否埋太深）。

### 测试

- 新增：页面上每个具名 checkbox 都必须登记进 `BOOLS`（防同类回归）、
  开关 true/false 往返写盘、大类顺序默认值含 `localdaily`、
  嵌套专辑布局可扫到、超深度目录不扫、非音频与 0 字节文件被忽略；
- 合计 802 passed（原 795）。

## [2.9.0] - 2026-09-13


**新功能：本地每日推荐歌单**——每天从本地曲库随机抽 N 首组成歌单。

### 与网易云「每日推荐」的区分

| | 网易云每日推荐 | 本地每日推荐 |
| :--- | :--- | :--- |
| 内容来源 | 网易云官方个性化推荐（账号绑定） | 本地曲库随机抽取 |
| 登录要求 | 需扫码登录 | 无需登录 |
| guid 命名空间 | `online:playlist:daily:` | `online:playlist:localdaily:` |
| 歌单名 | `每日推荐 MM-DD` | `本地每日推荐 MM-DD` |
| 缓存 | `recommend_cache/<user>/<day>.json` | `recommend_cache/<user>/local-<day>.json` |
| 播放路径 | 网易云取链 → CDN | 直接读本地文件（零外网） |

### 行为细节

- **随机种子按 用户+日期**：同一天内多次打开结果一致（不会刷新一次换一批），
  跨天自动换新；旧日缓存次日自动清理；
- 扫描范围：曲库目录两层深度内的音频文件（飞牛「库/歌手-专辑/文件」结构），
  「歌手 - 歌名.ext」文件名尽力解析出艺术家与标题；
- 曲目 guid 用绝对路径指纹（`local:file:<sha1>`），跨零点后旧页的播放请求
  仍可反查到文件（当天 + 昨天两份 bundle）；
- 空曲库 / 扫描失败时不注入空歌单（与网易云日推同样的「宁可不出现」原则）。

### 配置（管理页「网易云音源」区新增两项，保存即生效）

| 配置 | 默认 | 说明 |
| :--- | :--- | :--- |
| 启用「本地每日推荐」歌单 | 开 | 独立开关 |
| 本地每日推荐数量 | 50 | 1–500（`FNMUSIC_LOCAL_DAILY_LIMIT`） |

「歌单大类顺序」新增 `localdaily` 可排位（默认在网易云日推之后）。

### 测试

- 新增：guid 命名空间隔离、随机稳定与数量上限、缓存与清理、禁用与空库、
  端到端（列表注入 → 曲目 → 播放直读本地文件 → detail/batch-detail 回显）、
  大类顺序含 localdaily；合计 714 passed。

## [2.8.2] - 2026-09-13

**预热不再重复刷新刚刷过的歌单**（真机反馈：预热 33/33 后 07:15 定时任务
又全部重刷一遍；且每次都从 25/33 开始）。

### 现象解释（两部分，均可从代码对上）

1. **为什么会再预热**：每日定时刷新（07:15）是**强制全量**的，不受冷却期
   约束；代理进程重启（如保存配置）也会丢掉内存里的冷却记录，打开列表即
   重新自动预热。
2. **为什么从 25 开始、从来不是 0**：33 个在列歌单里有 8 个来自**轮换口径**
   （最典型是新碟上架——每天上新，guid 每次刷新清单都会变），它们没有旧缓存
   可继承；另外 25 个稳定歌单的缓存一直在——所以永远是 25 起步。这不是
   缓存丢失。

### 修复

- **定时刷新与自动预热跳过「仍新鲜」的缓存**（默认 1 小时内，`FNMUSIC_WARM_SKIP_FRESH_S`
  可调，0=一律刷新）：15 分钟前刚刷过的 25 个直接跳过，只拉 8 个新的轮换
  歌单与过期的——07:15 那轮从 ~100 秒降到 ~30 秒，上游配额省 3/4；
- **手动「预热歌单缓存」按钮保持全量刷新**（按下按钮就是明确要求刷新）；
- 完成日志改为「刷新 X、跳过（仍新鲜）Y、共 N 个」，一眼可见工作量。

### 测试

- 新增：定时/自动预热跳过新鲜缓存、新歌单与过期缓存照常拉取、手动按钮
  忽略阈值全量刷新；合计 708 passed。

## [2.8.1] - 2026-09-13

**移动网络歌单卡顿修复 + 本地曲库优先不生效修复**（真机反馈跟进）。

### 1. 封面缩图（修移动网络下列表卡顿）

用户定位正确：封面原图太大。网易云原图普遍几百 KB～1MB+，一个歌单列表
首屏三四十张就是几十 MB——移动网络下「列表打开 5 秒」的主力构成。

- 网易云系封面（music.126.net）走 **CDN 服务端缩图**：URL 追加
  `?param={N}y{N}`，300px 缩图约 20~50KB，体积缩到原图几十分之一；
- 客户端请求带 `size` 参数时优先采用其尺寸（下限 60、上限 800）；
- 缩图 URL 失败回落原图；302 兜底仍指原图；
- `FNMUSIC_COVER_RESIZE_PX`（默认 300，0=不压缩），保存即生效；
- 磁盘缓存按最终 URL 键控，不同尺寸各自缓存。

### 2. music.db 自动定位（修本地曲库优先不生效）

真机诊断铁证：`music.db 扫描 available=false`——默认路径
`/usr/local/apps/@appdata/trim.music/db/music.db` 在该机器上不存在（飞牛
应用数据实际在 `/vol*/@appdata` 下）。后果：**本地曲库优先的索引建立在
空库上，永远不命中**（「跟随飞牛」读偏好同样失效）。

- `resolve_music_db()`：显式配置存在才用 → 自动探测
  `/vol*/@appdata/trim.music/db/music.db` 等常见布局；探测结果记日志；
- 本地曲库索引就绪/为空均有日志留证（`本地曲库索引就绪：N 首` /
  `本地曲库索引为空`），真机上「功能为什么没生效」从此有第一现场。

### 测试

- 新增：缩图 URL 构造（126.net 独享/带参剥参/关闭）、客户端 size 优先、
  缩图失败回落、music.db 显式优先/探测回退/全无兜底；合计 706 passed。

## [2.8.0] - 2026-09-13

**移动数据远程访问提速 + 本地曲库优先播放**。

### 1. 远程访问识别（修移动数据下播放 7~8 秒的根因）

真机诊断铁证：`客户端音质/网络线索：暂未观察到任何带音质或网络语义的键`——
飞牛客户端**从不发送**网络类型标识，于是 `by_network` 策略里的「流量档」
**从未触发过**：移动数据远程访问时也一直按 WiFi 档发 jymaster（Hi-Res 母带，
动辄几十上百 MB），窄管道上起步缓冲 7~8 秒是必然的。

- 客户端无显式提示时，从 `X-Forwarded-For` / `X-Real-IP` 等头读真实客户端
  IP：出现**公网 IP** = 远程访问（移动数据/异地），默认按流量场景处理
  （`FNMUSIC_REMOTE_AS_CELLULAR`，默认开，可关）；只有**私网 IP** = 局域网；
- 采信的 IP 与判定计数进诊断页「客户端 IP 线索」——nginx 是否透传这些头
  一眼可见；若未透传则识别不可用，会明确提示改用固定音质策略。

### 2. 本地曲库优先播放（用户需求：有本地文件先读本地）

播放网易云歌单曲目时，先在飞牛官方 `music.db` 里找同名歌曲（**schema 容错**
扫描 + 标题归一化全等 + 主艺术家匹配，内存索引 TTL 缓存），命中且**音质类
与当前策略一致**时直接读本地文件——零外网、起步最快：

- 策略要 lossless、本地是无损/Hi-Res → 读本地；
- 策略要 exhigh（省流量）、本地是 Hi-Res → **不用本地**，仍按策略去网易云要
  320k——「根据音质策略决定是否降码率」：不能因为本地摸到母带就把省流量
  的意图顶掉；
- 策略要 lossless、本地只有 320k mp3 → 不用本地（拿不到想要的质量）；
- 网易云取链失败/CDN 不可达时，本地匹配（不论档位）作**最后兜底**——
  「能播」优先于「档位精确」；
- `FNMUSIC_LOCAL_FIRST=false` 可整体关闭。

### 3. 封面字节磁盘缓存（歌单列表渲染提速）

此前只缓存封面 URL（24h），**字节每次都现抓**——换设备、客户端清缓存、
多端同时打开列表，NAS 就要对同一批图反复跑 CDN 往返（每张几百毫秒），
列表渲染被拖长。现在字节落盘（`cache/cover_cache/`，上限 800 张按 mtime
淘汰），同一 URL 一生只出网一次。

### 4. 耗时留证日志

真机上「几秒」从此可量化、可归因：

- `play-start <来源> <guid> <ms>`：播放起步耗时与来源
  （tee-cache / local-first / local-fallback / netease）；
- `playlist tracks slow open: <ms> cache=hit|miss <guid>`：歌单打开超过 1s
  时记录——`cache=miss` 慢是上游链路，`cache=hit` 仍慢则瓶颈在传输/客户端，
  两者排障方向完全不同。

### 测试

- 新增：本地索引构建与匹配归一化、音质类约束三结局（读本地/按策略走
  网易云/兜底）、关闭开关、远程公网 IP 识别与流量档判定、封面磁盘缓存
  只出网一次；合计 700 passed。

## [2.7.1] - 2026-09-13

**修复歌单缓存计数显示矛盾**（真机报告：「缓存：59/34 个歌单已有本地缓存」，
分子大于分母）。

### 根因

两个统计口径不一致：

- `cached` 数的是**注册表里所有带曲目缓存的条目**。注册表按设计在「清单不
  完整时不清」（防止掉线一次就抹掉全部名字与封面），于是历史口径/旧分类/
  旧上限遗留的死条目会长期留存——真机上积到 59 条；
- `total` 数的是**当前配置实际注入**的歌单（34 个）。

且每日定时刷新（07:15）把注册表**全部 59 条**都拉了一遍：25 个死条目每天
白刷一次，纯浪费上游配额。

### 修复

- **统计口径统一**：preview 的 `cached`/`total` 都只看当前在列歌单（不含
  每日推荐，其曲目按用户+日期另存），分子永不超过分母；
- **预热只刷当前在列**：每日定时刷新与「预热歌单缓存」按钮先拉当前口径
  清单再预热（原先直接全量刷注册表）；打开飞牛歌单列表触发的自动预热直接
  复用当次清单，不重复拉取；
- **顺带清理死条目**：清单完整（complete=True）时 `forget_stale` 把死条目
  连同其曲目缓存一并清掉，注册表与飞牛里看到的歌单一一对应。清理后缓存
  占用也随之回收。

### 测试

- 更新/新增：预热按钮只刷当前在列歌单且清理死条目、preview 统计分子不超
  分母；合计 687 passed。

## [2.7.0] - 2026-09-13

**稳定性自愈 + 打开提速**：修复「应用异常退出」无自愈问题；显著缩短并稳定化
网易云歌曲与歌单/榜单的打开耗时。

### 1. 看门狗自愈（修复「应用异常退出」）

**背景（2026-09-12 20:30 真机事故）**：官方 trim-music 后端重启时会重绑
`/var/run/trim_music.socket`（先 unlink 再 bind），把本扩展代理的监听 socket
文件"抢走"。代理进程本身毫发无损（诊断里启动时间很长），但 `status.sh` 探测
不到接管 → 应用中心报「应用异常退出」，而扩展自己毫无察觉，在线音源从此失效，
直到用户手动重启。

- 新增 `bin/watchdog.sh`：每 `FNMUSIC_WATCHDOG_INTERVAL_S`（默认 30s，0=关闭）
  检查一次，代理进程死亡 / socket 接管丢失 / musicbox 死亡时自动调 `start.sh`
  幂等恢复；恢复连续失败指数退避（封顶 300s）；
- **主动停机保护**：`stop.sh` 先落 `stopped.flag` 再停看门狗；看门狗发起的
  `start.sh` 见到标志立即放弃——用户在应用中心点「停止」永远是最终状态，
  绝不会被"起死回生"；
- `start.sh` 新增僵尸代理清理：代理进程活着但接管已丢失时，先停掉旧进程再
  重新接管（否则旧代理永久占着 upstream socket，新代理也接不上）；
- 配置重启窗口（restart.inprogress）内看门狗不插手。

### 2. 播放打开提速（7~8 秒 → 稳定 2~3 秒）

- **播放直链短缓存**（`FNMUSIC_URL_CACHE_TTL`，默认 600s，0=关闭）：网易云
  CDN 直链约有 20 分钟有效期，切歌 / 重播 / seek 不再每次重取链（一次取链
  在上游要一到两次跨洋往返，且与歌单预热在 musicbox 全局锁上争抢——这正是
  「有时 7~8 秒」的主要来源之一）。缓存直链被 CDN 拒绝（403/404）时自动
  丢弃缓存、强制重取，拿到**不同的**新链才重试一次，绝不死循环；
- **曲目元数据并发拉取**：`_online_info` 的 info 与 lyric 由串行改为并发，
  首播一首歌少付一次跨洋往返的加和。

### 3. 歌单 / 榜单打开提速与稳定化

- **封面直读、免补齐往返**：musicbox 的歌单曲目端点返回的 song_info 本就带
  `album_pic_url` / `has_sq` / `has_hr`，`map_netease_song` 现在直读这些字段；
  曲目全部自带封面时跳过 enrich（原先每次打开歌单都要多打一次
  songs_detail + songs_url 两次跨洋往返）；
- **口径清单短缓存**（`FNMUSIC_CHANNEL_LIST_CACHE_TTL`，默认 300s，0=关闭）：
  歌单列表页（含榜单入口）在 TTL 内零上游往返；过期后先返回旧值并后台单飞
  刷新（stale-while-revalidate）——榜单打开速度不再随网易云抖动大起大落；
- **口径并发拉取**：toplists / category / mine 等口径由串行 await 改为
  `asyncio.gather`，列表页尾延迟从「各口径之和」降到「最慢一个」；
- **预热限流**：只预热当前启用口径的歌单（注册表里的历史口径条目不再浪费
  上游配额），间隔从 0.3s 放宽到 1s，进一步降低与播放路径的锁争抢。

### 4. 内存安全（防 OOM 崩溃）

在线播放 tee 缓冲原先无界：NAS 从 CDN 下载远快于客户端消费时，慢客户端的
一首无损（可达几十 MB）会整个堆在内存里；快速切歌时多个被放弃的流各占一整
首歌的内存，叠加后足以把代理进程推进 OOM（应用「异常退出」的另一候选根因）。

- 入队满 64 块（约 4MB）即暂停拉取，TCP 背压自然传导给 CDN；
- 客户端断开（暂停不限时、关页、切歌）→ 下载端转「只写盘」模式继续下完，
  **缓存文件照样完整落盘**，下次播放直接秒开。

### 兼容性

- 新增配置键均已收录 `.env.example`、`setup.sh`（pick_env + 自定义键保留名单）
  与 `proxy/env_merge.py`（NEW_DEFAULTS/NEW_PREFIXES），升级不丢配置；
- `/_ext/cache/invalidate` 同步清理新增的两份缓存（直链 / 口径清单）。

### 测试

- 新增：直链缓存复用与失效重试、tee 背压与断开补盘、封面直读、enrich 跳过、
  口径清单 SWR 缓存；既有 682 个用例全部保持通过（合计 687 passed）。

## [2.6.0] - 2026-09-12

**歌单打开提速**：曲目缓存 + stale-while-revalidate + 每日定时刷新 + 自动预热。

### 背景

点开一个伪歌单原先要现场跑完整条上游链路（歌单 trackIds → songs_detail →
songs_url 逐首可播性过滤 → 批量补封面），实测好几秒。本版把解析结果按 guid
落盘（`${PKGVAR}/playlist_cache/tracks`，跨升级持久），打开路径变成：

- **命中且新鲜（TTL 内）→ 直接返回，零上游往返**——这就是秒开的全部；
- **命中但过 TTL → 先返回旧值**（打开永远快），后台单飞刷新，下次就是新的；
- **未命中（首次打开）→ 现场拉取并落盘**，之后都走缓存；
- 刷新失败/拉到空列表时**保留旧缓存**，上游抖动不影响可用性。

### 预热（让缓存主动就位，而不是等你挨个点开）

- **打开歌单列表后自动预热**：飞牛里看一眼歌单列表，几秒后所有在列歌单的
  曲目缓存就都在本地了（串行 + 间隔 0.3s 对上游友好；冷却期 = TTL，不重复）；
- **每日定时刷新**：`FNMUSIC_PLAYLIST_REFRESH_AT`（默认 `04:30`，管理页可改，
  留空关闭）每天全量刷一遍，第二天打开就是最新内容；
- 管理页「歌单顺序」卡片新增**「预热歌单缓存」按钮**（立即触发，不受冷却
  限制）与缓存状态行（已缓存 N/M、TTL、定时时间）。

### 配置（管理页「网易云音源」区新增两项）

| 配置 | 默认 | 说明 |
| :--- | :--- | :--- |
| 歌单缓存有效期（小时） | 6 | 期内点开直接读本地；超期先返回缓存、后台刷新 |
| 每日定时刷新歌单缓存 | 04:30 | 每天该时间后台全量刷新；留空关闭 |

每日推荐的歌单本身已有按日磁盘缓存，不受影响；排行榜封面回落也自动受益于
曲目缓存（不再重复拉全量曲目）。

### 测试

新增 13 个用例：缓存命中零上游、旧值先返回+后台刷新落盘、刷新失败保留旧
缓存、warm 端点、preview 缓存统计、TTL/定时解析与校验、小时↔秒换算、升级
持久化。全套 686 passed, 1 skipped。

## [2.5.0] - 2026-09-12

管理页新增**「歌单顺序」手动排序**：网页里看到真实歌单名称，逐个调整先后。

### 功能

- 管理页新卡片「歌单顺序」：点「读取当前歌单」拉取当前实际注入飞牛的网易云歌单
  ——**真实名称**（如「网易云·我的自建单」「榜｜飙升榜」「每日推荐 09-12」），
  初始顺序与飞牛歌单列表当前显示**完全同源**（大类顺序 → 手动顺序覆盖）。
- 每行 ▲▼ 上移/下移，保存后**立即生效、无需重启**；「恢复默认」清回大类顺序。
- 数据链路：代理新增只读端点 `/_ext/playlists/preview`（与 `playlist_list`
  注入逻辑同源同序），管理页经 unix socket 取数；保存写入新配置
  `FNMUSIC_NETEASE_PLAYLIST_ORDER`（token 列表：`daily` 或歌单 guid）。
- **立即生效的实现**：代理对这份配置**实时读 `.env`**（mtime+size 指纹缓存），
  不走进程环境变量——否则用户排完序还得等代理重启。
- 注入时手动顺序整体覆盖大类顺序；没排到的新歌单按大类相对顺序跟在后面，
  不丢失。每日推荐以 token `daily` 参与（其真实 guid 含日期与用户 id）。

### 配套

- `.env` 合并/升级链路全部收录新键（升级不丢手动顺序）；管理页校验器只放行
  `daily` 与 `online:playlist:…` 形态 token（防注入）。
- 测试新增 8 个用例（实时 .env 读取、手动顺序覆盖与追加、preview 端点、
  校验器、单独保存不冲其它配置、升级持久化），全套 674 passed, 1 skipped。

## [2.4.1] - 2026-09-12

v2.4.0 真机反馈的两个遗留问题。

### 歌单顺序：每日推荐排到了最后（问题3修正）

v2.4.0 把注入歌单的时间戳做成了「当前时间递减」，推断客户端按 updatedAt
降序排——**猜反了**。真机日志证实：时间戳最大的每日推荐反而沉到最底部，
说明飞牛客户端对歌单列表按时间戳**升序**排列。

修正：注入条目的展示时间戳改为**从小基准（1）开始按位置递增**——fnOS 2023
年才发布，真实歌单时间戳都是 1.7e9 级，因此注入条目永远排在本地歌单之前，
且顺序与注入顺序完全一致（每日推荐第一、各大类按配置顺序、同口径内部保持
上游顺序）。同时把每日推荐的展示时间戳也写入注册表，`playlist/detail` 与
`playlist/batch-detail` 回显同一份时间戳，列表页与详情页排序语义不再打架；
daily 注册条目按 seen 字段 14 天过期清理（guid 含日期，不清理会无限累积）。

### 本地转码：应答逐字节透传 + 关键留证日志（问题3排查增强）

v2.4.0 的 Host 透传与 300s 超时上线后，真机日志仍只见客户端对 m3u8 各请求
一次、随即放弃（无分片请求），但没有留下任何上游应答线索。本版：

- m3u8 / 转码会话这类小应答改为**整包缓冲透传**（`forward_buffered`），
  原样保留 content-length，与官方直连逐字节等价；
- 流式转发（stream/分片）同样保留未压缩应答的 content-length；
- 每次本地播放链路转发（transcode-start / transcode-session / hls-playlist /
  hls-segment / local-stream）把**状态码、耗时、content-type、响应体开头**
  记入日志（非 200 记 WARNING）——下次「开转码播不了」时，日志能直接指出
  官方后端到底回了什么。

## [2.4.0] - 2026-09-12

修复真机实测反馈的五个问题。核心主题：**设置必须持久、歌单必须能出来且顺序固定、
保存配置不能闪退、开转码本地音乐必须能播**。

### 1. 升级/保存设置后配置全部丢失（严重）

`fpk/payload/bin/setup.sh` 的 `write_env_file` 每次执行都把 `.env` **整表重写**成
硬编码默认值——只特殊保留了 PushPlus token。于是每次应用升级（upgrade_callback）、
每次「应用设置」保存（config_callback），用户在管理页改过的**全部**设置都被冲掉：
歌单口径、收藏归档目录（保存音乐目录）、每口径上限、音质策略、日志保留……

修复：整表重写改为**三源合并取值**（`pick_env`）——向导值、既有 `.env` 值、默认值，
按场景决定优先级：

- **升级**（`upgrade_callback` 以 `FNMUSICEXT_PRESERVE_ENV=true` 调 setup）：
  既有值一律保留，向导值只用于补齐缺失键；
- **应用设置保存 / 安装**：向导覆盖的键用向导值（用户刚填的），其余键保留旧值；
- **卸载+重装**：从 `${PKGVAR}/.env.preserved` 快照恢复全部旧值（原先只恢复 token）；
- 用户手工加的**自定义键**一律原样保留；v1.x 废弃键（musicdl/lxmusic/LLM）不再带回。

### 2. 「我的歌单（自建+收藏）」不同步（两处 NameError）

- `musicbox-service/app.py`：v2.2.0 把 import 改成 `auth_detail as ne_auth_detail` 时
  漏改两处调用点，`NameError` 被外层 `except` 吞掉 → uid 恒为 0 → `/playlists/user`
  恒返回 `uid_unavailable`。
- `proxy/app.py` `_netease_logged_in()`：误调用不存在的 `musicbox_client()`，
  `NameError` 被自身 `except` 吞掉 → 代理**永远认为未登录** → mine/nrec/fm 这些
  需登录口径在代理侧就全部不注入。

两处均修复并补回归测试。排行榜/分类/新碟解析链路对照上游 NEMbox 0.5.3 源码逐一
核验过形状一致（`fetch_toplists` 返回 `(name, id)` 对、`trackIds` 兼容 dict 列表）。

### 3. 歌单顺序不固定 → 大类顺序可自定义

原先每条注入歌单的 `createdAt/updatedAt` 都是 `int(time.time())`——同一秒的一堆
相同时间戳，客户端非稳定排序下**每次刷新顺序都变**。现在注入头部统一盖
**互不相同且递减**的时间戳（`stamp_display_order`），客户端无论升序/降序排都得到
确定顺序；注册表 `ts` 同步，详情页与列表页语义一致。

新增 `FNMUSIC_NETEASE_CHANNEL_ORDER`（管理页「歌单大类顺序」输入框）：各大类
（含每日推荐）在飞牛歌单列表里的先后可自定义，漏写的按默认序排最后。

### 4. 管理页保存配置后 fpk 应用闪退

保存配置触发 stop→start，几秒到几十秒的窗口里 `cmd/main status` 返回 3（未运行），
飞牛桌面据此回收本应用已打开的窗口——用户看到的就是「保存一次、闪退一次」。

修复：新增**重启标记** `${PKGVAR}/restart.inprogress`。`restart_services.sh` /
`config_callback` 在重启窗口内打标记（trap EXIT 兜底清理），`status.sh` 看到
fresh 标记（≤300s，防 kill -9 留死标记）就继续报 running，窗口不再被收走。

### 5. 启用扩展后开转码，本地音乐播放不了

本地曲目转码链路（`/track/transcode` → `/track/hls/...`）经代理转发时有两处不透明：

- **Host 头被剥掉**：`copy_incoming_headers` 排除了 `host`，httpx 于是发
  `Host: unix`。官方后端在转码/HLS 链路里按请求 Host 拼绝对地址（m3u8 分片 URL），
  拼出来的地址客户端连不上。不开转码的 `/track/stream` 用相对路径，所以平时看不出来。
  现在原样透传 Host。
- **30s 共享读超时**：官方后端处理 `/track/transcode` 要等 ffmpeg 产出首个 HLS
  分片才应答，大文件（DSD/APE/FLAC）+ 慢磁盘时 30s 不够，超时把「能播」变 500。
  现在播放链路（stream/hls/transcode）的本地转发读超时放宽到
  `FNMUSIC_PLAYBACK_FORWARD_TIMEOUT_S`（默认 300s），且上游超时/传输错误
  改答 504/502 而不是裸 500。

### 测试

新增 24 个回归用例：.env 三源合并持久化（真实执行 setup.sh）、uid 解析与
channels 自检、大类顺序与稳定时间戳、重启标记、Host 透传与本地转码全链路。
全套 667 passed, 1 skipped。

## [2.3.0] - 2026-09-12

按飞牛的「音质偏好」（WiFi 用原始/标准、流量用原始/标准）**动态调整给网易云的音质档位**。
新模块 `proxy/quality.py`，管理页可配置，判定结果与发现证据进一键诊断。

### 为什么不是「直接读飞牛的设置」

飞牛把音质偏好放在哪个接口、哪个库表、哪个字段，**我们没有可靠证据**：真机上没有公开
契约文档，而猜一个接口名或字段名去读，猜错时**不会报错**，只会静默地一直走默认音质
——用户以为"跟随飞牛"生效了，其实从来没生效过。这类静默失效比明确不支持更难排查。

所以本版采取「**可发现 + 可报告 + 有手动兜底**」，而不是赌一个接口名：

1. **手动策略永远可用**，是确定生效的那条路（三选一）：
   - `follow_fnos` 跟随飞牛（读不到时回落到下面的手动值）
   - `by_network` 按网络分别设置（WiFi / 流量各一档）
   - `fixed` 固定一档，不分网络
2. **被动发现只依据真正观察到的东西**，不预设任何接口名/字段名：
   - 客户端请求线索：我们的代理就架在飞牛 socket 上，客户端**每个请求都经过这里**。
     中间件把带音质/网络语义的键（`quality`/`bitrate`/`network`/`pref`/`transcode` 等
     子串命中）连同**原始值样例**记录下来。真机放着用一会儿，就能看到飞牛实际传了什么。
   - `music.db` 的 **schema 容错扫描**：不预设表名列名，而是枚举所有表、只扫"像
     key-value 偏好"的表（列数 2–8 且含 key/name/setting/code/type/id 之类列），
     再看整行文本是否命中音质/网络语义词。只读打开（`mode=ro`），默认缓存 5 分钟
     （`FNMUSIC_QUALITY_DB_RESCAN`），扫不动只记日志，绝不影响播放。
3. **是否真跟随上必须可查证**：`resolve()` 返回 `source` 字段——`auto:*` 表示读到了
   飞牛偏好，`fallback:*` 表示没读到、在用手动/默认档位。一键诊断里直接把
   `current.source`、已观察到的客户端线索、`music.db` 命中行都打出来，并明确提示：
   当前是回落状态时，把这些证据贴出来就能确定飞牛把偏好放在哪里，进而改成真正的自动跟随。

### 档位映射：一处真实的语义歧义

飞牛只有「原始 / 标准」两档，网易云有 6 档（`jymaster/hires/lossless/exhigh/higher/standard`），
不是一一对应。映射刻意保守：**原始 → lossless，标准 → exhigh(320k)**。把「标准」映射成
`standard`(128k) 掉得太狠；映射成无损又失去省流量的意义。

歧义在于：**网易云自己也有一个叫 `standard` 的档位，含义是 128k**，与飞牛文案「标准」
字面相近却差两档。规则是**精确档位名优先**（来源若用的就是网易云词汇，照搬其含义最不易错），
只有中文标签「原始 / 标准」与 `original`/`无损`/`省流量` 这类**飞牛语义**才走映射表。
两种写法都有测试钉住。

另外实测确认：**中文网络标识只可能出现在 query，不可能出现在 header**——HTTP 头是
latin-1，Starlette 的 `Headers` 装非 latin-1 值会直接 `UnicodeEncodeError`；而 URL 解码后的
query 是 UTF-8，能正常携带「流量 / 无线」。所以中文关键词识别挂在 query 上。

### 生效路径与安全性

`resolve_netease_url()` 现在按本次请求动态选档（播放路径把 `request` 传进去，让 quality
看得到网络线索），**选中的档位上游不给直链时仍会继续降到 `exhigh`**——不能因为策略选了
高档就直接播放失败。不传 `request`（同步场景/测试）时按 WiFi 档处理，与升级前行为一致。

判定结果记日志供事后查证，但**每种 (档位, 来源) 组合只记一次**：一首歌至少一次取链，
每次都记会把日志刷满。

### 实现中发现并修掉的一个真 bug

`music.db` 的扫描缓存原先**没有按路径区分**：只看时间戳，换一个 db 路径会直接返回上一个库
的扫描结果。真机上音乐库路径可能变（换盘 / 换共享库），届时音质自动判定会一直基于旧库的
偏好行，**全程不报错、无从察觉**。已改为按路径做缓存键，并加测试覆盖（两个不同库必须
各自重扫，同一路径仍命中缓存）。

### 管理页

- 音质策略（下拉，3 选 1）
- WiFi / 原始音质档、流量 / 标准音质档、固定音质档（6 档可选，含 `hires`/`jymaster`；
  账号无对应权益时上游自动降级，不会因此播不出来）
- 每项都注明了到诊断页哪里去核实是否真的生效
- 一键诊断新增「音质策略与『跟随飞牛』发现情况」整段

诊断页读不到时会显示「代理进程不可达」而不是「没有偏好」——**观察记录只存在于代理进程
内存里**，管理页面是另一个进程，必须经 unix socket 取（新增 `GET /_ext/quality`，与
2.1.4 的 `/_ext/cache/invalidate` 同一套做法）。直接 import 读到的永远是空。

### 配置

`.env` 新增 5 键：`FNMUSIC_QUALITY_POLICY`（默认 `follow_fnos`）、
`FNMUSIC_QUALITY_FIXED`/`_WIFI`（默认 `lossless`）、`FNMUSIC_QUALITY_CELLULAR`
（默认 `exhigh`）、`FNMUSIC_QUALITY_DB_RESCAN`（默认 300 秒）。
`env_merge.NEW_PREFIXES` 放行 `FNMUSIC_QUALITY_`（只允许已在 `NEW_DEFAULTS` 列出的键被补齐）。

### 测试

新增 53 个用例（**644 passed / 5 skipped**，连跑 3 轮稳定）：

- 档位归一化全表（原始/无损/original→lossless，标准/320/高音质→exhigh…，认不出来返回空串）
- **`standard` 与「标准」的歧义**：精确档位名按网易云原义（128k），中文按飞牛语义（320k）
- 策略默认值与非法值回落；档位配置认不出来返回空串交 `resolve()` 统一回落（而不是在这里
  悄悄换成默认档，那会让页面显示与实际生效值对不上）
- 网络类型判定：header/query、英文/中文、键名不含网络语义时**不采信**；
  并注明中文只能来自 query（header 会 UnicodeEncodeError）
- 被动观察：只记命中语义的键、样例上限与超长截断、任何垃圾入参都不抛异常
- `music.db` 扫描：schema 容错命中偏好行且**不误报无关行**、宽表（列数 >8）不扫、
  缓存命中与强制重扫、**缓存按路径区分**、缺失/损坏/只读库都安全、扫描结果能正确映射档位
- 决策：三种策略各自的 `source`；自动跟随读到证据时以飞牛为准；读不到时回落手动值；
  连手动值都认不出来时沿用既有 `netease_quality`；`report()` 永不抛异常且证据完整
- 接线：取链真的用了动态档位（流量场景要低档，不再固定 lossless）；策略选高档而上游拒给
  直链时**必须继续降到 exhigh**；中间件只记 `/music/api/` 且观察器抛异常也不影响请求；
  `/_ext/quality` 正常出报告、报告失败时给出可读错误；日志按组合去重不刷屏
- 打包清单守护：`proxy/quality.py` 必须登记在 `build_fpk.sh` 两处清单里
  （2.2.0 踩过：包构建成功、自检还报"全部通过"，payload 里却没有新模块，装上去 import 就炸）

> 说明：本版新模块不依赖 `NetEase-MusicBox`，档位最终仍由 musicbox 侧
> `/api/v1/song/{id}/url?quality=` 执行，那条路径的可播性与降级行为由 2.1.5–2.2.0
> 的既有用例覆盖。

### 真机上怎么确认「跟随飞牛」到底生效没有

打开一键诊断，看 `quality` 段：

- `current.source` = `auto:music_db` → **真的读到了**飞牛偏好
- `current.source` = `fallback:manual_or_default` → 没读到，在用手动脉位；
  把同段的「客户端音质/网络线索」与「music.db 命中行」贴出来，就能确定飞牛把偏好存在哪，
  进而改成真正的自动跟随
- `observed_client_hints` 为空且 `music.db` 命中 0 行 → 两条被动发现路径都还没找到证据，
  此时建议直接用「按网络分别设置」或「固定音质」，这两条是确定生效的


## [2.2.1] - 2026-09-11

修一个 2.2.0 上线就带出去的真 bug：**管理页保存「收藏归档目录」必然失败**。

用户实际看到的报错：

```
失败：download_dir: 取值非法（attempted relative import with no known parent package）
```

### 根因：函数级相对导入 + 管理页以顶层模块方式运行

2.2.0 的路径校验器写成了这样：

```python
def _as_path(v):
    ...
    from . import download as _dl      # ← 函数级相对导入
    ok, why = _dl.validate_dir(path)
```

管理页在生产上以 `uvicorn --app-dir proxy` 启动，此时 `admin_ui` 是**顶层模块**、
没有父包，`from . import download` 必抛 `ImportError`。`proxy/admin_ui.py` 顶部本来就有
为这个场景准备的双分支导入（`try: from . import …` / `except ImportError: import …`），
我在函数体里另写了一份相对导入，等于绕过了那个机制——**而按包导入的测试环境根本触发不到**，
所以本地全绿、真机必挂。

更糟的是错误被二次误导：外层保存逻辑是

```python
except ValueError as exc:  errors.append(f"{field}: {exc}")
except Exception as exc:   errors.append(f"{field}: 取值非法（{exc}）")
```

`ImportError` 走进第二个分支，被包成「**取值非法**」。于是明明是程序错误，却显示成用户的
输入不合法——用户改一万遍路径也过不去，而且完全无从判断问题出在哪。

### 修复

1. `download` 改为与其它同级模块一致的**模块顶部双分支导入**，两种运行方式（`proxy.admin_ui`
   包模块 / `--app-dir proxy` 顶层模块）都成立。
2. `_as_path` 内部把「我们自己的异常」与「路径不合法」区分开：内部异常一律报
   `内部校验出错（非路径问题）: <类型>: <原因>`，**不再伪装成"取值非法"**。
   程序错误就该按程序错误的样子出现，否则会把用户指向错误方向。

### 测试

新增 9 个用例（**591 passed / 5 skipped**，连跑 4 轮均稳定）：

- 真实可写目录通过；留空/None/纯空白 = 关闭归档而非校验失败
- 系统目录（`/`、`/etc`、`/var`、`/root`）与相对路径被拒，且错误信息说清是哪条规则
- 目录不存在被拒并给出「不存在」
- **内部错误必须如实标注**：把 `validate_dir` 打桩成抛异常，断言报错含「内部校验出错」
  与「非路径问题」并带上原始异常——正是本次 bug 的本质，用户曾被指向错误方向
- **核心回归：以全新解释器、`PYTHONPATH=proxy` 的方式把 `admin_ui` 当顶层模块导入并执行
  `_as_path`**。这是生产上真实的启动方式，也是该 bug 只在真机出现的唯一原因；
  必须用子进程验证，排除本测试进程已按包导入过的干扰。

### 顺带揪出一个真实的测试竞态（不是测试写错，是产线代码的隔离缺陷）

`test_run_musicbox_actually_executes_resolved_cli` 偶发失败：`code=127`、报
「musicbox CLI not found」，而候选路径里明明列着夹具刚造好的假 venv；**单跑又永远正常**。

第一版归因（"`runner._CMD_CACHE` 没被夹具重置"）是**错的**——补上重置后仍然复现。
真正的根因是：2.1.6 加的 CLI 后台预热线程会调 `run_musicbox` → `resolve_musicbox_cmd`，
而解析读的是 `sys.executable` / `sys.prefix` 这类**全局**状态；测试里
`monkeypatch.setattr(mb_runner.sys, "executable", …)` 改的正是同一个全局对象。
线程与用例并发时，会在被打桩过的状态上完成解析，并把结果写进**进程级** `_CMD_CACHE`，
于是别的用例拿到一个错的"找不到 CLI"缓存。夹具在用例边界重置缓存，挡不住
"用例执行期间"的写入。

修复不是给测试加更多清理（那是绕过去），而是**消除并发本身**：

- 新增 `FNMUSIC_CLI_WARMUP`（默认 on），`off` 时不起任何预热线程。这也是一个有用的
  产线开关：慢速 NAS 上未必需要那次 120s 上限的后台探测。
- `conftest.py` 里 `setdefault` 成 `off`，并写明原因。
- 三个专门验证预热行为的用例显式把它设回 `true` 再测（否则它们测的就是"被关闭"）。
- 同时把 `reset_cmd_cache()` 并入 autouse 夹具——它不是本 bug 的根因，但确实是另一条
  真实的跨用例污染通道，一并堵上。
- 新增用例 `test_warmup_can_be_disabled`：断言关闭后绝不执行 CLI、不写缓存。

修完连跑 4 轮 `591 passed / 5 skipped` 全绿，偶发失败消失。


## [2.2.0] - 2026-09-11

功能版本。四件事：**更多口径的推荐歌单**、**账户歌单显示到飞牛**、**点收藏自动下载最高
品质音频+歌词（按歌手建子目录）**、**收藏同步回网易云（红心双向）**。

四项口径由用户在真机确认后落地：推荐口径全开但**可在管理页勾选**；账户歌单**自建+收藏
全部显示并加前缀**；归档走**管理页自定义路径 + 歌手子目录 + 最高品质**；红心**双向**同步。

### 1 & 2. 更多口径歌单 + 账户歌单

新模块 `proxy/playlists.py`。沿用既有「伪歌单」注入法（每日推荐就是这么做的），在官方
歌单列表头部追加由网易云内容构成的条目：

| 口径 | 上游能力 | 需登录 |
| --- | --- | --- |
| 我的歌单（自建+收藏） | `user_playlist(uid)` | 是 |
| 推荐歌单 | `recommend_resource()` | 是 |
| 排行榜 | `fetch_toplists()`（实测 63 个榜） | 否 |
| 分类歌单 | `top_playlists(cat, order)` + `playlist_catelogs()` | 否 |
| 新碟上架 | `new_albums()` + `album(id)` | 否 |
| 私人FM | `personal_fm()` | 是 |

guid 规范：真实网易云歌单（我的/推荐/排行榜/分类）统一为 `online:playlist:ne:{id}`，
新碟为 `online:playlist:nealbum:{albumId}`，FM 为固定 `online:playlist:nefm`。四个口径
共用一种 guid 是因为它们最终都是「歌单 id → 曲目 id」，取内容路径完全一致，口径差别只
体现在名字与封面上。

几个刻意的设计决定：

- **未登录时，需登录的口径直接不出现**，而不是塞一个点进去没内容的空歌单——空歌单比
  不出现更容易被误判成"坏了"。且此时**连上游都不打**，白跑一趟只会拖慢列表。
- **每个口径有注入上限**（默认 8）。排行榜上游 63 个、分类歌单一次能取 50 个，不设限
  会把用户自己的本地歌单彻底淹掉。
- 口径顺序按**规范序**输出，不随用户勾选先后漂移。
- 歌单名加前缀区分本地歌单：`网易云·自建单` / `网易云·收藏别人的单` / `榜｜飙升榜` /
  `华语｜xxx` / `新碟｜专辑 - 歌手`。

**注册表落盘**（`{PKGVAR}/playlist_cache/registry.json`）：飞牛取封面时只带 guid，
不带名字与封面，若只存内存，重启后 `playlist/detail` 与 `/static/cover` 就只能显示
「网易云歌单 12345」且无封面。**刻意放 `${PKGVAR}` 而不是 `${RUN_DIR}`**——后者在
「卸载+重装」时整个被删（2.1.7 刚踩过的坑）。

两个真实坑：

1. **歌单封面协议是 http**：上游歌单 `coverImgUrl` 返回 `http://p1.music.126.net/...`
   （歌曲 `picUrl` 才是 https）。飞牛 UI 跑在 https 下，http 图片会被浏览器按混合内容
   拦掉，表现就是裂图/无封面。统一升级为 https。
2. **`playlist_songlist` 的 `trackIds` 不是纯 id 列表**：实测返回的是
   `[{"id": …, "v": …, "at": …}, …]` 这种 dict 列表，直接当 int 用会全线炸。已兼容两种形态。

`/static/cover` 有个必须在通用分支之前拦住的坑：`online:playlist:ne:123` 同样满足
`is_online_guid()`，而通用逻辑是 `split(":")[-1]` 取"歌曲 id"，于是会把 `123` 当成
song_id 去查——**给歌单配上一首完全无关歌曲的封面**。排行榜口径上游不给封面，回落到
榜单第一首歌的专辑封面并写回注册表（代价较大，因此只发生一次）。

### 3. 点收藏自动下载

新模块 `proxy/download.py`，由 `favorite-track/create` 触发：

- 取该账号**能拿到的最高品质**：`jymaster → hires → lossless → exhigh` 逐档试
- 落盘 `{自定义目录}/{歌手}/{歌手} - {歌名}.{flac|mp3}`，同名 `.lrc` 歌词
- 写 id3/flac 标签（title/artist/album）+ `.fnmusic.json` sidecar（记录 song_id/品质/大小）
- 登记 `.archive.ref`，与「边播边存」的 tee 缓存**分用两个 ref 命名空间**——
  两者落在不同目录，复用同一个 ref 会互相覆盖，卸载时漏删一边
- 归档目录默认**留空 = 关闭**：这是往用户自己的磁盘写文件，不能替他决定写到哪儿

安全边界（这是本扩展唯一主动往用户指定目录写文件的功能）：

- 路径必须是**已存在的可写绝对路径**；`/`、`/etc`、`/var`、`/root`、`/vol1/@appdata`
  等系统目录**硬拦**——往这些地方递归建目录写音频，用户几乎无法自行清理
- 歌手名清洗：网易云多歌手是 `A / B` 形式，`/` 直接当目录名会建出错误层级
- **半截文件绝不留**：按 content-length 校验，写不满就删临时文件，宁可不归档也不能让
  飞牛扫到一段坏音频
- 已归档且品质不低于本次 → 跳过；拿到**更高**品质 → 重新下载替换
- 下载在后台任务里跑，收藏接口立即返回；同一首在跑则不重复下载

⚠️ **实现时发现并已修正的一个静默错误**：上游会按账号权益**自动降级**并以
`code=200` 返回。请求 `jymaster` 时，非臻品权益账号拿回来的可能是 `level=exhigh`
（实测匿名账号对免费曲：br=320000、type=mp3）。第一版把「请求的档位」当作「拿到的
档位」记进 `best_quality`，于是日志与 sidecar 都**谎称存了臻品母带，实际是 320k mp3**。
这种错不会报错，只会让用户某天发现硬盘上全是 mp3。现改为取响应里的实际 `level`，
并保留 `requested_level` 与 `downgraded` 以便对照。

### 4. 收藏同步回网易云

红心用上游现成的 `song_like(songid, like=…)`（eapi `/api/song/like`）。收藏=加红心，
取消收藏=撤销红心。**归档文件不随取消收藏删除**——那是用户主动下载的资产。

- 这是**对用户账号的写操作**，故不静默失败：结果如实进日志（成功不打扰，失败必留痕）
- `like=False` 这条分支在 NEMbox 源码里从未被调用过（CLI 只有加红心入口），属未验证
  路径，因此同样如实回传结果而不做乐观假设
- **响应体形状保持 `data: None` 不变**。第一版把红心/归档结果塞进了 `data`，被既有
  测试当场拦下：那是飞牛官方接口的响应契约，不能为了"回传细节"去改线上协议

两个必须挡住的输入坑（都由既有测试抓到）：`song_id_from_online_guid()` 返回的是
`netease:228908` 这种带来源的串，直接 `int()` 会 ValueError（项目其它地方一律再
`.split(":")[-1]`）；guid 也可能是 `online:kuwo:123` 这类**非网易云**来源，绝不能拿去
往用户网易云账号里加红心。

### 管理页

- 「歌单口径」多选框（6 个口径）→ 隐藏域 `FNMUSIC_NETEASE_CHANNELS`
- 每口径注入上限、分类歌单的分类、歌单曲目上限
- 收藏归档目录（保存时即校验路径，而不是等点收藏才发现路径不对）、
  自动下载开关、红心同步开关

`.env` 新增 9 个键，`env_merge.NEW_PREFIXES` 同步放行（只允许**已在 `NEW_DEFAULTS` 里
列出**的键被自动补齐，不会凭空给老用户塞未定义项）。

### 测试

新增 40 个用例（**584 passed / 1 skipped**，真实 `NetEase-MusicBox 0.5.3`；
**580 passed / 5 skipped**，系统 python）。

`test_playlists_channels.py`：guid 规范与命名空间互不误伤；封面 http→https 升级；
前缀命名；口径开关解析与规范序；上限裁剪；注册表读写/重启存活/部分更新不冲掉旧字段/
损坏时重建不炸；**未登录时需登录口径一个都不出现且不打上游**；公共口径正常注入且专辑
用独立 guid 前缀；单口径抛异常只影响它自己；曲目解析去重 + 只跑一次批量补封面 +
可播数回填注册表。

`test_download_archive.py`：路径安全（相对路径/不存在/系统目录硬拦）；歌手名清洗
（`A / B` 不得拆成两级、全分隔符不得生成空名、超长截断）；目录布局与 `.lrc` 同名同目录；
扩展名判定；正常归档（音频+歌词+sidecar+ref 登记+临时文件清理）；空歌词不留空文件；
**半截文件与超小流都必须丢弃**；上游 4xx 如实报告；无可用品质不写 0 字节文件充数；
未配目录即关闭；同品质跳过不重写；更高品质替换并更新 sidecar；品质排名顺序；
后台任务去重且结束自动出队。

最高品质取链：实际 level vs 请求 level、上游降级须如实标记、高档不给直链时继续下探、
四档全拒返回空、只给试听片段不算可归档、eapi 炸了走 weapi 降级。

#### 打包清单差点漏掉新模块（已加守护测试）

`build_fpk.sh` 用**显式文件清单**决定哪些文件进 payload（`sync_into_app` 逐个列出，
自检再列一遍）。新增 `proxy/playlists.py` 与 `proxy/download.py` 时没登记，结果
**包构建成功、自检还报"全部通过"**，但 payload 里根本没有这两个模块——装上去 import
就直接 ImportError。是解包核对 payload 时才发现的。已补进两处清单，并加了两个守护测试：
`proxy/` 下每个运行期 `.py` 都必须出现在清单里，自检清单同样必须覆盖。
这类"新文件忘了登记"的错不会有任何提示，只能靠测试钉住。

> 测试基建：系统 python 无 `NEMbox` 时，为 `_level_to_encode_type` 加 autouse 打桩——
> 否则它内部的 `from NEMbox.api import` 会 ImportError，被 `except` 吞掉后返回 `{}`，
> 一整类纯逻辑用例就悄悄测不到了（本次正是靠这个才发现）。装真实包时不打桩。

### 真机实测记录

匿名环境下（真实上游）：排行榜 63 个、分类目录 5 大类（语种/风格/场景/情感/主题）、
分类歌单与新碟封面均带 https、`飙升榜` 解析出 60 首可播曲目且逐首带封面、注册表落盘
9 条、`/static/cover` 对歌单 guid 返回 200 `image/jpg` 38KB 且带 `Cache-Control`。
归档真实落盘成功：1.78MB 音频 + 317B 真实 `.lrc`（`[00:01.751]拉不拉多`）+ sidecar +
ref 登记，多歌手目录名正确清洗为 `孙这 _ 合唱`。

需登录才能验证、**无法在沙箱内确认**的部分（`user_playlist` 的 `subscribed` /
`coverImgUrl` / `trackCount` 字段是否齐全、`recommend_resource` 与 `personal_fm` 的真实
返回、`song_like` 尤其 `like=False` 是否生效）一律做了容错处理：字段缺失只影响显示，
不影响功能。为此新增 `GET /api/v1/channels/selftest`，逐口径报告能否取到数据与条数，
并在未登录口径上如实标注 `skipped: not_logged_in`——出问题时能一眼看出是**哪一路**挂了。


## [2.1.7] - 2026-09-11

2.1.6 之后播放已经正常。本版处理真机反馈的三个问题：**每次装新版都要重新扫码**、
**搜索结果与每日推荐没有封面**、**点开一首歌到出声约 4 秒**。三者根因互不相同。

### 1. 登录凭证持久化：装新版不再需要重新扫码

**根因是卸载脚本里一个写错的条件**：

```bash
if is_true "${REMOVE_DATA}" || is_true "${REMOVE_LIB}" || true; then
    rm -rf "${RUN_DIR}"     # ← 末尾的 || true 让它恒为真
fi
```

紧挨着它上面的注释写的是「用户数据默认保留；`wizard_remove_data` /
`wizard_remove_library_cache` 为 true 时才删除」——`|| true` 使注释与代码完全相反：
**无论用户是否勾选，运行目录整个被删**。而网易云登录凭证当时住在
`${RUN_DIR}/musicbox-data` 里，于是每装一次新版就丢一次 cookie。

手动安装 fpk 走的正是「卸载 + 安装」而不是「保留数据的升级」（真机诊断日志里
每次安装前都有 `uninstall.log` 与完整 stop 流程，可与此对上），所以表现为**必现**。

修复分两层：

- **凭证搬家**：网易云 cookie 与 NEMbox 运行时数据迁到 `${PKGVAR}/musicbox-data`，
  即 RUN_DIR **之外**。依据是 `${PKGVAR}/logs` 本就能跨安装留存（诊断里能看到多次
  安装的历史日志），说明 PKGVAR 自身在卸载/重装后不会被清空。服务的
  `XDG_DATA_HOME/CACHE_HOME/CONFIG_HOME` 随之指向新位置。
- **改回尊重勾选**：默认保留运行数据；只有勾选删除数据时才连凭证一起清理。
  即便不删，也会把 `.env` 快照到 `${PKGVAR}/.env.preserved`，重装时若向导里
  PushPlus token 为空就自动恢复，免得重填。

迁移逻辑（`lib_migrate_musicbox_data`）刻意保守，全部有真实 bash 执行的测试覆盖：

- 幂等；旧目录不存在时安静跳过，绝不让安装失败
- **新位置已有 cookie 时绝不覆盖**——否则会把用户刚扫好的码冲掉，比不迁移更糟
- 迁移后把 `cookie.txt` 收紧到 **0600**、目录 0700；且这个 `chmod` 不能写在
  「能取到包用户名」的判断之后（原先如此），否则异常环境下敏感凭证停留在 0644，
  本机其他用户可读

### 2. 封面：改为由 NAS 代抓回传，不再依赖客户端直连网易云 CDN

先排除了一种猜测：**不是 HTTPS 混合内容问题**——实测上游 `al.picUrl` 返回的
就是 `https://p1.music.126.net/...`。也验证了我们这侧的输出完全正确：

| 检查项 | 实测结果 |
| --- | --- |
| 搜索列表 JSON 的 `cover_url` / `coverUrl` | 已填充 https 封面（enrich 生效） |
| `/static/cover` 裸冒号 / `%3A` 编码 / 双重编码 / query / path | 5 种形态**全部** 302 到正确封面 |

原实现是 `RedirectResponse(302)`，把取图这件事完全交给客户端。它依赖两件我们无法
保证的事：客户端能直连 `p1.music.126.net`，且该 CDN 不校验 Referer/Origin。
任一不成立，界面表现就是「列表里没有封面」。

因此改为**由 NAS 代抓图片字节再回传**，并附 `Cache-Control: public, max-age=86400`；
代抓失败时**退回原来的 302**，不把好走的那条路也堵死。

顺带发现并修掉一处浪费：封面端点原先调 `_online_info`，而它会**顺带去拉一次歌词**
（另一个上游往返）——为一张缩略图取歌词毫无意义。新增 `_online_cover_url()`
只打一次 `/song/{id}/info` 取 `al.picUrl`。

另据实测记录一笔：上游 `search` 接口返回的 `al` 是**完全空的**（`picUrl=None`），
封面必须靠 `/api/v1/songs/detail` 补齐；这一步本来就有（0.16s），无需新增。

### 3. 点开歌曲约 4 秒：元数据重复回源

实测拆解（真实 NEMbox + 真实上游）：

| 环节 | 耗时 |
| --- | --- |
| 流式转发首字节（Range 命中刚落盘的缓存） | **0.02s** |
| 流式转发首字节（含从网易云拉 1.78MB 整首） | 0.84s |
| `resolve_netease_url` | 0.13s |
| `resolve_netease_url` + `_online_info` 并发 | 0.30s |

音频流转本身没问题，时间耗在**元数据重复回源**上：`_online_info` 原先**没有任何缓存**，
而 `/static/metadata`、`/lyric/list`、`/static/cover` 与播放路径都各自调它一次，
每次都是 `/song/{id}/info` + `/song/{id}/lyric` 两个上游往返。点开一首歌要重复好几轮。

单曲的标题/艺术家/专辑/时长/封面/歌词是**静态**数据，因此加 TTL 缓存
（元数据默认 3600s、封面 86400s，可用 `FNMUSIC_INFO_CACHE_TTL` /
`FNMUSIC_COVER_CACHE_TTL` 调整），命中后**零上游往返**。

两条设计取舍：

- **只缓存成功结果**：失败多半是上游瞬时抖动，缓存下来会把一次偶发失败
  固化成整段 TTL 内都没有元数据，比多回源一次更糟。空封面同理。
- `_online_info` 成功时**顺带填封面缓存**，随后的 `/static/cover` 直接命中。
  缓存有上限（默认 2000 条），超限按写入时间淘汰最旧的一半，不会无界增长。
- 登录成功后经 `/_ext/cache/invalidate` 一并清空（新增 `online_info_entries` 计数字段）。

### 测试

新增 29 个用例（**540 passed / 5 skipped**，系统 python；
**544 passed / 1 skipped**，真实 `NetEase-MusicBox 0.5.3`）。

新建 `proxy/tests/test_fpk_persistence.py`，对安装/卸载脚本做**真实 bash 执行**验证，
而不只是比对文本：

- `|| true` 恒真条件的回归断言；凭证目录必须在 RUN_DIR 之外，且
  `fnmusic-lib.sh` 与 `uninstall_callback` 两处定义必须一致（脚本间契约）
- `start.sh` 的三个 XDG 变量必须都指向持久目录，且不得再出现旧路径
- `chmod 600` 必须排在「包用户名存在」判断之前
- 真实执行迁移：搬迁成功且内容原样、权限 0600/0700、幂等、**绝不覆盖新扫的码**、
  旧目录缺失时安全跳过、建出 NEMbox 需要的三级目录
- 真实执行 `uninstall_callback` 两个分支：默认保留凭证并生成 `.env` 快照；
  勾选删除时才删凭证与运行目录，且日志如实记录

缓存与封面：

- 第二次 `_online_info` 零上游往返；TTL 过期后必须回源；**失败不缓存**且允许重试
- `_online_info` 成功后必须填上封面缓存
- `_online_cover_url` **只打一次 `/info`、绝不打 `/lyric`**；空封面不缓存
- `invalidate_online_info_cache` 清空两份缓存；`_cache_put_prune` 淘汰最旧一半而非最新
- `/static/cover` 代抓成功时返回图片字节 + `image/*` + `Cache-Control: max-age`；
  代抓失败时退回 302 且 `Location` 为原封面地址

> 测试基建：conftest 的 autouse fixture 增加清理这两份新缓存。它们是进程级 dict，
> 不清会让「同 guid 第二次调用」静默命中上一个用例的数据，排查起来极其迷惑。


## [2.1.6] - 2026-09-11

修复 2.1.5 之后暴露的下一个症状：**「搜索结果和歌单都出来了，但一直缓冲、无法播放」**。

2.1.5 修好了列表层（试听判定误杀），于是曲目能正常出现在搜索结果与每日推荐里。但列表层
走的是**进程内** NEMbox，播放层走的却是**另一条路**——这条路的性能问题此前被列表层的
空结果掩盖着，列表一空就根本走不到播放。

### 根因：播放热路径每首歌 spawn 一个 CLI 子进程，且慢到必然超时

`/api/v1/song/{id}/url`（取播放直链）与 `/api/v1/song/{id}/info`（取曲目详情）原先都是：

```python
return exec_musicbox(["song", "url", str(song_id), "--quality", quality, "--json"])
```

这两个端点位于**播放热路径**：飞牛播放器每首歌各调一次，一次搜索 50 首就是上百次调用。
用真实 `NetEase-MusicBox 0.5.3` 实测：

| 调用方式 | 耗时 |
| --- | --- |
| CLI 子进程**冷启动** | **47.37s** |
| CLI 子进程稳态 | 1.40s |
| 进程内 eapi 取链 | **0.05s** |
| 进程内 `songs_detail` 原始详情 | **0.07s** |

而代理侧对这两个请求的 httpx 超时只有 **10s** ⇒ 冷启动**必然超时**，
稳态 1.4s × 上百次调用也让播放器一直等不到直链。真机装完 2.1.5 后的实测（同一进程内
服务，真实上游）：`search` 0.58s、取链 0.05s、详情 0.07s、歌词 0.13s——全部远离 10s 红线。

真机日志里那几十条 `musicbox /info failed for online:netease:<id>` 正是同一件事。

### 诊断盲区：超时的异常日志冒号后面是空的

真机日志只有一行、看不出任何原因：

```
resolve_netease_url error for 94344 (quality=exhigh):
```

原因是 **httpx 的超时异常 `str()` 恒为空字符串**（实测 `ReadTimeout`、`ConnectTimeout`、
`ReadError`、`TimeoutException` 全部如此），而日志写的是 `"...: %s", e`。
于是「超时 / 连接失败 / 解析错误」在日志里长得一模一样（都是空）。

### 修复

1. **播放热路径改进程内取数**：新增 `netease_ext.song_url_info()` 与
   `song_raw_detail()`，`/song/{id}/url`、`/song/{id}/info` 改为进程内优先，
   CLI 仅在「连上游都没问到」（抛异常或空响应）时兜底。
2. **结构化 404 不再兜底跑 CLI**：上游明确回答「取不到直链」（`code=404` / `url=null`）
   是权威结果，再 spawn 一次慢 CLI 不会有不同答案。若照旧兜底，代理依次试
   `lossless → exhigh` 会让**每首不可播曲目触发两次子进程调用**，把这次修复又抵消掉。
3. **异常日志补上类型**：`resolve_netease_url`、`_online_info`、歌词抓取、批量详情
   共 4 处从 `"%s", e` 改为 `"%s: %s", type(e).__name__, e`，日志不再出现空冒号。

### 关于音质参数的实现说明

上游 `api.songs_url(ids)` 的 `level` 取自**全局** `Config().get("music_quality")`，
**不接受参数**；而本服务需要按请求音质取链（代理会依次试 `lossless → exhigh`）。
直接改全局 Config 会写坏用户的配置文件，因此新增 `_urls_for_level()` 按上游同样的
逻辑显式传 level（含 eapi 主路径与 weapi `rate_map` 降级）。

已实测校验：用 Config 当前值走 `_urls_for_level()`，其返回与 `api.songs_url()`
**完全一致**，故这是等价改写而非另起一套；并有测试把该等价关系与 `rate_map`
（`lossless → 999000` 等）钉住。

顺带一处发现：CLI `musicbox song url <id> --quality exhigh` 会返回 `code=404 / url=null`
（带 `cannotListenReason:1`），而 `--quality lossless` 能拿到真实直链（`level` 回落到
`exhigh`）；进程内参数化取链则两种音质都返回 `code=200` 且带真实直链。因此改用进程内后，
代理原本「lossless 失败再试 exhigh」的降级链两级通常都能直接命中。

### 连带修掉：诊断页 CLI 区块整栏 `undefined`

2.1.5 的真机诊断里，「CLI 自检」整栏是空的：

```
cli_found    : undefined   resolved_by=-
cli_cmd      : []
interpreter  : -  (undefined)
XDG          : {}
```

同一根因的另一处表现：`/api/v1/selftest` 内部会 exec `musicbox --version`（原先给了
15s 超时），冷启动 47s 必然超时；而管理页面探测 selftest 的超时只有 **10s**，
于是整个响应被放弃，连 `cli_found`、`interpreter`、XDG 这些**根本不依赖 CLI** 的
字段也一起显示不出来。用户唯一顺手的排障工具变成一片空白。

更要紧的是：2.1.6 把播放热路径改成进程内之后，CLI 再没有任何被顺带预热的机会
（原先是播放失败的尝试把它"焐热"的），这一栏会从偶发空白**变成常态空白**。

处理：

1. **启动时后台预热**（挂在 lifespan 上）：把 47s 级的一次性代价挪到后台线程，
   不阻塞服务就绪。预热在进程内幂等，只起一个线程。
   刻意**不放在模块级**——那样光导入模块（例如跑单元测试）就会 spawn 真实
   `musicbox` 子进程，有专门的测试守住这一点。
2. **探测结果缓存 + 短超时**：`selftest` 改用 `probe_cli_exec(timeout_s=3.0)`，
   命中缓存立即返回；只缓存**确定性**结果（成功、非超时类硬失败），
   超时**不缓存**——它多半只是 CLI 还在冷启动，不该被一次 3s 快速探测永久钉死。
3. **作废在途写入**：预热线程可能在很久之后才回来写缓存，用一个自增「代号」标记轮次，
   重置后旧轮次的写入直接丢弃。否则测试会随机串味，生产里则会报告过期的探测结果。
4. **超时消息可 grep**：从 `timeout running musicbox --version` 改为带
   `MusicboxTimeoutError:` 前缀，并解释冷启动成因（首次执行需生成 deviceId，
   实测 ~47s）与「后台预热中，稍后重试」。
5. 顺带把已废弃的 `@app.on_event("startup")` 换成 FastAPI 推荐的 lifespan 处理器。

真机复验（真实 NEMbox 0.5.3）：启动后 `/healthz` 0.03s 即就绪；
连续三次 `/api/v1/selftest` 均 **0.00s** 返回且 `cli_exec_ok=true`、
`interpreter`/XDG 等字段完整；日志出现 `CLI 预热完成 cli_exec_ok=True`。
全部远低于管理页面 10s 的探测超时。

### 测试

新增 20 个用例（**518 passed / 1 skipped**，真实 NEMbox 环境；
**514 passed / 5 skipped**，系统 python；连跑两轮均稳定无 flake）：

- `_urls_for_level` 与上游参数构造的等价性（紧凑 JSON `ids`、`level`、`encodeType`），
  eapi 有结果时**不得**再走 weapi 降级
- weapi 降级使用上游同款 `rate_map`（`lossless → br=999000`）；eapi 抛异常时仍降级
- `_pick_by_id`：优先匹配 id；多条且对不上时返回 `None` 而不是瞎猜；脏数据不炸
- `song_url_info` 返回结构与 CLI `song url --json` 的 `data` 字段兼容，
  且能被 `quality_of()` 正确判为 `HD 320k`
- `song_raw_detail` 必须保持**上游原始形状**（`ar`/`al`/`dt`/`sq`/`hr`/`h`）——
  代理 `_online_info` 自己解析这些字段，若给映射后的 `song_name`/`album_pic_url` 会失效
- 端点层：进程内成功**绝不再** spawn CLI；结构化 404 **不得**兜底；
  抛异常/空响应才兜底且 CLI 参数正确；走兜底时不谎报 `engine=in-process`
- 音质白名单照旧生效（非法音质 400），未被本次改动放宽
- 代理日志盲区回归：用真实 `httpx.ReadTimeout('')` 复现，断言日志含 `ReadTimeout`
  且两级降级（lossless/exhigh）各留一条痕迹
- selftest 探测：命中缓存后不得再执行 CLI；超时不被缓存（重试能拿到真实结果）且
  消息含异常类型名与冷启动成因；非超时硬失败应当缓存
- CLI 卡住时 `selftest` 仍须**快速返回完整结构**，其余诊断字段不得整栏空白
  （即真机「CLI 区块 undefined」的直接回归）；探测超时必须远小于管理页面的 10s
- 预热幂等：重复调用只起一个线程；lifespan 进入时才触发预热
- 作废代号的写入必须被丢弃
- **导入 `app` 模块不得预热 CLI**：用全新解释器子进程验证（排除本测试进程已 import
  的干扰），否则光跑单元测试就会 spawn 真实 `musicbox` 子进程

> 为便于测试打桩，把 `_level_to_encode_type` / `_quality_to_level` 抽成模块级薄封装
> ——否则 `from NEMbox.api import ...` 写在函数体里，未安装真实 NetEase-MusicBox
> 的环境会直接 ImportError，这些纯逻辑分支就再也测不到。运行期行为不变。


## [2.1.5] - 2026-09-11

**这一版才是「搜不到任何在线歌曲 / 每日推荐永远为空」的真正根因。**

2.1.3 与 2.1.4 修的都是真实存在的 bug（`dig_info` 全或无、cookie 单例过期），但都不是
这个症状的原因。装完 2.1.4 后的真机诊断显示：登录态三层已完全一致（`auth/status`、
`auth/detail`、登录态探测全部为已登录 VIP，`free_only=false`），
日志也确实出现「已重建 NEMbox 实例」——**然而搜索与日推依旧为空**，
且连 CLI 回退也是 0 条。CLI 是全新子进程、读的就是最新 cookie，它同样拿不到数据，
说明问题已经不在登录态与 cookie 上。

### 根因

可播性判定把 NetEase 响应里**恒存在**的结构体当成了试听标记：

```python
free_trial = item.get("freeTrialInfo") or item.get("freeTrialPrivilege")
if not url or not str(url).strip() or code == 404 or free_trial:
    continue          # ← 100% 曲目在这里被剔除
```

真机匿名调用 `api.songs_url()` 抓到的真实响应里，**每一条**都长这样：

```python
'freeTrialInfo': None,                                          # 假值
'freeTrialPrivilege': {'resConsumable': False,
                       'userConsumable': False, ...},           # 非空 dict＝真理值
```

`freeTrialInfo` 为 `None` 于是 `or` 落到 `freeTrialPrivilege`——它是网易云 song/url
接口**每条响应都必带**的标准结构体，永远是非空 dict。因此 `free_trial` **恒为真**，
每一首歌无论是否登录、是否 VIP、是否真拿到直链，都被判成「试听片段」剔除。

实测复现（匿名，真实上游）：

| | `songs_url` 原始结果 | 我们的 `filter_playable_song_ids` |
| --- | --- | --- |
| 修复前 | 5 条，其中 3 条带真实 320k 直链 | **0 / 5** |
| 修复后 | 同上 | **4 / 5** |

这也解释了为什么前三轮修复都没能让症状消失——它们各自修对了真 bug，
但没有一个触及这一行。

两个字段的语义**恰好相反**，混用真值判断是错误来源：

- `freeTrialPrivilege`：**恒存在**，存在与否不代表试听；只有内部的
  `resConsumable` / `userConsumable` 为 True 才表示正在消耗试听额度。
- `freeTrialInfo`：`None` 表示**无**试听，只有确实是试听曲目才带非空内容
  （形如 `{"st": 起始秒, "et": 结束秒}`），对它做存在性判断才是安全的。

### 修复

- `musicbox-service/netease_ext.py`：抽出 `is_trial_snippet()` 按上述真实语义判定，
  替换 `playable_url_map()` 里的真值误判。
- `proxy/app.py`：抽出 `_has_trial_fragment()` 修正 `is_playable_online_track()` 里的
  **同源缺陷**。该函数共 5 处调用，其中第 1034 行直接把上游原始条目（`raw`）喂进去——
  当前主链路因 musicbox 已做字段映射而未触发，属于潜伏 bug，一并修掉。
- 判定只返回布尔、对任意坏字段类型都不抛异常，避免把整批搜索打成空。

### 验证

用真实 `NetEase-MusicBox 0.5.3` + 真实上游接口端到端复验（匿名账号）：

- 搜索「拉布拉多」→ musicbox 侧 3 条，经代理两道可播性关卡后**存活 3 条**，
  均为 `HD 320k` 且带真实直链；修复前为 **0 条**。
- 搜索「周杰伦」→ 0 条，**这是正确行为**：匿名下上游对该关键词全部 8 首返回
  `code=404`、`url=null`（网易云拒绝对未登录用户发放直链）。真机为已登录 VIP，
  会正常拿到直链。特意核对这一点，是为了不把「上游拒绝」误当成「我们的 bug」。

### 测试

新增 12 个用例（**495 passed / 1 skipped**，真实 NEMbox 环境；
**491 passed / 5 skipped**，系统 python）：

- 用**真机抓到的完整真实响应结构**钉住「`freeTrialPrivilege` 恒存在 ≠ 试听」
- 试听只能通过内部布尔位（`resConsumable` / `userConsumable`）识别，
  且 `True` 与字符串 `"true"` 都要认（不同接口序列化不一致）
- `freeTrialInfo` 非空即试听、`None` 与空 dict 不算
- 显式 `is_trial` 标记照旧拦截，不被本次修复顺手放宽
- 各类坏字段（`None` / 字符串 / 数字 / 列表）只返回布尔、绝不抛异常
- `playable_url_map` 与 `search_songs` 喂真实响应必须放行并给出正确音质
  （`br=320000 → HD 320k`）
- 代理侧同上（同源缺陷必须一并守住）

> 附带修正：上一版为 `test_get_api_rebuilds_when_cookie_file_changes` 等 4 个用例
> 引入的「需要真实 NEMbox 包」跳过机制工作正常，本次未改动。


## [2.1.4] - 2026-09-11

修复「扫码登录明明成功了，搜索与每日推荐依然是空的」——这是 2.1.3 之后仍然复现的
最后一个现象。根因不在可播性判定逻辑里，而在**进程内的长命单例握着登录前的旧
cookie**，所以 2.1.3 把 `dig_info` 的逐首过滤修对之后，过滤依然按「未登录」执行。

### 修复

#### 扫码登录后进程内仍是未登录态（搜索/日推为空）

上游 `NEMbox/api.py` 的 cookie 只在**构造时读一次**：

```python
self.cookie_jar = MozillaCookieJar(self.storage.cookie_path)
self.cookie_jar.load(...)      # ← 仅此一次，之后永不重读
```

而扫码登录是由 **musicbox CLI 子进程**完成并写盘的（`netease_login.sh` 与管理页面
的扫码流程都是子进程）。子进程写完新 cookie 后，音源服务进程里被缓存下来的那个
长命单例仍然握着登录前的旧 cookie。

于是出现本次的诊断特征——两个接口互相矛盾：

| 接口 | 数据来自 | 结果 |
| --- | --- | --- |
| `/api/v1/auth/status` | 每次**新起 CLI 子进程**，读到新 cookie | 显示已登录 |
| `/api/v1/auth/detail` | 走进程内单例，带旧 cookie | 显示未登录 |

后续的可播性过滤（`playable_url_map` → `songs_url`）用的是进程内单例，请求带着旧
cookie 发出去，VIP / 付费曲目全部拿不到直链而被逐首剔除，最终表现为「登录了还是
搜不到在线歌曲、每日推荐不出现」。

修复分三层，缺一层都只算修一半：

1. **按 cookie 文件指纹自动重建实例**（`netease_ext._get_api()`）
   每次取用时比对 cookie 文件的 `(mtime_ns, size)` 与实例创建时的快照，一旦变化
   说明有人（通常是 CLI 子进程）重新登录过，整个实例重建以重读 cookie。这一层覆盖了
   **登录发生在别的进程**的情况——包括 `netease_login.sh`、页面扫码、以及手工改
   cookie。指纹取不到时退化为空指纹，不抛异常。
2. **登录成功即重建 + 作废登录态缓存**（`auth_login_check` code 803 → `reset_api_instance()`）
   原先只调 `invalidate_login_cache()`，不够：登录态缓存重查后确实会说「已登录」，
   但可播性过滤用的 `songs_url` 仍旧带着旧 cookie 发请求。新增
   `reset_api_instance()` 同时丢弃实例、cookie 指纹与登录态缓存。
3. **通知代理进程清缓存**（新增 `POST /_ext/cache/invalidate`）
   代理与管理页面是**两个独立进程**。页面扫码成功只重置了页面自己的登录态，代理那边
   仍会拿旧的搜索结果缓存（登录前查到的可能全是空结果）和旧的登录态（TTL 默认
   300s）继续服务几分钟。新端点丢弃搜索缓存、取消未完成的每日推荐后台任务、清掉按
   旧账号生成的每日推荐歌单文件，并重置登录态探测。由管理页面在 code 803 时调用；
   端点只做「丢缓存」这一件事，不改配置、不影响播放、可重复调用，代理不可达时只记
   日志返回 False，绝不让登录回调失败。

### 测试

新增 15 个用例，其中 4 个需要真实 `NetEase-MusicBox` 包（`pip install
NetEase-MusicBox` 后自动启用，否则跳过），用真实构造流程验证核心不变量。

- 真实包下验证「cookie 文件一变必须重建实例」「cookie 未变必须复用实例」
  （不能每个请求都重建，那会反复重算 deviceId 并加重磁盘 IO）
- cookie 文件不存在 / 被删除时指纹为空且不抛异常；用例结束还原原始 cookie
- `_get_api()` 重建路径会同时作废登录态缓存；重建失败时继续用旧实例而不是崩掉
- 803 触发重建，800/801/802 一律不触发（避免白丢一次 deviceId 计算）
- 可播性过滤必须基于**当前**登录态而非实例创建时那一刻：未登录剔除 VIP 曲、
  登录后放行
- `/ext/cache/invalidate` 清空搜索缓存、取消 pending 每日推荐任务、只删 `.json`
  不误伤同目录其他文件、目录缺失与连打两次都不炸、清完后旧空结果不再被当答案返回
- 跨进程契约测试：管理页面写死的 `/_ext/cache/invalidate` 必须真的存在于代理路由表
  （两个进程各存一份路径字符串，改一边忘另一边不会报错，只会静默退化成「等 TTL」，
  极难排查）
- 顺带修正 `test_run_musicbox_missing_binary`：装了真实 NEMbox 的机器上，解释器相对
  路径与模块回退都会命中并真把 CLI 跑起来（返回 2 而非 127）——那是解析器的正确
  行为。该用例要测的是「什么都找不到」分支，因此显式封掉这两级回退。同时把
  `MODULE_FALLBACK` 抽成 `module_fallback()` 函数，使测试可以打桩到不存在的模块。

全量 479 passed / 5 skipped（其中 4 个 skip 为需要真实 NEMbox 包的用例）。

### 说明

上游 `NEMbox/api.py:336` 使用 `CachedSession(..., expire_after=3600)`，但
requests-cache 默认只缓存 GET，而 NEMbox 的核心接口均为 POST，故该磁盘缓存对
登录态的影响可忽略，本次未改动上游行为。


## [2.1.3] - 2026-09-11

修复 2.1.2 装机后实测反馈的三个问题（VIP 状态、推送状态、搜不到歌与日推不出现）。
三者互相独立，根因各不相同。

### 修复

用户在真机上装完 2.1.2 后反馈了四个现象。其中「扫码登录 502」已在 2.1.2 修复
（见下文），本次修掉剩下的三个——它们根因各不相同，且都比表面现象深一层。

#### 现象 3／4：飞牛里搜不到任何网易云在线歌曲；「每日推荐」歌单根本不出现

同一个根因，在上游 `NEMbox/api.py` 的 `dig_info()`：

```python
for s in sds:
    url_index = url_id_index.get(s["id"])
    if url_index is None:
        log.error("can't get song url, id: %s", s["id"])
        return []          # ★ 只要有一首取不到直链，整个列表返回空
```

上游注释自己都写了「可能因网络波动，返回空值」。而 `musicbox search` 与
`musicbox recommend songs` 两个 CLI 子命令都要经过 `dig_info`，于是**任何一首坏数据
就会把整个搜索结果 / 整份日推清空**，HTTP 仍是 200，看起来一切正常。

- `/api/v1/search?type=song` 与 `/api/v1/recommend/daily` 改为走**进程内实现**
  （新增 `netease_ext.search_songs()` / `daily_songs()`），**逐首**判定可播性：
  坏数据只影响它自己那一首。进程内异常或返回空时才回退 CLI，并在响应里用
  `engine` 字段如实标注 `in-process` / `cli-fallback`。
- `/api/v1/recommend/daily` 先判登录再抓取，未登录一律返回 `not_logged_in`，
  绝不把上游那份与账号画像无关的热门填充当作日推。

#### 现象 3 的第二层：空结果被缓存了 7 天

修好上面还不够。用户日志里 `逆战` 那次搜索**没有任何 httpx 调用记录**，说明命中了
搜索缓存——早先一次 `dig_info` 返空时，`items: []` 被以 `FNMUSIC_SEARCH_CACHE_TTL`
（默认 7 天）写进了缓存，此后该关键词怎么搜都是纯本地结果，**即便上游早已恢复**。

- 新增 `FNMUSIC_SEARCH_EMPTY_TTL`（默认 60s）：空结果改用短 TTL，让故障自愈。
  有效性判定按「结果是否为空」选择 TTL；聚合完成时刷新 `ts`，
  使自愈窗口从"确认为空"那一刻起算，而不是被慢搜索吃掉。
- 结果为空时打 INFO 日志说明按短 TTL 缓存，便于排查。

#### 现象 1：明明是 VIP 却显示「非 VIP」，且没有剩余天数

`netease_auth.fetch_state()` 探的是 `/api/v1/auth/status`——它走 musicbox CLI，
返回体只有 `logged_in / nickname / user_id`，**根本没有 VIP 字段**。
而我为 VIP 信息专门加的 `/api/v1/auth/detail` 从来没被登录态模块调用过
（只有诊断页在单独调它，所以日志里能看到 `vip_type=110`，页面却显示非 VIP）。

- 探测顺序改为 `/api/v1/auth/detail`（进程内 NEMbox，字段全）优先，
  `/api/v1/auth/status`（CLI）兜底；旧版音源服务没有 detail 时自动回落，不误报未登录。
- `LoginState` 新增 `vip_expires_known` 与 `probed_via`，`to_public_dict()` 增补
  `vip_type` / `vip_expires_known`。
- **剩余天数如实显示「上游未提供」**：NEMbox 0.5.3 没有任何返回 VIP 到期时间的接口
  （`get_account_info` 只有 `vipType`）。这里对若干候选字段名做尽力探测、并在拿不到时
  试一次 `/weapi/v1/user/detail/{uid}`，仍然拿不到就标记未知；
  页面显示「上游未提供到期时间」而不是「剩余 0 天」或一个编造的日期。
  时间戳只在"确实是未来的毫秒值"时才被采信。

#### 现象 2：PushPlus 已配置 token，页面却显示「已关闭」

`pushplus.enabled()` 只读 `os.environ`。代理进程由 `run_proxy.sh` 用 `set -a; . .env`
启动，所以它有；但**管理页面进程**的启动命令只显式传了一组必要变量
（刻意不把 token 塞进进程环境，免得它出现在 `/proc/<pid>/environ` 里），
于是 `os.environ` 里没有 token → 显示未启用，页面内触发的「登录成功」推送也发不出去
（日志里 `.env` 显示 token 已配置、长度 32，`send_enabled` 却是 false）。

- `pushplus` 支持从 `.env` **文件**读取：`os.environ` 优先（含显式设置的空值与 false），
  缺失才回落到文件；文件按 `(路径, mtime, size)` 缓存，**改完配置无需重启即生效**。
- 文件路径解析顺序：`FNMUSIC_PUSHPLUS_ENV_FILE` → `FNMUSIC_ADMIN_ENV_FILE` →
  `$FNMUSIC_HOME/.env`。
- `env_merge` 不可用时退到内置的极简 dotenv 解析，不让一个可选依赖拖垮推送能力。

#### 顺带修掉的同类缺陷

- `netease_items.has_lossless()` 只认 `SQ` / `HR` / `无损`，而 NEMbox 真实产出的是
  `LOSSLESS FLAC` / `HIRES FLAC` / `JYMASTER FLAC` / `EXHIGH FLAC`——
  也就是说**线上从来没判出过无损**，所有曲目格式声明都被压成 mp3。
  现补齐上游真实词汇，并保留旧缩写；新增一条「上游音质串 → 代理层无损判定」的
  契约测试，防止两套词汇再次漂移。
- `netease_ext.check_is_logged_in()` 每次可播性过滤都要向网易云发一次
  `/weapi/nuser/account/get`（搜索与日推每轮都调）。现加 TTL 缓存
  （`FNMUSIC_LOGIN_CACHE_TTL`，默认 300s），并在扫码登录成功（CLI 退出码 803）时
  主动作废，避免登录完还被当成未登录而过滤掉 VIP 曲目。
  探测**异常**时不写入缓存，防止一次网络抖动让账号在整个 TTL 内被误判为未登录。
- 搜索的在线音源请求原先串行排在 upstream 请求之后，现改为**并发发起**；
  上游返回非 200 / code≠0 时取消在线搜索任务，不留空跑协程。

### 测试

- 新增 `runner` 解析回归测试：构造真实 `<venv>/bin/{python,musicbox}` 布局并把 PATH
  刻意设为不含它，断言仍能解析并真实执行；另覆盖 `which` 回退、模块回退、
  结果缓存、子进程 PATH 注入、代理变量剥离、候选路径去重与 `Scripts/` 兼容。
  **此前的测试全部 mock 掉 `runner.run_musicbox`，恰好绕过了这段解析逻辑**，
  这是该缺陷能长期存活的直接原因，现已补上真实可执行文件的测试。
- 新增 admin UI 用例：`healthz 200 + auth/status 502` 必须被提升为 problem 且点明影响面、
  必须同时探两条链路才看得出分歧、diag 必须带 selftest 明细、
  旧版音源服务无 selftest 时不能 500、`login_error` 必须进入 problems。
- 新增 `netease_ext` 逐首过滤测试：一首无直链 + 一首试听片段混在好歌里时，
  只剔除两首坏的、绝不整体清空（直接对应上游 `dig_info` 的全或无缺陷）；
  未登录只留免费曲、已登录保留 VIP 曲、畸形行不崩、登录态 TTL 缓存只打一次上游、
  登录后作废缓存、探测异常不污染缓存、音质词汇与代理层无损判定的契约测试。
- 新增 pushplus 配置文件读取测试：仅存于 `.env` 的 token 必须被识别为已启用、
  进程环境优先于文件、按 mtime 失效（改配置免重启）。
- 新增 env_merge 不变量测试：`NEW_DEFAULTS` 里每个键都必须能被 `NEW_PREFIXES` 匹配，
  否则升级时不会被补齐——`FNMUSIC_SEARCH_EMPTY_TTL` 已经漏过一次。
- `462 passed / 1 skipped`。

---

## [2.1.2] - 2026-09-11

修复一个会让**整个在线音源瘫痪**、但健康检查看起来一切正常的严重缺陷。这是 v1.x 就存在的
历史问题，只是 Docker 模式下不会暴露（pip 把 console script 装进 `/usr/local/bin`，本来就在
PATH 上），而 fpk 与 host 模式恒走 venv，100% 命中。

### 修复

- **`musicbox-service/runner.py` 用裸命令名调 CLI，在 venv 形态下必然失败。**
  `musicbox` 是 pip 装进虚拟环境的 console script，位于 `<venv>/bin/musicbox`。
  但服务的启动方式是**用绝对路径调 venv 里的 uvicorn**：

  ```
  .venv-musicbox/bin/uvicorn app:app --host 127.0.0.1 --port 8770
  ```

  这种方式**不会**把 venv 的 `bin/` 加进 PATH（只有 `source bin/activate` 才会），
  于是 `subprocess.run(["musicbox", ...])` 抛 `FileNotFoundError` → 退出码 127 →
  `UpstreamException` → **HTTP 502**。

  受影响的端点共 12 个：`search`、`song url`、`song info`、`artist`、`album`、
  `playlist`、`auth status`、`auth login`、`auth login check`、三个二维码接口、
  `recommend daily`。也就是**搜索、播放直链解析、扫码登录全部不可用**。

  而 `/healthz`、`/api/v1/auth/detail`、`/api/v1/songs/detail`、`/api/v1/song/{id}/lyric`
  这四个走**进程内 NEMbox Python API**，照常返回 200 —— 于是诊断页上看到的是
  「音源服务运行中」，用户却只看到「生成二维码 失败：HTTP 502」，完全对不上。

  现改为分级解析并缓存结果：

  1. `sys.executable` 同目录下的 `musicbox`（venv 内最可靠，uvicorn 正是从这儿起的）；
  2. `sys.prefix/bin`、`sys.base_prefix/bin`（含 Windows 的 `Scripts/`）；
  3. `shutil.which("musicbox")`；
  4. `[sys.executable, "-m", "NEMbox"]`（入口等价：pyproject 里
     `musicbox = "NEMbox.__main__:start"`）。

  同时把 venv 的 `bin/` 补进**子进程** PATH（CLI 自身可能再派生子进程）。
  彻底解析不到时返回明确诊断文本（列出尝试过的路径 + 期望位置），而不是含糊的 127。

  已用真实环境验证：装真实 `NetEase-MusicBox 0.5.3` 的 venv、PATH 刻意不含 `venv/bin`，
  旧实现复现 `FileNotFoundError → 127 → 502`；新实现解析到 `<venv>/bin/musicbox`，
  执行 `--version` 返回 `NetEase-MusicBox installed version:0.5.3`。
  顺带用真包核实了 v2.0 依赖的 CLI 行为：`recommend songs --limit N --json`
  未登录时退出码 **3** 且返回 `{"ok":false,"error":{"type":"not_logged_in"}}`，
  `auth status --json` 返回 `{"ok":true,"data":{"logged_in":false,...}}`，与实现一致。

### 新增

- **`GET /api/v1/selftest`**（musicbox 服务）：报告 CLI 是怎么解析到的
  （`cli_found` / `cli_cmd` / `resolved_by` / `venv_bin_dir` / `interpreter`）、
  能否真的执行（`cli_exec_ok` + 输出摘要，含超时）、NEMbox 是否可导入、
  运行身份与 XDG 目录。这类「healthz 200 但 CLI 全线 502」的故障，
  以后一个请求就能定位，而不是靠猜。
- **诊断与健康检查主动暴露 CLI 故障**：
  - `/api/diag` 增探 `auth/status` 与 `selftest`，并新增「CLI 自检」小节
    （解析方式、执行结果、解释器路径、venv bin、NEMbox 可导入性、运行身份、XDG），
    `cli_found=false` 或「healthz 200 但 auth/status 502」时直接打出结论与修复指引；
  - `/api/health` 把上述情况提升为 `problems` 条目，明确写明**影响面**
    （「这会让搜索/取直链/扫码登录全部不可用，而 healthz 仍显示正常」），
    并把登录态探测的 `error`（如 `http_502`）一并提升。
    之前这条 502 只躺在 `netease_error` 字段里，没人会去看。
  - 对没有 selftest 端点的旧版音源服务保持兼容，不会因此 500。


## [2.1.1] - 2026-09-11

修复一个会让管理页面「打得开但什么都点不动」的路径 bug，并按需求加上异常可见性与日志保留策略。

### 修复

- **经反向代理 / 组网隧道访问时，页面能渲染但所有接口返回 404**。
  根因：页面里用相对路径 `api/health` 发请求，而浏览器在 `/app/fnmusicext`（**无尾斜杠**）
  下会把它解析成 `/app/api/health`——网关前缀被吃掉一段，请求根本到不了本服务，
  用户拿到的其实是飞牛网关的 404。修法是两条一起上：
  1. 服务端算出真实 base 前缀并注入页面（`var BASE="/app/fnmusicext/"`），
     前端一律用 `BASE + "api/..."` 拼**绝对** URL，不再依赖浏览器的相对路径解析；
     base 只有服务端自己知道（它能看到网关转发过来的原始路径），因此由服务端注入而不是前端猜。
  2. `normalize_path()` 对**任意层数**的前缀做归一化：先按配置的网关前缀剥离，
     失败再按已知端点做后缀匹配（把端点前面的整段当作 base），
     页面请求则识别「以应用名或 `/app` 结尾」。
     于是 `/group/app/fnmusicext/api/health`、`/nodebaby/tunnel/fnmusicext/api/config`
     这类再套一层隧道/反代的路径也能正常工作。
  - 鉴权与路径无关，仍然强制：**无网关身份 Header 一律 403、非管理员一律 403**，
    不管从哪条前缀进来都一样（已补用例逐一断言）。
  - 形似路径不会被误放行：`/app/fnmusicext/xapi/health` 仍 404。
- `loghouse.rotate_file()` 轮转后原文件被改名带走、原路径不存在，与「另存备份再清空」的
  契约不符。改为轮转后显式重建一个 0644 的空文件。
- 浏览器必然请求的 `favicon.ico` 现在返回 204，不再在日志里留下误导性的 404。

> 说明：v2.1.0 的端到端验证用的是带完整前缀的 `/app/fnmusicext/api/health`，
> 恰好绕过了浏览器相对路径解析这一步，所以没抓到。本次补上了「按页面 URL 推导前端实际
> 会请求的 URL，再验证该 URL 能被归一化」的用例，覆盖有/无尾斜杠与多层反代共 4 种情形。

### 新增

- **一键诊断 `GET /api/diag`**：把排障需要的信息一次性回吐到页面，不用再 SSH。包含
  请求路径三元组（浏览器侧 base / 后端收到的原始路径 / 归一化后的内部路径）、
  `X-Forwarded-Prefix`、网关身份、socket 接管状态与权限、音源服务 `healthz` 与
  `auth/detail` 的**具体失败原因**（含异常类型与耗时）、`.env` 路径/可写性/权限/键数、
  重启脚本是否存在、日志目录清单（含每个文件大小与修改时间）、Python 版本与进程运行时。
  **不含任何凭据**：token 只报「是否已配置 + 长度」，永不报值。
- **页面异常自动展开日志**：状态获取失败或二维码生成失败时，页面会把真实原因
  （不再是干巴巴的 `HTTP 404`）显示出来，并自动运行一次一键诊断，
  把 `info.log` / 当前选中的日志 / `musicbox.log` / `proxy.log` 一起抓出来，
  附「复制诊断信息」按钮（剪贴板不可用时降级为可选中复制）。
- **日志保留策略（`proxy/loghouse.py`）**：单文件超过 `FNMUSIC_LOG_MAX_MB`（默认 10）
  就地截断只保留最近一半；超过 `FNMUSIC_LOG_MAX_DAYS`（默认 30）的**备份**日志直接删除、
  **活跃**日志清空内容但保留文件。可在配置页调整，也可点「立即清理超额日志」手动触发
  （`POST /api/logs/rotate`，返回逐文件动作与回收量）。管理页面进程每小时自动巡检一次
  （`FNMUSIC_LOG_SCAN_INTERVAL`），启动时也先清一次；`cmd/main start` 在拉起服务前
  由 `lib_rotate_logs` 再走一遍 shell 侧轮转。
  - **为什么是「就地截断」而不是改名/删除**：本项目日志都由 shell 的 `>> file.log 2>&1`
    重定向产生，写入端以 `O_APPEND` 持有 inode。`rename` 之后写入端仍指向被改名的
    inode（新日志写进了 `.1`，`x.log` 永远不再增长）；`unlink` 更糟（进程继续写已删除
    的 inode，日志彻底看不见且磁盘要等进程退出才释放）。就地截断对 `O_APPEND` 是安全的：
    下一次 `write` 落在新末尾。已补用例用真实子进程验证截断后仍能继续追加、
    最新行位于文件末尾、无空洞。
  - `rename` 方式的 `rotate_file()` 只在**服务启动前**使用（此时没有进程持有 fd）。

### 变更

- `GET /api/logs` 响应增加 `size_bytes` 与 `policy`（目录、上限、保留天数），
  页面据此显示当前生效的策略与文件大小。
- 配置项新增 `log_max_mb` / `log_max_days`（对应 `FNMUSIC_LOG_MAX_MB` /
  `FNMUSIC_LOG_MAX_DAYS`，`0` 表示关闭对应策略），已纳入写入白名单与校验，
  `env_merge` 会为升级用户自动补齐。
- `start.sh` 向管理页面进程透传 `FNMUSIC_ADMIN_UI_SOCK` 与三项日志策略，
  使诊断面板能显示真实的 socket 路径与当前阈值。

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
