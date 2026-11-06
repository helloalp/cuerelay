# -*- coding: utf-8 -*-
"""在真窗口上跑一遍，把浮层每 120 毫秒拍一张。

浮层是给眼睛看的，代码写得再对，看不到就是没做。这个探针不判断对错，只
把过程存成图：收拢、落定、标签、涟漪、进度卡片，一张一张看。

用法：
    python tools/probe_overlay_live.py              # 只切换对话（不发消息）
    python tools/probe_overlay_live.py --send       # 完整跑一条定时消息
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cuerelay_app import automation, imaging, locator, winapi  # noqa: E402
from cuerelay_app.ui import overlay as overlay_module  # noqa: E402
from cuerelay_app.ui.theme import Theme  # noqa: E402

DATA = ROOT / "cuerelay_data.json"
OUT = ROOT / "live"
INTERVAL = 0.12


def main() -> int:
    winapi.set_dpi_awareness()
    settings = json.loads(DATA.read_text(encoding="utf-8")).get("settings", {})
    settings["show_overlay"] = True
    settings["step_delay_seconds"] = 1.6

    OUT.mkdir(exist_ok=True)
    for stale in OUT.glob("*.png"):
        stale.unlink()

    root = tk.Tk()
    root.withdraw()
    theme = Theme(str(settings.get("theme") or "dark"), winapi.get_system_dpi())
    overlay = overlay_module.Overlay(root, theme)

    events: queue.Queue = queue.Queue()
    logs: list[str] = []

    def emit(kind: str, payload) -> None:
        if kind == "overlay":
            events.put(payload)
        elif kind == "log":
            level, message = payload
            logs.append(f"[{level}] {message}")
            print(f"  {level:<8}{message}")

    runner = automation.Runner(
        settings,
        lambda level, message: logs.append(f"[{level}] {message}"),
        exclude_hwnd=0,
        emit=emit,
    )
    window = runner._locate_target()
    if window is None:
        print("没有找到目标窗口")
        return 1
    hwnd = int(window["hwnd"])
    winapi.ensure_foreground(hwnd)
    time.sleep(0.5)

    shot_rect = dict(runner._frame(hwnd).rect)
    frames: list[str] = []
    stop = threading.Event()

    def shoot() -> None:
        index = 0
        while not stop.is_set():
            index += 1
            shot = imaging.capture_screen(
                shot_rect["left"], shot_rect["top"], shot_rect["width"], shot_rect["height"]
            )
            if shot is not None:
                data, _, _ = shot.to_png(max_width=1100)
                path = OUT / f"f{index:03d}.png"
                path.write_bytes(data)
                frames.append(str(path))
                # 再存两张 1:1 的局部：整帧缩到 1100 宽之后，浮层上的字就看不清了。
                for tag, box in (
                    ("top", (0, 0, shot.width, min(240, shot.height))),
                    ("mid", (
                        int(shot.width * 0.18),
                        int(shot.height * 0.05),
                        int(shot.width * 0.72),
                        int(shot.height * 0.62),
                    )),
                ):
                    piece, _, _ = shot.crop(*box).to_png(1500)
                    (OUT / f"f{index:03d}_{tag}.png").write_bytes(piece)
            time.sleep(INTERVAL)

    def pump() -> None:
        while True:
            try:
                payload = events.get_nowait()
            except queue.Empty:
                break
            overlay_module.apply_event(overlay, payload)
        root.after(16, pump)

    def work() -> None:
        try:
            if "--send" in sys.argv:
                runner.run_task(
                    {
                        "name": "可视化自检",
                        "kind": "message",
                        "content": "可视化自检：这条消息只为拍下浮层。",
                        "conversation_title": "Clarify Say",
                    }
                )
            elif "--paste" in sys.argv:
                # 走完整条"定位 → 粘贴 → 校验"的链路，但不发送：
                # 浮层会一直画在窗口上，正好验证"自己的动画会不会被当成应用的变化"。
                runner.paste_only("可视化自检：这段文字只用来验证浮层与粘贴，不会发送。")
                time.sleep(0.6)
                runner.clear_input(hwnd)
            else:
                runner.open_conversation(hwnd, "Clarify Say")
        except Exception as exc:
            print("执行失败：", exc)
        finally:
            stop.set()

    root.after(16, pump)
    threading.Thread(target=shoot, daemon=True).start()
    threading.Thread(target=work, daemon=True).start()

    def finish() -> None:
        stop.set()
        time.sleep(0.2)
        overlay.hide()
        root.quit()

    root.after(200, lambda: threading.Thread(target=lambda: (time.sleep(30), finish()), daemon=True).start())
    root.mainloop()
    overlay.destroy()

    # 只留下有变化的帧：相邻两张一样就删掉前一张，省得看一叠重复图。
    kept: list[str] = []
    previous = None
    for path in frames:
        data = Path(path).read_bytes()
        if data != previous:
            kept.append(path)
            previous = data
        else:
            Path(path).unlink(missing_ok=True)
    print(f"\n拍了 {len(frames)} 张，去掉重复剩 {len(kept)} 张，存在 {OUT}")
    print("  " + ", ".join(Path(item).name for item in kept[:24]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
