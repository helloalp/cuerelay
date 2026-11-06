# -*- coding: utf-8 -*-
"""调度引擎。

同一时刻只允许一个任务真正驱动鼠标键盘，避免两个任务同时抢占目标窗口。
调度线程只读取轻量索引，不会为了判断是否到期而深拷贝全部任务内容。
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Any, Callable, Optional

from . import config, store as store_module
from .automation import Runner
from .config import friendly_error, iso_datetime, now_local, parse_datetime
from .store import TaskStore


class Scheduler:
    def __init__(self, store: TaskStore, emit: Callable[[str, Any], None], exclude_hwnd: int = 0) -> None:
        self.store = store
        self.emit = emit
        self.exclude_hwnd = int(exclude_hwnd or 0)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running: set[str] = set()
        self._running_lock = threading.Lock()
        self._window_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # -- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="cuerelay-scheduler", daemon=True)
        self._thread.start()
        self.emit("log", ("info", "调度引擎已启动。"))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def is_busy(self) -> bool:
        with self._running_lock:
            return bool(self._running)

    def _catch_up(self) -> int:
        try:
            return max(0, int(self.store.setting("catch_up_seconds", config.SCHEDULE_CATCH_UP_SECONDS)))
        except (TypeError, ValueError):
            return config.SCHEDULE_CATCH_UP_SECONDS

    # -- 触发 ----------------------------------------------------------

    def trigger(self, task_id: str, reason: str = "manual") -> bool:
        with self._running_lock:
            if task_id in self._running:
                return False
            self._running.add(task_id)
        threading.Thread(
            target=self._execute,
            args=(task_id, reason),
            name=f"cuerelay-run-{task_id}",
            daemon=True,
        ).start()
        return True

    def test_paste(self, content: str) -> bool:
        """只粘贴不发送，用来验证识别结果；和正式任务共用同一把窗口锁。"""

        if not str(content or "").strip():
            return False
        threading.Thread(
            target=self._run_paste_test,
            args=(content,),
            name="cuerelay-paste-test",
            daemon=True,
        ).start()
        return True

    def _run_paste_test(self, content: str) -> None:
        if not self._window_lock.acquire(timeout=120):
            self.emit("log", ("error", "另一个任务正在执行，粘贴测试已取消。"))
            return
        gate: Optional[threading.Event] = None
        try:
            gate = threading.Event()
            self.emit("prepare", gate)
            gate.wait(0.8)
            settings = self.store.settings()
            Runner(
                settings,
                lambda level, message: self.emit("log", (level, message)),
                exclude_hwnd=self.exclude_hwnd,
                emit=self.emit,
            ).paste_only(content)
            self._persist_composer(settings)
        except Exception as exc:
            self.emit("log", ("error", f"粘贴测试失败：{friendly_error(exc)}"))
        finally:
            if gate is not None and not gate.is_set():
                gate.set()
            self.emit("restore", None)
            self._window_lock.release()

    # -- 主循环 --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as exc:
                self.emit("log", ("error", f"调度异常：{friendly_error(exc)}"))
            self._wake.wait(0.5)
            self._wake.clear()

    def _scan(self) -> None:
        now = now_local()
        catch_up = self._catch_up()
        for task_id, next_run, enabled in self.store.index():
            if self._stop.is_set():
                return
            if not enabled or next_run is None or next_run > now:
                continue
            if (now - next_run).total_seconds() > catch_up:
                self._skip_overdue(task_id, now, catch_up)
            else:
                self.trigger(task_id, "schedule")

    def _skip_overdue(self, task_id: str, now: _dt.datetime, catch_up: int) -> None:
        task = self.store.get(task_id)
        if not task or not task.get("enabled"):
            return
        message = config.missed_schedule_message(catch_up)
        if task.get("schedule") == "once":
            self.store.update_task(
                task_id,
                enabled=False,
                next_run=None,
                status="expired",
                last_error=message,
            )
        else:
            future = store_module.advance_next_run_past_now(task, now)
            if future is None:
                return
            self.store.update_task(
                task_id,
                next_run=iso_datetime(future),
                status="waiting",
                last_error=message,
            )
        self.emit("log", ("warning", f"已跳过：{task.get('name') or task_id}（{message}）"))
        self.emit("refresh", None)

    # -- 执行 ----------------------------------------------------------

    def _advance_after_start(self, task: dict[str, Any], run_time: _dt.datetime) -> None:
        updates: dict[str, Any] = {"status": "running", "last_error": ""}
        schedule = task.get("schedule")
        if schedule == "once":
            updates["enabled"] = False
            updates["next_run"] = None
        elif schedule == "daily":
            candidate = parse_datetime(task.get("next_run")) or run_time
            while candidate <= run_time:
                candidate += _dt.timedelta(days=1)
            updates["next_run"] = iso_datetime(candidate)
        else:
            minutes = max(1, int(task.get("interval_minutes", 60) or 60))
            updates["next_run"] = iso_datetime(run_time + _dt.timedelta(minutes=minutes))
        self.store.update_task(str(task.get("id")), **updates)

    def _execute(self, task_id: str, reason: str) -> None:
        started = time.monotonic()
        if not self._window_lock.acquire(timeout=180):
            with self._running_lock:
                self._running.discard(task_id)
            self.emit("log", ("error", "上一个任务长时间未结束，本次执行已取消。"))
            return

        gate: Optional[threading.Event] = None
        try:
            task = self.store.get(task_id)
            if not task:
                return

            run_time = now_local()
            if reason == "schedule":
                self._advance_after_start(task, run_time)
            else:
                self.store.update_task(task_id, status="running", last_error="")
            self.emit("refresh", None)

            gate = threading.Event()
            self.emit("prepare", gate)
            gate.wait(0.8)

            settings = self.store.settings()
            runner = Runner(
                settings,
                lambda level, message: self.emit("log", (level, message)),
                exclude_hwnd=self.exclude_hwnd,
                emit=self.emit,
            )
            runner.run_task(task)

            self._persist_composer(settings)
            self.store.record_run(task_id, True, "", int((time.monotonic() - started) * 1000))
            self.emit("refresh", None)
        except Exception as exc:
            message = friendly_error(exc)
            elapsed = int((time.monotonic() - started) * 1000)
            task = self.store.get(task_id)
            self.store.record_run(task_id, False, message, elapsed)
            if reason == "schedule" and task and task.get("schedule") == "once":
                self.store.update_task(task_id, enabled=False, next_run=None)
            self.emit("log", ("error", f"任务失败：{message}"))
            self.emit("refresh", None)
        finally:
            if gate is not None and not gate.is_set():
                gate.set()
            self.emit("restore", None)
            with self._running_lock:
                self._running.discard(task_id)
            self._window_lock.release()

    def _persist_composer(self, settings: dict[str, Any]) -> None:
        """把运行期识别到的输入框位置写回，供下次识别失败时兜底。"""

        rect = settings.get("composer_rect")
        frame = settings.get("composer_frame")
        if not rect or not frame:
            return
        current = self.store.settings()
        if current.get("composer_rect") == rect and current.get("composer_frame") == frame:
            return
        self.store.update_settings(composer_rect=rect, composer_frame=frame)
