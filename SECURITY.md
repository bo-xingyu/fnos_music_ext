# Security

This project sits in front of fnOS Music (`trim.music`). Treat it as production infrastructure.

## Secrets

Two kinds of credential exist in this project. Both live only in `.env` (`chmod 600`)
or in the app's own data directory, and neither may ever be logged, echoed, cached,
or asserted on in tests.

| Credential | Where it lives | Notes |
| :--- | :--- | :--- |
| PushPlus token (`FNMUSIC_PUSHPLUS_TOKEN`) | `.env` (chmod 600); in the fpk build, `$TRIM_PKGVAR/app/.env` | Read as `type=password` in the wizard and with `read -r -s` in `install.sh`. Masked to first-4-chars in any log line. `proxy/pushplus.py` also redacts the token from server responses before logging, since a remote error message could echo it. |
| NetEase login cookie | `musicbox-data/` (XDG dirs) | Written by `musicbox`'s own `auth login`. Never read, forwarded, or logged by this project. Persist the directory or the user has to re-scan on every restart. |

`.env` is git-ignored, and `build_fpk.sh` aborts the build if any `.env`, `*.pem`,
or `id_rsa*` is found anywhere in the package staging tree.

Leaked keys must be rotated at the provider. If a PushPlus token leaked, reset it at
pushplus.plus. If a NetEase session leaked, log out of that device from the NetEase app.
Do not paste either into issues.

## Privileges

The proxy must run as root: it creates a bind socket under `/var/run`, which requires
write access to that **directory** (not just to the socket file). Everything that can be
de-privileged is: in the fpk build the NetEase source service runs as the dedicated package
user (`$TRIM_USERNAME`) via `runuser`, falling back to `su`, and warning loudly rather than
silently running as root when neither works. See `fpk/README.md`.

In the git-clone install path, `install.sh` runs the source service in a Docker container
as a non-root `appuser`, or under a systemd unit in host mode.

## What this project must not do

- Do not modify fnOS nginx configs (the system rewrites them).
- Do not patch `trim-music` binaries or write to the official `music.db`.
- Do not disable the fail-safe: if the proxy dies, official music must still work after
  `restore.sh` or automatic socket reclaim. **Socket restoration is the highest-risk
  operation in this codebase** — never delete `/var/run/trim_music.socket` unless its
  identity has been positively determined to be this proxy. When identity is unknown,
  leave it alone.
- Do not add a login-free third-party source resolution path. Every online track must come
  from the account the user signed into by QR code.
- Do not treat a track as playable based on search-result metadata alone. Playability must
  be verified against a real stream URL (`filter_playable_song_ids`); anything that only
  yields a trial snippet (`freeTrialInfo` / `freeTrialPrivilege`) is never playable.

## Failure isolation

- A source-service outage must degrade to local-library-only results, never to a 500 or a
  hung search.
- `pushplus.send()` swallows every exception by design: a notification failure must never
  break playback.
- The daily-recommendation playlist is **not** injected when logged out. An empty or
  filler playlist is worse than no playlist.

## Reporting

Open a GitHub issue describing the impact and reproduction **without** secrets or personal
library paths.
