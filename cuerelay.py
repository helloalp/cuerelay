#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CueRelay：Windows 定时提示词与屏幕操作助手。

一个不依赖第三方 Python 包的 Windows 桌面工具：

* 绝对置顶（可临时关闭）；
* 保存多个提示词任务；
* 单次、每天、按间隔定时；
* 通过剪贴板粘贴到目标 AI 应用输入框，再点击发送按钮或按 Enter；
* 目标位置使用相对窗口坐标保存，目标窗口移动或缩放后仍然可用；
* 使用 Windows 原生 API，不需要 pyautogui、pywin32 等额外依赖。

安全设计：执行前必须完成输入框位置校准；找不到目标窗口、目标窗口未激活、
或缺少发送按钮位置时会停止本次动作，不会盲目向其它窗口输入。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import datetime as _dt
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk


APP_NAME = "CueRelay"
APP_VERSION = "1.2.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "cuerelay_data.json"
LOG_PATH = BASE_DIR / "cuerelay.log"
CRASH_PATH = BASE_DIR / "cuerelay_crash.log"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def now_local() -> _dt.datetime:
    """返回本机当前时间，任务文件使用无时区的本地 ISO 时间。"""

    return _dt.datetime.now().replace(microsecond=0)


def iso_datetime(value: _dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat(timespec="seconds")


def parse_datetime(value: Any) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def format_datetime(value: Any) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "未设置"
    return parsed.strftime("%Y-%m-%d %H:%M")


def friendly_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


# ---------------------------------------------------------------------------
# Windows API 封装
# ---------------------------------------------------------------------------


if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    HWND = wintypes.HWND
    DWORD = wintypes.DWORD
    LPARAM = wintypes.LPARAM
    WPARAM = wintypes.WPARAM
    LRESULT = ctypes.c_ssize_t

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", HWND),
            ("message", wintypes.UINT),
            ("wParam", WPARAM),
            ("lParam", LPARAM),
            ("time", DWORD),
            ("pt", POINT),
        ]

    class MOUSELLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", POINT),
            ("mouseData", DWORD),
            ("flags", DWORD),
            ("time", DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", DWORD),
            ("scanCode", DWORD),
            ("flags", DWORD),
            ("time", DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    user32.EnumWindows.argtypes = [ctypes.c_void_p, LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = HWND
    user32.GetAncestor.argtypes = [HWND, wintypes.UINT]
    user32.GetAncestor.restype = HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = HWND
    user32.IsIconic.argtypes = [HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [DWORD, DWORD, DWORD, DWORD, ctypes.c_void_p]
    user32.mouse_event.restype = None
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, DWORD, ctypes.c_void_p]
    user32.keybd_event.restype = None
    user32.OpenClipboard.argtypes = [HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = LPARAM
    user32.PostThreadMessageW.argtypes = [DWORD, wintypes.UINT, WPARAM, LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, WPARAM, LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
    user32.GetWindowThreadProcessId.restype = DWORD
    user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [DWORD, wintypes.BOOL, DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = DWORD


class WindowsError(RuntimeError):
    pass


class Win32:
    """只保留本程序需要的少量 Windows 原生能力。"""

    SW_RESTORE = 9
    SW_MAXIMIZE = 3
    GA_ROOT = 2
    VK_CONTROL = 0x11
    VK_A = 0x41
    VK_V = 0x56
    VK_RETURN = 0x0D
    VK_ESCAPE = 0x1B
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    ERROR_ALREADY_EXISTS = 183
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WH_MOUSE_LL = 14
    WH_KEYBOARD_LL = 13
    WM_LBUTTONDOWN = 0x0201
    WM_KEYDOWN = 0x0100
    WM_QUIT = 0x0012
    PM_NOREMOVE = 0x0000
    VK_F8 = 0x77
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    @staticmethod
    def require_windows() -> None:
        if sys.platform != "win32":
            raise WindowsError("此工具只支持 Windows。")

    @staticmethod
    def set_dpi_awareness() -> None:
        if sys.platform != "win32":
            return
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

    @staticmethod
    def get_window_title(hwnd: int) -> str:
        if not hwnd or sys.platform != "win32":
            return ""
        length = user32.GetWindowTextLengthW(HWND(hwnd))
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(HWND(hwnd), buffer, len(buffer))
        return buffer.value.strip()

    @staticmethod
    def get_process_path(hwnd: int) -> str:
        if not hwnd or sys.platform != "win32":
            return ""
        pid = DWORD(0)
        user32.GetWindowThreadProcessId(HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(Win32.PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
        finally:
            kernel32.CloseHandle(handle)
        return ""

    @staticmethod
    def window_info(hwnd: int) -> dict[str, Any]:
        title = Win32.get_window_title(hwnd)
        path = Win32.get_process_path(hwnd)
        return {
            "hwnd": int(hwnd or 0),
            "title": title,
            "process_path": path,
            "process_name": Path(path).name if path else "",
        }

    @staticmethod
    def enumerate_windows() -> list[dict[str, Any]]:
        Win32.require_windows()
        windows: list[dict[str, Any]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, LPARAM)

        @callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            if user32.IsWindowVisible(hwnd):
                info = Win32.window_info(hwnd)
                process_name = info["process_name"].lower()
                if info["title"] or "codex" in process_name or "chatgpt" in process_name:
                    windows.append(info)
            return True

        user32.EnumWindows(callback, 0)
        return windows

    @staticmethod
    def find_codex_window(
        exclude_hwnd: int = 0,
        title_hint: str = "",
        process_hint: str = "",
    ) -> Optional[dict[str, Any]]:
        """按标题、进程名和当前前台窗口评分，找到最可能的 Codex 窗口。"""

        Win32.require_windows()
        active = int(user32.GetForegroundWindow() or 0)
        title_hint_lower = (title_hint or "").strip().lower()
        process_hint_lower = (process_hint or "").strip().lower()
        candidates: list[tuple[int, dict[str, Any]]] = []

        for info in Win32.enumerate_windows():
            hwnd = info["hwnd"]
            if not hwnd or hwnd == exclude_hwnd:
                continue
            title = info["title"].lower()
            process = info["process_name"].lower()
            if "定时输入助手" in title or APP_NAME.lower() in title:
                continue

            score = 0
            if "codex" in title:
                score += 120
            if "codex" in process:
                score += 120
            if "chatgpt" in title:
                score += 90
            if "chatgpt" in process:
                score += 90
            if "openai" in title or "openai" in process:
                score += 45
            if title_hint_lower and title_hint_lower in title:
                score += 65
            if process_hint_lower and process_hint_lower in process:
                score += 65
            if hwnd == active:
                score += 35

            # 若没有任何线索，不把任意前台窗口当作 Codex，避免误输入。
            if score > 0:
                candidates.append((score, info))

        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def get_window_rect(hwnd: int) -> Optional[dict[str, int]]:
        if not hwnd or sys.platform != "win32":
            return None
        rect = RECT()
        if not user32.GetWindowRect(HWND(hwnd), ctypes.byref(rect)):
            return None
        return {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
            "width": max(1, int(rect.right - rect.left)),
            "height": max(1, int(rect.bottom - rect.top)),
        }

    @staticmethod
    def point_to_root_window(x: int, y: int) -> int:
        point = POINT(int(x), int(y))
        child = int(user32.WindowFromPoint(point) or 0)
        if not child:
            return 0
        return int(user32.GetAncestor(HWND(child), Win32.GA_ROOT) or child)

    @staticmethod
    def get_cursor_pos() -> tuple[int, int]:
        """读取当前鼠标屏幕坐标；供 F8 虚拟捕获使用。"""

        if sys.platform != "win32":
            raise OSError("鼠标位置读取只支持 Windows。")
        point = POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError()
        return int(point.x), int(point.y)

    @staticmethod
    def capture_window_geometry(hwnd: int) -> Optional[dict[str, Any]]:
        rect = Win32.get_window_rect(hwnd)
        if rect is None:
            return None
        return {
            "left": rect["left"],
            "top": rect["top"],
            "width": rect["width"],
            "height": rect["height"],
            "maximized": bool(user32.IsZoomed(HWND(hwnd))),
            "minimized": bool(user32.IsIconic(HWND(hwnd))),
            "captured_at": iso_datetime(now_local()),
        }

    @staticmethod
    def restore_window_geometry(hwnd: int, geometry: Optional[dict[str, Any]]) -> None:
        """恢复校准时的窗口状态；恢复失败时由调用方继续使用当前窗口。"""

        if not geometry or not hwnd or not user32.IsWindow(HWND(hwnd)):
            return
        try:
            maximized = bool(geometry.get("maximized", False))
            if user32.IsIconic(HWND(hwnd)):
                user32.ShowWindow(HWND(hwnd), Win32.SW_RESTORE)
            if maximized:
                user32.ShowWindow(HWND(hwnd), Win32.SW_MAXIMIZE)
                return

            # 校准时是普通窗口，而当前可能已最大化；这里的还原是有意的。
            if user32.IsZoomed(HWND(hwnd)):
                user32.ShowWindow(HWND(hwnd), Win32.SW_RESTORE)
            left = int(geometry["left"])
            top = int(geometry["top"])
            width = max(320, int(geometry["width"]))
            height = max(240, int(geometry["height"]))
            user32.SetWindowPos(
                HWND(hwnd),
                HWND(0),
                left,
                top,
                width,
                height,
                Win32.SWP_NOZORDER | Win32.SWP_NOACTIVATE | Win32.SWP_SHOWWINDOW,
            )
        except (KeyError, TypeError, ValueError):
            return

    @staticmethod
    def anchor_from_screen_point(point: tuple[int, int], hwnd: int, edge_mode: bool = False) -> Optional[dict[str, Any]]:
        rect = Win32.get_window_rect(hwnd)
        if rect is None:
            return None
        x, y = point
        anchor: dict[str, Any] = {
            "rx": round((x - rect["left"]) / rect["width"], 6),
            "ry": round((y - rect["top"]) / rect["height"], 6),
            "screen_x": int(x),
            "screen_y": int(y),
            "window_rect": dict(rect),
            "title": Win32.get_window_title(hwnd),
            "process_name": Win32.window_info(hwnd)["process_name"],
            "captured_at": iso_datetime(now_local()),
        }
        if edge_mode:
            anchor["edge_mode"] = "bottom-right"
            anchor["right_offset"] = int(rect["right"] - x)
            anchor["bottom_offset"] = int(rect["bottom"] - y)
        return anchor

    @staticmethod
    def anchor_to_screen_point(anchor: dict[str, Any], hwnd: int) -> Optional[tuple[int, int]]:
        rect = Win32.get_window_rect(hwnd)
        if rect is None:
            return None
        saved_rect = anchor.get("window_rect")
        try:
            # 恢复到捕获时窗口大小后，优先使用物理屏幕坐标，避免响应式布局
            # 在相同尺寸下仍因为四舍五入而偏移。
            if isinstance(saved_rect, dict) and "screen_x" in anchor and "screen_y" in anchor:
                same_geometry = all(
                    abs(int(rect[key]) - int(saved_rect[key])) <= 3
                    for key in ("left", "top", "width", "height")
                )
                if same_geometry:
                    return int(anchor["screen_x"]), int(anchor["screen_y"])
        except (KeyError, TypeError, ValueError):
            pass
        if anchor.get("edge_mode") == "bottom-right":
            try:
                right_offset = int(anchor["right_offset"])
                bottom_offset = int(anchor["bottom_offset"])
                return rect["right"] - right_offset, rect["bottom"] - bottom_offset
            except (KeyError, TypeError, ValueError):
                pass
        try:
            rx = float(anchor["rx"])
            ry = float(anchor["ry"])
        except (KeyError, TypeError, ValueError):
            return None
        # 防止损坏的配置把鼠标移动到屏幕外。
        rx = min(1.0, max(0.0, rx))
        ry = min(1.0, max(0.0, ry))
        return (
            rect["left"] + round(rect["width"] * rx),
            rect["top"] + round(rect["height"] * ry),
        )

    @staticmethod
    def activate_window(hwnd: int, geometry: Optional[dict[str, Any]] = None) -> bool:
        if not hwnd or not user32.IsWindow(HWND(hwnd)):
            return False
        # 有校准几何参数时按校准时状态恢复；否则只恢复最小化窗口，绝不把
        # 最大化窗口无意中恢复成普通大小。
        if geometry:
            Win32.restore_window_geometry(hwnd, geometry)
        else:
            was_maximized = bool(user32.IsZoomed(HWND(hwnd)))
            if user32.IsIconic(HWND(hwnd)):
                user32.ShowWindow(HWND(hwnd), Win32.SW_RESTORE)

        # 模拟一次 Alt 可解除部分 Windows 前台切换限制。
        user32.keybd_event(Win32.VK_MENU, 0, 0, 0)
        user32.keybd_event(Win32.VK_MENU, 0, Win32.KEYEVENTF_KEYUP, 0)
        user32.BringWindowToTop(HWND(hwnd))
        user32.SetForegroundWindow(HWND(hwnd))
        time.sleep(0.18)
        # 某些窗口管理器在激活过程中会改变显示状态，最大化窗口在此处兜底恢复。
        if geometry and bool(geometry.get("maximized", False)) and not user32.IsZoomed(HWND(hwnd)):
            user32.ShowWindow(HWND(hwnd), Win32.SW_MAXIMIZE)
            time.sleep(0.12)
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)

    @staticmethod
    def click(point: tuple[int, int]) -> None:
        x, y = point
        if not user32.SetCursorPos(int(x), int(y)):
            raise WindowsError("无法移动鼠标到目标位置。")
        time.sleep(0.06)
        user32.mouse_event(Win32.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
        user32.mouse_event(Win32.MOUSEEVENTF_LEFTUP, 0, 0, 0, None)

    @staticmethod
    def _key_down(vk: int) -> None:
        user32.keybd_event(vk, 0, 0, 0)

    @staticmethod
    def _key_up(vk: int) -> None:
        user32.keybd_event(vk, 0, Win32.KEYEVENTF_KEYUP, 0)

    @staticmethod
    def ctrl_v() -> None:
        Win32._key_down(Win32.VK_CONTROL)
        Win32._key_down(Win32.VK_V)
        Win32._key_up(Win32.VK_V)
        Win32._key_up(Win32.VK_CONTROL)

    @staticmethod
    def ctrl_a() -> None:
        Win32._key_down(Win32.VK_CONTROL)
        Win32._key_down(Win32.VK_A)
        Win32._key_up(Win32.VK_A)
        Win32._key_up(Win32.VK_CONTROL)

    @staticmethod
    def press_enter() -> None:
        Win32._key_down(Win32.VK_RETURN)
        Win32._key_up(Win32.VK_RETURN)

    @staticmethod
    def press_escape() -> None:
        Win32._key_down(Win32.VK_ESCAPE)
        Win32._key_up(Win32.VK_ESCAPE)

    @staticmethod
    def get_clipboard_text() -> Optional[str]:
        if sys.platform != "win32":
            return None
        for _ in range(10):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.04)
        else:
            return None

        try:
            handle = user32.GetClipboardData(Win32.CF_UNICODETEXT)
            if not handle:
                return None
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    @staticmethod
    def set_clipboard_text(text: str) -> None:
        if sys.platform != "win32":
            raise WindowsError("剪贴板功能只支持 Windows。")
        for _ in range(12):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            raise WindowsError("剪贴板正被其它程序占用，请稍后重试。")

        handle = None
        try:
            if not user32.EmptyClipboard():
                raise WindowsError("无法清空 Windows 剪贴板。")
            encoded = ctypes.create_unicode_buffer(text)
            handle = kernel32.GlobalAlloc(Win32.GMEM_MOVEABLE, ctypes.sizeof(encoded))
            if not handle:
                raise WindowsError("无法分配剪贴板内存。")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise WindowsError("无法锁定剪贴板内存。")
            try:
                ctypes.memmove(pointer, ctypes.addressof(encoded), ctypes.sizeof(encoded))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(Win32.CF_UNICODETEXT, handle):
                raise WindowsError("无法写入 Windows 剪贴板。")
            # 成功后所有权已转移给系统。
            handle = None
        finally:
            if handle:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()

    @staticmethod
    def set_topmost(hwnd: int, enabled: bool) -> None:
        if sys.platform != "win32" or not hwnd:
            return
        HWND_TOPMOST = ctypes.c_void_p(-1)
        HWND_NOTOPMOST = ctypes.c_void_p(-2)
        flags = 0x0001 | 0x0002 | 0x0010  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        user32.SetWindowPos(HWND(hwnd), HWND_TOPMOST if enabled else HWND_NOTOPMOST, 0, 0, 0, 0, flags)

    @staticmethod
    def create_single_instance(name: str) -> tuple[Optional[Any], bool]:
        """返回 mutex 句柄和是否已存在，句柄需保持到进程退出。"""

        if sys.platform != "win32":
            return None, False
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return None, False
        already_exists = ctypes.get_last_error() == Win32.ERROR_ALREADY_EXISTS
        return handle, already_exists


class MouseCapture:
    """在隐藏主窗口时捕获下一次全局鼠标左键点击。"""

    def __init__(self, timeout_seconds: float = 12.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.point: Optional[tuple[int, int]] = None
        self.error: Optional[str] = None
        self._event = threading.Event()
        self._ready = threading.Event()
        self._thread_id = 0
        self._hook = None
        self._thread: Optional[threading.Thread] = None
        self._callback_ref = None

    def start(self) -> None:
        if sys.platform != "win32":
            self.error = "鼠标捕获只支持 Windows。"
            self._event.set()
            return
        self._thread = threading.Thread(target=self._run, name="mouse-capture", daemon=True)
        self._thread.start()
        self._ready.wait(1.5)

    def wait(self) -> Optional[tuple[int, int]]:
        self._event.wait(self.timeout_seconds)
        if not self._event.is_set():
            self.stop()
        if self._thread is not None:
            self._thread.join(1.0)
        return self.point

    def stop(self) -> None:
        if self._thread_id and sys.platform == "win32":
            user32.PostThreadMessageW(self._thread_id, Win32.WM_QUIT, 0, 0)

    def _run(self) -> None:
        if sys.platform != "win32":
            self.error = "鼠标捕获只支持 Windows。"
            self._event.set()
            return

        self._thread_id = int(kernel32.GetCurrentThreadId())
        msg = MSG()
        # 先创建消息队列，确保主线程可投递 WM_QUIT。
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, Win32.PM_NOREMOVE)

        callback_type = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

        @callback_type
        def hook_proc(n_code: int, w_param: int, l_param: int) -> int:
            if n_code == 0 and int(w_param) == Win32.WM_LBUTTONDOWN:
                data = ctypes.cast(l_param, ctypes.POINTER(MOUSELLHOOKSTRUCT)).contents
                self.point = (int(data.pt.x), int(data.pt.y))
                self._event.set()
                user32.PostThreadMessageW(self._thread_id, Win32.WM_QUIT, 0, 0)
            return int(user32.CallNextHookEx(self._hook, n_code, w_param, l_param))

        self._callback_ref = hook_proc
        module = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(Win32.WH_MOUSE_LL, hook_proc, module, 0)
        if not self._hook:
            self.error = f"无法启动鼠标捕获（错误码 {ctypes.get_last_error()}）。"
            self._ready.set()
            self._event.set()
            return
        self._ready.set()

        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            if not self._event.is_set():
                self._event.set()


class VirtualCapture:
    """轮询 F8 捕获鼠标当前屏幕坐标，不向目标窗口发送鼠标点击。"""

    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.point: Optional[tuple[int, int]] = None
        self.error: Optional[str] = None
        self._event = threading.Event()
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if sys.platform != "win32":
            self.error = "虚拟捕获只支持 Windows。"
            self._event.set()
            return
        self._thread = threading.Thread(target=self._run, name="virtual-capture", daemon=True)
        self._thread.start()
        self._ready.wait(1.5)

    def wait(self) -> Optional[tuple[int, int]]:
        self._event.wait(self.timeout_seconds)
        if not self._event.is_set():
            self.stop()
        if self._thread is not None:
            self._thread.join(1.0)
        return self.point

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        if sys.platform != "win32":
            self.error = "虚拟捕获只支持 Windows。"
            self._event.set()
            return

        self._ready.set()
        was_down = False
        deadline = time.monotonic() + self.timeout_seconds
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            is_down = bool(user32.GetAsyncKeyState(Win32.VK_F8) & 0x8000)
            if is_down and not was_down:
                try:
                    self.point = Win32.get_cursor_pos()
                except Exception as exc:
                    self.error = friendly_error(exc)
                self._event.set()
                return
            was_down = is_down
            time.sleep(0.025)
        if not self._stop_event.is_set() and self.point is None and self.error is None:
            self.error = "15 秒内没有检测到 F8；笔记本电脑请尝试 Fn+F8。"
        self._event.set()


# ---------------------------------------------------------------------------
# 持久化与任务逻辑
# ---------------------------------------------------------------------------


DEFAULT_DATA: dict[str, Any] = {
    "version": 1,
    "settings": {
        "always_on_top": True,
        "restore_target_geometry": True,
        "step_delay_seconds": 1.5,
        "input_anchor": None,
        "send_anchor": None,
        "conversation_search_anchor": None,
        "target_geometry": None,
        "target_title": "",
        "target_process": "",
    },
    "tasks": [],
}


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("任务文件不是 JSON 对象")
            except FileNotFoundError:
                raw = json.loads(json.dumps(DEFAULT_DATA))
            except Exception:
                # 保留损坏文件，避免用户丢失原始数据。
                try:
                    broken = self.path.with_name(self.path.stem + ".broken-" + str(int(time.time())) + self.path.suffix)
                    self.path.replace(broken)
                except Exception:
                    pass
                raw = json.loads(json.dumps(DEFAULT_DATA))

            settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
            tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
            self.data = {
                "version": 1,
                "settings": {**DEFAULT_DATA["settings"], **settings},
                "tasks": [self._normalize_task(item) for item in tasks if isinstance(item, dict)],
            }
            # 每日任务启动时不补发已经错过的历史时刻，滚到下一个未来日期。
            changed = False
            now = now_local()
            for task in self.data["tasks"]:
                if task.get("schedule") == "daily" and task.get("enabled"):
                    next_run = parse_datetime(task.get("next_run"))
                    if next_run is not None and next_run <= now:
                        while next_run <= now:
                            next_run += _dt.timedelta(days=1)
                        task["next_run"] = iso_datetime(next_run)
                        changed = True
            if changed:
                self._save_locked()

    @staticmethod
    def _normalize_task(item: dict[str, Any]) -> dict[str, Any]:
        schedule = item.get("schedule", "once")
        if schedule not in {"once", "daily", "interval"}:
            schedule = "once"
        action = item.get("action", "button")
        if action not in {"button", "enter"}:
            action = "button"
        next_run = parse_datetime(item.get("next_run"))
        try:
            interval_minutes = max(1, int(item.get("interval_minutes", 60) or 60))
        except (TypeError, ValueError):
            interval_minutes = 60
        return {
            "id": str(item.get("id") or uuid.uuid4().hex[:10]),
            "name": str(item.get("name") or "未命名提示词")[:80],
            "content": str(item.get("content") or ""),
            "conversation_title": str(item.get("conversation_title") or "")[:200],
            "target_profile": json.loads(json.dumps(item.get("target_profile"))) if isinstance(item.get("target_profile"), dict) else None,
            "schedule": schedule,
            "next_run": iso_datetime(next_run) if next_run else None,
            "interval_minutes": interval_minutes,
            "action": action,
            "enabled": bool(item.get("enabled", True)),
            "status": str(item.get("status") or "等待中"),
            "last_run": item.get("last_run"),
            "last_error": str(item.get("last_error") or ""),
        }

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.data.get("settings", {})))

    def update_settings(self, **updates: Any) -> None:
        with self._lock:
            self.data.setdefault("settings", {}).update(updates)
            self._save_locked()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self.data.get("tasks", [])))

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for task in self.data.get("tasks", []):
                if task.get("id") == task_id:
                    return json.loads(json.dumps(task))
        return None

    def add(self, task: dict[str, Any]) -> None:
        with self._lock:
            self.data.setdefault("tasks", []).append(self._normalize_task(task))
            self._save_locked()

    def replace(self, task_id: str, task: dict[str, Any]) -> bool:
        with self._lock:
            for index, old in enumerate(self.data.get("tasks", [])):
                if old.get("id") == task_id:
                    merged = {**old, **task, "id": task_id}
                    self.data["tasks"][index] = self._normalize_task(merged)
                    self._save_locked()
                    return True
        return False

    def update_task(self, task_id: str, **updates: Any) -> bool:
        with self._lock:
            for task in self.data.get("tasks", []):
                if task.get("id") == task_id:
                    task.update(updates)
                    self._save_locked()
                    return True
        return False

    def delete(self, task_id: str) -> bool:
        with self._lock:
            original = len(self.data.get("tasks", []))
            self.data["tasks"] = [task for task in self.data.get("tasks", []) if task.get("id") != task_id]
            if len(self.data["tasks"]) != original:
                self._save_locked()
                return True
        return False


def task_mode_label(schedule: str) -> str:
    return {"once": "单次", "daily": "每天", "interval": "间隔"}.get(schedule, "单次")


def task_action_label(action: str) -> str:
    return "点击发送" if action == "button" else "Enter 发送"


def task_status_label(task: dict[str, Any]) -> str:
    if task.get("status") == "运行中":
        return "运行中"
    if task.get("status") == "失败":
        return "失败"
    if task.get("schedule") == "once" and not task.get("enabled") and task.get("last_run"):
        return "已完成"
    if not task.get("enabled"):
        return "已暂停"
    return "等待中"


def task_next_label(task: dict[str, Any]) -> str:
    if not task.get("enabled") and task.get("schedule") != "once":
        return "已暂停"
    if task.get("schedule") == "once" and not task.get("enabled") and task.get("last_run"):
        return "已完成"
    return format_datetime(task.get("next_run"))


# ---------------------------------------------------------------------------
# 自动化执行
# ---------------------------------------------------------------------------


class AutomationError(RuntimeError):
    pass


class AutomationRunner:
    def __init__(self, settings: dict[str, Any], logger: Callable[[str, str], None]) -> None:
        self.settings = settings
        self.logger = logger

    def _profile_for_task(self, task: dict[str, Any]) -> dict[str, Any]:
        profile = task.get("target_profile")
        if not isinstance(profile, dict):
            return self.settings
        # 任务保存的是创建时的校准信息；新增加的校准项如果当时不存在，
        # 允许从当前全局设置补上，避免用户必须重建旧任务。
        merged = dict(self.settings)
        merged.update(profile)
        for key in ("input_anchor", "send_anchor", "conversation_search_anchor", "target_geometry", "target_title", "target_process"):
            value = profile.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                merged[key] = self.settings.get(key)
        return merged

    def _step_delay(self) -> float:
        try:
            return min(10.0, max(0.3, float(self.settings.get("step_delay_seconds", 1.5))))
        except (TypeError, ValueError):
            return 1.5

    def _switch_conversation(
        self,
        hwnd: int,
        title: str,
        search_anchor: dict[str, Any],
        delay: float,
    ) -> None:
        search_point = Win32.anchor_to_screen_point(search_anchor, hwnd)
        if search_point is None:
            raise AutomationError("左侧对话搜索按钮位置配置已损坏，请重新虚拟捕获。")

        self.logger("info", f"正在左侧搜索并切换对话：{title}")
        Win32.click(search_point)
        time.sleep(delay)
        # 搜索框由校准位置点击获得焦点；替换已有搜索内容后回车选择结果。
        Win32.ctrl_a()
        Win32.set_clipboard_text(title)
        Win32.ctrl_v()
        time.sleep(delay)
        Win32.press_enter()
        time.sleep(delay)

    def execute(self, task: dict[str, Any]) -> None:
        content = str(task.get("content") or "")
        if not content.strip():
            raise AutomationError("提示词内容为空，已跳过。")

        profile = self._profile_for_task(task)
        input_anchor = profile.get("input_anchor")
        if not isinstance(input_anchor, dict):
            raise AutomationError("尚未捕获 Codex 输入框位置。请先点击“捕获输入框”。")

        target = Win32.find_codex_window(
            title_hint=str(profile.get("target_title") or input_anchor.get("title") or ""),
            process_hint=str(profile.get("target_process") or input_anchor.get("process_name") or ""),
        )
        if not target:
            raise AutomationError("没有找到 Codex 窗口。请先打开 Codex，并确认窗口标题包含 Codex 或 ChatGPT。")

        hwnd = int(target["hwnd"])
        action = task.get("action", "button")
        send_anchor = profile.get("send_anchor")
        if action == "button":
            if not isinstance(send_anchor, dict):
                raise AutomationError("发送方式为“点击发送”，但尚未捕获发送按钮位置。")

        conversation_title = str(task.get("conversation_title") or "").strip()
        search_anchor = profile.get("conversation_search_anchor")
        if conversation_title and not isinstance(search_anchor, dict):
            raise AutomationError("任务指定了目标对话标题，但尚未捕获左侧对话搜索按钮。")

        self.logger("info", f"找到目标窗口：{target.get('title') or target.get('process_name') or 'Codex'}")
        geometry = profile.get("target_geometry") if self.settings.get("restore_target_geometry", True) else None
        if geometry:
            self.logger("info", "正在恢复捕获时的窗口位置和大小…")
        if not Win32.activate_window(hwnd, geometry=geometry):
            raise AutomationError("无法激活 Codex 窗口，已停止以避免误输入其它程序。")

        delay = self._step_delay()
        time.sleep(delay)
        old_clipboard = Win32.get_clipboard_text()
        try:
            if conversation_title:
                assert isinstance(search_anchor, dict)
                self._switch_conversation(hwnd, conversation_title, search_anchor, delay)
                if int(user32.GetForegroundWindow() or 0) != hwnd:
                    raise AutomationError("切换对话后 Codex 窗口失去焦点，已停止。")

            # 切换对话可能让内容区重新布局，因此在切换完成后重新计算输入框坐标。
            input_point = Win32.anchor_to_screen_point(input_anchor, hwnd)
            if input_point is None:
                raise AutomationError("输入框位置配置已损坏，请重新捕获。")
            Win32.set_clipboard_text(content)
            if int(user32.GetForegroundWindow() or 0) != hwnd:
                raise AutomationError("Codex 窗口在粘贴前失去焦点，已停止。")
            Win32.click(input_point)
            time.sleep(delay)
            Win32.ctrl_v()
            # 留出时间让输入框高度和语音/发送按钮状态完成切换。
            time.sleep(delay)

            if int(user32.GetForegroundWindow() or 0) != hwnd:
                raise AutomationError("粘贴后 Codex 窗口失去焦点，未继续发送。")
            if action == "button":
                assert isinstance(send_anchor, dict)
                # 在文字已经粘贴后再计算按钮位置，适配输入框展开和按钮状态切换。
                send_point = Win32.anchor_to_screen_point(send_anchor, hwnd)
                if send_point is None:
                    raise AutomationError("发送按钮位置配置已损坏，请重新捕获。")
                time.sleep(delay)
                Win32.click(send_point)
            else:
                time.sleep(delay)
                Win32.press_enter()
            self.logger("success", f"已发送：{task.get('name') or '未命名提示词'}")
        finally:
            # 尽量恢复执行前的文本剪贴板，避免定时任务干扰用户日常复制。
            if old_clipboard is not None:
                try:
                    Win32.set_clipboard_text(old_clipboard)
                except Exception as restore_error:
                    self.logger("warning", f"原剪贴板恢复失败：{friendly_error(restore_error)}")


class AutomationService:
    """调度线程只负责判断到期，实际自动化在独立线程运行。"""

    def __init__(self, store: TaskStore, emit: Callable[[str, Any], None]) -> None:
        self.store = store
        self.emit = emit
        self.stop_event = threading.Event()
        self.wakeup_event = threading.Event()
        self.running_ids: set[str] = set()
        self.running_lock = threading.Lock()
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True)

    def start(self) -> None:
        self.scheduler_thread.start()
        self.emit("log", ("info", "定时引擎已启动，每 250ms 检查一次。"))

    def stop(self) -> None:
        self.stop_event.set()
        self.wakeup_event.set()

    def wake(self) -> None:
        self.wakeup_event.set()

    def trigger(self, task_id: str, reason: str = "manual") -> bool:
        with self.running_lock:
            if task_id in self.running_ids:
                return False
            self.running_ids.add(task_id)
        worker = threading.Thread(target=self._execute, args=(task_id, reason), name=f"automation-{task_id}", daemon=True)
        worker.start()
        return True

    def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            now = now_local()
            for task in self.store.snapshot():
                if not task.get("enabled"):
                    continue
                next_run = parse_datetime(task.get("next_run"))
                if next_run is not None and next_run <= now:
                    self.trigger(str(task.get("id")), "schedule")
            self.wakeup_event.wait(0.25)
            self.wakeup_event.clear()

    def _execute(self, task_id: str, reason: str) -> None:
        try:
            task = self.store.get(task_id)
            if not task:
                return

            run_time = now_local()
            if reason == "schedule":
                updates: dict[str, Any] = {"status": "运行中", "last_error": ""}
                if task.get("schedule") == "once":
                    updates["enabled"] = False
                    updates["next_run"] = None
                elif task.get("schedule") == "daily":
                    candidate = parse_datetime(task.get("next_run")) or run_time
                    while candidate <= run_time:
                        candidate += _dt.timedelta(days=1)
                    updates["next_run"] = iso_datetime(candidate)
                else:
                    minutes = max(1, int(task.get("interval_minutes", 60) or 60))
                    updates["next_run"] = iso_datetime(run_time + _dt.timedelta(minutes=minutes))
                self.store.update_task(task_id, **updates)
            else:
                self.store.update_task(task_id, status="运行中", last_error="")

            self.emit("refresh", None)
            # 给 UI 一个机会隐藏置顶面板，避免面板遮挡 Codex 输入框。
            gate = threading.Event()
            self.emit("prepare", gate)
            gate.wait(0.8)

            settings = self.store.settings()
            runner = AutomationRunner(settings, lambda level, message: self.emit("log", (level, message)))
            runner.execute(task)

            status = "等待中" if task.get("enabled") else "已完成"
            self.store.update_task(task_id, status=status, last_run=iso_datetime(run_time), last_error="")
            self.emit("refresh", None)
        except Exception as exc:
            message = friendly_error(exc)
            task = self.store.get(task_id)
            updates = {"status": "失败", "last_error": message}
            if reason == "schedule" and task and task.get("schedule") == "once":
                updates["enabled"] = False
            self.store.update_task(task_id, **updates)
            self.emit("log", ("error", f"任务失败：{message}"))
            self.emit("refresh", None)
        finally:
            with self.running_lock:
                self.running_ids.discard(task_id)
            self.emit("restore", None)


# ---------------------------------------------------------------------------
# Tkinter 界面
# ---------------------------------------------------------------------------


class App:
    COLORS = {
        "bg": "#151515",
        "panel": "#202020",
        "panel_2": "#252525",
        "border": "#333333",
        "text": "#eeeeee",
        "muted": "#9b9b9b",
        "subtle": "#6f6f6f",
        "accent": "#59b7ff",
        "accent_dark": "#1e6fa8",
        "green": "#61d095",
        "yellow": "#f2c46d",
        "red": "#f07878",
        "white": "#ffffff",
    }

    def __init__(self, root: tk.Tk, store: TaskStore, mutex_handle: Any = None) -> None:
        self.root = root
        self.store = store
        self.mutex_handle = mutex_handle
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.service = AutomationService(store, self.emit)
        self.current_edit_id: Optional[str] = None
        self.root_hwnd = 0

        self.name_var = tk.StringVar(value="睡前提示词")
        self.conversation_title_var = tk.StringVar(value="")
        self.schedule_var = tk.StringVar(value="单次执行")
        self.date_var = tk.StringVar(value=now_local().strftime("%Y-%m-%d"))
        self.time_var = tk.StringVar(value=(now_local() + _dt.timedelta(minutes=5)).strftime("%H:%M"))
        self.interval_var = tk.StringVar(value="60")
        self.action_var = tk.StringVar(value="button")
        self.enabled_var = tk.BooleanVar(value=True)
        saved_settings = self.store.settings()
        self.topmost_var = tk.BooleanVar(value=bool(saved_settings.get("always_on_top", True)))
        self.restore_geometry_var = tk.BooleanVar(value=bool(saved_settings.get("restore_target_geometry", True)))
        try:
            saved_delay = float(saved_settings.get("step_delay_seconds", 1.5))
        except (TypeError, ValueError):
            saved_delay = 1.5
        self.speed_var = tk.StringVar(value=self._speed_label(saved_delay))
        self.target_status_var = tk.StringVar(value="尚未捕获目标位置")
        self.conversation_search_status_var = tk.StringVar(value="未设置")
        self.input_status_var = tk.StringVar(value="未设置")
        self.send_status_var = tk.StringVar(value="未设置")
        self.footer_var = tk.StringVar(value="正在启动定时引擎…")

        self._configure_root()
        self._build_styles()
        self._build_ui()
        self._apply_topmost()
        self.root.after(100, self._capture_root_hwnd)
        self.root.after(120, self._drain_events)
        self.root.after(1200, self._enforce_topmost)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.service.start()
        self.refresh_tasks()
        self._refresh_target_labels()

    def _configure_root(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("1120x760")
        self.root.minsize(820, 540)
        self.root.configure(bg=self.COLORS["bg"])
        self.root.option_add("*Font", ("Segoe UI", 10))
        self.root.option_add("*TButton.Cursor", "hand2")

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.TCombobox", fieldbackground=self.COLORS["panel_2"], background=self.COLORS["panel_2"], foreground=self.COLORS["text"], arrowcolor=self.COLORS["muted"], bordercolor=self.COLORS["border"])
        style.map("Dark.TCombobox", fieldbackground=[("readonly", self.COLORS["panel_2"])], foreground=[("readonly", self.COLORS["text"])])
        style.configure("Dark.Treeview", background=self.COLORS["panel"], fieldbackground=self.COLORS["panel"], foreground=self.COLORS["text"], rowheight=32, bordercolor=self.COLORS["border"], font=("Segoe UI", 9))
        style.map("Dark.Treeview", background=[("selected", "#29475d")], foreground=[("selected", self.COLORS["white"])])
        style.configure("Dark.Treeview.Heading", background=self.COLORS["panel_2"], foreground=self.COLORS["muted"], relief="flat", font=("Segoe UI", 9))
        style.map("Dark.Treeview.Heading", background=[("active", self.COLORS["panel_2"])])

    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.root, bg=self.COLORS["bg"], height=62)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 8))
        header.grid_columnconfigure(1, weight=1)

        tk.Label(header, text="●", fg=self.COLORS["green"], bg=self.COLORS["bg"], font=("Segoe UI", 13)).grid(row=0, column=0, rowspan=2, padx=(0, 9))
        tk.Label(header, text=APP_NAME, fg=self.COLORS["text"], bg=self.COLORS["bg"], font=("Segoe UI Semibold", 18)).grid(row=0, column=1, sticky="w")
        tk.Label(header, text="把准备好的提示词，按计划交给目标 AI 应用", fg=self.COLORS["muted"], bg=self.COLORS["bg"], font=("Segoe UI", 9)).grid(row=1, column=1, sticky="w", pady=(2, 0))

        self.pin_button = self._button(header, "📌 绝对置顶", self.toggle_topmost, kind="secondary", width=12)
        self.pin_button.grid(row=0, column=2, rowspan=2, padx=(8, 0))
        self._button(header, "×", self._on_close, kind="icon", width=3).grid(row=0, column=3, rowspan=2, padx=(7, 0))

        viewport = tk.Frame(self.root, bg=self.COLORS["bg"])
        viewport.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 12))
        viewport.grid_rowconfigure(0, weight=1)
        viewport.grid_columnconfigure(0, weight=1)

        self.viewport_canvas = tk.Canvas(viewport, bg=self.COLORS["bg"], highlightthickness=0, bd=0)
        self.viewport_canvas.grid(row=0, column=0, sticky="nsew")
        viewport_scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.viewport_canvas.yview)
        viewport_scroll.grid(row=0, column=1, sticky="ns", padx=(7, 0))
        self.viewport_canvas.configure(yscrollcommand=viewport_scroll.set)

        content = tk.Frame(self.viewport_canvas, bg=self.COLORS["bg"])
        self.content = content
        self.content_window = self.viewport_canvas.create_window((0, 0), window=content, anchor="nw")
        self.viewport_canvas.bind("<Configure>", self._resize_content_window)
        content.bind("<Configure>", lambda _event: self.viewport_canvas.configure(scrollregion=self.viewport_canvas.bbox("all")))
        self.viewport_canvas.bind("<Enter>", lambda _event: self.root.bind_all("<MouseWheel>", self._on_mousewheel))
        self.viewport_canvas.bind("<Leave>", lambda _event: self.root.unbind_all("<MouseWheel>"))

        content.grid_columnconfigure(0, weight=5, minsize=360, uniform="columns")
        content.grid_columnconfigure(1, weight=6, minsize=360, uniform="columns")
        content.grid_rowconfigure(0, weight=1)

        left = tk.Frame(content, bg=self.COLORS["bg"])
        self.left_column = left
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        left.grid_rowconfigure(0, weight=5)
        left.grid_rowconfigure(1, weight=3)
        left.grid_rowconfigure(2, weight=2)
        left.grid_columnconfigure(0, weight=1)

        right = tk.Frame(content, bg=self.COLORS["bg"])
        self.right_column = right
        right.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        right.grid_rowconfigure(0, weight=6)
        right.grid_rowconfigure(1, weight=4)
        right.grid_columnconfigure(0, weight=1)

        self._build_prompt_card(left)
        self._build_schedule_card(left)
        self._build_target_card(left)
        self._build_tasks_card(right)
        self._build_log_card(right)

        footer = tk.Frame(self.root, bg="#101010", height=32)
        footer.grid(row=2, column=0, sticky="ew")
        tk.Label(footer, textvariable=self.footer_var, fg=self.COLORS["muted"], bg="#101010", anchor="w", font=("Segoe UI", 9)).pack(side="left", padx=24, pady=7)
        tk.Label(footer, text=f"v{APP_VERSION}  ·  本地运行", fg=self.COLORS["subtle"], bg="#101010", anchor="e", font=("Segoe UI", 9)).pack(side="right", padx=24, pady=7)

    def _resize_content_window(self, event: Any) -> None:
        width = max(1, int(event.width))
        self.viewport_canvas.itemconfigure(self.content_window, width=width)
        narrow = width < 900
        if narrow:
            self.content.grid_columnconfigure(0, weight=1, minsize=0, uniform="columns")
            self.content.grid_columnconfigure(1, weight=0, minsize=0, uniform="columns")
            self.left_column.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 10))
            self.right_column.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=0, pady=0)
            self.content.grid_rowconfigure(0, weight=0)
            self.content.grid_rowconfigure(1, weight=0)
        else:
            self.content.grid_columnconfigure(0, weight=5, minsize=360, uniform="columns")
            self.content.grid_columnconfigure(1, weight=6, minsize=360, uniform="columns")
            self.left_column.grid(row=0, column=0, columnspan=1, sticky="nsew", padx=(0, 9), pady=0)
            self.right_column.grid(row=0, column=1, columnspan=1, sticky="nsew", padx=(9, 0), pady=0)
            self.content.grid_rowconfigure(0, weight=1)
            self.content.grid_rowconfigure(1, weight=0)
        self.viewport_canvas.configure(scrollregion=self.viewport_canvas.bbox("all"))

    def _on_mousewheel(self, event: Any) -> None:
        try:
            delta = int(event.delta)
            if delta:
                self.viewport_canvas.yview_scroll(-max(1, abs(delta) // 120) * (1 if delta < 0 else -1), "units")
        except tk.TclError:
            pass

    def _card(self, parent: tk.Widget, title: str, subtitle: str = "") -> tuple[tk.Frame, tk.Frame]:
        outer = tk.Frame(parent, bg=self.COLORS["border"], bd=0, highlightthickness=0)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)
        head = tk.Frame(outer, bg=self.COLORS["panel"], height=47)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        tk.Label(head, text=title, fg=self.COLORS["text"], bg=self.COLORS["panel"], font=("Segoe UI Semibold", 11), anchor="w").grid(row=0, column=0, sticky="w", padx=16, pady=(10, 0))
        if subtitle:
            tk.Label(head, text=subtitle, fg=self.COLORS["subtle"], bg=self.COLORS["panel"], font=("Segoe UI", 8), anchor="w").grid(row=1, column=0, sticky="w", padx=16, pady=(0, 9))
        body = tk.Frame(outer, bg=self.COLORS["panel"], padx=16, pady=13)
        body.grid(row=1, column=0, sticky="nsew")
        return outer, body

    def _entry(self, parent: tk.Widget, variable: tk.StringVar, width: int = 20) -> tk.Entry:
        return tk.Entry(parent, textvariable=variable, width=width, bg=self.COLORS["panel_2"], fg=self.COLORS["text"], insertbackground=self.COLORS["text"], relief="flat", highlightthickness=1, highlightbackground=self.COLORS["border"], highlightcolor=self.COLORS["accent"], font=("Segoe UI", 10))

    def _button(self, parent: tk.Widget, text: str, command: Callable[[], None], kind: str = "normal", width: Optional[int] = None) -> tk.Button:
        if kind == "primary":
            bg, active = self.COLORS["accent_dark"], "#2c85bf"
            fg = self.COLORS["white"]
        elif kind == "danger":
            bg, active = "#542828", "#753333"
            fg = "#ffb2b2"
        elif kind == "icon":
            bg, active = self.COLORS["panel_2"], self.COLORS["border"]
            fg = self.COLORS["muted"]
        else:
            bg, active = self.COLORS["panel_2"], self.COLORS["border"]
            fg = self.COLORS["text"]
        kwargs: dict[str, Any] = {
            "text": text,
            "command": command,
            "bg": bg,
            "activebackground": active,
            "activeforeground": self.COLORS["white"],
            "fg": fg,
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 0,
            "padx": 10,
            "pady": 7,
            "font": ("Segoe UI", 9),
        }
        if width is not None:
            kwargs["width"] = width
        return tk.Button(parent, **kwargs)

    def _build_prompt_card(self, parent: tk.Frame) -> None:
        card, body = self._card(parent, "提示词", "把睡前准备好的内容放在这里，程序会原样粘贴")
        card.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        body.grid_columnconfigure(1, weight=1)
        tk.Label(body, text="任务名称", bg=self.COLORS["panel"], fg=self.COLORS["muted"], anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.name_entry = self._entry(body, self.name_var)
        self.name_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8))

        tk.Label(body, text="目标对话标题", bg=self.COLORS["panel"], fg=self.COLORS["muted"], anchor="w").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.conversation_title_entry = self._entry(body, self.conversation_title_var)
        self.conversation_title_entry.grid(row=1, column=1, sticky="ew", pady=(0, 8))
        tk.Label(body, text="可选；填写后执行前会自动在左侧搜索并切换到该对话", bg=self.COLORS["panel"], fg=self.COLORS["subtle"], anchor="w", font=("Segoe UI", 8)).grid(row=2, column=1, sticky="w", pady=(0, 8))

        tk.Label(body, text="提示词内容", bg=self.COLORS["panel"], fg=self.COLORS["muted"], anchor="nw").grid(row=3, column=0, sticky="nw", padx=(0, 10))
        self.prompt_text = scrolledtext.ScrolledText(body, height=8, wrap="word", bg=self.COLORS["panel_2"], fg=self.COLORS["text"], insertbackground=self.COLORS["text"], selectbackground="#31516a", relief="flat", bd=0, highlightthickness=1, highlightbackground=self.COLORS["border"], highlightcolor=self.COLORS["accent"], font=("Segoe UI", 10), padx=9, pady=8)
        self.prompt_text.grid(row=3, column=1, sticky="nsew")
        body.grid_rowconfigure(3, weight=1)

        buttons = tk.Frame(body, bg=self.COLORS["panel"])
        buttons.grid(row=4, column=1, sticky="e", pady=(11, 0))
        self._button(buttons, "清空", self.clear_editor, kind="secondary").pack(side="left", padx=(0, 6))
        self._button(buttons, "仅粘贴测试", self.test_paste, kind="secondary").pack(side="left", padx=(0, 6))
        self.save_button = self._button(buttons, "＋ 保存到计划", self.save_task_from_editor, kind="primary")
        self.save_button.pack(side="left")

    def _build_schedule_card(self, parent: tk.Frame) -> None:
        card, body = self._card(parent, "执行计划", "单次、每天固定时间，或按分钟间隔运行")
        card.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(3, weight=1)

        tk.Label(body, text="执行方式", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 9))
        self.schedule_combo = ttk.Combobox(body, textvariable=self.schedule_var, values=["单次执行", "每天执行", "按间隔"], state="readonly", style="Dark.TCombobox", width=10)
        self.schedule_combo.grid(row=0, column=1, sticky="ew", pady=(0, 9))
        self.schedule_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_schedule_controls())
        self.enabled_check = tk.Checkbutton(body, text="启用任务", variable=self.enabled_var, bg=self.COLORS["panel"], fg=self.COLORS["text"], selectcolor=self.COLORS["panel_2"], activebackground=self.COLORS["panel"], activeforeground=self.COLORS["text"], font=("Segoe UI", 9))
        self.enabled_check.grid(row=0, column=2, columnspan=2, sticky="e", pady=(0, 9))

        tk.Label(body, text="日期", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 9))
        self.date_entry = self._entry(body, self.date_var, width=12)
        self.date_entry.grid(row=1, column=1, sticky="ew", pady=(0, 9), padx=(0, 12))
        tk.Label(body, text="时间", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=1, column=2, sticky="w", padx=(0, 8), pady=(0, 9))
        self.time_entry = self._entry(body, self.time_var, width=8)
        self.time_entry.grid(row=1, column=3, sticky="ew", pady=(0, 9))

        tk.Label(body, text="间隔分钟", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 9))
        self.interval_entry = self._entry(body, self.interval_var, width=12)
        self.interval_entry.grid(row=2, column=1, sticky="ew", pady=(0, 9), padx=(0, 12))
        tk.Label(body, text="发送方式", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=2, column=2, sticky="w", padx=(0, 8), pady=(0, 9))
        self.action_combo = ttk.Combobox(body, textvariable=self.action_var, values=["点击已捕获的发送按钮", "按 Enter 发送"], state="readonly", style="Dark.TCombobox", width=13)
        self.action_combo.grid(row=2, column=3, sticky="ew", pady=(0, 9))

        tk.Label(body, text="格式提示：日期 YYYY-MM-DD，时间 HH:MM", bg=self.COLORS["panel"], fg=self.COLORS["subtle"], font=("Segoe UI", 8)).grid(row=3, column=0, columnspan=4, sticky="w")
        self.schedule_combo.set("单次执行")
        self.action_combo.set("点击已捕获的发送按钮")
        self._update_schedule_controls()

    def _build_target_card(self, parent: tk.Frame) -> None:
        card, body = self._card(parent, "目标窗口与校准", "只会操作你捕获位置所在的目标窗口（Codex / ChatGPT）")
        card.grid(row=2, column=0, sticky="nsew")
        body.grid_columnconfigure(1, weight=1)
        tk.Label(body, text="目标窗口", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=0, column=0, sticky="w", padx=(0, 8))
        tk.Label(body, textvariable=self.target_status_var, bg=self.COLORS["panel"], fg=self.COLORS["green"], anchor="w").grid(row=0, column=1, sticky="ew")
        self._button(body, "检测", self.detect_target, kind="secondary", width=7).grid(row=0, column=2, padx=(8, 0))

        tk.Label(body, text="左侧对话搜索", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(11, 0))
        tk.Label(body, textvariable=self.conversation_search_status_var, bg=self.COLORS["panel"], fg=self.COLORS["text"], anchor="w").grid(row=1, column=1, sticky="ew", pady=(11, 0))
        self._button(body, "虚拟捕获搜索按钮", lambda: self.capture_anchor("conversation_search"), kind="secondary").grid(row=1, column=2, padx=(8, 0), pady=(11, 0))

        tk.Label(body, text="输入框", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(11, 0))
        tk.Label(body, textvariable=self.input_status_var, bg=self.COLORS["panel"], fg=self.COLORS["text"], anchor="w").grid(row=2, column=1, sticky="ew", pady=(11, 0))
        self._button(body, "虚拟捕获输入框", lambda: self.capture_anchor("input"), kind="secondary").grid(row=2, column=2, padx=(8, 0), pady=(11, 0))

        tk.Label(body, text="发送按钮", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        tk.Label(body, textvariable=self.send_status_var, bg=self.COLORS["panel"], fg=self.COLORS["text"], anchor="w").grid(row=3, column=1, sticky="ew", pady=(8, 0))
        self._button(body, "虚拟捕获发送按钮", lambda: self.capture_anchor("send"), kind="secondary").grid(row=3, column=2, padx=(8, 0), pady=(8, 0))

        self.restore_geometry_check = tk.Checkbutton(body, text="执行前恢复捕获时的窗口位置和大小", variable=self.restore_geometry_var, command=self.toggle_restore_geometry, bg=self.COLORS["panel"], fg=self.COLORS["text"], selectcolor=self.COLORS["panel_2"], activebackground=self.COLORS["panel"], activeforeground=self.COLORS["text"], font=("Segoe UI", 9))
        self.restore_geometry_check.grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))

        tk.Label(body, text="步骤间隔", bg=self.COLORS["panel"], fg=self.COLORS["muted"]).grid(row=5, column=0, sticky="w", padx=(0, 8), pady=(9, 0))
        self.speed_combo = ttk.Combobox(body, textvariable=self.speed_var, values=["0.8 秒（较快）", "1.5 秒（推荐）", "2.5 秒（较慢）", "4 秒（调试）"], state="readonly", style="Dark.TCombobox", width=16)
        self.speed_combo.grid(row=5, column=1, columnspan=2, sticky="ew", pady=(9, 0))
        self.speed_combo.bind("<<ComboboxSelected>>", lambda _event: self.save_speed_setting())

    def _build_tasks_card(self, parent: tk.Frame) -> None:
        card, body = self._card(parent, "计划列表", "到点后自动执行；手动运行不会改变原有计划时间")
        card.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        body.grid_rowconfigure(0, weight=1)
        body.grid_rowconfigure(2, weight=1)
        body.grid_columnconfigure(0, weight=1)

        columns = ("status", "name", "next", "mode")
        self.task_tree = ttk.Treeview(body, columns=columns, show="headings", style="Dark.Treeview", selectmode="browse")
        headings = {"status": "状态", "name": "任务", "next": "下次执行", "mode": "方式"}
        widths = {"status": 68, "name": 180, "next": 135, "mode": 80}
        for col in columns:
            self.task_tree.heading(col, text=headings[col])
            self.task_tree.column(col, width=widths[col], minwidth=55, anchor="w")
        self.task_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.task_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns", padx=(7, 0))
        self.task_tree.configure(yscrollcommand=scroll.set)
        self.task_tree.bind("<<TreeviewSelect>>", self._on_task_select)

        actions = tk.Frame(body, bg=self.COLORS["panel"])
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(11, 0))
        self._button(actions, "查看原始信息", self.show_selected_task_raw, kind="secondary").pack(side="left", padx=(0, 6))
        self._button(actions, "编辑选中", self.edit_selected_task, kind="secondary").pack(side="left", padx=(0, 6))
        self._button(actions, "立即运行", self.run_selected_task, kind="primary").pack(side="left", padx=(0, 6))
        self._button(actions, "暂停 / 启用", self.toggle_selected_task, kind="secondary").pack(side="left", padx=(0, 6))
        self._button(actions, "删除", self.delete_selected_task, kind="danger").pack(side="right")

        self.task_info_text = scrolledtext.ScrolledText(body, height=9, state="disabled", wrap="word", bg="#181818", fg=self.COLORS["muted"], insertbackground=self.COLORS["text"], selectbackground="#31516a", relief="flat", bd=0, highlightthickness=1, highlightbackground=self.COLORS["border"], highlightcolor=self.COLORS["accent"], font=("Consolas", 8), padx=9, pady=8)
        self.task_info_text.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        self._set_task_info_text("单击计划后，这里显示只读的原始任务信息。\n编辑器不会因单击列表而改变；需要修改时请点击“编辑选中”。")

    def _build_log_card(self, parent: tk.Frame) -> None:
        card, body = self._card(parent, "运行日志", "最近的调度、窗口查找与发送结果")
        card.grid(row=1, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(body, height=8, state="disabled", wrap="word", bg="#181818", fg=self.COLORS["muted"], insertbackground=self.COLORS["text"], relief="flat", bd=0, highlightthickness=0, font=("Consolas", 9), padx=9, pady=8)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("success", foreground=self.COLORS["green"])
        self.log_text.tag_configure("error", foreground=self.COLORS["red"])
        self.log_text.tag_configure("warning", foreground=self.COLORS["yellow"])
        self.log_text.tag_configure("info", foreground=self.COLORS["muted"])
        self._button(body, "清除日志", self.clear_log, kind="secondary", width=9).grid(row=1, column=0, sticky="e", pady=(8, 0))

    # ---- UI 小工具 -------------------------------------------------------

    def emit(self, event: str, payload: Any) -> None:
        self.events.put((event, payload))

    def _capture_root_hwnd(self) -> None:
        try:
            self.root_hwnd = int(self.root.winfo_id())
            self._apply_topmost()
        except tk.TclError:
            pass

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    level, text = payload
                    self.log(level, text)
                elif event == "refresh":
                    self.refresh_tasks()
                elif event == "prepare":
                    try:
                        self.root.withdraw()
                        self.root.attributes("-topmost", False)
                    except tk.TclError:
                        pass
                    payload.set()
                elif event == "restore":
                    try:
                        self.root.deiconify()
                        self._apply_topmost()
                        self.root.lift()
                    except tk.TclError:
                        pass
        except queue.Empty:
            pass
        try:
            self.root.after(120, self._drain_events)
        except tk.TclError:
            pass

    def _enforce_topmost(self) -> None:
        if self.topmost_var.get():
            try:
                self._apply_topmost()
                self.root.lift()
            except tk.TclError:
                return
        try:
            self.root.after(1200, self._enforce_topmost)
        except tk.TclError:
            pass

    def _apply_topmost(self) -> None:
        enabled = bool(self.topmost_var.get())
        try:
            self.root.attributes("-topmost", enabled)
            if self.root_hwnd:
                Win32.set_topmost(self.root_hwnd, enabled)
            self.pin_button.configure(text="📌 已置顶" if enabled else "📌 普通窗口")
        except tk.TclError:
            pass

    def toggle_topmost(self) -> None:
        self.topmost_var.set(not self.topmost_var.get())
        self.store.update_settings(always_on_top=self.topmost_var.get())
        self._apply_topmost()
        self.footer_var.set("已开启绝对置顶" if self.topmost_var.get() else "已关闭绝对置顶")

    def _update_schedule_controls(self) -> None:
        is_interval = self._schedule_value() == "interval"
        self.interval_entry.configure(state=tk.NORMAL if is_interval else tk.DISABLED)
        date_state = tk.DISABLED if is_interval else tk.NORMAL
        self.date_entry.configure(state=date_state)
        self.time_entry.configure(state=date_state)

    def _schedule_value(self) -> str:
        return {"单次执行": "once", "每天执行": "daily", "按间隔": "interval"}.get(self.schedule_var.get(), "once")

    @staticmethod
    def _speed_label(seconds: float) -> str:
        choices = [(0.8, "0.8 秒（较快）"), (1.5, "1.5 秒（推荐）"), (2.5, "2.5 秒（较慢）"), (4.0, "4 秒（调试）")]
        return min(choices, key=lambda item: abs(item[0] - seconds))[1]

    def _speed_seconds(self) -> float:
        match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", self.speed_combo.get())
        if not match:
            return 1.5
        try:
            return min(10.0, max(0.3, float(match.group(1))))
        except ValueError:
            return 1.5

    def save_speed_setting(self) -> None:
        self.store.update_settings(step_delay_seconds=self._speed_seconds())
        self.footer_var.set(f"步骤间隔已设置为 {self._speed_seconds():g} 秒")

    def toggle_restore_geometry(self) -> None:
        self.store.update_settings(restore_target_geometry=bool(self.restore_geometry_var.get()))
        self.footer_var.set("已开启捕获窗口尺寸恢复" if self.restore_geometry_var.get() else "已关闭捕获窗口尺寸恢复")

    def _action_value(self) -> str:
        return "button" if self.action_combo.get().startswith("点击") else "enter"

    def log(self, level: str, message: str) -> None:
        timestamp = _dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line, level if level in {"success", "error", "warning", "info"} else "info")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass
        self.footer_var.set(message)
        try:
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def clear_editor(self) -> None:
        self.name_var.set("睡前提示词")
        self.conversation_title_var.set("")
        self.prompt_text.delete("1.0", "end")
        self.current_edit_id = None
        self.save_button.configure(text="＋ 保存到计划")
        self.footer_var.set("编辑区已清空")

    # ---- 目标窗口 -------------------------------------------------------

    def detect_target(self) -> None:
        try:
            target = Win32.find_codex_window(exclude_hwnd=self.root_hwnd)
            if not target:
                self.target_status_var.set("未找到 Codex / ChatGPT")
                self.log("warning", "未找到目标窗口，请先打开 Codex。")
                return
            self.store.update_settings(target_title=target.get("title", ""), target_process=target.get("process_name", ""))
            title = target.get("title") or target.get("process_name") or "Codex"
            self.target_status_var.set(f"已找到：{title[:32]}")
            self.log("success", f"目标窗口检测成功：{title}")
        except Exception as exc:
            self.log("error", f"检测目标窗口失败：{friendly_error(exc)}")

    def _refresh_target_labels(self) -> None:
        settings = self.store.settings()
        conversation_search_anchor = settings.get("conversation_search_anchor")
        input_anchor = settings.get("input_anchor")
        send_anchor = settings.get("send_anchor")
        if isinstance(conversation_search_anchor, dict):
            suffix = " · 建议 F8 重捕" if "screen_x" not in conversation_search_anchor else ""
            self.conversation_search_status_var.set(f"已捕获{suffix}")
        else:
            self.conversation_search_status_var.set("未设置")
        if isinstance(input_anchor, dict):
            title = input_anchor.get("title") or input_anchor.get("process_name") or "Codex"
            suffix = " · 建议 F8 重捕" if "screen_x" not in input_anchor else ""
            self.input_status_var.set(f"已捕获{suffix} · {str(title)[:22]}")
        else:
            self.input_status_var.set("未设置")
        if isinstance(send_anchor, dict):
            suffix = "（建议 F8 重捕）" if send_anchor.get("edge_mode") != "bottom-right" else ""
            self.send_status_var.set(f"已捕获发送位置{suffix}")
        else:
            self.send_status_var.set("未设置")

    def _target_profile(self) -> dict[str, Any]:
        settings = self.store.settings()
        return {
            "input_anchor": json.loads(json.dumps(settings.get("input_anchor"))) if isinstance(settings.get("input_anchor"), dict) else None,
            "send_anchor": json.loads(json.dumps(settings.get("send_anchor"))) if isinstance(settings.get("send_anchor"), dict) else None,
            "conversation_search_anchor": json.loads(json.dumps(settings.get("conversation_search_anchor"))) if isinstance(settings.get("conversation_search_anchor"), dict) else None,
            "target_geometry": json.loads(json.dumps(settings.get("target_geometry"))) if isinstance(settings.get("target_geometry"), dict) else None,
            "target_title": str(settings.get("target_title") or ""),
            "target_process": str(settings.get("target_process") or ""),
        }

    def capture_anchor(self, kind: str) -> None:
        labels = {
            "conversation_search": "左侧对话搜索按钮",
            "input": "输入框",
            "send": "发送按钮",
        }
        label = labels.get(kind, "目标位置")
        if not messagebox.askokcancel(
            f"捕获{label}",
            f"点击“确定”后主窗口会暂时隐藏。\n\n请把鼠标移动到 Codex 的{label}，不要点击；\n然后在 15 秒内按一次 F8 完成虚拟捕获。\nF8 只记录鼠标位置，不会触发 Codex 的语音或发送动作。",
            parent=self.root,
        ):
            return

        capture = VirtualCapture(timeout_seconds=15.0)
        point: Optional[tuple[int, int]] = None
        helper_restored = True
        try:
            self.root.withdraw()
            self.root.update_idletasks()
            capture.start()
            point = capture.wait()
        except Exception as exc:
            capture.error = friendly_error(exc)
        finally:
            # 即使捕获线程或 Windows API 抛出异常，也必须把助手窗口恢复出来。
            capture.stop()
            try:
                self.root.deiconify()
                self.root.update_idletasks()
                self._apply_topmost()
                self.root.lift()
            except tk.TclError:
                helper_restored = False

        if not helper_restored:
            return

        if point is None:
            self.log("error", capture.error or f"虚拟捕获{label}超时，没有收到 F8。")
            messagebox.showwarning("捕获失败", capture.error or "已超时，请重新把鼠标移到目标位置并按 F8。", parent=self.root)
            return

        try:
            hwnd = Win32.point_to_root_window(*point)
            if not hwnd or hwnd == self.root_hwnd:
                raise WindowsError("点击位置不属于可用目标窗口。")
            info = Win32.window_info(hwnd)
            combined = f"{info.get('title', '')} {info.get('process_name', '')}".lower()
            if not any(token in combined for token in ("codex", "chatgpt", "openai")):
                raise WindowsError(f"捕获到的窗口不是 Codex / ChatGPT：{info.get('title') or info.get('process_name') or '未知窗口'}")
            anchor = Win32.anchor_from_screen_point(point, hwnd, edge_mode=(kind == "send"))
            if not anchor:
                raise WindowsError("无法读取目标窗口尺寸。")
            geometry = Win32.capture_window_geometry(hwnd)
            if kind == "conversation_search":
                self.store.update_settings(conversation_search_anchor=anchor, target_geometry=geometry, target_title=info.get("title", ""), target_process=info.get("process_name", ""))
            elif kind == "input":
                self.store.update_settings(input_anchor=anchor, target_geometry=geometry, target_title=info.get("title", ""), target_process=info.get("process_name", ""))
            else:
                self.store.update_settings(send_anchor=anchor, target_geometry=geometry, target_title=info.get("title", ""), target_process=info.get("process_name", ""))
            self._refresh_target_labels()
            title = info.get("title") or info.get("process_name") or "Codex"
            self.target_status_var.set(f"已锁定：{title[:32]}")
            self.log("success", f"{label}虚拟捕获成功：{title}，不会点击语音按钮。")
        except Exception as exc:
            self.log("error", f"{label}捕获失败：{friendly_error(exc)}")
            messagebox.showwarning("捕获失败", friendly_error(exc), parent=self.root)

    def test_paste(self) -> None:
        content = self.prompt_text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showwarning("无法测试", "请先输入提示词内容。", parent=self.root)
            return
        task = {"name": "测试粘贴", "content": content, "action": "enter"}
        self._run_ephemeral(task)

    def _run_ephemeral(self, task: dict[str, Any]) -> None:
        self.log("info", "开始仅粘贴测试，不会发送。")

        def worker() -> None:
            try:
                settings = self.store.settings()
                input_anchor = settings.get("input_anchor")
                if not isinstance(input_anchor, dict):
                    raise AutomationError("尚未捕获 Codex 输入框位置。")
                target = Win32.find_codex_window(title_hint=str(settings.get("target_title") or ""), process_hint=str(settings.get("target_process") or ""))
                if not target:
                    raise AutomationError("没有找到 Codex 窗口。")
                hwnd = int(target["hwnd"])
                geometry = settings.get("target_geometry") if settings.get("restore_target_geometry", True) else None
                if not Win32.activate_window(hwnd, geometry=geometry):
                    raise AutomationError("无法激活 Codex 窗口。")
                point = Win32.anchor_to_screen_point(input_anchor, hwnd)
                if point is None:
                    raise AutomationError("输入框位置配置已损坏，请重新虚拟捕获。")
                try:
                    delay = min(10.0, max(0.3, float(settings.get("step_delay_seconds", 1.5))))
                except (TypeError, ValueError):
                    delay = 1.5
                time.sleep(delay)
                old = Win32.get_clipboard_text()
                try:
                    Win32.set_clipboard_text(str(task["content"]))
                    if int(user32.GetForegroundWindow() or 0) != hwnd:
                        raise AutomationError("Codex 窗口失去焦点，未执行粘贴。")
                    Win32.click(point)
                    time.sleep(delay)
                    Win32.ctrl_v()
                    time.sleep(delay)
                    self.emit("log", ("success", "已完成仅粘贴测试，请检查 Codex 输入框内容。"))
                finally:
                    if old is not None:
                        try:
                            Win32.set_clipboard_text(old)
                        except Exception:
                            pass
            except Exception as exc:
                self.emit("log", ("error", f"仅粘贴测试失败：{friendly_error(exc)}"))
            finally:
                self.emit("restore", None)

        gate = threading.Event()
        self.emit("prepare", gate)
        threading.Thread(target=lambda: (gate.wait(0.8), worker()), name="paste-test", daemon=True).start()

    # ---- 任务编辑 -------------------------------------------------------

    def _parse_editor(self) -> Optional[dict[str, Any]]:
        name = self.name_var.get().strip() or "未命名提示词"
        content = self.prompt_text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showwarning("无法保存", "提示词内容不能为空。", parent=self.root)
            return None

        schedule = self._schedule_value()
        action = self._action_value()
        current_time = now_local()
        try:
            if schedule == "interval":
                minutes = int(self.interval_var.get().strip())
                if minutes < 1 or minutes > 525600:
                    raise ValueError
                next_run = current_time + _dt.timedelta(minutes=minutes)
            else:
                date_text = self.date_var.get().strip()
                time_text = self.time_var.get().strip()
                next_run = _dt.datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
                if schedule == "daily" and next_run <= current_time:
                    next_run += _dt.timedelta(days=1)
                if schedule == "once" and next_run < current_time - _dt.timedelta(minutes=1):
                    if not messagebox.askyesno("时间已过去", "该时间已经过去，保存后程序会立即执行。继续吗？", parent=self.root):
                        return None
        except ValueError:
            messagebox.showwarning("时间格式错误", "请使用 YYYY-MM-DD、HH:MM，并确认间隔分钟为正整数。", parent=self.root)
            return None

        return {
            "name": name,
            "conversation_title": self.conversation_title_var.get().strip()[:200],
            "content": content,
            "target_profile": self._target_profile(),
            "schedule": schedule,
            "next_run": iso_datetime(next_run),
            "interval_minutes": int(self.interval_var.get() or 60) if schedule == "interval" else 60,
            "action": action,
            "enabled": bool(self.enabled_var.get()),
            "status": "等待中" if self.enabled_var.get() else "已暂停",
            "last_run": None,
            "last_error": "",
        }

    def save_task_from_editor(self) -> None:
        task = self._parse_editor()
        if task is None:
            return
        try:
            if self.current_edit_id:
                self.store.replace(self.current_edit_id, task)
                message = "计划已更新"
            else:
                task["id"] = uuid.uuid4().hex[:10]
                self.store.add(task)
                message = "计划已保存"
            self.service.wake()
            self.refresh_tasks()
            self.log("success", message + f"：{task['name']}")
            self.clear_editor()
        except Exception as exc:
            self.log("error", f"保存计划失败：{friendly_error(exc)}")

    def refresh_tasks(self) -> None:
        if not hasattr(self, "task_tree"):
            return
        selected = self.task_tree.selection()
        selected_id = selected[0] if selected else None
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        for task in self.store.snapshot():
            task_id = str(task.get("id"))
            self.task_tree.insert("", "end", iid=task_id, values=(task_status_label(task), task.get("name", ""), task_next_label(task), task_mode_label(task.get("schedule", "once"))))
        if selected_id and self.task_tree.exists(selected_id):
            self.task_tree.selection_set(selected_id)
        self._on_task_select()

    def _selected_id(self) -> Optional[str]:
        if not hasattr(self, "task_tree"):
            return None
        selection = self.task_tree.selection()
        return str(selection[0]) if selection else None

    @staticmethod
    def _task_raw_text(task: dict[str, Any]) -> str:
        return json.dumps(task, ensure_ascii=False, indent=2)

    def _set_task_info_text(self, text: str) -> None:
        try:
            self.task_info_text.configure(state="normal")
            self.task_info_text.delete("1.0", "end")
            self.task_info_text.insert("1.0", text)
            self.task_info_text.configure(state="disabled")
            self.task_info_text.see("1.0")
        except tk.TclError:
            pass

    def show_selected_task_raw(self) -> None:
        task_id = self._selected_id()
        task = self.store.get(task_id) if task_id else None
        if not task:
            messagebox.showinfo("查看原始信息", "请先在计划列表中选择一个任务。", parent=self.root)
            return

        viewer = tk.Toplevel(self.root)
        viewer.title(f"原始任务信息 - {task.get('name', '未命名提示词')}")
        viewer.geometry("860x640")
        viewer.minsize(620, 420)
        viewer.configure(bg=self.COLORS["bg"])
        viewer.transient(self.root)
        viewer.lift()
        viewer.grid_rowconfigure(0, weight=1)
        viewer.grid_columnconfigure(0, weight=1)
        raw_text = scrolledtext.ScrolledText(viewer, state="disabled", wrap="word", bg="#181818", fg=self.COLORS["text"], insertbackground=self.COLORS["text"], selectbackground="#31516a", relief="flat", bd=0, highlightthickness=0, font=("Consolas", 9), padx=12, pady=10)
        raw_text.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        raw_text.configure(state="normal")
        raw_text.insert("1.0", self._task_raw_text(task))
        raw_text.configure(state="disabled")
        self._button(viewer, "关闭", viewer.destroy, kind="secondary", width=9).grid(row=1, column=0, sticky="e", padx=12, pady=(0, 12))

    def _on_task_select(self, _event: Any = None) -> None:
        task_id = self._selected_id()
        if not task_id:
            self._set_task_info_text("单击计划后，这里显示只读的原始任务信息。\n编辑器不会因单击列表而改变；需要修改时请点击“编辑选中”。")
            return
        task = self.store.get(task_id)
        if not task:
            self._set_task_info_text("未找到所选任务。")
            return
        self._set_task_info_text(self._task_raw_text(task))
        self.footer_var.set(f"已选择“{task.get('name', '未命名提示词')}”，原始信息已显示在计划列表下方")

    def edit_selected_task(self) -> None:
        task_id = self._selected_id()
        task = self.store.get(task_id) if task_id else None
        if not task:
            messagebox.showinfo("编辑任务", "请先在计划列表中选择一个任务。", parent=self.root)
            return
        self.current_edit_id = str(task["id"])
        self.name_var.set(task.get("name", ""))
        self.conversation_title_var.set(str(task.get("conversation_title") or ""))
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", task.get("content", ""))
        schedule = task.get("schedule", "once")
        schedule_display = {"once": "单次执行", "daily": "每天执行", "interval": "按间隔"}.get(schedule, "单次执行")
        self.schedule_var.set(schedule_display)
        self.schedule_combo.set(schedule_display)
        next_run = parse_datetime(task.get("next_run")) or now_local()
        self.date_var.set(next_run.strftime("%Y-%m-%d"))
        self.time_var.set(next_run.strftime("%H:%M"))
        self.interval_var.set(str(task.get("interval_minutes", 60)))
        self.enabled_var.set(bool(task.get("enabled", True)))
        action = task.get("action", "button")
        self.action_var.set(action)
        self.action_combo.set("点击已捕获的发送按钮" if action == "button" else "按 Enter 发送")
        self.save_button.configure(text="✓ 更新计划")
        self._update_schedule_controls()
        self.footer_var.set("已载入任务，请修改后更新计划")

    def toggle_selected_task(self) -> None:
        task_id = self._selected_id()
        task = self.store.get(task_id) if task_id else None
        if not task:
            messagebox.showinfo("暂停 / 启用", "请先选择一个任务。", parent=self.root)
            return
        enabled = not bool(task.get("enabled"))
        self.store.update_task(task_id, enabled=enabled, status="等待中" if enabled else "已暂停", last_error="")
        self.service.wake()
        self.refresh_tasks()
        self.log("info", f"任务已{'启用' if enabled else '暂停'}：{task.get('name', '')}")

    def run_selected_task(self) -> None:
        task_id = self._selected_id()
        task = self.store.get(task_id) if task_id else None
        if not task:
            messagebox.showinfo("立即运行", "请先选择一个任务。", parent=self.root)
            return
        if not self.store.settings().get("input_anchor"):
            messagebox.showwarning("无法运行", "请先捕获 Codex 输入框位置。", parent=self.root)
            return
        if not self.service.trigger(str(task["id"]), "manual"):
            self.log("warning", "该任务正在运行，请稍候。")
        else:
            self.log("info", f"开始立即运行：{task.get('name', '')}")

    def delete_selected_task(self) -> None:
        task_id = self._selected_id()
        task = self.store.get(task_id) if task_id else None
        if not task:
            messagebox.showinfo("删除任务", "请先选择一个任务。", parent=self.root)
            return
        if not messagebox.askyesno("删除任务", f"确定删除“{task.get('name', '')}”吗？\n删除后不能从本工具恢复。", parent=self.root):
            return
        self.store.delete(str(task["id"]))
        self.refresh_tasks()
        self.log("info", f"已删除任务：{task.get('name', '')}")

    def _on_close(self) -> None:
        if not messagebox.askyesno("退出程序", "退出后定时任务将不会继续。确定退出吗？", parent=self.root):
            return
        self.service.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 启动与异常处理
# ---------------------------------------------------------------------------


def write_crash_log(_exc: BaseException) -> None:
    try:
        text = "\n".join([
            f"{_dt.datetime.now().isoformat(timespec='seconds')} {APP_NAME} {APP_VERSION}",
            traceback.format_exc(),
            "",
        ])
        with CRASH_PATH.open("a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        pass


def main() -> int:
    Win32.require_windows()
    Win32.set_dpi_awareness()
    mutex, already_exists = Win32.create_single_instance("Local\\CueRelay-7C58B1A2")
    if already_exists:
        try:
            messagebox.showinfo(APP_NAME, "程序已经在运行中。")
        except Exception:
            pass
        return 0

    store = TaskStore(DATA_PATH)
    root = tk.Tk()
    app = App(root, store, mutex_handle=mutex)
    app.log("info", f"{APP_NAME} 已启动。配置文件：{DATA_PATH.name}")
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        write_crash_log(exc)
        try:
            messagebox.showerror(APP_NAME, f"程序遇到未处理错误：\n{friendly_error(exc)}\n\n详细信息已写入 {CRASH_PATH.name}")
        except Exception:
            pass
        raise
