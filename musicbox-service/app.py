"""HTTP wrapper for https://github.com/darknessomi/musicbox (NetEase-MusicBox CLI)."""
from __future__ import annotations

import io
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Path, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from netease_ext import (
    auth_detail as ne_auth_detail,
    batch_song_details,
    filter_playable_song_ids,
    song_lyric_pair,
)
import runner
from runner import MusicboxTimeoutError, ensure_xdg_dirs

ensure_xdg_dirs()

SEARCH_TYPES = {"song", "album", "artist", "playlist"}
QUALITY_WHITELIST = {"exhigh", "higher", "standard", "lossless", "hires", "jymaster"}

# musicbox CLI 退出码（NEMbox/cli.py）：3 表示未登录
CLI_EXIT_NOT_LOGGED_IN = 3


class UpstreamException(Exception):
    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = (stderr or "")[:2000]


app = FastAPI(title="fnmusic-musicbox", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": exc.errors()})


@app.exception_handler(UpstreamException)
async def upstream_exception_handler(request, exc: UpstreamException):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": "upstream_error", "exit_code": exc.exit_code, "stderr": exc.stderr},
    )


@app.exception_handler(MusicboxTimeoutError)
async def timeout_exception_handler(request, exc: MusicboxTimeoutError):
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content={"detail": "Upstream musicbox command timed out"},
    )


def exec_musicbox(args: list[str], timeout: float = 30.0) -> Any:
    code, stdout, stderr = runner.run_musicbox(args, timeout=timeout)
    if code != 0:
        raise UpstreamException(exit_code=code, stderr=stderr or stdout or "")
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamException(exit_code=code, stderr=stderr or stdout or "") from exc


def _extract_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("ok") is True and "data" in payload:
        return payload["data"]
    return payload


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_ids(ids_str: str | None) -> list[int]:
    if not ids_str or not ids_str.strip():
        raise HTTPException(status_code=422, detail="ids parameter is required")
    ids: list[int] = []
    for token in ids_str.split(","):
        token = token.strip()
        if not token:
            raise HTTPException(status_code=422, detail="Empty id in ids list")
        try:
            val = int(token)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid id {token!r}") from None
        if val <= 0:
            raise HTTPException(status_code=422, detail=f"Invalid id {token!r}")
        ids.append(val)
    if not 1 <= len(ids) <= 100:
        raise HTTPException(status_code=422, detail="ids count must be 1..100")
    return ids


@app.get("/healthz")
def healthz():
    return {"status": "ok", "source": "https://github.com/darknessomi/musicbox"}


@app.get("/api/v1/search")
def search(
    keyword: str = Query(...),
    type: str = Query("song"),
    limit: int = Query(20, ge=1, le=100),
):
    if not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword cannot be empty")
    if type not in SEARCH_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type {type!r}")
    res = exec_musicbox(["search", keyword, "--type", type, "--limit", str(limit), "--json"])
    if type == "song" and isinstance(res, dict):
        raw_list = res.get("data")
        if isinstance(raw_list, list):

            def _song_id(item: dict) -> int:
                # 畸形数据（非数字 id）一律归零，零不可能命中 playable 集合
                try:
                    return int(item.get("song_id") or item.get("id") or 0)
                except (ValueError, TypeError):
                    return 0

            song_ids = [sid for it in raw_list if isinstance(it, dict) for sid in [_song_id(it)] if sid]
            if song_ids:
                playable = filter_playable_song_ids(song_ids)
                res["data"] = [it for it in raw_list if isinstance(it, dict) and _song_id(it) in playable]
            else:
                res["data"] = []
    return res


@app.get("/api/v1/song/{song_id}/url")
def song_url(song_id: int = Path(..., ge=1), quality: str = Query("exhigh")):
    if quality not in QUALITY_WHITELIST:
        raise HTTPException(status_code=400, detail=f"Invalid quality {quality!r}")
    return exec_musicbox(["song", "url", str(song_id), "--quality", quality, "--json"])


@app.get("/api/v1/song/{song_id}/info")
def song_info(song_id: int = Path(..., ge=1)):
    return exec_musicbox(["song", "info", str(song_id), "--json"])


@app.get("/api/v1/songs/detail")
def songs_detail(ids: str = Query(None)):
    parsed = _parse_ids(ids)
    try:
        return {"ok": True, "data": batch_song_details(parsed)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v1/song/{song_id}/lyric")
def song_lyric(song_id: int = Path(..., ge=1)):
    try:
        return {"ok": True, "data": song_lyric_pair(song_id)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/v1/artist/{artist_id}")
def artist(artist_id: int = Path(..., ge=1), limit: int = Query(20, ge=1, le=100)):
    return exec_musicbox(["artist", str(artist_id), "--limit", str(limit), "--json"])


@app.get("/api/v1/album/{album_id}")
def album(album_id: int = Path(..., ge=1)):
    return exec_musicbox(["album", str(album_id), "--json"])


@app.get("/api/v1/playlist/{playlist_id}")
def playlist(playlist_id: int = Path(..., ge=1)):
    return exec_musicbox(["playlist", "show", str(playlist_id), "--json"])


@app.get("/api/v1/auth/status")
def auth_status():
    return exec_musicbox(["auth", "status", "--json"])


@app.get("/api/v1/auth/detail")
def auth_detail_endpoint():
    """登录态详情（含 VIP 类型与到期时间），供代理层做降级门控与 PushPlus 提醒。

    走 NEMbox 已缓存的账号信息，不额外请求网易云；任何异常都降级为"未登录"，
    绝不让探测失败拖垮音源服务。
    """
    try:
        detail = ne_auth_detail()
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "data": {"logged_in": False, "error": str(exc)[:200]}}
    return {"ok": True, "data": detail}


@app.get("/api/v1/recommend/daily")
def recommend_daily(limit: int = Query(20, ge=1, le=100)):
    """网易云官方「每日推荐」歌曲（需登录扫码的私人账号）。

    直接复用 musicbox CLI 的 `recommend songs` 子命令，它内部走
    /weapi/v3/discovery/recommend/songs 并把结果归一化成与 /api/v1/search
    完全一致的 song_info 结构（song_id / song_name / artist / album_name /
    mp3_url / duration / quality），因此代理层无需额外字段映射。

    返回：
      - 未登录 → HTTP 200 + {"ok": false, "error": "not_logged_in"}
      - 成功   → HTTP 200 + {"ok": true, "data": [ ...song_info... ]}
    """
    try:
        code, stdout, stderr = runner.run_musicbox(
            ["recommend", "songs", "--limit", str(limit), "--json"], timeout=40.0
        )
    except MusicboxTimeoutError as exc:
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"ok": False, "error": "timeout", "detail": str(exc)[:200]},
        )

    # musicbox CLI 退出码约定：3 = 未登录（EXIT_NOT_LOGGED_IN）
    if code == CLI_EXIT_NOT_LOGGED_IN:
        return {"ok": False, "error": "not_logged_in", "data": []}
    if code != 0:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"ok": False, "error": "upstream_error", "exit_code": code,
                     "detail": (stderr or stdout or "")[:500]},
        )
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"ok": False, "error": "bad_upstream_json", "detail": str(exc)[:200]},
        )

    data = _extract_payload(payload)
    songs = [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
    # 每日推荐里可能混入当前账号无权播放的曲目，按真实直链再过一遍
    ids: list[int] = []
    for s in songs:
        try:
            sid = int(s.get("song_id") or s.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid:
            ids.append(sid)
    if ids:
        playable = filter_playable_song_ids(ids)
        songs = [
            s
            for s in songs
            if _safe_int(s.get("song_id") or s.get("id")) in playable
        ]
    return {"ok": True, "data": songs[:limit]}


@app.post("/api/v1/auth/login")
def auth_login():
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    unikey = ""
    if isinstance(payload, dict):
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if unikey and isinstance(payload, dict):
        payload["qr_url"] = f"https://music.163.com/login?codekey={unikey}"
    return data


@app.get("/api/v1/auth/login/check")
def auth_login_check(unikey: str = Query(...)):
    if not unikey.strip():
        raise HTTPException(status_code=400, detail="unikey cannot be empty")
    return exec_musicbox(["auth", "login", "--check", unikey, "--json"])


@app.get("/api/v1/auth/login/qr.png")
@app.get("/api/v1/auth/qr.png")
def auth_login_qr():
    try:
        import qrcode
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="qrcode extra not installed") from exc
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    unikey = ""
    if isinstance(payload, dict):
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if not unikey:
        raise UpstreamException(0, "Missing unikey in auth login response")
    qr_url = f"https://music.163.com/login?codekey={unikey}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/api/v1/auth/login/qr", response_class=Response)
@app.get("/api/v1/auth/qr", response_class=Response)
def auth_login_qr_text():
    data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
    payload = _extract_payload(data)
    qr_ascii = ""
    unikey = ""
    if isinstance(payload, dict):
        qr_ascii = str(payload.get("qr_ascii") or "")
        unikey = str(payload.get("unikey") or payload.get("codekey") or "")
    if not qr_ascii:
        if not unikey:
            raise UpstreamException(0, "Missing unikey or qr_ascii in auth login response")
        qr_url = f"https://music.163.com/login?codekey={unikey}"
        try:
            import qrcode

            qr = qrcode.QRCode()
            qr.add_data(qr_url)
            qr.make(fit=True)
            f = io.StringIO()
            qr.print_ascii(out=f)
            qr_ascii = f.getvalue()
        except ImportError as exc:
            raise HTTPException(status_code=501, detail="qrcode extra not installed") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to render QR ascii: {exc}") from exc
    if not qr_ascii.endswith("\n"):
        qr_ascii += "\n"
    return Response(content=qr_ascii, media_type="text/plain; charset=utf-8")
