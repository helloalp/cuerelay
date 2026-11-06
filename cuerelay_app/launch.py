# -*- coding: utf-8 -*-
"""启动逻辑与命令行入口。"""

from __future__ import annotations

import sys
import traceback
from typing import List, Optional

from . import config, winapi
from .config import friendly_error

MUTEX_NAME = "Local\\CueRelay-7C58B1A2"


def _say(text: str) -> None:
    """pythonw 下 stdout 为 None，这里必须安静地跳过。"""

    stream = sys.stdout
    if stream is None:
        return
    try:
        print(text, flush=True)
    except Exception:
        pass


def _write_crash(exc: BaseException) -> None:
    try:
        text = "\n".join(
            [
                f"{config.now_local().isoformat(timespec='seconds')} {config.APP_NAME} {config.APP_VERSION}",
                traceback.format_exc(),
                "",
            ]
        )
        with config.CRASH_PATH.open("a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        pass


def _run_gui() -> int:
    import tkinter as tk

    from .store import TaskStore
    from .ui.app import App

    mutex, already_running = winapi.create_single_instance(MUTEX_NAME)
    if already_running:
        try:
            from tkinter import messagebox

            messagebox.showinfo(config.APP_NAME, "CueRelay 已经在运行。")
        except Exception:
            pass
        return 0

    store = TaskStore(config.DATA_PATH)
    root = tk.Tk()
    app = App(root, store, mutex_handle=mutex)
    app.log("info", f"{config.APP_NAME} 已启动。")
    root.mainloop()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    if "--version" in args:
        _say(f"{config.APP_NAME} {config.APP_VERSION}")
        return 0

    if "--check" in args:
        from . import selftest

        return 1 if selftest.run() else 0

    if "--help" in args or "-h" in args:
        _say(f"{config.APP_NAME} {config.APP_VERSION}")
        _say("  cuerelay.py            启动界面")
        _say("  cuerelay.py --check    运行无界面自检")
        _say("  cuerelay.py --version  显示版本")
        return 0

    if not winapi.IS_WINDOWS:
        _say("CueRelay 只支持 Windows。")
        return 1

    try:
        winapi.set_dpi_awareness()
        return _run_gui()
    except Exception as exc:
        _write_crash(exc)
        try:
            from tkinter import messagebox

            messagebox.showerror(
                config.APP_NAME,
                f"程序启动失败：\n{friendly_error(exc)}\n\n详细信息已写入 {config.CRASH_PATH.name}",
            )
        except Exception:
            pass
        _say(f"启动失败：{friendly_error(exc)}")
        return 1
