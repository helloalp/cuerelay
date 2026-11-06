# -*- coding: utf-8 -*-
"""目标应用的生命周期：检测、启动、前置、关闭。

CueRelay 要替用户"把 Codex 拉起来再发消息"，就必须自己管应用的开关：

- Codex 是 MSIX 包，不能按 exe 路径直接启动（WindowsApps 目录受保护），
  要走 `shell:AppsFolder\\<包族名>!<AppId>`。包族名可以从进程路径反推：
  安装目录是 `<Name>_<Version>_<Arch>__<PublisherId>`，而包族名是
  `<Name>_<PublisherId>`，所以不需要额外查询。
- Electron 应用点关闭往往只是收进托盘，进程还在。要真的退出得再终止进程。
- 窗口可能被收进托盘（不可见但句柄还在），此时先 ShowWindow 再前置，
  比重新启动一个新实例快得多，也不会丢状态。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import winapi

WINDOWS_APPS = "windowsapps"


def _log_nothing(_level: str, _message: str) -> None:
    pass


# ---------------------------------------------------------------------------
# MSIX 辅助
# ---------------------------------------------------------------------------


def family_from_install_path(path: str) -> str:
    """从 MSIX 安装路径反推包族名。

    `...\\WindowsApps\\OpenAI.Codex_26.1029.7803.0_x64__2p2nqsd0c76g0\\app\\ChatGPT.exe`
    → `OpenAI.Codex_2p2nqsd0c76g0`
    """

    for part in Path(path or "").parts:
        if "__" not in part:
            continue
        head, publisher = part.rsplit("__", 1)
        if not publisher or "_" not in head:
            continue
        name = head.split("_", 1)[0]
        if name:
            return f"{name}_{publisher}"
    return ""


def aumid_from_install_path(path: str, app_id: str = "App") -> str:
    family = family_from_install_path(path)
    return f"{family}!{app_id}" if family else ""


# ---------------------------------------------------------------------------
# 应用描述
# ---------------------------------------------------------------------------


@dataclass
class AppSpec:
    """一个可开关的应用。path 用于桌面程序，aumid 用于 MSIX。"""

    key: str
    label: str
    process: str
    aumid: str = ""
    path: str = ""
    path_contains: str = ""
    app_id: str = "App"
    log: Any = field(default=_log_nothing, repr=False, compare=False)

    # -- 状态 ----------------------------------------------------------

    def processes(self) -> list[dict[str, Any]]:
        if not self.process:
            return []
        return winapi.find_processes(self.process, self.path_contains)

    def running(self) -> bool:
        return bool(self.processes())

    def windows(self, include_hidden: bool = True) -> list[dict[str, Any]]:
        if not self.process:
            return []
        found = winapi.windows_for_process(self.process, self.path_contains, include_hidden)
        # 可见的排前面：收进托盘时句柄还在，但用户要的是那个能看见的窗口。
        found.sort(key=lambda item: (not item.get("visible", True), -int(item["rect"]["width"])))
        return found

    def window(self, include_hidden: bool = False) -> Optional[dict[str, Any]]:
        found = self.windows(include_hidden)
        return found[0] if found else None

    def status(self) -> str:
        if self.window() is not None:
            return "运行中"
        if self.running():
            return "在后台"
        return "未运行"

    # -- 启动 ----------------------------------------------------------

    def launch_target(self) -> str:
        aumid = self.aumid or (aumid_from_install_path(self.path, self.app_id) if self.path else "")
        if aumid:
            return f"shell:AppsFolder\\{aumid}"
        return self.path

    def launch(self) -> bool:
        target = self.launch_target()
        if not target:
            self.log("error", f"没有配置 {self.label} 的启动方式。")
            return False
        if not winapi.shell_open(target):
            self.log("error", f"启动 {self.label} 失败：系统拒绝了这次启动请求。")
            return False
        self.log("info", f"已请求启动 {self.label}。")
        return True

    # -- 前置 ----------------------------------------------------------

    def ensure_visible(self, timeout: float = 40.0, poll: float = 0.5) -> Optional[dict[str, Any]]:
        """把应用拉到前台；没开就启动，收进托盘就恢复。

        返回最终拿到的窗口信息，失败返回 None。窗口已经存在时不会再启动
        一个新实例——那样会开出两个一模一样的窗口，反而更难收拾。
        """

        # 1. 已经有可见窗口：直接前置。
        window = self.window()
        if window is not None:
            if self._bring_forward(int(window["hwnd"])):
                return window
            self.log("warning", f"{self.label} 的窗口没能切到前台。")
            return None

        # 2. 进程在但窗口被收进托盘：恢复那个句柄，比重启快得多（实测 0.04 秒）。
        hidden = self.window(include_hidden=True)
        if hidden is not None:
            hwnd = int(hidden["hwnd"])
            self.log("info", f"{self.label} 在后台，正在恢复窗口。")
            winapi.show_window(hwnd, winapi.SW_SHOW)
            winapi.show_window(hwnd, winapi.SW_RESTORE)
            if self._bring_forward(hwnd):
                restored = winapi.window_info(hwnd)
                rect = winapi.get_window_rect(hwnd)
                if rect:
                    restored["rect"] = rect
                    restored["visible"] = True
                return restored
            self.log("warning", f"恢复了 {self.label} 的窗口，但没能切到前台。")
            return None

        # 3. 没窗口也没进程：启动它，然后等窗口出现。
        self.launch()
        deadline = time.time() + max(6.0, timeout)
        while time.time() < deadline:
            time.sleep(poll)
            window = self.window()
            if window is None:
                continue
            if self._bring_forward(int(window["hwnd"])):
                return window
        self.log("error", f"等了 {int(timeout)} 秒，{self.label} 的窗口还是没有出现。")
        return None

    @staticmethod
    def _bring_forward(hwnd: int, attempts: int = 2) -> bool:
        """切前台。刚 ShowWindow 出来的窗口有时第一次切不过去，隔一下再试。"""

        for index in range(max(1, attempts)):
            if winapi.ensure_foreground(hwnd):
                return True
            if index + 1 < attempts:
                time.sleep(0.35)
        return False

    # -- 关闭 ----------------------------------------------------------

    def close(self, force: bool = True, timeout: float = 8.0, poll: float = 0.4) -> bool:
        """关闭应用。先请求正常退出，进程还在就终止。

        Electron 应用的"关闭"通常只是收进托盘，进程和内存都还在，
        所以定时关闭必须能做到真的退出，否则这个功能没有意义。
        """

        if not self.running():
            return True

        for window in self.windows(include_hidden=True):
            winapi.close_window(int(window["hwnd"]))
        if self._wait_exit(min(timeout, 4.0), poll):
            return True

        if not force:
            return False
        pids = [int(item["pid"]) for item in self.processes()]
        if not pids:
            return True
        self.log("info", f"{self.label} 没有自己退出，正在结束 {len(pids)} 个进程。")
        for pid in pids:
            winapi.terminate_process(pid)
        return self._wait_exit(timeout, poll)

    def _wait_exit(self, timeout: float, poll: float) -> bool:
        deadline = time.time() + max(0.4, timeout)
        while time.time() < deadline:
            if not self.running():
                return True
            time.sleep(poll)
        return not self.running()

    def wait_running(self, timeout: float = 25.0, poll: float = 0.4) -> bool:
        """等进程起来。托盘类程序（v2rayN）没有主窗口，只能看进程。"""

        deadline = time.time() + max(2.0, timeout)
        while time.time() < deadline:
            if self.running():
                return True
            time.sleep(poll)
        return self.running()


# ---------------------------------------------------------------------------
# 具体应用
# ---------------------------------------------------------------------------

# 已知的 Codex 包族名（从安装目录反推失败时的兜底）。
CODEX_AUMID = "OpenAI.Codex_2p2nqsd0c76g0!App"
CODEX_PROCESS = "ChatGPT"
V2RAY_PROCESS = "v2rayN"


def codex_app(settings: dict[str, Any], log: Any = _log_nothing) -> AppSpec:
    """Codex。AUMID 优先用设置里的，其次从运行中的进程路径反推。"""

    aumid = str(settings.get("codex_aumid") or "").strip()
    if not aumid:
        for item in winapi.find_processes(CODEX_PROCESS, WINDOWS_APPS):
            derived = aumid_from_install_path(str(item.get("path") or ""))
            if derived:
                aumid = derived
                break
    return AppSpec(
        key="codex",
        label="Codex",
        process=CODEX_PROCESS,
        aumid=aumid or CODEX_AUMID,
        path_contains=WINDOWS_APPS,
        log=log,
    )


def v2ray_app(settings: dict[str, Any], log: Any = _log_nothing) -> AppSpec:
    return AppSpec(
        key="v2ray",
        label="v2rayN",
        process=V2RAY_PROCESS,
        path=str(settings.get("v2ray_path") or "").strip(),
        log=log,
    )


# ---------------------------------------------------------------------------
# 自动探测
# ---------------------------------------------------------------------------

_SKIP_DIRS = {
    "windows",
    "windowsapps",
    "winsxs",
    "$recycle.bin",
    "system volume information",
    "node_modules",
    "appdata",
    "programdata",
}


def _registry_candidates(name_hint: str) -> list[str]:
    """从卸载表里找 DisplayIcon / InstallLocation。"""

    found: list[str] = []
    try:
        import winreg
    except ImportError:
        return found
    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, subkey in roots:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        child = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, child) as entry:
                            try:
                                display = str(winreg.QueryValueEx(entry, "DisplayName")[0] or "")
                            except OSError:
                                continue
                            if name_hint.lower() not in display.lower():
                                continue
                            for value in ("DisplayIcon", "InstallLocation"):
                                try:
                                    raw = str(winreg.QueryValueEx(entry, value)[0] or "")
                                except OSError:
                                    continue
                                raw = raw.strip().strip('"').split(",")[0]
                                if raw:
                                    found.append(raw)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _shallow_scan(filename: str, roots: list[Path], depth: int = 2) -> str:
    """在给定根目录下浅层找文件。深度小、跳过系统目录，不会拖太久。"""

    target = filename.lower()
    queue: list[tuple[Path, int]] = [(root, 0) for root in roots if root.is_dir()]
    while queue:
        folder, level = queue.pop(0)
        try:
            for entry in os.scandir(folder):
                try:
                    if entry.is_file() and entry.name.lower() == target:
                        return entry.path
                    if level < depth and entry.is_dir() and entry.name.lower() not in _SKIP_DIRS:
                        queue.append((Path(entry.path), level + 1))
                except OSError:
                    continue
        except OSError:
            continue
    return ""


def detect_v2ray_path() -> str:
    """找 v2rayN.exe。

    顺序：运行中的进程 → 注册表 → 常见目录 → 非系统盘浅扫。
    以管理员身份运行时普通权限读不到进程路径，所以不能只靠第一条。
    """

    for item in winapi.find_processes(V2RAY_PROCESS):
        path = str(item.get("path") or "")
        if path.lower().endswith(".exe") and Path(path).exists():
            return path

    for candidate in _registry_candidates("v2ray"):
        path = Path(candidate)
        if path.is_dir():
            exe = path / "v2rayN.exe"
            if exe.exists():
                return str(exe)
        elif path.name.lower() == "v2rayn.exe" and path.exists():
            return str(path)

    home = Path(os.environ.get("USERPROFILE") or "")
    local = Path(os.environ.get("LOCALAPPDATA") or "")
    common = [
        home / "Desktop",
        home / "Downloads",
        home / "Documents",
        local,
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        Path("C:/v2rayN"),
        Path("D:/v2rayN"),
        Path("E:/v2rayN"),
    ]
    for folder in common:
        if not folder.is_dir():
            continue
        exe = folder / "v2rayN.exe"
        if exe.exists():
            return str(exe)

    # 非系统盘浅扫：v2rayN 通常放在自建的 tools/programs 目录里。
    roots: list[Path] = []
    for drive in "DEFGH":
        root = Path(f"{drive}:/")
        if root.is_dir():
            roots.append(root)
    roots.extend([home / "Desktop", home / "Downloads"])
    return _shallow_scan("v2rayN.exe", roots, depth=2)


def looks_like_exe(path: str) -> bool:
    return bool(path) and path.lower().endswith(".exe") and Path(path).exists()


def ensure_proxy(
    settings: dict[str, Any],
    log: Any = _log_nothing,
    timeout: float = 25.0,
) -> tuple[bool, str]:
    """确保代理先起来。返回 (是否就绪, 说明)。

    v2rayN 通常需要管理员权限，普通权限启动会弹一次 UAC；这是系统行为，
    用户点过之后才真正开始监听端口，所以这里只等到进程出现，不去等窗口。
    """

    spec = v2ray_app(settings, log)
    if spec.status() != "未运行":
        return True, "v2rayN 已经在运行"
    if not spec.path:
        return False, "还没有绑定 v2rayN 的路径"
    if not spec.launch():
        return False, "启动 v2rayN 失败"
    if spec.wait_running(timeout):
        log("info", "v2rayN 已启动。")
        return True, "v2rayN 已启动"
    return False, "v2rayN 没有起来（可能需要管理员权限）"


def start_codex(settings: dict[str, Any], log: Any = _log_nothing, timeout: float = 40.0) -> Optional[dict[str, Any]]:
    """把 Codex 拉到前台：先确保代理，再确保窗口。"""

    if bool(settings.get("launch_v2ray_first", True)):
        ok, message = ensure_proxy(settings, log)
        if not ok:
            log("warning", f"代理没有就绪：{message}。仍尝试启动 Codex。")
    return codex_app(settings, log).ensure_visible(timeout)


def tidy_path(text: str) -> str:
    """用户手填的路径可能带引号、前后空格。"""

    value = str(text or "").strip().strip('"').strip("'")
    return re.sub(r"\s+", " ", value) if "\n" in value else value
