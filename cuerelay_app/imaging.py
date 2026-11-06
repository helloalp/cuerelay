# -*- coding: utf-8 -*-
"""截图数据容器与预览图生成。

Bitmap 只做最基础的像素读取；所有识别算法都在 vision.py 中，
这样可以单独对纯数据结构做测试。
"""

from __future__ import annotations

import struct
import time
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


def _blank(bitmap: Bitmap) -> bool:
    """整张图是不是一个颜色（全黑/全白）。

    PrintWindow 对少数窗口会"成功"但画出全黑，这种情况要当成失败，
    否则后面 OCR 不到东西，报错会指向识别算法而不是截图。
    """

    if bitmap.width < 4 or bitmap.height < 4:
        return True
    first = bitmap.data[0:4]
    step = max(4, (bitmap.width * bitmap.height // 512)) * 4
    for index in range(0, len(bitmap.data) - 4, step):
        if bitmap.data[index : index + 4] != first:
            return False
    return True


def _render(hwnd: int, rect: dict[str, int]) -> Optional[Bitmap]:
    """让窗口把自己画到离屏位图上；画出来是纯色就当作没拿到。"""

    try:
        data = winapi.capture_window_bgra(hwnd, rect["width"], rect["height"])
    except Exception:
        return None
    if not data:
        return None
    bitmap = Bitmap(rect["width"], rect["height"], data)
    return None if _blank(bitmap) else bitmap


def capture_client(hwnd: int, allow_raise: bool = True) -> Optional[tuple[Bitmap, dict[str, int]]]:
    """抓取窗口客户区，返回位图与客户区的屏幕矩形。

    优先让窗口把自己画到离屏位图上（PrintWindow）：被别的窗口挡住也能拿到
    它自己的画面。这条路对 Chromium 系的窗口有个前提——它得在出帧。被完全
    遮住时浏览器会停掉合成，PrintWindow 照样"成功"，但画出来是全黑。

    比全黑更糟的是"画出别人"：窗口被完全遮住时，PrintWindow 有时会直接返回
    挡着它的那个程序的像素。看上去一切正常——有字、有图、能 OCR——但认出来
    的是别人的界面。所以在拿画面之前先问一句"这个窗口真的露在屏幕上吗"，
    露得不够就先把它提上来；提不上来就明确失败，绝不拿别人的画面去点击。
    """

    rect = winapi.get_client_rect(hwnd)
    if not rect or rect["width"] <= 0 or rect["height"] <= 0:
        return None
    exposed = winapi.exposed_ratio(hwnd)
    bitmap = _render(hwnd, rect)
    if bitmap is not None and exposed >= 0.5:
        return bitmap, rect

    center = (rect["left"] + rect["width"] // 2, rect["top"] + rect["height"] // 2)
    on_top = winapi.point_to_root_window(*center) == int(hwnd)
    if allow_raise and exposed < 0.5 and not on_top:
        # 看不见它，它也画不出自己。提上来（不动焦点）再试一次。
        if winapi.raise_window(hwnd):
            time.sleep(0.3)
            exposed = winapi.exposed_ratio(hwnd)
            fresh = _render(hwnd, rect)
            if fresh is not None:
                bitmap = fresh
            on_top = winapi.point_to_root_window(*center) == int(hwnd)
    if exposed < 0.5:
        # 还是被挡着。这时候 PrintWindow 给的画面不可信，截屏截到的更是别人，
        # 两条路都不能用——交给调用方去说清楚"目标窗口被挡住了"。
        return None
    if bitmap is not None:
        return bitmap, rect
    if not on_top:
        # 目标窗口不在这个位置的最上层，截屏只会截到挡着它的那个程序。
        return None
    bitmap = capture_screen(rect["left"], rect["top"], rect["width"], rect["height"])
    if bitmap is None:
        return None
    return bitmap, rect
