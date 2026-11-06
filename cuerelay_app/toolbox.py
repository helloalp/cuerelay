# -*- coding: utf-8 -*-
"""Codex 工具箱：把「对当前窗口做一件事」封装成可以直接绑到按钮上的动作。

和任务流水线的区别在于关注点：任务关心计划、状态、重试；这里只关心
「现在立刻做一次」。两者共用同一个 `Runner`，所以识别、校验和浮层的
表现完全一致，不会出现「按钮做的事」和「定时任务做的事」两套逻辑。

这个模块不碰线程，也不碰窗口锁——调用方（调度器的 run_exclusive）负责
保证同一时刻只有一个动作在驱动鼠标键盘。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import automation, conversations
from .automation import AutomationError, RunOutcome


class ToolboxError(RuntimeError):
    pass


class Toolbox:
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

    # -- 内部 ----------------------------------------------------------

    def _runner(self) -> automation.Runner:
        return automation.Runner(self.settings, self.log, self.exclude_hwnd, self.emit)

    def _window(self, runner: automation.Runner) -> tuple[dict[str, Any], int]:
        try:
            return runner.attach()
        except AutomationError as exc:
            raise ToolboxError(str(exc)) from exc

    # -- 只读 ----------------------------------------------------------

    def conversations(self, scrolls: int = 6) -> list[conversations.Conversation]:
        """扫描侧边栏里的对话标题。不点击、不输入。"""

        runner = self._runner()
        _, hwnd = self._window(runner)
        try:
            return runner.scan_conversations(hwnd, scrolls)
        except AutomationError as exc:
            raise ToolboxError(str(exc)) from exc

    def current_window(self) -> dict[str, Any]:
        runner = self._runner()
        window, _ = self._window(runner)
        return window

    # -- 动作 ----------------------------------------------------------

    def open_conversation(self, title: str) -> str:
        title = str(title or "").strip()
        if not title:
            raise ToolboxError("对话标题是空的。")
        runner = self._runner()
        _, hwnd = self._window(runner)
        try:
            runner.open_conversation(hwnd, title)
        except AutomationError as exc:
            raise ToolboxError(str(exc)) from exc
        return f"已切换到：{title}"

    def new_conversation(self) -> str:
        runner = self._runner()
        _, hwnd = self._window(runner)
        if not runner.new_conversation(hwnd):
            raise ToolboxError("没有在侧边栏里找到「New chat」。")
        return "已新建对话"

    def stop_generation(self) -> str:
        runner = self._runner()
        _, hwnd = self._window(runner)
        if not runner.stop_generation(hwnd):
            raise ToolboxError("目标应用此刻没有在生成回答。")
        return "已请求停止生成"

    def clear_input(self) -> str:
        runner = self._runner()
        _, hwnd = self._window(runner)
        if not runner.clear_input(hwnd):
            raise ToolboxError("没认出输入框，无法清空。")
        return "已清空输入框"

    def send(
        self,
        text: str,
        conversation: str = "",
        action: str = "click",
        name: str = "快速发送",
    ) -> RunOutcome:
        """立刻把一段文字发出去。走的是和定时任务完全相同的流水线。"""

        content = str(text or "")
        if not content.strip():
            raise ToolboxError("内容是空的。")
        task = {
            "name": name,
            "kind": "prompt",
            "content": content,
            "conversation_title": str(conversation or "").strip(),
            "action": action or "click",
        }
        runner = self._runner()
        try:
            return runner.run_task(task)
        except AutomationError as exc:
            raise ToolboxError(str(exc)) from exc
