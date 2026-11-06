# -*- coding: utf-8 -*-
"""把 CueRelay 主窗口截图存成 PNG，用来检查首屏信息层级。

用法：
    python tools/shot_app.py out.png [--wait 2.5]

只截图，不点击、不输入。窗口不存在时报告并退出。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cuerelay_app import imaging, winapi


def find_app_window() -> int:
    for info in winapi.enumerate_windows():
        if "CueRelay" in (info.get("title") or ""):
            return int(info["hwnd"])
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python tools/shot_app.py out.png [--wait 秒]")
        return 2
    out = Path(argv[0])
    wait = 2.5
    if "--wait" in argv:
        wait = float(argv[argv.index("--wait") + 1])

    winapi.set_dpi_awareness()
    deadline = time.time() + 8.0
    hwnd = 0
    while time.time() < deadline:
        hwnd = find_app_window()
        if hwnd:
            break
        time.sleep(0.3)
    if not hwnd:
        print("没找到 CueRelay 窗口")
        return 1

    winapi.activate_window(hwnd)
    time.sleep(max(0.6, wait))

    captured = imaging.capture_client(hwnd)
    if captured is None:
        print("截图失败")
        return 1
    bitmap, rect = captured
    # max_width 要给得足够大，否则 to_png 会一直缩到 1/8。
    data, width, height = bitmap.to_png(max_width=9999)
    out.write_bytes(data)
    print(f"已保存 {out}  ({width}x{height}) 客户区 {rect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
