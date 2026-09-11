# musicbox-service

HTTP 包装 [darknessomi/musicbox](https://github.com/darknessomi/musicbox)（PyPI：`NetEase-MusicBox`），
供 fnmusic-ext 作为**唯一**在线音源使用（默认 `127.0.0.1:8770`）。

音源全部来自扫码登录的那一个私人网易云账号的权益——没有任何免登录第三方解析路径。

## 接口

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/healthz` | 存活探针 |
| GET | `/api/v1/search?keyword=&type=song&limit=` | 搜索。已按当前账号真实可播状态过滤 |
| GET | `/api/v1/song/{id}/url?quality=` | 播放直链。`quality`: lossless/exhigh/higher/standard/hires/jymaster |
| GET | `/api/v1/song/{id}/info` | 曲目详情（名称、歌手、专辑、封面、时长、是否无损） |
| GET | `/api/v1/song/{id}/lyric` | LRC 歌词与翻译歌词 |
| GET | `/api/v1/songs/detail?ids=1,2,3` | 批量详情（用于补封面与音质判定） |
| GET | `/api/v1/recommend/daily?limit=` | **网易云官方每日推荐**（需登录） |
| GET | `/api/v1/auth/status` | musicbox CLI 原始登录状态 |
| GET | `/api/v1/auth/detail` | 登录详情：`logged_in` / `nickname` / `user_id` / `vip_type` / `vip_expires_ms` |
| POST | `/api/v1/auth/login` | 发起扫码，返回 `unikey` 与 `qr_ascii` |
| GET | `/api/v1/auth/login/check?unikey=` | 轮询扫码结果（801 待扫 / 802 待确认 / 803 成功 / 800 已过期） |
| GET | `/api/v1/auth/login/qr.png` | 二维码图片（局域网浏览器扫码用） |
| GET | `/api/v1/auth/login/qr` | 终端 ASCII 二维码 |
| GET | `/api/v1/artist/{id}` `/api/v1/album/{id}` `/api/v1/playlist/{id}` | 歌手 / 专辑 / 歌单 |

### `/api/v1/recommend/daily`

复用 musicbox CLI 的 `recommend songs` 子命令（内部走网易云
`/weapi/v3/discovery/recommend/songs`），返回结构与 `/api/v1/search` 的 `song_info`
完全一致，因此代理层无需额外字段映射。

- 成功：`{"ok": true, "data": [...]}`
- 未登录：`{"ok": false, "error": "not_logged_in", "data": []}`（依据 CLI 退出码 3 判定）
- 上游异常：`502`；CLI 超时：`504`

结果还会再过一遍 `filter_playable_song_ids`，把当前账号无权播放的曲目剔除。

## 可播性过滤（`netease_ext.filter_playable_song_ids`）

批量请求 `songs_url` 校验**真实直链**，而不是相信搜索结果的字段：

- 拿不到 `url`、或 `code == 404` → 过滤；
- 带 `freeTrialInfo` / `freeTrialPrivilege`（只能试听片段）→ 一律过滤；
- 未登录且 `FNMUSIC_FREE_ONLY_ON_LOGOUT=true`（默认）→ 只保留 `fee in (0, 8)` 的免费曲目。

已登录时使用账号自身权益，VIP / 无损 / 已购付费专辑曲目均可放行。

## 环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_FREE_ONLY_ON_LOGOUT` | `true` | 未登录时降级为只播免费曲目；`false` 则未登录不放行任何在线曲目 |
| `XDG_DATA_HOME` / `XDG_CACHE_HOME` / `XDG_CONFIG_HOME` | — | 网易云登录凭证与缓存位置，务必持久化，否则重启要重新扫码 |

`runner.py` 会主动剥离 `http_proxy` / `https_proxy` 等代理环境变量以保证网易云直连
（网易云公网 API 只接受中国区域访问）；**海外部署需另行处理出口网络**。

## 本地运行

```bash
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8770
```

扫码登录走仓库根的 `./netease_login.sh`（终端 ASCII 二维码 + 过期自动刷新 + 状态轮询），
或浏览器打开 `http://<NAS_IP>:8770/api/v1/auth/login/qr.png`。
