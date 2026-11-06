# -*- coding: utf-8 -*-
"""任务与设置的持久化。

数据结构版本 2 与 1.2.x 的旧文件兼容：旧文件读入后会自动迁移，
原文件不会被覆盖成不可读的内容（迁移前会另存一份 .v1 备份）。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import config
from .config import deep_copy, iso_datetime, now_local, parse_datetime

SCHEMA_VERSION = 3

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
    # 锁定的目标窗口。填了 hwnd 就只认那一个窗口，不看关键词；标题是它的
    # 备份——窗口关了再开，句柄会变，但标题还在。
    "target_window_hwnd": 0,
    "target_window_title": "",
    # 执行前把目标窗口摆成固定大小。窗口可以是任意大小，识别会跟着走；
    # 但"先摆成已知形状"比"什么都能适应"更稳，尤其是定时任务。
    "force_window_size": False,
    # 空或 "max" 表示最大化，"1600x1000" 表示普通窗口 + 指定尺寸。
    "target_window_size": "",
    "anchor_composer": None,
    "anchor_send": None,
    "anchor_search": None,
    "composer_rect": None,
    "composer_frame": None,
    "recognition_enabled": True,
    "verify_paste": True,
    "verify_send": True,
    "strict_conversation": True,
    # 执行前如果目标应用没开，自动把它拉起来再发。
    "auto_launch_target": True,
    # 留空表示按 Codex 处理；填了 exe 路径就按普通桌面程序启动。
    "target_path": "",
    "codex_aumid": "",
    "v2ray_path": "",
    "launch_v2ray_first": True,
    # 执行时在目标窗口上盖一层框线浮层，标出识别到的输入框与发送按钮。
    "show_overlay": True,
    "default_action": "click",
    # 空字符串表示"按屏幕大小自动决定"，避免把物理像素当成设计尺寸。
    "window_size": "",
    "calibrated_at": "",
    # 发送成功后在浮层上停一会儿，让用户看清结果再消失。
    "overlay_linger_seconds": 1.2,
    # 命令面板的历史搜索词，回车后下次还能直接用。
    "last_palette_query": "",
    # 在任何程序里按 Ctrl+Alt+K 把窗口叫回来并打开命令面板。
    "global_hotkey": True,
    # 托盘图标。关掉之后"收进托盘"就没有意义了，关闭动作会退化成退出。
    "tray_enabled": True,
    # 点关闭时做什么：tray / minimize / ask / quit
    "close_action": "tray",
    # 点最小化时也收进托盘，而不是留在任务栏。
    "minimize_to_tray": True,
    # 配合开机自启：开机时只留托盘图标，不弹窗口。
    "start_hidden": False,
}

# 这些键在配置文件里必须是真正的布尔值。手改过的 JSON 里 "false" 是个
# 非空字符串，`bool("false")` 为真——所以不能只靠 bool() 转换。
BOOL_SETTINGS = (
    "always_on_top",
    "restore_target_geometry",
    "recognition_enabled",
    "verify_paste",
    "verify_send",
    "strict_conversation",
    "auto_launch_target",
    "launch_v2ray_first",
    "show_overlay",
    "force_window_size",
    "global_hotkey",
    "tray_enabled",
    "minimize_to_tray",
    "start_hidden",
)

# 关闭窗口时的动作。顺序就是界面上按钮的顺序。
CLOSE_ACTIONS = ("tray", "minimize", "ask", "quit")
CLOSE_ACTION_LABELS = {
    "tray": "收进托盘",
    "minimize": "最小化",
    "ask": "每次询问",
    "quit": "直接退出",
}


def normalize_close_action(value: Any) -> str:
    """认不出的值按默认处理，不抛异常：这个字段来自手改的配置文件。"""

    text = str(value or "").strip().lower()
    return text if text in CLOSE_ACTIONS else "tray"

SCHEDULE_VALUES = ("once", "daily", "interval")
ACTION_VALUES = ("click", "enter")
STATUS_VALUES = ("waiting", "running", "failed", "paused", "done", "expired")
# 任务类型：发送提示词 / 启动应用 / 关闭应用。后两种不需要提示词内容。
KIND_VALUES = ("prompt", "launch", "close")

KIND_LABELS = {
    "prompt": "发送提示词",
    "launch": "启动应用",
    "close": "关闭应用",
}

KIND_SHORT = {
    "prompt": "提示词",
    "launch": "启动",
    "close": "关闭",
}

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


def _coerce_bool(value: Any, default: bool) -> bool:
    """配置文件是手改过的 JSON，字符串 "false" 不能被当成 True。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _coerce_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("launch", "start", "open", "启动"):
        return "launch"
    if text in ("close", "stop", "quit", "关闭"):
        return "close"
    return "prompt"


def normalize_task(item: dict[str, Any]) -> dict[str, Any]:
    schedule = _coerce_schedule(item.get("schedule"))
    next_run = parse_datetime(item.get("next_run"))
    created = parse_datetime(item.get("created_at")) or now_local()
    return {
        "id": str(item.get("id") or uuid.uuid4().hex[:10]),
        "name": str(item.get("name") or "未命名任务")[:80],
        "kind": _coerce_kind(item.get("kind")),
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
        # 失败重试只对"还没点发送"的失败生效，见 scheduler。
        "retry_limit": _coerce_int(item.get("retry_limit"), 0, 0, 5),
        "retry_delay": _coerce_int(item.get("retry_delay"), 30, 5, 600),
        "created_at": iso_datetime(created),
        "updated_at": iso_datetime(parse_datetime(item.get("updated_at")) or created),
    }


# ---------------------------------------------------------------------------
# 片段
# ---------------------------------------------------------------------------


def normalize_snippet(item: dict[str, Any]) -> dict[str, Any]:
    created = parse_datetime(item.get("created_at")) or now_local()
    content = str(item.get("content") or "")
    name = str(item.get("name") or "").strip()
    if not name:
        # 片段常常是"先粘一段话，再想名字"。没名字就用内容开头顶上，
        # 列表里至少能认出是哪一条。
        flat = " ".join(content.split())
        name = flat[:24] + ("…" if len(flat) > 24 else "") if flat else "未命名片段"
    return {
        "id": str(item.get("id") or uuid.uuid4().hex[:10]),
        "name": name[:60],
        "content": content,
        "use_count": _coerce_int(item.get("use_count"), 0, 0, 10_000_000),
        "created_at": iso_datetime(created),
        "updated_at": iso_datetime(parse_datetime(item.get("updated_at")) or created),
    }


# ---------------------------------------------------------------------------
# 执行历史
# ---------------------------------------------------------------------------

# 历史只是"最近发生了什么"，不是审计日志。留太多会让数据文件越来越重，
# 而且列表本身也没人会往下翻几百条。
HISTORY_LIMIT = 200


def normalize_history(item: dict[str, Any]) -> dict[str, Any]:
    at = parse_datetime(item.get("at")) or now_local()
    return {
        "id": str(item.get("id") or uuid.uuid4().hex[:10]),
        "task_id": str(item.get("task_id") or ""),
        "task_name": str(item.get("task_name") or "")[:80],
        "kind": _coerce_kind(item.get("kind")),
        "ok": bool(item.get("ok", False)),
        "message": str(item.get("message") or "")[:400],
        "duration_ms": _coerce_int(item.get("duration_ms"), 0, 0, 86_400_000),
        "at": iso_datetime(at),
        # 自动重试的第几次；0 表示第一次执行。手动执行也记 0。
        "attempt": _coerce_int(item.get("attempt"), 0, 0, 99),
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
    if state == "failed":
        return "失败"
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


def kind_label(task: dict[str, Any]) -> str:
    return KIND_LABELS.get(str(task.get("kind") or "prompt"), KIND_LABELS["prompt"])


def is_lifecycle(task: dict[str, Any]) -> bool:
    """启动/关闭这类任务不需要提示词，执行流程也不同。"""

    return str(task.get("kind") or "prompt") in ("launch", "close")


def content_snippet(task: dict[str, Any], limit: int = 46) -> str:
    """列表里用来区分同名任务的一行内容摘要。"""

    text = " ".join(str(task.get("content") or "").split())
    if not text:
        if is_lifecycle(task):
            # 开关类任务没有提示词，用类型名占住这一行，避免列表高低不一。
            return kind_label(task)
        return "（空内容）"
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 分组与整理
# ---------------------------------------------------------------------------

# 列表按这三组展示，避免几十个任务平铺在一起分不清轻重。
GROUP_ORDER: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("active", "进行中", ("running", "waiting")),
    ("failed", "失败", ("failed",)),
    ("idle", "已停止", ("paused", "done", "expired")),
)


def state_group(task: dict[str, Any]) -> str:
    state = task_state(task)
    for key, _label, states in GROUP_ORDER:
        if state in states:
            return key
    return "idle"


def group_label(key: str) -> str:
    for group_key, label, _states in GROUP_ORDER:
        if group_key == key:
            return label
    return "其它"


def content_signature(task: dict[str, Any]) -> str:
    """判重用的归一化内容：忽略大小写与空白差异。"""

    return re.sub(r"\s+", " ", str(task.get("content") or "").strip()).lower()


def duplicate_groups(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内容完全相同的任务归为一组，每组保留最近修改的一条。

    返回 [{"content": 归一化内容, "keep": 保留的任务, "extra": [可删除的任务]}]，
    按可删除数量从多到少排序。
    """

    buckets: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        key = content_signature(task)
        if not key:
            continue
        buckets.setdefault(key, []).append(task)

    groups: list[dict[str, Any]] = []
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        items.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        groups.append({"content": key, "keep": items[0], "extra": items[1:]})
    groups.sort(key=lambda group: (-len(group["extra"]), group["content"]))
    return groups


def stopped_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [task for task in tasks if state_group(task) == "idle"]


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "settings": deep_copy(DEFAULT_SETTINGS),
            "tasks": [],
            "snippets": [],
            "history": [],
        }
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
        settings["show_overlay"] = _coerce_bool(settings.get("show_overlay"), True)
        # 所有布尔设置统一走 _coerce_bool。手改配置留下的 "false" 字符串
        # 在 Python 里是真值，会让"关闭自动拉起"这种开关失效。
        for key in BOOL_SETTINGS:
            settings[key] = _coerce_bool(settings.get(key), bool(DEFAULT_SETTINGS.get(key)))
        settings["close_action"] = normalize_close_action(settings.get("close_action"))
        try:
            linger = float(settings.get("overlay_linger_seconds", 1.2))
        except (TypeError, ValueError):
            linger = 1.2
        settings["overlay_linger_seconds"] = round(max(0.0, min(5.0, linger)), 2)

        tasks = [normalize_task(item) for item in raw_tasks if isinstance(item, dict)]
        raw_snippets = raw.get("snippets") if isinstance(raw.get("snippets"), list) else []
        snippets = [normalize_snippet(item) for item in raw_snippets if isinstance(item, dict)]
        raw_history = raw.get("history") if isinstance(raw.get("history"), list) else []
        history = [normalize_history(item) for item in raw_history if isinstance(item, dict)]
        return {
            "version": SCHEMA_VERSION,
            "settings": settings,
            "tasks": tasks,
            "snippets": snippets,
            "history": history[:HISTORY_LIMIT],
        }

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

    def delete_many(self, task_ids: list[str]) -> int:
        """批量删除，返回实际删除的数量。"""

        targets = {str(task_id) for task_id in task_ids}
        if not targets:
            return 0
        with self._lock:
            before = len(self.data.get("tasks", []))
            self.data["tasks"] = [
                task for task in self.data.get("tasks", []) if str(task.get("id")) not in targets
            ]
            removed = before - len(self.data["tasks"])
            if removed:
                self._save_locked()
            return removed

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

    # -- 片段 ----------------------------------------------------------

    def snippets(self) -> list[dict[str, Any]]:
        with self._lock:
            return deep_copy(self.data.get("snippets", []))

    def get_snippet(self, snippet_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not snippet_id:
            return None
        with self._lock:
            for item in self.data.get("snippets", []):
                if item.get("id") == snippet_id:
                    return deep_copy(item)
        return None

    def add_snippet(self, snippet: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            normalized = normalize_snippet(snippet)
            normalized["id"] = str(snippet.get("id") or uuid.uuid4().hex[:10])
            self.data.setdefault("snippets", []).append(normalized)
            self._save_locked()
            return deep_copy(normalized)

    def replace_snippet(self, snippet_id: str, snippet: dict[str, Any]) -> bool:
        with self._lock:
            for index, old in enumerate(self.data.get("snippets", [])):
                if old.get("id") == snippet_id:
                    merged = {**old, **snippet, "id": snippet_id}
                    self.data["snippets"][index] = normalize_snippet(merged)
                    self._save_locked()
                    return True
        return False

    def delete_snippet(self, snippet_id: str) -> bool:
        with self._lock:
            before = len(self.data.get("snippets", []))
            self.data["snippets"] = [
                item for item in self.data.get("snippets", []) if item.get("id") != snippet_id
            ]
            if len(self.data["snippets"]) != before:
                self._save_locked()
                return True
        return False

    def touch_snippet(self, snippet_id: str) -> None:
        """记一次使用。片段列表按使用次数排序，常用的会浮上来。"""

        with self._lock:
            for item in self.data.get("snippets", []):
                if item.get("id") == snippet_id:
                    item["use_count"] = int(item.get("use_count", 0)) + 1
                    item["updated_at"] = iso_datetime(now_local())
                    self._save_locked()
                    return

    # -- 历史 ----------------------------------------------------------

    def history(self, limit: int = 0) -> list[dict[str, Any]]:
        """最近执行记录，新的在前。"""

        with self._lock:
            items = deep_copy(self.data.get("history", []))
        return items[:limit] if limit > 0 else items

    def add_history(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            normalized = normalize_history(entry)
            items = self.data.setdefault("history", [])
            items.insert(0, normalized)
            del items[HISTORY_LIMIT:]
            self._save_locked()
            return deep_copy(normalized)

    def clear_history(self) -> int:
        with self._lock:
            count = len(self.data.get("history", []))
            self.data["history"] = []
            self._save_locked()
            return count

    def finish_run(
        self,
        task: dict[str, Any],
        ok: bool,
        message: str,
        duration_ms: int,
        attempt: int = 0,
    ) -> dict[str, Any]:
        """一次执行的收尾：更新任务状态，同时留下一条历史。

        两件事必须一起做。分开写的话，某个调用点漏了其中一个，就会出现
        「任务显示成功但历史里没有」这种对不上的状态。
        """

        task_id = str(task.get("id") or "")
        entry = self.add_history(
            {
                "task_id": task_id,
                "task_name": str(task.get("name") or ""),
                "kind": str(task.get("kind") or "prompt"),
                "ok": ok,
                "message": message,
                "duration_ms": duration_ms,
                "attempt": attempt,
            }
        )
        self.record_run(task_id, ok, message, duration_ms)
        return entry
