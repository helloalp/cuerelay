# -*- coding: utf-8 -*-
"""键盘上的两件事。

- ``VirtualCapture``：F8 虚拟捕获，只记录鼠标位置，不向目标窗口发送任何点击。
- ``GlobalHotkey``：进程外的全局热键，在任何程序里都能把 CueRelay 叫回来。

两个都用 Win32 的键盘状态，都不需要额外依赖。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

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


# ----------------------------------------------------------------------
# 全局热键
# ----------------------------------------------------------------------

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# 进程内唯一即可。热键是按线程注册的，用一个不常见的数字避免和别处撞上。
HOTKEY_ID = 0x0C0E

# Ctrl+Alt+K：和界面里的 Ctrl+K 是同一个动作，多一个 Alt 就不会和任何编辑器抢。
DEFAULT_MODS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
DEFAULT_VK = winapi.VK_K
DEFAULT_LABEL = "Ctrl+Alt+K"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
# GetMessage 出错时返回 -1，所以这里必须是 c_int 而不是 BOOL。
_user32.GetMessageW.restype = ctypes.c_int
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class GlobalHotkey:
    """一个组合键，按下时调用 callback。

    回调发生在热键线程里，不能直接碰 tkinter —— 调用方应该只往队列里丢一个
    事件，让界面线程去处理。
    """

    def __init__(
        self,
        callback: Callable[[], None],
        mods: int = DEFAULT_MODS,
        vk: int = DEFAULT_VK,
    ) -> None:
        self._callback = callback
        self._mods = mods
        self._vk = vk
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready = threading.Event()
        self._registered = False
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> tuple[bool, str]:
        """注册热键，返回 (是否成功, 失败原因)。

        注册结果是同步等回来的：开关显示"已启用"、按下去却毫无反应，那种状态
        比直接报错更难查。
        """

        if self._active:
            return True, ""
        if not winapi.IS_WINDOWS:
            return False, "全局热键只支持 Windows"
        self._ready.clear()
        self._registered = False
        self._thread = threading.Thread(target=self._loop, name="cuerelay-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(2.0):
            return False, "热键线程没有响应"
        if not self._registered:
            return False, f"{DEFAULT_LABEL} 已被其它程序占用"
        self._active = True
        return True, ""

    def stop(self) -> None:
        thread_id = self._thread_id
        self._active = False
        if thread_id:
            # 消息循环卡在 GetMessage 里，只能靠投一条 WM_QUIT 把它叫醒。
            _user32.PostThreadMessageW(wintypes.DWORD(thread_id), WM_QUIT, 0, 0)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(1.5)
        self._thread = None
        self._thread_id = None

    def _loop(self) -> None:
        self._thread_id = int(_kernel32.GetCurrentThreadId())
        ok = bool(_user32.RegisterHotKey(None, HOTKEY_ID, self._mods, self._vk))
        self._registered = ok
        self._ready.set()
        if not ok:
            return
        try:
            msg = wintypes.MSG()
            while True:
                result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                if msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                    try:
                        self._callback()
                    except Exception:
                        # 回调只是往队列里丢一个事件。真出问题也不该让热键线程
                        # 退出——退出之后这个键就永远失效了。
                        pass
        finally:
            _user32.UnregisterHotKey(None, HOTKEY_ID)
            self._active = False
