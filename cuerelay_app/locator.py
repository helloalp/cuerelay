# -*- coding: utf-8 -*-
"""目标位置解析。

解析顺序始终是：实时识别 -> 上次识别缓存 -> 手动锚点。
识别失败不会中断流程，只是退化到更粗糙的定位方式，并在日志里说明。
"""

from __future__ import annotations

from typing import Callable, Optional

from . import imaging, ocr, vision, winapi
from .imaging import Bitmap
from .vision import Rect

# 输入框空着时，它里面那行占位文字是最可靠的锚点。
# 面板识别靠"均匀色块 + 上下长边缘"，一旦窗口变成三栏、或者输入框不再居中，
# 采样列就会落到面板外面；而占位文字永远在输入框里。
# 顺序有意义：先试应用专属的长文案，最后才是 "Message" 这种到处都是的短词。
COMPOSER_PLACEHOLDERS = (
    "Work with Codex",
    "Ask anything",
    "Do anything",
    "Send a message",
    "随便说点什么",
    "给 Codex 发送消息",
    "Message",
)


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

    def composer_by_text(self, frame: Frame) -> Optional[Rect]:
        """输入框为空时，用占位符文字反推整个输入框的位置。"""

        if not ocr.available():
            return None
        # 只看窗口最下面这一条：占位符一定在这里，正文里偶然出现的同名词不会混进来。
        region = Rect(0, int(frame.height * 0.65), frame.width, frame.height)
        if region.height < 60:
            return None
        crop = frame.bitmap.crop(region.left, region.top, region.right, region.bottom)
        lines = ocr.recognize(crop)
        if not lines:
            return None

        chat_left, chat_right = vision.detect_chat_bounds(frame.bitmap)

        def derive(line: ocr.OcrLine) -> Optional[Rect]:
            rect = vision.detect_panel_around(
                frame.bitmap,
                region.left + max(1, line.left - 12),
                region.top + line.center[1],
                probe_y=region.top + line.bottom + 6,
            )
            if rect is None:
                return None
            # 输入框必须贴着窗口底部、有正常宽度，而且落在消息区里面。
            # 少了最后这条，横幅按钮上的"Reset usage"之类会被当成占位符。
            if rect.bottom < frame.height * 0.85 or rect.width < 200:
                return None
            if rect.left < chat_left - 8 or rect.right > chat_right + 8:
                return None
            return rect

        for placeholder in COMPOSER_PLACEHOLDERS:
            line = ocr.find_text(lines, placeholder, 0.62)
            if line is None:
                continue
            rect = derive(line)
            if rect is not None:
                return rect

        # 没有占位符说明输入框里已经有内容了。这时候窗口里最靠下的那段文字
        # 一定在输入框里（消息区的内容不会贴到窗口底边），拿它当锚点一样准。
        # 输入框右下角那排小字（模型名、权限）常常也落在候选里，所以多试几行。
        candidates = [line for line in lines if region.left + line.left >= chat_left]
        for line in sorted(candidates, key=lambda item: item.bottom, reverse=True)[:8]:
            rect = derive(line)
            if rect is not None:
                return rect
        return None

    def composer_is_empty(self, frame: Frame, composer: Optional[Rect] = None) -> Optional[bool]:
        """输入框是不是空的（占位符文字回来了）。

        发送成功的判据。像素"变了"靠不住：点错按钮、弹出提示、开始录音
        都会让输入框变化，只有占位符回来才说明内容真的被送走了。
        识别不了时返回 None，由调用方决定怎么处理。
        """

        if not ocr.available():
            return None
        if composer is not None and composer.width >= 120 and composer.height >= 30:
            # 有输入框矩形就只看它：范围越小，越不会被界面别处的文字干扰。
            region = Rect(composer.left, composer.top, composer.right, composer.bottom)
        else:
            region = Rect(0, int(frame.height * 0.55), frame.width, frame.height)
        if region.height < 60:
            return None
        crop = frame.bitmap.crop(region.left, region.top, region.right, region.bottom)
        lines = ocr.recognize(crop)
        if not lines:
            return None
        texts = [ocr.normalize(line.text) for line in lines]
        for placeholder in COMPOSER_PLACEHOLDERS:
            target = ocr.normalize(placeholder)
            if len(placeholder) < 10:
                # 短词（Message 之类）只认完整出现，不做模糊匹配：它在界面别处
                # 太容易撞上，误判成"输入框是空的"会把本来能发的消息拦下来。
                if any(target in text for text in texts):
                    return True
                continue
            if ocr.find_text(lines, placeholder, 0.62) is not None:
                return True
        return False

    def composer(self, frame: Frame) -> tuple[Optional[Rect], str]:
        if self.recognition_enabled:
            # 先看占位符：它是"这里确实是输入框"的直接证据。像素面板识别在
            # 空白较多的界面上容易把一大片背景当成输入框，点下去就全错了。
            try:
                from_text = self.composer_by_text(frame)
            except Exception as exc:
                self.log("warning", f"输入框文字识别异常：{exc}")
                from_text = None
            if from_text is not None:
                return from_text, "识别"

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

    def send_slot(self, frame: Frame, composer: Optional[Rect]) -> tuple[Optional[Rect], str]:
        """发送位置上的那个方块，以及它此刻是发送箭头还是暂停键。

        这个位置会随状态换图标：输入框有内容时是发送箭头，正在生成回答时是暂停。
        外壳一样，必须分辨——点错了会打断正在生成的回答。
        """

        if composer is None or not self.recognition_enabled:
            return None, vision.SEND_SLOT_UNKNOWN
        try:
            slot = vision.detect_send_slot(frame.bitmap, composer)
        except Exception as exc:
            self.log("warning", f"发送按钮识别异常：{exc}")
            return None, vision.SEND_SLOT_UNKNOWN
        if slot is None:
            return None, vision.SEND_SLOT_UNKNOWN
        try:
            kind = vision.send_slot_kind(frame.bitmap, slot)
        except Exception as exc:
            self.log("warning", f"发送图标识别异常：{exc}")
            kind = vision.SEND_SLOT_UNKNOWN
        return slot, kind

    def send_point(self, frame: Frame, composer: Optional[Rect]) -> Optional[tuple[int, int]]:
        if composer is None:
            return None
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
                edge = vision.detect_sidebar_right(frame.bitmap, composer.left) or composer.left
                field = vision.detect_search_field(frame.bitmap, edge)
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
