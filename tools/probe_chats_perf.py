# -*- coding: utf-8 -*-
"""量一下"操作一下要新建多少控件"。

频闪的根源是销毁重建：Tk 在 Windows 上不做双缓冲，一次建几十个控件，中间
会露出父容器底色。这个探针给 tkinter 的三个基础控件计数，然后走一遍
搜索 / 切筛选 / 点选 / 折叠，看交互期间还有没有新建。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from cuerelay_app import config, winapi
from cuerelay_app.store import TaskStore

counts = {"frame": 0, "label": 0, "canvas": 0}


def main() -> int:
    winapi.set_dpi_awareness()
    store = TaskStore(config.DATA_PATH)

    from cuerelay_app.ui.app import App

    root = tk.Tk()
    app = App(root, store)
    app.show_view("chats")
    browser = app.chat_browser

    def patch() -> None:
        base_frame = tk.Frame.__init__
        base_label = tk.Label.__init__
        base_canvas = tk.Canvas.__init__

        def frame_init(self, *a, **k):
            counts["frame"] += 1
            return base_frame(self, *a, **k)

        def label_init(self, *a, **k):
            counts["label"] += 1
            return base_label(self, *a, **k)

        def canvas_init(self, *a, **k):
            counts["canvas"] += 1
            return base_canvas(self, *a, **k)

        tk.Frame.__init__ = frame_init
        tk.Label.__init__ = label_init
        tk.Canvas.__init__ = canvas_init

    def measure(label: str, fn) -> None:
        before = dict(counts)
        start = time.perf_counter()
        fn()
        root.update_idletasks()
        elapsed = (time.perf_counter() - start) * 1000
        created = sum(counts.values()) - sum(before.values())
        print(f"{label:22s} {elapsed:7.1f} ms   新建控件 {created}")

    steps = []

    def step(label: str, fn) -> None:
        steps.append((label, fn))

    def loaded() -> None:
        print(f"首屏 {len(browser._rows)} 行，控件池 {len(browser._row_pool)} 行")
        patch()
        measure("选中另一条", lambda: browser.select(browser._order[3], animate=True))
        measure("搜索 liquidglass", lambda: (browser._search_var.set("liquidglass"), browser._apply_query()))
        measure("清空搜索", lambda: (browser._search_var.set(""), browser._apply_query()))
        measure("切到无项目", lambda: browser.chips.select("loose"))
        measure("切回全部", lambda: browser.chips.select("all"))
        measure("折叠第一个分组", lambda: browser._toggle_group(browser._header_pool[0]["key"]))
        measure("展开第一个分组", lambda: browser._toggle_group(browser._header_pool[0]["key"]))
        measure("继续加载", browser._load_more)
        root.after(400, finish)

    def finish() -> None:
        print("\n结论：交互期间新建控件数应为 0，否则说明还在销毁重建。")
        root.destroy()

    root.after(1400, loaded)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
