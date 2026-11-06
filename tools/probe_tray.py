# -*- coding: utf-8 -*-
"""托盘冒烟测试：起一个真托盘图标，停 3 秒，再撤掉。

用法：
    python tools/probe_tray.py [--hold 秒]

会顺带把通知区域截图存到临时目录，肉眼确认图标真的出现了。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cuerelay_app import config, imaging, winapi  # noqa: E402
from cuerelay_app.tray import Tray  # noqa: E402


def main(argv: list[str]) -> int:
    hold = 3.0
    if "--hold" in argv:
        hold = float(argv[argv.index("--hold") + 1])

    winapi.set_dpi_awareness()
    hits: list[str] = []

    def menu() -> list:
        return [
            ("显示主窗口", lambda: hits.append("show")),
            None,
            ("退出", lambda: hits.append("quit")),
        ]

    tray = Tray(
        tooltip="CueRelay 托盘测试",
        on_activate=lambda: hits.append("activate"),
        menu_provider=menu,
        icon_path=config.ICON_PATH,
    )
    ok, reason = tray.start()
    print(f"启动：{ok} {reason}")
    if not ok:
        return 1
    print(f"active={tray.active}")
    tray.set_tooltip("CueRelay 托盘测试 · 提示已更新")
    tray.notify("CueRelay", "这是一条测试气泡。")

    # 通知区域在屏幕右下角。整屏截图太大，只截右下角一条。
    width = winapi.user32.GetSystemMetrics(0)
    height = winapi.user32.GetSystemMetrics(1)
    time.sleep(1.2)
    box = (max(0, width - 900), max(0, height - 120), 900, 120)
    data = winapi.capture_screen_bgra(*box)
    bitmap = imaging.Bitmap(box[2], box[3], data)
    png, w, h = bitmap.to_png(max_width=9999)
    out = Path(config.BASE_DIR) / ".." / "cuerelay-tray-shot.png"
    out = out.resolve()
    out.write_bytes(png)
    print(f"通知区域截图 {out}  {w}x{h}")

    time.sleep(max(0.5, hold))
    tray.stop()
    print(f"停止后 active={tray.active}")
    print(f"回调记录：{hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
