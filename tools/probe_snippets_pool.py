# -*- coding: utf-8 -*-
"""用临时数据文件验证片段列表的控件池：筛选、选中、清空都不该新建控件。

不改动真实数据——全程用一个临时 JSON。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from cuerelay_app import winapi
from cuerelay_app.store import TaskStore

counts = {"n": 0}


def main() -> int:
    winapi.set_dpi_awareness()
    tmp = Path(tempfile.mkdtemp()) / "data.json"
    store = TaskStore(tmp)
    for index in range(12):
        store.add_snippet({
            "name": f"常用提示词 {index:02d}",
            "content": f"把这段内容整理成要点，第 {index} 条。",
        })

    from cuerelay_app.ui.app import App

    root = tk.Tk()
    app = App(root, store)
    app.show_view("snippets")

    def patch() -> None:
        base = tk.Frame.__init__

        def counted(self, *a, **k):
            counts["n"] += 1
            return base(self, *a, **k)

        tk.Frame.__init__ = counted

    def step() -> None:
        print("初始 行", len(app._snippet_rows), "池", len(app._snippet_pool))
        patch()
        before = counts["n"]
        app.snippet_filter_var.set("常用")
        root.update_idletasks()
        app.snippet_filter_var.set("")
        root.update_idletasks()
        app.select_snippet(list(app._snippet_rows)[3])
        root.update_idletasks()
        app.snippet_filter_var.set("没有这个")
        root.update_idletasks()
        app.snippet_filter_var.set("")
        root.update_idletasks()
        made = counts["n"] - before
        print(f"五次操作新建 Frame {made} 个（应为 0）")
        print("末尾 行", len(app._snippet_rows), "池", len(app._snippet_pool))
        root.after(120, root.destroy)

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
    root.after(1200, step)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
