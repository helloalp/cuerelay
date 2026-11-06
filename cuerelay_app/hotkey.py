# -*- coding: utf-8 -*-
"""F8 虚拟捕获：只记录鼠标位置，不向目标窗口发送任何点击。"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import winapi


class VirtualCapture:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.point: Optional[tuple[int, int]] = None
        self.error: Optional[str] = None
        self._done = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not winapi.IS_WINDOWS:
            self.error = "虚拟捕获只支持 Windows。"
            self._done.set()
            return
        self._thread = threading.Thread(target=self._run, name="cuerelay-capture", daemon=True)
        self._thread.start()

    def wait(self) -> Optional[tuple[int, int]]:
        self._done.wait(self.timeout_seconds + 1.0)
        if self._thread is not None:
            self._thread.join(0.5)
        return self.point

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        was_down = False
        while not self._stop.is_set() and time.monotonic() < deadline:
            is_down = bool(winapi.user32.GetAsyncKeyState(winapi.VK_F8) & 0x8000)
            if is_down and not was_down:
                try:
                    self.point = winapi.get_cursor_pos()
                except Exception as exc:
                    self.error = str(exc) or exc.__class__.__name__
                self._done.set()
                return
            was_down = is_down
            time.sleep(0.025)
        if self.point is None and self.error is None:
            self.error = "15 秒内没有检测到 F8；笔记本可能需要按 Fn+F8。"
        self._done.set()
