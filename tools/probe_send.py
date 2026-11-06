# -*- coding: utf-8 -*-
"""端到端验证：真的发一条消息到指定对话。

用法：python tools/probe_send.py "Clarify say" "消息内容"
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuerelay_app import automation, winapi  # noqa: E402


def main() -> None:
    winapi.set_dpi_awareness()
    title = sys.argv[1] if len(sys.argv) > 1 else "Clarify say"
    content = sys.argv[2] if len(sys.argv) > 2 else "端到端测试：CueRelay 识别链路 OK"

    settings = {
        "target_keywords": "chatgpt,codex",
        "target_title": "",
        "target_process": "",
        "step_delay_seconds": 0.8,
        "recognition_enabled": True,
        "verify_paste": True,
        "verify_send": True,
        "strict_conversation": True,
        "show_overlay": False,
        "restore_target_geometry": False,
    }

    def log(level: str, message: str) -> None:
        print(f"[{level}] {message}")

    runner = automation.Runner(settings, log, exclude_hwnd=0, emit=None)
    task = {
        "id": "probe",
        "name": "端到端测试",
        "content": content,
        "action": "click",
        "conversation_title": title,
    }
    started = time.time()
    outcome = runner.run_task(task)
    print("结果", outcome.ok, outcome.message, "耗时", round(time.time() - started, 2), "秒")


if __name__ == "__main__":
    main()
