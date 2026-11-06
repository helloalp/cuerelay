# -*- coding: utf-8 -*-
"""用后台线程连续抓屏，看交互回调之后窗口到底画了什么。

两个必须遵守的点：

1. 抓帧放在另一个线程里跑。Tk 的回调是阻塞的，`after` 排在回调后面，抓不到
   回调中间的画面；而真正会闪的那一下就在回调里。
2. 主线程绝不能 `sleep`。`time.sleep` 不处理 Tk 事件，窗口根本不会重画，
   抓到的就是一张冻住的旧画面——看上去"没有闪烁"，其实是没量到。

用法：
    python tools/probe_chats_flicker.py --view chats --act chips
    python tools/probe_chats_flicker.py --view tasks --act taskfilter
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from cuerelay_app import config, imaging, winapi
from cuerelay_app.store import TaskStore

FRAMES: list[tuple[float, imaging.Bitmap]] = []
LOCK = threading.Lock()
STOP = threading.Event()
RECT = {"left": 0, "top": 0, "width": 0, "height": 0}


def grabber() -> None:
    while not STOP.is_set():
        if RECT["width"] <= 0:
            time.sleep(0.01)
            continue
        stamp = time.perf_counter()
        bitmap = imaging.capture_screen(RECT["left"], RECT["top"], RECT["width"], RECT["height"])
        if bitmap is not None:
            with LOCK:
                FRAMES.append((stamp, bitmap))


def main(argv: list[str]) -> int:
    theme = argv[argv.index("--theme") + 1] if "--theme" in argv else ""
    act = argv[argv.index("--act") + 1] if "--act" in argv else "chips"
    view = argv[argv.index("--view") + 1] if "--view" in argv else "chats"
    region = argv[argv.index("--region") + 1] if "--region" in argv else "full"

    winapi.set_dpi_awareness()
    store = TaskStore(config.DATA_PATH)
    previous_theme = str(store.setting("theme") or "light")
    if theme:
        store.update_settings(theme=theme)

    from cuerelay_app.ui.app import App

    root = tk.Tk()
    app = App(root, store)
    app.show_view(view)
    browser = getattr(app, "chat_browser", None)

    marks: list[tuple[float, str]] = []
    stage = {"start": 0.0, "end": 0.0}

    def mark(label: str) -> None:
        marks.append((time.perf_counter(), label))

    def act_once() -> None:
        stage["start"] = time.perf_counter()
        mark("start")
        if act == "chips":
            browser.chips.select("loose")
        elif act == "chips-back":
            browser.chips.select("all")
        elif act == "select":
            browser.select(browser._order[3], animate=True)
        elif act == "search":
            browser._search_var.set("liquid")
            browser._apply_query()
        elif act == "clear":
            browser._search_var.set("")
            browser._apply_query()
        elif act == "taskfilter":
            app.filter_var.set("liquid")
        elif act == "taskfilter-clear":
            app.filter_var.set("")
        elif act == "taskrefresh":
            app.refresh_tasks(force=True)
        elif act == "snippetfilter":
            app.snippet_filter_var.set("a")
        elif act == "taskselect":
            ids = [str(t.get("id")) for t in app.store.tasks()]
            app.select_task(ids[2] if len(ids) > 2 else ids[0])
        elif act == "taskgroup":
            # 展开默认收起的「已停止」组。选中项所在的分组是折不了的——
            # 那条规则是故意的：选中的任务不能藏在收起的分组里。
            app._toggle_group("idle")
        elif act == "snippets":
            app.show_view("snippets")
        elif act == "view":
            app.show_view("chats" if app.view != "chats" else "tasks")
        mark("done")
        stage["end"] = time.perf_counter()

    def boot() -> None:
        hwnd = winapi.root_window(int(root.winfo_id()))
        winapi.activate_window(hwnd)
        root.attributes("-topmost", True)
        root.update()
        rect = winapi.get_client_rect(hwnd)
        if region == "list":
            # 只抓列表那一竖条：位图小了三四倍，抓帧率能翻上去，
            # 才抓得到三十毫秒以内的那一下。
            rect = {
                "left": rect["left"] + int(rect["width"] * 0.03),
                "top": rect["top"] + int(rect["height"] * 0.15),
                "width": int(rect["width"] * 0.57),
                "height": int(rect["height"] * 0.80),
            }
        RECT.update(rect)
        print(f"窗口 {rect['width']}x{rect['height']} @ {rect['left']},{rect['top']}")
        threading.Thread(target=grabber, daemon=True, name="flicker-grab").start()
        # 先空跑一段，拿到"静止基线"
        root.after(300, act_once)
        root.after(1200, shutdown)

    def shutdown() -> None:
        STOP.set()
        root.attributes("-topmost", False)
        analyze()
        root.destroy()

    def analyze() -> None:
        with LOCK:
            frames = list(FRAMES)
        if len(frames) < 6:
            print(f"帧数太少（{len(frames)}），抓取失败")
            return

        first = frames[0][0]
        print(f"共抓 {len(frames)} 帧，跨度 {(frames[-1][0] - first) * 1000:.0f} ms，"
              f"约 {len(frames) / ((frames[-1][0] - first) or 1):.0f} fps")
        if stage["end"]:
            print(f"交互回调本身耗时 {(stage['end'] - stage['start']) * 1000:.1f} ms")

        final = frames[-1][1]
        width, height = final.width, final.height
        left, right = 0, width
        top, bottom = 0, height
        step_x = max(1, (right - left) // 80)
        step_y = max(1, (bottom - top) // 80)

        def diff(a: imaging.Bitmap, b: imaging.Bitmap) -> float:
            total = 0
            count = 0
            for y in range(top, bottom, step_y):
                for x in range(left, right, step_x):
                    pa = a.rgb(x, y)
                    pb = b.rgb(x, y)
                    total += abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) + abs(pa[2] - pb[2])
                    count += 1
            return total / max(1, count) / 3.0

        scores = [(index, diff(bitmap, final)) for index, (_, bitmap) in enumerate(frames)]
        print("\n时间轴（相对第一帧；色差 = 与最终画面的平均通道差 0-255）：")
        for index, value in scores:
            stamp = (frames[index][0] - first) * 1000
            near = [m for m in marks if abs(m[0] - frames[index][0]) < 0.02]
            flag = f"  <-- {near[0][1]}" if near else ""
            bar = "#" * int(min(36, value / 2))
            print(f"  {index:02d} {stamp:7.0f} ms {value:7.2f} {bar}{flag}")

        baseline = max(value for index, value in scores[:3])
        peak_index, peak = max(scores, key=lambda pair: pair[1])
        print(f"\n静止基线 {baseline:.2f}，峰值 {peak:.2f}（第 {peak_index} 帧）")
        if peak > baseline + 1.0:
            print("→ 峰值高于静止基线，说明过程中画出了不该出现的东西（闪）")
        else:
            print("→ 没有超过静止基线，过程中没有露出底色")

        if "--dump" in argv:
            spec = argv[argv.index("--dump") + 1]
            low, _, high = spec.partition("-")
            for i in range(int(low), min(len(frames), int(high) + 1)):
                data, _w, _h = frames[i][1].to_png(max_width=9999)
                Path(f"demo/seq-{act}-{i:03d}.png").write_bytes(data)
            print(f"  已导出 {low}..{high} 帧")

        for label, index in (("first", 0), ("peak", peak_index), ("last", len(frames) - 1)):
            data, w, h = frames[index][1].to_png(max_width=9999)
            out = Path(f"demo/flicker-{act}-{label}.png")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            print(f"  {label:6s} #{index:02d} -> {out}")
    # 任何回调抛异常都立刻关窗：脚本挂了也不能把窗口留在屏幕上。
    def _bail(exc, val, tb):
        print(f"回调异常：{exc.__name__}: {val}")
        try:
            root.destroy()
        except tk.TclError:
            pass

    root.report_callback_exception = _bail
    # 看门狗兜底：正常结束也会被 destroy 打断，不会真等满。
    root.after(25000, lambda: root.winfo_exists() and root.destroy())
    root.after(1500, boot)
    root.mainloop()
    if theme:
        store.update_settings(theme=previous_theme)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
