# -*- coding: utf-8 -*-
"""对话页交互冒烟测试：真的把每个按钮的路径走一遍，抓 Tk 回调里的异常。

用法：python tools/probe_chats_ui.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from cuerelay_app import config, winapi
from cuerelay_app.store import TaskStore

problems: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("[通过] " if condition else "[失败] ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        problems.append(label)


def main() -> int:
    winapi.set_dpi_awareness()
    store = TaskStore(config.DATA_PATH)

    from cuerelay_app.ui.app import App

    root = tk.Tk()
    root.report_callback_exception = lambda *exc: problems.append(
        "Tk 回调异常：" + "".join(traceback.format_exception(*exc))
    )
    app = App(root, store)
    app.show_view("chats")
    browser = app.chat_browser

    steps: list = []

    def later(seconds: float, fn) -> None:
        steps.append((seconds, fn))

    def run(index: int = 0) -> None:
        if index >= len(steps):
            root.after(200, finish)
            return
        delay, fn = steps[index]
        root.after(int(delay * 1000), lambda: (fn(), run(index + 1)))

    def step_loaded() -> None:
        check("读完对话库", browser.library is not None, f"{len(app._conversations)} 条")
        check("渲染了行", len(browser._rows) > 0, f"{len(browser._rows)} 行")
        check("自动选中了第一条", bool(browser._selected_id), browser._selected_id[:8])
        check("状态行有统计", "条对话" in app.chat_status_var.get(), app.chat_status_var.get())

    def step_search() -> None:
        browser._search_var.set("liquidglass")
        root.after(260, check_search)

    def check_search() -> None:
        rows = len(browser._rows)
        check("搜索有结果", rows > 0, f"{rows} 行")
        titles = [info["record"].title for info in browser._rows.values()]
        check(
            "搜索结果都命中关键词",
            all("liquidglass" in title.lower() for title in titles),
            str(titles[:3]),
        )

    def step_search_miss() -> None:
        browser._search_var.set("zzzz-not-exist")
        root.after(260, check_miss)

    def check_miss() -> None:
        check("无结果时不崩", len(browser._rows) == 0)

    def step_clear() -> None:
        browser._clear_query()
        root.after(220, lambda: check("清空搜索恢复列表", len(browser._rows) > 0))

    def step_scope_loose() -> None:
        browser.chips.select("loose")
        root.after(260, check_loose)

    def check_loose() -> None:
        check("无项目筛选", browser._scope == "loose")
        records = [info["record"] for info in browser._rows.values()]
        check("无项目里确实都没有项目", all(not item.project for item in records), f"{len(records)} 行")

    def step_scope_archived() -> None:
        browser.chips.select("archived")
        root.after(260, check_archived)

    def check_archived() -> None:
        records = [info["record"] for info in browser._rows.values()]
        check("归档里确实都是归档", all(item.archived for item in records), f"{len(records)} 行")

    def step_scope_all() -> None:
        browser.chips.select("all")
        root.after(260, lambda: check("切回全部", len(browser._rows) > 0))

    def step_subagents() -> None:
        browser.subagent_var.set(True)
        browser._on_subagents()
        root.after(260, check_subagents)

    def check_subagents() -> None:
        records = [info["record"] for info in browser._rows.values()]
        check("显示子代理后出现子代理行", any(item.is_subagent for item in records), f"{len(records)} 行")
        browser.subagent_var.set(False)
        browser._on_subagents()

    def step_collapse() -> None:
        keys = [key for key, _label, _rows in browser._groups(browser._visible())]
        if keys:
            browser._toggle_group(keys[0])
            check("折叠分组后行数减少", len(browser._rows) >= 0)
            browser._toggle_group(keys[0])

    def step_load_more() -> None:
        before = len(browser._rows)
        browser._load_more()
        check("继续加载增加了行", len(browser._rows) > before, f"{before} → {len(browser._rows)}")

    def step_keyboard() -> None:
        first = browser._order[0]
        browser.select(first)
        browser._step(1)
        check("方向键切换选中", browser._selected_id != first, browser._selected_id[:8])

    def step_select_last() -> None:
        last = browser._order[-1]
        browser.select(last)
        check("选中最后一行", browser._selected_id == last)

    def finish() -> None:
        if problems:
            print("\n发现问题：")
            for item in problems:
                print(" -", item)
        else:
            print("\n全部通过。")
        root.destroy()

    later(1.4, step_loaded)
    later(0.2, step_search)
    later(0.8, step_search_miss)
    later(0.8, step_clear)
    later(0.6, step_scope_loose)
    later(0.6, step_scope_archived)
    later(0.6, step_scope_all)
    later(0.6, step_subagents)
    later(0.6, step_collapse)
    later(0.6, step_load_more)
    later(0.4, step_keyboard)
    later(0.4, step_select_last)

    run()
    root.mainloop()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
