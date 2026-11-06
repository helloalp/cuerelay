# -*- coding: utf-8 -*-
"""检查任务列表的分组折叠行为。

在临时副本上跑，不碰真实数据文件：
  1. 打开后「已停止」默认收起，列表里没有它的行
  2. 点表头能展开、能再收起
  3. 选中的任务在收起的分组里时，该分组自动展开
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuerelay_app import store as store_module, winapi
from cuerelay_app.store import TaskStore
from cuerelay_app.ui import app as app_module


def main() -> int:
    winapi.set_dpi_awareness()
    tmpdir = Path(tempfile.mkdtemp())
    tmp = tmpdir / "cuerelay_data.json"
    shutil.copy(ROOT / "cuerelay_data.json", tmp)
    task_store = TaskStore(tmp)

    idle_ids = {
        str(task["id"])
        for task in task_store.tasks()
        if store_module.state_group(task) == "idle"
    }
    print(f"已停止任务 {len(idle_ids)} 个")

    root = tk.Tk()
    root.withdraw()
    window = app_module.App(root, task_store)
    root.update()

    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(("[通过] " if ok else "[失败] ") + name)
        if not ok:
            failures.append(name)

    visible = set(window._row_widgets)
    check("「已停止」默认收起", not (visible & idle_ids))

    window._toggle_group("idle")
    root.update()
    visible = set(window._row_widgets)
    check("点表头后展开", idle_ids and idle_ids <= visible)

    window._toggle_group("idle")
    root.update()
    visible = set(window._row_widgets)
    check("再点一次收起", not (visible & idle_ids))

    target = sorted(idle_ids)[0]
    window.select_task(target)
    root.update()
    visible = set(window._row_widgets)
    check("选中收起分组里的任务会自动展开", target in visible)

    root.destroy()
    shutil.rmtree(tmpdir, ignore_errors=True)
    print()
    print(f"失败 {len(failures)} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
