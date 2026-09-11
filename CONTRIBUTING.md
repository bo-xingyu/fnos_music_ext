# Contributing

## Ground rules

- Keep the Unix Socket takeover model. Do not change nginx files.
- `./extend.sh` and `./restore.sh` must remain idempotent and safe.
- No secrets in commits, tests, or sample configs (use `.env.example` placeholders).
  This includes the NetEase cookie and the PushPlus token — neither may ever be logged,
  echoed, cached, or asserted on in tests.
- Prefer adding tests in `proxy/tests/` for any proxy behavior change.

## Dev loop

```bash
python3 -m py_compile proxy/app.py proxy/recommend.py proxy/netease_auth.py \
  proxy/pushplus.py proxy/netease_items.py proxy/env_merge.py
bash -n install.sh extend.sh restore.sh netease_login.sh ensure_base_image.sh proxy/run_proxy.sh
.venv-proxy/bin/python -m pytest proxy/tests -q     # 288 passed, 1 skipped (needs ffmpeg)
```

## Architecture

The only online source is NetEase Cloud Music, wrapped by `musicbox-service/` on top of
[darknessomi/musicbox](https://github.com/darknessomi/musicbox) (PyPI: `NetEase-MusicBox`).
Everything served online comes from **the single account you sign in with by QR code** —
there is no login-free third-party resolution path, and please do not add one.

Key proxy modules:

| Module | Responsibility |
| :--- | :--- |
| `proxy/app.py` | FastAPI proxy, all intercepted endpoints |
| `proxy/netease_auth.py` | Login-state probe + TTL cache, degradation gate, push triggers |
| `proxy/netease_items.py` | NetEase `song_info` → unified item mapping (shared by search and daily recommend) |
| `proxy/pushplus.py` | PushPlus client with throttling and redaction |
| `proxy/recommend.py` | Fetch/cache NetEase's own daily recommendation playlist |
| `proxy/env_merge.py` | Incremental safe `.env` merge (preserve user values, drop obsolete keys) |

When touching online playback, keep these invariants in mind:

- A track with no real stream URL, or with `freeTrialInfo` / `freeTrialPrivilege`, must never be
  treated as playable.
- When not logged in the catalog degrades to free tracks only (`FNMUSIC_FREE_ONLY_ON_LOGOUT`);
  setting it to `false` means no online playback at all until the user scans the QR code.
- Daily recommendation is **not injected when logged out** — an empty or filler playlist is worse
  than no playlist.
- Push failures must never break playback: `pushplus.send()` swallows every exception.
