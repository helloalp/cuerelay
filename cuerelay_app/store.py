# -*- coding: utf-8 -*-
"""任务与设置的持久化。

数据结构版本 2 与 1.2.x 的旧文件兼容：旧文件读入后会自动迁移，
原文件不会被覆盖成不可读的内容（迁移前会另存一份 .v1 备份）。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import config
from .config import deep_copy, iso_datetime, now_local, parse_datetime

SCHEMA_VERSION = 2

DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": "light",
    "always_on_top": True,
    "restore_target_geometry": True,
    "step_delay_seconds": 1.2,
    "catch_up_seconds": config.SCHEDULE_CATCH_UP_SECONDS,
    "target_keywords": config.TARGET_DEFAULT_KEYWORDS,
    "target_title": "",
    "target_process": "",
    "target_geometry": None,
    "anchor_composer": None,
    "anchor_send": None,
    "anchor_search": None,
    "composer_rect": None,
    "composer_frame": None,
    "recognition_enabled": True,
    "verify_paste": True,
    "verify_send": True,
    "strict_conversation": True,
    "default_action": "click",
    # 空字符串表示"按屏幕大小自动决定"，避免把物理像素当成设计尺寸。
    "window_size": "",
    "calibrated_at": "",
}

SCHEDULE_VALUES = ("once", "daily", "interval")
ACTION_VALUES = ("click", "enter")
STATUS_VALUES = ("waiting", "running", "failed", "paused", "done", "expired")

LEGACY_STATUS = {
    "等待中": "waiting",
    "运行中": "running",
    "失败": "failed",
    "已暂停": "paused",
    "已完成": "done",
    "已过期": "expired",
}

STATE_LABELS = {
    "waiting": "等待中",
    "running": "运行中",
    "failed": "失败",
    "paused": "已暂停",
    "done": "已完成",
    "expired": "已过期",
}

STATE_TONE = {
    "waiting": "text_3",
    "running": "accent",
    "failed": "danger",
    "paused": "warning",
    "done": "success",
    "expired": "text_3",
}


def _coerce_action(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("enter", "回车", "按 enter 发送"):
        return "enter"
    if text in ("button", "click", "点击", "点击发送"):
        return "click"
    return "click"


def _coerce_status(value: Any) -> str:
    text = str(value or "").strip()
    if text in STATUS_VALUES:
        return text
    return LEGACY_STATUS.get(text, "waiting")


def _coerce_schedule(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in SCHEDULE_VALUES else "once"


def _coerce_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalize_task(item: dict[str, Any]) -> dict[str, Any]:
    schedule = _coerce_schedule(item.get("schedule"))
    next_run = parse_datetime(item.get("next_run"))
    created = parse_datetime(item.get("created_at")) or now_local()
    return {
        "id": str(item.get("id") or uuid.uuid4().hex[:10]),
        "name": str(item.get("name") or "未命名任务")[:80],
        "content": str(item.get("content") or ""),
        "conversation_title": str(item.get("conversation_title") or "")[:200],
        "schedule": schedule,
        "next_run": iso_datetime(next_run) if next_run else None,
        "interval_minutes": _coerce_int(item.get("interval_minutes"), 60, 1, 525600),
        "action": _coerce_action(item.get("action")),
        "enabled": bool(item.get("enabled", True)),
        "status": _coerce_status(item.get("status")),
        "last_run": iso_datetime(parse_datetime(item.get("last_run"))) if parse_datetime(item.get("last_run")) else None,
        "last_error": str(item.get("last_error") or "")[:400],
        "run_count": _coerce_int(item.get("run_count"), 0, 0, 10_000_000),
        "last_duration_ms": _coerce_int(item.get("last_duration_ms"), 0, 0, 86_400_000),
        "created_at": iso_datetime(created),
        "updated_at": iso_datetime(parse_datetime(item.get("updated_at")) or created),
    }


def advance_next_run_past_now(task: dict[str, Any], now: _dt.datetime) -> Optional[_dt.datetime]:
    """把重复任务推进到 now 之后，跳过已经过期的执行点。"""

    candidate = parse_datetime(task.get("next_run"))
    if candidate is None:
        return None

    schedule = task.get("schedule")
    if schedule == "daily":
        while candidate <= now:
            candidate += _dt.timedelta(days=1)
        return candidate

    if schedule == "interval":
        minutes = max(1, int(task.get("interval_minutes", 60) or 60))
        interval = _dt.timedelta(minutes=minutes)
        if candidate <= now:
            steps = int((now - candidate).total_seconds() // interval.total_seconds()) + 1
            candidate += interval * max(1, steps)
        while candidate <= now:
            candidate += interval
        return candidate

    return None


def task_state(task: dict[str, Any]) -> str:
    status = str(task.get("status") or "waiting")
    if status == "running":
        return "running"
    if status == "expired":
        return "expired"
    if status == "failed":
        return "failed"
    if not task.get("enabled"):
        if task.get("schedule") == "once" and task.get("last_run"):
            return "done"
        return "paused"
    if task.get("schedule") == "once" and task.get("last_run"):
        return "done"
    return "waiting"


def schedule_text(task: dict[str, Any]) -> str:
    schedule = task.get("schedule")
    if schedule == "daily":
        return "每天"
    if schedule == "interval":
        return f"每 {int(task.get('interval_minutes', 60) or 60)} 分钟"
    return "单次"


def next_run_text(task: dict[str, Any]) -> str:
    state = task_state(task)
    if state == "expired":
        return "已跳过"
    if state == "paused":
        return "已暂停"
    if state == "done":
        return "已完成"
    if state == "running":
        return "正在执行"
    return config.format_short_datetime(task.get("next_run"))


def task_summary(task: dict[str, Any]) -> str:
    return f"{schedule_text(task)} · {next_run_text(task)}"


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": SCHEMA_VERSION, "settings": deep_copy(DEFAULT_SETTINGS), "tasks": []}
        self.load()

    # -- 读写 ----------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            raw: dict[str, Any] = {}
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
                else:
                    raise ValueError("任务文件不是 JSON 对象")
            except FileNotFoundError:
                raw = {}
            except Exception:
                raw = self._quarantine()

            migrated = self._migrate(raw)
            self.data = migrated
            if self._fix_overdue_locked():
                self._save_locked()

    def _quarantine(self) -> dict[str, Any]:
        """损坏文件改名保留，避免用户数据被静默覆盖。"""

        try:
            broken = self.path.with_name(f"{self.path.stem}.broken-{int(time.time())}{self.path.suffix}")
            self.path.replace(broken)
        except Exception:
            pass
        return {}

    def _migrate(self, raw: dict[str, Any]) -> dict[str, Any]:
        version = raw.get("version")
        try:
            version_number = int(version)
        except (TypeError, ValueError):
            version_number = 1 if raw.get("tasks") else SCHEMA_VERSION

        raw_settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        raw_tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []

        if version_number < SCHEMA_VERSION and raw:
            self._backup_legacy()

        settings = deep_copy(DEFAULT_SETTINGS)
        for key, value in raw_settings.items():
            if key in settings:
                settings[key] = value
            elif key == "always_on_top":
                settings["always_on_top"] = bool(value)

        if version_number < 2:
            # 1.2.x 的锚点以窗口外框为坐标系，标记 space 让解析器按旧坐标换算。
            settings["anchor_composer"] = self._legacy_anchor(raw_settings.get("input_anchor"))
            settings["anchor_send"] = self._legacy_anchor(raw_settings.get("send_anchor"))
            settings["anchor_search"] = self._legacy_anchor(raw_settings.get("conversation_search_anchor"))
            settings["target_geometry"] = raw_settings.get("target_geometry")
            settings["target_title"] = str(raw_settings.get("target_title") or "")
            settings["target_process"] = str(raw_settings.get("target_process") or "")
            settings["step_delay_seconds"] = raw_settings.get("step_delay_seconds", settings["step_delay_seconds"])
            settings["restore_target_geometry"] = bool(
                raw_settings.get("restore_target_geometry", settings["restore_target_geometry"])
            )
            settings["always_on_top"] = bool(raw_settings.get("always_on_top", settings["always_on_top"]))

        try:
            delay = float(settings.get("step_delay_seconds", 1.2))
        except (TypeError, ValueError):
            delay = 1.2
        settings["step_delay_seconds"] = round(max(0.3, min(10.0, delay)), 2)
        settings["catch_up_seconds"] = _coerce_int(
            settings.get("catch_up_seconds"), config.SCHEDULE_CATCH_UP_SECONDS, 0, 3600
        )

        tasks = [normalize_task(item) for item in raw_tasks if isinstance(item, dict)]
        return {"version": SCHEMA_VERSION, "settings": settings, "tasks": tasks}

    @staticmethod
    def _legacy_anchor(value: Any) -> Optional[dict[str, Any]]:
        if not isinstance(value, dict):
            return None
        try:
            anchor = {
                "rx": float(value.get("rx", 0.5)),
                "ry": float(value.get("ry", 0.5)),
                "space": "window",
            }
        except (TypeError, ValueError):
            return None
        if "screen_x" in value and "screen_y" in value:
            anchor["screen_x"] = int(value["screen_x"])
            anchor["screen_y"] = int(value["screen_y"])
        return anchor

    def _backup_legacy(self) -> None:
        try:
            backup = self.path.with_name(f"{self.path.stem}.v1{self.path.suffix}")
            if self.path.exists() and not backup.exists():
                backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass

    def _fix_overdue_locked(self) -> bool:
        changed = False
        now = now_local()
        catch_up = int(self.data["settings"].get("catch_up_seconds", config.SCHEDULE_CATCH_UP_SECONDS) or 0)
        for task in self.data.get("tasks", []):
            if not task.get("enabled"):
                continue
            next_run = parse_datetime(task.get("next_run"))
            if next_run is None or next_run > now:
                continue
            if (now - next_run).total_seconds() <= catch_up:
                continue
            message = config.missed_schedule_message(catch_up)
            if task.get("schedule") == "once":
                task["enabled"] = False
                task["next_run"] = None
                task["status"] = "expired"
            else:
                future = advance_next_run_past_now(task, now)
                if future is None:
                    continue
                task["next_run"] = iso_datetime(future)
                task["status"] = "waiting"
            task["last_error"] = message
            changed = True
        return changed

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    # -- 设置 ----------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return deep_copy(self.data.get("settings", {}))

    def setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return deep_copy(self.data.get("settings", {}).get(key, default))

    def update_settings(self, **updates: Any) -> None:
        with self._lock:
            self.data.setdefault("settings", {}).update(updates)
            self._save_locked()

    # -- 任务 ----------------------------------------------------------

    def tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return deep_copy(self.data.get("tasks", []))

    def index(self) -> list[tuple[str, Optional[_dt.datetime], bool]]:
        """调度线程专用：只读取必要字段，不做整表深拷贝。"""

        with self._lock:
            return [
                (str(task.get("id")), parse_datetime(task.get("next_run")), bool(task.get("enabled")))
                for task in self.data.get("tasks", [])
            ]

    def get(self, task_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not task_id:
            return None
        with self._lock:
            for task in self.data.get("tasks", []):
                if task.get("id") == task_id:
                    return deep_copy(task)
        return None

    def add(self, task: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            normalized = normalize_task(task)
            normalized["id"] = str(task.get("id") or uuid.uuid4().hex[:10])
            self.data.setdefault("tasks", []).append(normalized)
            self._save_locked()
            return deep_copy(normalized)

    def replace(self, task_id: str, task: dict[str, Any]) -> bool:
        with self._lock:
            for index, old in enumerate(self.data.get("tasks", [])):
                if old.get("id") == task_id:
                    merged = {**old, **task, "id": task_id}
                    self.data["tasks"][index] = normalize_task(merged)
                    self._save_locked()
                    return True
        return False

    def update_task(self, task_id: str, **updates: Any) -> bool:
        with self._lock:
            for task in self.data.get("tasks", []):
                if task.get("id") == task_id:
                    task.update(updates)
                    task["updated_at"] = iso_datetime(now_local())
                    self._save_locked()
                    return True
        return False

    def delete(self, task_id: str) -> bool:
        with self._lock:
            before = len(self.data.get("tasks", []))
            self.data["tasks"] = [task for task in self.data.get("tasks", []) if task.get("id") != task_id]
            if len(self.data["tasks"]) != before:
                self._save_locked()
                return True
        return False

    def record_run(self, task_id: str, ok: bool, message: str, duration_ms: int) -> None:
        with self._lock:
            for task in self.data.get("tasks", []):
                if task.get("id") != task_id:
                    continue
                task["last_run"] = iso_datetime(now_local())
                task["last_duration_ms"] = int(max(0, duration_ms))
                task["run_count"] = int(task.get("run_count", 0)) + (1 if ok else 0)
                task["last_error"] = "" if ok else str(message)[:400]
                if ok:
                    task["status"] = "waiting"
                else:
                    task["status"] = "failed"
                task["updated_at"] = iso_datetime(now_local())
                self._save_locked()
                return
