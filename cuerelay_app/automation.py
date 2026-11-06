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

from . import imaging, locator, vision, winapi
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
    ) -> tuple[bool, Any]:
        deadline = time.monotonic() + max(0.2, timeout)
        latest = baseline
        while time.monotonic() < deadline:
            current = self._capture(rect)
            if current is None:
                time.sleep(0.12)
                continue
            latest = current
            if vision.looks_changed(baseline, current, threshold):
                return True, current
            time.sleep(0.12)
        return False, latest

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

    def _switch_conversation(self, hwnd: int, title: str) -> None:
        frame = self._frame(hwnd)
        composer, _ = self._locator().composer(frame)
        point = self._locator().search_point(frame, composer)
        if point is None:
            if self.settings.get("strict_conversation", True):
                raise AutomationError("无法定位左侧搜索框，已停止。可在设置中关闭“对话切换失败时中止”。")
            self.log("warning", "无法定位左侧搜索框，改为在当前对话发送。")
            return

        # 切换前先记住右侧消息区的样子：切没切过去，看它变没变。
        # 搜索不到对话时右侧不会有任何变化，据此可以拦住"发错对话"。
        region = None
        baseline = None
        if composer is not None:
            region = vision.Rect(composer.left, 0, frame.width, max(1, composer.top))
            baseline = self._capture(frame.to_screen(region))

        self.log("info", f"切换对话：{title}")
        winapi.click(frame.point_to_screen(point))
        time.sleep(self._delay())
        winapi.ctrl_a()
        winapi.set_clipboard_text(title)
        winapi.ctrl_v()
        time.sleep(self._delay())
        winapi.press_enter()
        time.sleep(self._delay())
        if winapi.foreground_window() != hwnd:
            raise AutomationError("切换对话后目标窗口失去焦点。")

        if region is None or baseline is None:
            return
        switched, _ = self._wait_change(frame.to_screen(region), baseline, 6.0)
        if switched:
            return
        # 目标对话本来就是当前对话时，消息区不会变化，所以这里只提示不中止，
        # 避免把"本来就在这个对话里"的正常任务拦下来。
        self.log(
            "warning",
            f"没有检测到对话切换：{title}。如果该对话名不存在，消息会发到当前对话。",
        )

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
