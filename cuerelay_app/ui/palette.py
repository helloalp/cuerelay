# -*- coding: utf-8 -*-
"""命令面板。

一个输入框加一列结果：打字过滤，上下键选择，回车执行。所有能点的东西
都能在这里打出来——切视图、跑任务、发片段、跳对话、开关 Codex。

这里没有动画也没有图标：面板出现得越快越好，剩下的交给键盘。
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from typing import Callable, Optional

from .theme import Theme
from .widgets import ScrollArea


@dataclass
class Command:
    """一条可以执行的命令。

    keywords 用来放别名：中文界面下用户可能打拼音、英文或简称，
    都塞进这里，不必在标题里堆一串同义词。
    """

    label: str
    group: str = ""
    hint: str = ""
    keywords: str = ""
    run: Callable[[], None] = field(default=lambda: None, repr=False, compare=False)

    def haystack(self) -> str:
        return f"{self.label} {self.group} {self.keywords}".lower()


def filter_commands(commands: list[Command], query: str) -> list[Command]:
    """按输入过滤。空输入原样返回，保证打开面板就能看到全部。"""

    text = str(query or "").strip().lower()
    if not text:
        return list(commands)
    terms = [term for term in text.split() if term]
    matched = [item for item in commands if all(term in item.haystack() for term in terms)]
    # 前缀命中的排前面：打 "任务" 时「任务」视图应该比「新建任务」更靠前。
    matched.sort(key=lambda item: (0 if item.label.lower().startswith(text) else 1,))
    return matched


class CommandPalette(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        theme: Theme,
        commands: list[Command],
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(master, bg=theme.c("surface"))
        self.theme = theme
        self.commands = list(commands)
        self._on_close = on_close
        self._results: list[Command] = []
        self._rows: list[tk.Frame] = []
        self._index = 0

        self.overrideredirect(True)
        self.transient(master)

        self.query_var = tk.StringVar()
        self._build()
        self._place_over(master)
        self._apply("")

        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Return>", lambda _event: self._run_current())
        self.bind("<Down>", lambda _event: self._move(1))
        self.bind("<Up>", lambda _event: self._move(-1))
        self.bind("<FocusOut>", self._on_focus_out)

        self.entry.focus_force()
        self.grab_set()

    # -- 构建 ----------------------------------------------------------

    def _build(self) -> None:
        theme = self.theme
        pad = theme.px(14)

        self.entry = tk.Entry(
            self,
            textvariable=self.query_var,
            bg=theme.c("surface"),
            fg=theme.c("text"),
            insertbackground=theme.c("accent"),
            font=theme.font("title"),
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        self.entry.pack(fill="x", padx=pad, pady=(pad, theme.px(10)))
        self.entry.bind("<KeyRelease>", lambda _event: self._apply(self.query_var.get()))

        tk.Frame(self, bg=theme.c("border"), height=1).pack(fill="x", padx=pad)

        self.list_area = ScrollArea(self, theme, "surface")
        self.list_area.pack(fill="both", expand=True, padx=pad, pady=(theme.px(6), pad))

    def _place_over(self, master: tk.Misc) -> None:
        theme = self.theme
        width = theme.px(560)
        height = theme.px(400)
        try:
            root_x = master.winfo_rootx()
            root_y = master.winfo_rooty()
            root_w = master.winfo_width()
        except tk.TclError:
            root_x, root_y, root_w = 0, 0, width
        x = max(0, root_x + (root_w - width) // 2)
        y = root_y + theme.px(96)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.configure(highlightthickness=1, highlightbackground=theme.c("border_strong"))

    # -- 列表 ----------------------------------------------------------

    def _apply(self, query: str) -> None:
        self._results = filter_commands(self.commands, query)
        self._index = 0
        self._render()

    def _render(self) -> None:
        theme = self.theme
        body = self.list_area.body
        for child in body.winfo_children():
            child.destroy()
        self._rows = []

        if not self._results:
            tk.Label(
                body,
                text="没有匹配的命令",
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                anchor="w",
            ).pack(fill="x", padx=theme.px(2), pady=theme.px(8))
            return

        for position, command in enumerate(self._results):
            row = tk.Frame(body, bg=theme.c("surface"), cursor="hand2")
            row.pack(fill="x", pady=(0, theme.px(2)))

            text = tk.Label(
                row,
                text=command.label,
                bg=theme.c("surface"),
                fg=theme.c("text"),
                font=theme.font("body"),
                anchor="w",
                padx=theme.px(10),
                pady=theme.px(7),
            )
            text.pack(side="left", fill="x", expand=True)
            hint = tk.Label(
                row,
                text=command.hint or command.group,
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("tiny"),
                anchor="e",
                padx=theme.px(10),
            )
            hint.pack(side="right")

            for widget in (row, text, hint):
                widget.bind("<Button-1>", lambda _event, i=position: self._run_index(i))
                widget.bind("<Enter>", lambda _event, i=position: self._set_index(i))
            self._rows.append(row)

        self._paint()

    def _paint(self) -> None:
        theme = self.theme
        for position, row in enumerate(self._rows):
            active = position == self._index
            fill = theme.c("accent_soft") if active else theme.c("surface")
            for widget in row.winfo_children():
                widget.configure(bg=fill)
            row.configure(bg=fill)

    def _set_index(self, index: int) -> None:
        if not self._rows:
            return
        self._index = max(0, min(len(self._rows) - 1, index))
        self._paint()

    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        self._set_index((self._index + delta) % len(self._rows))

    # -- 执行 ----------------------------------------------------------

    def _run_current(self) -> None:
        self._run_index(self._index)

    def _run_index(self, index: int) -> None:
        if not self._results:
            return
        command = self._results[max(0, min(len(self._results) - 1, index))]
        self.close()
        try:
            command.run()
        except Exception:
            # 命令自己负责报错；面板不该因为某条命令失败而留在屏幕上。
            pass

    def _on_focus_out(self, _event=None) -> None:
        # 面板失焦就关掉：它是个临时浮层，留着会挡住下面的界面。
        try:
            self.after(60, self._close_if_unfocused)
        except tk.TclError:
            pass

    def _close_if_unfocused(self) -> None:
        try:
            focused = self.focus_get()
        except (tk.TclError, KeyError):
            self.close()
            return
        if focused is None or not str(focused).startswith(str(self)):
            self.close()

    def close(self) -> None:
        if self._on_close is not None:
            callback, self._on_close = self._on_close, None
            try:
                callback()
            except Exception:
                pass
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass
