# -*- coding: utf-8 -*-
"""执行流水线。

与 1.x 的关键区别：不再"点一下再睡一会儿然后祈祷成功"，而是每一步都验证。
粘贴后确认输入框像素真的变了，发送后确认输入框真的清空了；任一步没有
发生变化就重试一次，仍然失败则明确报错，而不是静默失败。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import imaging, locator, ocr, vision, winapi
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

    def _find_window(self) -> dict[str, Any]:
        keywords = winapi.parse_keywords(str(self.settings.get("target_keywords") or ""))
        window = winapi.find_target_window(
            keywords,
            exclude_hwnd=self.exclude_hwnd,
            title_hint=str(self.settings.get("target_title") or ""),
            process_hint=str(self.settings.get("target_process") or ""),
        )
        if not window:
            raise AutomationError("没有找到目标窗口。请先打开目标应用，或在设置中调整关键词。")
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
            raise AutomationError("无法定位输入框。请在设置中重新识别目标。")
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

    def _composer_empty(self, hwnd: int) -> Optional[bool]:
        """输入框是否已经回到空状态。识别不了时返回 None。"""

        try:
            frame = self._frame(hwnd)
        except AutomationError:
            return None
        try:
            return self._locator().composer_is_empty(frame)
        except Exception:
            return None

    def _locate_row(
        self,
        hwnd: int,
        composer: Optional[Rect],
        title: str,
        scrolls: int = 8,
    ) -> tuple[locator.Frame, Optional[ocr.OcrLine]]:
        """在侧边栏列表里找到标题对应的那一行，必要时滚动列表。

        列表只会往下滚，所以先回到顶部再一屏一屏往下找，
        否则上一轮留下的滚动位置会让靠上的对话永远找不到。
        """

        frame = self._frame(hwnd)
        rows = self._list_rows(frame, composer)
        row = ocr.find_line(rows, title, 0.9, strict=True)
        if row is not None:
            return frame, row

        region = self._list_region(frame, composer)
        point = frame.point_to_screen(region.center)
        signature = tuple(item.text for item in rows)

        # 回到顶部：连续上滚，直到列表内容不再变化
        for _ in range(6):
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

        # 逐屏向下找
        for _ in range(max(1, scrolls)):
            winapi.scroll(point, 6)
            time.sleep(0.4)
            frame = self._frame(hwnd)
            rows = self._list_rows(frame, composer)
            row = ocr.find_line(rows, title, 0.9, strict=True)
            if row is not None:
                return frame, row
            current = tuple(item.text for item in rows)
            if current == signature:
                break
            signature = current
            self.log("info", f"列表里没有「{title}」，往下翻一屏继续找。")
        return frame, None

    def _open_search(self, hwnd: int, frame: locator.Frame, composer: Optional[Rect]) -> bool:
        """尝试唤出搜索框。先按快捷键，再点记录下来的放大镜位置。

        判定标准是"侧边栏内容大范围变了"：点中某一行的效果只是那一行高亮，
        面积远小于整个列表被搜索结果替换，两者不会混淆。
        """

        region = self._list_region(frame, composer)
        screen_region = frame.to_screen(region)
        baseline = self._stable_baseline(screen_region)
        if baseline is None:
            return False

        winapi.ctrl_k()
        if self._wait_change(screen_region, baseline, 1.4, min_ratio=0.08)[0]:
            self.log("info", "搜索框已打开（Ctrl+K）")
            return True

        point = locator.anchor_point(
            self.settings.get("anchor_search"),
            frame.rect,
            winapi.get_window_rect(hwnd),
        )
        if point is None:
            return False
        winapi.click(point)
        if self._wait_change(screen_region, baseline, 1.8, min_ratio=0.08)[0]:
            self.log("info", "搜索框已打开（放大镜）")
            return True
        return False

    def _switch_conversation(self, hwnd: int, title: str) -> None:
        strict = bool(self.settings.get("strict_conversation", True))
        frame = self._frame(hwnd)
        composer, _ = self._locator().composer(frame)
        self.log("info", f"切换对话：{title}")

        # 首选路径：侧边栏列表里本来就有这个对话，直接点它。
        # 这条路不依赖搜索框，也不用关心应用当前停在哪个对话上。
        frame, row = self._locate_row(hwnd, composer, title)
        if row is not None:
            self._overlay_box(
                frame.to_screen(Rect(row.left, row.top, row.right, row.bottom)),
                "search",
                f"对话 · {row.text}",
            )
            winapi.click(frame.point_to_screen(row.center))
            time.sleep(min(self._delay(), 1.2))
            if self._verify_conversation(hwnd, title, 6.0):
                self.log("info", f"已切换到：{title}")
                return
            self.log("warning", f"点了「{row.text}」，但标题栏没有变成「{title}」。")

        # 兜底路径：列表里没有（可能折在项目分组里），改用搜索框。
        if self._open_search(hwnd, frame, composer):
            region = frame.to_screen(self._list_region(frame, composer))
            before_typing = self._capture(region)
            winapi.ctrl_a()
            winapi.set_clipboard_text(title)
            winapi.ctrl_v()
            # 只有确认文字真的进了搜索框，才敢按回车；否则回车可能把别处的
            # 内容发出去，宁可不切也不发错对话。
            typed = before_typing is not None and self._wait_change(
                region, before_typing, 2.0, min_ratio=0.004
            )[0]
            if typed:
                time.sleep(min(self._delay(), 1.0))
                winapi.press_enter()
                time.sleep(min(self._delay(), 1.5))
                if winapi.foreground_window() != hwnd:
                    raise AutomationError("切换对话后目标窗口失去焦点。")
                if self._verify_conversation(hwnd, title, 5.0):
                    self.log("info", f"已切换到：{title}")
                    return
            else:
                self.log("warning", "搜索框没有接收输入，已放弃这次切换。")

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

    # -- 主流程 --------------------------------------------------------

    def run_task(self, task: dict[str, Any]) -> RunOutcome:
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

        def advance(text: str, tone: str = "accent") -> None:
            nonlocal step_index
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
                self._switch_conversation(hwnd, conversation)

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
            self._overlay_show(fresh)
            self._overlay_box(
                fresh.to_screen(grown),
                "composer",
                f"输入框 · {grown.width}×{grown.height}",
            )
            send_region = fresh.to_screen(grown)

            send_point = None
            if action == "click":
                send_point = self._locator().send_point(fresh, grown)
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
                    "发送按钮",
                )
            # 发送按钮的框线可能压在输入框上，基线必须在画完框之后再取。
            before_send = self._capture(send_region)
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
                if self._composer_empty(hwnd) is False:
                    self.log("warning", "输入框里仍然有内容，补按一次 Enter。")
                    winapi.press_enter()
                    time.sleep(min(self._delay(), 1.2))
                if self._composer_empty(hwnd) is False:
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
