# -*- coding: utf-8 -*-
"""切主题会把所有页面重建一遍，是控件池最容易失效的地方。

池子里存的是控件引用；`_build()` 把控件全销毁之后，如果池子没跟着清空，
下一次渲染就会往已销毁的控件上 `configure`，抛 TclError。

这个探针只做一件事：深色 → 浅色来回切，每切一次就检查列表还画得出来。
窗口一定会被关掉（三重保险：正常结束、异常兜底、超时看门狗），
不会像上次那样留一个卡在某个页面的窗口。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from cuerelay_app import config, winapi
from cuerelay_app.store import TaskStore

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    RESULTS.append((bool(ok), label))


def main() -> int:
    winapi.set_dpi_awareness()
    store = TaskStore(config.DATA_PATH)
    original_theme = str(store.setting("theme") or "light")
    from cuerelay_app.ui.app import App

    root = tk.Tk()
    app = App(root, store)
    errors: list[str] = []
    root.report_callback_exception = lambda exc, val, tb: errors.append(f"{exc.__name__}: {val}")
    finished = {"done": False}

    def watchdog() -> None:
        # 看门狗：脚本自己挂了也不能把窗口留在屏幕上。
        if not finished["done"]:
            print("超时，强制关窗")
            try:
                root.destroy()
            except tk.TclError:
                pass

    def run() -> None:
        try:
            app.show_view("tasks")
            root.update_idletasks()
            check(len(app._row_widgets) > 0, f"切主题前任务行 {len(app._row_widgets)}")

            for target in ("dark", "light", "dark", "light"):
                app._on_theme_change(target)
                root.update_idletasks()
                rows = len(app._row_widgets)
                heads = len(app._task_heads)
                check(rows > 0, f"切到 {target} 后任务行 {rows}")
                check(heads > 0, f"切到 {target} 后分组头 {heads}")
                app.show_view("snippets")
                root.update_idletasks()
                app.show_view("chats")
                root.update_idletasks()
                browser = getattr(app, "chat_browser", None)
                check(browser is not None, f"切到 {target} 后对话页还在")
                app.show_view("tasks")
                root.update_idletasks()

            # 池子必须跟着重建一起清空，否则会指向已销毁的控件
            check(all(p["frame"].winfo_exists() for p in app._task_rows), "池子里的控件都还活着")
            check(not errors, "回调没有抛异常" + (f"：{errors}" if errors else ""))
        except Exception:
            traceback.print_exc()
            check(False, "脚本自身抛了异常")
        finally:
            finished["done"] = True
            store.update_settings(theme=original_theme)
            try:
                root.destroy()
            except tk.TclError:
                pass

    root.after(1200, run)
    root.after(12000, watchdog)
    root.mainloop()

    passed = sum(1 for ok, _ in RESULTS if ok)
    for ok, label in RESULTS:
        print(f"[{'通过' if ok else '失败'}] {label}")
    print(f"\n{passed}/{len(RESULTS)} 项通过。主题已恢复为 {original_theme}。")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
