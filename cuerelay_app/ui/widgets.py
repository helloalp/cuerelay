# -*- coding: utf-8 -*-
"""自绘控件。

Tk 原生按钮/单选框在 Windows 上观感陈旧，这里用 Frame + Label 自绘，
只保留扁平色块、1px 边框和悬停反馈，没有动画。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Sequence

from .theme import Theme

# 所有存活的滚动区域。滚轮事件由 dispatch_wheel 统一分发。
_AREAS: list["ScrollArea"] = []


def dispatch_wheel(event):
    """按指针位置把滚轮事件交给对应区域；文本控件自己处理滚轮。"""

    widget = getattr(event, "widget", None)
    if isinstance(widget, (tk.Text, tk.Entry, tk.Spinbox, ttk.Combobox, ttk.Treeview)):
        return None
    try:
        x_root = int(event.x_root)
        y_root = int(event.y_root)
        delta = int(event.delta)
    except (AttributeError, TypeError, ValueError):
        return None
    # 倒着找：后创建的区域盖在先创建的上面（命令面板就是后创建的）。
    # 正着找会把滚轮交给被面板挡住的那个列表。
    for area in reversed(list(_AREAS)):
        try:
            if area.contains(x_root, y_root):
                area.scroll_by(delta)
                return "break"
        except tk.TclError:
            continue
    return None


def blend(color_a: str, color_b: str, ratio: float) -> str:
    """在两个 #RRGGBB 之间线性插值，用于生成悬停色。"""

    ratio = max(0.0, min(1.0, ratio))

    def parse(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        if len(value) != 6:
            return (0, 0, 0)
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))

    first = parse(color_a)
    second = parse(color_b)
    mixed = tuple(round(first[i] + (second[i] - first[i]) * ratio) for i in range(3))
    return "#%02x%02x%02x" % mixed


# ---------------------------------------------------------------------------
# 按钮
# ---------------------------------------------------------------------------

_BUTTON_KINDS = {
    "primary": ("accent", "accent_text", None, "accent"),
    "ghost": ("surface", "text", "surface_3", "border"),
    "subtle": ("bg", "text_2", "surface_3", None),
    "danger": ("surface", "danger", "danger_soft", "border"),
}


class Button(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        text: str,
        command: Optional[Callable[[], None]] = None,
        kind: str = "ghost",
        size: str = "md",
        font_key: str = "body",
    ) -> None:
        self.theme = theme
        self._kind = kind if kind in _BUTTON_KINDS else "ghost"
        self._command = command
        self._enabled = True
        fill_key, text_key, hover_key, border_key = _BUTTON_KINDS[self._kind]

        fill = theme.c(fill_key)
        super().__init__(
            parent,
            bg=fill,
            bd=0,
            highlightthickness=1 if border_key else 0,
            highlightbackground=theme.c(border_key) if border_key else fill,
            highlightcolor=theme.c(border_key) if border_key else fill,
            cursor="hand2",
        )

        self._fill = fill
        self._foreground = theme.c(text_key)
        if hover_key:
            self._hover = theme.c(hover_key)
        elif self._kind == "primary":
            self._hover = blend(fill, "#000000", 0.14)
        else:
            self._hover = blend(fill, theme.c("text"), 0.06)

        pad_x = theme.px(15) if size == "md" else theme.px(10)
        pad_y = theme.px(8) if size == "md" else theme.px(4)
        self.label = tk.Label(
            self,
            text=text,
            bg=fill,
            fg=self._foreground,
            font=theme.font(font_key),
            padx=pad_x,
            pady=pad_y,
            cursor="hand2",
            anchor="center",
        )
        self.label.pack(fill="both", expand=True)
        for widget in (self, self.label):
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
            widget.bind("<Button-1>", self._on_press)
            widget.bind("<ButtonRelease-1>", self._on_release)

    # -- 外观 ----------------------------------------------------------

    def _paint(self, fill: str, foreground: Optional[str] = None) -> None:
        self.configure(bg=fill)
        self.label.configure(bg=fill, fg=foreground or self.label.cget("fg"))

    def _on_enter(self, _event=None) -> None:
        if self._enabled:
            self._paint(self._hover)

    def _on_leave(self, _event=None) -> None:
        if self._enabled:
            self._paint(self._fill, self._foreground)

    def _on_press(self, _event=None) -> None:
        if self._enabled:
            self._paint(blend(self._hover, self.theme.c("text"), 0.10))

    def _on_release(self, event=None) -> None:
        if not self._enabled:
            return
        self._paint(self._hover)
        inside = True
        if event is not None:
            try:
                inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
            except tk.TclError:
                inside = True
        if inside and self._command is not None:
            self._command()

    # -- 状态 ----------------------------------------------------------

    def set_text(self, text: str) -> None:
        self.label.configure(text=text)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if self._enabled:
            self._paint(self._fill, self._foreground)
            self.configure(cursor="hand2")
            self.label.configure(cursor="hand2")
        else:
            disabled = self.theme.c("text_3")
            self._paint(self.theme.c("surface_3"), disabled)
            self.configure(cursor="arrow")
            self.label.configure(cursor="arrow")

    def configure(self, **kwargs):  # noqa: D401 - 与 Tk 接口保持一致
        text = kwargs.pop("text", None)
        state = kwargs.pop("state", None)
        if kwargs:
            super().configure(**kwargs)
        if text is not None and hasattr(self, "label"):
            self.label.configure(text=text)
        if state is not None:
            self.set_enabled(str(state) != "disabled")

    config = configure


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


class Switch(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        text: str,
        variable: tk.BooleanVar,
        command: Optional[Callable[[], None]] = None,
        bg_key: str = "surface",
    ) -> None:
        background = theme.c(bg_key)
        super().__init__(parent, bg=background)
        self.theme = theme
        self.variable = variable
        self._command = command
        self._background = background

        self._width = theme.px(38)
        self._height = theme.px(21)
        self.canvas = tk.Canvas(
            self,
            width=self._width,
            height=self._height,
            bg=background,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.canvas.pack(side="left")
        self.label = tk.Label(
            self,
            text=text,
            bg=background,
            fg=theme.c("text"),
            font=theme.font("body"),
            cursor="hand2",
        )
        self.label.pack(side="left", padx=(theme.px(9), 0))
        for widget in (self.canvas, self.label):
            widget.bind("<Button-1>", self._toggle)
        self._trace = self.variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Destroy>", self._on_destroy)
        self._draw()

    def _on_destroy(self, event) -> None:
        if event.widget is not self:
            return
        try:
            self.variable.trace_remove("write", self._trace)
        except Exception:
            pass

    def _draw(self) -> None:
        try:
            self.canvas.delete("all")
        except tk.TclError:
            return
        width, height = self._width, self._height
        radius = height // 2
        active = bool(self.variable.get())
        track = self.theme.c("accent") if active else self.theme.c("border_strong")
        self.canvas.create_oval(0, 0, height, height, fill=track, outline="")
        self.canvas.create_oval(width - height, 0, width, height, fill=track, outline="")
        self.canvas.create_rectangle(radius, 0, width - radius, height, fill=track, outline="")
        knob = height - self.theme.px(6)
        offset = self.theme.px(3)
        left = width - knob - offset if active else offset
        self.canvas.create_oval(left, offset, left + knob, offset + knob, fill="#FFFFFF", outline="")

    def _toggle(self, _event=None) -> None:
        self.variable.set(not self.variable.get())
        if self._command is not None:
            self._command()

    def set_text(self, text: str) -> None:
        self.label.configure(text=text)


# ---------------------------------------------------------------------------
# 分段选择
# ---------------------------------------------------------------------------


class Segmented(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        options: Sequence[tuple[str, str]],
        variable: tk.StringVar,
        command: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(
            parent,
            bg=theme.c("surface_3"),
            highlightthickness=1,
            highlightbackground=theme.c("border"),
            highlightcolor=theme.c("border"),
        )
        self.theme = theme
        self.variable = variable
        self._command = command
        # 注意：不能叫 _options —— 那是 tkinter.Misc 的内部方法，
        # 覆盖后 pack/grid/place 全部会抛 TypeError。
        self._segments = list(options)
        self._labels: list[tk.Label] = []

        for value, text in self._segments:
            label = tk.Label(
                self,
                text=text,
                bg=theme.c("surface_3"),
                fg=theme.c("text_3"),
                font=theme.font("body"),
                padx=theme.px(14),
                pady=theme.px(6),
                cursor="hand2",
            )
            label.pack(side="left", fill="both", expand=True)
            label.bind("<Button-1>", lambda _event, v=value: self.select(v))
            self._labels.append(label)

        self._trace = self.variable.trace_add("write", lambda *_: self._restyle())
        self.bind("<Destroy>", self._on_destroy)
        self._restyle()

    def _on_destroy(self, event) -> None:
        if event.widget is not self:
            return
        try:
            self.variable.trace_remove("write", self._trace)
        except Exception:
            pass

    def _restyle(self) -> None:
        current = self.variable.get()
        for (value, _text), label in zip(self._segments, self._labels):
            if value == current:
                label.configure(bg=self.theme.c("surface"), fg=self.theme.c("text"), font=self.theme.font("strong"))
            else:
                label.configure(bg=self.theme.c("surface_3"), fg=self.theme.c("text_3"), font=self.theme.font("body"))

    def select(self, value: str) -> None:
        if self.variable.get() == value:
            return
        self.variable.set(value)
        if self._command is not None:
            self._command(value)


# ---------------------------------------------------------------------------
# 滚动区域
# ---------------------------------------------------------------------------


class ScrollArea(tk.Frame):
    """带滚动的容器。

    滚轮统一由一个全局分发器处理：按指针位置决定滚动哪个区域，
    避免 Tk 的 Enter/Leave 在子控件之间来回触发导致滚轮失效。
    """

    def __init__(self, parent: tk.Misc, theme: Theme, bg_key: str = "bg") -> None:
        background = theme.c(bg_key)
        super().__init__(parent, bg=background)
        self.canvas = tk.Canvas(self, bg=background, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            style="Cue.Vertical.TScrollbar",
        )
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.body = tk.Frame(self.canvas, bg=background)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        _AREAS.append(self)
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, event) -> None:
        if event.widget is not self:
            return
        try:
            _AREAS.remove(self)
        except ValueError:
            pass

    def _on_body_configure(self, _event=None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            pass

    def _on_canvas_configure(self, event) -> None:
        try:
            self.canvas.itemconfigure(self._window, width=max(1, int(event.width)))
        except tk.TclError:
            pass

    def contains(self, x_root: int, y_root: int) -> bool:
        left = self.winfo_rootx()
        top = self.winfo_rooty()
        return left <= x_root < left + self.winfo_width() and top <= y_root < top + self.winfo_height()

    def scroll_by(self, delta: int) -> None:
        steps = int(delta / 120)
        if steps == 0:
            steps = 1 if delta > 0 else -1
        try:
            self.canvas.yview_scroll(-steps, "units")
        except tk.TclError:
            pass

    def scroll_to_top(self) -> None:
        try:
            self.canvas.yview_moveto(0.0)
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 表单控件
# ---------------------------------------------------------------------------


class Field(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        label: str,
        variable: tk.StringVar,
        hint: str = "",
        bg_key: str = "surface",
        show: Optional[str] = None,
        placeholder: str = "",
    ) -> None:
        super().__init__(parent, bg=theme.c(bg_key))
        self.theme = theme
        self.variable = variable
        self._focused = False
        if label:
            tk.Label(
                self,
                text=label,
                bg=theme.c(bg_key),
                fg=theme.c("text_2"),
                font=theme.font("small"),
                anchor="w",
            ).pack(fill="x", pady=(0, theme.px(6)))

        self.box = tk.Frame(
            self,
            bg=theme.c("surface"),
            padx=theme.px(10),
            pady=theme.px(7),
            highlightthickness=1,
            highlightbackground=theme.c("border"),
            highlightcolor=theme.c("accent"),
        )
        self.box.pack(fill="x")
        self.entry = tk.Entry(
            self.box,
            textvariable=variable,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=theme.c("surface"),
            fg=theme.c("text"),
            insertbackground=theme.c("text"),
            selectbackground=theme.c("accent_soft"),
            selectforeground=theme.c("text"),
            font=theme.font("body"),
            show=show or "",
        )
        self.entry.pack(fill="x")
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)

        if placeholder:
            self.placeholder_label = tk.Label(
                self.box,
                text=placeholder,
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("body"),
                anchor="w",
                cursor="xterm",
            )
            self.placeholder_label.bind("<Button-1>", lambda _event: self.entry.focus_set())
            self.variable.trace_add("write", lambda *_: self._sync_placeholder())
            self._sync_placeholder()

        if hint:
            tk.Label(
                self,
                text=hint,
                bg=theme.c(bg_key),
                fg=theme.c("text_3"),
                font=theme.font("tiny"),
                anchor="w",
            ).pack(fill="x", pady=(theme.px(5), 0))

    def _on_focus_in(self, _event=None) -> None:
        self._focused = True
        self.box.configure(highlightbackground=self.theme.c("accent"))
        self._sync_placeholder()

    def _on_focus_out(self, _event=None) -> None:
        self._focused = False
        self.box.configure(highlightbackground=self.theme.c("border"))
        self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        if not hasattr(self, "placeholder_label"):
            return
        try:
            if self._focused or self.variable.get():
                self.placeholder_label.place_forget()
            else:
                self.placeholder_label.place(x=0, rely=0.5, anchor="w")
        except tk.TclError:
            pass


class TextArea(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        label: str,
        height: int = 10,
        bg_key: str = "surface",
        hint: str = "",
    ) -> None:
        super().__init__(parent, bg=theme.c(bg_key))
        self.theme = theme
        if label:
            tk.Label(
                self,
                text=label,
                bg=theme.c(bg_key),
                fg=theme.c("text_2"),
                font=theme.font("small"),
                anchor="w",
            ).pack(fill="x", pady=(0, theme.px(6)))

        self.box = tk.Frame(
            self,
            bg=theme.c("surface"),
            padx=theme.px(10),
            pady=theme.px(8),
            highlightthickness=1,
            highlightbackground=theme.c("border"),
            highlightcolor=theme.c("accent"),
        )
        self.box.pack(fill="both", expand=True)
        self.text = tk.Text(
            self.box,
            height=height,
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=theme.c("surface"),
            fg=theme.c("text"),
            insertbackground=theme.c("text"),
            selectbackground=theme.c("accent_soft"),
            selectforeground=theme.c("text"),
            font=theme.font("body"),
            undo=True,
            spacing1=theme.px(2),
            spacing3=theme.px(2),
            padx=0,
            pady=0,
        )
        self.text.pack(fill="both", expand=True)
        self.text.bind("<FocusIn>", lambda _e: self.box.configure(highlightbackground=theme.c("accent")))
        self.text.bind("<FocusOut>", lambda _e: self.box.configure(highlightbackground=theme.c("border")))

        if hint:
            tk.Label(
                self,
                text=hint,
                bg=theme.c(bg_key),
                fg=theme.c("text_3"),
                font=theme.font("tiny"),
                anchor="w",
            ).pack(fill="x", pady=(theme.px(5), 0))

    def get(self) -> str:
        return self.text.get("1.0", "end-1c")

    def set(self, value: str) -> None:
        self.text.delete("1.0", "end")
        if value:
            self.text.insert("1.0", value)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def card(parent: tk.Misc, theme: Theme, bg_key: str = "surface") -> tuple[tk.Frame, tk.Frame]:
    outer = tk.Frame(parent, bg=theme.c("border"), highlightthickness=0, bd=0)
    inner = tk.Frame(outer, bg=theme.c(bg_key))
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    return outer, inner


def divider(parent: tk.Misc, theme: Theme, bg_key: str = "surface") -> tk.Frame:
    line = tk.Frame(parent, bg=theme.c("border"), height=1)
    line.configure(bg=theme.c("border"))
    return line


def dot(parent: tk.Misc, theme: Theme, color: str, size: Optional[int] = None) -> tk.Canvas:
    diameter = size or theme.px(8)
    canvas = tk.Canvas(
        parent,
        width=diameter,
        height=diameter,
        bg=parent.cget("bg") if hasattr(parent, "cget") else theme.c("surface"),
        highlightthickness=0,
        bd=0,
    )
    canvas.create_oval(0, 0, diameter - 1, diameter - 1, fill=color, outline="")
    return canvas


def label(
    parent: tk.Misc,
    theme: Theme,
    text: str,
    font_key: str = "body",
    fg: str = "text",
    bg: str = "surface",
    anchor: str = "w",
) -> tk.Label:
    return tk.Label(
        parent,
        text=text,
        bg=theme.c(bg),
        fg=theme.c(fg),
        font=theme.font(font_key),
        anchor=anchor,
        justify="left",
    )
