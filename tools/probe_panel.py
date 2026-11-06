# -*- coding: utf-8 -*-
"""把搜索面板的原始识别结果摊开看。

识别链路出问题时，看"算出来的框"没用——得看 OCR 到底读到了什么、在哪。
这个探针打开搜索面板，把客户区里每一条识别结果按行打印出来（带坐标），
再把面板区域截成 PNG，好和坐标对上。改完几何算法就拿它复核。

用法：
    python tools/probe_panel.py 1600x1000
    python tools/probe_panel.py max --keep      # 不按 Esc，面板留着看
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuerelay_app import automation, locator, ocr, winapi  # noqa: E402

SETTINGS = {
    "target_keywords": "codex, chatgpt, openai",
    "target_title": "ChatGPT",
    "target_process": "ChatGPT.exe",
    "recognition_enabled": True,
    "strict_conversation": False,
    "show_overlay": False,
    "step_delay_seconds": 0.8,
}


def dump(frame, runner: automation.Runner) -> None:
    lines = ocr.recognize(frame.bitmap)
    print(f"识别到 {len(lines)} 行，客户区 {frame.width}x{frame.height}")
    print(f"{'top':>5}{'bottom':>7}{'left':>6}{'right':>7}  text")
    for line in sorted(lines, key=lambda item: (item.top, item.left)):
        print(
            f"{line.top:>5}{line.bottom:>7}{line.left:>6}{line.right:>7}  {line.text[:70]}"
        )

    row = runner._filter_row(lines, frame)
    print("\n筛选标签行：", [(line.text, line.left, line.right) for line in row])
    print("面板左边缘：", runner._panel_column(lines, frame))
    print("面板是否打开：", runner._panel_column(lines, frame) is not None)
    _, box = runner._search_input(frame.hwnd)
    print("输入框：", box)


def main() -> int:
    spec = sys.argv[1] if len(sys.argv) > 1 else ""
    keep = "--keep" in sys.argv
    winapi.set_dpi_awareness()
    logs: list[str] = []
    runner = automation.Runner(SETTINGS, lambda level, msg: logs.append(f"[{level}] {msg}"), None)
    window = runner._locate_target()
    if window is None:
        print("没有找到目标窗口")
        return 1
    hwnd = int(window["hwnd"])
    print(f"窗口 {hwnd}：{window.get('title')}")
    if spec:
        winapi.force_window_size(hwnd, spec)
    winapi.ensure_foreground(hwnd)
    time.sleep(0.8)
    rect = winapi.get_window_rect(hwnd) or {}
    print(f"实际尺寸 {rect.get('width')}x{rect.get('height')}")

    winapi.press_escape()
    time.sleep(0.3)
    if "--raw" in sys.argv:
        for attempt in range(3):
            winapi.ctrl_k()
            time.sleep(0.9)
            frame = runner._frame(hwnd)
            if runner._filter_row(ocr.recognize(frame.bitmap), frame):
                print(f"按了 Ctrl+K（第 {attempt + 1} 次），面板开了")
                break
        else:
            print("按了三次 Ctrl+K，面板还是没开")
    else:
        frame = runner._frame(hwnd)
        _, box = runner._open_search(hwnd, frame)
        print(f"_open_search 返回 {box}")
        time.sleep(0.4)
    frame = runner._frame(hwnd)
    dump(frame, runner)

    out = Path(__file__).resolve().parent.parent / "cuerelay_panel.png"
    top = max(0, int(frame.height * 0.02))
    bottom = min(frame.height, int(frame.height * 0.55))
    png, width, height = frame.bitmap.crop(0, top, frame.width, bottom).to_png(1400)
    out.write_bytes(png)
    print(f"\n面板截图（y {top}~{bottom}）已存到 {out}  {width}x{height}")

    if not keep:
        winapi.press_escape()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
