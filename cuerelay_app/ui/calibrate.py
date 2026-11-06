# -*- coding: utf-8 -*-
"""识别结果预览。

自动识别不可能 100% 正确，所以把识别到的区域直接画在截图上让用户确认，
比让用户盲猜坐标可靠得多。
"""

from __future__ import annotations

import base64
import tkinter as tk
from typing import Callable, Optional

from .. import locator, vision, winapi
from ..config import iso_datetime, now_local
from ..vision import Rect
from .theme import Theme
from .widgets import Button, card


class CalibrationDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        theme: Theme,
        settings: dict,
        window: dict,
        frame: locator.Frame,
        recapture: Callable[[], Optional[locator.Frame]],
        on_save: Callable[[dict], None],
    ) -> None:
        super().__init__(master, bg=theme.c("bg"))
        self.theme = theme
        self.settings = settings
        self.window = window
        self.frame = frame
        self._recapture = recapture
        self._on_save = on_save

        self.composer: Optional[Rect] = None
        self.send: Optional[tuple[int, int]] = None
        self.search: Optional[Rect] = None
        self._photo = None

        self.title("识别结果")
        self.configure(bg=theme.c("bg"))
        self.transient(master)
        self.resizable(False, False)

        self._build()
        self._detect()
        self._render()

    # -- 构建 ----------------------------------------------------------

    def _build(self) -> None:
        theme = self.theme
        pad = theme.px(22)

        header = tk.Frame(self, bg=theme.c("bg"))
        header.pack(fill="x", padx=pad, pady=(pad, theme.px(14)))
        tk.Label(
            header,
            text="识别结果",
            bg=theme.c("bg"),
            fg=theme.c("text"),
            font=theme.font("title"),
            anchor="w",
        ).pack(fill="x")
        self.target_label = tk.Label(
            header,
            text=str(self.window.get("title") or self.window.get("process_name") or ""),
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
            anchor="w",
        )
        self.target_label.pack(fill="x", pady=(theme.px(3), 0))

        outer, inner = card(self, theme, "surface")
        outer.pack(fill="both", expand=True, padx=pad)
        self.canvas = tk.Canvas(inner, bg=theme.c("surface_3"), highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=1, pady=1)

        rows = tk.Frame(self, bg=theme.c("bg"))
        rows.pack(fill="x", padx=pad, pady=(theme.px(16), 0))
        self._status_labels: dict[str, tk.Label] = {}
        for index, (key, text) in enumerate(
            (("composer", "输入框"), ("send", "发送按钮"), ("search", "对话搜索"))
        ):
            row = tk.Frame(rows, bg=theme.c("bg"))
            row.pack(fill="x", pady=(0, theme.px(6)))
            tk.Label(
                row,
                text=text,
                bg=theme.c("bg"),
                fg=theme.c("text_2"),
                font=theme.font("small"),
                width=8,
                anchor="w",
            ).pack(side="left")
            value = tk.Label(
                row,
                text="检测中",
                bg=theme.c("bg"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                anchor="w",
            )
            value.pack(side="left")
            self._status_labels[key] = value

        tk.Label(
            self,
            text="执行时优先实时识别；只有识别失败才会退回到这里保存的位置。",
            bg=theme.c("bg"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        ).pack(fill="x", padx=pad, pady=(theme.px(6), 0))

        footer = tk.Frame(self, bg=theme.c("bg"))
        footer.pack(fill="x", padx=pad, pady=(theme.px(14), pad))
        Button(footer, theme, "重新识别", self._redetect, kind="ghost").pack(side="left")
        Button(footer, theme, "取消", self.destroy, kind="subtle").pack(side="right")
        self.save_button = Button(footer, theme, "保存", self._save, kind="primary")
        self.save_button.pack(side="right", padx=(0, theme.px(8)))

    # -- 识别 ----------------------------------------------------------

    def _detect(self) -> None:
        bitmap = self.frame.bitmap
        try:
            self.composer = vision.detect_composer(bitmap)
        except Exception:
            self.composer = None
        if self.composer is not None:
            try:
                self.send = vision.detect_send_button(bitmap, self.composer)
            except Exception:
                self.send = None
            try:
                self.search = vision.detect_search_field(bitmap, self.composer.left)
            except Exception:
                self.search = None
        else:
            self.send = None
            self.search = None

    def _redetect(self) -> None:
        self.save_button.set_enabled(False)
        self.save_button.set_text("识别中")
        self.update_idletasks()
        self.withdraw()
        try:
            frame = self._recapture()
        finally:
            self.deiconify()
            self.lift()
            self.save_button.set_text("保存")
            self.save_button.set_enabled(True)
        if frame is None:
            self._set_status("composer", "无法截取目标窗口", "danger")
            return
        self.frame = frame
        self._detect()
        self._render()

    # -- 渲染 ----------------------------------------------------------

    def _render(self) -> None:
        theme = self.theme
        bitmap = self.frame.bitmap
        try:
            png, width, height = bitmap.to_png(680, 540)
            self._photo = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
        except Exception:
            self._photo = None

        self.canvas.delete("all")
        if self._photo is not None:
            self.canvas.configure(width=width, height=height)
            self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
            scale = bitmap.width / max(1, width)
            if self.search is not None:
                self._draw_rect(self.search, theme.c("warning"), scale)
            if self.composer is not None:
                self._draw_rect(self.composer, theme.c("accent"), scale)
            if self.send is not None:
                x = self.send[0] / scale
                y = self.send[1] / scale
                radius = theme.px(9)
                self.canvas.create_oval(
                    x - radius, y - radius, x + radius, y + radius,
                    outline=theme.c("success"), width=2,
                )
        else:
            self.canvas.configure(width=theme.px(560), height=theme.px(240))
            self.canvas.create_text(
                theme.px(280), theme.px(120),
                text="无法显示预览图",
                fill=theme.c("text_3"),
                font=theme.font("body"),
            )

        if self.composer is None:
            self._set_status("composer", "未识别", "danger")
        else:
            self._set_status(
                "composer",
                f"已识别 · {self.composer.width}×{self.composer.height}",
                "success",
            )
        self._set_status(
            "send",
            "已识别" if self.send else "未识别（发送时改用 Enter）",
            "success" if self.send else "warning",
        )
        self._set_status(
            "search",
            "已识别" if self.search else "未识别（目标对话留空即可）",
            "success" if self.search else "warning",
        )

    def _draw_rect(self, rect: Rect, color: str, scale: float) -> None:
        self.canvas.create_rectangle(
            rect.left / scale,
            rect.top / scale,
            rect.right / scale,
            rect.bottom / scale,
            outline=color,
            width=2,
        )

    def _set_status(self, key: str, text: str, tone: str) -> None:
        widget = self._status_labels.get(key)
        if widget is not None:
            widget.configure(text=text, fg=self.theme.tone(tone))

    # -- 保存 ----------------------------------------------------------

    def _save(self) -> None:
        if self.composer is None:
            self._set_status("composer", "未识别，无法保存", "danger")
            return

        frame = self.frame
        rect = frame.rect
        width = max(1, int(rect["width"]))
        height = max(1, int(rect["height"]))

        def anchor(point: tuple[int, int]) -> dict:
            return {
                "rx": round((point[0] - int(rect["left"])) / width, 6),
                "ry": round((point[1] - int(rect["top"])) / height, 6),
                "space": "client",
            }

        patch: dict = {
            "target_title": str(self.window.get("title") or ""),
            "target_process": str(self.window.get("process_name") or ""),
            "target_geometry": winapi.capture_window_geometry(int(self.window["hwnd"])),
            "composer_rect": self.composer.as_dict(),
            "composer_frame": {"width": width, "height": height},
            "anchor_composer": anchor(frame.point_to_screen(self.composer.center)),
            "calibrated_at": iso_datetime(now_local()),
        }
        if self.send is not None:
            patch["anchor_send"] = anchor(frame.point_to_screen(self.send))
        if self.search is not None:
            patch["anchor_search"] = anchor(frame.point_to_screen(self.search.center))

        self._on_save(patch)
        self.destroy()
