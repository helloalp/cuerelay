# -*- coding: utf-8 -*-
"""系统托盘图标。

tkinter 没有托盘。Win32 的做法是 `Shell_NotifyIcon`，而它要求有一个窗口来
接收回调消息——所以这里自己开一个线程：建一个不可见的消息窗口、挂上托盘
图标、跑消息循环。

为什么不用主窗口当那个窗口：主窗口的消息过程归 Tk 管，从外面替换它等于和
Tk 抢消息，出问题的地方很难查。这里用一个独立的 message-only 窗口
（`HWND_MESSAGE`），它不在屏幕上、不在任务栏里，只用来收消息。

所有托盘操作（加图标、改提示、弹气泡、弹菜单）都必须在那个线程上做，
所以对外只暴露 `post()`：把活儿丢进队列，再用 PostThreadMessage 叫醒循环。
"""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

WM_APP = 0x8000
WM_TRAY = WM_APP + 1  # 托盘回调消息
WM_TRAY_TASK = WM_APP + 2  # 队列里有活儿
WM_NULL = 0x0000
WM_QUIT = 0x0012

WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

GWLP_WNDPROC = -4
HWND_MESSAGE = -3
SM_CXSMICON = 49
SM_CYSMICON = 50

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """shell32 的托盘数据结构。字段顺序不能改，cbSize 也要按这个结构算。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p
_user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
_user32.CallWindowProcW.restype = ctypes.c_ssize_t
_user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE,
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.LoadImageW.restype = wintypes.HANDLE
# 第二个参数是 MAKEINTRESOURCE(32512)：一个"值就是资源号"的指针，
# 所以按 LPVOID 收，而不是 LPCWSTR（那样 ctypes 会当成字符串去读内存）。
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
_user32.LoadIconW.restype = wintypes.HANDLE
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = ctypes.c_ssize_t
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.LPVOID,
]
_user32.TrackPopupMenu.restype = ctypes.c_int
_user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR]
_user32.AppendMenuW.restype = wintypes.BOOL
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.DestroyMenu.argtypes = [wintypes.HMENU]
_user32.DestroyMenu.restype = wintypes.BOOL
_user32.DestroyIcon.argtypes = [wintypes.HICON]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.restype = wintypes.BOOL
_kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL

# 菜单项：文字 + 回调；None 表示一条分隔线。
MenuItem = Optional[tuple[str, Callable[[], None]]]


class Tray:
    """一个托盘图标，外加它自己的消息线程。

    `menu_provider` 在托盘线程上被调用，返回当前菜单——所以它只能读状态、
    发事件，不能碰 tkinter。
    """

    def __init__(
        self,
        tooltip: str,
        on_activate: Callable[[], None],
        menu_provider: Callable[[], list[MenuItem]],
        icon_path: Optional[Path] = None,
    ) -> None:
        self._tooltip = str(tooltip or "CueRelay")[:120]
        self._on_activate = on_activate
        self._menu_provider = menu_provider
        self._icon_path = icon_path

        self._tasks: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready = threading.Event()
        self._hwnd: Optional[int] = None
        self._proc = None  # WNDPROC 回调必须一直持有，否则会被回收
        self._old_proc = None
        self._icon = None
        self._active = False
        self._error = ""

    # -- 生命周期 ------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> tuple[bool, str]:
        if self._active:
            return True, ""
        self._ready.clear()
        self._error = ""
        self._thread = threading.Thread(target=self._loop, name="cuerelay-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0):
            return False, "托盘线程没有响应"
        if not self._active:
            return False, self._error or "托盘图标创建失败"
        return True, ""

    def stop(self) -> None:
        thread_id = self._thread_id
        self._active = False
        if thread_id:
            _user32.PostThreadMessageW(wintypes.DWORD(thread_id), WM_QUIT, 0, 0)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(1.5)
        self._thread = None
        self._thread_id = None

    def post(self, work: Callable[[], None]) -> None:
        """把一件事排到托盘线程上做。线程不在时静默丢弃。"""

        thread_id = self._thread_id
        if not thread_id:
            return
        self._tasks.put(work)
        _user32.PostThreadMessageW(wintypes.DWORD(thread_id), WM_TRAY_TASK, 0, 0)

    # -- 对外动作 ------------------------------------------------------

    def set_tooltip(self, text: str) -> None:
        text = str(text or "CueRelay")[:120]
        self._tooltip = text
        self.post(lambda: self._modify(tip=text))

    def notify(self, title: str, text: str, level: str = "info") -> None:
        """弹一条气泡。标题 64 字、正文 256 字是系统给的上限。"""

        flags = {"info": NIIF_INFO, "warning": NIIF_WARNING, "error": NIIF_ERROR}.get(level, NIIF_INFO)
        title = str(title or "")[:60]
        text = str(text or "")[:240]
        self.post(lambda: self._modify(info=(title, text, flags)))

    # -- 线程内部 ------------------------------------------------------

    def _loop(self) -> None:
        self._thread_id = int(_kernel32.GetCurrentThreadId())
        try:
            hwnd = _user32.CreateWindowExW(
                0,
                "Message",
                "CueRelayTray",
                0,
                0,
                0,
                0,
                0,
                wintypes.HWND(HWND_MESSAGE),
                None,
                _kernel32.GetModuleHandleW(None),
                None,
            )
            if not hwnd:
                self._error = "创建托盘消息窗口失败"
                return
            self._hwnd = int(hwnd)
            self._proc = _WNDPROC(self._wndproc)
            self._old_proc = _user32.SetWindowLongPtrW(
                hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p)
            )
            self._icon = self._load_icon()
            if not self._add():
                self._error = "系统拒绝了托盘图标（通知区域可能没有运行）"
                return
            self._active = True
            # 图标已经在托盘上了才能放开 start()。这个必须在进消息循环之前，
            # 循环一进去就是阻塞的，等到退出才放开等于让调用方白等。
            self._ready.set()

            msg = wintypes.MSG()
            while True:
                result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                if msg.message == WM_TRAY_TASK:
                    self._drain()
                    continue
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._cleanup()
            self._active = False
            # 线程结束前一定要放开 start()，否则调用方要白等满 3 秒。
            self._ready.set()

    def _drain(self) -> None:
        while True:
            try:
                work = self._tasks.get_nowait()
            except queue.Empty:
                return
            try:
                work()
            except Exception:
                # 托盘线程挂了就再也不会有图标了，单条消息出错不能拖垮循环。
                pass

    def _load_icon(self):
        size_x = _user32.GetSystemMetrics(SM_CXSMICON) or 16
        size_y = _user32.GetSystemMetrics(SM_CYSMICON) or 16
        if self._icon_path is not None and Path(self._icon_path).exists():
            handle = _user32.LoadImageW(
                None,
                str(self._icon_path),
                IMAGE_ICON,
                size_x,
                size_y,
                LR_LOADFROMFILE,
            )
            if handle:
                return handle
        return _user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = wintypes.HWND(self._hwnd)
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icon
        data.szTip = self._tooltip
        return data

    def _add(self) -> bool:
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        return bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))

    def _modify(self, tip: Optional[str] = None, info: Optional[tuple[str, str, int]] = None) -> None:
        if not self._hwnd:
            return
        flags = 0
        data = self._data(0)
        if tip is not None:
            flags |= NIF_TIP
            data.szTip = tip
        if info is not None:
            title, text, level = info
            flags |= NIF_INFO
            data.szInfoTitle = title
            data.szInfo = text
            data.dwInfoFlags = level
        data.uFlags = flags
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def _cleanup(self) -> None:
        if self._hwnd:
            try:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
            except Exception:
                pass
        if self._icon:
            try:
                _user32.DestroyIcon(self._icon)
            except Exception:
                pass
            self._icon = None
        if self._hwnd:
            try:
                _user32.DestroyWindow(wintypes.HWND(self._hwnd))
            except Exception:
                pass
            self._hwnd = None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                try:
                    self._on_activate()
                except Exception:
                    pass
            elif event == WM_RBUTTONUP:
                self._show_menu()
            return 0
        return _user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

    def _show_menu(self) -> None:
        try:
            items = self._menu_provider() or []
        except Exception:
            items = []
        if not items:
            return
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        callbacks: dict[int, Callable[[], None]] = {}
        try:
            command = 1
            for item in items:
                if item is None:
                    _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                label, callback = item
                _user32.AppendMenuW(menu, MF_STRING, command, str(label))
                callbacks[command] = callback
                command += 1

            point = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            # 弹菜单前必须把窗口设成前台，否则点菜单外面它不会消失。
            _user32.SetForegroundWindow(wintypes.HWND(self._hwnd))
            choice = _user32.TrackPopupMenu(
                menu,
                TPM_RETURNCMD | TPM_RIGHTBUTTON,
                point.x,
                point.y,
                0,
                wintypes.HWND(self._hwnd),
                None,
            )
            # 这条空消息是微软文档里写明的补丁：不补的话菜单偶尔会残留。
            _user32.PostMessageW(wintypes.HWND(self._hwnd), WM_NULL, 0, 0)
        finally:
            _user32.DestroyMenu(menu)
        action = callbacks.get(int(choice)) if choice else None
        if action is not None:
            try:
                action()
            except Exception:
                pass
