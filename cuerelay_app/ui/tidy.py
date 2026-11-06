# -*- coding: utf-8 -*-
"""整理任务。

任务攒多了以后，列表里会出现大量同名、同内容、早已停用的记录。
这个对话框只做两件事：合并内容重复的任务、清掉已经停止的任务。
两项都先显示"要删几条"，再由用户确认。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Any, Callable

from ..store import duplicate_groups, stopped_tasks
from .theme import Theme
from .widgets import Button, ScrollArea, Switch, card


class TidyDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        theme: Theme,
        tasks: list[dict[str, Any]],
        on_apply: Callable[[list[str]], None],
    ) -> None:
        super().__init__(master, bg=theme.c("bg"))
        self.theme = theme
        self.tasks = list(tasks)
        self._on_apply = on_apply

        self.groups = duplicate_groups(self.tasks)
        self.stopped = stopped_tasks(self.tasks)

        self.dup_var = tk.BooleanVar(value=bool(self.groups))
        self.stopped_var = tk.BooleanVar(value=False)

        self.title("整理任务")
        self.configure(bg=theme.c("bg"))
        self.transient(master)
        self.resizable(False, False)

        self._build()
        self._refresh()

    # -- 构建 ----------------------------------------------------------

    def _build(self) -> None:
        theme = self.theme
        pad = theme.px(22)

        header = tk.Frame(self, bg=theme.c("bg"))
        header.pack(fill="x", padx=pad, pady=(pad, theme.px(4)))
        tk.Label(
            header,
            text="整理任务",
            bg=theme.c("bg"),
            fg=theme.c("text"),
            font=theme.font("title"),
            anchor="w",
        ).pack(fill="x")
        summary = (
            f"共 {len(self.tasks)} 个任务 · "
            f"{len(self.groups)} 组内容重复 · "
            f"{len(self.stopped)} 个已停止"
        )
        tk.Label(
            header,
            text=summary,
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
            anchor="w",
        ).pack(fill="x", pady=(theme.px(4), 0))

        outer, inner = card(self, theme, "surface")
        outer.pack(fill="x", padx=pad, pady=(theme.px(14), 0))
        body = tk.Frame(inner, bg=theme.c("surface"))
        body.pack(fill="x", padx=theme.px(18), pady=theme.px(16))

        # 重复内容
        Switch(
            body,
            theme,
            "合并内容重复的任务",
            self.dup_var,
            command=self._refresh,
            bg_key="surface",
        ).pack(anchor="w")
        tk.Label(
            body,
            text="每组只保留最近修改的一条。",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        ).pack(fill="x", pady=(theme.px(4), theme.px(8)))

        if self.groups:
            row_height = theme.px(30)
            height = min(theme.px(150), row_height * len(self.groups) + theme.px(4))
            holder = tk.Frame(body, bg=theme.c("surface"), height=height)
            holder.pack(fill="x")
            holder.pack_propagate(False)
            scroll = ScrollArea(holder, theme, "surface")
            scroll.pack(fill="both", expand=True)
            for group in self.groups:
                self._render_group(scroll.body, group)
        else:
            tk.Label(
                body,
                text="没有内容重复的任务。",
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                anchor="w",
            ).pack(fill="x")

        tk.Frame(body, bg=theme.c("border"), height=1).pack(fill="x", pady=theme.px(14))

        # 已停止
        Switch(
            body,
            theme,
            "删除已停止的任务",
            self.stopped_var,
            command=self._refresh,
            bg_key="surface",
        ).pack(anchor="w")
        tk.Label(
            body,
            text=f"已暂停、已完成、已过期的任务共 {len(self.stopped)} 个，删除后无法恢复。",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        ).pack(fill="x", pady=(theme.px(4), 0))

        self.preview = tk.Label(
            self,
            text="",
            bg=theme.c("bg"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        )
        self.preview.pack(fill="x", padx=pad, pady=(theme.px(14), 0))

        footer = tk.Frame(self, bg=theme.c("bg"))
        footer.pack(fill="x", padx=pad, pady=(theme.px(10), pad))
        Button(footer, theme, "取消", self.destroy, kind="subtle").pack(side="right")
        self.apply_button = Button(footer, theme, "执行清理", self._apply, kind="primary")
        self.apply_button.pack(side="right", padx=(0, theme.px(8)))

    def _render_group(self, parent: tk.Frame, group: dict[str, Any]) -> None:
        theme = self.theme
        row = tk.Frame(parent, bg=theme.c("surface"))
        row.pack(fill="x", pady=(0, theme.px(6)))
        count = len(group["extra"])
        snippet = " ".join(str(group["content"]).split())[:52] or "（空内容）"
        tk.Label(
            row,
            text=f"{count + 1} 条 · {snippet}",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small"),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            row,
            text=f"删除 {count} 条",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="e",
        ).pack(side="right")

    # -- 状态 ----------------------------------------------------------

    def _targets(self) -> list[str]:
        ids: list[str] = []
        if self.dup_var.get():
            for group in self.groups:
                ids.extend(str(task.get("id")) for task in group["extra"])
        if self.stopped_var.get():
            ids.extend(str(task.get("id")) for task in self.stopped)
        # 去重但保持顺序
        seen: set[str] = set()
        unique: list[str] = []
        for task_id in ids:
            if task_id and task_id not in seen:
                seen.add(task_id)
                unique.append(task_id)
        return unique

    def _refresh(self) -> None:
        targets = self._targets()
        remaining = len(self.tasks) - len(targets)
        if targets:
            self.preview.configure(
                text=f"将删除 {len(targets)} 个任务，清理后保留 {remaining} 个。",
                fg=self.theme.c("text"),
            )
            self.apply_button.set_text(f"执行清理（{len(targets)}）")
            self.apply_button.set_enabled(True)
        else:
            self.preview.configure(text="没有选中任何整理项。", fg=self.theme.c("text_3"))
            self.apply_button.set_text("执行清理")
            self.apply_button.set_enabled(False)

    def _apply(self) -> None:
        targets = self._targets()
        if not targets:
            return
        remaining = len(self.tasks) - len(targets)
        if remaining <= 0:
            if not messagebox.askyesno(
                "清空任务",
                f"这样会删除全部 {len(self.tasks)} 个任务，且无法恢复。确定吗？",
                parent=self,
            ):
                return
        self._on_apply(targets)
        self.destroy()
