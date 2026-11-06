# -*- coding: utf-8 -*-
"""打开主窗口、切到对话页、等它读完库，然后截一张图。

用法：
    python tools/shot_chats.py out.png [--view chats] [--theme dark] [--wait 2.5]

只用于人工核对界面，不进自检。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from cuerelay_app import config, imaging, winapi
from cuerelay_app.store import TaskStore


def main(argv: list[str]) -> int:
    out = Path(argv[0] if argv else "chats.png")
    view = "chats"
    theme = ""
    wait = 2.5
    if "--view" in argv:
        view = argv[argv.index("--view") + 1]
    if "--theme" in argv:
        theme = argv[argv.index("--theme") + 1]
    if "--wait" in argv:
        wait = float(argv[argv.index("--wait") + 1])

    winapi.set_dpi_awareness()
    store = TaskStore(config.DATA_PATH)
    previous_theme = str(store.setting("theme") or "light")
    if theme:
        store.update_settings(theme=theme)

    from cuerelay_app.ui.app import App

    if "--skeleton" in argv:
        # 故意把读库拖慢，用来看骨架屏长什么样。
        from cuerelay_app import codexdb as codexdb_module

        original = codexdb_module.scan

        def slow_scan(*args, **kwargs):
            time.sleep(1.6)
            return original(*args, **kwargs)

        codexdb_module.scan = slow_scan

    root = tk.Tk()
    app = App(root, store)
    app.show_view(view)

    query = argv[argv.index("--query") + 1] if "--query" in argv else ""
    scope = argv[argv.index("--scope") + 1] if "--scope" in argv else ""

    def apply_state() -> None:
        browser = getattr(app, "chat_browser", None)
        if browser is None:
            return
        if scope:
            browser.chips.select(scope)
        if query:
            browser._search_var.set(query)

    if query or scope:
        root.after(int(max(0.4, wait - 0.8) * 1000), apply_state)

    result = {"code": 1}

    def capture() -> None:
        try:
            root.update_idletasks()
            hwnd = winapi.root_window(int(root.winfo_id()))
            winapi.activate_window(hwnd)
            time.sleep(0.5)
            got = imaging.capture_client(hwnd)
            if got is None:
                print("截图失败")
            else:
                bitmap, rect = got
                data, width, height = bitmap.to_png(max_width=9999)
                out.write_bytes(data)
                print(f"已保存 {out} ({width}x{height}) 客户区 {rect}")
                result["code"] = 0
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    root.after(int(wait * 1000), capture)
    root.mainloop()
    if theme:
        store.update_settings(theme=previous_theme)
    return result["code"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
