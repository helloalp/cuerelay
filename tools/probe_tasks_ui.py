# -*- coding: utf-8 -*-
"""任务列表的交互自检：控件池改造之后，行为要和新版一模一样。

只读，不改数据——除了「新建任务」那一项，它只动草稿不动存储。
"""

from __future__ import annotations

import sys
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
    from cuerelay_app.ui.app import App

    root = tk.Tk()
    app = App(root, store)
    app.show_view("tasks")

    # 任何回调抛异常都立刻关窗：脚本挂了也不能把窗口留在屏幕上。
    errors: list[str] = []

    def _bail(exc, val, tb):
        errors.append(f"{exc.__name__}: {val}")
        try:
            root.destroy()
        except tk.TclError:
            pass

    root.report_callback_exception = _bail
    # 看门狗兜底：正常结束也会被 destroy 打断，不会真等满。
    root.after(25000, lambda: root.winfo_exists() and root.destroy())

    def run() -> None:
        total = len(store.tasks())
        check(len(app._row_widgets) > 0, f"首屏渲染了行  {len(app._row_widgets)} 行")
        check(len(app._task_heads) >= 1, f"分组头已建  {len(app._task_heads)} 个")

        # 选中：点哪条，右边就载入哪条
        ids = [str(t["id"]) for t in store.tasks()]
        app.select_task(ids[2])
        check(app.selected_id == ids[2], "选中第三条")
        check(app.draft.get("id") == ids[2], "右侧载入了同一条")
        parts = app._row_widgets.get(ids[2])
        check(parts is not None, "选中行在可见列表里")

        # 高亮只落在一行
        lit = [tid for tid, p in app._row_widgets.items() if p["task_id"] == app.selected_id]
        check(len(lit) == 1, "高亮只有一行")

        # 筛选
        app.filter_var.set("睡前")
        hits = len(app._row_widgets)
        check(0 < hits <= total, f"关键词「睡前」命中 {hits}/{total} 行")
        leaked = [
            tid
            for tid in app._row_widgets
            if "睡前" not in str(store.get(tid).get("name") or "").lower()
            and "睡前" not in str(store.get(tid).get("content") or "").lower()
        ]
        check(not leaked, "命中的行确实都含关键词")
        app.filter_var.set("绝不可能匹配的词")
        check(len(app._row_widgets) == 0, "无结果时列表清空")
        check(app._task_empty is not None and app._task_empty["frame"].winfo_manager() == "grid",
              "空状态显示了")
        app.filter_var.set("")
        check(len(app._row_widgets) > 0, "清空搜索恢复列表")

        # 分组折叠 / 展开
        before = len(app._row_widgets)
        app._toggle_group("failed")
        after = len(app._row_widgets)
        check(after != before, f"折叠失败组  {before} → {after} 行")
        app._toggle_group("failed")
        check(len(app._row_widgets) == before, "再展开恢复行数")

        # 池子复用：来回切十次，池子不该一直长
        for _ in range(10):
            app._toggle_group("failed")
            app._toggle_group("failed")
        check(len(app._task_rows) <= total + 2, f"来回折叠十次后池子仍为 {len(app._task_rows)} 行")

        # 悬停不改选中
        keep = app.selected_id
        app._hover_parts(app._row_widgets[ids[1]], True)
        check(app.selected_id == keep, "悬停不改变选中")
        app._hover_parts(app._row_widgets[ids[1]], False)

        # 新建任务：清空草稿，不动存储
        app.new_task()
        check(app.selected_id is None, "新建后没有选中项")
        check(app.editor_title.cget("text") == "新建任务", "右侧标题变成新建任务")
        check(len(store.tasks()) == total, "新建没有写入存储")

        check(not errors, "回调没有抛异常" + (f"：{errors}" if errors else ""))
        root.after(120, root.destroy)

    root.after(1500, run)
    root.mainloop()

    passed = sum(1 for ok, _ in RESULTS if ok)
    for ok, label in RESULTS:
        print(f"[{'通过' if ok else '失败'}] {label}")
    print(f"\n{passed}/{len(RESULTS)} 项通过。")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
