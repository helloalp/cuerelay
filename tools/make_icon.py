# -*- coding: utf-8 -*-
"""生成 assets/cuerelay.ico。

图标只用纯标准库绘制：先按超采样计算覆盖度，再把像素写成 PNG，
最后打包成 Vista 之后支持的 PNG-ICO。改动图形后重新运行本脚本即可。

用法：python tools/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
ICON_PATH = ASSET_DIR / "cuerelay.ico"

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPERSAMPLE = 4

ACCENT = (44, 95, 224)
WHITE = (255, 255, 255)


def _round_box_distance(x: float, y: float, half: float, radius: float) -> float:
    """点到圆角矩形边界的距离，负数表示在内部。"""

    dx = abs(x) - (half - radius)
    dy = abs(y) - (half - radius)
    outside = (max(dx, 0.0) ** 2 + max(dy, 0.0) ** 2) ** 0.5
    inside = min(max(dx, dy), 0.0)
    return outside + inside - radius


def _segment_distance(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = x - ax, y - ay
    length_sq = vx * vx + vy * vy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    px, py = ax + vx * t, ay + vy * t
    return ((x - px) ** 2 + (y - py) ** 2) ** 0.5


def _sample(x: float, y: float) -> tuple[float, tuple[int, int, int]]:
    """返回 (不透明度, 颜色)，坐标范围 -1..1。"""

    coverage = 0
    glyph = 0
    for row in range(SUPERSAMPLE):
        for column in range(SUPERSAMPLE):
            px = x + (column + 0.5) / SUPERSAMPLE / 8 - 0.5 / 8
            py = y + (row + 0.5) / SUPERSAMPLE / 8 - 0.5 / 8
            if _round_box_distance(px, py, 0.94, 0.30) > 0:
                continue
            coverage += 1
            if _in_glyph(px, py):
                glyph += 1
    if coverage == 0:
        return 0.0, ACCENT
    total = SUPERSAMPLE * SUPERSAMPLE
    ratio = glyph / coverage
    color = tuple(round(ACCENT[i] + (WHITE[i] - ACCENT[i]) * ratio) for i in range(3))
    return coverage / total, color


def _in_glyph(x: float, y: float) -> bool:
    stroke = 0.115
    # 折线 “>”
    if _segment_distance(x, y, -0.46, -0.40, -0.06, 0.0) - stroke <= 0:
        return True
    if _segment_distance(x, y, -0.06, 0.0, -0.46, 0.40) - stroke <= 0:
        return True
    # 下划线
    if _segment_distance(x, y, 0.10, 0.40, 0.50, 0.40) - stroke <= 0:
        return True
    return False


def _render(size: int) -> bytes:
    rows = []
    for row in range(size):
        line = bytearray()
        for column in range(size):
            x = (column + 0.5) / size * 2 - 1
            y = (row + 0.5) / size * 2 - 1
            alpha, color = _sample(x, y)
            line += bytes(
                (
                    color[0],
                    color[1],
                    color[2],
                    max(0, min(255, round(alpha * 255))),
                )
            )
        rows.append(line)

    raw = bytearray()
    stride = size * 4
    for line in rows:
        raw.append(0)
        raw += line
    return bytes(raw)


def _png(size: int) -> bytes:
    raw = _render(size)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    images = [(size, _png(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        payload += data
        offset += len(data)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    ICON_PATH.write_bytes(header + bytes(entries) + bytes(payload))
    print(f"已生成 {ICON_PATH}（{len(SIZES)} 个尺寸，{ICON_PATH.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
