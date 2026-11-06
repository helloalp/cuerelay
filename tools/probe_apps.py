# -*- coding: utf-8 -*-
"""验证应用生命周期与「执行前自动拉起」。

会真的关掉 Codex 再拉起来，跑之前请确认没有正在生成的内容。

  1. 关闭 Codex，确认进程真的没了
  2. automation 在找不到窗口时自动把它拉起来（这就是定时任务要的行为）
  3. 运行一条「关闭应用」任务
  4. 运行一条「启动应用」任务
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuerelay_app import apps, winapi
from cuerelay_app.automation import Runner
from cuerelay_app.store import TaskStore


def log(level: str, message: str) -> None:
    print(f"    [{level}] {message}")


def check(name: str, ok: bool) -> bool:
    print(("[通过] " if ok else "[失败] ") + name)
    return ok


def main() -> int:
    winapi.set_dpi_awareness()
    failures: list[str] = []

    def expect(name: str, ok: bool) -> None:
        if not check(name, ok):
            failures.append(name)

    tmpdir = Path(tempfile.mkdtemp())
    task_store = TaskStore(tmpdir / "cuerelay_data.json")
    task_store.update_settings(v2ray_path=apps.detect_v2ray_path(), launch_v2ray_first=False)
    settings = task_store.settings()
    runner = Runner(settings, log)

    codex = apps.codex_app(settings, log)

    print("\n1. 关闭 Codex")
    codex.close(force=True)
    expect("关闭后进程消失", not codex.running())
    expect("关闭后状态是未运行", codex.status() == "未运行")

    print("\n2. 找不到窗口时自动拉起")
    started = time.time()
    window = runner._find_window()
    elapsed = time.time() - started
    expect("自动拉起了目标窗口", bool(window and window.get("hwnd")))
    print(f"    用时 {elapsed:.1f}s，窗口 {window.get('hwnd') if window else None}")

    print("\n3. 关闭任务")
    close_task = task_store.add({"kind": "close", "name": "关闭 Codex", "schedule": "daily"})
    outcome = runner.run_task(task_store.get(close_task["id"]))
    expect("关闭任务执行成功", outcome.ok)
    expect("执行后 Codex 已退出", not codex.running())

    print("\n4. 启动任务")
    launch_task = task_store.add({"kind": "launch", "name": "启动 Codex", "schedule": "daily"})
    started = time.time()
    outcome = runner.run_task(task_store.get(launch_task["id"]))
    expect("启动任务执行成功", outcome.ok)
    expect("执行后 Codex 在前台", codex.status() == "运行中")
    print(f"    用时 {time.time() - started:.1f}s")

    print("\n5. 数据层")
    saved = TaskStore(tmpdir / "cuerelay_data.json").tasks()
    expect("两条任务都存下来了", len(saved) == 2)
    expect("类型字段保留", sorted(t["kind"] for t in saved) == ["close", "launch"])
    expect("开关任务允许空内容", all(t["content"] == "" for t in saved))

    shutil.rmtree(tmpdir, ignore_errors=True)
    print()
    print(f"失败 {len(failures)} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
