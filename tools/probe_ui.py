# -*- coding: utf-8 -*-
"""界面冒烟测试：把每个视图都建一遍、切一遍，再截图。

不碰真实数据（复制到临时目录），也不占用单实例锁，所以可以和正在运行的
CueRelay 同时存在。

用法：
    python tools/probe_ui.py [输出目录]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cuerelay_app import config, locator, winapi  # noqa: E402
from cuerelay_app.store import TaskStore  # noqa: E402
from cuerelay_app.ui.app import App  # noqa: E402


def pump(root: tk.Tk, rounds: int = 20) -> None:
    for _ in range(rounds):
        try:
            root.update()
        except tk.TclError:
            return
        time.sleep(0.02)


def shot(root: tk.Tk, path: Path) -> None:
    hwnd = winapi.root_window(int(root.winfo_id()))
    frame = locator.grab(hwnd)
    if frame is None:
        print(f"  截图失败：读不到窗口 {hwnd}")
        return
    data, width, height = frame.bitmap.to_png(max_width=9999)
    path.write_bytes(data)
    print(f"  截图 {path}  {width}x{height}")


def main(argv: list[str]) -> int:
    winapi.set_dpi_awareness()
    out_dir = Path(argv[0]) if argv else Path(tempfile.gettempdir()) / "cuerelay-ui"
    out_dir.mkdir(parents=True, exist_ok=True)

    temp = Path(tempfile.mkdtemp(prefix="cuerelay-ui-"))
    data_path = temp / "cuerelay_data.json"
    if config.DATA_PATH.exists():
        shutil.copy(config.DATA_PATH, data_path)
    store = TaskStore(data_path)
    print(f"任务 {len(store.tasks())} 条 · 片段 {len(store.snippets())} 个 · 记录 {len(store.history())} 条")

    failures = 0
    root = tk.Tk()
    app = App(root, store)
    pump(root, 30)

    for view in ("tasks", "snippets", "chats", "apps", "log", "settings"):
        try:
            app.show_view(view)
            pump(root, 12)
            print(f"[通过] 视图 {view}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[失败] 视图 {view}: {exc.__class__.__name__}: {exc}")

    # 日志视图的两种模式
    try:
        app.show_view("log")
        app.log_mode_var.set("history")
        app._on_log_mode("history")
        pump(root, 10)
        print("[通过] 执行记录面板")
        app.log_mode_var.set("log")
        app._on_log_mode("log")
        pump(root, 6)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[失败] 执行记录面板: {exc.__class__.__name__}: {exc}")

    # 片段：新建 -> 保存 -> 选中
    try:
        app.show_view("snippets")
        app.new_snippet()
        app.snippet_name_var.set("冒烟测试片段")
        app.snippet_area.set("这是一段用来验证界面的内容。")
        assert app.save_snippet(), "保存失败"
        pump(root, 8)
        assert store.snippets(), "片段没有落盘"
        assert app.selected_snippet_id, "保存后没有选中"
        print(f"[通过] 片段保存：{store.snippets()[0]['name']}")
        app.refresh_snippets(force=True)
        pump(root, 6)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[失败] 片段: {exc.__class__.__name__}: {exc}")

    # 对话视图的空状态
    try:
        app.show_view("chats")
        app._apply_conversations([])
        pump(root, 8)
        print("[通过] 对话空状态")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[失败] 对话空状态: {exc.__class__.__name__}: {exc}")

    # 命令面板：打开 -> 过滤 -> 关闭
    try:
        app.show_view("tasks")
        pump(root, 6)
        app.open_palette()
        pump(root, 8)
        palette = app._palette
        assert palette is not None, "面板没有打开"
        total = len(palette.commands)
        palette.query_var.set("片段")
        palette._apply("片段")
        pump(root, 4)
        filtered = len(palette._results)
        assert 0 < filtered < total, f"过滤没有生效：{filtered}/{total}"
        print(f"[通过] 命令面板：共 {total} 条，过滤「片段」后 {filtered} 条")
        palette.close()
        pump(root, 4)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[失败] 命令面板: {exc.__class__.__name__}: {exc}")

    # 全局热键：真的按一次 Ctrl+Alt+K，看窗口和面板会不会被叫出来。
    # keybd_event 造的按键和真按一样会触发 RegisterHotKey，所以这一条走的是
    # 完整链路：热键线程 -> 队列 -> 界面线程 -> 唤出。
    try:
        app.show_view("tasks")
        pump(root, 6)
        if app._palette is not None:
            app._palette.close()
            pump(root, 4)
        if app._hotkey is None or not app._hotkey.active:
            print("[跳过] 全局热键：没有注册成功（多半是被别的程序占了）")
        else:
            KEYUP = 0x0002
            user32 = winapi.user32
            for vk in (winapi.VK_CONTROL, winapi.VK_MENU, winapi.VK_K):
                user32.keybd_event(vk, 0, 0, 0)
            for vk in (winapi.VK_K, winapi.VK_MENU, winapi.VK_CONTROL):
                user32.keybd_event(vk, 0, KEYUP, 0)
            for _ in range(90):
                root.update()
                if app._palette is not None:
                    break
                time.sleep(0.02)
            assert app._palette is not None, "热键没有唤出命令面板"
            print("[通过] 全局热键唤出")
            app._palette.close()
            pump(root, 4)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[失败] 全局热键: {exc.__class__.__name__}: {exc}")

    # 每个视图截一张，人工核对排版
    for view in ("tasks", "snippets", "chats", "apps", "settings", "log"):
        try:
            app.show_view(view)
            pump(root, 10)
            shot(root, out_dir / f"{view}.png")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[失败] 截图 {view}: {exc.__class__.__name__}: {exc}")

    try:
        root.destroy()
    except tk.TclError:
        pass
    print(f"\n失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
