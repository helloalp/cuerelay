# -*- coding: utf-8 -*-
"""在不同窗口尺寸下真的切一遍对话，验证搜索链路。

只认结果：切完读标题栏，对上了才算过。顺带把日志收下来，好看出这一次走的
是哪条路（放大镜 / Ctrl+K / 几何兜底）——"能跑通"和"走的是正路"是两件事。

用法：
    python tools/probe_switch.py                     # 默认两个对话，全尺寸
    python tools/probe_switch.py --sizes 1200x800    # 只测一档
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuerelay_app import automation, winapi  # noqa: E402

SETTINGS = {
    "target_keywords": "chatgpt,codex",
    "step_delay_seconds": 0.8,
    "recognition_enabled": True,
    "show_overlay": False,
    "strict_conversation": False,
}

SIZES = ("1200x800", "1600x1000", "1920x1200", "2560x1440", "max")
TARGETS = ("PDF 渲染与 DOCX 处理", "Clarify Say")


def main() -> int:
    winapi.set_dpi_awareness()
    sizes = SIZES
    if "--sizes" in sys.argv:
        sizes = tuple(sys.argv[sys.argv.index("--sizes") + 1].split(","))

    logs: list[str] = []
    runner = automation.Runner(SETTINGS, lambda level, msg: logs.append(f"[{level}] {msg}"), None)
    window = runner._locate_target()
    if window is None:
        print("没有找到目标窗口")
        return 1
    hwnd = int(window["hwnd"])
    print(f"窗口 {hwnd}：{window.get('title')}")
    print(f"{'尺寸':<12}{'实际':<12}{'目标':<24}{'结果':<10}{'耗时':<7}路线")
    failures = 0

    for spec in sizes:
        winapi.force_window_size(hwnd, spec)
        winapi.ensure_foreground(hwnd)
        time.sleep(1.0)
        rect = winapi.get_window_rect(hwnd) or {}
        actual = f"{rect.get('width')}x{rect.get('height')}"
        for title in TARGETS:
            logs.clear()
            started = time.time()
            try:
                runner.open_conversation(hwnd, title)
                ok = runner._verify_conversation(hwnd, title, 4.0)
                note = "成功" if ok else "标题没变"
            except automation.AutomationError as exc:
                ok = False
                note = str(exc)[:16]
            spent = time.time() - started
            if not ok:
                failures += 1
            route = "放大镜"
            for line in logs:
                if "Ctrl+K" in line:
                    route = "Ctrl+K"
                elif "本来就开着" in line:
                    route = "已开着"
            print(f"{spec:<12}{actual:<12}{title:<24}{note:<10}{spent:>5.1f}s  {route}")
        time.sleep(0.3)

    print(f"\n共 {len(sizes) * len(TARGETS)} 次，失败 {failures} 次")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
