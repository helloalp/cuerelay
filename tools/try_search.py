# -*- coding: utf-8 -*-
"""现场跑一遍放大镜链路：放大镜 → 输入框 → 关键词 → 结果 → 点开 → 标题变了。

用法：
    python tools/try_search.py "20261021 18"

只做"切过去"这一件事，不发送任何提示词。每一步都留一张截图，
失败时可以直接看画面，不用猜。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuerelay_app import apps, automation, locator, ocr, vision, winapi  # noqa: E402

OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = (1300, 660, 2620, 1400)


def shot(frame, name: str, box=PANEL) -> None:
    left, top, right, bottom = box
    crop = frame.bitmap.crop(left, top, min(frame.width, right), min(frame.height, bottom))
    png, _, _ = crop.to_png(max_width=1400)
    with open(os.path.join(OUT, f".try_{name}.png"), "wb") as stream:
        stream.write(png)
    print(f"   截图 .try_{name}.png")


def panel_text(frame) -> None:
    for line in ocr.recognize(frame.bitmap):
        if PANEL[1] < line.top < PANEL[3] and PANEL[0] < line.left < PANEL[2]:
            print(f"     {line.top:>5d},{line.left:>5d} {line.width:>4d}x{line.height:<4d} {line.text!r}")


def main(argv: list[str]) -> int:
    title = argv[0] if argv else "20261021 18"
    winapi.set_dpi_awareness()
    hwnd = int(apps.codex_app({}).window()["hwnd"])
    if not winapi.ensure_foreground(hwnd):
        print("切不到前台")
        return 1

    def log(level: str, message: str) -> None:
        print(f"  [{level}] {message}")

    runner = automation.Runner(
        {"step_delay_seconds": 0.8, "recognition_enabled": True, "show_overlay": False},
        log,
    )

    started = time.monotonic()
    frame = locator.grab(hwnd)
    print(f"客户区 {frame.width}x{frame.height}  侧边栏 {runner.sidebar_state(frame)}")

    fresh, box = runner._open_search(hwnd, frame)
    print(f"1 放大镜 → 输入框 {box}")
    if box is None or fresh is None:
        shot(frame, "fail_open")
        return 1
    shot(fresh, "open")

    winapi.click(fresh.point_to_screen(box.center))
    time.sleep(0.4)
    winapi.ctrl_a()
    winapi.set_clipboard_text(title)
    winapi.ctrl_v()
    time.sleep(1.4)
    after = locator.grab(hwnd)
    shot(after, "typed")
    print("2 已输入，面板里现在有：")
    panel_text(after)

    frame, hit = runner._search_result(hwnd, title, box)
    print(f"3 结果 {hit}")
    if hit is None:
        shot(frame, "fail_result")
        winapi.press_escape()
        return 1

    winapi.click(frame.point_to_screen(hit.center))
    time.sleep(1.3)
    ok = runner._verify_conversation(hwnd, title, 8.0)
    print(f"4 验证 {ok}   总用时 {time.monotonic() - started:.1f}s")
    if not ok:
        shot(locator.grab(hwnd), "fail_title", (0, 0, 2600, 260))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
