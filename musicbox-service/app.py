"""HTTP wrapper for https://github.com/darknessomi/musicbox (NetEase-MusicBox CLI)."""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Path, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from netease_ext import (
    auth_detail as ne_auth_detail,
    batch_song_details,
    check_is_logged_in as ne_check_is_logged_in,
    daily_songs as ne_daily_songs,
    filter_playable_song_ids,
    invalidate_login_cache,
    reset_api_instance,
    search_songs as ne_search_songs,
    song_lyric_pair,
    song_raw_detail,
    song_url_info,
)
import runner
from runner import MusicboxTimeoutError, ensure_xdg_dirs

ensure_xdg_dirs()

logger = logging.getLogger("fnmusic_musicbox")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

SEARCH_TYPES = {"song", "album", "artist", "playlist"}
QUALITY_WHITELIST = {"exhigh", "higher", "standard", "lossless", "hires", "jymaster"}

# musicbox CLI 退出码（NEMbox/cli.py）：3 表示未登录
CLI_EXIT_NOT_LOGGED_IN = 3


class UpstreamException(Exception):
    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = (stderr or "")[:2000]


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    """服务生命周期：启动时预热 CLI。

    放在 lifespan 而不是模块级调用——否则**导入本模块**（例如跑单元测试）就会
    spawn ``musicbox --version`` 子进程，既拖慢测试又会污染进程内的探测缓存。
    预热只付一次冷启动代价（实测 47s），且在后台线程里跑，不阻塞服务就绪。
    """
    _warm_cli_probe_in_background()
    yield


app = FastAPI(title="fnmusic-musicbox", version="1.0.0", lifespan=_lifespan)


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


# ---------------------------------------------------------------------------
# CLI 探测结果缓存 + 启动预热
#
# `musicbox` CLI 首次执行要先完成 deviceId 生成等初始化，实测**冷启动 47.37s**、
# 稳态 1.4s。管理页面探测 /api/v1/selftest 的超时是 10s，selftest 内部又给 CLI
# 留了 15s —— 冷启动必然双双超时，诊断页整个 CLI 区块显示 undefined / []，
# 把用户唯一顺手可用的排障工具变成一片空白。
#
# 而从 2.1.6 起播放热路径不再 spawn CLI（改进程内直取），CLI 从此没有任何被
# 顺带预热的机会，上面这个现象会从「偶发」变成「常态」。因此在启动时用后台线程
# 把这份一次性代价提前付掉，并缓存探测结果供 selftest 立即复用。
# ---------------------------------------------------------------------------

_CLI_PROBE: dict[str, Any] | None = None
_CLI_PROBE_LOCK = threading.Lock()
_CLI_WARMUP_STARTED = False
# 预热代号：每轮预热带一个自增代号，重置时代号作废。
# 预热线程可能在很久之后（实测冷启动 47s）才回来写缓存，若期间发生过重置
# （测试里是 fixture，生产中是重新预热），那次写入必须被丢弃，否则会写进
# 一个已经不属于它的轮次 —— 表现为测试随机串味、生产里报告过期的探测结果。
_CLI_PROBE_GEN = 0


def _cli_probe_generation() -> int:
    with _CLI_PROBE_LOCK:
        return _CLI_PROBE_GEN


def _cache_cli_probe(ok: bool, detail: str, gen: int | None = None) -> None:
    global _CLI_PROBE
    with _CLI_PROBE_LOCK:
        if gen is not None and gen != _CLI_PROBE_GEN:
            return                      # 已被作废的轮次，丢弃
        if _CLI_PROBE is None:
            _CLI_PROBE = {"ok": ok, "detail": detail}


def reset_cli_probe_for_test() -> None:
    """测试钩子：清空 CLI 探测缓存与预热标记，并作废在途预热线程的写入。

    必须在**前后**都调用：测试里 ``TestClient(app)`` 的 startup 会拉起预热线程，
    它可能在下一个用例执行期间才回来写缓存，只靠「用例开始时清一次」挡不住。
    """
    global _CLI_PROBE, _CLI_WARMUP_STARTED, _CLI_PROBE_GEN
    with _CLI_PROBE_LOCK:
        _CLI_PROBE = None
        _CLI_WARMUP_STARTED = False
        _CLI_PROBE_GEN += 1


def probe_cli_exec(timeout_s: float = 3.0) -> tuple[bool, str]:
    """跑一次 ``musicbox --version`` 验证 CLI 真的可执行；结果缓存后复用。

    只缓存**确定性**结果（成功、或非超时类的硬失败）。超时不缓存：它多半意味着
    CLI 还在冷启动，后台预热线程随后会拿到真实结果，届时不该被一次 3s 的
    快速探测永久钉死成「超时」。
    """
    with _CLI_PROBE_LOCK:
        if _CLI_PROBE is not None:
            return bool(_CLI_PROBE["ok"]), str(_CLI_PROBE["detail"])
    gen = _cli_probe_generation()
    try:
        code, stdout, stderr = runner.run_musicbox(["--version"], timeout=timeout_s)
        ok = code == 0
        detail = ((stdout or stderr or "").strip()[:200]) or f"exit={code}"
        _cache_cli_probe(ok, detail, gen)
        return ok, detail
    except MusicboxTimeoutError as exc:
        # 带上异常类型名：超时是这里最可能的故障形态，日志必须可 grep
        return False, (
            f"{type(exc).__name__}: CLI 冷启动 timeout，未能在 {timeout_s}s 内完成"
            f"（首次执行需生成 deviceId，实测可达 ~47s）；后台预热中，稍后重试即可"
        )
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
        _cache_cli_probe(ok, detail, gen)
        return ok, detail


def _warm_cli_probe_in_background() -> None:
    """启动时预热 CLI，把 47s 级的一次性代价挪到后台，不阻塞服务就绪。

    进程内**幂等**：只起一个预热线程。否则每次 startup 都拉起一个跑
    ``musicbox --version`` 的后台线程，既浪费 CPU/IO（CLI 稳态也要 1.4s），
    又会在测试里每个 TestClient 都 spawn 一次子进程。
    """
    global _CLI_WARMUP_STARTED
    with _CLI_PROBE_LOCK:
        if _CLI_WARMUP_STARTED:
            return
        _CLI_WARMUP_STARTED = True

    gen = _cli_probe_generation()

    def _run():
        try:
            with _CLI_PROBE_LOCK:
                if _CLI_PROBE is not None:
                    return
            # 给足冷启动余量：实测 47s，留到 120s 覆盖 NAS 上更慢的磁盘/CPU
            code, stdout, stderr = runner.run_musicbox(["--version"], timeout=120.0)
            ok = code == 0
            detail = ((stdout or stderr or "").strip()[:200]) or f"exit={code}"
            _cache_cli_probe(ok, detail, gen)
            logger.info("CLI 预热完成 cli_exec_ok=%s detail=%s", ok, detail[:120])
        except Exception as exc:  # noqa: BLE001 - 预热失败不影响服务本身
            logger.warning("CLI 预热失败（不影响播放，播放已不走 CLI）: %s: %s",
                           type(exc).__name__, exc)

    threading.Thread(target=_run, name="cli-warmup", daemon=True).start()


@app.get("/api/v1/selftest")
def selftest():
    """自检：报告 musicbox CLI 是怎么解析到的、能不能真的跑起来。

    这个端点存在的理由是一个真实事故：服务用绝对路径的 venv uvicorn 启动时，
    venv 的 bin/ 不在 PATH 上，裸命令名 `musicbox` 解析不到，导致 12 个走 CLI
    的端点全部 502，而 /healthz 依然返回 200 —— 看起来"服务是好的"。
    """
    cmd, how = runner.musicbox_cmd()
    result: dict[str, Any] = {
        "cli_found": bool(cmd),
        "cli_cmd": cmd,
        "resolved_by": how,
        "interpreter": sys.executable,
        "venv_bin_dir": runner.bin_dir(),
        "python_version": sys.version.split()[0],
        "xdg": {
            "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME", ""),
            "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", ""),
            "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
        },
        "running_as": os.environ.get("USER") or "?",
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "nembox_importable": False,
        "cli_exec_ok": False,
        "cli_exec_detail": "",
    }
    try:
        import NEMbox  # noqa: F401

        result["nembox_importable"] = True
    except Exception as exc:  # noqa: BLE001
        result["nembox_importable"] = False
        result["nembox_error"] = f"{type(exc).__name__}: {exc}"[:200]

    if cmd:
        # 用带缓存的短超时探测：命中缓存时立即返回，冷启动时也不会把响应拖到
        # 管理页面 10s 超时之外（那会导致整个 CLI 区块显示 undefined）。
        ok, detail = probe_cli_exec(timeout_s=3.0)
        result["cli_exec_ok"] = ok
        result["cli_exec_detail"] = detail
    return {"ok": True, "data": result}


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

    # 歌曲搜索走进程内实现：CLI 的 dig_info 在任意一首取不到直链时会 return []，
    # 把整个结果集清空（HTTP 仍 200）——用户表现就是"搜不到任何在线歌曲"。
    # 这里逐首过滤，坏数据只影响它自己那一首。
    if type == "song":
        try:
            rows = ne_search_songs(keyword, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("in-process search failed, falling back to CLI: %s", exc)
            rows = None
        if rows is not None:
            return {"ok": True, "data": rows, "engine": "in-process"}
        # 进程内失败才回退 CLI（至少不比原来差）
        res = exec_musicbox(["search", keyword, "--type", type, "--limit", str(limit), "--json"])
        if isinstance(res, dict):
            res["engine"] = "cli-fallback"
        return res

    return exec_musicbox(["search", keyword, "--type", type, "--limit", str(limit), "--json"])


@app.get("/api/v1/song/{song_id}/url")
def song_url(song_id: int = Path(..., ge=1), quality: str = Query("exhigh")):
    """取播放直链。**播放热路径**：飞牛每首歌各调一次。

    优先走进程内 NEMbox（实测 0.05s）。原先这里 exec CLI，冷启动实测 **47s**、
    稳态 1.4s，而代理侧超时只有 10s ⇒ 必然超时，且 ``httpx.ReadTimeout('')`` 的
    ``str()`` 是空串，日志里只剩一行看不出原因的 warning，用户侧表现为
    「搜索结果出来了但一直缓冲」。CLI 现仅作进程内失败时的兜底。
    """
    if quality not in QUALITY_WHITELIST:
        raise HTTPException(status_code=400, detail=f"Invalid quality {quality!r}")
    info = {}
    reached_upstream = False
    try:
        info = song_url_info(song_id, quality)
        # 只要拿到结构化的上游应答（哪怕 code=404 / url 为空）就视为可信权威结果：
        # 那表示「这首歌确实取不到直链」，再跑一次慢 CLI 不会有不同答案，反而在
        # proxy 依次试 lossless→exhigh 时把每首不可播的曲目变成两次子进程调用。
        reached_upstream = isinstance(info, dict) and ("code" in info or "id" in info)
    except Exception as exc:  # noqa: BLE001
        logger.warning("in-process song_url_info failed for %s(q=%s): %s: %s",
                       song_id, quality, type(exc).__name__, exc)
    if reached_upstream:
        return {"ok": True, "data": info, "engine": "in-process"}
    # 进程内连上游都没问到（异常/空响应）才兜底走 CLI，两者共享同一份 cookie 文件
    return exec_musicbox(["song", "url", str(song_id), "--quality", quality, "--json"])


@app.get("/api/v1/song/{song_id}/info")
def song_info(song_id: int = Path(..., ge=1)):
    """取单曲原始详情（ar/al/dt/sq/hr/h）。同样是播放热路径，逻辑同上。"""
    raw = {}
    reached_upstream = False
    try:
        raw = song_raw_detail(song_id)
        reached_upstream = isinstance(raw, dict) and bool(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("in-process song_raw_detail failed for %s: %s: %s",
                       song_id, type(exc).__name__, exc)
    if reached_upstream:
        return {"ok": True, "data": raw, "engine": "in-process"}
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

    **进程内实现，不走 ``musicbox recommend songs`` CLI。**
    CLI 内部调 ``dig_info``，而 dig_info 在任意一首歌取不到直链时会 ``return []``，
    把整份日推清空（HTTP 仍 200），用户表现为"飞牛里根本不出现每日推荐歌单"。
    这里逐首判定：只有真正拿不到直链的那几首被剔除。

    返回：
      - 未登录 → HTTP 200 + {"ok": false, "error": "not_logged_in"}
      - 成功   → HTTP 200 + {"ok": true, "data": [...], "engine": "..."}
    """
    # 先判登录：未登录时上游 v3 接口会返回一份与账号画像无关的热门填充，
    # 名不副实，宁可不给。
    try:
        logged_in = ne_check_is_logged_in()
    except Exception as exc:  # noqa: BLE001
        logger.warning("login check failed for daily rec: %s", exc)
        logged_in = False
    if not logged_in:
        return {"ok": False, "error": "not_logged_in", "data": []}

    rows: list[dict] | None = None
    try:
        rows = ne_daily_songs(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("in-process daily rec failed, will fall back to CLI: %s", exc)
        rows = None

    if rows:
        return {"ok": True, "data": rows[:limit], "engine": "in-process"}

    # 进程内拿到空结果或异常 → 回退 CLI（至少不比原来差），并如实标注来源
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
    ids = [i for i in (_safe_int(s.get("song_id") or s.get("id")) for s in songs) if i]
    if ids:
        playable = filter_playable_song_ids(ids)
        songs = [s for s in songs if _safe_int(s.get("song_id") or s.get("id")) in playable]

    if not songs:
        logger.info(
            "daily rec empty: 进程内=%s 条, CLI 回退=%s 条（上游 dig_info 可能已清空结果）",
            0 if rows is None else len(rows or []), len(songs),
        )
        return {"ok": True, "data": [], "engine": "cli-fallback",
                "note": "上游返回空列表；可能是网络波动或该账号今日无日推"}

    return {"ok": True, "data": songs[:limit], "engine": "cli-fallback"}


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
    data = exec_musicbox(["auth", "login", "--check", unikey, "--json"])
    # 登录成功（803）后必须重建进程内的 NetEase 实例：NEMbox 只在 __init__ 里
    # 读一次 cookie 文件，而登录是由 CLI 子进程写盘的，父进程那个长命单例仍握着
    # 登录前的旧 cookie。只清登录态缓存不够——可播性过滤用的 songs_url 仍会
    # 带着旧 cookie 发请求，VIP/付费曲目全部拿不到直链而被剔除，
    # 表现为"扫码登录成功了，但搜索和每日推荐还是空的"。
    payload = _extract_payload(data)
    code = payload.get("code") if isinstance(payload, dict) else None
    if code == 803 or code == "803":
        reset_api_instance()
        logger.info("扫码登录成功，已重建 NEMbox 实例并清空登录态缓存")
    return data


@app.get("/api/v1/auth/login/qr.png")
@app.get("/api/v1/auth/qr.png")
def auth_login_qr(unikey: str = Query("")):
    """二维码 PNG。

    不传 unikey 时新发起一次登录并返回该次的码；传 unikey 时**渲染已存在的码**，
    这样网页端可以先拿 unikey 开始轮询，再取图，两者不会错位（否则会各生成一张
    不同的码，扫了也登不上轮询的那个 unikey）。
    """
    try:
        import qrcode
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="qrcode extra not installed") from exc

    unikey = (unikey or "").strip()
    if not unikey:
        data = exec_musicbox(["auth", "login", "--no-wait", "--json"])
        payload = _extract_payload(data)
        if isinstance(payload, dict):
            unikey = str(payload.get("unikey") or payload.get("codekey") or "")
        if not unikey:
            raise UpstreamException(0, "Missing unikey in auth login response")

    qr_url = f"https://music.163.com/login?codekey={quote(unikey)}"
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
