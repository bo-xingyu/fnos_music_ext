"""本地每日推荐歌单封面（现生成 PNG）回归测试。

历史坑：早期实现对本地每日推荐封面直接返回 404（本地文件没有封面 URL），真机上
确实出现过个别客户端因歌单封面 404 而整条不渲染——日志一行 404，界面则是
「歌单凭空消失」。封面必须是**永远拿得到的真图**，这条测试钉死它。
"""

from __future__ import annotations

import struct
import zlib

import pytest

try:  # 作为包导入（从仓库根跑 pytest）
    from proxy import app
except ImportError:  # 扁平导入（从 proxy/ 目录跑）
    import app  # type: ignore


def _parse_png(data: bytes) -> tuple[int, int, int, bytes]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "必须是合法 PNG 签名"
    pos = 8
    size = ctype = idat = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            w, h, depth, color = struct.unpack(">IIBB", body[:10])
            size = (w, h)
            ctype = color
        elif tag == b"IDAT":
            idat = (idat or b"") + body
        pos += 12 + length
    assert size is not None and idat is not None
    return size[0], size[1], ctype or 0, zlib.decompress(idat)


@pytest.mark.parametrize("px", [160, 300])
def test_cover_png_is_valid_image(px):
    w, h, color, raw = _parse_png(app._local_daily_cover_png(px))
    assert (w, h) == (px, px)
    assert color == 2  # truecolor RGB
    # 每行 = 1 字节 filter + w*3 字节像素
    assert len(raw) == h * (1 + w * 3)


def test_cover_png_is_cached_by_size():
    a = app._local_daily_cover_png(200)
    b = app._local_daily_cover_png(200)
    assert a is b  # 同尺寸必须命中内存缓存，列表页几十次回源不能重复画


def test_cover_png_rejects_absurd_sizes():
    """超界尺寸收敛而不是画一张天文数字的图把内存吃光。"""
    w, h, _c, _raw = _parse_png(app._local_daily_cover_png(999999))
    assert 96 <= w <= 512
    assert w == h
