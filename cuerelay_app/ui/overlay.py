# -*- coding: utf-8 -*-
"""识别与执行过程的浮层。

在目标窗口上方盖一层透明、可穿透鼠标的浮层，把"程序现在在干什么"画出来：
正在哪一块里找、认准了哪个元素、这一步是第几步、走到了哪里。

浮层用颜色键做透明：整块画布填一个几乎不会出现在界面里的颜色，
Tk 会把这个颜色整片抠掉；再补上 WS_EX_TRANSPARENT，鼠标点击会完全穿透，
不会影响自动化自己的点击。

2.11 起把"找"和"找到"拆成两种视觉——以前只有一种框，认出来了和还在找
长得一模一样，用户没法判断"它是不是卡住了"：

  扫描   四条很淡的角标 + 一道来回扫的光带 + 转圈的标签，说明还在找；
  锁定   角标从外面收拢到元素上，落定闪一下，标签才浮出来；
  进度   卡片贴着当前那个框，写着第几步、在做什么、走了多远。

颜色键只能做"整片透明"，做不了半透明：任何掺过色的像素都是不透明的。
所以淡出只能用在细线和文字上（它们在深色界面上看起来像淡出），
大面积色块一律实色——把底色往键色掺只会得到一块更黑的色块。
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from typing import Any, Optional

from .. import winapi
from . import anim
from .theme import Theme

# 颜色键：纯黑或纯白都可能出现在界面上，用一个偏色值最安全。
KEY_COLOR = "#010203"

KIND_COLORS = {
    "composer": "accent",
    "send": "success",
    "search": "warning",
    "frame": "border_strong",
}

STEP_LABELS = {
    "locate": "定位输入框",
    "switch": "切换对话",
    "paste": "粘贴提示词",
    "verify_paste": "校验粘贴",
    "send": "发送",
    "verify_send": "校验发送",
}

# 收拢动画的时长。必须短于 Runner.BOX_DWELL（0.3 秒）：框还没停稳就去截图，
# 会把动画中途的线一起拍进去，识别和像素比对都会跟着抖。
ACQUIRE_SECONDS = 0.24
MORPH_SECONDS = 0.16
PULSE_SECONDS = 0.42
# 落定之后那一下闪：框已经贴住了，再亮一下告诉眼睛"就是它"。
FLASH_SECONDS = 0.34
BAR_SECONDS = 0.28
CARD_SECONDS = 0.22
# 光带扫一个来回要多久。太快像故障，太慢像卡住。
SCAN_SECONDS = 1.5

# 收拢 + 闪都走完，画面才算静止。Runner 拿这个值决定"什么时候可以截图比对"。
SETTLE_SECONDS = ACQUIRE_SECONDS + FLASH_SECONDS


class _Box:
    """一个被框住的元素，以及它自己的动画状态。"""

    __slots__ = ("rect", "kind", "label", "born", "morph_from", "previous")

    def __init__(self, rect: dict[str, int], kind: str, label: str) -> None:
        self.rect = dict(rect)
        self.kind = kind
        self.label = label
        self.born = time.monotonic()
        self.morph_from: Optional[dict[str, int]] = None
        self.previous: Optional[dict[str, int]] = None

    def tuple(self) -> tuple[float, float, float, float]:
        return (
            float(self.rect.get("left", 0)),
            float(self.rect.get("top", 0)),
            float(self.rect.get("right", 0)),
            float(self.rect.get("bottom", 0)),
        )

    def from_tuple(self) -> tuple[float, float, float, float]:
        source = self.morph_from or self.rect
        return (
            float(source.get("left", 0)),
            float(source.get("top", 0)),
            float(source.get("right", 0)),
            float(source.get("bottom", 0)),
        )

    def age(self) -> float:
        return max(0.0, time.monotonic() - self.born)

    def settled(self) -> bool:
        return self.age() >= (MORPH_SECONDS if self.morph_from else ACQUIRE_SECONDS)

    def flashing(self) -> bool:
        """刚落定的那一下还在闪。只有"新认出来"才闪，换目标不闪。"""

        if self.morph_from is not None:
            return False
        return ACQUIRE_SECONDS <= self.age() <= SETTLE_SECONDS


class _Scan:
    """一块正在被查找的区域。

    它和 _Box 是两件事：_Box 是"认准了"，_Scan 是"还在找"。混在一起会
    出现"框已经画好了但其实还没认出来"的错觉。
    """

    __slots__ = ("rect", "label", "tone", "born")

    def __init__(self, rect: dict[str, int], label: str, tone: str) -> None:
        self.rect = dict(rect)
        self.label = label
        self.tone = tone
        self.born = time.monotonic()

    def age(self) -> float:
        return max(0.0, time.monotonic() - self.born)


class Overlay:
    """覆盖在目标窗口客户区上的可视化浮层。"""

    def __init__(self, root: tk.Misc, theme: Theme) -> None:
        self.root = root
        self.theme = theme
        self.window: Optional[tk.Toplevel] = None
        self.canvas: Optional[tk.Canvas] = None
        self.visible = False
        self._rect: Optional[dict[str, int]] = None
        self._boxes: list[_Box] = []
        self._scans: list[_Scan] = []
        self._progress: Optional[tuple[int, int, str, str, str]] = None
        self._bar_from = 0.0
        self._bar_started = 0.0
        self._card_started = 0.0
        self._pulses: list[tuple[tuple[float, float], float]] = []
        self._after: Optional[str] = None

    # -- 生命周期 ------------------------------------------------------

    def _ensure(self) -> bool:
        if self.window is not None:
            try:
                if self.window.winfo_exists():
                    return True
            except tk.TclError:
                pass
            self.window = None
        try:
            window = tk.Toplevel(self.root)
            window.withdraw()
            window.overrideredirect(True)
            window.configure(bg=KEY_COLOR)
            window.attributes("-topmost", True)
            try:
                window.attributes("-transparentcolor", KEY_COLOR)
            except tk.TclError:
                pass
            canvas = tk.Canvas(window, bg=KEY_COLOR, highlightthickness=0, bd=0)
            canvas.pack(fill="both", expand=True)
        except tk.TclError:
            return False
        self.window = window
        self.canvas = canvas
        return True

    def show(self, rect: dict[str, int]) -> None:
        if not self._ensure() or self.window is None:
            return
        try:
            left = int(rect["left"])
            top = int(rect["top"])
            width = max(1, int(rect["width"]))
            height = max(1, int(rect["height"]))
        except (KeyError, TypeError, ValueError):
            return
        same_rect = self._rect == {
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }
        self._rect = {"left": left, "top": top, "width": width, "height": height}
        # 同一块客户区重复 show 时不要清空框线和进度，否则进度条会闪一下。
        if not same_rect:
            self._boxes = []
            self._scans = []
            self._progress = None
            self._pulses = []
        try:
            self.window.geometry(f"{width}x{height}+{left}+{top}")
            self.window.deiconify()
            self.window.lift()
            self.window.update_idletasks()
            hwnd = winapi.root_window(int(self.window.winfo_id()))
            winapi.make_click_through(hwnd)
        except tk.TclError:
            return
        self.visible = True
        self._render()
        self._schedule()

    def hide(self) -> None:
        self.visible = False
        self._boxes = []
        self._scans = []
        self._progress = None
        self._pulses = []
        self._stop()
        if self.window is None:
            return
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self.hide()
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
        self.window = None
        self.canvas = None

    def retheme(self, theme: Theme) -> None:
        self.theme = theme
        if self.visible:
            self._render()

    # -- 内容 ----------------------------------------------------------

    def scan(self, rect: dict[str, int], label: str = "识别中", tone: str = "warning") -> None:
        """标出"正在这一块里找"。

        同一时刻只保留一块：流水线是一步一步走的，同时扫两处只会让人
        分不清现在到底在找什么。锁定（box）时自动撤掉。
        """

        if not self.visible:
            return
        try:
            fresh = {
                "left": int(rect["left"]),
                "top": int(rect["top"]),
                "right": int(rect["right"]),
                "bottom": int(rect["bottom"]),
            }
        except (KeyError, TypeError, ValueError):
            return
        self._scans = [_Scan(fresh, label or "识别中", tone or "warning")]
        self._render()
        self._schedule()

    def box(self, rect: dict[str, int], kind: str, label: str = "") -> None:
        """框住一个元素。

        同一个位置重复框（比如每轮都重新识别输入框）不重播动画——那样框会
        一直抖。换了位置就从旧位置过渡过去：一眼能看出"它换目标了"。
        """

        if not self.visible:
            return
        try:
            fresh = {
                "left": int(rect["left"]),
                "top": int(rect["top"]),
                "right": int(rect["right"]),
                "bottom": int(rect["bottom"]),
            }
        except (KeyError, TypeError, ValueError):
            return
        # 认出来了，扫描就该收工——留着会让人以为还在找。
        self._scans = []
        existing = next((item for item in self._boxes if item.kind == kind), None)
        if existing is not None:
            same = all(abs(int(existing.rect.get(key, 0)) - fresh[key]) <= 2 for key in fresh)
            existing.label = label or existing.label
            if same:
                self._render()
                return
            existing.morph_from = dict(existing.rect)
            existing.rect = fresh
            existing.born = time.monotonic()
        else:
            self._boxes.append(_Box(fresh, kind, label))
        self._render()
        self._schedule()

    def progress(
        self, index: int, total: int, text: str, tone: str = "accent", detail: str = ""
    ) -> None:
        if not self.visible:
            return
        previous = self._progress[0] / max(1, self._progress[1]) if self._progress else 0.0
        old_text = (self._progress[2], self._progress[4]) if self._progress else ("", "")
        self._progress = (max(0, index), max(1, total), text, tone, detail)
        self._bar_from = previous
        self._bar_started = time.monotonic()
        # 只有文案变了才重播"浮上来"的动作；同一句话反复推进（bump=False）
        # 不该让卡片一跳一跳。
        if (text, detail) != old_text:
            self._card_started = time.monotonic()
        self._render()
        self._schedule()

    def clear_boxes(self) -> None:
        if not self.visible:
            return
        self._boxes = []
        self._scans = []
        self._render()

    def pulse(self, point: tuple[int, int]) -> None:
        """在点击的位置画一圈扩散的涟漪。

        点击是整条流水线里唯一一个"对外产生了影响"的动作，给它一个明确的
        视觉回执：点在哪、什么时候点的。
        """

        if not self.visible or self._rect is None:
            return
        local = (float(point[0]) - self._rect["left"], float(point[1]) - self._rect["top"])
        self._pulses.append((local, time.monotonic()))
        self._render()
        self._schedule()

    # -- 动画驱动 ------------------------------------------------------

    def _animating(self) -> bool:
        now = time.monotonic()
        if self._scans:
            return True
        if self._pulses:
            return True
        if any(not box.settled() or box.flashing() for box in self._boxes):
            return True
        if self._progress is not None:
            if (now - self._bar_started) < BAR_SECONDS:
                return True
            if (now - self._card_started) < CARD_SECONDS:
                return True
        return False

    def _interval(self) -> int:
        """只有扫描在跑的时候降到 30fps：光带是慢动作，没必要 60fps 重画。"""

        if self._pulses:
            return 16
        if any(not box.settled() or box.flashing() for box in self._boxes):
            return 16
        now = time.monotonic()
        if self._progress is not None and (
            (now - self._bar_started) < BAR_SECONDS or (now - self._card_started) < CARD_SECONDS
        ):
            return 16
        return 33

    def _schedule(self) -> None:
        if self._after is not None or self.window is None:
            return
        if not self._animating():
            return
        try:
            self._after = self.window.after(self._interval(), self._tick)
        except tk.TclError:
            self._after = None

    def _stop(self) -> None:
        if self._after is not None and self.window is not None:
            try:
                self.window.after_cancel(self._after)
            except Exception:
                pass
        self._after = None

    def _tick(self) -> None:
        self._after = None
        if not self.visible:
            return
        self._render()
        self._schedule()

    # -- 绘制 ----------------------------------------------------------

    def _local(self, rect: dict[str, int], pad: float = 0.0) -> tuple[float, float, float, float]:
        origin = self._rect
        if origin is None:
            return (0.0, 0.0, 0.0, 0.0)
        return (
            float(rect.get("left", 0)) - origin["left"] - pad,
            float(rect.get("top", 0)) - origin["top"] - pad,
            float(rect.get("right", 0)) - origin["left"] + pad,
            float(rect.get("bottom", 0)) - origin["top"] + pad,
        )

    def _render(self) -> None:
        canvas = self.canvas
        rect = self._rect
        if canvas is None or rect is None:
            return
        try:
            canvas.delete("all")
        except tk.TclError:
            return

        now = time.monotonic()
        # 过期的涟漪在这里清掉：留着会一直占着"还在动"的判断，让浮层空转。
        self._pulses = [item for item in self._pulses if now - item[1] <= PULSE_SECONDS]

        self._draw_frame(rect["width"], rect["height"])

        for scan in list(self._scans):
            self._draw_scan(scan)

        for box in list(self._boxes):
            self._draw_box(box)

        for center, started in list(self._pulses):
            self._draw_pulse(center, started)

        if self._progress is not None:
            self._draw_progress(*self._progress)

    def _draw_frame(self, width: int, height: int) -> None:
        """四角标记，而不是整圈边框。

        整圈边框会紧贴客户区上下边缘画一条线，识别算法会把它当成面板边缘，
        于是输入框被误判成"从顶部一直延伸到底部"。四角标记只占每条边最外侧
        一小段，落在识别采样带之外，既说明了"识别的是这一块"，又不会干扰识别。
        """

        canvas = self.canvas
        if canvas is None:
            return
        theme = self.theme
        color = theme.c("accent")
        arm = max(12, theme.px(28))
        weight = max(2, theme.px(2))
        right = width - 1
        bottom = height - 1
        try:
            canvas.create_line(0, 0, arm, 0, fill=color, width=weight)
            canvas.create_line(0, 0, 0, arm, fill=color, width=weight)
            canvas.create_line(right - arm, 0, right, 0, fill=color, width=weight)
            canvas.create_line(right, 0, right, arm, fill=color, width=weight)
            canvas.create_line(0, bottom, arm, bottom, fill=color, width=weight)
            canvas.create_line(0, bottom - arm, 0, bottom, fill=color, width=weight)
            canvas.create_line(right - arm, bottom, right, bottom, fill=color, width=weight)
            canvas.create_line(right, bottom - arm, right, bottom, fill=color, width=weight)
        except tk.TclError:
            return

    def _draw_scan(self, scan: _Scan) -> None:
        """还在找：淡角标 + 来回扫的光带 + 转圈的标签。

        刻意不画实心框——实心框是"认准了"的语言。还在找的时候只给出范围，
        让人知道"它正在这一块里翻"，而不是"它认成了这一大块"。
        """

        canvas = self.canvas
        if canvas is None or self._rect is None:
            return
        theme = self.theme
        age = scan.age()
        if age > 20.0:  # 卡住太久的扫描不再闪，免得一直占着画面
            return
        left, top, right, bottom = self._local(scan.rect, 0)
        if right - left < 60 or bottom - top < 40:
            return

        base = theme.c(scan.tone)
        breath = 0.5 + 0.5 * math.sin(age * 5.0)
        arm = float(max(10, min(26, int((right - left) * 0.05))))
        corner = anim.fade(base, 0.44 - 0.18 * breath, KEY_COLOR)
        # 往里收 1 像素再画角标：贴着窗口边缘画的话，外侧那一条会被裁掉一半，
        # 看起来像几粒脏点。
        try:
            for x1, y1, x2, y2 in anim.bracket_segments(
                (left + 1, top + 1, right - 1, bottom - 1), 1.0, spread=0.0, arm_range=(arm, arm)
            ):
                canvas.create_line(x1, y1, x2, y2, fill=corner, width=1)

            # 光带：一条亮的 + 两条渐隐的拖尾，方向感来自拖尾在身后。
            span = max(1.0, right - left)
            cycle = (age / SCAN_SECONDS) % 1.0
            t = cycle * 2.0 if cycle < 0.5 else (1.0 - cycle) * 2.0
            x = left + span * anim.ease("in_out_cubic", t)
            for offset, fade, weight in ((0.0, 0.3, 2), (-6.0, 0.58, 1), (-13.0, 0.76, 1)):
                edge = x + offset
                if edge < left or edge > right:
                    continue
                canvas.create_line(
                    edge,
                    top + 2,
                    edge,
                    bottom - 2,
                    fill=anim.fade(base, fade, KEY_COLOR),
                    width=weight,
                )
        except tk.TclError:
            return

        if scan.label:
            self._draw_chip((left, top, right, bottom), scan.label, base, 1.0, spinner=True)

    def _draw_box(self, box: _Box) -> None:
        canvas = self.canvas
        origin = self._rect
        if canvas is None or origin is None:
            return
        theme = self.theme
        color = theme.c(KIND_COLORS.get(box.kind, "accent"))
        weight = max(2, theme.px(2))

        morphing = box.morph_from is not None
        if morphing:
            progress = anim.ease("out_quint", box.age() / MORPH_SECONDS)
            rect = anim.lerp_rect(box.from_tuple(), box.tuple(), progress)
            acquired = 1.0
        else:
            progress = min(1.0, box.age() / ACQUIRE_SECONDS)
            acquired = progress
            rect = box.tuple()

        # 坐标换到浮层自己的画布上。框线画在目标矩形外侧，别压住被描述元素的
        # 真实边缘——否则识别算法会把框线当成元素自己的边缘。
        pad = max(2, theme.px(2))
        left = rect[0] - origin["left"] - pad
        right = rect[2] - origin["left"] + pad
        top = rect[1] - origin["top"] - pad
        bottom = rect[3] - origin["top"] + pad

        if not morphing:
            self._draw_guides((left, top, right, bottom), acquired, color)

        # 落定那一下：角标先亮到发白，再退回本色；同时一圈亮边往外推开。
        # 这是"认到了"唯一的正向回执——没有它，框只是出现在那里，用户分不清
        # 是刚认出来的还是上一轮留下的。
        flash = (box.age() - ACQUIRE_SECONDS) / FLASH_SECONDS if box.flashing() else None
        stroke = anim.pulse_color(color, flash, KEY_COLOR) if flash is not None else color

        # 角标从外向内收拢；收到位之后才把整圈框线淡进来。
        # 角标比框线粗一档：落定之后四个角仍然是"锁住"的视觉重心，
        # 而不是和框线糊成一条均匀的边。
        converge = 1.0 if morphing else anim.ease("out_cubic", acquired)
        for x1, y1, x2, y2 in anim.bracket_segments((left, top, right, bottom), converge):
            try:
                canvas.create_line(x1, y1, x2, y2, fill=stroke, width=weight + 1)
            except tk.TclError:
                return

        outline = 0.0 if morphing else max(0.0, min(1.0, (acquired - 0.55) / 0.35))
        if outline > 0:
            try:
                canvas.create_rectangle(
                    left, top, right, bottom,
                    outline=anim.fade(color, 1.0 - outline * 0.72, KEY_COLOR),
                    width=weight,
                )
            except tk.TclError:
                return

        if flash is not None:
            grow = 9.0 * anim.ease("out_cubic", flash)
            try:
                canvas.create_rectangle(
                    left - grow, top - grow, right + grow, bottom + grow,
                    outline=anim.pulse_color(color, flash, KEY_COLOR),
                    width=max(1, weight),
                )
            except tk.TclError:
                return

        chip = 1.0 if morphing else max(0.0, min(1.0, (acquired - 0.7) / 0.3))
        if box.label and chip > 0:
            self._draw_chip((left, top, right, bottom), box.label, color, chip)

    def _draw_guides(
        self, rect: tuple[float, float, float, float], progress: float, color: str
    ) -> None:
        """两根从窗口边缘滑过来的定位线。

        它们停在元素外侧，不压在元素上：作用是指出"目标在这一侧"，再把视线
        引过去。滑到位之后淡出，只留下框——一直留着会像一把游标卡尺。
        """

        canvas = self.canvas
        origin = self._rect
        if canvas is None or origin is None or progress <= 0.0:
            return
        if progress >= 0.86:
            return
        frame = (float(origin["width"]), float(origin["height"]))
        fade = 1.0 - max(0.0, (progress - 0.55) / 0.3)
        if fade <= 0.02:
            return
        line_color = anim.fade(color, 1.0 - fade, KEY_COLOR)
        weight = max(1, self.theme.px(1))
        try:
            for x1, y1, x2, y2 in anim.guide_segments(rect, progress, frame):
                canvas.create_line(x1, y1, x2, y2, fill=line_color, width=weight, dash=(7, 5))
                # 线的尽头补一小段横杠：像瞄准镜的准星，视线会顺着它落到框上。
                if abs(x1 - x2) < 1:
                    canvas.create_line(
                        x1 - 5, y1, x1 + 5, y1, fill=line_color, width=weight
                    )
                else:
                    canvas.create_line(
                        x1, y1 - 5, x1, y1 + 5, fill=line_color, width=weight
                    )
        except tk.TclError:
            return

    def _draw_pulse(self, center: tuple[float, float], started: float) -> None:
        canvas = self.canvas
        if canvas is None:
            return
        age = time.monotonic() - started
        if age > PULSE_SECONDS:
            return
        progress = age / PULSE_SECONDS
        left, top, right, bottom = anim.ripple_segments(center, progress)
        color = anim.fade(self.theme.c("success"), progress, KEY_COLOR)
        try:
            canvas.create_oval(left, top, right, bottom, outline=color, width=max(1, self.theme.px(2)))
            # 内圈一点点亮芯：点下去的那一瞬间要有一个"实"的落点。
            if progress < 0.4:
                core = 3.0 + 2.0 * progress
                canvas.create_oval(
                    center[0] - core, center[1] - core, center[0] + core, center[1] + core,
                    outline=anim.fade(self.theme.c("success"), 0.2 + progress, KEY_COLOR),
                    width=1,
                )
        except tk.TclError:
            return

    def _draw_chip(
        self,
        rect: tuple[float, float, float, float],
        text: str,
        color: str,
        visibility: float = 1.0,
        spinner: bool = False,
    ) -> None:
        """框旁边那枚小标签：一句话说明这个框是什么。

        大元素把标签放在上沿（放不下就放下沿）；小图标放在右侧——图标本身
        只有二三十像素，标签压在它身上就等于把它盖掉了，而"框住的是哪个图标"
        恰恰是这时候唯一要看的东西。
        """

        canvas = self.canvas
        if canvas is None:
            return
        theme = self.theme
        left, top, right, bottom = rect
        pad_x = theme.px(7)
        pad_y = theme.px(3)
        gap = theme.px(6)
        spin = theme.px(13) if spinner else 0
        try:
            canvas_width = int(canvas.winfo_width())
        except tk.TclError:
            return
        beside = (right - left) < theme.px(96) or (bottom - top) < theme.px(30)
        if beside:
            text_x = right + gap + pad_x + spin
            text_y = top - pad_y
            anchor = "nw"
        else:
            # 靠近上边缘时把标签压到框线内侧，否则会被窗口裁掉。
            above = top - gap - pad_y - theme.px(13) >= 0
            if above:
                text_y, anchor = top - gap, "sw"
            elif (bottom - top) > theme.px(120):
                # 一大片区域（扫描范围常常是半个窗口）：标签贴上沿内侧。
                # 放到区域"下面"会飘到几百像素以外，跟这块区域看着没关系。
                text_y, anchor = top + gap, "nw"
            else:
                text_y, anchor = bottom + gap, "nw"
            text_x = left + pad_x + spin
        text_color = anim.fade("#FFFFFF", 1.0 - visibility, KEY_COLOR)
        chip_color = anim.fade(color, 1.0 - visibility, KEY_COLOR)
        try:
            item = canvas.create_text(
                text_x,
                text_y,
                text=text,
                anchor=anchor,
                fill=text_color,
                font=theme.font("tiny"),
            )
            box = canvas.bbox(item)
        except tk.TclError:
            return
        if box is None:
            return
        # 贴到右边缘时挪一挪：大元素的标签左移，小图标的标签整个挪到图标左边
        # （右下角的发送按钮就是这种情况，往右挪只会把它自己盖住）。
        overflow = (box[2] + pad_x) - (canvas_width - theme.px(4))
        if overflow > 0:
            if beside:
                dx = (left - gap) - (box[2] + pad_x)
                if box[0] + dx - pad_x - spin < theme.px(4):
                    dx = theme.px(4) - (box[0] - pad_x - spin)
            else:
                dx = -overflow
            try:
                canvas.move(item, dx, 0)
                box = canvas.bbox(item)
            except tk.TclError:
                return
            if box is None:
                return
        chip_left = max(0.0, box[0] - pad_x - spin)
        try:
            chip = canvas.create_rectangle(
                chip_left,
                box[1] - pad_y,
                box[2] + pad_x,
                box[3] + pad_y,
                fill=chip_color,
                outline="",
            )
            canvas.tag_lower(chip, item)
            if spinner:
                # 一圈转着的缺口环：比"..."更能说明"还在动"。
                radius = max(3, theme.px(4))
                cx = chip_left + pad_x + radius
                cy = (box[1] + box[3]) / 2.0
                start = (time.monotonic() * 300.0) % 360.0
                canvas.create_arc(
                    cx - radius, cy - radius, cx + radius, cy + radius,
                    start=start, extent=260, style="arc",
                    outline=anim.fade("#FFFFFF", 1.0 - visibility, KEY_COLOR),
                    width=max(1, theme.px(1)),
                )
        except tk.TclError:
            return

    # -- 进度卡片 ------------------------------------------------------

    def _container_of(self, box: _Box) -> Optional[_Box]:
        """包住这个框的那个框（没有就返回 None）。

        发送按钮的框只有几十像素，还贴在窗口右下角。卡片跟着它走就会被挤到
        边上、压在输入框上。这时候跟着**包住它的那个输入框**走更对：视线本来
        就在输入框那一块，发送按钮只是其中一个点。
        """

        left, top, right, bottom = self._local(box.rect, 0)
        best: Optional[tuple[float, _Box]] = None
        for other in self._boxes:
            if other is box:
                continue
            o_left, o_top, o_right, o_bottom = self._local(other.rect, 0)
            if not (
                o_left <= left + 4
                and o_top <= top + 4
                and o_right >= right - 4
                and o_bottom >= bottom - 4
            ):
                continue
            area = float((o_right - o_left) * (o_bottom - o_top))
            if best is None or area < best[0]:
                best = (area, other)
        return best[1] if best else None

    def _card_origin(self, width: int, height: int) -> tuple[int, int]:
        """卡片放哪：贴着当前那个框，别盖住它。

        固定在窗口顶上会和应用的标题栏、菜单撞在一起；固定在底下会压住输入框。
        贴着"这一步正在处理的那个框"最不容易看错——卡片说的是谁，一眼就对上。
        """

        rect = self._rect
        if rect is None:
            return 8, 8
        frame_w = rect["width"]
        frame_h = rect["height"]
        margin = self.theme.px(8)
        gap = self.theme.px(10)
        box = self._boxes[-1] if self._boxes else None
        if box is not None:
            box = self._container_of(box) or box
            left, top, right, bottom = self._local(box.rect, 0)
            # 框的上沿还挂着一枚标签。卡片放到框上面的时候要把这一条让出来，
            # 否则卡片会正好压在标签上——那句话恰恰是"这个框是什么"。
            chip_room = self.theme.px(24) if box.label else 0
            below = bottom + gap
            above = top - gap - chip_room - height
            can_below = below + height <= frame_h - margin
            can_above = above >= margin
            # 高个子（搜索面板）优先放上面：它的重点在上沿（输入框、标签），
            # 放到下沿会把卡片甩到几百像素以外，和这一步看着没关系。
            if (bottom - top) > frame_h * 0.4 and can_above:
                y = above
            elif can_below:
                y = below
            elif can_above:
                y = above
            else:
                y = margin
            x = left
        else:
            x = (frame_w - width) / 2.0
            y = margin
        x = max(margin, min(frame_w - width - margin, x))
        return int(x), int(max(margin, y))

    def _draw_progress(self, index: int, total: int, text: str, tone: str, detail: str) -> None:
        canvas = self.canvas
        rect = self._rect
        if canvas is None or rect is None:
            return
        theme = self.theme
        tone_color = theme.c(tone)
        px = theme.px

        # 宽度跟着窗口走：窗口只有八百多像素时，一张 450 像素的卡片会横掉半个屏。
        width = max(px(170), min(px(300), int(rect["width"] * 0.42)))
        height = px(80) if detail else px(66)
        box = self._boxes[-1] if self._boxes else None
        if box is not None:
            box = self._container_of(box) or box
        left, top = self._card_origin(width, height)

        # 换了一句话，卡片轻轻往上浮一点再落定——说明"这一步是新的"。
        age = time.monotonic() - self._card_started
        lift = 0.0
        if CARD_SECONDS > 0 and age < CARD_SECONDS:
            lift = 5.0 * (1.0 - anim.ease("out_cubic", age / CARD_SECONDS))
        top = top - int(round(lift))

        try:
            if box is not None:
                self._draw_leader(left, top, width, height, box, tone_color)
            canvas.create_rectangle(
                left, top, left + width, top + height,
                fill="#0F0F13", outline=anim.fade(tone_color, 0.45, KEY_COLOR), width=1,
            )
            # 左边一条竖色条：不读字也知道这一步是正常、在等、还是出错。
            canvas.create_rectangle(
                left, top, left + px(3), top + height, fill=tone_color, outline="",
            )
            canvas.create_text(
                left + px(14), top + px(20),
                text=text, anchor="w", fill="#F4F4F7", font=theme.font("strong"),
            )
            canvas.create_text(
                left + width - px(14), top + px(20),
                text=f"{min(index, total)}/{total}", anchor="e",
                fill="#8E8E9A", font=theme.font("tiny"),
            )
            if detail:
                canvas.create_text(
                    left + px(14), top + px(40),
                    text=detail, anchor="w", fill="#9A9AA6", font=theme.font("tiny"),
                )
            self._draw_bar(left, top, width, height, index, total, tone_color)
        except tk.TclError:
            return

    def _draw_leader(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
        box: _Box,
        color: str,
    ) -> None:
        """从卡片拉一根细线到它说的那个框：谁在说谁，不用猜。"""

        canvas = self.canvas
        if canvas is None:
            return
        bx1, by1, bx2, by2 = self._local(box.rect, 0)
        anchor_x = min(max((bx1 + bx2) / 2.0, left + 10), left + width - 10)
        if top > by2 + 2:
            y1, y2 = float(top), by2
        elif top + height < by1 - 2:
            y1, y2 = float(top + height), by1
        else:
            return
        if abs(y1 - y2) < 4 or abs(y1 - y2) > 160:
            return
        try:
            canvas.create_line(
                anchor_x, y1, anchor_x, y2,
                fill=anim.fade(color, 0.62, KEY_COLOR), width=1,
            )
        except tk.TclError:
            return

    def _draw_bar(
        self, left: int, top: int, width: int, height: int, index: int, total: int, tone_color: str
    ) -> None:
        canvas = self.canvas
        if canvas is None:
            return
        theme = self.theme
        px = theme.px
        track = px(4)
        bar_top = top + height - px(15)
        bar_left = left + px(14)
        bar_right = left + width - px(14)
        bar_width = max(1, bar_right - bar_left)
        try:
            # 圆头线当进度条：Tk 没有圆角矩形，但线帽是圆的。
            canvas.create_line(
                bar_left, bar_top, bar_right, bar_top,
                fill="#3A3A44", width=track, capstyle="round",
            )
            target = max(0.0, min(1.0, index / max(1, total)))
            age = time.monotonic() - self._bar_started
            eased = anim.ease("out_cubic", min(1.0, age / BAR_SECONDS)) if BAR_SECONDS else 1.0
            ratio = self._bar_from + (target - self._bar_from) * eased
            filled = int(bar_width * max(0.0, min(1.0, ratio)))
            if filled > 0:
                head_x = bar_left + filled
                canvas.create_line(
                    bar_left, bar_top, head_x, bar_top,
                    fill=tone_color, width=track, capstyle="round",
                )
                # 前端一颗小亮点：一眼看出"还在走"而不是"卡住了"。
                radius = max(2, track // 2 + 1)
                canvas.create_oval(
                    head_x - radius, bar_top - radius, head_x + radius, bar_top + radius,
                    fill=anim.blend(tone_color, "#FFFFFF", 0.45), outline="",
                )
            # 刻度：把一条连续的条切成 total 段，读得出"一共几步"。
            if 1 < total <= 12:
                for step in range(1, total):
                    x = bar_left + bar_width * step / total
                    canvas.create_line(
                        x, bar_top - px(4), x, bar_top + px(4), fill="#0F0F13", width=1,
                    )
        except tk.TclError:
            return


def apply_event(overlay: Overlay, payload: Any) -> None:
    """把调度线程发来的浮层事件翻译成绘制动作。"""

    if not isinstance(payload, dict):
        return
    action = payload.get("action")
    if action == "show":
        overlay.show(payload.get("rect") or {})
    elif action == "hide":
        overlay.hide()
    elif action == "scan":
        overlay.scan(
            payload.get("rect") or {},
            str(payload.get("label") or "识别中"),
            str(payload.get("tone") or "warning"),
        )
    elif action == "box":
        overlay.box(payload.get("rect") or {}, str(payload.get("kind") or ""), str(payload.get("label") or ""))
    elif action == "clear":
        overlay.clear_boxes()
    elif action == "pulse":
        point = payload.get("point") or ()
        if len(point) >= 2:
            overlay.pulse((int(point[0]), int(point[1])))
    elif action == "progress":
        overlay.progress(
            int(payload.get("index") or 0),
            int(payload.get("total") or 1),
            str(payload.get("text") or ""),
            str(payload.get("tone") or "accent"),
            str(payload.get("detail") or ""),
        )
