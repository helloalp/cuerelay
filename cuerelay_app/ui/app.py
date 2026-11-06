# -*- coding: utf-8 -*-
"""主窗口。

三个视图：任务、日志、设置。左侧窄导航栏常驻，右侧内容区切换。
没有装饰性文案、没有 emoji、没有动画，只有层级、留白和可读的字号。
"""

from __future__ import annotations

import datetime as _dt
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from .. import apps as apps_module
from .. import config, locator, store as store_module, winapi
from ..automation import AutomationError
from ..config import friendly_error, iso_datetime, now_local, parse_datetime
from ..conversations import Conversation
from ..hotkey import VirtualCapture
from ..scheduler import Scheduler
from ..store import (
    KIND_LABELS,
    TaskStore,
    advance_next_run_past_now,
    content_snippet,
    is_lifecycle,
    kind_label,
    next_run_text,
    normalize_snippet,
    task_state,
)
from ..toolbox import Toolbox, ToolboxError
from .calibrate import CalibrationDialog
from .overlay import Overlay, apply_event
from .palette import Command, CommandPalette
from .theme import Theme
from .tidy import TidyDialog
from .widgets import Button, Field, ScrollArea, Segmented, Switch, TextArea, card, dispatch_wheel

ACTION_OPTIONS = (("click", "点击发送"), ("enter", "Enter 发送"))
SCHEDULE_OPTIONS = (("once", "单次"), ("daily", "每天"), ("interval", "间隔"))
KIND_OPTIONS = (("prompt", "发送提示词"), ("launch", "启动应用"), ("close", "关闭应用"))
DELAY_OPTIONS = (("0.8", "0.8 秒"), ("1.2", "1.2 秒"), ("1.8", "1.8 秒"), ("2.5", "2.5 秒"))
THEME_OPTIONS = (("light", "浅色"), ("dark", "深色"))
FILTER_OPTIONS = (("all", "全部"), ("info", "信息"), ("success", "成功"), ("warning", "警告"), ("error", "错误"))
LOG_MODE_OPTIONS = (("log", "运行日志"), ("history", "执行记录"))

# 分组默认展开状态。失败要立刻看到，"已停止"往往是几十条历史，默认收起，
# 否则一打开就是一大片同名的旧任务。
GROUP_DEFAULT_OPEN = {"active": True, "failed": True, "idle": False}

# 对话页的分区小标题。侧边栏里的分区名照抄，用户一眼能对上。
_SECTION_TITLES = {"pinned": "置顶", "projects": "项目", "chats": "对话"}


def _clip(text: str, limit: int) -> str:
    """压成一行并截断，用于状态行这类只有一行的位置。"""

    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _empty_draft() -> dict[str, Any]:
    now = now_local()
    return {
        "id": None,
        "kind": "prompt",
        "name": "",
        "content": "",
        "conversation": "",
        "schedule": "once",
        "date": now.strftime("%Y-%m-%d"),
        "time": (now + _dt.timedelta(minutes=5)).strftime("%H:%M"),
        "interval": "60",
        "action": "click",
        "enabled": True,
        # 失败重试只对"还没把内容交出去"的失败生效，默认关闭：
        # 大多数失败是额度或网络问题，重试只会白等一轮。
        "retry": "0",
        "retry_delay": "30",
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

        # 片段编辑器的状态。和任务草稿一样，靠"签名"判断有没有未保存的修改。
        self.selected_snippet_id: Optional[str] = None
        self._snippet_baseline: Optional[tuple] = None
        self._snippet_rows: dict[str, dict[str, Any]] = {}

        # 对话列表是"扫描一次、手动刷新"的：每次扫描要滚动侧边栏、跑好几次 OCR，
        # 不可能像应用状态那样每两秒刷一遍。
        self._conversations: list[Conversation] = []
        self._conversation_scanning = False
        self._conversation_rows: list[dict[str, Any]] = []

        self._palette: Optional[CommandPalette] = None
        self._apps_poll_id: Optional[str] = None
        self._log_mode = "log"
        self._snippet_signature: Optional[tuple] = None

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
        self._refresh_apps()
        self._apps_poll_id = self.root.after(2500, self._poll_apps)
        self._bind_shortcuts()
        self.refresh_snippets()
        if not str(self.store.setting("v2ray_path") or ""):
            # 第一次打开就顺手找一下 v2rayN，省得用户还要自己去点「自动检测」。
            threading.Thread(target=self._autodetect_v2ray, daemon=True).start()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _create_variables(self, settings: dict[str, Any]) -> None:
        self.name_var = tk.StringVar(value=self.draft["name"])
        self.kind_var = tk.StringVar(value=self.draft["kind"])
        self.conversation_var = tk.StringVar(value=self.draft["conversation"])
        self.schedule_var = tk.StringVar(value=self.draft["schedule"])
        self.date_var = tk.StringVar(value=self.draft["date"])
        self.time_var = tk.StringVar(value=self.draft["time"])
        self.interval_var = tk.StringVar(value=self.draft["interval"])
        self.action_var = tk.StringVar(value=self.draft["action"])
        self.retry_var = tk.StringVar(value=self.draft["retry"])
        self.retry_delay_var = tk.StringVar(value=self.draft["retry_delay"])
        self.enabled_var = tk.BooleanVar(value=bool(self.draft["enabled"]))
        self.filter_var = tk.StringVar(value="")
        self.log_filter_var = tk.StringVar(value="all")
        self.log_mode_var = tk.StringVar(value="log")
        self.snippet_name_var = tk.StringVar(value="")
        self.snippet_filter_var = tk.StringVar(value="")
        self.snippet_count_var = tk.StringVar(value="0 个片段")
        self.chat_status_var = tk.StringVar(value="还没有扫描")

        self.theme_var = tk.StringVar(value=str(settings.get("theme") or "light"))
        self.topmost_var = tk.BooleanVar(value=bool(settings.get("always_on_top", True)))
        self.restore_var = tk.BooleanVar(value=bool(settings.get("restore_target_geometry", True)))
        self.recognition_var = tk.BooleanVar(value=bool(settings.get("recognition_enabled", True)))
        self.verify_paste_var = tk.BooleanVar(value=bool(settings.get("verify_paste", True)))
        self.verify_send_var = tk.BooleanVar(value=bool(settings.get("verify_send", True)))
        self.strict_var = tk.BooleanVar(value=bool(settings.get("strict_conversation", True)))
        self.overlay_var = tk.BooleanVar(value=bool(settings.get("show_overlay", True)))
        self.keywords_var = tk.StringVar(value=str(settings.get("target_keywords") or ""))
        self.auto_launch_var = tk.BooleanVar(value=bool(settings.get("auto_launch_target", True)))
        self.v2ray_path_var = tk.StringVar(value=str(settings.get("v2ray_path") or ""))
        self.launch_v2ray_var = tk.BooleanVar(value=bool(settings.get("launch_v2ray_first", True)))
        self.autostart_var = tk.BooleanVar(value=apps_module.autostart_enabled())
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
        self.snippet_filter_var.trace_add("write", lambda *_: self.refresh_snippets(force=True))

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
        for key, text in (
            ("tasks", "任务"),
            ("snippets", "片段"),
            ("chats", "对话"),
            ("apps", "应用"),
            ("log", "日志"),
            ("settings", "设置"),
        ):
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
        # 命令面板是快捷键驱动的，不写出来没人会去猜。
        hint = tk.Label(
            bottom,
            text="Ctrl+K 命令",
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
            cursor="hand2",
        )
        hint.pack(fill="x", pady=(theme.px(6), 0))
        hint.bind("<Button-1>", lambda _event: self.open_palette())

    def _build_content(self, parent: tk.Frame) -> None:
        theme = self.theme
        content = tk.Frame(parent, bg=theme.c("bg"))
        content.pack(side="left", fill="both", expand=True, padx=(0, theme.px(20)), pady=(0, theme.px(12)))
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self.views: dict[str, tk.Frame] = {}
        for key in ("tasks", "snippets", "chats", "apps", "log", "settings"):
            frame = tk.Frame(content, bg=theme.c("bg"))
            frame.grid(row=0, column=0, sticky="nsew")
            self.views[key] = frame

        self._build_tasks_view(self.views["tasks"])
        self._build_snippets_view(self.views["snippets"])
        self._build_chats_view(self.views["chats"])
        self._build_apps_view(self.views["apps"])
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

    # ------------------------------------------------------------------
    # 片段视图
    # ------------------------------------------------------------------

    def _build_snippets_view(self, parent: tk.Frame) -> None:
        theme = self.theme
        left = tk.Frame(parent, bg=theme.c("bg"), width=theme.px(286))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        head = tk.Frame(left, bg=theme.c("bg"))
        head.pack(fill="x", pady=(theme.px(16), theme.px(10)))
        tk.Label(
            head,
            textvariable=self.snippet_count_var,
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
            anchor="w",
        ).pack(side="left")
        Button(head, theme, "新建", self.new_snippet, kind="ghost", size="sm").pack(side="right")

        search = Field(left, theme, "", self.snippet_filter_var, bg_key="bg", placeholder="搜索片段")
        search.pack(fill="x", pady=(0, theme.px(10)))
        search.entry.configure(font=theme.font("small"))

        wrap = tk.Frame(left, bg=theme.c("bg"))
        wrap.pack(fill="both", expand=True)
        self.snippet_scroll = ScrollArea(wrap, theme, "bg")
        self.snippet_scroll.pack(fill="both", expand=True)
        self.snippet_list_body = self.snippet_scroll.body

        right = tk.Frame(parent, bg=theme.c("bg"))
        right.pack(side="left", fill="both", expand=True, padx=(theme.px(20), 0))
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        # 片段编辑器不用外层滚动：内容框自己会滚，外层再套一层只会让卡片
        # 停在自然高度，下面留一大块空白。
        pad = theme.px(20)
        outer, body = card(right, theme, "surface")
        outer.grid(row=0, column=0, sticky="nsew")
        self.snippet_name_field = Field(body, theme, "名称", self.snippet_name_var)
        self.snippet_name_field.pack(fill="x", padx=pad, pady=(pad, 0))
        self.snippet_area = TextArea(body, theme, "内容", height=8)
        self.snippet_area.pack(fill="both", expand=True, padx=pad, pady=(theme.px(14), pad))

        actions = tk.Frame(right, bg=theme.c("bg"))
        actions.grid(row=1, column=0, sticky="ew", pady=(theme.px(12), 0))
        self.snippet_delete_button = Button(actions, theme, "删除", self.delete_snippet, kind="danger")
        self.snippet_delete_button.pack(side="left")
        # 片段的主按钮是「发送」：这个页面的意义就是把常用的话一句话发出去。
        Button(actions, theme, "发送", self.send_snippet, kind="primary").pack(side="right")
        Button(actions, theme, "填入任务", self.snippet_to_task, kind="ghost").pack(
            side="right", padx=(0, theme.px(8))
        )
        Button(actions, theme, "保存", self.save_snippet, kind="ghost").pack(
            side="right", padx=(0, theme.px(8))
        )
        self.snippet_state = tk.Label(
            actions,
            text="",
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        )
        self.snippet_state.pack(side="left", padx=(theme.px(12), 0))

    def refresh_snippets(self, force: bool = False) -> None:
        if not hasattr(self, "snippet_list_body"):
            return
        items = self.store.snippets()
        total = len(items)
        keyword = self.snippet_filter_var.get().strip().lower()
        if keyword:
            items = [
                item
                for item in items
                if keyword in str(item.get("name") or "").lower()
                or keyword in str(item.get("content") or "").lower()
            ]
        # 常用的排前面：片段库的价值在于"那几句我天天用"。
        items.sort(key=lambda item: (-int(item.get("use_count") or 0), str(item.get("name") or "")))

        signature = tuple((item["id"], item["name"], int(item.get("use_count") or 0)) for item in items)
        if not force and signature == self._snippet_signature:
            self._paint_snippets()
            return
        self._snippet_signature = signature

        for child in self.snippet_list_body.winfo_children():
            child.destroy()
        self._snippet_rows.clear()

        if not items:
            tk.Label(
                self.snippet_list_body,
                text="没有匹配的片段" if keyword else "还没有片段，点右上角「新建」",
                bg=self.theme.c("bg"),
                fg=self.theme.c("text_3"),
                font=self.theme.font("small"),
                anchor="w",
            ).pack(fill="x", padx=self.theme.px(4), pady=self.theme.px(14))
        for item in items:
            self._render_snippet_row(item)

        self.snippet_count_var.set(f"{total} 个片段")
        self._paint_snippets()

    def _render_snippet_row(self, snippet: dict[str, Any]) -> None:
        theme = self.theme
        row = tk.Frame(self.snippet_list_body, bg=theme.c("bg"), cursor="hand2")
        row.pack(fill="x", pady=(0, theme.px(2)))

        inner = tk.Frame(row, bg=theme.c("bg"))
        inner.pack(fill="x", padx=theme.px(10), pady=theme.px(7))

        name = tk.Label(
            inner,
            text=str(snippet.get("name") or ""),
            bg=theme.c("bg"),
            fg=theme.c("text"),
            font=theme.font("body"),
            anchor="w",
            justify="left",
        )
        name.pack(fill="x")

        used = int(snippet.get("use_count") or 0)
        preview = " ".join(str(snippet.get("content") or "").split())
        if len(preview) > 40:
            preview = preview[:39] + "…"
        meta = f"用过 {used} 次 · {preview}" if used else preview
        detail = tk.Label(
            inner,
            text=meta,
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
            justify="left",
        )
        detail.pack(fill="x", pady=(theme.px(3), 0))

        for widget in (row, inner, name, detail):
            widget.bind("<Button-1>", lambda _event, i=snippet["id"]: self.select_snippet(i))
        self._snippet_rows[str(snippet["id"])] = {"frame": row, "inner": inner, "name": name, "detail": detail}

    def _paint_snippets(self) -> None:
        theme = self.theme
        for snippet_id, widgets in self._snippet_rows.items():
            active = snippet_id == self.selected_snippet_id
            fill = theme.c("accent_soft") if active else theme.c("bg")
            for widget in widgets.values():
                try:
                    widget.configure(bg=fill)
                except tk.TclError:
                    pass
            if active:
                widgets["name"].configure(fg=theme.c("accent_label"))
                widgets["detail"].configure(fg=theme.c("accent_label"))
            else:
                widgets["name"].configure(fg=theme.c("text"))
                widgets["detail"].configure(fg=theme.c("text_3"))

    def _snippet_signature_now(self) -> tuple:
        return (self.snippet_name_var.get().strip(), self.snippet_area.get())

    def _snippet_dirty(self) -> bool:
        if self._snippet_baseline is None:
            return bool(self.snippet_name_var.get().strip() or self.snippet_area.get().strip())
        return self._snippet_signature_now() != self._snippet_baseline

    def _confirm_discard_snippet(self) -> bool:
        if not self._snippet_dirty():
            return True
        return messagebox.askyesno("放弃修改", "当前片段有未保存的修改，确定放弃吗？", parent=self.root)

    def _set_snippet_state(self, text: str, tone: str = "text_3") -> None:
        try:
            self.snippet_state.configure(text=_clip(text, 40), fg=self.theme.c(tone))
        except tk.TclError:
            pass

    def new_snippet(self) -> None:
        if not self._confirm_discard_snippet():
            return
        self.selected_snippet_id = None
        self.snippet_name_var.set("")
        self.snippet_area.set("")
        self._snippet_baseline = ("", "")
        self._paint_snippets()
        self._set_snippet_state("")
        self.snippet_area.text.focus_set()

    def select_snippet(self, snippet_id: str) -> None:
        if snippet_id == self.selected_snippet_id:
            return
        if not self._confirm_discard_snippet():
            return
        item = self.store.get_snippet(snippet_id)
        if item is None:
            return
        self.selected_snippet_id = snippet_id
        self.snippet_name_var.set(str(item.get("name") or ""))
        self.snippet_area.set(str(item.get("content") or ""))
        self._snippet_baseline = self._snippet_signature_now()
        self._paint_snippets()
        self._set_snippet_state("")

    def save_snippet(self) -> bool:
        content = self.snippet_area.get()
        if not content.strip():
            self._set_snippet_state("内容不能为空", "danger")
            return False
        name = self.snippet_name_var.get().strip()
        if self.selected_snippet_id:
            self.store.replace_snippet(self.selected_snippet_id, {"name": name, "content": content})
            message = "已保存"
        else:
            item = self.store.add_snippet({"name": name, "content": content})
            self.selected_snippet_id = str(item["id"])
            message = "已新建"
        self._snippet_baseline = self._snippet_signature_now()
        self.refresh_snippets(force=True)
        self._set_snippet_state(message, "success")
        return True

    def delete_snippet(self) -> None:
        if not self.selected_snippet_id:
            self._set_snippet_state("还没有选中片段", "warning")
            return
        name = self.snippet_name_var.get().strip() or "这个片段"
        if not messagebox.askyesno("删除片段", f"确定删除「{name}」吗？", parent=self.root):
            return
        self.store.delete_snippet(self.selected_snippet_id)
        self.new_snippet()
        self.refresh_snippets(force=True)
        self._set_snippet_state("已删除", "success")

    def send_snippet(self) -> None:
        """把片段立刻发出去。保存不是前提，但改了内容要先存下来。"""

        if not self.save_snippet():
            return
        snippet_id = self.selected_snippet_id
        content = self.snippet_area.get()
        name = self.snippet_name_var.get().strip() or "片段"

        def work(toolbox: Toolbox) -> str:
            outcome = toolbox.send(content, name=name)
            return outcome.message or "已发送"

        self._run_toolbox(f"发送「{name}」", work)
        if snippet_id:
            self.store.touch_snippet(snippet_id)
            self.refresh_snippets(force=True)

    def snippet_to_task(self) -> None:
        """把片段变成一条新任务草稿。不直接保存，先让用户定计划。"""

        content = self.snippet_area.get()
        if not content.strip():
            self._set_snippet_state("内容不能为空", "danger")
            return
        if not self._confirm_discard():
            return
        self.selected_id = None
        self.draft = _empty_draft()
        self.draft["name"] = self.snippet_name_var.get().strip()
        self.draft["content"] = content
        self._sync_editor_from_draft()
        self._baseline = None
        self.show_view("tasks")
        self._set_status("已填入新任务，选好计划后保存")

    # ------------------------------------------------------------------
    # 对话视图
    # ------------------------------------------------------------------

    def _build_chats_view(self, parent: tk.Frame) -> None:
        theme = self.theme
        head = tk.Frame(parent, bg=theme.c("bg"))
        head.pack(fill="x", pady=(theme.px(16), theme.px(6)))
        tk.Label(
            head,
            text="对话",
            bg=theme.c("bg"),
            fg=theme.c("text"),
            font=theme.font("section"),
        ).pack(side="left")
        tk.Label(
            head,
            textvariable=self.chat_status_var,
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
        ).pack(side="left", padx=(theme.px(12), 0))
        Button(head, theme, "扫描对话", self.scan_conversations, kind="primary", size="sm").pack(side="right")
        Button(head, theme, "新建对话", self.new_conversation, kind="ghost", size="sm").pack(
            side="right", padx=(0, theme.px(8))
        )

        tk.Label(
            parent,
            text="读 Codex 侧边栏里当前能看到的标题；点一条切过去，切换后核对标题栏。",
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
            anchor="w",
            justify="left",
            wraplength=theme.px(720),
        ).pack(fill="x", pady=(0, theme.px(12)))

        outer, inner = card(parent, theme, "surface")
        outer.pack(fill="both", expand=True)
        self.chat_scroll = ScrollArea(inner, theme, "surface")
        self.chat_scroll.pack(fill="both", expand=True, padx=theme.px(6), pady=theme.px(6))
        self.chat_list_body = self.chat_scroll.body
        self._render_chats()

    def _render_chats(self) -> None:
        if not hasattr(self, "chat_list_body"):
            return
        theme = self.theme
        for child in self.chat_list_body.winfo_children():
            child.destroy()
        self._conversation_rows.clear()

        if self._conversation_scanning:
            tk.Label(
                self.chat_list_body,
                text="正在读取侧边栏…",
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                anchor="w",
            ).pack(fill="x", padx=theme.px(12), pady=theme.px(14))
            return

        if not self._conversations:
            tk.Label(
                self.chat_list_body,
                text="还没有扫描。点右上角「扫描对话」读一次侧边栏。",
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                anchor="w",
            ).pack(fill="x", padx=theme.px(12), pady=theme.px(14))
            return

        section = ""
        for index, item in enumerate(self._conversations):
            if item.section != section:
                section = item.section
                tk.Label(
                    self.chat_list_body,
                    text=_SECTION_TITLES.get(section, "对话"),
                    bg=theme.c("surface"),
                    fg=theme.c("text_3"),
                    font=theme.font("small_strong"),
                    anchor="w",
                ).pack(fill="x", padx=theme.px(12), pady=(theme.px(10), theme.px(2)))

            row = tk.Frame(self.chat_list_body, bg=theme.c("surface"), cursor="hand2")
            row.pack(fill="x")
            label = tk.Label(
                row,
                text=item.title,
                bg=theme.c("surface"),
                fg=theme.c("text"),
                font=theme.font("body"),
                anchor="w",
                padx=theme.px(12),
                pady=theme.px(7),
                justify="left",
            )
            label.pack(fill="x")
            for widget in (row, label):
                widget.bind("<Button-1>", lambda _event, title=item.title: self.open_conversation(title))
                widget.bind("<Enter>", lambda _event, r=row, l=label: self._hover_chat(r, l, True))
                widget.bind("<Leave>", lambda _event, r=row, l=label: self._hover_chat(r, l, False))
            self._conversation_rows.append({"frame": row, "label": label, "title": item.title})

    def _hover_chat(self, row: tk.Frame, label: tk.Label, hover: bool) -> None:
        theme = self.theme
        fill = theme.c("surface_3") if hover else theme.c("surface")
        try:
            row.configure(bg=fill)
            label.configure(bg=fill)
        except tk.TclError:
            pass

    def scan_conversations(self) -> None:
        if self._conversation_scanning:
            return
        self._conversation_scanning = True
        self.chat_status_var.set("正在读取…")
        self._render_chats()

        def work(toolbox: Toolbox) -> str:
            try:
                items = toolbox.conversations()
            except Exception as exc:
                self.emit("chats", exc)
                raise
            self.emit("chats", items)
            return f"扫描到 {len(items)} 个对话"

        self.scheduler.run_exclusive(work, "扫描对话")

    def _apply_conversations(self, payload: Any) -> None:
        self._conversation_scanning = False
        if isinstance(payload, Exception):
            self.chat_status_var.set("扫描失败")
        else:
            self._conversations = list(payload)
            self.chat_status_var.set(f"{len(self._conversations)} 个对话")
        self._render_chats()

    def open_conversation(self, title: str) -> None:
        def work(toolbox: Toolbox) -> str:
            return toolbox.open_conversation(title)

        self._run_toolbox(f"切换「{title}」", work)

    def new_conversation(self) -> None:
        self._run_toolbox("新建对话", lambda toolbox: toolbox.new_conversation())

    def stop_generation(self) -> None:
        self._run_toolbox("停止生成", lambda toolbox: toolbox.stop_generation())

    def clear_input(self) -> None:
        self._run_toolbox("清空输入框", lambda toolbox: toolbox.clear_input())

    # ------------------------------------------------------------------
    # 工具箱动作的公共入口
    # ------------------------------------------------------------------

    def _run_toolbox(self, label: str, work: Callable[[Toolbox], Any]) -> None:
        """把一次工具箱动作交给调度器执行。

        调度器会隐藏主窗口、拿到窗口锁、跑完再恢复——所以界面按钮和定时任务
        不会同时去点同一个窗口。
        """

        settings = self.store.settings()
        log = self.log_async
        emit = self.emit
        exclude = self.root_hwnd

        def runner() -> str:
            result = work(Toolbox(settings, log, exclude_hwnd=exclude, emit=emit))
            if isinstance(result, str) and result:
                return result
            return f"{label}完成"

        self.scheduler.run_exclusive(runner, label)
        self._set_status(f"{label}…")

    # ------------------------------------------------------------------
    # 应用视图
    # ------------------------------------------------------------------

    def _build_apps_view(self, parent: tk.Frame) -> None:
        theme = self.theme
        scroll = ScrollArea(parent, theme, "bg")
        scroll.pack(fill="both", expand=True)
        body = scroll.body

        self.app_cards: dict[str, dict[str, Any]] = {}
        self._app_card(
            body,
            "codex",
            "Codex",
            "消息最终发到这里。没开的时候执行任务会先把它拉起来。",
            [
                ("启动", lambda: self._app_launch_codex(), "primary"),
                ("关闭", lambda: self._app_close_codex(), "ghost"),
            ],
        )
        self._app_card(
            body,
            "v2ray",
            "v2rayN",
            "先起代理再起 Codex，避免 Codex 打开后连不上。",
            [
                ("启动", lambda: self._app_launch_v2ray(), "primary"),
                ("重新检测", lambda: self._app_detect_v2ray(), "ghost"),
            ],
        )

        quick = tk.Frame(body, bg=theme.c("bg"))
        quick.pack(fill="x", pady=(theme.px(4), theme.px(16)))
        Button(
            quick,
            theme,
            "一键启动：v2rayN → Codex",
            self._app_start_chain,
            kind="primary",
        ).pack(side="left")

        tk.Label(
            body,
            text="定时开关",
            bg=theme.c("bg"),
            fg=theme.c("text_2"),
            font=theme.font("small_strong"),
            anchor="w",
        ).pack(fill="x", pady=(0, theme.px(8)))

        schedule_row = tk.Frame(body, bg=theme.c("bg"))
        schedule_row.pack(fill="x")
        Button(
            schedule_row,
            theme,
            "新建「启动 Codex」任务",
            lambda: self.new_lifecycle_task("launch"),
        ).pack(side="left")
        Button(
            schedule_row,
            theme,
            "新建「关闭 Codex」任务",
            lambda: self.new_lifecycle_task("close"),
        ).pack(side="left", padx=(theme.px(8), 0))

    def _app_card(
        self,
        parent: tk.Frame,
        key: str,
        title: str,
        hint: str,
        actions: list[tuple[str, Callable[[], None], str]],
    ) -> None:
        theme = self.theme
        outer, body = card(parent, theme, "surface")
        outer.pack(fill="x", pady=(theme.px(12), 0))
        pad = theme.px(20)

        head = tk.Frame(body, bg=theme.c("surface"))
        head.pack(fill="x", padx=pad, pady=(pad, 0))
        tk.Label(
            head,
            text=title,
            bg=theme.c("surface"),
            fg=theme.c("text"),
            font=theme.font("title"),
            anchor="w",
        ).pack(side="left")
        status = tk.Label(
            head,
            text="—",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
            anchor="w",
        )
        status.pack(side="left", padx=(theme.px(12), 0))
        for text, command, kind in reversed(actions):
            Button(head, theme, text, command, kind=kind, size="sm").pack(
                side="right", padx=(theme.px(6), 0)
            )

        path = tk.Label(
            body,
            text=hint,
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
            anchor="w",
            justify="left",
            wraplength=theme.px(620),
        )
        path.pack(fill="x", padx=pad, pady=(theme.px(8), pad))
        self.app_cards[key] = {"status": status, "path": path, "hint": hint}

    def _refresh_apps(self) -> None:
        if not hasattr(self, "app_cards") or not self.app_cards:
            return
        theme = self.theme
        settings = self.store.settings()
        tones = {"运行中": "success", "在后台": "warning", "未运行": "text_3"}
        try:
            codex = apps_module.codex_app(settings)
            status = codex.status()
            card = self.app_cards.get("codex")
            if card:
                card["status"].configure(text=status, fg=theme.tone(tones.get(status, "text_3")))
                card["path"].configure(text=f"{card['hint']}   {codex.launch_target()}")

            path = str(settings.get("v2ray_path") or "")
            v2ray = apps_module.v2ray_app(settings)
            status = v2ray.status()
            if not path and status != "未运行":
                status = f"{status} · 未绑定"
            card = self.app_cards.get("v2ray")
            if card:
                card["status"].configure(text=status, fg=theme.tone(tones.get(status.split(" ")[0], "text_3")))
                card["path"].configure(
                    text=f"{path}" if path else "还没有绑定路径，点「重新检测」自动找，或到设置里手填。"
                )
            if path and self.v2ray_path_var.get() != path:
                self.v2ray_path_var.set(path)
        except Exception:
            pass

    # -- 应用动作 ------------------------------------------------------

    def _app_worker(self, label: str, worker: Callable[[], str]) -> None:
        """启动/关闭可能要几秒，放到后台线程，结果通过事件队列回到界面。"""

        self._set_status(f"{label}…")

        def run() -> None:
            try:
                message = worker()
                self.emit("log", ("success", message or f"{label}完成"))
            except Exception as exc:
                self.emit("log", ("error", f"{label}失败：{friendly_error(exc)}"))
            finally:
                self.emit("apps", None)

        threading.Thread(target=run, daemon=True).start()

    def _app_launch_codex(self) -> None:
        def worker() -> str:
            window = apps_module.codex_app(self.store.settings(), self.log_async).ensure_visible()
            if window is None:
                raise RuntimeError("没能把 Codex 拉到前台")
            return "Codex 已在前台"

        self._app_worker("启动 Codex", worker)

    def _app_close_codex(self) -> None:
        def worker() -> str:
            spec = apps_module.codex_app(self.store.settings(), self.log_async)
            if spec.status() == "未运行":
                return "Codex 本来就没有运行"
            if not spec.close(force=True):
                raise RuntimeError("进程还在运行")
            return "Codex 已关闭"

        self._app_worker("关闭 Codex", worker)

    def _app_launch_v2ray(self) -> None:
        def worker() -> str:
            ok, message = apps_module.ensure_proxy(self.store.settings(), self.log_async)
            if not ok:
                raise RuntimeError(message)
            return message

        self._app_worker("启动 v2rayN", worker)

    def _app_start_chain(self) -> None:
        def worker() -> str:
            window = apps_module.start_codex(self.store.settings(), self.log_async)
            if window is None:
                raise RuntimeError("Codex 没能起来")
            return "v2rayN 与 Codex 都已就绪"

        self._app_worker("一键启动", worker)

    def _app_detect_v2ray(self) -> None:
        def worker() -> str:
            path = apps_module.detect_v2ray_path()
            if not path:
                raise RuntimeError("没找到 v2rayN.exe，请到设置里手动指定")
            self.store.update_settings(v2ray_path=path)
            return f"已绑定 v2rayN：{path}"

        self._app_worker("检测 v2rayN", worker)

    def log_async(self, level: str, message: str) -> None:
        """后台线程里记日志，走事件队列，不直接碰 Tk。"""

        self.emit("log", (level, message))

    def _autodetect_v2ray(self) -> None:
        """启动时自动绑定 v2rayN。找不到就安静地算了，用户还能手填。"""

        try:
            path = apps_module.detect_v2ray_path()
        except Exception:
            return
        if not path:
            return
        self.store.update_settings(v2ray_path=path)
        self.emit("log", ("info", f"已自动绑定 v2rayN：{path}"))
        self.emit("apps", None)

    def new_lifecycle_task(self, kind: str) -> None:
        """从「应用」页直接建一个定时开关任务。"""

        if not self._confirm_discard():
            return
        self.selected_id = None
        self.draft = _empty_draft()
        self.draft["kind"] = kind
        self.draft["name"] = "启动 Codex" if kind == "launch" else "关闭 Codex"
        self.draft["schedule"] = "daily"
        self._sync_editor_from_draft()
        self._baseline = self._editor_signature()
        self.editor_title.configure(text="新建任务")
        self.delete_button.set_enabled(False)
        self.editor_state.configure(text="", fg=self.theme.c("text_3"))
        self._apply_selection()
        self.show_view("tasks")
        self._set_status(f"新建：{self.draft['name']}，设好时间后保存")

    # ------------------------------------------------------------------
    # 编辑区
    # ------------------------------------------------------------------

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

        # 类型决定下面出现哪些字段：发送提示词要写内容、选发送方式；开关应用不需要。
        Segmented(body, theme, KIND_OPTIONS, self.kind_var, self._on_kind_change).pack(
            fill="x", padx=pad, pady=(0, theme.px(14))
        )

        self.name_field = Field(body, theme, "名称", self.name_var)
        self.name_field.pack(fill="x", padx=pad)

        # 只属于「发送提示词」的字段各自装进一个分组。开关类任务把分组整块收起，
        # 剩下的就是：类型 -> 名称 -> 计划，一屏看完。
        self.prompt_group = tk.Frame(body, bg=theme.c("surface"))
        self.prompt_group.pack(fill="x", after=self.name_field)

        # 提示词不再吃掉全部剩余高度：把计划、发送方式、目标对话留在首屏可见，
        # 用户一眼就能看完整条流程（写什么 -> 发到哪 -> 什么时候发 -> 怎么发）。
        self.prompt_area = TextArea(self.prompt_group, theme, "提示词", height=7)
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
        self.schedule_segment = Segmented(
            body, theme, SCHEDULE_OPTIONS, self.schedule_var, self._on_schedule_change
        )
        self.schedule_segment.pack(fill="x", padx=pad)

        self.schedule_holder = tk.Frame(body, bg=theme.c("surface"))
        self.schedule_holder.pack(fill="x", padx=pad, pady=(theme.px(10), 0))

        self.time_row = tk.Frame(self.schedule_holder, bg=theme.c("surface"))
        date_field = Field(self.time_row, theme, "日期", self.date_var)
        date_field.pack(side="left", fill="x", expand=True)
        time_field = Field(self.time_row, theme, "时间", self.time_var)
        time_field.pack(side="left", fill="x", expand=True, padx=(theme.px(12), 0))

        self.interval_row = tk.Frame(self.schedule_holder, bg=theme.c("surface"))
        Field(self.interval_row, theme, "间隔分钟", self.interval_var).pack(fill="x")

        # 重试属于"计划"的一部分：这次没成，要不要再试一次。
        self.retry_row = tk.Frame(self.schedule_holder, bg=theme.c("surface"))
        Field(
            self.retry_row,
            theme,
            "失败重试次数",
            self.retry_var,
            hint="0 表示不重试；已提交内容后不会重试",
        ).pack(side="left", fill="x", expand=True)
        Field(self.retry_row, theme, "重试间隔（秒）", self.retry_delay_var).pack(
            side="left", fill="x", expand=True, padx=(theme.px(12), 0)
        )

        self.send_group = tk.Frame(body, bg=theme.c("surface"))
        self.send_group.pack(fill="x", after=self.schedule_holder)

        tk.Label(
            self.send_group,
            text="发送方式",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        ).pack(fill="x", padx=pad, pady=(theme.px(16), theme.px(6)))
        Segmented(self.send_group, theme, ACTION_OPTIONS, self.action_var).pack(fill="x", padx=pad)

        self.conversation_field = Field(
            self.send_group,
            theme,
            "目标对话",
            self.conversation_var,
            placeholder="留空则发送到当前对话",
        )
        self.conversation_field.pack(fill="x", padx=pad, pady=(theme.px(14), 0))

        self._on_schedule_change(self.schedule_var.get())
        self._on_kind_change(self.kind_var.get())

    def _on_kind_change(self, value: str) -> None:
        """开关类任务不需要提示词、发送方式和目标对话，把这两组整块收起。"""

        lifecycle = value in ("launch", "close")
        for group in (self.prompt_group, self.send_group):
            try:
                if lifecycle:
                    group.pack_forget()
            except tk.TclError:
                pass
        if not lifecycle:
            # 收起再放回时 pack 会排到末尾，用 after= 摆回原来的位置。
            self.prompt_group.pack(fill="x", after=self.name_field)
            self.send_group.pack(fill="x", after=self.schedule_holder)
        # 收起后最后一排是日期/时间，卡片底部要靠它自己留出内边距。
        pad = self.theme.px(20)
        try:
            self.schedule_holder.pack_configure(pady=(self.theme.px(10), pad if lifecycle else 0))
        except tk.TclError:
            pass

    def _on_schedule_change(self, value: str) -> None:
        try:
            self.retry_row.pack_forget()
            if value == "interval":
                self.time_row.pack_forget()
                self.interval_row.pack(fill="x")
            else:
                self.interval_row.pack_forget()
                self.time_row.pack(fill="x")
            self.retry_row.pack(fill="x", pady=(self.theme.px(12), 0))
        except (AttributeError, tk.TclError):
            pass

    # ------------------------------------------------------------------
    # 日志视图
    # ------------------------------------------------------------------

    def _build_log_view(self, parent: tk.Frame) -> None:
        theme = self.theme
        head = tk.Frame(parent, bg=theme.c("bg"))
        head.pack(fill="x", pady=(theme.px(16), theme.px(12)))
        tk.Label(
            head,
            text="日志",
            bg=theme.c("bg"),
            fg=theme.c("text"),
            font=theme.font("section"),
        ).pack(side="left")
        self.log_file_button = Button(head, theme, "打开日志文件", self._open_log_file, kind="ghost", size="sm")
        self.log_file_button.pack(side="right")
        self.log_clear_button = Button(head, theme, "清空", self._clear_current, kind="ghost", size="sm")
        self.log_clear_button.pack(side="right", padx=(0, theme.px(8)))

        mode_row = tk.Frame(parent, bg=theme.c("bg"))
        mode_row.pack(fill="x", pady=(0, theme.px(12)))
        Segmented(mode_row, theme, LOG_MODE_OPTIONS, self.log_mode_var, self._on_log_mode).pack(side="left")

        # 两个面板交替显示。日志是"这一次跑了什么"，记录是"每条任务最后怎么样"，
        # 内容不同但都是"回头看"，放在同一个视图里用一段切换最省位置。
        self.log_panel = tk.Frame(parent, bg=theme.c("bg"))
        self.history_panel = tk.Frame(parent, bg=theme.c("bg"))

        filters = tk.Frame(self.log_panel, bg=theme.c("bg"))
        filters.pack(fill="x", pady=(0, theme.px(12)))
        Segmented(filters, theme, FILTER_OPTIONS, self.log_filter_var, self._on_log_filter).pack(
            side="left"
        )

        outer, inner = card(self.log_panel, theme, "surface")
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

        outer, inner = card(self.history_panel, theme, "surface")
        outer.pack(fill="both", expand=True)
        self.history_scroll = ScrollArea(inner, theme, "surface")
        self.history_scroll.pack(fill="both", expand=True, padx=theme.px(6), pady=theme.px(6))
        self.history_body = self.history_scroll.body

        self._on_log_mode(self.log_mode_var.get())

    def _on_log_mode(self, value: str) -> None:
        self._log_mode = value
        if not hasattr(self, "log_panel"):
            return
        if value == "history":
            self.log_panel.pack_forget()
            self.history_panel.pack(fill="both", expand=True)
            self._render_history()
        else:
            self.history_panel.pack_forget()
            self.log_panel.pack(fill="both", expand=True)
            self._render_log()

    def _render_history(self) -> None:
        if not hasattr(self, "history_body"):
            return
        theme = self.theme
        for child in self.history_body.winfo_children():
            child.destroy()

        items = self.store.history(limit=120)
        if not items:
            tk.Label(
                self.history_body,
                text="还没有执行记录。任务跑过一次之后，结果会留在这里。",
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                anchor="w",
            ).pack(fill="x", padx=theme.px(12), pady=theme.px(14))
            return

        for entry in items:
            row = tk.Frame(self.history_body, bg=theme.c("surface"))
            row.pack(fill="x", padx=theme.px(12), pady=theme.px(5))

            stamp = parse_datetime(entry.get("at"))
            when = config.format_short_datetime(stamp, "")
            tk.Label(
                row,
                text=when,
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("mono"),
                width=14,
                anchor="w",
            ).pack(side="left")

            ok = bool(entry.get("ok"))
            tk.Label(
                row,
                text="成功" if ok else "失败",
                bg=theme.c("surface"),
                fg=theme.c("success") if ok else theme.c("danger"),
                font=theme.font("small_strong"),
                width=5,
                anchor="w",
            ).pack(side="left")

            name = str(entry.get("task_name") or "未命名任务")
            attempt = int(entry.get("attempt") or 0)
            if attempt:
                name = f"{name}（重试 {attempt}）"
            detail = str(entry.get("message") or "")
            duration = int(entry.get("duration_ms") or 0) / 1000.0
            tail = f"{name} · {duration:.1f}s" + (f" · {detail}" if detail else "")
            tk.Label(
                row,
                text=_clip(tail, 90),
                bg=theme.c("surface"),
                fg=theme.c("text_2") if ok else theme.c("danger"),
                font=theme.font("small"),
                anchor="w",
                justify="left",
            ).pack(side="left", fill="x", expand=True)

    def _clear_current(self) -> None:
        if self._log_mode == "history":
            if not self.store.history():
                return
            if not messagebox.askyesno("清空记录", "确定清空全部执行记录吗？", parent=self.root):
                return
            self.store.clear_history()
            self._render_history()
            self._set_status("已清空执行记录")
            return
        self._clear_log()

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

        Switch(
            inner,
            theme,
            "执行前自动拉起目标应用",
            self.auto_launch_var,
            command=self._save_auto_launch,
            bg_key="surface",
        ).pack(fill="x", pady=(theme.px(14), 0))

        # 代理与启动
        card_body = self._settings_card(body, "启动与代理")
        inner = tk.Frame(card_body, bg=theme.c("surface"))
        inner.pack(fill="x", padx=theme.px(20), pady=(0, theme.px(18)))

        v2ray_field = Field(
            inner,
            theme,
            "v2rayN 路径",
            self.v2ray_path_var,
            hint="留空则自动探测；以管理员身份运行的 v2rayN 需要在这里手填一次",
        )
        v2ray_field.pack(fill="x")
        v2ray_field.entry.bind("<FocusOut>", lambda _event: self._save_v2ray_path())
        row = tk.Frame(inner, bg=theme.c("surface"))
        row.pack(fill="x", pady=(theme.px(10), 0))
        Button(row, theme, "自动检测", self._app_detect_v2ray, kind="ghost", size="sm").pack(side="left")
        Button(row, theme, "打开所在文件夹", self._open_v2ray_folder, kind="ghost", size="sm").pack(
            side="left", padx=(theme.px(8), 0)
        )
        Switch(
            inner,
            theme,
            "启动 Codex 前先确保 v2rayN 已运行",
            self.launch_v2ray_var,
            command=self._save_launch_v2ray,
            bg_key="surface",
        ).pack(fill="x", pady=(theme.px(14), 0))
        Switch(
            inner,
            theme,
            "开机自动启动 CueRelay",
            self.autostart_var,
            command=self._save_autostart,
            bg_key="surface",
        ).pack(fill="x", pady=(theme.px(10), 0))
        tk.Label(
            inner,
            text="定时任务只有在 CueRelay 运行时才会执行。",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        ).pack(fill="x", pady=(theme.px(5), 0))

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
        if self.view == "apps":
            self._refresh_apps()
        elif self.view == "snippets":
            self.refresh_snippets()
        elif self.view == "log":
            self._render_log()

    def _poll_apps(self) -> None:
        """应用页的状态会随外部变化（用户自己关掉、托盘恢复），定时刷一下。

        这里只保留一个定时器 id。以前每轮都往 _after_ids 里追加一个，
        跑一整天就是几万条永远不会被取消的字符串。
        """

        try:
            if self.view == "apps":
                self._refresh_apps()
        except tk.TclError:
            return
        try:
            self._apps_poll_id = self.root.after(2500, self._poll_apps)
        except tk.TclError:
            return

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
            "kind": str(task.get("kind") or "prompt"),
            "name": str(task.get("name") or ""),
            "content": str(task.get("content") or ""),
            "conversation": str(task.get("conversation_title") or ""),
            "schedule": str(task.get("schedule") or "once"),
            "date": planned.strftime("%Y-%m-%d"),
            "time": planned.strftime("%H:%M"),
            "interval": str(int(task.get("interval_minutes", 60) or 60)),
            "action": str(task.get("action") or "click"),
            "enabled": bool(task.get("enabled", True)),
            "retry": str(int(task.get("retry_limit", 0) or 0)),
            "retry_delay": str(int(task.get("retry_delay", 30) or 30)),
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
                text=_clip(self._run_summary(task), 52),
                fg=self.theme.c("text_3"),
            )
        self._set_status(f"已载入：{self.draft['name'] or '未命名任务'}")

    @staticmethod
    def _run_summary(task: dict[str, Any]) -> str:
        """编辑区底部那一行：上次什么时候跑的、跑了多久、一共几次。"""

        stamp = parse_datetime(task.get("last_run"))
        if stamp is None:
            return "还没有执行过"
        duration = int(task.get("last_duration_ms") or 0) / 1000.0
        return f"上次 {config.format_short_datetime(stamp)} · {duration:.1f}s · 共 {int(task.get('run_count') or 0)} 次"

    def _sync_editor_from_draft(self) -> None:
        self.name_var.set(self.draft["name"])
        self.kind_var.set(self.draft["kind"])
        self.conversation_var.set(self.draft["conversation"])
        self.schedule_var.set(self.draft["schedule"])
        self.date_var.set(self.draft["date"])
        self.time_var.set(self.draft["time"])
        self.interval_var.set(self.draft["interval"])
        self.action_var.set(self.draft["action"])
        self.retry_var.set(self.draft["retry"])
        self.retry_delay_var.set(self.draft["retry_delay"])
        self.enabled_var.set(bool(self.draft["enabled"]))
        if hasattr(self, "prompt_area"):
            self.prompt_area.set(self.draft["content"])
            self._on_schedule_change(self.draft["schedule"])
            self._on_kind_change(self.draft["kind"])

    def _sync_draft_from_editor(self) -> None:
        self.draft["name"] = self.name_var.get().strip()
        self.draft["kind"] = self.kind_var.get() or "prompt"
        self.draft["conversation"] = self.conversation_var.get().strip()
        self.draft["schedule"] = self.schedule_var.get()
        self.draft["date"] = self.date_var.get().strip()
        self.draft["time"] = self.time_var.get().strip()
        self.draft["interval"] = self.interval_var.get().strip()
        self.draft["action"] = self.action_var.get()
        self.draft["retry"] = self.retry_var.get().strip()
        self.draft["retry_delay"] = self.retry_delay_var.get().strip()
        self.draft["enabled"] = bool(self.enabled_var.get())
        if hasattr(self, "prompt_area"):
            self.draft["content"] = self.prompt_area.get()

    def _editor_signature(self) -> tuple:
        return (
            self.draft["kind"],
            self.draft["name"],
            self.draft["content"],
            self.draft["conversation"],
            self.draft["schedule"],
            self.draft["date"],
            self.draft["time"],
            self.draft["interval"],
            self.draft["action"],
            self.draft["retry"],
            self.draft["retry_delay"],
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

    def _retry_limit(self) -> int:
        try:
            value = int(float(self.draft["retry"] or "0"))
        except (TypeError, ValueError):
            return 0
        return max(0, min(5, value))

    def _retry_delay(self) -> int:
        try:
            value = int(float(self.draft["retry_delay"] or "30"))
        except (TypeError, ValueError):
            return 30
        return max(5, min(600, value))

    def _build_task_payload(self) -> Optional[dict[str, Any]]:
        self._sync_draft_from_editor()
        kind = self.draft["kind"]
        if kind == "prompt" and not self.draft["content"].strip():
            messagebox.showwarning("无法保存", "提示词内容不能为空。", parent=self.root)
            return None
        name = self.draft["name"] or KIND_LABELS.get(kind, "未命名任务")

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
            "kind": kind,
            "name": name[:80],
            "content": self.draft["content"],
            "conversation_title": self.draft["conversation"][:200],
            "schedule": schedule,
            "next_run": iso_datetime(next_run),
            "interval_minutes": interval,
            "action": self.draft["action"],
            "retry_limit": self._retry_limit(),
            "retry_delay": self._retry_delay(),
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

    def _save_auto_launch(self) -> None:
        self.store.update_settings(auto_launch_target=bool(self.auto_launch_var.get()))
        self._set_status("执行前自动拉起：" + ("开" if self.auto_launch_var.get() else "关"))

    def _save_v2ray_path(self) -> None:
        path = apps_module.tidy_path(self.v2ray_path_var.get())
        self.v2ray_path_var.set(path)
        self.store.update_settings(v2ray_path=path)
        self._refresh_apps()

    def _save_autostart(self) -> None:
        enabled = bool(self.autostart_var.get())
        ok, reason = apps_module.set_autostart(enabled)
        if not ok:
            self.autostart_var.set(apps_module.autostart_enabled())
            messagebox.showwarning(
                "开机自启", f"设置失败：{reason}", parent=self.root
            )
            return
        self._set_status("已设为开机自动启动" if enabled else "已取消开机自动启动")

    def _save_launch_v2ray(self) -> None:
        self.store.update_settings(launch_v2ray_first=bool(self.launch_v2ray_var.get()))

    def _open_v2ray_folder(self) -> None:
        path = apps_module.tidy_path(self.v2ray_path_var.get())
        if not path:
            path = apps_module.detect_v2ray_path()
        if not path:
            self._set_status("还没有绑定 v2rayN，先点「自动检测」")
            return
        folder = os.path.dirname(path)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception as exc:
            self._set_status(f"无法打开：{friendly_error(exc)}")

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

    # ------------------------------------------------------------------
    # 快捷键与命令面板
    # ------------------------------------------------------------------

    def _bind_shortcuts(self) -> None:
        """全局快捷键。全部绑在 root 上，切到哪个视图都能用。"""

        # Ctrl+S / Ctrl+N / Ctrl+Enter 按当前视图分派：在片段页里按 Ctrl+S
        # 存的应该是片段，而不是背后那个看不见的任务编辑器。
        bindings = (
            ("<Control-k>", self.open_palette),
            ("<Control-K>", self.open_palette),
            ("<Control-n>", self._shortcut_new),
            ("<Control-s>", self._shortcut_save),
            ("<Control-Return>", self._shortcut_run),
        )
        for sequence, handler in bindings:
            try:
                self.root.bind_all(sequence, lambda _event, h=handler: h())
            except tk.TclError:
                pass

    def _shortcut_new(self) -> None:
        if self.view == "snippets":
            self.new_snippet()
        elif self.view == "tasks":
            self.new_task()

    def _shortcut_save(self) -> None:
        if self.view == "snippets":
            self.save_snippet()
        elif self.view == "tasks":
            self.save_task()

    def _shortcut_run(self) -> None:
        if self.view == "snippets":
            self.send_snippet()
        elif self.view == "tasks":
            self.save_and_run()

    def open_palette(self) -> None:
        if self._palette is not None:
            # 再按一次 Ctrl+K 就是收起，符合"同一个键开同一个面板"的直觉。
            try:
                self._palette.close()
            except Exception:
                pass
            self._palette = None
            return
        try:
            self._palette = CommandPalette(
                self.root,
                self.theme,
                self._palette_commands(),
                on_close=self._on_palette_closed,
            )
        except tk.TclError:
            self._palette = None

    def _on_palette_closed(self) -> None:
        self._palette = None

    def _palette_commands(self) -> list[Command]:
        """面板里的每一条命令。顺序就是默认顺序：先视图，再动作，最后数据。"""

        items: list[Command] = []

        def add(
            label: str,
            run: Callable[[], Any],
            group: str = "",
            hint: str = "",
            keywords: str = "",
        ) -> None:
            items.append(Command(label=label, group=group, hint=hint, keywords=keywords, run=run))

        for key, text in (
            ("tasks", "任务"),
            ("snippets", "片段"),
            ("chats", "对话"),
            ("apps", "应用"),
            ("log", "日志"),
            ("settings", "设置"),
        ):
            add(f"打开：{text}", lambda k=key: self.show_view(k), "视图", "视图", text)

        add("新建任务", self.new_task, "任务", "Ctrl+N", "new task 新建")
        add("新建片段", lambda: (self.show_view("snippets"), self.new_snippet()), "片段", "", "new snippet")
        add("扫描对话", lambda: (self.show_view("chats"), self.scan_conversations()), "对话", "", "scan 刷新")
        add("新建对话", lambda: (self.show_view("chats"), self.new_conversation()), "对话", "", "new chat")
        add("停止生成", self.stop_generation, "动作", "", "stop 暂停")
        add("清空输入框", self.clear_input, "动作", "", "clear 清空")
        add("启动 Codex", self._app_launch_codex, "应用", "", "start launch 打开")
        add("关闭 Codex", self._app_close_codex, "应用", "", "quit close 退出")
        add("启动 v2rayN", self._app_launch_v2ray, "应用", "", "proxy 代理")
        add("一键启动 v2rayN → Codex", self._app_start_chain, "应用", "", "chain 启动")
        add("打开数据目录", self._open_data_dir, "设置", "", "data folder")

        for task in self.store.tasks():
            name = str(task.get("name") or "未命名任务")
            task_id = str(task.get("id"))
            add(
                f"执行：{name}",
                lambda i=task_id: self.run_task(i),
                "任务",
                kind_label(task),
                f"{name} run",
            )
            add(
                f"编辑：{name}",
                lambda i=task_id: self._open_task_from_palette(i),
                "任务",
                "打开",
                f"{name} open edit",
            )

        for snippet in self.store.snippets():
            name = str(snippet.get("name") or "片段")
            snippet_id = str(snippet.get("id"))
            add(
                f"发送片段：{name}",
                lambda i=snippet_id: self._send_snippet_by_id(i),
                "片段",
                "立即发送",
                f"{name} send",
            )
            add(
                f"编辑片段：{name}",
                lambda i=snippet_id: (self.show_view("snippets"), self.select_snippet(i)),
                "片段",
                "打开",
                f"{name} edit",
            )

        for item in self._conversations:
            title = item.title
            add(
                f"切换对话：{title}",
                lambda t=title: self.open_conversation(t),
                "对话",
                "切换",
                title,
            )

        return items

    def _open_task_from_palette(self, task_id: str) -> None:
        self.show_view("tasks")
        self.select_task(task_id)

    def _send_snippet_by_id(self, snippet_id: str) -> None:
        item = self.store.get_snippet(snippet_id)
        if item is None:
            return
        content = str(item.get("content") or "")
        name = str(item.get("name") or "片段")
        self._run_toolbox(f"发送「{name}」", lambda toolbox: toolbox.send(content, name=name))
        self.store.touch_snippet(snippet_id)
        self.refresh_snippets(force=True)

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
                elif event == "apps":
                    try:
                        self._refresh_apps()
                    except tk.TclError:
                        pass
                elif event == "chats":
                    try:
                        self._apply_conversations(payload)
                    except tk.TclError:
                        pass
                elif event == "action_done":
                    if self._conversation_scanning:
                        self._conversation_scanning = False
                        self.chat_status_var.set("扫描没有完成")
                        try:
                            self._render_chats()
                        except tk.TclError:
                            pass
                elif event == "history":
                    try:
                        if self.view == "log":
                            self._render_log()
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
        if self._apps_poll_id:
            try:
                self.root.after_cancel(self._apps_poll_id)
            except Exception:
                pass
            self._apps_poll_id = None
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
