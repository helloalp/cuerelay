# -*- coding: utf-8 -*-
"""验证「对话」页的能力：扫描侧边栏，可选切换到一个标题。

用法：
    python tools/probe_chats.py                 只扫描
    python tools/probe_chats.py "标题"          扫描并切过去
    python tools/probe_chats.py --new           点一次「New chat」

不发送任何消息。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuerelay_app import winapi  # noqa: E402
from cuerelay_app.toolbox import Toolbox, ToolboxError  # noqa: E402


def main(argv: list[str]) -> int:
    winapi.set_dpi_awareness()
    settings = {
        "target_keywords": "chatgpt,codex",
        "step_delay_seconds": 0.8,
        "recognition_enabled": True,
        "show_overlay": False,
        "strict_conversation": False,
    }

    def log(level: str, message: str) -> None:
        print(f"[{level}] {message}")

    box = Toolbox(settings, log)

    started = time.time()
    try:
        items = box.conversations()
    except ToolboxError as exc:
        print(f"扫描失败：{exc}")
        return 1
    print(f"扫描到 {len(items)} 个对话，用时 {time.time() - started:.1f}s")
    for item in items:
        print(f"  [{item.section:8s}] {item.title}")

    if "--new" in argv:
        try:
            print(box.new_conversation())
        except ToolboxError as exc:
            print(f"新建失败：{exc}")
            return 1
        return 0

    target = next((arg for arg in argv if not arg.startswith("-")), "")
    if target:
        started = time.time()
        try:
            print(box.open_conversation(target))
        except ToolboxError as exc:
            print(f"切换失败：{exc}")
            return 1
        print(f"切换用时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
