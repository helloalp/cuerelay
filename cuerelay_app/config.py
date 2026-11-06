# -*- coding: utf-8 -*-
"""路径、常量与时间工具。

本模块只依赖标准库，任何其它模块都可以安全导入。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Optional

APP_NAME = "CueRelay"
APP_VERSION = "2.5.0"

# cuerelay.py 位于项目根目录，本文件位于 cuerelay_app/ 下。
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = BASE_DIR / "cuerelay_data.json"
LOG_PATH = BASE_DIR / "cuerelay.log"
CRASH_PATH = BASE_DIR / "cuerelay_crash.log"
ICON_PATH = BASE_DIR / "assets" / "cuerelay.ico"

# 调度线程偶尔会因为系统时间校准、短暂休眠或负载跳过某个轮询点。
# 只在很短的窗口内补执行，避免多年以前的旧任务在启动时突然执行。
SCHEDULE_CATCH_UP_SECONDS = 300

# 调度循环最长睡眠时间，保证新增/修改任务后能迅速生效。
SCHEDULER_MAX_WAIT_SECONDS = 1.0

# 默认的目标窗口关键词，可在设置中修改。
TARGET_DEFAULT_KEYWORDS = "codex, chatgpt, openai"

# 用于识别目标窗口的窗口类名/进程名不参与匹配，只匹配标题与进程名。
SELF_TITLE_TOKENS = ("cuerelay", "定时输入助手")


def now_local() -> _dt.datetime:
    """本机当前时间，去掉微秒；任务文件统一使用无时区的本地时间。"""

    return _dt.datetime.now().replace(microsecond=0)


def iso_datetime(value: _dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat(timespec="seconds")


def parse_datetime(value: Any) -> Optional[_dt.datetime]:
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value.replace(tzinfo=None, microsecond=0)
    try:
        parsed = _dt.datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None, microsecond=0)


def format_datetime(value: Any, empty: str = "未设置") -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return empty
    return parsed.strftime("%Y-%m-%d %H:%M")


def format_short_datetime(value: Any, empty: str = "未设置") -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return empty
    today = now_local().date()
    if parsed.date() == today:
        return parsed.strftime("今天 %H:%M")
    if parsed.date() == today + _dt.timedelta(days=1):
        return parsed.strftime("明天 %H:%M")
    return parsed.strftime("%m-%d %H:%M")


def friendly_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def deep_copy(value: Any) -> Any:
    """任务数据都是纯 JSON 结构，用序列化做深拷贝最省事也最安全。"""

    return json.loads(json.dumps(value, ensure_ascii=False))


def missed_schedule_message(seconds: int = SCHEDULE_CATCH_UP_SECONDS) -> str:
    minutes = max(1, int(seconds) // 60)
    return f"错过执行时间超过 {minutes} 分钟，已跳过本次执行。"
