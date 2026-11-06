# -*- coding: utf-8 -*-
"""把浮层真的画出来看一眼：框住放大镜、搜索框、搜索结果，再截屏核对。

只画、不点。用来确认"可视化识别过程"这件事在真实窗口上成立，
而不是只在代码里成立。

用法：python tools/probe_overlay.py
"""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuerelay_app import apps, automation, imaging, locator, ocr, winapi  # noqa: E402
from cuerelay_app.ui.overlay import Overlay  # noqa: E402
from cuerelay_app.ui.theme import Theme  # noqa: E402

OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    winapi.set_dpi_awareness()
    hwnd = int(apps.codex_app({}).window()["hwnd"])
    if not winapi.ensure_foreground(hwnd):
        print("切不到前台。")
        return 1
    time.sleep(0.5)
    frame = locator.grab(hwnd)
    if frame is None:
        print("读不到画面。")
        return 1
    print(f"客户区 {frame.width}x{frame.height}")

    runner = automation.Runner(
        {"step_delay_seconds": 0.6, "recognition_enabled": True, "show_overlay": True},
        lambda level, message: print(f"  [{level}] {message}"),
    )

    root = tk.Tk()
    root.withdraw()
    theme = Theme(mode="dark", dpi=winapi.get_system_dpi())
    overlay = Overlay(root, theme)

    steps = []
    point = runner._magnifier_point(frame)
    if point is None:
        print("没找到放大镜。")
        return 1
    steps.append(("放大镜", frame.to_screen(automation.Rect(point[0] - 14, point[1] - 14, point[0] + 14, point[1] + 14))))

    if "--search" in sys.argv[1:]:
        query = "20261021 18"
        fresh, box = runner._open_search(hwnd, frame)
        if box is not None and fresh is not None:
            winapi.click(fresh.point_to_screen(box.center))
            time.sleep(0.3)
            winapi.ctrl_a()
            winapi.set_clipboard_text(query)
            winapi.ctrl_v()
            time.sleep(1.4)
        frame = locator.grab(hwnd)

    fresh, box = runner._search_input(hwnd)
    if box is not None:
        steps.append(("搜索框", fresh.to_screen(box)))
        lines = ocr.recognize(fresh.bitmap)
        column = runner._panel_column(lines, fresh)
        chat = runner._chat_section(lines, column) if column is not None else None
        hit = None
        if chat is not None:
            top, bottom = runner._section_bounds(lines, chat, column)
            hit = runner._best_row(runner._section_rows(lines, top, bottom, column), "20261021 18")
        if hit is not None:
            steps.append(
                (
                    f"搜索结果 · {runner._result_label(hit.text, '20261021 18')}",
                    fresh.to_screen(
                        automation.Rect(hit.left - 8, hit.top - 8, hit.right + 8, hit.bottom + 8)
                    ),
                )
            )
    else:
        print("搜索面板没开，只画放大镜。")

    overlay.show(dict(frame.rect))
    for index, (label, rect) in enumerate(steps, start=1):
        overlay.progress(index, len(steps), label)
        overlay.box(rect.as_dict(), "search", label)
        root.update()
        time.sleep(0.6)
        shot = imaging.capture_screen(
            frame.rect["left"], frame.rect["top"], frame.rect["width"], frame.rect["height"]
        )
        if shot is not None:
            png, _, _ = shot.crop(1250, 600, min(shot.width, 2650), min(shot.height, 1450)).to_png(max_width=1400)
            with open(os.path.join(OUT, f".overlay_{index}.png"), "wb") as stream:
                stream.write(png)
            print(f"  {label} → .overlay_{index}.png")

    overlay.hide()
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
