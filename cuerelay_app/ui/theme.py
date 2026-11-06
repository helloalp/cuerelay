# -*- coding: utf-8 -*-
"""设计令牌：颜色、字号、间距。

所有尺寸都经过 DPI 缩放，保证在 125%/150%/200% 缩放下文字依然清晰可读，
而不是被系统位图拉伸成模糊的小字。
"""

from __future__ import annotations

from typing import Optional

LIGHT = {
    "bg": "#F4F4F6",
    "surface": "#FFFFFF",
    "surface_2": "#FAFAFB",
    "surface_3": "#EFEFF2",
    "border": "#E2E2E6",
    "border_strong": "#CFCFD6",
    "text": "#17171A",
    "text_2": "#5A5A63",
    "text_3": "#8C8C96",
    "accent": "#2C5FE0",
    "accent_hover": "#2450C4",
    "accent_soft": "#E9EEFD",
    "accent_label": "#2C5FE0",
    "accent_text": "#FFFFFF",
    "success": "#1B7F4B",
    "success_soft": "#E7F4EC",
    "warning": "#96620C",
    "warning_soft": "#FAF0DC",
    "danger": "#B4362C",
    "danger_soft": "#FBE9E7",
    "danger_text": "#FFFFFF",
}

DARK = {
    "bg": "#121214",
    "surface": "#1B1B1E",
    "surface_2": "#202024",
    "surface_3": "#26262B",
    "border": "#2E2E34",
    "border_strong": "#3D3D45",
    "text": "#F2F2F4",
    "text_2": "#A9A9B3",
    "text_3": "#75757F",
    "accent": "#3E6BE8",
    "accent_hover": "#4E79F0",
    "accent_soft": "#1C2748",
    "accent_label": "#9DB8FF",
    "accent_text": "#FFFFFF",
    "success": "#4FC088",
    "success_soft": "#152A20",
    "warning": "#D9A24E",
    "warning_soft": "#2E2416",
    "danger": "#E5766A",
    "danger_soft": "#301B19",
    "danger_text": "#1A0D0C",
}

FONT_SPECS = {
    "display": (19, "bold"),
    "title": (14, "bold"),
    "section": (11, "bold"),
    "body": (11, "normal"),
    "strong": (11, "bold"),
    "small": (10, "normal"),
    "small_strong": (10, "bold"),
    "tiny": (9, "normal"),
    "mono": (10, "normal"),
}

CJK_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI")
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "Courier New")


class Theme:
    def __init__(self, mode: str = "light", dpi: int = 96) -> None:
        self.mode = "dark" if str(mode).lower() == "dark" else "light"
        self.colors = dict(DARK if self.mode == "dark" else LIGHT)
        self.scale = max(1.0, float(dpi) / 96.0)
        self.family = CJK_FAMILIES[0]
        self.mono_family = MONO_FAMILIES[0]
        self._fonts: dict[str, tuple] = {}

    # -- 基础 ----------------------------------------------------------

    def c(self, key: str) -> str:
        return self.colors.get(key, "#000000")

    def px(self, value: float) -> int:
        return max(1, int(round(value * self.scale)))

    def detect_fonts(self, root) -> None:
        try:
            from tkinter import font as tkfont

            available = {name.lower() for name in tkfont.families(root)}
        except Exception:
            available = set()
        for family in CJK_FAMILIES:
            if family.lower() in available:
                self.family = family
                break
        for family in MONO_FAMILIES:
            if family.lower() in available:
                self.mono_family = family
                break
        self._fonts.clear()

    def font(self, key: str) -> tuple:
        if key in self._fonts:
            return self._fonts[key]
        size, weight = FONT_SPECS.get(key, FONT_SPECS["body"])
        family = self.mono_family if key == "mono" else self.family
        spec = (family, size, weight) if weight != "normal" else (family, size)
        self._fonts[key] = spec
        return spec

    def tone(self, name: str) -> str:
        mapping = {
            "accent": self.c("accent_label"),
            "success": self.c("success"),
            "warning": self.c("warning"),
            "danger": self.c("danger"),
            "text_2": self.c("text_2"),
            "text_3": self.c("text_3"),
            "text": self.c("text"),
        }
        return mapping.get(name, self.c("text"))


def load_theme(mode: Optional[str]) -> Theme:
    return Theme(mode or "light")
