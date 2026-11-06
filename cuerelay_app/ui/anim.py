# -*- coding: utf-8 -*-
"""浮层动画的缓动与几何。

识别结果从"认出来"到"框住它"之间隔着一层解释：用户得先看见一条线收拢到
某个元素上，才敢相信程序认的就是它。这里每个函数只做一件事——给定进度 t
（0~1），算出那一刻该画在哪里。

全部是纯函数，不碰 Tk，所以自检可以直接验证"t=1 时一定精确落在目标上"。
"""

from __future__ import annotations

import math
from typing import Iterable

RectTuple = tuple[float, float, float, float]


# ---------------------------------------------------------------------------
# 缓动
# ---------------------------------------------------------------------------


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


def ease(name: str, t: float) -> float:
    """把线性进度换成"看着舒服"的进度。

    收拢用 out_cubic：一开始快、贴近目标时慢下来，像镜头对焦。
    out_back 有一点点过冲，用在"落定"那一下，框会轻轻弹一下再稳住。
    """

    t = clamp01(t)
    if name == "linear":
        return t
    if name == "out_cubic":
        return 1.0 - (1.0 - t) ** 3
    if name == "out_quint":
        return 1.0 - (1.0 - t) ** 5
    if name == "in_out_cubic":
        return 4.0 * t ** 3 if t < 0.5 else 1.0 - (-2.0 * t + 2.0) ** 3 / 2.0
    if name == "out_back":
        c1 = 1.70158
        c3 = c1 + 1.0
        return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2
    return t


def lerp(start: float, end: float, t: float) -> float:
    return start + (end - start) * clamp01(t)


def lerp_rect(start: RectTuple, end: RectTuple, t: float) -> RectTuple:
    return (
        lerp(start[0], end[0], t),
        lerp(start[1], end[1], t),
        lerp(start[2], end[2], t),
        lerp(start[3], end[3], t),
    )


def scale_rect(rect: RectTuple, factor: float) -> RectTuple:
    """以中心为原点缩放。factor > 1 就是"从外面收进来"的起点。"""

    left, top, right, bottom = rect
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    half_w = (right - left) * factor / 2.0
    half_h = (bottom - top) * factor / 2.0
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


# ---------------------------------------------------------------------------
# 收拢用的线段
# ---------------------------------------------------------------------------


def bracket_segments(
    rect: RectTuple,
    progress: float,
    spread: float = 0.26,
    arm_ratio: float = 0.34,
    arm_range: tuple[float, float] = (10.0, 30.0),
) -> list[tuple[float, float, float, float]]:
    """四角的直角标记，从外向内收拢到 rect 上。

    progress=0 时四个角在 rect 外扩 spread 的位置（看起来是"正在找"），
    progress=1 时严丝合缝地贴在 rect 的四个角上。八条线段（每个角两条）。

    用角标而不是整圈边框：整圈会沿着元素边缘画一条实线，识别算法会把它当成
    元素自己的边缘；角标只占每边最外侧一小段，落在采样带之外。
    """

    t = clamp01(progress)
    current = scale_rect(rect, 1.0 + spread * (1.0 - t))
    left, top, right, bottom = current
    short = min(right - left, bottom - top)
    arm = max(arm_range[0], min(arm_range[1], short * arm_ratio))
    arm = min(arm, max(2.0, (right - left) / 2.0), max(2.0, (bottom - top) / 2.0))
    return [
        (left, top, left + arm, top),
        (left, top, left, top + arm),
        (right - arm, top, right, top),
        (right, top, right, top + arm),
        (left, bottom, left + arm, bottom),
        (left, bottom - arm, left, bottom),
        (right - arm, bottom, right, bottom),
        (right, bottom - arm, right, bottom),
    ]


def guide_segments(
    rect: RectTuple,
    progress: float,
    frame: tuple[float, float],
    reach: float = 46.0,
) -> list[tuple[float, float, float, float]]:
    """两根从窗口边缘滑过来的定位线：一根竖线、一根横线。

    它们不落在元素上，而是停在元素外侧 reach 像素的地方——作用是指出"目标
    在这一侧"，同时把视线引过去。progress 到 1 时它们应该已经淡出（由调用方
    用颜色插值实现），这里只负责位置。
    """

    t = ease("out_quint", progress)
    left, top, right, bottom = rect
    width, height = frame
    x = lerp(0.0, max(0.0, left - reach), t)
    y = lerp(0.0, max(0.0, top - reach), t)
    return [
        (x, max(0.0, top - reach), x, min(height, bottom + reach)),
        (max(0.0, left - reach), y, min(width, right + reach), y),
    ]


def ripple_segments(center: tuple[float, float], progress: float, radius: float = 42.0) -> RectTuple:
    """点击位置那一圈扩散的圆（返回外接矩形）。"""

    t = ease("out_cubic", progress)
    cx, cy = center
    r = max(2.0, radius * t)
    return (cx - r, cy - r, cx + r, cy + r)


# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = (color or "").lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return (0, 0, 0)
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def rgb_to_hex(rgb: Iterable[int]) -> str:
    r, g, b = (max(0, min(255, int(round(item)))) for item in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def blend(first: str, second: str, t: float) -> str:
    a = hex_to_rgb(first)
    b = hex_to_rgb(second)
    k = clamp01(t)
    return rgb_to_hex((a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k))


def fade(color: str, progress: float, key_color: str) -> str:
    """把颜色往"透明色"里掺，模拟淡出。

    Tk 的画布项没有透明度，但浮层本身是按颜色键抠出来的：颜色越接近键色，
    在屏幕上就越淡。所以淡出 = 向键色插值，而不是调 alpha。
    """

    return blend(color, key_color, clamp01(progress))


def pulse_color(color: str, progress: float, key_color: str) -> str:
    """落定那一下的闪：先亮一点，再迅速退回本色。"""

    t = clamp01(progress)
    if t < 0.35:
        return blend(color, "#FFFFFF", 0.55 * (t / 0.35))
    return blend(color, key_color, (t - 0.35) / 0.65 * 0.35)


# ---------------------------------------------------------------------------
# 时间轴
# ---------------------------------------------------------------------------


class Timeline:
    """在 Tk 上跑的一组短动画。

    只有一条 after 循环：有动画在跑的时候每 16ms 画一帧，全部结束就停，
    不留一个永远在空转的定时器（那会让笔记本的风扇一直响）。
    """

    def __init__(self, widget) -> None:
        self.widget = widget
        self._after = None
        self._started = 0.0
        self._duration = 0.0
        self._running = False

    def running(self) -> bool:
        return self._running

    def start(self, duration: float, tick, done=None) -> None:
        import time

        self._duration = max(0.02, float(duration))
        self._started = time.monotonic()
        self._tick = tick
        self._done = done
        self._running = True
        if self._after is None:
            self._after = self.widget.after(16, self._frame)

    def stop(self) -> None:
        self._running = False
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _frame(self) -> None:
        import time

        self._after = None
        if not self._running:
            return
        elapsed = time.monotonic() - self._started
        progress = min(1.0, elapsed / self._duration) if self._duration else 1.0
        try:
            self._tick(progress)
        except Exception:
            self.stop()
            return
        if progress >= 1.0:
            self._running = False
            done = getattr(self, "_done", None)
            self._done = None
            if done is not None:
                try:
                    done()
                except Exception:
                    pass
            return
        try:
            self._after = self.widget.after(16, self._frame)
        except Exception:
            self._running = False


def easing_names() -> tuple[str, ...]:
    return ("linear", "out_cubic", "out_quint", "in_out_cubic", "out_back")


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
