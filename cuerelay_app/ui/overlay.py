# -*- coding: utf-8 -*-
"""识别与执行过程的浮层。

在目标窗口上方盖一层透明、可穿透鼠标的浮层，用框线标出识别到的输入框、
发送按钮和搜索框，并在顶部显示当前进行到哪一步。

浮层用颜色键做透明：整块画布填一个几乎不会出现在界面里的颜色，
Tk 会把这个颜色整片抠掉；再补上 WS_EX_TRANSPARENT，鼠标点击会完全穿透，
不会影响自动化自己的点击。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Optional

from .. import winapi
from .theme import Theme

# 颜色键：纯黑或纯白都可能出现在界面上，用一个偏色值最安全。
KEY_COLOR = "#010203"

KIND_COLORS = {
    "composer": "accent",
    "send": "success",
    "search": "warning",
    "frame": "border_strong",
}

STEP_LABELS = {
    "locate": "定位输入框",
    "switch": "切换对话",
    "paste": "粘贴提示词",
    "verify_paste": "校验粘贴",
    "send": "发送",
    "verify_send": "校验发送",
}


class Overlay:
    """覆盖在目标窗口客户区上的可视化浮层。"""

    def __init__(self, root: tk.Misc, theme: Theme) -> None:
        self.root = root
        self.theme = theme
        self.window: Optional[tk.Toplevel] = None
        self.canvas: Optional[tk.Canvas] = None
        self.visible = False
        self._rect: Optional[dict[str, int]] = None
        self._boxes: list[tuple[dict[str, int], str, str]] = []
        self._progress: Optional[tuple[int, int, str, str]] = None

    # -- 生命周期 ------------------------------------------------------

    def _ensure(self) -> bool:
        if self.window is not None:
            try:
                if self.window.winfo_exists():
                    return True
            except tk.TclError:
                pass
            self.window = None
        try:
            window = tk.Toplevel(self.root)
            window.withdraw()
            window.overrideredirect(True)
            window.configure(bg=KEY_COLOR)
            window.attributes("-topmost", True)
            try:
                window.attributes("-transparentcolor", KEY_COLOR)
            except tk.TclError:
                pass
            canvas = tk.Canvas(window, bg=KEY_COLOR, highlightthickness=0, bd=0)
            canvas.pack(fill="both", expand=True)
        except tk.TclError:
            return False
        self.window = window
        self.canvas = canvas
        return True

    def show(self, rect: dict[str, int]) -> None:
        if not self._ensure() or self.window is None:
            return
        try:
            left = int(rect["left"])
            top = int(rect["top"])
            width = max(1, int(rect["width"]))
            height = max(1, int(rect["height"]))
        except (KeyError, TypeError, ValueError):
            return
        same_rect = self._rect == {
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }
        self._rect = {"left": left, "top": top, "width": width, "height": height}
        # 同一块客户区重复 show 时不要清空框线和进度，否则进度条会闪一下。
        if not same_rect:
            self._boxes = []
            self._progress = None
        try:
            self.window.geometry(f"{width}x{height}+{left}+{top}")
            self.window.deiconify()
            self.window.lift()
            self.window.update_idletasks()
            hwnd = winapi.root_window(int(self.window.winfo_id()))
            winapi.make_click_through(hwnd)
        except tk.TclError:
            return
        self.visible = True
        self._redraw()

    def hide(self) -> None:
        self.visible = False
        self._boxes = []
        self._progress = None
        if self.window is None:
            return
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self.hide()
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
        self.window = None
        self.canvas = None

    def retheme(self, theme: Theme) -> None:
        self.theme = theme
        if self.visible:
            self._redraw()

    # -- 内容 ----------------------------------------------------------

    def box(self, rect: dict[str, int], kind: str, label: str = "") -> None:
        if not self.visible:
            return
        self._boxes = [item for item in self._boxes if item[1] != kind]
        self._boxes.append((dict(rect), kind, label))
        self._redraw()

    def progress(self, index: int, total: int, text: str, tone: str = "accent") -> None:
        if not self.visible:
            return
        self._progress = (max(0, index), max(1, total), text, tone)
        self._redraw()

    def clear_boxes(self) -> None:
        if not self.visible:
            return
        self._boxes = []
        self._redraw()

    # -- 绘制 ----------------------------------------------------------

    def _redraw(self) -> None:
        canvas = self.canvas
        rect = self._rect
        if canvas is None or rect is None:
            return
        try:
            canvas.delete("all")
        except tk.TclError:
            return

        self._draw_frame(rect["width"], rect["height"])

        for box_rect, kind, label in self._boxes:
            self._draw_box(box_rect, kind, label)

        if self._progress is not None:
            self._draw_progress(*self._progress)

    def _draw_frame(self, width: int, height: int) -> None:
        """四角标记，而不是整圈边框。

        整圈边框会紧贴客户区上下边缘画一条线，识别算法会把它当成面板边缘，
        于是输入框被误判成"从顶部一直延伸到底部"。四角标记只占每条边最外侧
        一小段，落在识别采样带之外，既说明了"识别的是这一块"，又不会干扰识别。
        """

        canvas = self.canvas
        if canvas is None:
            return
        theme = self.theme
        color = theme.c("accent")
        arm = max(12, theme.px(26))
        weight = max(2, theme.px(2))
        right = width - 1
        bottom = height - 1
        try:
            canvas.create_line(0, 0, arm, 0, fill=color, width=weight)
            canvas.create_line(0, 0, 0, arm, fill=color, width=weight)
            canvas.create_line(right - arm, 0, right, 0, fill=color, width=weight)
            canvas.create_line(right, 0, right, arm, fill=color, width=weight)
            canvas.create_line(0, bottom, arm, bottom, fill=color, width=weight)
            canvas.create_line(0, bottom - arm, 0, bottom, fill=color, width=weight)
            canvas.create_line(right - arm, bottom, right, bottom, fill=color, width=weight)
            canvas.create_line(right, bottom - arm, right, bottom, fill=color, width=weight)
        except tk.TclError:
            return

    def _draw_box(self, rect: dict[str, int], kind: str, label: str) -> None:
        canvas = self.canvas
        origin = self._rect
        if canvas is None or origin is None:
            return
        # 框线画在目标矩形外侧，别压住被描述元素的真实边缘——
        # 否则识别算法会把框线当成元素自己的边缘。
        pad = max(2, self.theme.px(2))
        try:
            left = int(rect["left"]) - origin["left"] - pad
            top = int(rect["top"]) - origin["top"] - pad
            right = int(rect["right"]) - origin["left"] + pad
            bottom = int(rect["bottom"]) - origin["top"] + pad
        except (KeyError, TypeError, ValueError):
            return
        color = self.theme.c(KIND_COLORS.get(kind, "accent"))
        try:
            canvas.create_rectangle(left, top, right, bottom, outline=color, width=max(2, self.theme.px(2)))
        except tk.TclError:
            return
        if not label:
            return
        self._draw_chip(left, top, label, color)

    def _draw_chip(self, x: int, y: int, text: str, color: str) -> None:
        canvas = self.canvas
        if canvas is None:
            return
        pad_x = self.theme.px(7)
        pad_y = self.theme.px(3)
        gap = self.theme.px(4)
        try:
            canvas_width = int(canvas.winfo_width())
        except tk.TclError:
            return
        # 靠近上边缘时把标签压到框线内侧，否则会被窗口裁掉。
        above = y - gap - pad_y - self.theme.px(13) >= 0
        anchor_y = y - gap if above else y + gap
        anchor = "sw" if above else "nw"
        try:
            item = canvas.create_text(
                x + pad_x,
                anchor_y,
                text=text,
                anchor=anchor,
                fill="#FFFFFF",
                font=self.theme.font("tiny"),
            )
            box = canvas.bbox(item)
        except tk.TclError:
            return
        if box is None:
            return
        # 靠近右边缘时整体左移，保证标签完整可见。
        overflow = (box[2] + pad_x) - (canvas_width - self.theme.px(4))
        if overflow > 0:
            try:
                canvas.move(item, -overflow, 0)
                box = canvas.bbox(item)
            except tk.TclError:
                return
            if box is None:
                return
        try:
            rect = canvas.create_rectangle(
                box[0] - pad_x,
                box[1] - pad_y,
                box[2] + pad_x,
                box[3] + pad_y,
                fill=color,
                outline="",
            )
            canvas.tag_lower(rect, item)
        except tk.TclError:
            return

    def _draw_progress(self, index: int, total: int, text: str, tone: str) -> None:
        canvas = self.canvas
        rect = self._rect
        if canvas is None or rect is None:
            return
        theme = self.theme
        width = theme.px(320)
        height = theme.px(58)
        left = max(theme.px(8), (rect["width"] - width) // 2)
        top = theme.px(10)
        try:
            canvas.create_rectangle(
                left, top, left + width, top + height,
                fill="#121214", outline=theme.c("border_strong"), width=1,
            )
            canvas.create_text(
                left + theme.px(14),
                top + theme.px(17),
                text=text,
                anchor="w",
                fill="#F2F2F4",
                font=theme.font("small_strong"),
            )
            canvas.create_text(
                left + width - theme.px(14),
                top + theme.px(17),
                text=f"{min(index, total)}/{total}",
                anchor="e",
                fill="#A9A9B3",
                font=theme.font("tiny"),
            )
            bar_left = left + theme.px(14)
            bar_top = top + height - theme.px(16)
            bar_width = width - theme.px(28)
            canvas.create_rectangle(
                bar_left, bar_top, bar_left + bar_width, bar_top + theme.px(4),
                fill="#2E2E34", outline="",
            )
            filled = int(bar_width * max(0.0, min(1.0, index / max(1, total))))
            if filled > 0:
                canvas.create_rectangle(
                    bar_left, bar_top, bar_left + filled, bar_top + theme.px(4),
                    fill=theme.c(tone), outline="",
                )
        except tk.TclError:
            return


def apply_event(overlay: Overlay, payload: Any) -> None:
    """把调度线程发来的浮层事件翻译成绘制动作。"""

    if not isinstance(payload, dict):
        return
    action = payload.get("action")
    if action == "show":
        overlay.show(payload.get("rect") or {})
    elif action == "hide":
        overlay.hide()
    elif action == "box":
        overlay.box(payload.get("rect") or {}, str(payload.get("kind") or ""), str(payload.get("label") or ""))
    elif action == "clear":
        overlay.clear_boxes()
    elif action == "progress":
        overlay.progress(
            int(payload.get("index") or 0),
            int(payload.get("total") or 1),
            str(payload.get("text") or ""),
            str(payload.get("tone") or "accent"),
        )
