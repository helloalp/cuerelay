# -*- coding: utf-8 -*-
"""看清侧边栏：现在是展开还是收起，收起时那一竖条里的图标在哪。

只读。用 PrintWindow 让窗口自己把画面画出来，不移动鼠标、不切换前台，
所以跑它不会抢焦点、也不会让别的窗口滚起来。

用法：
    python tools/probe_sidebar.py            看状态 + 左上角墨迹分布
    python tools/probe_sidebar.py --expand   顺手调一次 ensure_sidebar
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuerelay_app import apps, automation, locator, vision, winapi  # noqa: E402
from cuerelay_app.vision import Rect  # noqa: E402

COLUMNS = 13   # 每格 20 像素，覆盖 0~260
CELL = 20
ROWS = 22      # 覆盖 0~440，够看到侧边栏顶部的几个图标


def ink_map(frame: locator.Frame) -> None:
    """把左上角切成 20 像素的格子，用字符表示每格里有多少"不是背景色"的像素。"""

    print("     " + "".join(f"{x:>3d}" for x in range(0, COLUMNS * CELL, CELL)))
    for row in range(ROWS):
        top = row * CELL
        if top >= frame.height:
            break
        line = []
        for col in range(COLUMNS):
            left = col * CELL
            if left >= frame.width:
                break
            rect = Rect(left + 2, top + 2, left + CELL - 2, top + CELL - 2)
            ratio = vision.ink_ratio(frame.bitmap, rect, step=1)
            if ratio < 0.02:
                line.append("  .")
            elif ratio < 0.10:
                line.append("  -")
            elif ratio < 0.30:
                line.append("  +")
            elif ratio < 0.60:
                line.append("  *")
            else:
                line.append("  #")
        print(f"{top:>4d}" + "".join(line))


def main(argv: list[str]) -> int:
    winapi.set_dpi_awareness()
    spec = apps.codex_app({})
    window = spec.window()
    if window is None:
        print("Codex / ChatGPT 没在运行。")
        return 1
    hwnd = int(window.get("hwnd") or 0)
    print(f"窗口：{window.get('title')!r} hwnd={hwnd}")

    frame = locator.grab(hwnd)
    if frame is None:
        print("读不到画面。")
        return 1
    print(f"客户区：{frame.width}x{frame.height}")

    def log(level: str, message: str) -> None:
        print(f"  [{level}] {message}")

    runner = automation.Runner(
        {"step_delay_seconds": 0.8, "recognition_enabled": True, "show_overlay": False},
        log,
    )
    state = runner.sidebar_state(frame)
    print(f"侧边栏：{state}（{automation.Runner.SIDEBAR_BAND} 这一条竖带的墨迹）")
    ink_map(frame)

    if "--expand" in argv:
        print("ensure_sidebar ->", runner.ensure_sidebar(hwnd))
        frame = locator.grab(hwnd)
        if frame is not None:
            print("之后：", runner.sidebar_state(frame))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
