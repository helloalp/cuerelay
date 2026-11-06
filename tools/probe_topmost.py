"""验证「置顶幽灵」的检测与修复。

思路：造一个真·置顶窗口盖住某个点，确认
  1. WindowFromPoint 命中它（点击确实被它吃掉了）；
  2. topmost_windows() 能把它揪出来；
  3. repair_window() 之后它不再置顶，同一个点的命中目标换成了别人。

跑法：python tools/probe_topmost.py
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuerelay_app import winapi  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def main() -> int:
    print("probe_topmost")
    root = tk.Tk()
    root.geometry("420x300+260+180")
    root.title("置顶探针（临时窗口）")
    root.configure(bg="#1b1b1f")
    tk.Label(
        root, text="这是一块故意置顶的面板", bg="#1b1b1f", fg="#f0f0f0", font=("Segoe UI", 12)
    ).pack(expand=True)
    root.update_idletasks()
    root.update()

    hwnd = winapi.root_window(root.winfo_id())
    rect = winapi.get_window_rect(hwnd)
    if not rect:
        print("拿不到窗口矩形，探针没法继续")
        root.destroy()
        return 1
    spot = (rect["left"] + rect["width"] // 2, rect["top"] + rect["height"] // 2)
    print(f"探针窗口 hwnd={hwnd} rect={rect['left']},{rect['top']} {rect['width']}x{rect['height']}")

    try:
        # --- 1. 造出置顶状态 ---------------------------------------------
        winapi.set_topmost(hwnd, True)
        root.update()
        check("set_topmost 之后 is_topmost 为真", winapi.is_topmost(hwnd))

        hit = winapi.point_to_root_window(*spot)
        check("WindowFromPoint 命中的是探针窗口", hit == hwnd, f"hit={hit}")

        # --- 2. 扫描能不能发现它 ------------------------------------------
        # 排除自己的 pid 时不该出现：程序不能把自己报成"嫌疑人"。
        mine = winapi.topmost_windows(exclude_pid=os.getpid())
        check(
            "排除自身 pid 后列表里没有自己",
            all(item["hwnd"] != hwnd for item in mine),
            f"{len(mine)} 个",
        )

        all_top = winapi.topmost_windows()
        found = [item for item in all_top if item["hwnd"] == hwnd]
        # 标题里带 "CueRelay" 的窗口会被当成自己，所以探针窗口故意不带这个名字。
        check("不排除时能扫到自己", len(found) == 1, f"共 {len(all_top)} 个置顶窗口")
        if found:
            item = found[0]
            check(
                "扫出来的记录带进程名和类名",
                bool(item["process_name"]) and bool(item["class_name"]),
                f"{item['process_name']} / {item['class_name']}",
            )
            check(
                "扫描结果按面积从大到小",
                all(
                    (a["rect"] or {}).get("width", 0) * (a["rect"] or {}).get("height", 0)
                    >= (b["rect"] or {}).get("width", 0) * (b["rect"] or {}).get("height", 0)
                    for a, b in zip(all_top, all_top[1:])
                ),
            )

        # --- 3. 修复 ------------------------------------------------------
        result = winapi.repair_window(hwnd)
        root.update()
        check("repair_window 报告取消了置顶", bool(result.get("topmost")), str(result))
        check("修复后 is_topmost 为假", not winapi.is_topmost(hwnd))
        check("修复后窗口还在（没有把它关掉）", winapi.is_window(hwnd))

        after = winapi.point_to_root_window(*spot)
        # 取消置顶不会把窗口挪到别人后面——它只是不再压在所有程序之上。
        # 真正要验的是：它从"嫌疑人名单"里消失了。
        check(
            "修复后它不再出现在置顶名单里",
            all(item["hwnd"] != hwnd for item in winapi.topmost_windows()),
        )
        check(
            "修复后该点命中的窗口不再是置顶状态",
            not winapi.is_topmost(after) if after else True,
            f"after={after}",
        )

        # 已经修好的窗口再修一次，不该谎报"改动过"。
        again = winapi.repair_window(hwnd)
        check("重复修复不会谎报取消了置顶", not again.get("topmost"), str(again))
    finally:
        root.destroy()

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for name in FAILS:
            print(f"  - {name}")
        return 1
    print("失败 0 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
