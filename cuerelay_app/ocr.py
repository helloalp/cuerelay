# -*- coding: utf-8 -*-
"""用 Windows 自带的 OCR 引擎读取界面上的文字。

识别能力来自系统的 Windows.Media.Ocr（Windows 10 起自带，中英文识别包随语言安装）。
Python 侧不引入任何第三方依赖：把位图写成临时 PNG，交给一个常驻的 PowerShell
进程调用 WinRT，再把识别结果读回来。

常驻进程是为了速度：每次现启动 PowerShell 要 600ms 以上，常驻之后单次识别
只要 150ms 左右，界面识别才能做到"点之前先看一眼"这种节奏。

界面文字的价值在于它比像素可靠：输入框占位符、对话列表里的标题、消息区顶部
的会话名都能直接读到，不用去猜"这块面板是不是搜索框"。
"""

from __future__ import annotations

import atexit
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Optional, Sequence

from . import imaging

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_worker.ps1")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_END = "--END--"

DEFAULT_LANG = "zh-Hans-CN"
_available_cache: Optional[bool] = None


class OcrError(RuntimeError):
    pass


class OcrLine:
    """一条识别结果，坐标以传入位图的左上角为原点。"""

    __slots__ = ("text", "left", "top", "width", "height")

    def __init__(self, text: str, left: int, top: int, width: int, height: int) -> None:
        self.text = text
        self.left = int(left)
        self.top = int(top)
        self.width = int(width)
        self.height = int(height)

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"OcrLine({self.text!r}, {self.left},{self.top} {self.width}x{self.height})"


def _powershell() -> str:
    for name in ("powershell", "pwsh"):
        path = shutil.which(name)
        if path:
            return path
    fallback = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    if os.path.exists(fallback):
        return fallback
    raise OcrError("找不到 PowerShell，无法调用系统 OCR。")


class _Worker:
    """一个常驻的 PowerShell 进程，按行收发请求。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._broken = False

    def _start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            [
                _powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                _WORKER,
                "-Serve",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        self._queue = queue.Queue()
        reader = threading.Thread(target=self._pump, args=(self._proc, self._queue), daemon=True)
        reader.start()
        self._reader = reader

    @staticmethod
    def _pump(proc: subprocess.Popen, sink: "queue.Queue[Optional[str]]") -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                sink.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except Exception:
            pass
        sink.put(None)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write(b"QUIT\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def request(self, path: str, lang: str, timeout: float) -> list[str]:
        with self._lock:
            if self._broken:
                raise OcrError("OCR 进程不可用。")
            self._start()
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise OcrError("OCR 进程启动失败。")
            try:
                proc.stdin.write(f"{lang}|{path}\n".encode("utf-8"))
                proc.stdin.flush()
            except Exception as error:
                self._broken = True
                raise OcrError(f"OCR 进程写入失败：{error}") from error

            lines: list[str] = []
            while True:
                try:
                    item = self._queue.get(timeout=max(0.5, timeout))
                except queue.Empty:
                    self._broken = True
                    self.stop()
                    raise OcrError("OCR 超时。")
                if item is None:
                    self._broken = True
                    self.stop()
                    raise OcrError("OCR 进程意外退出。")
                if item == _END:
                    return lines
                lines.append(item)


_worker = _Worker()


def _shutdown() -> None:
    _worker.stop()


atexit.register(_shutdown)


def available() -> bool:
    """系统 OCR 是否可用。结果缓存，避免重复探测。"""

    global _available_cache
    if _available_cache is not None:
        return _available_cache
    if sys.platform != "win32" or not os.path.exists(_WORKER):
        _available_cache = False
        return False
    try:
        handle, path = tempfile.mkstemp(prefix="cuerelay_ocr_", suffix=".png")
        os.close(handle)
        try:
            from . import imaging as _imaging

            bitmap = _imaging.Bitmap(8, 8, b"\x00" * (8 * 8 * 4))
            png, _, _ = bitmap.to_png(max_width=1 << 16, max_height=1 << 16)
            with open(path, "wb") as stream:
                stream.write(png)
            lines = _worker.request(path, DEFAULT_LANG, 20.0)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        _available_cache = not any(line.startswith("NO_ENGINE") for line in lines)
    except Exception:
        _available_cache = False
    return _available_cache


def recognize(
    bitmap: imaging.Bitmap,
    lang: str = DEFAULT_LANG,
    timeout: float = 12.0,
) -> list[OcrLine]:
    """识别整张位图，返回按纵向排列的文字行。"""

    if not available():
        return []
    handle, path = tempfile.mkstemp(prefix="cuerelay_ocr_", suffix=".png")
    os.close(handle)
    try:
        png, _, _ = bitmap.to_png(max_width=1 << 16, max_height=1 << 16)
        with open(path, "wb") as stream:
            stream.write(png)
        raw_lines = _worker.request(path, lang, timeout)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    lines: list[OcrLine] = []
    for raw in raw_lines:
        if raw.startswith("NO_ENGINE") or raw.startswith("ERROR"):
            continue
        parts = raw.split("|", 4)
        if len(parts) != 5:
            continue
        try:
            left, top, width, height = (int(part) for part in parts[:4])
        except ValueError:
            continue
        content = parts[4].strip()
        if not content:
            continue
        lines.append(OcrLine(content, left, top, width, height))
    lines.sort(key=lambda line: (line.top, line.left))
    return lines


_PUNCT = re.compile(r"[\s\u3000·・.,:;!?/\\|_\-—–'\"“”‘’()\[\]{}<>《》【】、，。：；！？]+")


def normalize(text: str) -> str:
    """比对用：去掉空白和标点，统一大小写。"""

    return _PUNCT.sub("", str(text or "")).lower()


def similarity(first: str, second: str) -> float:
    """宽松比对，用于占位符这类固定文案（允许 OCR 把 with 读成 vvith）。"""

    left = normalize(first)
    right = normalize(second)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        short, long_ = (left, right) if len(left) <= len(right) else (right, left)
        return 0.86 + 0.14 * (len(short) / len(long_))
    import difflib

    return difflib.SequenceMatcher(None, left, right).ratio()


def title_similarity(first: str, second: str) -> float:
    """对话标题的严格比对。

    这里不能用编辑距离：`Helloalp Read 20261012 1` 和 `Helloalp Read 20261010 1`
    只差两个数字，字符相似度高达 0.90，可它们是两个不同的对话。
    只认"完全一致"和"一方完整包含另一方"（OCR 常常会在标题前面多认出一个图标字符）。
    """

    left = normalize(first)
    right = normalize(second)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        short, long_ = (left, right) if len(left) <= len(right) else (right, left)
        share = len(short) / max(1, len(long_))
        if share >= 0.6:
            return 0.88 + 0.12 * share
    return 0.0


def find_line(
    lines: Sequence[OcrLine],
    needle: str,
    threshold: float = 0.72,
    strict: bool = False,
) -> Optional[OcrLine]:
    """在一批识别结果里找最像 needle 的那一行。"""

    if not needle or not lines:
        return None
    score_of = title_similarity if strict else similarity
    best: Optional[OcrLine] = None
    best_score = 0.0
    for line in lines:
        score = score_of(line.text, needle)
        if score > best_score:
            best_score = score
            best = line
    if best is None or best_score < threshold:
        return None
    return best


def find_text(
    lines: Sequence[OcrLine],
    needle: str,
    threshold: float = 0.72,
) -> Optional[OcrLine]:
    """同 find_line，但优先要求目标文字完整出现，用于找固定文案（如占位符）。"""

    target = normalize(needle)
    if not target:
        return None
    for line in lines:
        if target in normalize(line.text):
            return line
    return find_line(lines, needle, threshold)
