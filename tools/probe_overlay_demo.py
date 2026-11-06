# -*- coding: utf-8 -*-
"""在假窗口上放一遍浮层，把每一帧拍下来。

真窗口那套（probe_overlay_live.py）能看到"对不对"，但改一次样子就要跑一遍
识别，太慢。这个探针不碰 Codex：自己开一个深色假窗口当背景，按脚本发浮层
事件，60 毫秒拍一张。看的是线怎么收、标签怎么浮出来、卡片落在哪。

用法：
    python tools/probe_overlay_demo.py            # 拍一整段
    python tools/probe_overlay_demo.py --hold     # 拍完停住，方便肉眼盯
"""

from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cuerelay_app import imaging, winapi  # noqa: E402
from cuerelay_app.ui import overlay as overlay_module  # noqa: E402
from cuerelay_app.ui.theme import Theme  # noqa: E402

OUT = ROOT / "demo"
INTERVAL = 0.06
WIDTH = 1000
HEIGHT = 640


def build_background(root: tk.Tk) -> tk.Toplevel:
    """一个像目标应用的深色窗口：标题栏、侧边栏、对话、输入框。"""

    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.geometry(f"{WIDTH}x{HEIGHT}+120+90")
    win.configure(bg="#16161A")
    canvas = tk.Canvas(win, bg="#16161A", highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_rectangle(0, 0, WIDTH, 34, fill="#1D1D22", outline="")
    canvas.create_text(16, 17, text="Codex", anchor="w", fill="#8E8E9A", font=("Segoe UI", 9))
    canvas.create_text(120, 17, text="Clarify Say", anchor="w", fill="#E6E6EC", font=("Segoe UI", 9))
    canvas.create_rectangle(0, 34, 210, HEIGHT, fill="#121216", outline="")
    for index in range(9):
        canvas.create_text(
            28, 70 + index * 26, text=f"对话 {index + 1}", anchor="w",
            fill="#6E6E7A", font=("Segoe UI", 9),
        )
    canvas.create_text(260, 120, text="这是一条对话消息，用来当背景。", anchor="w",
                       fill="#C8C8D2", font=("Segoe UI", 10))
    canvas.create_text(260, 150, text="浮层画在它上面。", anchor="w",
                       fill="#C8C8D2", font=("Segoe UI", 10))
    canvas.create_rectangle(250, 520, 960, 610, fill="#1B1B20", outline="#2E2E36")
    canvas.create_text(272, 548, text="Ask anything", anchor="w", fill="#5E5E6A",
                       font=("Segoe UI", 10))
    canvas.create_oval(900, 560, 936, 596, fill="#3E6BE8", outline="")
    return win


def main() -> int:
    winapi.set_dpi_awareness()
    OUT.mkdir(exist_ok=True)
    for stale in OUT.glob("*.png"):
        stale.unlink()

    root = tk.Tk()
    root.withdraw()
    theme = Theme("dark", winapi.get_system_dpi())
    background = build_background(root)
    root.update()

    rect = {
        "left": background.winfo_rootx(),
        "top": background.winfo_rooty(),
        "width": WIDTH,
        "height": HEIGHT,
    }
    overlay = overlay_module.Overlay(root, theme)
    overlay.show(rect)

    def local(x1: int, y1: int, x2: int, y2: int) -> dict:
        """脚本里写客户区坐标，浮层要的是屏幕坐标——这里补上窗口原点。

        忘了这一步的后果很隐蔽：框会整体偏出去，看起来像"识别错了"，
        其实只是坐标系错了。
        """

        return {
            "left": rect["left"] + x1,
            "top": rect["top"] + y1,
            "right": rect["left"] + x2,
            "bottom": rect["top"] + y2,
        }

    # 脚本：扫描 → 认出输入框 → 下一步 → 认出搜索框 → 点一下 → 完成。
    # 每一步之间留够时间，让 60ms 的采样能抓到收拢和落定的中间帧。
    script: list[tuple[float, callable]] = [
        (0.10, lambda: overlay.scan(local(0, 300, WIDTH, HEIGHT), "扫描输入框")),
        (1.70, lambda: overlay.box(local(250, 520, 960, 610), "composer", "输入框 · 识别 · 710×90")),
        (2.10, lambda: overlay.progress(1, 5, "定位输入框", "accent", detail="已锁定 · 识别")),
        (2.60, lambda: overlay.box(local(120, 8, 160, 28), "search", "搜索框")),
        (2.95, lambda: overlay.progress(2, 5, "切换对话", "accent", detail="搜索「Clarify Say」")),
        (3.40, lambda: overlay.box(local(900, 560, 936, 596), "send", "发送按钮")),
        (3.60, lambda: overlay.pulse((918, 578))),
        (3.95, lambda: overlay.progress(3, 5, "发送", "accent", detail="点击发送按钮")),
        (4.60, lambda: overlay.progress(5, 5, "已发送", "success", detail="用时 4.3 秒")),
        (5.40, lambda: overlay.progress(3, 5, "已停止", "danger", detail="输入框里没有内容")),
        (6.30, lambda: overlay.hide()),
    ]

    frames: list[str] = []
    started = time.monotonic()
    stop = threading.Event()

    def shoot() -> None:
        index = 0
        while not stop.is_set():
            index += 1
            shot = imaging.capture_screen(rect["left"], rect["top"], rect["width"], rect["height"])
            if shot is not None:
                data, _, _ = shot.to_png(max_width=1000)
                path = OUT / f"d{index:03d}.png"
                path.write_bytes(data)
                frames.append(str(path))
            time.sleep(INTERVAL)

    def drive() -> None:
        for delay, action in script:
            while time.monotonic() - started < delay:
                time.sleep(0.01)
            try:
                action()
            except Exception as exc:
                print("  动作失败：", exc)
        stop.set()
        print(f"拍了 {len(frames)} 张")

    threading.Thread(target=shoot, daemon=True).start()
    threading.Thread(target=drive, daemon=True).start()

    def finish() -> None:
        if "--hold" in sys.argv:
            return
        overlay.destroy()
        root.quit()

    root.after(int((script[-1][0] + 1.2) * 1000), finish)
    if "--hold" in sys.argv:
        root.after(int((script[-1][0] + 1.2) * 1000), lambda: None)
    root.mainloop()

    kept: list[str] = []
    previous = None
    for path in frames:
        data = Path(path).read_bytes()
        if data != previous:
            kept.append(path)
            previous = data
        else:
            Path(path).unlink(missing_ok=True)
    print(f"去掉重复剩 {len(kept)} 张，存在 {OUT}")
    print("  " + ", ".join(Path(item).name for item in kept))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
