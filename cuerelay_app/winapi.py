# -*- coding: utf-8 -*-
"""Windows 原生能力封装。

只使用 ctypes 与标准库，不依赖 pywin32 / pyautogui。
所有函数在非 Windows 平台都会抛出 Win32Error，方便其它模块在导入时不炸掉。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .config import SELF_TITLE_TOKENS

IS_WINDOWS = sys.platform == "win32"


class Win32Error(RuntimeError):
    pass


if IS_WINDOWS:  # pragma: no branch - 只在 Windows 上真正执行
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
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

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    # --- user32 -----------------------------------------------------------
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
    user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
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
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
    user32.GetWindowThreadProcessId.restype = DWORD
    user32.SetWindowPos.argtypes = [
        HWND,
        HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetDC.argtypes = [HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int

    # --- gdi32 ------------------------------------------------------------
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFOHEADER),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.GetDeviceCaps.restype = ctypes.c_int

    # --- kernel32 ---------------------------------------------------------
    kernel32.OpenProcess.argtypes = [DWORD, wintypes.BOOL, DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(DWORD),
    ]
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


# 常量集中放在这里，避免散落在各处。
SW_RESTORE = 9
SW_MAXIMIZE = 3
GA_ROOT = 2
VK_CONTROL = 0x11
VK_A = 0x41
VK_V = 0x56
VK_K = 0x4B
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_MENU = 0x12
VK_F8 = 0x77
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
LOGPIXELSX = 88
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


def _require() -> None:
    if not IS_WINDOWS:
        raise Win32Error("此功能只支持 Windows。")


# ---------------------------------------------------------------------------
# 进程与 DPI
# ---------------------------------------------------------------------------


def set_dpi_awareness() -> None:
    """必须在创建 Tk 窗口之前调用，否则界面在缩放屏幕上会发虚。"""

    if not IS_WINDOWS:
        return
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def get_system_dpi() -> int:
    if not IS_WINDOWS:
        return 96
    try:
        user32.GetDpiForSystem.restype = wintypes.UINT
        value = int(user32.GetDpiForSystem())
        if value > 0:
            return value
    except Exception:
        pass
    try:
        dc = user32.GetDC(None)
        if dc:
            try:
                value = int(gdi32.GetDeviceCaps(dc, LOGPIXELSX))
                if value > 0:
                    return value
            finally:
                user32.ReleaseDC(None, dc)
    except Exception:
        pass
    return 96


# ---------------------------------------------------------------------------
# 窗口查询
# ---------------------------------------------------------------------------


def get_window_title(hwnd: int) -> str:
    if not hwnd or not IS_WINDOWS:
        return ""
    length = user32.GetWindowTextLengthW(HWND(hwnd))
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(HWND(hwnd), buffer, len(buffer))
    return buffer.value.strip()


def get_process_path(hwnd: int) -> str:
    if not hwnd or not IS_WINDOWS:
        return ""
    pid = DWORD(0)
    user32.GetWindowThreadProcessId(HWND(hwnd), ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
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


def window_info(hwnd: int) -> dict[str, Any]:
    path = get_process_path(hwnd)
    return {
        "hwnd": int(hwnd or 0),
        "title": get_window_title(hwnd),
        "process_path": path,
        "process_name": Path(path).name if path else "",
    }


def _rect_dict(rect: RECT) -> dict[str, int]:
    return {
        "left": int(rect.left),
        "top": int(rect.top),
        "right": int(rect.right),
        "bottom": int(rect.bottom),
        "width": max(1, int(rect.right - rect.left)),
        "height": max(1, int(rect.bottom - rect.top)),
    }


def get_window_rect(hwnd: int) -> Optional[dict[str, int]]:
    """窗口外框（含标题栏与边框），用于兼容旧版保存的锚点。"""

    if not hwnd or not IS_WINDOWS:
        return None
    rect = RECT()
    if not user32.GetWindowRect(HWND(hwnd), ctypes.byref(rect)):
        return None
    return _rect_dict(rect)


def get_client_rect(hwnd: int) -> Optional[dict[str, int]]:
    """客户区在屏幕上的位置与大小；识别结果统一使用这个坐标系。"""

    if not hwnd or not IS_WINDOWS:
        return None
    rect = RECT()
    if not user32.GetClientRect(HWND(hwnd), ctypes.byref(rect)):
        return None
    origin = POINT(0, 0)
    if not user32.ClientToScreen(HWND(hwnd), ctypes.byref(origin)):
        return None
    width = max(1, int(rect.right - rect.left))
    height = max(1, int(rect.bottom - rect.top))
    return {
        "left": int(origin.x),
        "top": int(origin.y),
        "right": int(origin.x) + width,
        "bottom": int(origin.y) + height,
        "width": width,
        "height": height,
    }


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and IS_WINDOWS and bool(user32.IsWindow(HWND(hwnd)))


def is_minimized(hwnd: int) -> bool:
    return is_window(hwnd) and bool(user32.IsIconic(HWND(hwnd)))


def is_maximized(hwnd: int) -> bool:
    return is_window(hwnd) and bool(user32.IsZoomed(HWND(hwnd)))


def foreground_window() -> int:
    if not IS_WINDOWS:
        return 0
    return int(user32.GetForegroundWindow() or 0)


def point_to_root_window(x: int, y: int) -> int:
    _require()
    child = int(user32.WindowFromPoint(POINT(int(x), int(y))) or 0)
    if not child:
        return 0
    return int(user32.GetAncestor(HWND(child), GA_ROOT) or child)


def root_window(hwnd: int) -> int:
    """把子窗口句柄换成带标题栏的顶层窗口。

    Tk 的 winfo_id() 返回的是子窗口，对它调用 SetWindowPos 只能改变
    子窗口在父窗口内的层级，置顶会失效。
    """

    if not IS_WINDOWS or not hwnd:
        return int(hwnd or 0)
    try:
        root = int(user32.GetAncestor(HWND(int(hwnd)), GA_ROOT) or 0)
    except Exception:
        return int(hwnd)
    return root or int(hwnd)


def make_click_through(hwnd: int) -> bool:
    """把窗口变成不抢焦点、不接收鼠标的浮层。

    只用颜色键做透明时，画上去的框线仍然会挡住点击；加上 WS_EX_TRANSPARENT
    之后整个窗口对鼠标完全透明，自动化点击才能穿透到目标应用。
    """

    if not IS_WINDOWS or not hwnd:
        return False
    try:
        getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        getter.argtypes = [HWND, ctypes.c_int]
        getter.restype = ctypes.c_ssize_t
        setter.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]
        setter.restype = ctypes.c_ssize_t
        style = int(getter(HWND(int(hwnd)), GWL_EXSTYLE))
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        setter(HWND(int(hwnd)), GWL_EXSTYLE, ctypes.c_ssize_t(style))
        return True
    except Exception:
        return False


def enumerate_windows() -> list[dict[str, Any]]:
    _require()
    windows: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if user32.IsWindowVisible(hwnd):
                rect = get_window_rect(hwnd)
                if rect and rect["width"] >= 320 and rect["height"] >= 240:
                    info = window_info(hwnd)
                    if info["title"]:
                        info["rect"] = rect
                        windows.append(info)
        except Exception:
            pass
        return True

    user32.EnumWindows(callback, 0)
    return windows


def parse_keywords(text: str) -> list[str]:
    parts = re.split(r"[,，;；\s]+", text or "")
    return [part.strip().lower() for part in parts if part.strip()]


def _looks_like_self(info: dict[str, Any]) -> bool:
    title = str(info.get("title") or "").lower()
    return any(token in title for token in SELF_TITLE_TOKENS)


def find_target_window(
    keywords: list[str],
    exclude_hwnd: int = 0,
    title_hint: str = "",
    process_hint: str = "",
) -> Optional[dict[str, Any]]:
    """按关键词、进程名和当前前台状态打分，返回最可能的目标窗口。"""

    _require()
    keywords = [kw for kw in (keywords or []) if kw]
    if not keywords:
        return None

    active = foreground_window()
    title_hint = (title_hint or "").strip().lower()
    process_hint = (process_hint or "").strip().lower()
    candidates: list[tuple[int, dict[str, Any]]] = []

    for info in enumerate_windows():
        hwnd = info["hwnd"]
        if not hwnd or hwnd == exclude_hwnd or _looks_like_self(info):
            continue
        title = str(info.get("title") or "").lower()
        process = str(info.get("process_name") or "").lower()

        score = 0
        for keyword in keywords:
            if keyword in title:
                score += 110
            if keyword in process:
                score += 110
        if score == 0:
            continue
        if title_hint and title_hint in title:
            score += 45
        if process_hint and process_hint in process:
            score += 45
        if hwnd == active:
            score += 30
        candidates.append((score, info))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# 窗口激活与几何
# ---------------------------------------------------------------------------


def capture_window_geometry(hwnd: int) -> Optional[dict[str, Any]]:
    rect = get_window_rect(hwnd)
    if rect is None:
        return None
    return {
        "left": rect["left"],
        "top": rect["top"],
        "width": rect["width"],
        "height": rect["height"],
        "maximized": is_maximized(hwnd),
    }


def restore_window_geometry(hwnd: int, geometry: Optional[dict[str, Any]]) -> None:
    if not geometry or not is_window(hwnd):
        return
    try:
        if is_minimized(hwnd):
            user32.ShowWindow(HWND(hwnd), SW_RESTORE)
        if bool(geometry.get("maximized", False)):
            user32.ShowWindow(HWND(hwnd), SW_MAXIMIZE)
            return
        if is_maximized(hwnd):
            user32.ShowWindow(HWND(hwnd), SW_RESTORE)
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
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    except (KeyError, TypeError, ValueError):
        return


def activate_window(hwnd: int, geometry: Optional[dict[str, Any]] = None) -> bool:
    """把目标窗口切到前台。返回是否真的成为前台窗口。"""

    if not is_window(hwnd):
        return False
    if geometry:
        restore_window_geometry(hwnd, geometry)
    elif is_minimized(hwnd):
        user32.ShowWindow(HWND(hwnd), SW_RESTORE)

    # 模拟一次 Alt 可以解除 Windows 的前台切换限制。
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.BringWindowToTop(HWND(hwnd))
    user32.SetForegroundWindow(HWND(hwnd))
    time.sleep(0.18)
    if geometry and bool(geometry.get("maximized", False)) and not is_maximized(hwnd):
        user32.ShowWindow(HWND(hwnd), SW_MAXIMIZE)
        time.sleep(0.12)
    return foreground_window() == int(hwnd)


def ensure_foreground(hwnd: int, geometry: Optional[dict[str, Any]] = None, attempts: int = 4) -> bool:
    for index in range(max(1, attempts)):
        if foreground_window() == int(hwnd):
            return True
        activate_window(hwnd, geometry if index == 0 else None)
        if foreground_window() == int(hwnd):
            return True
        time.sleep(0.15)
    return foreground_window() == int(hwnd)


def set_topmost(hwnd: int, enabled: bool) -> None:
    if not IS_WINDOWS or not hwnd:
        return
    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
    target = HWND_TOPMOST if enabled else HWND_NOTOPMOST
    user32.SetWindowPos(HWND(hwnd), HWND(target), 0, 0, 0, 0, flags)


def set_dark_frame(hwnd: int, enabled: bool) -> None:
    """让标题栏跟随主题。只在 Windows 10 1809 及以上有效，失败时静默忽略。"""

    if not IS_WINDOWS or not hwnd:
        return
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        return
    try:
        # 不声明 argtypes 时句柄会按 32 位整数传递，64 位下会截断。
        dwmapi.DwmSetWindowAttribute.argtypes = [
            HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    except AttributeError:
        return
    value = ctypes.c_int(1 if enabled else 0)
    applied = False
    # 20 是正式常量，19 用于 1809 之前的预览版本。
    for attribute in (DWMWA_USE_IMMERSIVE_DARK_MODE, 19):
        try:
            result = dwmapi.DwmSetWindowAttribute(
                HWND(int(hwnd)),
                ctypes.c_uint(attribute),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
        except Exception:
            continue
        if result == 0:
            applied = True
            break
    if not applied:
        return
    try:
        user32.SetWindowPos(
            HWND(int(hwnd)),
            None,
            0,
            0,
            0,
            0,
            SWP_FRAMECHANGED | SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 鼠标与键盘
# ---------------------------------------------------------------------------


def get_cursor_pos() -> tuple[int, int]:
    _require()
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise Win32Error("无法读取鼠标位置。")
    return int(point.x), int(point.y)


def move_cursor(point: tuple[int, int]) -> None:
    _require()
    if not user32.SetCursorPos(int(point[0]), int(point[1])):
        raise Win32Error("无法移动鼠标到目标位置。")


def click(point: tuple[int, int]) -> None:
    _require()
    move_cursor(point)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    time.sleep(0.02)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


def scroll(point: tuple[int, int], notches: int) -> None:
    """在指定位置滚动滚轮。notches 为正表示向下滚。"""

    _require()
    move_cursor(point)
    time.sleep(0.05)
    # dwData 是 32 位无符号，负数要先按补码转过去
    delta = (-int(notches) * WHEEL_DELTA) & 0xFFFFFFFF
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, None)


def _key_down(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)


def _key_up(vk: int) -> None:
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def tap(vk: int) -> None:
    _key_down(vk)
    _key_up(vk)


def ctrl_v() -> None:
    _key_down(VK_CONTROL)
    _key_down(VK_V)
    _key_up(VK_V)
    _key_up(VK_CONTROL)


def ctrl_a() -> None:
    _key_down(VK_CONTROL)
    _key_down(VK_A)
    _key_up(VK_A)
    _key_up(VK_CONTROL)


def press_enter() -> None:
    tap(VK_RETURN)


def ctrl_k() -> None:
    """多数对话式应用用 Ctrl+K 唤出搜索。"""

    _key_down(VK_CONTROL)
    time.sleep(0.02)
    _key_down(VK_K)
    time.sleep(0.02)
    _key_up(VK_K)
    _key_up(VK_CONTROL)


def press_escape() -> None:
    tap(VK_ESCAPE)


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------


def get_clipboard_text() -> Optional[str]:
    if not IS_WINDOWS:
        return None
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.04)
    else:
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
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


def set_clipboard_text(text: str) -> None:
    _require()
    for _ in range(12):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise Win32Error("剪贴板正被其它程序占用，请稍后重试。")

    handle = None
    try:
        if not user32.EmptyClipboard():
            raise Win32Error("无法清空 Windows 剪贴板。")
        encoded = ctypes.create_unicode_buffer(text)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(encoded))
        if not handle:
            raise Win32Error("无法分配剪贴板内存。")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise Win32Error("无法锁定剪贴板内存。")
        try:
            ctypes.memmove(pointer, ctypes.addressof(encoded), ctypes.sizeof(encoded))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise Win32Error("无法写入 Windows 剪贴板。")
        handle = None  # 所有权已转移给系统
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------


def create_single_instance(name: str) -> tuple[Optional[Any], bool]:
    if not IS_WINDOWS:
        return None, False
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None, False
    return handle, ctypes.get_last_error() == ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------------------
# 屏幕捕获（GDI）
# ---------------------------------------------------------------------------


def capture_screen_bgra(x: int, y: int, width: int, height: int) -> bytes:
    """抓取屏幕矩形，返回自上而下的 BGRA 字节串。"""

    _require()
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        raise Win32Error("截图区域为空。")
    if width * height > 24_000_000:
        raise Win32Error("截图区域过大。")

    screen = user32.GetDC(None)
    if not screen:
        raise Win32Error("无法获取屏幕设备上下文。")
    memory = None
    bitmap = None
    old_object = None
    try:
        memory = gdi32.CreateCompatibleDC(screen)
        if not memory:
            raise Win32Error("无法创建兼容设备上下文。")
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        if not bitmap:
            raise Win32Error("无法创建兼容位图。")
        old_object = gdi32.SelectObject(memory, bitmap)
        if not gdi32.BitBlt(memory, 0, 0, width, height, screen, int(x), int(y), SRCCOPY):
            raise Win32Error("屏幕截图失败。")
        gdi32.SelectObject(memory, old_object)
        old_object = None

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # 负值表示自上而下
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        lines = gdi32.GetDIBits(
            memory,
            bitmap,
            0,
            height,
            ctypes.cast(buffer, ctypes.c_void_p),
            ctypes.byref(header),
            DIB_RGB_COLORS,
        )
        if lines == 0:
            raise Win32Error("读取位图数据失败。")
        return buffer.raw
    finally:
        if old_object:
            gdi32.SelectObject(memory, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
