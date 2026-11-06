# -*- coding: utf-8 -*-
"""截图数据容器与预览图生成。

Bitmap 只做最基础的像素读取；所有识别算法都在 vision.py 中，
这样可以单独对纯数据结构做测试。
"""

from __future__ import annotations

import struct
import zlib
from typing import Optional

from . import winapi


class Bitmap:
    """自上而下、每像素 4 字节（BGRA）的位图。"""

    __slots__ = ("width", "height", "data")

    def __init__(self, width: int, height: int, data: bytes) -> None:
        self.width = int(width)
        self.height = int(height)
        self.data = data

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Bitmap({self.width}x{self.height})"

    def rgb(self, x: int, y: int) -> tuple[int, int, int]:
        index = (y * self.width + x) * 4
        data = self.data
        return (data[index + 2], data[index + 1], data[index])

    def lum(self, x: int, y: int) -> int:
        index = (y * self.width + x) * 4
        data = self.data
        return (data[index + 2] * 299 + data[index + 1] * 587 + data[index] * 114) // 1000

    def crop(self, left: int, top: int, right: int, bottom: int) -> "Bitmap":
        left = max(0, min(self.width, int(left)))
        right = max(left + 1, min(self.width, int(right)))
        top = max(0, min(self.height, int(top)))
        bottom = max(top + 1, min(self.height, int(bottom)))
        width = right - left
        height = bottom - top
        chunks = []
        for row in range(top, bottom):
            start = (row * self.width + left) * 4
            chunks.append(self.data[start : start + width * 4])
        return Bitmap(width, height, b"".join(chunks))

    def to_png(self, max_width: int = 760, max_height: int = 0) -> tuple[bytes, int, int]:
        """转成 PNG，Tk 8.6 可以直接显示，不需要 Pillow。

        按整数倍缩小，缩放时取左上角像素，避免引入插值带来的模糊。
        """

        scale = 1
        while scale < 8:
            candidate_w = self.width // scale
            candidate_h = self.height // scale
            if candidate_w <= max_width and (max_height <= 0 or candidate_h <= max_height):
                break
            scale += 1
        out_width = max(1, self.width // scale)
        out_height = max(1, self.height // scale)

        stride = 4 * scale
        raw = bytearray()
        for out_y in range(out_height):
            raw.append(0)  # 每行的过滤器类型：None
            start = (out_y * scale) * self.width * 4
            row = self.data[start : start + self.width * 4]
            red = row[2::stride][:out_width].ljust(out_width, b"\x00")
            green = row[1::stride][:out_width].ljust(out_width, b"\x00")
            blue = row[0::stride][:out_width].ljust(out_width, b"\x00")
            line = bytearray(out_width * 3)
            line[0::3] = red
            line[1::3] = green
            line[2::3] = blue
            raw += line

        def chunk(tag: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + tag
                + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", out_width, out_height, 8, 2, 0, 0, 0)
        data = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )
        return data, out_width, out_height


def capture_screen(left: int, top: int, width: int, height: int) -> Optional[Bitmap]:
    try:
        data = winapi.capture_screen_bgra(left, top, width, height)
    except Exception:
        return None
    return Bitmap(width, height, data)


def capture_client(hwnd: int) -> Optional[tuple[Bitmap, dict[str, int]]]:
    """抓取窗口客户区，返回位图与客户区的屏幕矩形。"""

    rect = winapi.get_client_rect(hwnd)
    if not rect:
        return None
    bitmap = capture_screen(rect["left"], rect["top"], rect["width"], rect["height"])
    if bitmap is None:
        return None
    return bitmap, rect
