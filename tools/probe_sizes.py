# -*- coding: utf-8 -*-
"""在不同窗口尺寸下跑一遍识别链路。

Codex 的窗口可以是任意大小，所以"某个尺寸下能认出来"不算数。这个探针把
窗口依次摆成几种大小，每次重新识别输入框、放大镜和搜索面板，把结果打成
一张表。哪一档开始失效，一眼能看出来。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuerelay_app import automation, locator, vision, winapi  # noqa: E402

SIZES = ("1200x800", "1600x1000", "1920x1200", "2560x1440", "max")


def main() -> int:
    winapi.set_dpi_awareness()
    settings = {
        "target_keywords": "codex, chatgpt, openai",
        "target_title": "ChatGPT",
        "target_process": "ChatGPT.exe",
        "recognition_enabled": True,
        "strict_conversation": False,
        "show_overlay": False,
        "step_delay_seconds": 0.8,
    }
    logs: list[str] = []

    def log(level: str, message: str) -> None:
        logs.append(f"[{level}] {message}")

    runner = automation.Runner(settings, log, emit=None)
    window = runner._locate_target()
    if window is None:
        print("没有找到目标窗口")
        return 1
    hwnd = int(window["hwnd"])
    print(f"窗口 {hwnd}：{window.get('title')}")
    print(f"{'尺寸':<12}{'实际':<14}{'输入框':<26}{'放大镜':<14}{'侧边栏':<10}{'面板'}")

    for spec in SIZES:
        ok = winapi.force_window_size(hwnd, spec)
        winapi.ensure_foreground(hwnd)
        # 上一档结束时面板可能还开着。面板一开整个背景就被压暗，放大镜那一行
        # 的对比度跟着掉，`detect_panels` 认不出侧边栏——量出来会是"没找到"，
        # 但那是探针自己没收拾干净，不是识别的问题。
        winapi.press_escape()
        time.sleep(0.8)
        rect = winapi.get_window_rect(hwnd) or {}
        actual = f"{rect.get('width')}x{rect.get('height')}"
        try:
            frame = locator.grab(hwnd)
        except Exception:
            frame = None
        if frame is None:
            print(f"{spec:<12}{actual:<14}{'—':<26}{'—':<14}{'—':<10}窗口拿不到画面")
            continue
        composer, source = locator.Locator(settings, log).composer(frame)
        magnifier = runner._magnifier_point(frame)
        sidebar = runner.sidebar_state(frame)
        panel = "—"
        try:
            winapi.press_escape()
            time.sleep(0.3)
            fresh, box = runner._open_search(hwnd, runner._frame(hwnd))
            panel = f"{int(box.width)}x{int(box.height)} @{int(box.left)},{int(box.top)}" if box else "没打开"
            if box:
                winapi.press_escape()
                time.sleep(0.3)
        except automation.AutomationError as exc:
            panel = f"失败：{exc}"
        box_text = f"{composer.width}x{composer.height}({source})" if composer else "没认出"
        print(f"{spec:<12}{actual:<14}{box_text:<26}{str(magnifier):<14}{sidebar:<10}{panel}")
        time.sleep(0.2)

    print("")
    for line in logs[-6:]:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
