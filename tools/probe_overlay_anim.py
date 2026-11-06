# -*- coding: utf-8 -*-
"""浮层动画的肉眼验证。

造一个假的"目标窗口"，把浮层盖上去，按时间抓几帧存成 PNG。
没有这个探针，动画只能靠读代码判断——"收拢得对不对"是看不出来的。
"""

from __future__ import annotations

import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuerelay_app import imaging, winapi  # noqa: E402
from cuerelay_app.ui.overlay import Overlay  # noqa: E402
from cuerelay_app.ui.theme import Theme  # noqa: E402

TARGET = {"left": 260, "top": 180, "width": 900, "height": 620}
BOX = {"left": 520, "top": 620, "right": 1300, "bottom": 700}
SEND = {"left": 1180, "top": 660, "right": 1240, "bottom": 720}


def main() -> int:
    winapi.set_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    theme = Theme("light", 96)

    stage = tk.Toplevel(root)
    stage.geometry(f"{TARGET['width']}x{TARGET['height']}+{TARGET['left']}+{TARGET['top']}")
    stage.configure(bg="#1B1B1E")
    stage.overrideredirect(True)
    canvas = tk.Canvas(stage, bg="#1B1B1E", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    # 假界面：侧边栏、几行"对话"、底部输入框。
    canvas.create_rectangle(0, 0, 240, TARGET["height"], fill="#17171A", outline="")
    for index in range(8):
        canvas.create_rectangle(16, 90 + index * 44, 220, 118 + index * 44, fill="#26262B", outline="")
    canvas.create_rectangle(300, 560, 840, 760, fill="#232327", outline="#3D3D45")
    canvas.create_text(330, 600, text="Work with Codex", anchor="w", fill="#75757F", font=("Segoe UI", 13))
    # 目标应用可能正好铺满整个屏幕（窗口拉到最大时就是这样），不把这块假界面
    # 提到最前面，截出来的就是真的 Codex 窗口，浮层画在哪都看不出来。
    stage.attributes("-topmost", True)
    stage.lift()

    overlay = Overlay(root, theme)
    overlay.show(dict(TARGET))
    overlay.progress(2, 5, "选中结果")

    frames: list[str] = []

    def snap(tag: str) -> None:
        shot = imaging.capture_screen(
            TARGET["left"], TARGET["top"], TARGET["width"], TARGET["height"]
        )
        if shot is None:
            return
        data, _, _ = shot.to_png(max_width=TARGET["width"])
        path = Path(__file__).resolve().parent.parent / f".anim_{tag}.png"
        path.write_bytes(data)
        frames.append(tag)

    def start() -> None:
        overlay.box(dict(BOX), "search", "搜索结果 · Clarify Say")
        overlay.box(dict(SEND), "send", "发送按钮")

    def finish() -> None:
        overlay.pulse((1230, 690))
        root.after(60, lambda: snap("4_pulse"))
        root.after(300, lambda: (overlay.hide(), root.after(120, root.destroy)))

    root.after(80, start)
    root.after(110, lambda: snap("1_converge"))
    root.after(190, lambda: snap("2_near"))
    root.after(320, lambda: snap("3_settled"))
    root.after(420, finish)
    root.mainloop()
    print("frames:", ", ".join(frames) if frames else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
