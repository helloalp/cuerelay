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
    user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
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
    user32.SwitchToThisWindow.argtypes = [HWND, wintypes.BOOL]
    user32.SwitchToThisWindow.restype = None
    user32.BringWindowToTop.argtypes = [HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    # 句柄在 64 位下是指针宽度。GetWindowLongW 的返回值同样是 LONG_PTR，
    # 不声明的话 ctypes 会按 32 位截断，样式位看着"读到了"其实是垃圾。
    _get_style = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    _get_style.argtypes = [HWND, ctypes.c_int]
    _get_style.restype = ctypes.c_ssize_t
    user32.InvalidateRect.argtypes = [HWND, ctypes.c_void_p, wintypes.BOOL]
    user32.InvalidateRect.restype = wintypes.BOOL
    user32.RedrawWindow.argtypes = [HWND, ctypes.c_void_p, wintypes.HANDLE, wintypes.UINT]
    user32.RedrawWindow.restype = wintypes.BOOL
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
    user32.PrintWindow.argtypes = [HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
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
    # 返回的是 HANDLE，不声明 restype 会在 64 位上被截断成 32 位。
    kernel32.CreateToolhelp32Snapshot.argtypes = [DWORD, DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL

    user32.PostMessageW.argtypes = [HWND, wintypes.UINT, WPARAM, LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.restype = wintypes.BOOL


# 常量集中放在这里，避免散落在各处。
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_MAXIMIZE = 3
GA_ROOT = 2
TH32CS_SNAPPROCESS = 0x00000002
WM_CLOSE = 0x0010
PROCESS_TERMINATE = 0x0001
VK_CONTROL = 0x11
VK_A = 0x41
VK_V = 0x56
VK_K = 0x4B
VK_B = 0x42
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_MENU = 0x12
VK_F8 = 0x77
VK_DELETE = 0x2E
VK_BACK = 0x08
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
# PrintWindow 的两种额外模式：只要客户区，以及让 Chromium/Electron 这类
# 自己画自己的窗口也走离屏渲染（否则拿到的是黑图）。
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
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
WS_EX_TOPMOST = 0x00000008
RDW_INVALIDATE = 0x0001
RDW_FRAME = 0x0400
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100


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


def window_class(hwnd: int) -> str:
    """窗口类名。标题会被用户改、被应用改，类名相对稳，用它认系统窗口。"""

    if not IS_WINDOWS or not hwnd:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(256)
        length = user32.GetClassNameW(HWND(int(hwnd)), buffer, 256)
        return str(buffer.value) if length else ""
    except Exception:
        return ""


def window_pid(hwnd: int) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    try:
        pid = DWORD()
        user32.GetWindowThreadProcessId(HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 进程快照与生命周期
# ---------------------------------------------------------------------------


def _snapshot_processes() -> list[dict[str, Any]]:
    """一次进程快照，只带 pid 与可执行文件名（不解析路径，快）。"""

    _require()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    processes: list[dict[str, Any]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            name = str(entry.szExeFile or "")
            processes.append(
                {
                    "pid": int(entry.th32ProcessID),
                    "name": name,
                    "stem": Path(name).stem.lower() if name else "",
                }
            )
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return processes


def find_processes(stem: str, path_contains: str = "") -> list[dict[str, Any]]:
    """按进程名（不含 .exe，忽略大小写）找进程，可选按路径片段过滤。

    以管理员身份运行的进程，普通权限读不到路径；这种情况 path 为空字符串，
    此时不做路径过滤——名字对上了就算对上。
    """

    target = (stem or "").strip().lower()
    if target.endswith(".exe"):
        target = target[:-4]
    if not target:
        return []
    needle = (path_contains or "").lower()
    found: list[dict[str, Any]] = []
    for entry in _snapshot_processes():
        if entry["stem"] != target:
            continue
        path = _process_path_by_pid(entry["pid"])
        if needle and path and needle not in path.lower():
            continue
        found.append({**entry, "path": path})
    return found


def process_running(stem: str, path_contains: str = "") -> bool:
    return bool(find_processes(stem, path_contains))


def _process_path_by_pid(pid: int) -> str:
    if not pid or not IS_WINDOWS:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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


def terminate_process(pid: int, exit_code: int = 1) -> bool:
    if not pid or not IS_WINDOWS:
        return False
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, exit_code))
    finally:
        kernel32.CloseHandle(handle)


def close_window(hwnd: int) -> bool:
    """请求窗口正常关闭。很多应用会把它当成"收进托盘"而不是退出。"""

    if not hwnd or not IS_WINDOWS:
        return False
    return bool(user32.PostMessageW(HWND(hwnd), WM_CLOSE, 0, 0))


def show_window(hwnd: int, command: int = SW_SHOW) -> bool:
    if not hwnd or not IS_WINDOWS:
        return False
    return bool(user32.ShowWindow(HWND(hwnd), command))


def is_window_visible(hwnd: int) -> bool:
    if not hwnd or not IS_WINDOWS:
        return False
    return bool(user32.IsWindowVisible(HWND(hwnd)))


def windows_for_process(
    stem: str,
    path_contains: str = "",
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    """某个进程的顶层窗口。默认只要可见的；include_hidden 时把收进托盘的也算上。"""

    _require()
    pids = {int(item["pid"]) for item in find_processes(stem, path_contains)}
    if not pids:
        return []
    found: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            pid = DWORD(0)
            user32.GetWindowThreadProcessId(HWND(hwnd), ctypes.byref(pid))
            if int(pid.value) not in pids:
                return True
            visible = bool(user32.IsWindowVisible(HWND(hwnd)))
            cloaked = is_cloaked(hwnd)
            # 遮蔽的窗口"可见"但屏幕上没有，按隐藏处理：这样状态会显示成
            # 「在后台」，恢复逻辑也会走 ShowWindow 而不是直接切前台。
            if not include_hidden and (not visible or cloaked):
                return True
            if not get_window_title(hwnd):
                return True
            rect = get_window_rect(hwnd)
            if rect is None or rect["width"] < 320 or rect["height"] < 240:
                return True
            info = window_info(hwnd)
            info["rect"] = rect
            info["visible"] = bool(visible and not cloaked)
            info["cloaked"] = bool(cloaked)
            found.append(info)
        except Exception:
            pass
        return True

    user32.EnumWindows(callback, 0)
    return found


def shell_open(target: str, verb: str = "open") -> bool:
    """用系统默认方式打开路径或 shell: 地址。返回值 >32 才算成功。"""

    _require()
    if not target:
        return False
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = shell32.ShellExecuteW(None, verb, target, None, None, SW_SHOWNORMAL)
    code = ctypes.cast(result, ctypes.c_void_p).value or 0
    return int(code) > 32


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


# DwmGetWindowAttribute 的 DWMWA_CLOAKED。
# 被 DWM 遮蔽的窗口（最小化到托盘、在别的虚拟桌面上、UWP 挂起）依然
# IsWindowVisible 为真、依然有正常矩形，但屏幕上根本没有它。截屏会截到
# 它背后的桌面，点下去就是点在壁纸上——这是"看起来执行了其实没执行"的
# 另一种来源。
DWMWA_CLOAKED = 14


def is_cloaked(hwnd: int) -> bool:
    if not is_window(hwnd):
        return False
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        value = ctypes.c_int(0)
        result = dwmapi.DwmGetWindowAttribute(
            HWND(int(hwnd)),
            ctypes.c_uint(DWMWA_CLOAKED),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if result != 0:
            return False
        return bool(value.value)
    except Exception:
        # 查不到就当作没被遮蔽：宁可让后续校验去发现异常，也不要凭空拒绝。
        return False


def is_foreground_usable(hwnd: int) -> bool:
    """窗口此刻是否真的在屏幕上、能截到内容。"""

    return is_window(hwnd) and is_window_visible(hwnd) and not is_cloaked(hwnd)


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
        # 被 DWM 遮蔽的窗口在屏幕上不存在，截屏只会截到壁纸。它仍然算
        # "进程在跑"，但绝不能当成可操作的目标。
        if is_cloaked(hwnd):
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
        # 工具窗口通常是应用自己开的辅助面板（浮窗、取词条之类），不是
        # 用户正在用的那个窗口。同分时它应该排在后面。
        if is_tool_window(hwnd):
            score -= 60
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
    if is_cloaked(hwnd):
        # 被 DWM 遮蔽（收进托盘、在别的虚拟桌面）：先让它真的回到屏幕上。
        # 不先做这一步，后面的 SetForegroundWindow 会"成功"但用户看不见。
        user32.ShowWindow(HWND(hwnd), SW_SHOW)
        user32.ShowWindow(HWND(hwnd), SW_RESTORE)
        time.sleep(0.15)
    if geometry:
        restore_window_geometry(hwnd, geometry)
    elif is_minimized(hwnd):
        user32.ShowWindow(HWND(hwnd), SW_RESTORE)

    # 以前这里会注入一次 Alt 来绕过 Windows 的前台切换限制。那个做法有代价：
    # 全局按下的 Alt 会落到"当前前台的那个程序"身上，运气不好就打开了它的
    # 菜单栏，把用户正在输入的东西打断。改用 SetForegroundWindow + 兜底的
    # SwitchToThisWindow，只影响我们自己的窗口。
    user32.BringWindowToTop(HWND(hwnd))
    if not user32.SetForegroundWindow(HWND(hwnd)):
        try:
            user32.SwitchToThisWindow(HWND(hwnd), True)
        except Exception:
            pass
    time.sleep(0.12)
    if geometry and bool(geometry.get("maximized", False)) and not is_maximized(hwnd):
        user32.ShowWindow(HWND(hwnd), SW_MAXIMIZE)
        time.sleep(0.12)
    return foreground_window() == int(hwnd)


def ensure_foreground(hwnd: int, geometry: Optional[dict[str, Any]] = None, attempts: int = 4) -> bool:
    """切到前台并确认它真的在屏幕上。

    只看 GetForegroundWindow 不够：被 DWM 遮蔽的窗口也能成为"前台窗口"，
    但屏幕上什么都没有，截屏截到的是壁纸。
    """

    for index in range(max(1, attempts)):
        if foreground_window() == int(hwnd) and not is_cloaked(hwnd):
            return True
        activate_window(hwnd, geometry if index == 0 else None)
        if foreground_window() == int(hwnd) and not is_cloaked(hwnd):
            return True
        time.sleep(0.15)
    return foreground_window() == int(hwnd) and not is_cloaked(hwnd)


def set_topmost(hwnd: int, enabled: bool) -> None:
    if not IS_WINDOWS or not hwnd:
        return
    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
    target = HWND_TOPMOST if enabled else HWND_NOTOPMOST
    user32.SetWindowPos(HWND(hwnd), HWND(target), 0, 0, 0, 0, flags)


def _ex_style(hwnd: int) -> int:
    """读扩展样式。读不到就返回 0——调用方只关心"有没有某一位"。"""

    if not IS_WINDOWS or not hwnd:
        return 0
    try:
        getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        return int(getter(HWND(int(hwnd)), GWL_EXSTYLE))
    except Exception:
        return 0


def is_topmost(hwnd: int) -> bool:
    """窗口是不是被钉在了最上层。

    这是"点不着"最常见的原因：置顶窗口即使没有画出来，点击也会先落到它身上，
    下面的程序看起来一切正常，就是不理人。
    """

    return bool(_ex_style(hwnd) & WS_EX_TOPMOST)


def is_tool_window(hwnd: int) -> bool:
    return bool(_ex_style(hwnd) & WS_EX_TOOLWINDOW)


def repair_window(hwnd: int, nudge: bool = True) -> dict[str, bool]:
    """把"看着在、点不着"的窗口拉回正常状态。

    典型症状：窗口 IsWindowVisible 为真、矩形正常、甚至还是前台窗口，但屏幕上
    画的是别的东西（或者干脆没有），点击却全被它吃掉——因为 DWM 认为它在最上层。

    三步：取消置顶、恢复显示、强制重绘。最后"抖一下尺寸"是关键：Chromium 只有
    收到尺寸变化才会重新布局并重画，单纯 InvalidateRect 对它是没用的。
    """

    result = {"topmost": False, "restored": False, "repainted": False}
    if not IS_WINDOWS or not hwnd:
        return result
    hwnd = int(hwnd)
    if not is_window(hwnd):
        return result
    try:
        if is_topmost(hwnd):
            # 置顶的窗口永远盖在所有程序上面，点击自然落不到下面那个程序。
            user32.SetWindowPos(
                HWND(hwnd),
                HWND(HWND_NOTOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            result["topmost"] = True
        if is_minimized(hwnd) or is_cloaked(hwnd):
            user32.ShowWindow(HWND(hwnd), SW_SHOW)
            user32.ShowWindow(HWND(hwnd), SW_RESTORE)
            result["restored"] = True
        user32.SetWindowPos(
            HWND(hwnd),
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        user32.InvalidateRect(HWND(hwnd), None, True)
        user32.RedrawWindow(
            HWND(hwnd),
            None,
            None,
            RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW | RDW_FRAME,
        )
        result["repainted"] = True
        rect = get_window_rect(hwnd)
        # 最大化时改尺寸会把它变成"普通窗口"，那等于改了用户的工作区。
        if nudge and rect and not is_maximized(hwnd):
            user32.SetWindowPos(
                HWND(hwnd),
                0,
                0,
                0,
                rect["width"] + 1,
                rect["height"],
                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
            time.sleep(0.06)
            user32.SetWindowPos(
                HWND(hwnd),
                0,
                rect["left"],
                rect["top"],
                rect["width"],
                rect["height"],
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
    except Exception:
        pass
    return result


# 外壳自己画的置顶窗口：任务栏、桌面、缩略图预览、托盘溢出面板。它们本来就该
# 在最上层，取消置顶只会把系统界面弄坏，所以扫描时要跳过。
SHELL_CLASSES = frozenset(
    {
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "Progman",
        "WorkerW",
        "TaskListThumbnailWnd",
        "NotifyIconOverflowWindow",
        "TopLevelWindowForOverflowXamlIsland",
        "XamlExplorerHostIslandWindow",
        "MultitaskingViewFrame",
        "ForegroundStaging",
        "Windows.UI.Input.InputSite.WindowClass",
    }
)


def topmost_windows(exclude_pid: int = 0) -> list[dict[str, Any]]:
    """列出正压在别人头上的窗口。

    判据是 WS_EX_TOPMOST 且真的在屏幕上（没最小化、没被 DWM 遮蔽）。这是
    「鼠标能动能点别处、就是点不动某个程序」最常见的成因：一个置顶窗口挡在
    中间，它自己没画出来，点击却全被它吃掉。
    """

    _require()
    found: list[dict[str, Any]] = []
    for info in enumerate_windows():
        hwnd = info["hwnd"]
        if not hwnd or is_cloaked(hwnd) or is_minimized(hwnd):
            continue
        if not is_topmost(hwnd):
            continue
        class_name = window_class(hwnd)
        if class_name in SHELL_CLASSES:
            continue
        # 自己的窗口永远不是嫌疑人——程序不能指控自己。
        if _looks_like_self(info):
            continue
        pid = window_pid(hwnd)
        if exclude_pid and pid == exclude_pid:
            continue
        found.append(
            {
                "hwnd": hwnd,
                "title": info.get("title") or "",
                "process_name": info.get("process_name") or "",
                "class_name": class_name,
                "pid": pid,
                "rect": get_window_rect(hwnd),
                "tool": is_tool_window(hwnd),
            }
        )
    # 覆盖面积大的排前面：全屏的幽灵比一个小浮窗更可能是元凶。
    found.sort(key=lambda item: -((item["rect"] or {}).get("width", 0) * (item["rect"] or {}).get("height", 0)))
    return found


def repair_topmost(exclude_pid: int = 0) -> list[dict[str, Any]]:
    """把所有可疑的置顶窗口拉回来，返回真正改过的那些。"""

    changed: list[dict[str, Any]] = []
    for item in topmost_windows(exclude_pid):
        if repair_window(item["hwnd"]).get("topmost"):
            item["result"] = "已取消置顶"
            changed.append(item)
    return changed


def repaint_window(hwnd: int) -> bool:
    """立刻把窗口重画一遍。

    withdraw 之后马上 deiconify，Windows 有时只把边框画出来，客户区留白，
    要等下一次输入才补画。这里主动发一次重绘，不等用户去点。
    """

    if not IS_WINDOWS or not hwnd or not is_window(hwnd):
        return False
    try:
        user32.SetWindowPos(
            HWND(int(hwnd)),
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        user32.InvalidateRect(HWND(int(hwnd)), None, True)
        user32.RedrawWindow(
            HWND(int(hwnd)),
            None,
            None,
            RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW | RDW_FRAME,
        )
        return True
    except Exception:
        return False


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


def ctrl_b() -> None:
    """多数桌面应用用 Ctrl+B 收起 / 展开左侧栏。"""

    _key_down(VK_CONTROL)
    time.sleep(0.02)
    _key_down(VK_B)
    time.sleep(0.02)
    _key_up(VK_B)
    _key_up(VK_CONTROL)


def press_escape() -> None:
    tap(VK_ESCAPE)


def press_delete() -> None:
    tap(VK_DELETE)


def select_all_and_delete() -> None:
    """清空当前输入框。用 Ctrl+A + Delete：Backspace 在多行输入框里
    只会退一格，Delete 才是"删掉选中内容"。"""

    ctrl_a()
    time.sleep(0.05)
    press_delete()


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


def capture_window_bgra(hwnd: int, width: int, height: int) -> bytes:
    """让窗口把自己画到一张离屏位图上，返回 BGRA 字节串。

    和截屏的区别：窗口被别的程序挡住、甚至被压在最下面，这里拿到的仍然是
    它自己的画面。以前只截屏，一旦目标窗口没真的在最上面，截到的就是挡在
    它前面的那个程序——OCR 出来的标题全是别人的对话，滚动也滚错了地方。
    """

    _require()
    width = int(width)
    height = int(height)
    if not hwnd or width <= 0 or height <= 0:
        raise Win32Error("窗口区域为空。")
    if width * height > 24_000_000:
        raise Win32Error("窗口区域过大。")

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
        flags = PW_CLIENTONLY | PW_RENDERFULLCONTENT
        if not user32.PrintWindow(HWND(int(hwnd)), memory, flags):
            if not user32.PrintWindow(HWND(int(hwnd)), memory, PW_CLIENTONLY):
                raise Win32Error("窗口拒绝离屏渲染。")
        gdi32.SelectObject(memory, old_object)
        old_object = None
        return _dib_bgra(memory, bitmap, width, height)
    finally:
        if old_object:
            gdi32.SelectObject(memory, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)


def _dib_bgra(memory, bitmap, width: int, height: int) -> bytes:
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
        return _dib_bgra(memory, bitmap, width, height)
    finally:
        if old_object:
            gdi32.SelectObject(memory, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory:
            gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
