# -*- coding: utf-8 -*-
"""把识别结果画在目标窗口的截图上，用来肉眼确认框的位置对不对。

用法：
    python tools/probe_boxes.py [输出.png] [--hwnd 句柄]

不点击、不输入、不发送，只截图和识别。发送按钮是否真的被点中，光看坐标
看不出来——上一版就是把麦克风当成了发送按钮，画出来一眼就能发现。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuerelay_app import config, locator, ocr, winapi
from cuerelay_app.store import DEFAULT_SETTINGS
from cuerelay_app.vision import Rect


def find_target() -> int:
    keywords = list(config.TARGET_DEFAULT_KEYWORDS)
    found = winapi.find_target_window(keywords, "", "")
    return int(found["hwnd"]) if found else 0


def draw_rect(bitmap, rect: Rect, color: tuple[int, int, int], thickness: int = 3) -> None:
    """在位图上画空心矩形。Bitmap.data 是自上而下的 BGRA。"""

    data = bitmap.data
    if not isinstance(data, bytearray):
        data = bytearray(data)
    pixel = bytes((color[2], color[1], color[0], 255))
    width, height = bitmap.width, bitmap.height

    def paint(x: int, y: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 4
            data[offset : offset + 4] = pixel

    left, right = max(0, rect.left), min(width - 1, rect.right)
    top, bottom = max(0, rect.top), min(height - 1, rect.bottom)
    for i in range(thickness):
        for x in range(left, right + 1):
            paint(x, top + i)
            paint(x, bottom - i)
        for y in range(top, bottom + 1):
            paint(left + i, y)
            paint(right - i, y)
    bitmap.data = bytes(data)


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv and not argv[0].startswith("--") else Path("boxes.png")
    winapi.set_dpi_awareness()

    hwnd = 0
    if "--hwnd" in argv:
        hwnd = int(argv[argv.index("--hwnd") + 1])
    if not hwnd:
        hwnd = find_target()
    if not hwnd:
        print("没找到目标窗口")
        return 1

    print(f"窗口 {hwnd}: {winapi.get_window_title(hwnd)}")
    print(f"OCR 可用: {ocr.available()}")

    winapi.activate_window(hwnd)
    time.sleep(0.8)

    frame = locator.grab(hwnd)
    if frame is None:
        print("截图失败")
        return 1
    print(f"客户区 {frame.width}x{frame.height}")

    settings = dict(DEFAULT_SETTINGS)
    engine = locator.Locator(settings, lambda level, message: print(f"  [{level}] {message}"))

    started = time.time()
    composer, source = engine.composer(frame)
    print(f"输入框: {composer}  来源 {source}  ({(time.time() - started) * 1000:.0f}ms)")
    if composer is None:
        print("识别不到输入框")
        return 1

    slot, kind = engine.send_slot(frame, composer)
    print(f"发送方块: {slot}  图标: {kind}")
    point = slot.center if slot is not None else None
    print(f"发送点: {point}")

    empty = engine.composer_is_empty(frame)
    print(f"输入框为空: {empty}")

    draw_rect(frame.bitmap, composer, (46, 120, 220), 3)
    if point is not None:
        # 用一个 44x44 的方框圈出发送点，方便看它落在哪个图标上
        half = 22
        draw_rect(
            frame.bitmap,
            Rect(point[0] - half, point[1] - half, point[0] + half, point[1] + half),
            (220, 60, 46),
            3,
        )
    data, width, height = frame.bitmap.to_png(max_width=9999)
    out.write_bytes(data)
    print(f"已保存 {out} ({width}x{height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
