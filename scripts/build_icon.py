#!/usr/bin/env python3
"""用 Pillow/ImageDraw 直接绘制 NewAPI 图标并生成多尺寸 ICO。"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48, 64, 128, 256)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def draw_icon(size: int) -> Image.Image:
    scale = 4
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def p(value: float) -> int:
        return round(value * scale)

    draw.rounded_rectangle(
        (p(6), p(6), p(size - 6), p(size - 6)),
        radius=p(max(4, size * 0.20)),
        fill="#102746",
        outline="#2e68b5",
        width=max(1, p(size * 0.018)),
    )

    cx = size * 0.50
    shield = [
        (p(cx), p(size * 0.13)),
        (p(size * 0.23), p(size * 0.25)),
        (p(size * 0.23), p(size * 0.49)),
        (p(size * 0.29), p(size * 0.66)),
        (p(cx), p(size * 0.82)),
        (p(size * 0.71), p(size * 0.66)),
        (p(size * 0.77), p(size * 0.49)),
        (p(size * 0.77), p(size * 0.25)),
    ]
    draw.polygon(shield, fill="#347edc")
    draw.line(
        [(p(size * 0.33), p(size * 0.47)), (p(size * 0.45), p(size * 0.59)), (p(size * 0.68), p(size * 0.34))],
        fill="#ffffff",
        width=max(1, p(size * 0.09)),
        joint="curve",
    )

    # 时钟在右下角，尺寸太小时保留一个高对比圆点和指针。
    clock_r = size * 0.17
    clock_cx, clock_cy = size * 0.73, size * 0.72
    draw.ellipse(
        (p(clock_cx - clock_r), p(clock_cy - clock_r), p(clock_cx + clock_r), p(clock_cy + clock_r)),
        fill="#0c1e3a",
        outline="#9acfff",
        width=max(1, p(size * 0.035)),
    )
    draw.line(
        [(p(clock_cx), p(clock_cy)), (p(clock_cx), p(clock_cy - clock_r * 0.55))],
        fill="#ffffff",
        width=max(1, p(size * 0.035)),
    )
    draw.line(
        [(p(clock_cx), p(clock_cy)), (p(clock_cx + clock_r * 0.43), p(clock_cy + clock_r * 0.26))],
        fill="#ffffff",
        width=max(1, p(size * 0.035)),
    )
    draw.ellipse(
        (p(clock_cx - size * 0.025), p(clock_cy - size * 0.025), p(clock_cx + size * 0.025), p(clock_cy + size * 0.025)),
        fill="#ffffff",
    )

    image = image.resize((size, size), Image.Resampling.LANCZOS)
    return image


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    icons = [draw_icon(size) for size in SIZES]
    # 以最大图作为 ICO 主图，并显式写入全部尺寸，避免 Windows 只读到单一缩放。
    icons[-1].save(output, format="ICO", sizes=[(size, size) for size in SIZES])
    # Pillow 会从最大图生成各尺寸；再次打开确认 ICO 中确实包含全部目录项。
    with Image.open(output) as check:
        available = set(check.info.get("sizes", []))
        if available and not {(size, size) for size in SIZES}.issubset(available):
            raise RuntimeError(f"ICO 尺寸不完整: {available}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("assets/newapi_checkin.ico"))
    args = parser.parse_args()
    output = build(args.output)
    print(f"generated {output} sizes={','.join(map(str, SIZES))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
