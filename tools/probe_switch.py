# -*- coding: utf-8 -*-
"""端到端验证：切换对话（不发送）。"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuerelay_app import automation, winapi  # noqa: E402


def main() -> None:
    winapi.set_dpi_awareness()
    info = winapi.find_target_window(["chatgpt", "codex"])
    if info is None:
        raise SystemExit("找不到 Codex 窗口")
    hwnd = info["hwnd"]

    settings = {
        "target_keywords": "chatgpt,codex",
        "step_delay_seconds": 0.8,
        "recognition_enabled": True,
        "show_overlay": False,
        "strict_conversation": False,
    }

    def log(level: str, message: str) -> None:
        print(f"[{level}] {message}")

    runner = automation.Runner(settings, log, exclude_hwnd=0, emit=None)
    runner._activate(info)
    time.sleep(0.5)

    frame = runner._frame(hwnd)
    composer, source = runner._locator().composer(frame)
    print("composer", composer, source)
    print("sidebar_right", runner._sidebar_right(frame, composer))

    rows = runner._list_rows(frame, composer)
    print(f"--- 列表识别出 {len(rows)} 行 ---")
    for row in rows:
        print(f"  {row.left:5d},{row.top:5d} {row.width:4d}x{row.height:3d}  {row.text}")

    print("--- 当前对话标题 ---")
    for line in runner._chat_title_lines(frame):
        print("  ", line)

    title = sys.argv[1] if len(sys.argv) > 1 else "20261028 28"
    print(f"--- 切换对话：{title} ---")
    started = time.time()
    runner._switch_conversation(hwnd, title)
    print("耗时", round(time.time() - started, 2), "秒")

    frame = runner._frame(hwnd)
    print("切换后的标题：", [line.text for line in runner._chat_title_lines(frame)])


if __name__ == "__main__":
    main()
