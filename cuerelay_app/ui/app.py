# -*- coding: utf-8 -*-
"""主窗口。

三个视图：任务、日志、设置。左侧窄导航栏常驻，右侧内容区切换。
没有装饰性文案、没有 emoji、没有动画，只有层级、留白和可读的字号。
"""

from __future__ import annotations

import datetime as _dt
import os
import queue
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from .. import config, locator, store as store_module, winapi
from ..automation import AutomationError
from ..config import friendly_error, iso_datetime, now_local, parse_datetime
from ..hotkey import VirtualCapture
from ..scheduler import Scheduler
from ..store import (
    TaskStore,
    advance_next_run_past_now,
    content_snippet,
    next_run_text,
    task_state,
)
from .calibrate import CalibrationDialog
from .overlay import Overlay, apply_event
from .theme import Theme
from .tidy import TidyDialog
from .widgets import Button, Field, ScrollArea, Segmented, Switch, TextArea, card, dispatch_wheel

ACTION_OPTIONS = (("click", "点击发送"), ("enter", "Enter 发送"))
SCHEDULE_OPTIONS = (("once", "单次"), ("daily", "每天"), ("interval", "间隔"))
DELAY_OPTIONS = (("0.8", "0.8 秒"), ("1.2", "1.2 秒"), ("1.8", "1.8 秒"), ("2.5", "2.5 秒"))
THEME_OPTIONS = (("light", "浅色"), ("dark", "深色"))
FILTER_OPTIONS = (("all", "全部"), ("info", "信息"), ("success", "成功"), ("warning", "警告"), ("error", "错误"))

# 分组默认展开状态。失败要立刻看到，"已停止"往往是几十条历史，默认收起，
# 否则一打开就是一大片同名的旧任务。
GROUP_DEFAULT_OPEN = {"active": True, "failed": True, "idle": False}


def _clip(text: str, limit: int) -> str:
    """压成一行并截断，用于状态行这类只有一行的位置。"""

    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _empty_draft() -> dict[str, Any]:
    now = now_local()
    return {
        "id": None,
        "name": "",
        "content": "",
        "conversation": "",
        "schedule": "once",
        "date": now.strftime("%Y-%m-%d"),
        "time": (now + _dt.timedelta(minutes=5)).strftime("%H:%M"),
        "interval": "60",
        "action": "click",
        "enabled": True,
    }


class App:
    def __init__(self, root: tk.Tk, store: TaskStore, mutex_handle: Any = None) -> None:
        self.root = root
        self.store = store
        self.mutex_handle = mutex_handle
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.root_hwnd = 0
        self.view = "tasks"
        self.selected_id: Optional[str] = None
        self.draft = _empty_draft()
        self._baseline: Optional[tuple] = None
        self._row_widgets: dict[str, dict[str, Any]] = {}
        self._list_signature: Optional[tuple] = None
        self._group_open: dict[str, bool] = {}
        self._log_lines: list[tuple[str, str]] = []
        self._log_filter = "all"
        self._calibrated_at = str(self.store.setting("calibrated_at") or "")

        settings = self.store.settings()
        self.theme = Theme(str(settings.get("theme") or "light"), winapi.get_system_dpi())
        self.overlay = Overlay(self.root, self.theme)

        self._create_variables(settings)
        self._configure_root()
        self._build()
        self._start_scheduler()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_ids: list[str] = []
        self._after_ids.append(self.root.after(120, self._drain_events))
        self._after_ids.append(self.root.after(200, self._capture_root_hwnd))

        self.refresh_tasks(force=True)
        if not self._select_initial_task():
            self._sync_editor_from_draft()
            self._set_status(f"配置文件：{config.DATA_PATH.name}")

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _create_variables(self, settings: dict[str, Any]) -> None:
        self.name_var = tk.StringVar(value=self.draft["name"])
        self.conversation_var = tk.StringVar(value=self.draft["conversation"])
        self.schedule_var = tk.StringVar(value=self.draft["schedule"])
        self.date_var = tk.StringVar(value=self.draft["date"])
        self.time_var = tk.StringVar(value=self.draft["time"])
        self.interval_var = tk.StringVar(value=self.draft["interval"])
        self.action_var = tk.StringVar(value=self.draft["action"])
        self.enabled_var = tk.BooleanVar(value=bool(self.draft["enabled"]))
        self.filter_var = tk.StringVar(value="")
        self.log_filter_var = tk.StringVar(value="all")

        self.theme_var = tk.StringVar(value=str(settings.get("theme") or "light"))
        self.topmost_var = tk.BooleanVar(value=bool(settings.get("always_on_top", True)))
        self.restore_var = tk.BooleanVar(value=bool(settings.get("restore_target_geometry", True)))
        self.recognition_var = tk.BooleanVar(value=bool(settings.get("recognition_enabled", True)))
        self.verify_paste_var = tk.BooleanVar(value=bool(settings.get("verify_paste", True)))
        self.verify_send_var = tk.BooleanVar(value=bool(settings.get("verify_send", True)))
        self.strict_var = tk.BooleanVar(value=bool(settings.get("strict_conversation", True)))
        self.overlay_var = tk.BooleanVar(value=bool(settings.get("show_overlay", True)))
        self.keywords_var = tk.StringVar(value=str(settings.get("target_keywords") or ""))
        self.catch_up_var = tk.StringVar(value=str(int(settings.get("catch_up_seconds", 300) or 300) // 60))
        try:
            saved_delay = float(settings.get("step_delay_seconds", 1.2))
        except (TypeError, ValueError):
            saved_delay = 1.2
        nearest = min((0.8, 1.2, 1.8, 2.5), key=lambda value: abs(value - saved_delay))
        self.delay_var = tk.StringVar(value=f"{nearest:g}")

        self.status_var = tk.StringVar(value="")
        self.target_var = tk.StringVar(value="尚未识别目标窗口")
        self.calibrated_var = tk.StringVar(value="")
        self.count_var = tk.StringVar(value="")
        self.engine_var = tk.StringVar(value="调度未启动")
        self.anchor_vars = {
            "composer": tk.StringVar(value=""),
            "send": tk.StringVar(value=""),
            "search": tk.StringVar(value=""),
        }

        # 只注册一次；界面重建时不会重复累积。
        self.filter_var.trace_add("write", lambda *_: self.refresh_tasks(force=True))

    def _configure_root(self) -> None:
        theme = self.theme
        dpi = winapi.get_system_dpi()
        try:
            self.root.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
        except tk.TclError:
            pass
        theme.detect_fonts(self.root)
        self.root.title(config.APP_NAME)
        if config.ICON_PATH.exists():
            try:
                self.root.iconbitmap(default=str(config.ICON_PATH))
            except tk.TclError:
                pass
        self.root.configure(bg=theme.c("bg"))
        self.root.minsize(theme.px(880), theme.px(580))
        self.root.bind_all("<MouseWheel>", dispatch_wheel)

        # 默认尺寸按屏幕可用区域收敛，避免在 1366×768 或高缩放下超出屏幕。
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        default_width = min(theme.px(1180), int(screen_width * 0.92))
        default_height = min(theme.px(820), int(screen_height * 0.88))

        saved = self.store.setting("window_size")
        applied = False
        if isinstance(saved, str) and "x" in saved:
            try:
                size_part = saved.split("+")[0]
                saved_width = int(size_part.split("x")[0])
                saved_height = int(size_part.split("x")[1])
            except (IndexError, ValueError):
                saved_width = saved_height = 0
            # 超出屏幕或小于最小尺寸的旧记录直接丢弃，按当前屏幕重新决定。
            if (
                theme.px(880) <= saved_width <= screen_width
                and theme.px(580) <= saved_height <= screen_height
            ):
                try:
                    self.root.geometry(saved)
                    applied = True
                except tk.TclError:
                    applied = False
        if not applied:
            self.root.geometry(f"{default_width}x{default_height}")

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------

    def _build(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        theme = self.theme
        self.root.configure(bg=theme.c("bg"))
        self._row_widgets.clear()
        self._build_style()

        # 根窗口统一使用 grid，避免 pack 的 expand 抢占状态栏空间。
        self.root.grid_rowconfigure(2, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        header = self._build_header()
        header.grid(row=0, column=0, sticky="ew")
        tk.Frame(self.root, bg=theme.c("border"), height=1).grid(row=1, column=0, sticky="ew")

        body = tk.Frame(self.root, bg=theme.c("bg"))
        body.grid(row=2, column=0, sticky="nsew")
        self._build_rail(body)
        self._build_content(body)

        self._build_statusbar()

        self._apply_topmost()
        self._apply_frame_theme()
        self._render_log()
        self._apply_view()
        self._apply_selection()

    def _apply_frame_theme(self) -> None:
        """标题栏跟随主题；句柄还没拿到时留给 _capture_root_hwnd 补上。"""

        if not self.root_hwnd:
            return
        try:
            winapi.set_dark_frame(self.root_hwnd, self.theme.mode == "dark")
        except Exception:
            pass

    def _build_style(self) -> None:
        theme = self.theme
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Cue.Vertical.TScrollbar",
            troughcolor=theme.c("bg"),
            background=theme.c("border_strong"),
            bordercolor=theme.c("bg"),
            arrowcolor=theme.c("bg"),
            arrowsize=0,
            width=theme.px(9),
            relief="flat",
        )
        style.map(
            "Cue.Vertical.TScrollbar",
            background=[("active", theme.c("text_3"))],
        )

    def _build_header(self) -> tk.Frame:
        theme = self.theme
        header = tk.Frame(self.root, bg=theme.c("surface"), height=theme.px(58))
        header.pack_propagate(False)

        inner = tk.Frame(header, bg=theme.c("surface"))
        inner.pack(fill="both", expand=True, padx=theme.px(22))
        tk.Label(
            inner,
            text=config.APP_NAME,
            bg=theme.c("surface"),
            fg=theme.c("text"),
            font=theme.font("title"),
        ).pack(side="left")

        self.target_label = tk.Label(
            inner,
            textvariable=self.target_var,
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
        )
        self.target_label.pack(side="left", padx=(theme.px(16), 0))

        Switch(
            inner,
            theme,
            "窗口置顶",
            self.topmost_var,
            command=self._toggle_topmost,
            bg_key="surface",
        ).pack(side="right")
        return header

    def _build_rail(self, parent: tk.Frame) -> None:
        theme = self.theme
        rail = tk.Frame(parent, bg=theme.c("bg"), width=theme.px(146))
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)

        items = tk.Frame(rail, bg=theme.c("bg"))
        items.pack(fill="x", pady=(theme.px(16), 0))

        self.rail_items: dict[str, dict[str, Any]] = {}
        for key, text in (("tasks", "任务"), ("log", "日志"), ("settings", "设置")):
            item = tk.Frame(items, bg=theme.c("bg"), cursor="hand2")
            item.pack(fill="x", pady=(0, theme.px(2)))
            bar = tk.Frame(item, bg=theme.c("bg"), width=theme.px(3))
            bar.pack(side="left", fill="y")
            label = tk.Label(
                item,
                text=text,
                bg=theme.c("bg"),
                fg=theme.c("text_2"),
                font=theme.font("body"),
                anchor="w",
                padx=theme.px(16),
                pady=theme.px(8),
                cursor="hand2",
            )
            label.pack(side="left", fill="both", expand=True)
            for widget in (item, label):
                widget.bind("<Button-1>", lambda _event, k=key: self.show_view(k))
            self.rail_items[key] = {"frame": item, "bar": bar, "label": label}

        bottom = tk.Frame(rail, bg=theme.c("bg"))
        bottom.pack(side="bottom", fill="x", padx=theme.px(19), pady=theme.px(16))
        tk.Label(
            bottom,
            textvariable=self.engine_var,
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
            justify="left",
        ).pack(fill="x")
        tk.Label(
            bottom,
            text=f"v{config.APP_VERSION}",
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        ).pack(fill="x", pady=(theme.px(4), 0))

    def _build_content(self, parent: tk.Frame) -> None:
        theme = self.theme
        content = tk.Frame(parent, bg=theme.c("bg"))
        content.pack(side="left", fill="both", expand=True, padx=(0, theme.px(20)), pady=(0, theme.px(12)))
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self.views: dict[str, tk.Frame] = {}
        for key in ("tasks", "log", "settings"):
            frame = tk.Frame(content, bg=theme.c("bg"))
            frame.grid(row=0, column=0, sticky="nsew")
            self.views[key] = frame

        self._build_tasks_view(self.views["tasks"])
        self._build_log_view(self.views["log"])
        self._build_settings_view(self.views["settings"])

    def _build_statusbar(self) -> None:
        theme = self.theme
        bar = tk.Frame(self.root, bg=theme.c("bg"), height=theme.px(30))
        bar.grid(row=4, column=0, sticky="ew")
        bar.grid_propagate(False)
        tk.Label(
            bar,
            textvariable=self.status_var,
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        ).pack(side="left", padx=theme.px(22))
        # 任务数量已经在列表表头显示，这里不再重复一遍。
        tk.Frame(self.root, bg=theme.c("border"), height=1).grid(row=3, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # 任务视图
    # ------------------------------------------------------------------

    def _build_tasks_view(self, parent: tk.Frame) -> None:
        theme = self.theme
        left = tk.Frame(parent, bg=theme.c("bg"), width=theme.px(286))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        head = tk.Frame(left, bg=theme.c("bg"))
        head.pack(fill="x", pady=(theme.px(16), theme.px(10)))
        self.list_count_label = tk.Label(
            head,
            textvariable=self.count_var,
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
            anchor="w",
        )
        self.list_count_label.pack(side="left")
        Button(head, theme, "新建", self.new_task, kind="ghost", size="sm").pack(side="right")
        self.tidy_button = Button(head, theme, "整理", self.tidy_tasks, kind="ghost", size="sm")
        self.tidy_button.pack(side="right", padx=(0, theme.px(6)))

        search = Field(left, theme, "", self.filter_var, bg_key="bg", placeholder="搜索名称或内容")
        search.pack(fill="x", pady=(0, theme.px(10)))
        search.entry.configure(font=theme.font("small"))

        list_wrap = tk.Frame(left, bg=theme.c("bg"))
        list_wrap.pack(fill="both", expand=True)
        self.list_scroll = ScrollArea(list_wrap, theme, "bg")
        self.list_scroll.pack(fill="both", expand=True)
        self.list_body = self.list_scroll.body

        right = tk.Frame(parent, bg=theme.c("bg"))
        right.pack(side="left", fill="both", expand=True, padx=(theme.px(20), 0))
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.editor_scroll = ScrollArea(right, theme, "bg")
        self.editor_scroll.grid(row=0, column=0, sticky="nsew")
        self._build_editor(self.editor_scroll.body)

        actions = tk.Frame(right, bg=theme.c("bg"))
        actions.grid(row=1, column=0, sticky="ew", pady=(theme.px(12), 0))
        self.delete_button = Button(actions, theme, "删除", self.delete_task, kind="danger")
        self.delete_button.pack(side="left")
        # 主按钮只有一个：立刻把这条提示词发出去。保存是次要动作。
        self.run_button = Button(actions, theme, "立即执行", self.save_and_run, kind="primary")
        self.run_button.pack(side="right")
        self.save_button = Button(actions, theme, "保存", self.save_task, kind="ghost")
        self.save_button.pack(side="right", padx=(0, theme.px(8)))
        self.editor_state = tk.Label(
            actions,
            text="",
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        )
        self.editor_state.pack(side="left", padx=(theme.px(12), 0))

    def _build_editor(self, parent: tk.Frame) -> None:
        theme = self.theme
        pad = theme.px(20)
        outer, body = card(parent, theme, "surface")
        outer.pack(fill="x")

        head = tk.Frame(body, bg=theme.c("surface"))
        head.pack(fill="x", padx=pad, pady=(pad, theme.px(16)))
        self.editor_title = tk.Label(
            head,
            text="新建任务",
            bg=theme.c("surface"),
            fg=theme.c("text"),
            font=theme.font("title"),
            anchor="w",
        )
        self.editor_title.pack(side="left")
        Switch(head, theme, "启用", self.enabled_var, bg_key="surface").pack(side="right")

        self.name_field = Field(body, theme, "名称", self.name_var)
        self.name_field.pack(fill="x", padx=pad)

        # 提示词不再吃掉全部剩余高度：把计划、发送方式、目标对话留在首屏可见，
        # 用户一眼就能看完整条流程（写什么 -> 发到哪 -> 什么时候发 -> 怎么发）。
        self.prompt_area = TextArea(body, theme, "提示词", height=7)
        self.prompt_area.pack(fill="x", padx=pad, pady=(theme.px(14), 0))

        schedule_head = tk.Label(
            body,
            text="计划",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        )
        schedule_head.pack(fill="x", padx=pad, pady=(theme.px(16), theme.px(6)))
        Segmented(body, theme, SCHEDULE_OPTIONS, self.schedule_var, self._on_schedule_change).pack(
            fill="x", padx=pad
        )

        self.schedule_holder = tk.Frame(body, bg=theme.c("surface"))
        self.schedule_holder.pack(fill="x", padx=pad, pady=(theme.px(10), 0))

        self.time_row = tk.Frame(self.schedule_holder, bg=theme.c("surface"))
        date_field = Field(self.time_row, theme, "日期", self.date_var)
        date_field.pack(side="left", fill="x", expand=True)
        time_field = Field(self.time_row, theme, "时间", self.time_var)
        time_field.pack(side="left", fill="x", expand=True, padx=(theme.px(12), 0))

        self.interval_row = tk.Frame(self.schedule_holder, bg=theme.c("surface"))
        Field(self.interval_row, theme, "间隔分钟", self.interval_var).pack(fill="x")

        action_head = tk.Label(
            body,
            text="发送方式",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        )
        action_head.pack(fill="x", padx=pad, pady=(theme.px(16), theme.px(6)))
        Segmented(body, theme, ACTION_OPTIONS, self.action_var).pack(fill="x", padx=pad)

        self.conversation_field = Field(
            body,
            theme,
            "目标对话",
            self.conversation_var,
            placeholder="留空则发送到当前对话",
        )
        self.conversation_field.pack(fill="x", padx=pad, pady=(theme.px(14), pad))

        self._on_schedule_change(self.schedule_var.get())

    def _on_schedule_change(self, value: str) -> None:
        if value == "interval":
            self.time_row.pack_forget()
            self.interval_row.pack(fill="x")
        else:
            self.interval_row.pack_forget()
            self.time_row.pack(fill="x")

    # ------------------------------------------------------------------
    # 日志视图
    # ------------------------------------------------------------------

    def _build_log_view(self, parent: tk.Frame) -> None:
        theme = self.theme
        head = tk.Frame(parent, bg=theme.c("bg"))
        head.pack(fill="x", pady=(theme.px(16), theme.px(12)))
        tk.Label(
            head,
            text="运行日志",
            bg=theme.c("bg"),
            fg=theme.c("text"),
            font=theme.font("section"),
        ).pack(side="left")
        Button(head, theme, "打开日志文件", self._open_log_file, kind="ghost", size="sm").pack(side="right")
        Button(head, theme, "清空", self._clear_log, kind="ghost", size="sm").pack(
            side="right", padx=(0, theme.px(8))
        )

        filters = tk.Frame(parent, bg=theme.c("bg"))
        filters.pack(fill="x", pady=(0, theme.px(12)))
        Segmented(filters, theme, FILTER_OPTIONS, self.log_filter_var, self._on_log_filter).pack(
            side="left"
        )

        outer, inner = card(parent, theme, "surface")
        outer.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            inner,
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            insertbackground=theme.c("text"),
            font=theme.font("mono"),
            padx=theme.px(14),
            pady=theme.px(12),
            spacing1=theme.px(2),
            spacing3=theme.px(2),
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=1, pady=1)
        for level, color in (
            ("info", theme.c("text_2")),
            ("success", theme.c("success")),
            ("warning", theme.c("warning")),
            ("error", theme.c("danger")),
            ("time", theme.c("text_3")),
        ):
            self.log_text.tag_configure(level, foreground=color)

    def _on_log_filter(self, value: str) -> None:
        self._log_filter = value
        self._render_log()

    def _render_log(self) -> None:
        if not hasattr(self, "log_text"):
            return
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            for level, line in self._log_lines:
                if self._log_filter != "all" and level != self._log_filter:
                    continue
                self.log_text.insert("end", line + "\n", level)
            self.log_text.configure(state="disabled")
            self.log_text.see("end")
        except tk.TclError:
            pass

    def _clear_log(self) -> None:
        self._log_lines.clear()
        self._render_log()

    def _open_log_file(self) -> None:
        try:
            if config.LOG_PATH.exists():
                os.startfile(str(config.LOG_PATH))  # type: ignore[attr-defined]
            else:
                os.startfile(str(config.BASE_DIR))  # type: ignore[attr-defined]
        except Exception as exc:
            self._set_status(f"无法打开日志：{friendly_error(exc)}")

    # ------------------------------------------------------------------
    # 设置视图
    # ------------------------------------------------------------------

    def _settings_card(self, parent: tk.Frame, title: str) -> tk.Frame:
        theme = self.theme
        outer, body = card(parent, theme, "surface")
        outer.pack(fill="x", pady=(0, theme.px(12)))
        tk.Label(
            body,
            text=title,
            bg=theme.c("surface"),
            fg=theme.c("text"),
            font=theme.font("section"),
            anchor="w",
        ).pack(fill="x", padx=theme.px(20), pady=(theme.px(16), theme.px(12)))
        return body

    def _build_settings_view(self, parent: tk.Frame) -> None:
        theme = self.theme
        head = tk.Frame(parent, bg=theme.c("bg"))
        head.pack(fill="x", pady=(theme.px(16), theme.px(12)))
        tk.Label(
            head,
            text="设置",
            bg=theme.c("bg"),
            fg=theme.c("text"),
            font=theme.font("section"),
        ).pack(side="left")

        scroll = ScrollArea(parent, theme, "bg")
        scroll.pack(fill="both", expand=True)
        self.settings_scroll = scroll
        body = scroll.body

        # 目标应用
        card_body = self._settings_card(body, "目标应用")
        inner = tk.Frame(card_body, bg=theme.c("surface"))
        inner.pack(fill="x", padx=theme.px(20), pady=(0, theme.px(18)))
        keywords_field = Field(
            inner,
            theme,
            "窗口关键词",
            self.keywords_var,
            hint="用逗号分隔；标题或进程名包含任一关键词即视为目标",
        )
        keywords_field.pack(fill="x")
        keywords_field.entry.bind("<FocusOut>", lambda _event: self._save_keywords())
        row = tk.Frame(inner, bg=theme.c("surface"))
        row.pack(fill="x", pady=(theme.px(12), 0))
        tk.Label(
            row,
            textvariable=self.target_var,
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        ).pack(side="left")
        Button(row, theme, "识别目标窗口", self.detect_target, kind="ghost", size="sm").pack(side="right")

        # 识别与校准
        card_body = self._settings_card(body, "识别与校准")
        inner = tk.Frame(card_body, bg=theme.c("surface"))
        inner.pack(fill="x", padx=theme.px(20), pady=(0, theme.px(18)))
        tk.Label(
            inner,
            text="执行时优先实时识别输入框与发送按钮，识别失败才使用下面保存的位置。",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
            justify="left",
            wraplength=theme.px(620),
        ).pack(fill="x")
        Button(inner, theme, "自动识别并预览", self.calibrate, kind="primary").pack(
            anchor="w", pady=(theme.px(12), theme.px(6))
        )
        tk.Label(
            inner,
            textvariable=self.calibrated_var,
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        ).pack(fill="x", pady=(0, theme.px(12)))

        anchors = tk.Frame(inner, bg=theme.c("surface"))
        anchors.pack(fill="x")
        for key, text in (("composer", "输入框"), ("send", "发送按钮"), ("search", "对话搜索")):
            line = tk.Frame(anchors, bg=theme.c("surface"))
            line.pack(fill="x", pady=(0, theme.px(6)))
            tk.Label(
                line,
                text=text,
                bg=theme.c("surface"),
                fg=theme.c("text_2"),
                font=theme.font("small"),
                width=8,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                line,
                textvariable=self.anchor_vars[key],
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                anchor="w",
            ).pack(side="left")
            Button(
                line,
                theme,
                "手动指定",
                lambda k=key: self.manual_anchor(k),
                kind="subtle",
                size="sm",
            ).pack(side="right")

        switches = tk.Frame(inner, bg=theme.c("surface"))
        switches.pack(fill="x", pady=(theme.px(10), 0))
        for text, variable, command in (
            ("执行时显示识别框线", self.overlay_var, self._save_recognition),
            ("执行时重新识别界面", self.recognition_var, self._save_recognition),
            ("校验粘贴结果", self.verify_paste_var, self._save_recognition),
            ("校验发送结果", self.verify_send_var, self._save_recognition),
            ("对话切换失败时中止", self.strict_var, self._save_recognition),
        ):
            Switch(switches, theme, text, variable, command=command, bg_key="surface").pack(
                anchor="w", pady=(0, theme.px(6))
            )

        # 执行
        card_body = self._settings_card(body, "执行")
        inner = tk.Frame(card_body, bg=theme.c("surface"))
        inner.pack(fill="x", padx=theme.px(20), pady=(0, theme.px(18)))
        tk.Label(
            inner,
            text="步骤间隔",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        ).pack(fill="x", pady=(0, theme.px(6)))
        Segmented(inner, theme, DELAY_OPTIONS, self.delay_var, lambda _v: self._save_delay()).pack(
            fill="x"
        )
        tk.Label(
            inner,
            text="界面响应慢时可以调大；识别与校验本身会自适应等待，不受此值限制。",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        ).pack(fill="x", pady=(theme.px(6), theme.px(14)))
        Field(
            inner,
            theme,
            "补执行窗口（分钟）",
            self.catch_up_var,
            hint="错过执行时间超过该值就跳过本次，避免旧任务突然运行",
        ).pack(fill="x")
        Button(inner, theme, "保存执行设置", self._save_execution, kind="ghost", size="sm").pack(
            anchor="w", pady=(theme.px(12), 0)
        )

        # 界面
        card_body = self._settings_card(body, "界面")
        inner = tk.Frame(card_body, bg=theme.c("surface"))
        inner.pack(fill="x", padx=theme.px(20), pady=(0, theme.px(18)))
        tk.Label(
            inner,
            text="主题",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        ).pack(fill="x", pady=(0, theme.px(6)))
        Segmented(inner, theme, THEME_OPTIONS, self.theme_var, self._on_theme_change).pack(fill="x")
        Switch(
            inner,
            theme,
            "窗口置顶",
            self.topmost_var,
            command=self._toggle_topmost,
            bg_key="surface",
        ).pack(anchor="w", pady=(theme.px(14), 0))
        Switch(
            inner,
            theme,
            "执行前恢复目标窗口位置和大小",
            self.restore_var,
            command=self._save_execution,
            bg_key="surface",
        ).pack(anchor="w", pady=(theme.px(6), 0))

        # 数据
        card_body = self._settings_card(body, "数据")
        inner = tk.Frame(card_body, bg=theme.c("surface"))
        inner.pack(fill="x", padx=theme.px(20), pady=(0, theme.px(18)))
        tk.Label(
            inner,
            text=str(config.DATA_PATH),
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        ).pack(fill="x")
        Button(inner, theme, "打开所在文件夹", self._open_data_dir, kind="ghost", size="sm").pack(
            anchor="w", pady=(theme.px(10), 0)
        )

    # ------------------------------------------------------------------
    # 视图切换
    # ------------------------------------------------------------------

    def show_view(self, key: str) -> None:
        if key not in self.views:
            return
        self.view = key
        self._apply_view()

    def _apply_view(self) -> None:
        for key, frame in self.views.items():
            if key == self.view:
                frame.tkraise()
        theme = self.theme
        for key, parts in self.rail_items.items():
            active = key == self.view
            parts["frame"].configure(bg=theme.c("surface_3") if active else theme.c("bg"))
            parts["label"].configure(
                bg=theme.c("surface_3") if active else theme.c("bg"),
                fg=theme.c("text") if active else theme.c("text_2"),
                font=theme.font("strong") if active else theme.font("body"),
            )
            parts["bar"].configure(bg=theme.c("accent") if active else theme.c("bg"))

    # ------------------------------------------------------------------
    # 任务列表
    # ------------------------------------------------------------------

    def refresh_tasks(self, force: bool = False) -> None:
        if not hasattr(self, "list_body"):
            return
        tasks = self.store.tasks()
        keyword = self.filter_var.get().strip().lower()
        if keyword:
            tasks = [
                task
                for task in tasks
                if keyword in str(task.get("name") or "").lower()
                or keyword in str(task.get("content") or "").lower()
            ]
        tasks.sort(key=self._sort_key)

        grouped = self._group_tasks(tasks)
        signature = tuple(
            (
                key,
                tuple(
                    (task.get("id"), task.get("name"), self._row_meta(task), content_snippet(task))
                    for task in items
                ),
            )
            for key, items in grouped
        )
        if not force and signature == self._list_signature:
            return
        self._list_signature = signature

        for child in self.list_body.winfo_children():
            child.destroy()
        self._row_widgets.clear()

        if not tasks:
            message = "没有匹配的任务" if keyword else "还没有任务，点右上角「新建」"
            tk.Label(
                self.list_body,
                text=message,
                bg=self.theme.c("bg"),
                fg=self.theme.c("text_3"),
                font=self.theme.font("small"),
                anchor="w",
            ).pack(fill="x", padx=self.theme.px(4), pady=self.theme.px(14))
        for key, items in grouped:
            expanded = self._group_open_state(key)
            # 选中的任务不能藏在收起的分组里，否则看起来像被删掉了。
            if not expanded and any(str(task.get("id")) == self.selected_id for task in items):
                expanded = True
            self._render_group_head(key, len(items), expanded)
            if expanded:
                for task in items:
                    self._render_row(task)

        total = len(self.store.tasks())
        self.count_var.set(f"{total} 个任务")
        self._apply_selection()

    def _select_initial_task(self) -> bool:
        """打开窗口就落在第一个真实任务上。

        空白的「新建任务」表单看不出这个工具在做什么；载入一条真实任务，
        名称、提示词、计划、发送方式、目标对话一次全在首屏。
        """

        grouped = self._group_tasks(sorted(self.store.tasks(), key=self._sort_key))
        if not grouped:
            return False
        first = grouped[0][1][0]
        task = self.store.get(str(first.get("id")))
        if task is None:
            return False
        self.selected_id = str(task.get("id"))
        self._load_task(task)
        self._apply_selection()
        return True

    def _group_open_state(self, key: str) -> bool:
        if key in self._group_open:
            return self._group_open[key]
        return GROUP_DEFAULT_OPEN.get(key, True)

    def _toggle_group(self, key: str) -> None:
        self._group_open[key] = not self._group_open_state(key)
        self.refresh_tasks(force=True)

    def _group_tasks(self, tasks: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            buckets.setdefault(store_module.state_group(task), []).append(task)
        return [
            (key, buckets[key])
            for key, _label, _states in store_module.GROUP_ORDER
            if buckets.get(key)
        ]

    def _render_group_head(self, key: str, count: int, expanded: bool = True) -> None:
        theme = self.theme
        head = tk.Frame(self.list_body, bg=theme.c("bg"), cursor="hand2")
        head.pack(fill="x", padx=theme.px(4), pady=(theme.px(10), theme.px(4)))

        # 收起画向右的三角，展开画向下的三角：一个符号说清可点，不用再加说明文字。
        caret = tk.Canvas(
            head,
            width=theme.px(9),
            height=theme.px(9),
            bg=theme.c("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        if expanded:
            caret.create_polygon(
                1, 2, theme.px(7), 2, theme.px(4), theme.px(7),
                fill=theme.c("text_3"), outline="",
            )
        else:
            caret.create_polygon(
                2, 1, 2, theme.px(7), theme.px(7), theme.px(4),
                fill=theme.c("text_3"), outline="",
            )
        caret.pack(side="left", pady=(theme.px(4), 0))

        label = tk.Label(
            head,
            text=store_module.group_label(key),
            bg=theme.c("bg"),
            fg=theme.c("text_2"),
            font=theme.font("small_strong"),
            anchor="w",
            cursor="hand2",
        )
        label.pack(side="left", padx=(theme.px(5), 0))

        count_label = tk.Label(
            head,
            text=str(count),
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="e",
            cursor="hand2",
        )
        count_label.pack(side="right")

        for widget in (head, caret, label, count_label):
            widget.bind("<Button-1>", lambda _event, k=key: self._toggle_group(k))

    @staticmethod
    def _sort_key(task: dict[str, Any]) -> tuple:
        next_run = parse_datetime(task.get("next_run")) or _dt.datetime.max
        return (0 if task.get("enabled") else 1, next_run, str(task.get("name") or ""))

    @staticmethod
    def _row_meta(task: dict[str, Any]) -> str:
        """行右侧的短标签。

        在「失败」分组里每条都写一遍「失败」是废话，改成失败发生的时间；
        其它状态照旧显示计划时间。
        """

        if task_state(task) == "failed":
            stamp = parse_datetime(task.get("last_run"))
            # 没有执行记录就留空：分组名和红点已经说明失败了，不必再写一遍。
            return config.format_short_datetime(stamp, "") if stamp is not None else ""
        return next_run_text(task)

    def _render_row(self, task: dict[str, Any]) -> None:
        theme = self.theme
        task_id = str(task.get("id"))
        row = tk.Frame(self.list_body, bg=theme.c("bg"), cursor="hand2")
        row.pack(fill="x", pady=(0, theme.px(1)))

        bar = tk.Frame(row, bg=theme.c("bg"), width=theme.px(3))
        bar.pack(side="left", fill="y")

        content = tk.Frame(row, bg=theme.c("bg"))
        content.pack(side="left", fill="both", expand=True, padx=(theme.px(11), theme.px(6)), pady=theme.px(8))

        name_row = tk.Frame(content, bg=theme.c("bg"))
        name_row.pack(fill="x")
        state = task_state(task)
        dot = tk.Canvas(
            name_row,
            width=theme.px(7),
            height=theme.px(7),
            bg=theme.c("bg"),
            highlightthickness=0,
            bd=0,
        )
        dot.create_oval(0, 0, theme.px(6), theme.px(6), fill=theme.tone(store_module.STATE_TONE[state]), outline="")
        dot.pack(side="left", pady=(theme.px(5), 0))
        name_label = tk.Label(
            name_row,
            text=str(task.get("name") or "未命名任务"),
            bg=theme.c("bg"),
            fg=theme.c("text"),
            font=theme.font("strong"),
            anchor="w",
        )
        name_label.pack(side="left", padx=(theme.px(7), 0))

        meta_label = tk.Label(
            name_row,
            text=self._row_meta(task),
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="e",
        )
        meta_label.pack(side="right")

        snippet_label = tk.Label(
            content,
            text=content_snippet(task, 40),
            bg=theme.c("bg"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        )
        snippet_label.pack(fill="x", padx=(theme.px(14), 0), pady=(theme.px(3), 0))

        widgets = (row, content, name_row, name_label, snippet_label, meta_label)
        for widget in widgets:
            widget.bind("<Button-1>", lambda _event, tid=task_id: self.select_task(tid))
            widget.bind("<Button-3>", lambda event, tid=task_id: self._open_row_menu(event, tid))
            widget.bind("<Enter>", lambda _event, tid=task_id: self._hover_row(tid, True))
            widget.bind("<Leave>", lambda _event, tid=task_id: self._hover_row(tid, False))

        self._row_widgets[task_id] = {
            "row": row,
            "bar": bar,
            "content": content,
            "name_row": name_row,
            "name": name_label,
            "snippet": snippet_label,
            "meta_label": meta_label,
            "dot": dot,
            "hover": False,
        }

    def _hover_row(self, task_id: str, hover: bool) -> None:
        parts = self._row_widgets.get(task_id)
        if not parts:
            return
        parts["hover"] = hover
        self._paint_row(task_id)

    def _paint_row(self, task_id: str) -> None:
        parts = self._row_widgets.get(task_id)
        if not parts:
            return
        theme = self.theme
        selected = task_id == self.selected_id
        if selected:
            background = theme.c("accent_soft")
        elif parts["hover"]:
            background = theme.c("surface_3")
        else:
            background = theme.c("bg")
        try:
            parts["row"].configure(bg=background)
            parts["content"].configure(bg=background)
            parts["name_row"].configure(bg=background)
            parts["name"].configure(bg=background, fg=theme.c("accent_label") if selected else theme.c("text"))
            parts["snippet"].configure(bg=background)
            parts["meta_label"].configure(bg=background)
            parts["dot"].configure(bg=background)
            parts["bar"].configure(bg=theme.c("accent") if selected else background)
        except tk.TclError:
            return

    def _apply_selection(self) -> None:
        for task_id in self._row_widgets:
            self._paint_row(task_id)

    def select_task(self, task_id: str) -> None:
        if task_id == self.selected_id:
            return
        if not self._confirm_discard():
            return
        task = self.store.get(task_id)
        if not task:
            return
        self.selected_id = task_id
        # 选中的任务落在收起的分组里时先展开，否则列表上看不出它被选中了。
        group = store_module.state_group(task)
        if not self._group_open_state(group):
            self._group_open[group] = True
            self.refresh_tasks(force=True)
        self._apply_selection()
        self._load_task(task)

    def _load_task(self, task: dict[str, Any]) -> None:
        # 没有计划时间的任务（已完成、失败、暂停）不能回填"现在"：那个时间点往往
        # 已经过去了，用户一保存就会被问"是否立即执行"。回填 5 分钟后更符合预期。
        planned = parse_datetime(task.get("next_run")) or (now_local() + _dt.timedelta(minutes=5))
        self.draft = {
            "id": str(task.get("id")),
            "name": str(task.get("name") or ""),
            "content": str(task.get("content") or ""),
            "conversation": str(task.get("conversation_title") or ""),
            "schedule": str(task.get("schedule") or "once"),
            "date": planned.strftime("%Y-%m-%d"),
            "time": planned.strftime("%H:%M"),
            "interval": str(int(task.get("interval_minutes", 60) or 60)),
            "action": str(task.get("action") or "click"),
            "enabled": bool(task.get("enabled", True)),
        }
        self._sync_editor_from_draft()
        self._baseline = self._editor_signature()
        self.editor_title.configure(text="编辑任务")
        self.delete_button.set_enabled(True)
        error = str(task.get("last_error") or "")
        if error:
            # 完整报错在日志里；这里只留一句，太长会顶到右边的按钮上。
            self.editor_state.configure(text=_clip(error, 52), fg=self.theme.c("danger"))
        else:
            self.editor_state.configure(
                text=f"运行 {int(task.get('run_count', 0) or 0)} 次",
                fg=self.theme.c("text_3"),
            )
        self._set_status(f"已载入：{self.draft['name'] or '未命名任务'}")

    def _sync_editor_from_draft(self) -> None:
        self.name_var.set(self.draft["name"])
        self.conversation_var.set(self.draft["conversation"])
        self.schedule_var.set(self.draft["schedule"])
        self.date_var.set(self.draft["date"])
        self.time_var.set(self.draft["time"])
        self.interval_var.set(self.draft["interval"])
        self.action_var.set(self.draft["action"])
        self.enabled_var.set(bool(self.draft["enabled"]))
        if hasattr(self, "prompt_area"):
            self.prompt_area.set(self.draft["content"])
            self._on_schedule_change(self.draft["schedule"])

    def _sync_draft_from_editor(self) -> None:
        self.draft["name"] = self.name_var.get().strip()
        self.draft["conversation"] = self.conversation_var.get().strip()
        self.draft["schedule"] = self.schedule_var.get()
        self.draft["date"] = self.date_var.get().strip()
        self.draft["time"] = self.time_var.get().strip()
        self.draft["interval"] = self.interval_var.get().strip()
        self.draft["action"] = self.action_var.get()
        self.draft["enabled"] = bool(self.enabled_var.get())
        if hasattr(self, "prompt_area"):
            self.draft["content"] = self.prompt_area.get()

    def _editor_signature(self) -> tuple:
        return (
            self.draft["name"],
            self.draft["content"],
            self.draft["conversation"],
            self.draft["schedule"],
            self.draft["date"],
            self.draft["time"],
            self.draft["interval"],
            self.draft["action"],
            bool(self.draft["enabled"]),
        )

    def _is_dirty(self) -> bool:
        self._sync_draft_from_editor()
        return self._baseline is not None and self._editor_signature() != self._baseline

    def _confirm_discard(self) -> bool:
        if not self._is_dirty():
            return True
        if not self.draft["content"].strip() and not self.draft["name"]:
            return True
        return messagebox.askyesno(
            "放弃修改",
            "当前编辑的内容还没有保存，切换后会丢失。继续吗？",
            parent=self.root,
        )

    # ------------------------------------------------------------------
    # 任务操作
    # ------------------------------------------------------------------

    def new_task(self) -> None:
        if not self._confirm_discard():
            return
        self.selected_id = None
        self.draft = _empty_draft()
        self._sync_editor_from_draft()
        self._baseline = self._editor_signature()
        self.editor_title.configure(text="新建任务")
        self.delete_button.set_enabled(False)
        self.editor_state.configure(text="", fg=self.theme.c("text_3"))
        self._apply_selection()
        self._set_status("新建任务")

    def _build_task_payload(self) -> Optional[dict[str, Any]]:
        self._sync_draft_from_editor()
        if not self.draft["content"].strip():
            messagebox.showwarning("无法保存", "提示词内容不能为空。", parent=self.root)
            return None
        name = self.draft["name"] or "未命名任务"

        schedule = self.draft["schedule"]
        current = now_local()
        try:
            if schedule == "interval":
                minutes = int(float(self.draft["interval"] or "60"))
                if minutes < 1 or minutes > 525600:
                    raise ValueError
                next_run = current + _dt.timedelta(minutes=minutes)
                interval = minutes
            else:
                next_run = _dt.datetime.strptime(f"{self.draft['date']} {self.draft['time']}", "%Y-%m-%d %H:%M")
                interval = 60
                if schedule == "daily" and next_run <= current:
                    next_run += _dt.timedelta(days=1)
                if schedule == "once" and next_run < current - _dt.timedelta(minutes=1):
                    if not messagebox.askyesno(
                        "时间已经过去",
                        "该时间已经过去，保存后会立即执行。继续吗？",
                        parent=self.root,
                    ):
                        return None
        except (TypeError, ValueError):
            messagebox.showwarning(
                "时间格式错误",
                "日期请使用 2026-11-06，时间请使用 22:30，间隔请填正整数分钟。",
                parent=self.root,
            )
            return None

        return {
            "name": name[:80],
            "content": self.draft["content"],
            "conversation_title": self.draft["conversation"][:200],
            "schedule": schedule,
            "next_run": iso_datetime(next_run),
            "interval_minutes": interval,
            "action": self.draft["action"],
            "enabled": bool(self.draft["enabled"]),
            "status": "waiting" if self.draft["enabled"] else "paused",
            "last_error": "",
        }

    def save_task(self) -> Optional[str]:
        payload = self._build_task_payload()
        if payload is None:
            return None
        try:
            if self.draft["id"]:
                self.store.replace(str(self.draft["id"]), payload)
                task_id = str(self.draft["id"])
                message = "已更新"
            else:
                created = self.store.add(payload)
                task_id = str(created["id"])
                self.draft["id"] = task_id
                message = "已保存"
            self.selected_id = task_id
            self._baseline = self._editor_signature()
            self._list_signature = None
            self.refresh_tasks(force=True)
            if self.scheduler:
                self.scheduler.wake()
            self.editor_title.configure(text="编辑任务")
            self.delete_button.set_enabled(True)
            self._set_status(f"{message}：{payload['name']}")
            self.log("success", f"{message}：{payload['name']}")
            return task_id
        except Exception as exc:
            self.log("error", f"保存失败：{friendly_error(exc)}")
            messagebox.showerror("保存失败", friendly_error(exc), parent=self.root)
            return None

    def save_and_run(self) -> None:
        task_id = self.save_task()
        if task_id:
            self.run_task(task_id)

    def delete_task(self, task_id: Optional[str] = None) -> None:
        target = str(task_id or self.draft["id"] or "")
        if not target:
            self._set_status("当前编辑区是新建任务，没有可删除的记录。")
            return
        task = self.store.get(target)
        if not task:
            return
        if not messagebox.askyesno(
            "删除任务",
            f"确定删除“{task.get('name')}”吗？删除后无法恢复。",
            parent=self.root,
        ):
            return
        self.store.delete(target)
        self.log("info", f"已删除：{task.get('name')}")
        self._list_signature = None
        if self.draft["id"] != target:
            self.refresh_tasks(force=True)
            self._set_status("已删除")
            return
        self.selected_id = None
        self.draft = _empty_draft()
        self._sync_editor_from_draft()
        self._baseline = self._editor_signature()
        self.editor_title.configure(text="新建任务")
        self.delete_button.set_enabled(False)
        self.editor_state.configure(text="", fg=self.theme.c("text_3"))
        self.refresh_tasks(force=True)
        self._set_status("已删除")

    def duplicate_task(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if not task:
            return
        copy = {
            key: value
            for key, value in task.items()
            if key not in ("id", "created_at", "updated_at", "last_run", "last_error", "run_count", "last_duration_ms")
        }
        copy["name"] = f"{task.get('name') or '未命名任务'} 副本"[:80]
        copy["enabled"] = False
        copy["status"] = "paused"
        # 副本默认暂停，但下次执行时间不能留在过去，否则一启用就会被判为过期。
        now = now_local()
        upcoming = parse_datetime(copy.get("next_run"))
        if upcoming is None or upcoming <= now:
            future = advance_next_run_past_now(copy, now) if copy.get("schedule") != "once" else None
            copy["next_run"] = iso_datetime(future or now + _dt.timedelta(minutes=5))
        created = self.store.add(copy)
        self._list_signature = None
        self.refresh_tasks(force=True)
        self.log("info", f"已复制：{created.get('name')}")
        self._set_status(f"已复制：{created.get('name')}")

    def tidy_tasks(self) -> None:
        tasks = self.store.tasks()
        if len(tasks) < 2:
            messagebox.showinfo("整理任务", "任务太少，暂时不需要整理。", parent=self.root)
            return
        TidyDialog(self.root, self.theme, tasks, self._apply_tidy)

    def _apply_tidy(self, task_ids: list[str]) -> None:
        removed = self.store.delete_many(task_ids)
        if self.draft["id"] and self.draft["id"] in set(task_ids):
            self.selected_id = None
            self.draft = _empty_draft()
            self._sync_editor_from_draft()
            self._baseline = self._editor_signature()
            self.editor_title.configure(text="新建任务")
            self.delete_button.set_enabled(False)
            self.editor_state.configure(text="", fg=self.theme.c("text_3"))
        if self.selected_id and not self.store.get(self.selected_id):
            self.selected_id = None
        self._list_signature = None
        self.refresh_tasks(force=True)
        self.log("info", f"整理完成：删除 {removed} 个任务")
        self._set_status(f"已删除 {removed} 个任务，剩余 {len(self.store.tasks())} 个")

    def _open_row_menu(self, event, task_id: str) -> None:
        task = self.store.get(task_id)
        if not task:
            return
        theme = self.theme
        enabled = bool(task.get("enabled"))
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=theme.c("surface"),
            fg=theme.c("text"),
            activebackground=theme.c("accent"),
            activeforeground=theme.c("accent_text"),
            bd=0,
            relief="flat",
            font=theme.font("body"),
        )
        menu.add_command(label="暂停" if enabled else "启用", command=lambda: self.toggle_task(task_id))
        menu.add_command(label="立即运行", command=lambda: self.run_task(task_id))
        menu.add_separator()
        menu.add_command(label="复制一份", command=lambda: self.duplicate_task(task_id))
        menu.add_command(label="删除", command=lambda: self.delete_task(task_id))
        try:
            menu.tk_popup(int(event.x_root), int(event.y_root))
        except tk.TclError:
            pass
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    def toggle_task(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if not task:
            return
        enabled = not bool(task.get("enabled"))
        self.store.update_task(
            task_id,
            enabled=enabled,
            status="waiting" if enabled else "paused",
            last_error="",
        )
        self._list_signature = None
        self.refresh_tasks(force=True)
        if self.scheduler:
            self.scheduler.wake()
        self.log("info", f"{'启用' if enabled else '暂停'}：{task.get('name')}")

    def run_task(self, task_id: Optional[str] = None) -> None:
        task_id = task_id or self.selected_id
        if not task_id:
            messagebox.showinfo("立即运行", "请先在左侧选择一个任务。", parent=self.root)
            return
        if (
            not self.store.setting("recognition_enabled", True)
            and not self.store.setting("anchor_composer")
            and not self.store.setting("composer_rect")
        ):
            messagebox.showwarning(
                "尚未校准",
                "实时识别已关闭，且没有保存过输入框位置。请先在设置页完成一次识别。",
                parent=self.root,
            )
            return
        if not self.scheduler:
            return
        if not self.scheduler.trigger(str(task_id), "manual"):
            self._set_status("该任务正在执行中。")

    # ------------------------------------------------------------------
    # 设置操作
    # ------------------------------------------------------------------

    def _save_keywords(self) -> None:
        self.store.update_settings(target_keywords=self.keywords_var.get().strip())

    def _save_recognition(self) -> None:
        self.store.update_settings(
            recognition_enabled=bool(self.recognition_var.get()),
            verify_paste=bool(self.verify_paste_var.get()),
            verify_send=bool(self.verify_send_var.get()),
            strict_conversation=bool(self.strict_var.get()),
            show_overlay=bool(self.overlay_var.get()),
        )
        self._set_status("识别设置已保存")

    def _save_delay(self) -> None:
        try:
            value = float(self.delay_var.get())
        except (TypeError, ValueError):
            value = 1.2
        value = max(0.3, min(10.0, value))
        self.store.update_settings(step_delay_seconds=value)
        self._set_status(f"步骤间隔：{value:g} 秒")

    def _save_execution(self) -> None:
        try:
            minutes = max(0, min(60, int(float(self.catch_up_var.get()))))
        except (TypeError, ValueError):
            minutes = 5
        self.store.update_settings(
            catch_up_seconds=minutes * 60,
            restore_target_geometry=bool(self.restore_var.get()),
        )
        self._set_status("执行设置已保存")

    def _on_theme_change(self, value: str) -> None:
        if value == self.theme.mode:
            return
        self._sync_draft_from_editor()
        self.store.update_settings(theme=value)
        self.theme = Theme(value, winapi.get_system_dpi())
        self.overlay.retheme(self.theme)
        self._build()
        self.refresh_tasks(force=True)
        self._sync_editor_from_draft()
        self._refresh_anchor_labels()
        self._set_status("主题已切换")

    def _toggle_topmost(self) -> None:
        enabled = bool(self.topmost_var.get())
        self.store.update_settings(always_on_top=enabled)
        self._apply_topmost()
        self._set_status("窗口置顶已开启" if enabled else "窗口置顶已关闭")

    def _apply_topmost(self) -> None:
        enabled = bool(self.topmost_var.get())
        try:
            self.root.attributes("-topmost", enabled)
            if self.root_hwnd:
                winapi.set_topmost(self.root_hwnd, enabled)
        except tk.TclError:
            pass

    def _open_data_dir(self) -> None:
        try:
            os.startfile(str(config.BASE_DIR))  # type: ignore[attr-defined]
        except Exception as exc:
            self._set_status(f"无法打开目录：{friendly_error(exc)}")

    def _target_keywords(self) -> list[str]:
        return winapi.parse_keywords(self.keywords_var.get())

    def detect_target(self) -> None:
        try:
            window = winapi.find_target_window(self._target_keywords(), exclude_hwnd=self.root_hwnd)
        except Exception as exc:
            self._set_status(f"检测失败：{friendly_error(exc)}")
            return
        if not window:
            self.target_var.set("未找到目标窗口")
            self._set_status("未找到目标窗口，请确认应用已打开")
            return
        self.store.update_settings(
            target_title=str(window.get("title") or ""),
            target_process=str(window.get("process_name") or ""),
        )
        self.target_var.set(f"目标：{self._short(str(window.get('title') or window.get('process_name')))}")
        self._set_status(f"已找到：{window.get('title') or window.get('process_name')}")

    @staticmethod
    def _short(text: str, limit: int = 28) -> str:
        text = text.strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _capture_target(self, hwnd: int) -> Optional[locator.Frame]:
        """隐藏自身、激活目标窗口后截图，避免把自己截进去。"""

        try:
            self.root.withdraw()
            self.root.update_idletasks()
        except tk.TclError:
            pass
        try:
            winapi.ensure_foreground(hwnd)
            time.sleep(0.35)
            return locator.grab(hwnd)
        except Exception as exc:
            self._set_status(f"截图失败：{friendly_error(exc)}")
            return None
        finally:
            try:
                self.root.deiconify()
                self._apply_topmost()
                self.root.lift()
            except tk.TclError:
                pass

    def calibrate(self) -> None:
        try:
            window = winapi.find_target_window(self._target_keywords(), exclude_hwnd=self.root_hwnd)
        except Exception as exc:
            messagebox.showerror("识别失败", friendly_error(exc), parent=self.root)
            return
        if not window:
            messagebox.showwarning(
                "没有找到目标窗口",
                "请先打开目标应用，或到设置里调整窗口关键词。",
                parent=self.root,
            )
            return

        hwnd = int(window["hwnd"])
        frame = self._capture_target(hwnd)
        if frame is None:
            messagebox.showwarning("无法截图", "无法读取目标窗口画面。", parent=self.root)
            return

        CalibrationDialog(
            self.root,
            self.theme,
            self.store.settings(),
            window,
            frame,
            recapture=lambda: self._capture_target(hwnd),
            on_save=self._apply_calibration,
        )

    def _apply_calibration(self, patch: dict[str, Any]) -> None:
        self.store.update_settings(**patch)
        self._calibrated_at = str(patch.get("calibrated_at") or "")
        self._refresh_anchor_labels()
        self._list_signature = None
        self.log("success", "识别结果已保存。")
        self._set_status("识别结果已保存")

    def manual_anchor(self, kind: str) -> None:
        labels = {"composer": "输入框", "send": "发送按钮", "search": "对话搜索"}
        name = labels.get(kind, "位置")
        if not messagebox.askokcancel(
            f"手动指定{name}",
            f"点击“确定”后窗口会隐藏。\n\n把鼠标移到{name}上，不要点击，然后在 15 秒内按一次 F8。",
            parent=self.root,
        ):
            return

        capture = VirtualCapture(15.0)
        point: Optional[tuple[int, int]] = None
        try:
            self.root.withdraw()
            self.root.update_idletasks()
            capture.start()
            point = capture.wait()
        finally:
            capture.stop()
            try:
                self.root.deiconify()
                self._apply_topmost()
                self.root.lift()
            except tk.TclError:
                pass

        if point is None:
            messagebox.showwarning("捕获失败", capture.error or "没有收到 F8。", parent=self.root)
            return
        try:
            hwnd = winapi.point_to_root_window(*point)
            rect = winapi.get_client_rect(hwnd) if hwnd else None
            if not rect:
                raise AutomationError("无法读取该位置所在窗口。")
            anchor = {
                "rx": round((point[0] - rect["left"]) / max(1, rect["width"]), 6),
                "ry": round((point[1] - rect["top"]) / max(1, rect["height"]), 6),
                "space": "client",
            }
            self.store.update_settings(**{f"anchor_{kind}": anchor})
            self._refresh_anchor_labels()
            self._set_status(f"{name}已保存")
        except Exception as exc:
            messagebox.showwarning("捕获失败", friendly_error(exc), parent=self.root)

    def _refresh_anchor_labels(self) -> None:
        settings = self.store.settings()
        for key, variable in self.anchor_vars.items():
            anchor = settings.get(f"anchor_{key}")
            if isinstance(anchor, dict):
                try:
                    variable.set(f"已保存 · {float(anchor.get('rx', 0)):.3f}, {float(anchor.get('ry', 0)):.3f}")
                except (TypeError, ValueError):
                    variable.set("已保存")
            else:
                variable.set("未设置")
        title = str(settings.get("target_title") or "")
        if title:
            self.target_var.set(f"目标：{self._short(title)}")
        stamp = str(settings.get("calibrated_at") or "")
        self.calibrated_var.set(f"上次识别：{config.format_datetime(stamp)}" if stamp else "尚未识别过目标窗口")

    # ------------------------------------------------------------------
    # 调度与事件
    # ------------------------------------------------------------------

    def _start_scheduler(self) -> None:
        self.scheduler = Scheduler(self.store, self.emit, exclude_hwnd=self.root_hwnd)
        self.scheduler.start()
        self.engine_var.set("调度运行中")
        self._refresh_anchor_labels()

    def emit(self, event: str, payload: Any) -> None:
        self.events.put((event, payload))

    def _capture_root_hwnd(self) -> None:
        try:
            self.root_hwnd = winapi.root_window(int(self.root.winfo_id()))
            if self.scheduler:
                self.scheduler.exclude_hwnd = self.root_hwnd
            self._apply_topmost()
            self._apply_frame_theme()
        except tk.TclError:
            pass

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    level, message = payload
                    self.log(level, message)
                elif event == "refresh":
                    self.refresh_tasks()
                elif event == "prepare":
                    try:
                        self.root.withdraw()
                    except tk.TclError:
                        pass
                    if isinstance(payload, object) and hasattr(payload, "set"):
                        payload.set()
                elif event == "restore":
                    try:
                        self.root.deiconify()
                        self._apply_topmost()
                    except tk.TclError:
                        pass
                elif event == "overlay":
                    try:
                        apply_event(self.overlay, payload)
                    except tk.TclError:
                        pass
        except queue.Empty:
            pass
        try:
            self._after_ids = [self.root.after(120, self._drain_events)]
        except tk.TclError:
            pass

    def log(self, level: str, message: str) -> None:
        level = level if level in {"info", "success", "warning", "error"} else "info"
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        line = f"{stamp}  {message}"
        self._log_lines.append((level, line))
        if len(self._log_lines) > 600:
            del self._log_lines[: len(self._log_lines) - 600]
        if hasattr(self, "log_text") and (self._log_filter in ("all", level)):
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n", level)
                self.log_text.configure(state="disabled")
                self.log_text.see("end")
            except tk.TclError:
                pass
        self._set_status(message)
        try:
            with config.LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {message}\n")
        except Exception:
            pass

    def _set_status(self, message: str) -> None:
        """状态栏只放一行结论，长句子折成单行并截断，避免变成一段说明文字。"""

        text = " ".join(str(message or "").split())
        if len(text) > 56:
            text = text[:55] + "…"
        self.status_var.set(text)

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if not messagebox.askyesno("退出", "退出后定时任务将不再执行。确定退出吗？", parent=self.root):
            return
        try:
            geometry = self.root.geometry()
            if geometry:
                self.store.update_settings(window_size=geometry)
        except Exception:
            pass
        for after_id in list(getattr(self, "_after_ids", [])):
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._after_ids = []
        try:
            self.overlay.destroy()
        except Exception:
            pass
        if self.scheduler:
            self.scheduler.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
