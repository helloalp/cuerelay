# -*- coding: utf-8 -*-
"""目标位置解析。

解析顺序始终是：实时识别 -> 上次识别缓存 -> 手动锚点。
识别失败不会中断流程，只是退化到更粗糙的定位方式，并在日志里说明。
"""

from __future__ import annotations

from typing import Callable, Optional

from . import imaging, vision, winapi
from .imaging import Bitmap
from .vision import Rect


class Frame:
    """一次窗口客户区快照，识别和坐标换算都以它为基准。"""

    __slots__ = ("hwnd", "rect", "bitmap")

    def __init__(self, hwnd: int, rect: dict[str, int], bitmap: Bitmap) -> None:
        self.hwnd = hwnd
        self.rect = rect
        self.bitmap = bitmap

    @property
    def width(self) -> int:
        return int(self.rect["width"])

    @property
    def height(self) -> int:
        return int(self.rect["height"])

    def to_screen(self, rect: Rect) -> Rect:
        return rect.offset(int(self.rect["left"]), int(self.rect["top"]))

    def point_to_screen(self, point: tuple[int, int]) -> tuple[int, int]:
        return (int(self.rect["left"]) + point[0], int(self.rect["top"]) + point[1])


def grab(hwnd: int) -> Optional[Frame]:
    if not winapi.is_window(hwnd):
        return None
    captured = imaging.capture_client(hwnd)
    if captured is None:
        return None
    bitmap, rect = captured
    return Frame(hwnd, rect, bitmap)


# ---------------------------------------------------------------------------
# 锚点
# ---------------------------------------------------------------------------


def anchor_point(
    anchor: object,
    client_rect: Optional[dict[str, int]],
    window_rect: Optional[dict[str, int]],
) -> Optional[tuple[int, int]]:
    if not isinstance(anchor, dict):
        return None
    rect = window_rect if anchor.get("space") == "window" else client_rect
    if not rect:
        return None
    try:
        rx = float(anchor.get("rx", 0.5))
        ry = float(anchor.get("ry", 0.5))
    except (TypeError, ValueError):
        return None
    rx = min(1.0, max(0.0, rx))
    ry = min(1.0, max(0.0, ry))
    return (
        int(rect["left"]) + round(int(rect["width"]) * rx),
        int(rect["top"]) + round(int(rect["height"]) * ry),
    )


def anchor_rect(anchor: object, frame: Frame) -> Optional[Rect]:
    """只有锚点时，围绕锚点构造一个足够大的校验区域。"""

    point = anchor_point(anchor, frame.rect, winapi.get_window_rect(frame.hwnd))
    if point is None:
        return None
    local_x = point[0] - int(frame.rect["left"])
    local_y = point[1] - int(frame.rect["top"])
    width = max(220, int(frame.width * 0.45))
    height = 64
    left = max(0, min(frame.width - 8, local_x - width // 2))
    top = max(0, min(frame.height - 8, local_y - height // 2))
    return Rect(left, top, min(frame.width, left + width), min(frame.height, top + height))


def cached_composer(settings: dict, frame: Frame) -> Optional[Rect]:
    rect = Rect.from_dict(settings.get("composer_rect"))
    size = settings.get("composer_frame")
    if rect is None or not isinstance(size, dict):
        return None
    try:
        source = (int(size.get("width", 0)), int(size.get("height", 0)))
    except (TypeError, ValueError):
        return None
    if source[0] <= 0 or source[1] <= 0:
        return None
    scaled = rect.scale_to(source, (frame.width, frame.height))
    if scaled.width < 80 or scaled.height < 24:
        return None
    return scaled


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


class Locator:
    def __init__(self, settings: dict, log: Callable[[str, str], None]) -> None:
        self.settings = settings
        self.log = log

    @property
    def recognition_enabled(self) -> bool:
        return bool(self.settings.get("recognition_enabled", True))

    def composer(self, frame: Frame) -> tuple[Optional[Rect], str]:
        if self.recognition_enabled:
            try:
                detected = vision.detect_composer(frame.bitmap)
            except Exception as exc:  # 识别永远不应该让任务直接崩掉
                self.log("warning", f"输入框识别异常：{exc}")
                detected = None
            if detected is not None:
                return detected, "识别"

        cached = cached_composer(self.settings, frame)
        if cached is not None:
            return cached, "缓存"

        fallback = anchor_rect(self.settings.get("anchor_composer"), frame)
        if fallback is not None:
            return fallback, "锚点"
        return None, "失败"

    def send_point(self, frame: Frame, composer: Rect) -> Optional[tuple[int, int]]:
        if self.recognition_enabled:
            try:
                detected = vision.detect_send_button(frame.bitmap, composer)
            except Exception as exc:
                self.log("warning", f"发送按钮识别异常：{exc}")
                detected = None
            if detected is not None:
                return detected

        point = anchor_point(
            self.settings.get("anchor_send"),
            frame.rect,
            winapi.get_window_rect(frame.hwnd),
        )
        if point is not None:
            return (point[0] - int(frame.rect["left"]), point[1] - int(frame.rect["top"]))
        return None

    def search_point(self, frame: Frame, composer: Optional[Rect]) -> Optional[tuple[int, int]]:
        if self.recognition_enabled and composer is not None:
            try:
                field = vision.detect_search_field(frame.bitmap, composer.left)
            except Exception as exc:
                self.log("warning", f"搜索框识别异常：{exc}")
                field = None
            if field is not None:
                return field.center

        point = anchor_point(
            self.settings.get("anchor_search"),
            frame.rect,
            winapi.get_window_rect(frame.hwnd),
        )
        if point is not None:
            return (point[0] - int(frame.rect["left"]), point[1] - int(frame.rect["top"]))
        return None


def remember_composer(settings: dict, frame: Frame, composer: Rect) -> None:
    """把识别结果写回设置，下次识别失败时可以按比例复用。"""

    settings["composer_rect"] = composer.as_dict()
    settings["composer_frame"] = {"width": frame.width, "height": frame.height}
