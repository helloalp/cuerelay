# -*- coding: utf-8 -*-
"""执行流水线。

与 1.x 的关键区别：不再"点一下再睡一会儿然后祈祷成功"，而是每一步都验证。
粘贴后确认输入框像素真的变了，发送后确认输入框真的清空了；任一步没有
发生变化就重试一次，仍然失败则明确报错，而不是静默失败。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import apps, conversations, imaging, locator, ocr, templates, vision, winapi
from .vision import Rect


class AutomationError(RuntimeError):
    pass


@dataclass
class RunOutcome:
    ok: bool
    message: str
    duration_ms: int = 0
    detail: str = ""


class Runner:
    # 框画出来之后停一下再点。框和点击几乎是同时发生的，不停这一下，
    # 用户只看到窗口闪了一下，看不到"它认的是哪一个元素"。
    BOX_DWELL = 0.3

    def __init__(
        self,
        settings: dict[str, Any],
        log: Callable[[str, str], None],
        exclude_hwnd: int = 0,
        emit: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self.settings = settings
        self.log = log
        self.exclude_hwnd = int(exclude_hwnd or 0)
        self.emit = emit
        # 是否已经把内容交给目标应用。失败重试要靠它判断"重来一次会不会发两遍"：
        # 发送动作一旦发生，再重试就可能让同一个提示词发出去两次。
        self.sent = False

    # -- 浮层 ----------------------------------------------------------

    def _overlay(self, action: str, **payload: Any) -> None:
        """浮层只是给人看的，任何异常都不能影响执行。"""

        if self.emit is None:
            return
        if not self.settings.get("show_overlay", True):
            return
        try:
            self.emit("overlay", {"action": action, **payload})
        except Exception:
            pass

    def _overlay_hide(self) -> None:
        self._overlay("hide")

    def _overlay_show(self, frame: locator.Frame) -> None:
        self._overlay("show", rect=dict(frame.rect))

    def _overlay_box(self, rect: Rect, kind: str, label: str = "") -> None:
        self._overlay("box", rect=rect.as_dict(), kind=kind, label=label)

    def _overlay_progress(self, index: int, total: int, text: str, tone: str = "accent") -> None:
        self._overlay("progress", index=index, total=total, text=text, tone=tone)

    # -- 基础 ----------------------------------------------------------

    def _delay(self) -> float:
        try:
            value = float(self.settings.get("step_delay_seconds", 1.2))
        except (TypeError, ValueError):
            value = 1.2
        return max(0.3, min(10.0, value))

    def _locator(self) -> locator.Locator:
        return locator.Locator(self.settings, self.log)

    def _frame(self, hwnd: int) -> locator.Frame:
        frame = locator.grab(hwnd)
        if frame is None:
            raise AutomationError("无法读取目标窗口画面，请确认窗口没有被最小化。")
        return frame

    @staticmethod
    def _capture(rect: Rect):
        return imaging.capture_screen(rect.left, rect.top, rect.width, rect.height)

    def _wait_change(
        self,
        rect: Rect,
        baseline,
        timeout: float,
        threshold: float = 3.0,
        min_ratio: float = 0.0,
    ) -> tuple[bool, Any]:
        deadline = time.monotonic() + max(0.2, timeout)
        latest = baseline
        while time.monotonic() < deadline:
            current = self._capture(rect)
            if current is None:
                time.sleep(0.12)
                continue
            latest = current
            if min_ratio > 0:
                # 要求"变化面积足够大"，用来区分"面板真的换了"和"某个控件闪了一下"
                if vision.changed_ratio(baseline, current) >= min_ratio:
                    return True, current
            elif vision.looks_changed(baseline, current, threshold):
                return True, current
            time.sleep(0.12)
        return False, latest

    def _stable_baseline(self, rect: Rect, tries: int = 4):
        """等画面稳定下来再取基线，避免滚动动画的余波被当成"界面变了"。"""

        previous = self._capture(rect)
        for _ in range(max(1, tries)):
            time.sleep(0.25)
            current = self._capture(rect)
            if previous is not None and current is not None:
                if vision.changed_ratio(previous, current) < 0.004:
                    return current
            previous = current
        return previous

    def _locate_target(self) -> Optional[dict[str, Any]]:
        """按关键词找当前可见的目标窗口。"""

        keywords = winapi.parse_keywords(str(self.settings.get("target_keywords") or ""))
        return winapi.find_target_window(
            keywords,
            exclude_hwnd=self.exclude_hwnd,
            title_hint=str(self.settings.get("target_title") or ""),
            process_hint=str(self.settings.get("target_process") or ""),
        )

    def _target_app(self) -> Optional[apps.AppSpec]:
        """目标应用的启动方式。填了自定义路径就按路径启动，否则按 Codex 处理。"""

        path = str(self.settings.get("target_path") or "").strip()
        if path:
            stem = Path(path).stem or "target"
            return apps.AppSpec(
                key="target",
                label=stem,
                process=stem,
                path=path,
                log=self.log,
            )
        return apps.codex_app(self.settings, self.log)

    def _find_window(self) -> dict[str, Any]:
        """找目标窗口；没开就按设置把它拉起来。

        用户要的是"到点就把消息发出去"，不该因为应用没打开就失败。所以这里
        先看有没有窗口，再退一步看进程在不在（收进托盘的窗口不可见），
        最后才启动一个新实例。
        """

        window = self._locate_target()
        if window is not None:
            return window

        if not bool(self.settings.get("auto_launch_target", True)):
            raise AutomationError("没有找到目标窗口。请先打开目标应用，或在设置中调整关键词。")

        spec = self._target_app()
        if spec is None:
            raise AutomationError("没有找到目标窗口，也没有可用的启动方式。")
        self.log("info", f"目标应用没有在前台可见，正在拉起 {spec.label}。")
        window = spec.ensure_visible()
        if window is None:
            raise AutomationError(
                f"没能把 {spec.label} 拉到前台，已停止以避免误输入。"
                "如果它需要管理员权限，请先手动启动一次。"
            )
        self.log("info", f"已前置 {spec.label}：{window.get('title') or ''}")
        return window

    def _activate(self, window: dict[str, Any]) -> int:
        hwnd = int(window["hwnd"])
        geometry = None
        if self.settings.get("restore_target_geometry", True):
            geometry = self.settings.get("target_geometry")
        if not winapi.ensure_foreground(hwnd, geometry):
            raise AutomationError("无法把目标窗口切到前台，已停止以避免误输入。")
        return hwnd

    def _resolve_composer(self, hwnd: int) -> tuple[locator.Frame, Rect, str]:
        frame = self._frame(hwnd)
        composer, source = self._locator().composer(frame)
        if composer is None:
            # 生成回答时输入框会被"停止"按钮顶掉，这时候报"重新识别目标"
            # 会把人引到错误的方向。先说清楚可能是哪种情况。
            raise AutomationError(
                "没有找到输入框：目标可能正在生成回复，或者界面还没加载完。"
                "等它停下来再试；如果一直这样，再到设置里重新识别目标。"
            )
        return frame, composer, source

    # -- 对话切换 ------------------------------------------------------

    def _sidebar_right(self, frame: locator.Frame, composer: Optional[Rect]) -> int:
        """左侧面板的右边界。识别不出来时按窗口宽度取一个保守值。"""

        limit = composer.left if composer is not None else 0
        try:
            edge = vision.detect_sidebar_right(frame.bitmap, limit)
        except Exception:
            edge = None
        if edge and edge > 160:
            return int(edge)
        return max(200, int(frame.width * 0.16))

    def _list_region(self, frame: locator.Frame, composer: Optional[Rect]) -> Rect:
        """侧边栏里"对话列表"所在的那块区域，去掉左侧图标栏。"""

        edge = self._sidebar_right(frame, composer)
        left = max(0, edge * 13 // 100)
        bottom = composer.top if composer is not None else int(frame.height * 0.88)
        return Rect(left, 0, edge, max(left + 60, bottom))

    def _list_rows(self, frame: locator.Frame, composer: Optional[Rect]) -> list[ocr.OcrLine]:
        """把侧边栏列表读成文字行，坐标换回客户区。"""

        region = self._list_region(frame, composer)
        crop = frame.bitmap.crop(region.left, region.top, region.right, region.bottom)
        rows: list[ocr.OcrLine] = []
        for line in ocr.recognize(crop):
            if line.width < 24:  # 图标被识别成的单个字符，跳过
                continue
            rows.append(
                ocr.OcrLine(
                    line.text,
                    region.left + line.left,
                    region.top + line.top,
                    line.width,
                    line.height,
                )
            )
        return rows

    def _chat_title_lines(self, frame: locator.Frame) -> list[ocr.OcrLine]:
        """消息区顶部显示的当前对话名。"""

        edge = self._sidebar_right(frame, None)
        region = Rect(edge + 2, 0, min(frame.width, edge + 1200), 170)
        crop = frame.bitmap.crop(region.left, region.top, region.right, region.bottom)
        lines = [line for line in ocr.recognize(crop) if line.width >= 40]
        return [
            ocr.OcrLine(line.text, region.left + line.left, region.top + line.top, line.width, line.height)
            for line in lines
        ]

    def _conversation_matches(self, frame: locator.Frame, title: str) -> bool:
        if not title:
            return True
        lines = self._chat_title_lines(frame)
        return ocr.find_line(lines, title, 0.9, strict=True) is not None

    def _verify_conversation(self, hwnd: int, title: str, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            try:
                frame = self._frame(hwnd)
            except AutomationError:
                return False
            if self._conversation_matches(frame, title):
                return True
            time.sleep(0.45)
        return False

    def _composer_empty(self, hwnd: int, composer: Optional[Rect] = None) -> Optional[bool]:
        """输入框是否已经回到空状态。识别不了时返回 None。"""

        try:
            frame = self._frame(hwnd)
        except AutomationError:
            return None
        try:
            return self._locator().composer_is_empty(frame, composer)
        except Exception:
            return None

    def _wait_send_ready(
        self,
        hwnd: int,
        composer: Rect,
        timeout: float = 20.0,
    ) -> tuple[locator.Frame, Rect, Optional[Rect], str]:
        """等目标应用生成完回答：发送位置从暂停键变回发送箭头。

        返回刷新后的画面、输入框、发送方块和它此刻的身份。等不到就把身份
        留在 pause，让调用方明确放弃这次发送，而不是去点那个暂停键。
        """

        deadline = time.time() + max(2.0, timeout)
        frame = self._frame(hwnd)
        slot, kind = self._locator().send_slot(frame, composer)
        while kind == vision.SEND_SLOT_PAUSE and time.time() < deadline:
            time.sleep(0.8)
            try:
                frame = self._frame(hwnd)
            except AutomationError:
                break
            grown, _ = self._locator().composer(frame)
            if grown is not None:
                composer = grown
            slot, kind = self._locator().send_slot(frame, composer)
        return frame, composer, slot, kind

    def _locate_row(
        self,
        hwnd: int,
        composer: Optional[Rect],
        title: str,
        scrolls: int = 40,
    ) -> tuple[locator.Frame, Optional[ocr.OcrLine]]:
        """在侧边栏列表里找到标题对应的那一行。

        四层，从便宜到贵：
          1. 当前这一屏里就有；
          2. 回到顶部，再一屏一屏往下翻，直到列表不再变化（翻到底了）；
          3. 列表里有「Show more」就点开，新露出来的对话再找一遍；
          4. 交给调用方去用搜索框。

        第 2 层不写死"翻几屏就放弃"——历史长的账号一屏一屏数根本数不完。
        第 3 层是因为这个列表是按项目分组的，一个项目下超过五条就折成一行
        「Show more」：折叠着的那部分根本不在列表里，只滚不点永远找不到。
        """

        frame: locator.Frame = self._frame(hwnd)
        for _ in range(3):
            frame, row = self._scroll_and_find(hwnd, composer, title, scrolls)
            if row is not None:
                return frame, row
            if not self._click_show_more(hwnd):
                break
        return frame, None

    def _scroll_and_find(
        self,
        hwnd: int,
        composer: Optional[Rect],
        title: str,
        scrolls: int,
    ) -> tuple[locator.Frame, Optional[ocr.OcrLine]]:
        """先回到顶部，再一屏一屏往下翻，边翻边找。"""

        frame = self._frame(hwnd)
        rows = self._list_rows(frame, composer)
        row = ocr.find_line(rows, title, 0.9, strict=True)
        if row is not None:
            return frame, row

        region = self._list_region(frame, composer)
        point = frame.point_to_screen(region.center)
        signature = tuple(item.text for item in rows)

        # 回到顶部：连续上滚，直到列表内容不再变化
        for _ in range(12):
            winapi.scroll(point, -10)
            time.sleep(0.35)
            frame = self._frame(hwnd)
            rows = self._list_rows(frame, composer)
            row = ocr.find_line(rows, title, 0.9, strict=True)
            if row is not None:
                return frame, row
            current = tuple(item.text for item in rows)
            if current == signature:
                break
            signature = current

        # 逐屏向下找，一直翻到列表不动为止
        limit = max(1, int(scrolls))
        for index in range(1, limit + 1):
            winapi.scroll(point, 6)
            time.sleep(0.4)
            frame = self._frame(hwnd)
            rows = self._list_rows(frame, composer)
            row = ocr.find_line(rows, title, 0.9, strict=True)
            if row is not None:
                return frame, row
            current = tuple(item.text for item in rows)
            if current == signature:
                self.log("info", f"已翻到列表底部，没有「{title}」。")
                break
            signature = current
            if index % 5 == 0:
                # 列表很长时翻起来要一会儿，得让人看见还在动。
                self.log("info", f"列表里暂时没有「{title}」，已往下翻 {index} 屏。")
        return frame, None

    # 侧边栏里"折叠着更多"的那一行，不同版本叫法不一样。
    SHOW_MORE_KEYS = frozenset(
        ocr.normalize(item)
        for item in ("show more", "show less", "see more", "see all", "显示更多", "查看更多", "展开更多")
    )

    def _click_show_more(self, hwnd: int) -> bool:
        """点开列表里的「Show more」，点到了就返回 True。

        一个项目下超过五条对话就会折成这一行。折叠着的那部分不在列表里，
        只滚不点，永远也找不到。
        """

        frame, composer, rows = self._rows_once(hwnd)
        for row in rows:
            stripped, _ = conversations.strip_icon(row.text)
            if ocr.normalize(stripped) not in self.SHOW_MORE_KEYS:
                continue
            if winapi.foreground_window() != int(hwnd):
                self.log("warning", "目标窗口不在前台，没有点「Show more」。")
                return False
            winapi.click(frame.point_to_screen(row.center))
            time.sleep(min(self._delay(), 1.2))
            self.log("info", f"点开了「{stripped}」，继续找。")
            return True
        return False    # 搜索面板里的分组标题。有它们就说明面板开着。
    # 面板空着的时候是「Quick actions」，输入关键词之后变成「Chats」。
    SEARCH_HEADERS = frozenset(
        ocr.normalize(item)
        for item in (
            "Quick actions",
            "Chats",
            "Recent files",
            "快捷操作",
            "聊天",
            "最近文件",
        )
    )

    # 面板浮在窗口水平方向的正中，左边那一窄条是原来的对话列表。只在这个
    # 范围里找，侧边栏里同名的行就不会被当成面板的一部分。
    PANEL_SPAN = (0.22, 0.78)

    def _panel_span(self, frame: locator.Frame) -> tuple[float, float]:
        return frame.width * self.PANEL_SPAN[0], frame.width * self.PANEL_SPAN[1]

    def _panel_anchor(self, lines: list[ocr.OcrLine], frame: locator.Frame) -> Optional[ocr.OcrLine]:
        """面板最上面那个分组标题。

        取最靠上的一个：面板里「Quick actions」在「Chats」上面，认到哪个都
        能确定输入框的位置——输入框就在第一个分组标题的上面。

        三条限制都是被真实画面逼出来的：
          · 只在中段找，左边侧边栏和右边面板的文字一律不算；
          · 只在上半部分找，对话正文里的「Chats ...」不会跑进来；
          · 标题后面最多容忍两个字符，OCR 会把「Chats」读成「Chats O」，
            但绝不能把「ChatGPT can edit any file...」当成标题。
        """

        low, high = self._panel_span(frame)
        ceiling = frame.height * 0.7
        inside = [
            line for line in lines if 40 < line.top < ceiling and low < line.left < high
        ]
        inside.sort(key=lambda line: line.top)
        for line in inside:
            key = ocr.normalize(line.text)
            for head in self.SEARCH_HEADERS:
                if key == head:
                    return line
                if key.startswith(head) and len(key) <= len(head) + 2:
                    return line
        return None

    def _search_input(self, hwnd: int) -> tuple[locator.Frame, Optional[Rect]]:
        """找搜索面板的输入框，返回它在客户区里的矩形。

        面板浮在窗口正中：最上面是输入框，下面是一行分组标题（空着的时候是
        「Quick actions」，输入之后是「Chats」）。所以先认分组标题确认面板开着，
        输入框就是它上面那一行。

        不靠占位文字认输入框：搜过一次之后词会留在框里，占位文字就没了，
        按"Search ChatGPT"去找会一直找不到——明明面板就在眼前。
        """

        frame = self._frame(hwnd)
        lines = ocr.recognize(frame.bitmap)
        mark = self._panel_anchor(lines, frame)
        if mark is None:
            return frame, None

        # 面板里所有的字，用来量面板有多宽：最左的那行是行首图标，最右的
        # 那行是右侧的快捷键（Ctrl+N）或项目名。
        inside = [
            line
            for line in lines
            if mark.top - 100 <= line.top <= mark.top + 700 and line.left >= mark.left - 40
        ]
        left = min((line.left for line in inside), default=mark.left) - 12
        right = max((line.right for line in inside), default=mark.right) + 12

        # 输入框里那一行字：标题正上方最近的一行
        above = [line for line in inside if line.bottom < mark.top - 6 and line.top > mark.top - 100]
        if above:
            top = min(line.top for line in above) - 12
            bottom = max(line.bottom for line in above) + 12
        else:
            # 输入框空着，认不到字：按标题的位置往上退一行。
            top = mark.top - 62
            bottom = mark.top - 22
        return frame, Rect(left, max(0, top), right, bottom)

    # 标题栏那一行（「Codex ⌄」+ 铃铛 + 放大镜）的垂直位置。窗口大小变了、
    # 缩放比例变了它都不动，所以先按这个固定范围扫，扫不到再用 OCR 认「Codex」。
    HEADER_BAND = (86, 118)

    def _magnifier_point(self, frame: locator.Frame) -> Optional[tuple[int, int]]:
        """侧边栏标题栏里那个放大镜的位置。

        不存坐标：标题栏里从左到右是「Codex ⌄」、铃铛、放大镜，右边还有
        内容区的图标。所以先找出侧边栏的右边界，再在这一段里找图标大小
        （10~30 像素宽）的连通块，最后一个就是放大镜。

        垂直位置不能按窗口高度算：菜单栏和标题栏都是固定像素高，窗口从
        3840x2115 缩到 2560x1410 时，按比例算出来的那条带会落在菜单栏和
        标题栏之间的空白上，一个图标都扫不到。改成先用固定范围，扫不到再
        用 OCR 认「Codex」——它和图标在同一行，认出来就知道这一行在哪。
        """

        right = vision.detect_sidebar_right(frame.bitmap) or 0
        if not (120 < right <= min(frame.width, 1000)):
            return None
        for top, bottom in self._header_bands(frame, right):
            point = self._icon_in_band(frame, right, top, bottom)
            if point is not None:
                return point
        return None

    def _header_bands(self, frame: locator.Frame, right: int) -> list[tuple[int, int]]:
        """先给固定范围，再给一个用 OCR 算出来的范围。"""

        bands = [self.HEADER_BAND]
        try:
            strip = frame.bitmap.crop(0, 0, min(frame.width, right), min(frame.height, 220))
            head = ocr.find_text(ocr.recognize(strip), "Codex", 0.8)
        except Exception:
            head = None
        if head is not None:
            bands.insert(0, (max(0, head.top - 6), min(frame.height, head.bottom + 6)))
        return bands

    @staticmethod
    def _icon_in_band(
        frame: locator.Frame, right: int, top: int, bottom: int
    ) -> Optional[tuple[int, int]]:
        """在一条横带里找图标大小的墨迹块，取最右边那个。"""

        if bottom <= top + 4:
            return None
        runs: list[tuple[int, int]] = []
        start = None
        for x in range(40, max(41, right - 6)):
            inked = any(frame.bitmap.lum(x, y) > 90 for y in range(top, bottom))
            if inked and start is None:
                start = x
            elif not inked and start is not None:
                runs.append((start, x))
                start = None
        if start is not None:
            runs.append((start, max(41, right - 6)))
        icons = [item for item in runs if 10 <= item[1] - item[0] <= 30]
        if not icons:
            return None
        left, end = icons[-1]
        return ((left + end) // 2, (top + bottom) // 2)

    def _result_lines(
        self, lines: list[ocr.OcrLine], width: int, below: Rect
    ) -> list[ocr.OcrLine]:
        """搜索面板结果区里的候选行。

        三个条件：在输入框下面、贴着面板的左边缘、在窗口水平方向的中段。

        「贴着面板左边缘」是最要紧的一条。搜索面板是浮在对话正文上面的，
        正文里完全可能出现一行字就叫「20261021 18」——只按文字相似度挑，
        会挑中正文里那一行，点下去等于什么都没做（真的踩过）。结果行的行首
        永远和输入框里的字对齐，正文的行首要靠右得多。
        """

        low = width * self.PANEL_SPAN[0]
        high = width * self.PANEL_SPAN[1]
        return [
            line
            for line in lines
            if line.top > below.bottom + 8
            and low < line.left < high
            and below.left - 20 <= line.left <= below.left + 140
        ]

    def _search_result(
        self, hwnd: int, title: str, below: Rect
    ) -> tuple[locator.Frame, Optional[ocr.OcrLine]]:
        """在搜索面板的结果列表里找标题那一行。

        结果不是瞬间出来的：等不到就再看一眼，最多三眼。
        """

        for attempt in range(3):
            frame = self._frame(hwnd)
            lines = self._result_lines(ocr.recognize(frame.bitmap), frame.width, below)
            hit = self._best_title(lines, title)
            if hit is not None:
                return frame, hit
            if attempt < 2:
                time.sleep(0.6)
        return self._frame(hwnd), None




    @staticmethod
    def _result_label(text: str, title: str) -> str:
        """结果行在浮层上显示什么。

        OCR 常把行首那个对话气泡图标读成一个字符（`0 20261021 18`）。去掉
        之后剩下的部分还能和标题对上，就按剩下的显示——否则原样显示，别
        为了好看把真正认到的内容改掉。
        """

        cleaned = text.strip()
        first, _, rest = cleaned.partition(" ")
        rest = rest.strip()
        if len(first) <= 2 and rest and ocr.title_similarity(rest, title) >= 0.9:
            return rest
        return cleaned

    @staticmethod
    def _best_title(lines: list[ocr.OcrLine], title: str) -> Optional[ocr.OcrLine]:
        """挑出最像目标标题的那一行。

        不要求一模一样：搜索结果前面常带着一个图标，OCR 会把它读成 "0" 或者
        一个字母（"0 20261021 18"）。按相似度取最高的那一行，阈值放在
        0.75——低于这个就宁可认成没找到，也不乱点一条别的对话。
        """

        best = None
        score = 0.0
        for line in lines:
            value = ocr.title_similarity(line.text, title)
            if value > score:
                best, score = line, value
        return best if score >= 0.75 else None

    def _open_search(
        self, hwnd: int, frame: locator.Frame, note: Optional[Callable[[str], None]] = None
    ) -> tuple[Optional[locator.Frame], Optional[Rect]]:
        """点放大镜打开搜索面板，返回面板所在画面和输入框的矩形。

        只走放大镜这一条路：标题栏里那个图标点下去，面板浮到窗口正中。
        开没开不看像素变化，看画面上有没有分组标题——面板在，标题就在。
        """

        if winapi.foreground_window() != int(hwnd):
            self.log("warning", "目标窗口不在前台，没有去点放大镜。")
            return None, None

        point = self._magnifier_point(frame)
        if point is None:
            self.log("warning", "标题栏里没找到放大镜。")
            return None, None
        self._overlay_box(
            frame.to_screen(Rect(point[0] - 14, point[1] - 14, point[0] + 14, point[1] + 14)),
            "search",
            "放大镜",
        )
        if note is not None:
            note("打开搜索")

        # 面板可能已经开着（上一次留下的，或者上一次点了没关掉）。开着就直接
        # 用——放大镜是开关，再点一下反而会把它关掉。
        fresh, box = self._search_input(hwnd)
        if box is not None:
            self._overlay_box(fresh.to_screen(box), "search", "搜索框")
            self.log("info", "搜索面板本来就开着。")
            return fresh, box

        # 第一次点击有时只是把窗口激活，面板不弹。点两三次都算正常，
        # 每次都以"画面上有没有分组标题"为准，没出来就再点一次。
        for attempt in range(3):
            winapi.click(frame.point_to_screen(point))
            time.sleep(0.9)
            fresh, box = self._search_input(hwnd)
            if box is not None:
                self._overlay_box(fresh.to_screen(box), "search", "搜索框")
                self.log("info", "搜索面板已打开。")
                return fresh, box
            if attempt < 2:
                self.log("info", "还没看到搜索面板，再点一次放大镜。")
        self.log("warning", "点了放大镜，但画面上没有出现搜索面板。")
        return None, None

    def _switch_conversation(
        self,
        hwnd: int,
        title: str,
        step: Optional[Callable[..., None]] = None,
    ) -> None:
        strict = bool(self.settings.get("strict_conversation", True))
        frame = self._frame(hwnd)
        composer, _ = self._locator().composer(frame)
        self.log("info", f"切换对话：{title}")

        def note(text: str, tone: str = "accent") -> None:
            """换一下浮层上那行字，不占新的进度格。"""

            if step is None:
                return
            try:
                step(text, tone, bump=False)
            except TypeError:
                pass

        # 浮层要先亮起来。没亮的时候 box()/progress() 都是空操作，下面每一步
        # 画的框会悄悄丢掉——用户看到的就只剩"窗口自己动了几下"。
        self._overlay_show(frame)
        note("切换对话")

        # 侧边栏收着的时候列表区里只有图标，翻也翻不出标题。先展开；展不开
        # 就别在图标上白翻，直接走搜索。
        if not self.ensure_sidebar(hwnd):
            self.log("warning", "侧边栏收着，列表里看不到对话，改用搜索框。")

        # 首选路径：侧边栏列表里本来就有这个对话，直接点它。
        # 这条路不依赖搜索框，也不用关心应用当前停在哪个对话上。
        frame, row = self._locate_row(hwnd, composer, title)
        if row is not None:
            self._overlay_box(
                frame.to_screen(Rect(row.left, row.top, row.right, row.bottom)),
                "search",
                f"对话 · {row.text}",
            )
            time.sleep(self.BOX_DWELL)
            winapi.click(frame.point_to_screen(row.center))
            time.sleep(min(self._delay(), 1.2))
            if self._verify_conversation(hwnd, title, 6.0):
                self.log("info", f"已切换到：{title}")
                return
            self.log("warning", f"点了「{row.text}」，但标题栏没有变成「{title}」。")

        # 兜底路径：列表里没有（可能折在项目分组里，也可能滚不到底），改用搜索。
        fresh, box = self._open_search(hwnd, frame, note)
        if box is not None and fresh is not None:
            note("输入关键词")
            time.sleep(self.BOX_DWELL)
            winapi.click(fresh.point_to_screen(box.center))
            time.sleep(0.3)
            winapi.ctrl_a()
            winapi.set_clipboard_text(title)
            winapi.ctrl_v()
            time.sleep(min(self._delay(), 1.2))
            self.log("info", f"已把「{title}」输入搜索框。")

            # 结果就在输入框下面。认出哪一行是目标，框住它，再点——
            # 比按回车稳：回车落在"当前高亮的那一条"，而高亮在哪并不确定。
            note("选中结果")
            frame, hit = self._search_result(hwnd, title, box)
            if hit is not None:
                shown = self._result_label(hit.text, title)
                self._overlay_box(
                    frame.to_screen(
                        Rect(hit.left - 8, hit.top - 8, hit.right + 8, hit.bottom + 8)
                    ),
                    "search",
                    f"搜索结果 · {shown}",
                )
                self.log("info", f"搜索结果里认出了「{shown}」，点它。")
                time.sleep(self.BOX_DWELL)
                winapi.click(frame.point_to_screen(hit.center))
                time.sleep(min(self._delay(), 1.2))
                if self._verify_conversation(hwnd, title, 6.0):
                    self.log("info", f"已切换到：{title}")
                    return
                self.log("warning", f"点了搜索结果「{shown}」，标题栏没有变成「{title}」。")
            else:
                self.log("warning", f"搜索结果里没认出「{title}」这一条。")
                winapi.press_escape()

        message = f"没能切换到「{title}」，消息会发到当前对话。"
        if strict:
            raise AutomationError(f"找不到对话「{title}」，已停止发送。")
        self.log("warning", message)

    # -- 粘贴 ----------------------------------------------------------

    def _paste(self, hwnd: int, frame: locator.Frame, composer: Rect, retry: bool = False) -> None:
        if retry:
            point = (composer.center[0], composer.top + int(composer.height * 0.72))
        else:
            point = composer.center
        winapi.click(frame.point_to_screen(point))
        time.sleep(min(self._delay(), 0.5))
        if winapi.foreground_window() != hwnd and not winapi.ensure_foreground(hwnd):
            raise AutomationError("目标窗口在粘贴前失去焦点。")
        winapi.ctrl_v()
        time.sleep(0.12)

    # -- 应用开关 ------------------------------------------------------

    def _run_launch(self, task: dict[str, Any]) -> RunOutcome:
        """定时启动：先把代理确保起来，再把目标拉到前台。"""

        started = time.monotonic()
        window = apps.start_codex(self.settings, self.log)
        duration = int((time.monotonic() - started) * 1000)
        if window is None:
            raise AutomationError("没能把目标应用拉到前台。")
        title = str(window.get("title") or "")
        message = f"已启动并前置{f'：{title}' if title else ''}"
        self.log("success", message)
        return RunOutcome(ok=True, message=message, duration_ms=duration)

    def _run_close(self, task: dict[str, Any]) -> RunOutcome:
        """定时关闭：真的退出，而不是收进托盘。"""

        started = time.monotonic()
        spec = apps.codex_app(self.settings, self.log)
        if spec.status() == "未运行":
            message = "目标应用本来就没有运行"
            self.log("info", message)
            return RunOutcome(ok=True, message=message, duration_ms=0)
        if not spec.close(force=True):
            raise AutomationError("关闭目标应用失败，进程还在运行。")
        duration = int((time.monotonic() - started) * 1000)
        message = f"已关闭 {spec.label}"
        self.log("success", message)
        return RunOutcome(ok=True, message=message, duration_ms=duration)

    # -- 主流程 --------------------------------------------------------

    def run_task(self, task: dict[str, Any]) -> RunOutcome:
        kind = str(task.get("kind") or "prompt")
        if kind == "launch":
            return self._run_launch(task)
        if kind == "close":
            return self._run_close(task)

        started = time.monotonic()
        content = str(task.get("content") or "")
        if not content.strip():
            raise AutomationError("提示词内容为空。")

        action = str(task.get("action") or "click")
        conversation = str(task.get("conversation_title") or "").strip()

        steps = 3
        if conversation:
            steps += 1
        if self.settings.get("verify_paste", True):
            steps += 1
        if self.settings.get("verify_send", True):
            steps += 1
        step_index = 0

        def advance(text: str, tone: str = "accent", bump: bool = True) -> None:
            """推进一格进度。bump=False 只改文案，不占新的格数。"""

            nonlocal step_index
            if bump:
                step_index += 1
            self._overlay_progress(step_index, steps, text, tone)

        old_clipboard = winapi.get_clipboard_text()
        try:
            saved_cursor = None
            try:
                saved_cursor = winapi.get_cursor_pos()
            except Exception:
                saved_cursor = None

            window = self._find_window()
            hwnd = self._activate(window)
            self.log("info", f"目标窗口：{window.get('title') or window.get('process_name')}")
            time.sleep(min(self._delay(), 0.6))

            if conversation:
                advance("切换对话")
                self._switch_conversation(hwnd, conversation, advance)

            advance("定位输入框")
            frame, composer, source = self._resolve_composer(hwnd)
            self.log("info", f"输入框定位：{source}")
            self._overlay_show(frame)
            self._overlay_box(
                frame.to_screen(composer),
                "composer",
                f"输入框 · {source} · {composer.width}×{composer.height}",
            )
            locator.remember_composer(self.settings, frame, composer)

            screen_composer = frame.to_screen(composer)
            # 变量在最后一刻展开：定时任务里的 {{date}} 要的是"执行那天"，
            # 而不是当初保存任务那天。{{clipboard}} 用的是替换剪贴板之前的值。
            content = templates.expand(content, clipboard=old_clipboard or "")
            winapi.set_clipboard_text(content)

            advance("粘贴提示词")
            # 基线要在浮层画完之后取：浮层同样会被截进画面，
            # 否则"输入框是否变化"会被自己的框线骗成"已经变化"。
            baseline = self._capture(screen_composer)
            if baseline is None:
                raise AutomationError("无法读取输入框画面，请确认目标窗口没有被遮挡。")
            self._paste(hwnd, frame, composer)
            verify_paste = bool(self.settings.get("verify_paste", True))
            if verify_paste:
                advance("校验粘贴")
                changed, _ = self._wait_change(screen_composer, baseline, 4.0)
                if not changed:
                    self.log("warning", "输入框没有变化，重试粘贴。")
                    self._paste(hwnd, frame, composer, retry=True)
                    changed, _ = self._wait_change(screen_composer, baseline, 4.0)
                if not changed:
                    raise AutomationError("粘贴后输入框没有变化，已停止发送。")
            else:
                time.sleep(min(self._delay(), 1.0))

            # 输入框可能因为多行内容而变高，发送前必须重新定位。
            fresh = self._frame(hwnd)
            grown, _ = self._locator().composer(fresh)
            if grown is None:
                grown = composer

            # 粘贴完必须确认输入框里真的有内容。空的时候同一个位置画的是麦克风，
            # 点下去会开始录音而不是发送——这正是之前"看起来发了其实没发"的原因。
            if self._composer_empty(hwnd, grown) is True:
                raise AutomationError("提示词没有进入输入框，已停止发送。")

            send_point = None
            slot_kind = vision.SEND_SLOT_UNKNOWN
            if action == "click":
                slot, slot_kind = self._locator().send_slot(fresh, grown)
                if slot_kind == vision.SEND_SLOT_PAUSE:
                    # 这个位置此刻是暂停键，说明目标应用还在生成回答。等它画回
                    # 发送箭头再点，否则这一下会把正在生成的回答掐掉。
                    advance("等待生成结束", "warning", bump=False)
                    self.log("info", "目标应用正在生成回答，等它结束再发送。")
                    fresh, grown, slot, slot_kind = self._wait_send_ready(hwnd, grown)
                if slot_kind == vision.SEND_SLOT_PAUSE:
                    raise AutomationError(
                        "目标应用一直在生成回答，发送位置此刻是暂停键，已跳过这次发送。"
                    )
                if slot is not None:
                    send_point = slot.center
                elif not self._locator().recognition_enabled:
                    # 关掉了识别，只能退回校准锚点。
                    send_point = self._locator().send_point(fresh, grown)
                else:
                    self.log("warning", "没认出发送按钮，改用 Enter 发送。")

            self._overlay_show(fresh)
            self._overlay_box(
                fresh.to_screen(grown),
                "composer",
                f"输入框 · {grown.width}×{grown.height}",
            )
            send_region = fresh.to_screen(grown)

            advance("发送")
            screen_send = None
            if send_point is not None:
                screen_send = fresh.point_to_screen(send_point)
                half = 22
                self._overlay_box(
                    vision.Rect(
                        screen_send[0] - half,
                        screen_send[1] - half,
                        screen_send[0] + half,
                        screen_send[1] + half,
                    ),
                    "send",
                    "发送按钮" if slot_kind != vision.SEND_SLOT_UNKNOWN else "发送位置",
                )
            # 发送按钮的框线可能压在输入框上，基线必须在画完框之后再取。
            before_send = self._capture(send_region)
            # 从这里开始内容就交出去了：重试可能让同一个提示词发两遍。
            self.sent = True
            if screen_send is not None:
                winapi.click(screen_send)
            else:
                if action == "click":
                    self.log("warning", "未识别到发送按钮，改用 Enter 发送。")
                winapi.press_enter()

            verify_send = bool(self.settings.get("verify_send", True))
            if verify_send:
                advance("校验发送")
                cleared, _ = self._wait_change(send_region, before_send, 6.0)
                if not cleared:
                    self.log("warning", "输入框没有清空，补按一次 Enter。")
                    winapi.press_enter()
                    cleared, _ = self._wait_change(send_region, before_send, 4.0)
                # 像素变化只能说明"输入框动了"，点中麦克风开始录音也算变化。
                # 真正的判据是占位符回来——那才代表内容被送走了。
                if self._composer_empty(hwnd, grown) is False:
                    self.log("warning", "输入框里仍然有内容，补按一次 Enter。")
                    winapi.press_enter()
                    time.sleep(min(self._delay(), 1.2))
                if self._composer_empty(hwnd, grown) is False:
                    raise AutomationError(
                        "发送后输入框里仍有内容，消息没有发出去。"
                        "目标应用可能暂停了这个对话，或额度已经用尽。"
                    )
                if not cleared:
                    raise AutomationError("发送后输入框没有清空，消息可能没有发出去。")
            else:
                time.sleep(min(self._delay(), 1.0))

            elapsed = int((time.monotonic() - started) * 1000)
            self._overlay_progress(step_index, steps, "已发送", "success")
            self.log("success", f"已发送：{task.get('name') or '未命名任务'}")
            return RunOutcome(True, "已发送", elapsed)
        finally:
            if old_clipboard is not None:
                try:
                    winapi.set_clipboard_text(old_clipboard)
                except Exception:
                    pass
            if saved_cursor is not None:
                try:
                    winapi.move_cursor(saved_cursor)
                except Exception:
                    pass
            self._overlay_hide()

    # -- 工具箱用的公开入口 --------------------------------------------
    #
    # 下面这些方法不做完整流水线，只做"一件事"，供界面上的按钮直接调用。
    # 它们和 run_task 共用同一把窗口锁（由 Scheduler.run_exclusive 提供），
    # 所以不会和正在执行的任务抢鼠标键盘。

    def attach(self) -> tuple[dict[str, Any], int]:
        """找到（必要时拉起）目标窗口并切到前台。"""

        window = self._find_window()
        hwnd = self._activate(window)
        return window, hwnd

    def _rows_once(self, hwnd: int) -> tuple[locator.Frame, Optional[Rect], list[ocr.OcrLine]]:
        frame = self._frame(hwnd)
        composer, _ = self._locator().composer(frame)
        return frame, composer, self._list_rows(frame, composer)

    def sidebar_rows(self, hwnd: int, scrolls: int = 0) -> list[ocr.OcrLine]:
        """读侧边栏文字。scrolls > 0 时先回到顶部，再一屏一屏往下扫。

        返回的是"扫到过的所有行"的并集，每一行带着它当时那一刻的坐标——
        所以只适合用来认标题，不能拿去点。要点击请走 open_conversation，
        它会重新定位并校验。
        """

        frame, composer, rows = self._rows_once(hwnd)
        if scrolls <= 0:
            return rows

        collected: dict[str, ocr.OcrLine] = {}
        for row in rows:
            collected.setdefault(ocr.normalize(row.text), row)

        region = self._list_region(frame, composer)
        point = frame.point_to_screen(region.center)
        signature = tuple(row.text for row in rows)

        # 滚动之前先确认两件事：目标窗口在前台，而且那个点确实落在它的客户区
        # 里。滚轮是发给"鼠标底下那个窗口"的，窗口一旦被挡住，这一串滚动就会
        # 落到别人的界面上——用户看到的是自己的页面莫名其妙一直在往上翻。
        if winapi.foreground_window() != int(hwnd):
            raise AutomationError("目标窗口不在前台，已停止滚动，避免滚到别的窗口上。")
        if not (0 <= point[0] - frame.rect["left"] < frame.width
                and 0 <= point[1] - frame.rect["top"] < frame.height):
            raise AutomationError("侧边栏位置异常，已停止滚动。")

        # 先回到顶部：上一轮留下的滚动位置会让靠上的对话永远扫不到。
        for _ in range(6):
            winapi.scroll(point, -10)
            time.sleep(0.3)
            frame, composer, rows = self._rows_once(hwnd)
            for row in rows:
                collected.setdefault(ocr.normalize(row.text), row)
            current = tuple(row.text for row in rows)
            if current == signature:
                break
            signature = current

        for _ in range(max(1, scrolls)):
            winapi.scroll(point, 6)
            time.sleep(0.35)
            frame, composer, rows = self._rows_once(hwnd)
            for row in rows:
                collected.setdefault(ocr.normalize(row.text), row)
            current = tuple(row.text for row in rows)
            if current == signature:
                break
            signature = current

        return list(collected.values())

    # 判断侧边栏收没收起时看的那一竖条：图标栏大约 48~72 像素宽，展开后的
    # 侧边栏有 240 以上，所以 90~190 这一段在"收起"时是纯背景，在"展开"时
    # 一定压着标题文字。
    SIDEBAR_BAND = (90, 190)

    def sidebar_state(self, frame: locator.Frame) -> str:
        """侧边栏现在是展开还是收起：expanded / collapsed / unknown。

        以前是按"分隔线位置占窗口宽度的比例"判断，两个问题：分隔线经常检测
        不到，代码就退回一个按比例估的值（2560 宽时估成 409），于是收起状态
        被当成展开；窗口很宽时阈值也跟着水涨船高，展开的 300 像素侧边栏反而
        被判成收起。改成看那一竖条里有没有字，和窗口宽度无关。
        """

        left, right = self.SIDEBAR_BAND
        if frame.width <= right + 40:
            return "unknown"
        top = max(60, int(frame.height * 0.12))
        bottom = int(frame.height * 0.86)
        if bottom - top < 40:
            return "unknown"
        ratio = vision.ink_ratio(frame.bitmap, Rect(left, top, right, bottom), step=3)
        return "expanded" if ratio >= 0.012 else "collapsed"

    def sidebar_collapsed(self, frame: locator.Frame, composer: Optional[Rect] = None) -> bool:
        """侧边栏是不是收起了（保留给旧调用点，内部走 sidebar_state）。"""

        return self.sidebar_state(frame) == "collapsed"

    def ensure_sidebar(self, hwnd: int) -> bool:
        """保证侧边栏是展开的，展开不了就明确失败。

        收起时列表区里根本没有标题，OCR 只会从图标上读出几个乱码字符。与其
        把乱码当成对话列出来，不如先把它展开——这本来就是用户自己会做的事。

        三条路：先按 Ctrl+Shift+S（Codex 桌面版 View 菜单里的「Toggle Sidebar」），
        再按 Ctrl+B（网页版认这个），最后才去点标题栏那个方形开关。每一条都
        以"重新判断一次侧边栏状态"为准，不靠等固定时间。
        """

        frame = self._frame(hwnd)
        state = self.sidebar_state(frame)
        if state == "expanded":
            return True
        if state == "unknown":
            self.log("warning", "判断不出侧边栏是展开还是收起，按展开处理。")
            return True
        if winapi.foreground_window() != int(hwnd):
            # 点之前它必须在前台，否则这一下点到的是挡在它前面的程序。
            self.log("warning", "目标窗口不在前台，没有动侧边栏。")
            return False

        self.log("info", "侧边栏是收起的，正在展开。")
        # Ctrl+Shift+S 是 Codex 的「Toggle Sidebar」（View 菜单里写着）；
        # Ctrl+B 留给网页版。两个都试一遍再动鼠标。
        for shortcut, name in ((winapi.ctrl_shift_s, "Ctrl+Shift+S"), (winapi.ctrl_b, "Ctrl+B")):
            shortcut()
            # 展开有动画，0.6 秒未必够。给它两秒，看到就是看到了，别猜。
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                time.sleep(0.35)
                if self._sidebar_expanded(hwnd):
                    self.log("info", f"侧边栏已展开（{name}）。")
                    return True

        for point in self._sidebar_toggle_points(frame):
            winapi.click(frame.point_to_screen(point))
            time.sleep(0.5)
            if self._sidebar_expanded(hwnd):
                self.log("info", "侧边栏已展开。")
                return True
        self.log("warning", "没能展开侧边栏，请手动点一下标题栏左上角那个开关。")
        return False

    def _sidebar_expanded(self, hwnd: int) -> bool:
        try:
            return self.sidebar_state(self._frame(hwnd)) == "expanded"
        except AutomationError:
            return False

    # 标题栏里侧边栏开关的兜底坐标（150% 缩放、全宽窗口下量出来的）。
    # 正常情况下用 _sidebar_toggle_points 现场找，这里只是找不到时的最后手段。
    SIDEBAR_TOGGLE_HINT = (131, 27)

    def _sidebar_toggle_points(self, frame: locator.Frame) -> list[tuple[int, int]]:
        """标题栏里那几个图标的位置，从左往右，第一个候选就是侧边栏开关。

        标题栏固定是：返回、前进、侧边栏开关，然后是 File/Edit/View 菜单。
        所以数到第三个图标就是它。不写死坐标是因为窗口缩放比例不同时像素
        位置会跟着变；万一图标没找到，退回量好的坐标。
        """

        top = 4
        bottom = max(24, int(frame.height * 0.045))
        if bottom - top < 10:
            return [self.SIDEBAR_TOGGLE_HINT]
        width = min(frame.width, 420)
        inked = []
        for x in range(0, width):
            ratio = vision.ink_ratio(frame.bitmap, Rect(x, top, x + 1, bottom), step=1)
            inked.append(ratio >= 0.06)

        groups: list[tuple[int, int]] = []
        start = None
        for x, filled in enumerate(inked):
            if filled and start is None:
                start = x
            elif not filled and start is not None:
                groups.append((start, x))
                start = None
        if start is not None:
            groups.append((start, len(inked)))
        # 太窄的是噪点，太宽的是菜单文字（File/Edit/View 一整个连成一片）。
        icons = [item for item in groups if 8 <= item[1] - item[0] <= 44]
        points = [((left + right) // 2, (top + bottom) // 2) for left, right in icons[:3]]
        if len(points) >= 3:
            return [points[2]]
        return [self.SIDEBAR_TOGGLE_HINT] + points

    def scan_conversations(self, hwnd: int, scrolls: int = 30) -> list[conversations.Conversation]:
        """把侧边栏里的对话标题读出来。

        默认往下翻 30 屏，但真正停下来的是"列表不再变化"——历史长的账号
        固定翻几屏根本到不了底，能不能扫全不该取决于这个数字。
        """

        if not self.ensure_sidebar(hwnd):
            raise AutomationError(
                "侧边栏是收起的，扫描不到对话标题。点一下标题栏左上角的侧边栏开关再试。"
            )
        return conversations.parse(self.sidebar_rows(hwnd, scrolls))

    def open_conversation(self, hwnd: int, title: str) -> None:
        """按标题切过去。找不到会按 strict_conversation 决定报错还是照发。"""

        self._switch_conversation(hwnd, title)

    def new_conversation(self, hwnd: int) -> bool:
        """点侧边栏里的「New chat」。

        不走快捷键：不同版本的新建快捷键不一样，按错可能开出新窗口或弹菜单。
        点那一行至少是用户平时自己也在做的事。
        """

        frame, composer, rows = self._rows_once(hwnd)
        target = None
        for row in rows:
            stripped, _ = conversations.strip_icon(row.text)
            key = ocr.normalize(stripped)
            if key.startswith(("newchat", "新对话", "新建对话")):
                target = row
                break
        if target is None:
            self.log("warning", "侧边栏里没有找到「New chat」。")
            return False
        winapi.click(frame.point_to_screen(target.center))
        time.sleep(min(self._delay(), 1.2))
        return True

    def stop_generation(self, hwnd: int) -> bool:
        """停止生成。只在那个位置真的是暂停键时才点——空闲时它是发送箭头，
        点下去会把输入框里没写完的内容发出去。"""

        frame = self._frame(hwnd)
        composer, _ = self._locator().composer(frame)
        if composer is None:
            self.log("warning", "没认出输入框，无法定位暂停键。")
            return False
        slot, kind = self._locator().send_slot(frame, composer)
        if slot is None or kind != vision.SEND_SLOT_PAUSE:
            self.log("info", "目标应用此刻没有在生成回答。")
            return False
        winapi.click(frame.point_to_screen(slot.center))
        self.log("success", "已请求停止生成。")
        return True

    def clear_input(self, hwnd: int) -> bool:
        """清空输入框。空的时候不动手，免得在占位符上乱按。"""

        frame = self._frame(hwnd)
        composer, _ = self._locator().composer(frame)
        if composer is None:
            self.log("warning", "没认出输入框。")
            return False
        try:
            if self._locator().composer_is_empty(frame, composer) is True:
                return True
        except Exception:
            pass
        winapi.click(frame.point_to_screen(composer.center))
        time.sleep(0.15)
        winapi.select_all_and_delete()
        time.sleep(0.2)
        return True

    def paste_only(self, content: str) -> RunOutcome:
        """只粘贴不发送，用于验证识别与输入框定位是否正常。"""

        started = time.monotonic()
        if not content.strip():
            raise AutomationError("提示词内容为空。")

        old_clipboard = winapi.get_clipboard_text()
        try:
            saved_cursor = None
            try:
                saved_cursor = winapi.get_cursor_pos()
            except Exception:
                saved_cursor = None

            window = self._find_window()
            hwnd = self._activate(window)
            time.sleep(min(self._delay(), 0.6))
            frame, composer, source = self._resolve_composer(hwnd)
            locator.remember_composer(self.settings, frame, composer)

            screen_composer = frame.to_screen(composer)
            baseline = self._capture(screen_composer)
            content = templates.expand(content, clipboard=old_clipboard or "")
            winapi.set_clipboard_text(content)
            self._paste(hwnd, frame, composer)
            changed, _ = self._wait_change(screen_composer, baseline, 4.0)
            if not changed:
                raise AutomationError("粘贴后输入框没有变化，请检查识别结果。")

            elapsed = int((time.monotonic() - started) * 1000)
            self.log("success", f"已粘贴到输入框（定位方式：{source}），未发送。")
            return RunOutcome(True, "已粘贴", elapsed)
        finally:
            if old_clipboard is not None:
                try:
                    winapi.set_clipboard_text(old_clipboard)
                except Exception:
                    pass
            if saved_cursor is not None:
                try:
                    winapi.move_cursor(saved_cursor)
                except Exception:
                    pass
