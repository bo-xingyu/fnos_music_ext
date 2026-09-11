#!/usr/bin/env python3
"""生成 fnmusic-ext 的应用图标（ICON.PNG 64x64 / ICON_256.PNG 256x256）。

官方要求（developer.fnnas.com/docs/core-concepts/icon/）：
  - PNG 或 JPG、sRGB、单文件 <= 1024 KB
  - 完整正方形画布，视觉主体为**圆角矩形**风格（不要直角满铺）
  - 64px 下仍清晰可辨

设计：深靛蓝→品红的对角渐变圆角方块，中央一枚白色音符（符头 + 符干 + 旗），
下方一条随音符起伏的声波曲线，暗示「在线音源接入」。两个尺寸都由同一套
矢量参数按比例绘制，保证 64px 下的笔画不会糊成一团（线宽按尺寸缩放）。

用法：
    python3 fpk/tools/make_icons.py [--out fpk] [--sizes 64,256]
"""
from __future__ import annotations

import argparse
import math
import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - 依赖缺失时给出可操作的提示
    sys.stderr.write(
        "缺少 Pillow。请先安装：python3 -m pip install pillow\n"
    )
    raise SystemExit(1)

# 圆角矩形底色渐变（深靛蓝 -> 品红），与飞牛深色桌面协调
TOP_LEFT = (37, 32, 92)        # #25205C
BOTTOM_RIGHT = (168, 45, 120)  # #A82D78
FOREGROUND = (255, 255, 255)
FOREGROUND_DIM = (255, 255, 255, 165)

SUPERSAMPLE = 8


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def gradient_square(size: int) -> Image.Image:
    """对角线性渐变底色，返回未加圆角的 RGB 图。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    denom = max(1, (size - 1) * 2)
    for y in range(size):
        for x in range(size):
            t = (x + y) / denom
            px[x, y] = (
                lerp(TOP_LEFT[0], BOTTOM_RIGHT[0], t),
                lerp(TOP_LEFT[1], BOTTOM_RIGHT[1], t),
                lerp(TOP_LEFT[2], BOTTOM_RIGHT[2], t),
            )
    return img


def rounded_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    radius = int(round(size * radius_ratio))
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def draw_note(draw: ImageDraw.ImageDraw, s: float) -> None:
    """白色八分音符。s = 目标尺寸；所有坐标按 s 归一，笔画随尺寸等比缩放。"""
    # 符头：椭圆，略微倾斜的观感靠椭圆长宽比实现
    head_cx, head_cy = 0.375 * s, 0.615 * s
    head_rx, head_ry = 0.135 * s, 0.100 * s
    draw.ellipse(
        [head_cx - head_rx, head_cy - head_ry, head_cx + head_rx, head_cy + head_ry],
        fill=FOREGROUND,
    )

    # 符干：从符头右缘向上
    stem_w = max(1.0, 0.052 * s)
    stem_x = head_cx + head_rx - stem_w * 0.55
    stem_top = 0.235 * s
    draw.rectangle(
        [stem_x - stem_w / 2, stem_top, stem_x + stem_w / 2, head_cy + head_ry * 0.35],
        fill=FOREGROUND,
    )

    # 旗：符干顶部向右下方的曲线尾旗，用多边形逼近
    flag = [
        (stem_x, stem_top),
        (stem_x + 0.20 * s, stem_top + 0.045 * s),
        (stem_x + 0.235 * s, stem_top + 0.155 * s),
        (stem_x + 0.155 * s, stem_top + 0.095 * s),
        (stem_x + 0.075 * s, stem_top + 0.075 * s),
        (stem_x, stem_top + 0.075 * s),
    ]
    draw.polygon(flag, fill=FOREGROUND)


def draw_wave(draw: ImageDraw.ImageDraw, s: float) -> None:
    """底部声波曲线，暗示在线音源接入。"""
    width = max(1.0, 0.030 * s)
    x0, x1 = 0.20 * s, 0.80 * s
    base_y = 0.815 * s
    amp = 0.045 * s
    pts = []
    steps = 96
    for i in range(steps + 1):
        t = i / steps
        x = x0 + (x1 - x0) * t
        # 两端收敛为 0，中间起伏
        envelope = math.sin(math.pi * t)
        pts.append((x, base_y - amp * envelope * math.sin(t * math.pi * 3)))
    draw.line(pts, fill=FOREGROUND_DIM, width=int(round(width)), joint="curve")


def render(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    base = gradient_square(big).convert("RGBA")
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    draw_note(draw, float(big))
    draw_wave(draw, float(big))

    base.alpha_composite(layer)
    # 圆角遮罩 + 抗锯齿下采样
    base.putalpha(rounded_mask(big))
    return base.resize((size, size), Image.LANCZOS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 fnmusic-ext 应用图标")
    parser.add_argument("--out", default=None, help="输出目录（默认 fpk/，即脚本上级目录）")
    parser.add_argument("--sizes", default="64,256", help="逗号分隔的边长，默认 64,256")
    args = parser.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out or os.path.dirname(here)
    os.makedirs(out_dir, exist_ok=True)

    names = {64: "ICON.PNG", 256: "ICON_256.PNG"}
    for raw in args.sizes.split(","):
        size = int(raw.strip())
        if size <= 0:
            raise SystemExit(f"非法尺寸: {raw!r}")
        img = render(size)
        name = names.get(size, f"ICON_{size}.PNG")
        path = os.path.join(out_dir, name)
        img.save(path, format="PNG", optimize=True)
        kb = os.path.getsize(path) / 1024
        print(f"生成 {path}  {img.size[0]}x{img.size[1]}  {kb:.1f} KB")
        if kb > 1024:
            raise SystemExit(f"{path} 超过官方 1024 KB 限制（{kb:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
