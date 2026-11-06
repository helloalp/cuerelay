# -*- coding: utf-8 -*-
"""对话页：一个真正的本地对话浏览器。

这一页要回答的问题只有三个：**有哪些对话、它们属于哪个项目、我现在在看哪一条**。
所以它不是一个平铺的列表，而是三件事叠在一起：

- 左边是分组的目录（项目 / 无项目 / 归档），组头能折叠，条数直接写在组名后面；
- 中间一行是搜索和筛选，筛选胶囊下面那块高亮会滑过去，而不是跳过去；
- 右边是选中那条的详情：轮次、令牌、模型、目录、首条消息，以及「去 Codex 打开」。

动效不是装饰，是用来交代因果的：

- 列表第一次出现时逐行错峰淡入，眼睛能顺着往下走，而不是"啪"地一整块砸出来；
- 选中时左边那条竖杠从上一行滑到这一行——它说明"选择变了"，而背景色只说明
  "现在选的是谁"，两个信息分开画才不会互相盖住；
- 详情里那条强调线从 0 长到满宽，轮次从 0 滚到真值，说明"这块内容刚换过"；
- 读库的几十毫秒里显示骨架屏，而不是留一片空白让人以为按钮没反应。

**控件一律复用，不在交互时销毁重建。** Tk 在 Windows 上不做双缓冲：一次
destroy + create 五十行，中间会露出父容器的底色，看起来就是"操作一下就闪一下"。
所以列表的行、分组头、详情页的每一个字段都是一次建好、之后只改文字和位置；
筛选变化时移动的是 grid 的 row 号，不是重新造控件。淡入只在第一次加载时播，
因为它是"这里刚出现了一批东西"的说明，不该在每次打字时重播。

Tk 没有透明度，所有"淡入"都是把颜色从背景色插值到前景色；所有"滑动"都是
改画布上那块多边形的坐标。这一点在 anim.py 里已经有一整套纯函数了。
"""

from __future__ import annotations

import datetime as _dt
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Any, Callable, Optional, Sequence

from .. import codexdb
from .anim import Timeline, blend, ease
from .theme import Theme
from .widgets import ScrollArea, Switch, card

# 一屏先画多少条。900 条一次性建控件会卡住界面，而且没人会一屏一屏往下翻到底。
PAGE_SIZE = 50

# 逐行淡入最多铺多少行、每行错开多少。铺满一屏就够，后面的直接出现。
STAGGER_ROWS = 14
STAGGER_STEP = 0.018
FADE_SECONDS = 0.30

SCOPES: tuple[tuple[str, str], ...] = (
    ("all", "全部"),
    ("projects", "项目"),
    ("loose", "无项目"),
    ("archived", "归档"),
)

KIND_LABELS = {
    codexdb.KIND_USER: "用户对话",
    codexdb.KIND_SUBAGENT: "子代理",
    codexdb.KIND_AGENT: "代理创建",
    codexdb.KIND_FORK: "分叉",
}


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------


def format_when(stamp: float) -> str:
    """列表右侧那个短时间：越近越具体，越远越模糊。"""

    if not stamp or stamp <= 0:
        return "—"
    now = time.time()
    delta = now - stamp
    if delta < 0:
        delta = 0
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    moment = _dt.datetime.fromtimestamp(stamp)
    today = _dt.date.today()
    if moment.date() == today:
        return moment.strftime("%H:%M")
    if moment.date() == today - _dt.timedelta(days=1):
        return "昨天"
    if moment.year == today.year:
        return moment.strftime("%m-%d")
    return moment.strftime("%Y-%m-%d")


def format_full(stamp: float) -> str:
    if not stamp or stamp <= 0:
        return "—"
    return _dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def format_tokens(count: int) -> str:
    if count <= 0:
        return "—"
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.2f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# 画布小工具
# ---------------------------------------------------------------------------


def round_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> list[float]:
    """圆角矩形的 24 个点。点数固定，所以能直接用 coords() 就地改形。"""

    r = max(0.0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def draw_round(canvas: tk.Canvas, x1, y1, x2, y2, radius, **kwargs) -> int:
    return canvas.create_polygon(round_points(x1, y1, x2, y2, radius), smooth=True, **kwargs)


class _Fonts:
    """按字号缓存 Font 对象：测量文字宽度要用它，每次新建一个太浪费。"""

    def __init__(self, theme: Theme) -> None:
        self.theme = theme
        self._cache: dict[str, tkfont.Font] = {}

    def get(self, key: str) -> tkfont.Font:
        if key not in self._cache:
            spec = self.theme.font(key)
            family, size = spec[0], spec[1]
            weight = spec[2] if len(spec) > 2 else "normal"
            self._cache[key] = tkfont.Font(family=family, size=size, weight=weight)
        return self._cache[key]

    def width(self, text: str, key: str = "body") -> int:
        try:
            return self.get(key).measure(text)
        except tk.TclError:
            return len(text) * 8


# ---------------------------------------------------------------------------
# 筛选胶囊
# ---------------------------------------------------------------------------


class ChipBar(tk.Canvas):
    """一排筛选胶囊，选中那块底色会滑过去。

    用画布而不是若干 Label：只有画布能在两个状态之间做补间。高亮"跳"过去
    只会告诉用户"现在选的是这个"，滑过去才说明"从那个换到了这个"。

    切换时只改那块多边形的坐标和几段文字的颜色，不 delete 重画——画布清空
    再重建会有一下空白，那就是"闪"。
    """

    PAD_X = 14
    GAP = 4
    INSET = 3

    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        options: Sequence[tuple[str, str]],
        value: str,
        on_change: Callable[[str], None],
    ) -> None:
        self.theme = theme
        self._items = list(options)
        self._value = value
        self._on_change = on_change
        self._fonts = _Fonts(theme)
        self._height = theme.px(30)
        self._pill: Optional[int] = None
        self._texts: dict[str, int] = {}
        self._rects: list[tuple[float, float, str]] = []
        self._timeline = Timeline(self)

        self._layout()
        super().__init__(
            parent,
            width=self._total_width,
            height=self._height,
            bg=theme.c("surface"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._draw()
        self.bind("<Button-1>", self._on_click)

    # -- 几何 ----------------------------------------------------------

    def _layout(self) -> None:
        self._rects = []
        x = float(self.INSET)
        for value, text in self._items:
            width = self._fonts.width(text, "body") + self.PAD_X * 2
            self._rects.append((x, x + width, value))
            x += width + self.GAP
        self._total_width = int(x - self.GAP + self.INSET)
        self._chip_text = {value: text for value, text in self._items}

    def _rect_of(self, value: str) -> tuple[float, float]:
        for left, right, key in self._rects:
            if key == value:
                return left, right
        return self.INSET, self.INSET

    # -- 绘制 ----------------------------------------------------------

    def _draw(self) -> None:
        theme = self.theme
        radius = (self._height - 2) / 2.0
        draw_round(
            self,
            0,
            0,
            max(self._total_width, 1),
            self._height,
            radius,
            fill=theme.c("surface_3"),
            outline="",
        )
        left, right = self._rect_of(self._value)
        self._pill = draw_round(
            self,
            left,
            self.INSET,
            right,
            self._height - self.INSET,
            radius - self.INSET,
            fill=theme.c("surface"),
            outline=theme.c("border"),
        )
        self._texts = {}
        for start, end, value in self._rects:
            active = value == self._value
            self._texts[value] = self.create_text(
                (start + end) / 2,
                self._height / 2,
                text=self._chip_text.get(value, value),
                fill=theme.c("text") if active else theme.c("text_3"),
                font=theme.font("strong") if active else theme.font("body"),
            )

    def _paint_pill(self, left: float, right: float) -> None:
        if self._pill is None:
            return
        try:
            self.coords(
                self._pill,
                *round_points(left, self.INSET, right, self._height - self.INSET, (self._height - 2) / 2.0 - self.INSET),
            )
        except tk.TclError:
            pass

    def _restyle_texts(self) -> None:
        for value, item in self._texts.items():
            active = value == self._value
            try:
                self.itemconfigure(
                    item,
                    fill=self.theme.c("text") if active else self.theme.c("text_3"),
                    font=self.theme.font("strong") if active else self.theme.font("body"),
                )
            except tk.TclError:
                pass

    # -- 交互 ----------------------------------------------------------

    def _on_click(self, event) -> None:
        x = float(event.x)
        for left, right, value in self._rects:
            if left <= x <= right:
                self.select(value)
                return

    def select(self, value: str, animate: bool = True) -> None:
        if value == self._value:
            return
        start = self._rect_of(self._value)
        target = self._rect_of(value)
        self._value = value
        # 文字颜色和字重要立刻换：跟着一起补间会有一瞬间"两个都是选中"。
        self._restyle_texts()
        if animate:
            self._paint_pill(*start)
            self._timeline.start(
                0.22,
                lambda t: self._paint_pill(
                    start[0] + (target[0] - start[0]) * ease("out_cubic", t),
                    start[1] + (target[1] - start[1]) * ease("out_cubic", t),
                ),
            )
        else:
            self._paint_pill(*target)
        if self._on_change is not None:
            self._on_change(value)


# ---------------------------------------------------------------------------
# 骨架屏
# ---------------------------------------------------------------------------


class Skeleton(tk.Canvas):
    """读库时占位的那几行灰条，带一道来回扫的高光。

    空白的列表和"正在读"的列表看起来一模一样，用户只能靠猜。灰条至少说明
    "这里马上会有东西"，扫过去的高光说明"它还在动"。
    """

    def __init__(self, parent: tk.Misc, theme: Theme) -> None:
        super().__init__(parent, bg=theme.c("surface"), highlightthickness=0, bd=0)
        self.theme = theme
        self._bars: list[tuple[int, float]] = []
        self._after: Optional[str] = None
        self._phase = 0.0
        self.bind("<Configure>", lambda _e: self._layout())
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, event) -> None:
        if event.widget is not self:
            return
        self.stop()

    def _layout(self) -> None:
        try:
            self.delete("all")
        except tk.TclError:
            return
        self._bars = []
        width = max(self.winfo_width(), 80)
        row_height = self.theme.px(58)
        for index in range(9):
            top = self.theme.px(16) + index * row_height
            long_width = width * (0.58 + 0.06 * (index % 3))
            short_width = width * 0.30
            bar = self.create_polygon(
                round_points(self.theme.px(16), top, self.theme.px(16) + long_width, top + self.theme.px(11), self.theme.px(5)),
                smooth=True,
                fill=self.theme.c("surface_3"),
                outline="",
            )
            meta = self.create_polygon(
                round_points(
                    self.theme.px(16),
                    top + self.theme.px(18),
                    self.theme.px(16) + short_width,
                    top + self.theme.px(26),
                    self.theme.px(4),
                ),
                smooth=True,
                fill=self.theme.c("surface_2"),
                outline="",
            )
            self._bars.append((bar, index * 0.07))
            self._bars.append((meta, 0.5 + index * 0.07))

    def start(self) -> None:
        if self._after is not None:
            return
        self._layout()
        self._tick()

    def stop(self) -> None:
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _tick(self) -> None:
        self._after = None
        self._phase = (self._phase + 0.035) % 1.0
        base = self.theme.c("surface_3")
        light = self.theme.c("border")
        for item, offset in self._bars:
            wave = 0.5 + 0.5 * _sin((self._phase + offset) * 6.28318)
            try:
                self.itemconfigure(item, fill=blend(base, light, 0.75 * wave))
            except tk.TclError:
                return
        try:
            self._after = self.after(48, self._tick)
        except tk.TclError:
            self._after = None


def _sin(value: float) -> float:
    import math

    return math.sin(value)


# ---------------------------------------------------------------------------
# 搜索框
# ---------------------------------------------------------------------------


class SearchBox(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        variable: tk.StringVar,
        on_change: Callable[[str], None],
        width: int = 18,
    ) -> None:
        super().__init__(
            parent,
            bg=theme.c("surface"),
            highlightthickness=1,
            highlightbackground=theme.c("border"),
            highlightcolor=theme.c("border"),
            bd=0,
        )
        self.theme = theme
        self.variable = variable
        self._on_change = on_change
        self._timeline = Timeline(self)
        self._focused = False

        self.icon = tk.Canvas(
            self,
            width=theme.px(14),
            height=theme.px(14),
            bg=theme.c("surface"),
            highlightthickness=0,
            bd=0,
        )
        self.icon.pack(side="left", padx=(theme.px(10), theme.px(6)))
        self._draw_icon(theme.c("text_3"))

        self.entry = tk.Entry(
            self,
            textvariable=variable,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=theme.c("surface"),
            fg=theme.c("text"),
            insertbackground=theme.c("accent"),
            selectbackground=theme.c("accent_soft"),
            selectforeground=theme.c("text"),
            font=theme.font("body"),
            width=width,
        )
        self.entry.pack(side="left", fill="x", expand=True, pady=theme.px(7))
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)

        self.placeholder = tk.Label(
            self,
            text="搜索标题、项目、目录或 ID",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("body"),
            cursor="xterm",
        )
        self.placeholder.place(x=theme.px(30), rely=0.5, anchor="w")
        self.placeholder.bind("<Button-1>", lambda _e: self.entry.focus_set())

        self.clear = tk.Label(
            self,
            text="×",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("strong"),
            cursor="hand2",
            padx=theme.px(8),
        )
        self.clear.bind("<Button-1>", lambda _e: self.variable.set(""))
        # 变量既可能来自键盘，也可能来自外部（比如命令面板预填）。
        # 只在自己的事件里同步占位符，外部写入时占位符会盖住真正的关键词。
        self.variable.trace_add("write", lambda *_: self._sync())
        self._sync()

    def _draw_icon(self, color: str) -> None:
        theme = self.theme
        size = theme.px(14)
        self.icon.delete("all")
        self.icon.create_oval(1, 1, size - 5, size - 5, outline=color, width=1.4)
        self.icon.create_line(size - 5, size - 5, size - 1, size - 1, fill=color, width=1.4)

    def _on_focus_in(self, _event=None) -> None:
        self._focused = True
        self._glow(True)

    def _on_focus_out(self, _event=None) -> None:
        self._focused = False
        self._glow(False)

    def _glow(self, active: bool) -> None:
        """边框从灰变蓝再变回来。焦点是"你现在打的字会进这里"的唯一提示。"""

        start = self.theme.c("border") if active else self.theme.c("accent")
        end = self.theme.c("accent") if active else self.theme.c("border")

        def tick(t: float) -> None:
            color = blend(start, end, ease("out_cubic", t))
            try:
                self.configure(highlightbackground=color, highlightcolor=color)
            except tk.TclError:
                pass

        self._timeline.start(0.18, tick)

    def _sync(self) -> None:
        value = self.variable.get()
        try:
            if value:
                self.placeholder.place_forget()
                self.clear.pack(side="right", padx=(0, self.theme.px(4)))
            else:
                self.placeholder.place(x=self.theme.px(30), rely=0.5, anchor="w")
                self.clear.pack_forget()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 详情面板
# ---------------------------------------------------------------------------


class DetailPane(tk.Frame):
    """选中那条对话的档案：数字、目录、首条消息、动作。

    所有字段一次建好，切换对话时只改文字。以前的版本每点一行就把整页控件
    销毁重建，Windows 上会看到一下空白——那正是"点一下就闪一下"的来源。
    """

    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        on_open: Callable[[codexdb.ThreadRecord], None],
    ) -> None:
        super().__init__(parent, bg=theme.c("surface"))
        self.theme = theme
        self._on_open = on_open
        self._timeline = Timeline(self)
        self._record: Optional[codexdb.ThreadRecord] = None
        self._fields: dict[str, tuple[tk.Frame, tk.Label]] = {}
        self._tags: dict[str, tk.Label] = {}
        self._tags_mapped: set[str] = set()

        self.scroll = ScrollArea(self, theme, "surface")
        self.scroll.pack(fill="both", expand=True)
        self.body = self.scroll.body

        self.actions = tk.Frame(self, bg=theme.c("surface"))
        self.actions.pack(fill="x", side="bottom", padx=theme.px(20), pady=(0, theme.px(14)))
        # 动作条和正文之间那条线：正文是能滚的，滚到一半被裁断时，
        # 没有这条线看起来就像按钮压在了字上。
        self.divider = tk.Frame(self, bg=theme.c("border"), height=1)
        self.divider.pack(fill="x", side="bottom")

        self._content = tk.Frame(self.body, bg=theme.c("surface"))
        self._empty = tk.Frame(self.body, bg=theme.c("surface"))

        self._build_content()
        self._build_empty()
        self._build_actions()
        self.body.bind("<Configure>", self._on_body_configure)
        self.show_empty()

    # -- 一次性搭建 ----------------------------------------------------

    def _build_content(self) -> None:
        theme = self.theme
        content = self._content

        self._title = tk.Label(
            content,
            text="",
            bg=theme.c("surface"),
            fg=theme.c("text"),
            font=theme.font("display"),
            anchor="w",
            justify="left",
            wraplength=theme.px(520),
        )
        self._title.pack(fill="x", padx=theme.px(20), pady=(theme.px(18), theme.px(6)))

        chips = tk.Frame(content, bg=theme.c("surface"))
        chips.pack(fill="x", padx=theme.px(20))
        self._updated = tk.Label(
            chips,
            text="",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
        )
        self._updated.pack(side="right")
        for key, accent in (("project", True), ("kind", False), ("archived", False), ("pinned", False)):
            self._tags[key] = tk.Label(
                chips,
                text="",
                bg=theme.c("accent_soft") if accent else theme.c("surface_3"),
                fg=theme.c("accent_label") if accent else theme.c("text_2"),
                font=theme.font("tiny"),
                padx=theme.px(8),
                pady=theme.px(3),
            )

        self._rule = tk.Canvas(
            content,
            height=theme.px(3),
            bg=theme.c("surface"),
            highlightthickness=0,
            bd=0,
        )
        self._rule.pack(fill="x", padx=theme.px(20), pady=(theme.px(12), theme.px(14)))

        stats = tk.Frame(content, bg=theme.c("surface"))
        stats.pack(fill="x", padx=theme.px(20))
        self._stats: dict[str, tk.Label] = {}
        for index, (key, label) in enumerate((("turns", "轮次"), ("tokens", "令牌"), ("model", "模型"))):
            cell = tk.Frame(stats, bg=theme.c("surface_2"), padx=theme.px(12), pady=theme.px(8))
            cell.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else theme.px(8), 0))
            value = tk.Label(
                cell,
                text="—",
                bg=theme.c("surface_2"),
                fg=theme.c("text"),
                font=theme.font("title"),
                anchor="w",
            )
            value.pack(fill="x")
            tk.Label(
                cell,
                text=label,
                bg=theme.c("surface_2"),
                fg=theme.c("text_3"),
                font=theme.font("tiny"),
                anchor="w",
            ).pack(fill="x")
            self._stats[key] = value

        for key, label in (
            ("cwd", "目录"),
            ("created", "创建"),
            ("origin", "来源"),
            ("parent", "父对话"),
            ("id", "对话 ID"),
        ):
            row = tk.Frame(content, bg=theme.c("surface"))
            tk.Label(
                row,
                text=label,
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                width=8,
                anchor="w",
            ).pack(side="left")
            value = tk.Label(
                row,
                text="—",
                bg=theme.c("surface"),
                fg=theme.c("text_2"),
                font=theme.font("mono" if key in ("cwd", "parent", "id") else "body"),
                anchor="w",
                justify="left",
            )
            value.pack(side="left", fill="x", expand=True)
            self._fields[key] = (row, value)

        self._excerpt_head = tk.Label(
            content,
            text="首条消息",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("small_strong"),
            anchor="w",
        )
        self._excerpt_body = tk.Label(
            content,
            text="",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("body"),
            anchor="w",
            justify="left",
            wraplength=theme.px(520),
        )

    def _build_empty(self) -> None:
        theme = self.theme
        holder = tk.Frame(self._empty, bg=theme.c("surface"))
        holder.pack(fill="both", expand=True, pady=(theme.px(70), 0))
        self._empty_title = tk.Label(
            holder,
            text="选一条对话",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("strong"),
        )
        self._empty_title.pack()
        self._empty_hint = tk.Label(
            holder,
            text="",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("small"),
            justify="center",
            wraplength=theme.px(360),
        )
        self._empty_hint.pack(pady=(theme.px(6), 0))

    def _build_actions(self) -> None:
        theme = self.theme
        self._open_button = tk.Label(
            self.actions,
            text="在 Codex 打开",
            bg=theme.c("accent"),
            fg=theme.c("accent_text"),
            font=theme.font("strong"),
            padx=theme.px(16),
            pady=theme.px(8),
            cursor="hand2",
        )
        self._open_button.pack(side="right")
        self._open_button.bind("<Button-1>", lambda _e: self._open_current())

        self._feedback = tk.Label(
            self.actions,
            text="",
            bg=theme.c("surface"),
            fg=theme.c("success"),
            font=theme.font("small"),
        )
        self._feedback.pack(side="left")

        self._copy_buttons: dict[str, tk.Label] = {}
        for key, text in (("title", "复制标题"), ("id", "复制 ID"), ("cwd", "复制目录")):
            button = tk.Label(
                self.actions,
                text=text,
                bg=theme.c("surface"),
                fg=theme.c("text_2"),
                font=theme.font("small"),
                padx=theme.px(10),
                pady=theme.px(6),
                cursor="hand2",
            )
            button.pack(side="left", padx=(0, theme.px(4)))
            button.bind("<Button-1>", lambda _e, k=key: self._copy(k))
            self._copy_buttons[key] = button

    # -- 展示 ----------------------------------------------------------

    def show_empty(self, title: str = "选一条对话", hint: str = "") -> None:
        self._record = None
        try:
            self._content.pack_forget()
            self._empty.pack(fill="both", expand=True)
            self._empty_title.configure(text=title)
            self._empty_hint.configure(text=hint)
            self._open_button.configure(fg=self.theme.c("text_3"))
            for button in self._copy_buttons.values():
                button.configure(fg=self.theme.c("text_3"))
        except tk.TclError:
            pass

    def show(self, record: codexdb.ThreadRecord, animate: bool = True) -> None:
        self._record = record
        theme = self.theme
        try:
            self._empty.pack_forget()
            if not self._content.winfo_ismapped():
                self._content.pack(fill="both", expand=True)
            self._open_button.configure(fg=theme.c("accent_text"))
            for button in self._copy_buttons.values():
                button.configure(fg=theme.c("text_2"))

            self._title.configure(text=record.title)
            self._updated.configure(text=f"更新于 {format_full(record.updated)}")

            tags = [
                ("project", record.project or "无项目"),
                ("kind", KIND_LABELS.get(record.kind, record.kind)),
                ("archived", "已归档" if record.archived else ""),
                ("pinned", "置顶" if record.pinned else ""),
            ]
            for key, text in tags:
                label = self._tags[key]
                if not text:
                    if key in self._tags_mapped:
                        label.pack_forget()
                        self._tags_mapped.discard(key)
                    continue
                label.configure(text=text)
                if key not in self._tags_mapped:
                    # 顺序：项目 → 类型 → 归档 → 置顶。pack 到末尾就正好是这个顺序。
                    label.pack(side="left", padx=(0, theme.px(6)))
                    self._tags_mapped.add(key)

            self._stats["turns"].configure(text=str(record.turns) if record.turns else "—")
            self._stats["tokens"].configure(text=format_tokens(record.tokens))
            self._stats["model"].configure(text=record.model or "—")

            self._set_field("cwd", record.cwd)
            self._set_field("created", format_full(record.created))
            self._set_field("origin", record.origin)
            self._set_field("parent", record.parent)
            self._set_field("id", record.id)

            excerpt = record.full_title or record.subtitle
            if excerpt and excerpt.strip() and excerpt.strip() != record.title.strip():
                self._excerpt_body.configure(text=clip(excerpt, 900))
                if not self._excerpt_head.winfo_ismapped():
                    self._excerpt_head.pack(fill="x", padx=theme.px(20), pady=(theme.px(10), theme.px(6)))
                    self._excerpt_body.pack(fill="x", padx=theme.px(20), pady=(0, theme.px(16)))
            else:
                self._excerpt_head.pack_forget()
                self._excerpt_body.pack_forget()

            # 不再在这里 update_idletasks：折行宽度由 body 的 <Configure> 驱动，
            # 内容变了宽度没变，本来就不用重排。强制同步排版一次要 80 毫秒，
            # 而这段时间里窗口是画不出来的——那才是"点一下卡一下"的体感来源。
            self.scroll.scroll_to_top()
        except tk.TclError:
            return

        if animate:
            self._animate(record)

    _FIELD_ORDER = ("cwd", "created", "origin", "parent", "id")

    def _set_field(self, key: str, value: str) -> None:
        row, label = self._fields[key]
        if not value:
            row.pack_forget()
            return
        label.configure(text=clip(value, 110))
        if row.winfo_ismapped():
            return
        # 重新插回原位，而不是直接 pack 到末尾：来源和父对话不是每行都有，
        # 一旦它们先隐藏再出现，顺序就会掉到「对话 ID」后面去。
        anchor = None
        for name in self._FIELD_ORDER[self._FIELD_ORDER.index(key) + 1:]:
            candidate = self._fields[name][0]
            if candidate.winfo_ismapped():
                anchor = candidate
                break
        if anchor is None and self._excerpt_head.winfo_ismapped():
            anchor = self._excerpt_head
        try:
            if anchor is None:
                row.pack(fill="x", padx=self.theme.px(20), pady=(0, self.theme.px(8)))
            else:
                row.pack(fill="x", padx=self.theme.px(20), pady=(0, self.theme.px(8)), before=anchor)
        except tk.TclError:
            pass

    # -- 动作 ----------------------------------------------------------

    def _open_current(self) -> None:
        if self._record is not None:
            self._on_open(self._record)

    def _copy(self, key: str) -> None:
        record = self._record
        if record is None:
            return
        value = {"title": record.title, "id": record.id, "cwd": record.cwd}.get(key, "")
        if not value:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(value)
        except tk.TclError:
            return
        button = self._copy_buttons.get(key)
        if button is None:
            return
        original = button.cget("text")
        button.configure(text="已复制", fg=self.theme.c("success"))
        try:
            self._feedback.configure(text=f"{original}成功")
        except tk.TclError:
            pass

        def restore() -> None:
            try:
                button.configure(text=original, fg=self.theme.c("text_2"))
                self._feedback.configure(text="")
            except tk.TclError:
                pass

        self.after(1300, restore)

    # -- 动画 ----------------------------------------------------------

    def _animate(self, record: codexdb.ThreadRecord) -> None:
        rule = self._rule
        turns_label = self._stats["turns"]
        target = record.turns

        def tick(t: float) -> None:
            try:
                width = rule.winfo_width() or self.theme.px(520)
                rule.delete("all")
                rule.create_rectangle(
                    0,
                    0,
                    max(2, width * ease("out_quint", t)),
                    self.theme.px(2),
                    fill=self.theme.c("accent"),
                    outline="",
                )
                if target:
                    turns_label.configure(text=str(int(round(target * ease("out_cubic", t)))))
            except tk.TclError:
                pass

        self._timeline.start(0.45, tick)

    # -- 折行 ----------------------------------------------------------

    def _on_body_configure(self, event) -> None:
        """窗口变窄时重新折行。

        wraplength 写死的话，窗口一缩窄，标题和正文的右半截就跑到可视区外面
        去了——看起来像"这段内容就到这里"，其实是被裁掉了。
        """

        self._rewrap(int(event.width))

    def _rewrap(self, width: int) -> None:
        target = max(self.theme.px(240), int(width) - self.theme.px(56))
        for widget in (self._title, self._excerpt_body):
            try:
                current = int(widget.cget("wraplength"))
            except (tk.TclError, ValueError):
                continue
            if current and abs(current - target) > 8:
                try:
                    widget.configure(wraplength=target)
                except tk.TclError:
                    pass


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------


class ChatBrowser(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        on_open: Callable[[codexdb.ThreadRecord], None],
        on_new: Callable[[], None],
        on_refresh: Callable[[], None],
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent, bg=theme.c("bg"))
        self.theme = theme
        self._on_open = on_open
        self._on_new = on_new
        self._on_refresh = on_refresh
        self._on_status = on_status or (lambda _text: None)

        self.library: Optional[codexdb.Library] = None
        self._scope = "all"
        self._query = ""
        self._include_subagents = False
        self._collapsed: set[str] = set()
        self._selected_id = ""
        self._limit = PAGE_SIZE
        self._rows: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._loading = False
        self._error = ""

        # 控件池。列表来回切筛选时复用同一批控件，只改文字和 grid 行号。
        self._row_pool: list[dict[str, Any]] = []
        self._header_pool: list[dict[str, Any]] = []
        self._active: list[tk.Widget] = []
        # 当前真正挂在布局里的控件 id。有了它，收起时只动该动的那些。
        self._shown: set[int] = set()
        self._more_frame: Optional[tk.Frame] = None
        self._more_label: Optional[tk.Label] = None
        self._message_frame: Optional[tk.Frame] = None
        self._message_command: Optional[Callable[[], None]] = None

        self._query_after: Optional[str] = None
        self._fade = Timeline(self)
        self._select_timeline = Timeline(self)
        self._indicator_timeline = Timeline(self)
        self._indicator: Optional[int] = None
        self._indicator_canvas: Optional[tk.Canvas] = None

        self._search_var = tk.StringVar(value="")
        self._search_var.trace_add("write", lambda *_: self._on_query_changed())
        self.subagent_var = tk.BooleanVar(value=False)

        self._build()
        self._render_list()

    # -- 结构 ----------------------------------------------------------

    def _build(self) -> None:
        theme = self.theme

        toolbar = tk.Frame(self, bg=theme.c("bg"))
        toolbar.pack(fill="x", pady=(0, theme.px(10)))

        self.search = SearchBox(toolbar, theme, self._search_var, self._on_query_changed)
        self.search.pack(side="left", fill="x", expand=True)

        self.chips = ChipBar(toolbar, theme, SCOPES, self._scope, self._on_scope)
        self.chips.pack(side="left", padx=(theme.px(10), theme.px(10)))

        Switch(
            toolbar,
            theme,
            "子代理",
            self.subagent_var,
            command=self._on_subagents,
            bg_key="bg",
        ).pack(side="left")

        panes = tk.Frame(self, bg=theme.c("bg"))
        panes.pack(fill="both", expand=True)

        left_outer, left_inner = card(panes, theme, "surface")
        left_outer.configure(width=theme.px(376))
        left_outer.pack(side="left", fill="y")
        left_outer.pack_propagate(False)

        self.list_host = tk.Frame(left_inner, bg=theme.c("surface"))
        self.list_host.pack(fill="both", expand=True, padx=theme.px(4), pady=theme.px(4))

        self.scroll = ScrollArea(self.list_host, theme, "surface")
        self.scroll.pack(fill="both", expand=True)
        self.list_body = self.scroll.body
        self.list_body.grid_columnconfigure(0, weight=1)

        self.skeleton = Skeleton(self.list_host, theme)

        right_outer, right_inner = card(panes, theme, "surface")
        right_outer.pack(side="left", fill="both", expand=True, padx=(theme.px(12), 0))
        self.detail = DetailPane(right_inner, theme, self._open)
        self.detail.pack(fill="both", expand=True)

        # 选中位置那根竖杠。它和行是两个控件，才能"从上一行滑到这一行"。
        self._indicator_canvas = tk.Canvas(
            self.list_body,
            width=theme.px(3),
            bg=theme.c("surface"),
            highlightthickness=0,
            bd=0,
        )
        self._indicator = self._indicator_canvas.create_polygon(
            round_points(0, 0, theme.px(3), theme.px(20), theme.px(1.5)),
            smooth=True,
            fill=theme.c("accent"),
            outline="",
        )

        self.bind("<Down>", lambda _e: self._step(1))
        self.bind("<Up>", lambda _e: self._step(-1))
        self.bind("<Return>", lambda _e: self._open_selected())
        self.bind("<Control-f>", lambda _e: self._focus_search())
        # 焦点在输入框里时，按键事件不会冒泡到外面的 Frame，所以两处都要绑。
        self.search.entry.bind("<Down>", lambda _e: self._step(1))
        self.search.entry.bind("<Up>", lambda _e: self._step(-1))
        self.search.entry.bind("<Return>", lambda _e: self._open_selected())
        self.search.entry.bind("<Escape>", lambda _e: self._clear_query())

    # -- 对外 ----------------------------------------------------------

    def show_loading(self) -> None:
        self._loading = True
        self._error = ""
        try:
            self.scroll.pack_forget()
            self.skeleton.place(x=0, y=0, relwidth=1, relheight=1)
            self.skeleton.lift()
            self.skeleton.start()
        except tk.TclError:
            pass
        # 右边不能留一张大白纸：空白和"读不出来"在视觉上没有区别。
        self.detail.show_empty("正在读取本地对话库", "标题、项目、轮次和令牌都从 Codex 自己的数据库里读。")
        self._on_status("正在读取 Codex 本地库…")

    def show_library(self, library: codexdb.Library, animate: bool = True) -> None:
        self._loading = False
        self._error = ""
        self.library = library
        self._limit = PAGE_SIZE
        try:
            self.skeleton.stop()
            self.skeleton.place_forget()
            self.scroll.pack(fill="both", expand=True)
        except tk.TclError:
            pass

        counts = library.counts()
        if library.threads:
            text = (
                f"{counts['active']} 条对话 · {counts['projects']} 个项目 · "
                f"{counts['loose']} 条无项目 · {library.elapsed_ms} ms"
            )
        else:
            text = "没有读到对话"
        self._on_status(text)
        self._render_list(animate=animate)

        target = self._selected_id if self._record_of(self._selected_id) is not None else ""
        if not target:
            first = self._first_visible()
            target = first.id if first is not None else ""
        if target:
            self.select(target, animate=False, force=True)
        else:
            self.detail.show_empty("没有可显示的对话", "换一个筛选，或者点「扫描对话」重新读一次。")

    def show_error(self, message: str) -> None:
        self._loading = False
        self._error = str(message or "读取失败")
        try:
            self.skeleton.stop()
            self.skeleton.place_forget()
            self.scroll.pack(fill="both", expand=True)
        except tk.TclError:
            pass
        self._on_status(self._error)
        self._render_list(animate=False)

    def select(self, record_id: str, animate: bool = True, force: bool = False) -> None:
        if not force and record_id == self._selected_id:
            return
        record = self._record_of(record_id)
        if record is None:
            return
        previous = self._rows.get(self._selected_id)
        self._selected_id = record_id
        current = self._rows.get(record_id)
        self.detail.show(record, animate=animate)
        self._paint_selection(previous, current, animate)
        self._place_indicator(animate=animate, row=current)

    def focus_search(self) -> None:
        self._focus_search()

    # -- 数据 ----------------------------------------------------------

    def _record_of(self, record_id: str) -> Optional[codexdb.ThreadRecord]:
        if not self.library or not record_id:
            return None
        for item in self.library.threads:
            if item.id == record_id:
                return item
        return None

    def _visible(self) -> list[codexdb.ThreadRecord]:
        if not self.library:
            return []
        archived = self._scope == "archived"
        pool = [item for item in self.library.threads if item.archived == archived]
        if not self._include_subagents:
            pool = [item for item in pool if not item.is_subagent]
        if self._scope == "projects":
            pool = [item for item in pool if item.project]
        elif self._scope == "loose":
            pool = [item for item in pool if not item.project]
        if self._query:
            pool = codexdb.search(pool, self._query)
        return pool

    def _groups(self, items: Sequence[codexdb.ThreadRecord]) -> list[tuple[str, str, list[codexdb.ThreadRecord]]]:
        """分组的顺序：置顶 → 项目（按最近活跃）→ 无项目。

        项目内部按时间倒序；置顶单独拎出来是因为它可能横跨好几个项目，
        埋在项目里就永远看不见。
        """

        pinned = [item for item in items if item.pinned]
        rest = [item for item in items if not item.pinned]

        groups: list[tuple[str, str, list[codexdb.ThreadRecord]]] = []
        if pinned:
            groups.append(("pinned", "置顶", pinned))

        buckets: dict[str, list[codexdb.ThreadRecord]] = {}
        for item in rest:
            buckets.setdefault(item.project or "", []).append(item)

        named = sorted(
            ((name, rows) for name, rows in buckets.items() if name),
            key=lambda pair: max(row.updated for row in pair[1]),
            reverse=True,
        )
        for name, rows in named:
            groups.append((f"project:{name}", name, rows))

        loose = buckets.get("")
        if loose:
            groups.append(("loose", "无项目", loose))
        return groups

    def _first_visible(self) -> Optional[codexdb.ThreadRecord]:
        for _key, _label, rows in self._groups(self._visible()):
            if rows:
                return rows[0]
        return None

    # -- 列表 ----------------------------------------------------------

    def _on_query_changed(self) -> None:
        if self._query_after is not None:
            try:
                self.after_cancel(self._query_after)
            except tk.TclError:
                pass
        self._query_after = self.after(110, self._apply_query)

    def _apply_query(self) -> None:
        self._query_after = None
        self._query = self._search_var.get().strip()
        self._limit = PAGE_SIZE
        self._render_list()
        self._auto_select()

    def _on_scope(self, value: str) -> None:
        self._scope = value
        self._limit = PAGE_SIZE
        self._render_list()
        self._auto_select()

    def _on_subagents(self) -> None:
        self._include_subagents = bool(self.subagent_var.get())
        self._limit = PAGE_SIZE
        self._render_list()
        self._auto_select()

    def _auto_select(self) -> None:
        if self._loading or self.library is None:
            return
        if self._selected_id and self._record_of(self._selected_id) in self._visible():
            return
        first = self._first_visible()
        if first is not None:
            self.select(first.id, animate=False)
        else:
            self._selected_id = ""
            self.detail.show_empty("没有匹配的对话", "换个词，或把筛选切回「全部」。")

    # -- 渲染（复用控件） ----------------------------------------------

    def _render_list(self, animate: bool = False) -> None:
        if self._loading:
            self._hide_all()
            return

        self._rows.clear()
        self._order.clear()
        used: list[tk.Widget] = []

        if self._error:
            used.append(self._message("读不出来", self._error, "重试", self._on_refresh))
            self._finish_render(used)
            return

        if self.library is None:
            used.append(self._message("还没有读取对话", "点「扫描对话」读一次 Codex 的本地库。", "开始扫描", self._on_refresh))
            self._finish_render(used)
            return

        items = self._visible()
        if not items:
            if self._query:
                used.append(
                    self._message(
                        "没有匹配的对话",
                        f"「{clip(self._query, 24)}」没有命中标题、项目、目录或 ID。",
                        "清空搜索",
                        self._clear_query,
                    )
                )
            elif self._scope == "archived":
                used.append(self._message("没有归档的对话", "Codex 里归档过的对话会出现在这里。", "", None))
            elif self._scope == "loose":
                used.append(self._message("没有无项目的对话", "所有对话都归到了某个项目目录下。", "", None))
            else:
                used.append(self._message("没有对话", "Codex 里还没有记录。", "重新扫描", self._on_refresh))
            self._finish_render(used)
            return

        drawn = 0
        row_index = 0
        header_index = 0
        position = 0
        for key, label, rows in self._groups(items):
            if drawn >= self._limit:
                break
            collapsed = key in self._collapsed
            header = self._header(header_index)
            header_index += 1
            self._update_header(header, key, label, len(rows), collapsed)
            header["row"].grid(row=position, column=0, sticky="ew")
            used.append(header["row"])
            position += 1
            if collapsed:
                continue
            for record in rows:
                if drawn >= self._limit:
                    break
                info = self._row(row_index)
                row_index += 1
                self._update_row(info, record)
                info["frame"].grid(row=position, column=0, sticky="ew")
                used.append(info["frame"])
                position += 1
                self._rows[record.id] = info
                self._order.append(record.id)
                drawn += 1

        remaining = len(items) - drawn
        if remaining > 0:
            more = self._more_row(remaining)
            more.grid(row=position, column=0, sticky="ew")
            used.append(more)

        self._finish_render(used)

        if animate:
            self._fade_rows()

    def _finish_render(self, used: list[tk.Widget]) -> None:
        """把这一轮没用到的控件藏起来，而不是销毁。

        grid_remove 只摘掉布局，控件本身还在——下次筛选切回来时直接复用，
        不会再有"销毁 → 露出底色 → 重建"的那一下白闪。

        只对"上一轮还露着"的控件动手：grid_remove 每次都会让 Tk 重算一遍
        几何，对已经藏起来的控件再喊一次是白花钱。池子里几百个行的时候，
        这一下能省掉十几毫秒。
        """

        active = {id(widget) for widget in used}
        stale = self._shown - active
        if stale:
            for info in self._row_pool:
                if id(info["frame"]) in stale:
                    try:
                        info["frame"].grid_remove()
                    except tk.TclError:
                        pass
            for info in self._header_pool:
                if id(info["row"]) in stale:
                    try:
                        info["row"].grid_remove()
                    except tk.TclError:
                        pass
            for frame in (self._more_frame, self._message_frame):
                if frame is not None and id(frame) in stale:
                    try:
                        frame.grid_remove()
                    except tk.TclError:
                        pass

        self._shown = active
        self._active = used
        # 不在这里 update_idletasks：强制同步布局会把一次筛选卡住一百毫秒。
        # 让 Tk 按自己的节奏排版，竖杠等这一帧排完再放（after_idle）。
        try:
            self.after_idle(self._settle_indicator)
        except tk.TclError:
            pass

    def _settle_indicator(self) -> None:
        self._place_indicator(animate=False)

    def _hide_all(self) -> None:
        for info in self._row_pool:
            try:
                info["frame"].grid_remove()
            except tk.TclError:
                pass
        for info in self._header_pool:
            try:
                info["row"].grid_remove()
            except tk.TclError:
                pass
        for frame in (self._more_frame, self._message_frame):
            if frame is not None:
                try:
                    frame.grid_remove()
                except tk.TclError:
                    pass
        self._active = []
        self._shown = set()

    # -- 控件池 --------------------------------------------------------

    def _row(self, index: int) -> dict[str, Any]:
        while index >= len(self._row_pool):
            self._row_pool.append(self._create_row())
        return self._row_pool[index]

    def _create_row(self) -> dict[str, Any]:
        theme = self.theme
        base = theme.c("surface")

        frame = tk.Frame(self.list_body, bg=base, cursor="hand2")
        inner = tk.Frame(frame, bg=base)
        inner.pack(fill="x", padx=(theme.px(14), theme.px(12)), pady=(theme.px(7), theme.px(7)))
        inner.grid_columnconfigure(0, weight=1)

        title = tk.Label(
            inner,
            text="",
            bg=base,
            fg=theme.c("text"),
            font=theme.font("body"),
            anchor="w",
            justify="left",
            wraplength=theme.px(238),
        )
        title.grid(row=0, column=0, sticky="w")

        when = tk.Label(
            inner,
            text="",
            bg=base,
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="e",
        )
        when.grid(row=0, column=1, sticky="e", padx=(theme.px(8), 0))

        meta = tk.Label(
            inner,
            text="",
            bg=base,
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        )
        meta.grid(row=1, column=0, columnspan=2, sticky="w", pady=(theme.px(3), 0))

        info: dict[str, Any] = {
            "frame": frame,
            "inner": inner,
            "title": title,
            "when": when,
            "meta": meta,
            "record": None,
        }
        for widget in (frame, inner, title, when, meta):
            widget.bind("<Button-1>", lambda _e, i=info: self._click_row(i))
            widget.bind("<Double-Button-1>", lambda _e, i=info: self._open_row(i))
            widget.bind("<Enter>", lambda _e, i=info: self._hover_row(i, True))
            widget.bind("<Leave>", lambda _e, i=info: self._hover_row(i, False))
        return info

    def _update_row(self, info: dict[str, Any], record: codexdb.ThreadRecord) -> None:
        theme = self.theme
        selected = record.id == self._selected_id
        color = theme.c("accent_soft") if selected else theme.c("surface")
        info["record"] = record

        meta_bits = []
        if record.turns:
            meta_bits.append(f"{record.turns} 轮")
        if record.tokens:
            meta_bits.append(format_tokens(record.tokens))
        if record.is_subagent:
            meta_bits.append(record.agent or "子代理")
        if not meta_bits:
            meta_bits.append(record.model or "无记录")

        try:
            info["frame"].configure(bg=color)
            info["inner"].configure(bg=color)
            info["title"].configure(text=clip(record.title, 42), bg=color, fg=theme.c("text"))
            info["when"].configure(text=format_when(record.updated), bg=color)
            info["meta"].configure(text=clip(" · ".join(meta_bits), 46), bg=color)
        except tk.TclError:
            pass

    def _click_row(self, info: dict[str, Any]) -> None:
        record = info.get("record")
        if record is not None:
            self.select(record.id)

    def _open_row(self, info: dict[str, Any]) -> None:
        record = info.get("record")
        if record is not None:
            self.select(record.id)
            self._open(record)

    def _hover_row(self, info: dict[str, Any], entering: bool) -> None:
        record = info.get("record")
        if record is None or record.id == self._selected_id:
            return
        color = self.theme.c("surface_2") if entering else self.theme.c("surface")
        self._paint_row(info, color)

    def _header(self, index: int) -> dict[str, Any]:
        while index >= len(self._header_pool):
            self._header_pool.append(self._create_header())
        return self._header_pool[index]

    def _create_header(self) -> dict[str, Any]:
        theme = self.theme
        row = tk.Frame(self.list_body, bg=theme.c("surface"), cursor="hand2")
        chevron = tk.Canvas(
            row,
            width=theme.px(10),
            height=theme.px(10),
            bg=theme.c("surface"),
            highlightthickness=0,
            bd=0,
        )
        chevron.pack(side="left", padx=(theme.px(10), theme.px(6)))
        label = tk.Label(
            row,
            text="",
            bg=theme.c("surface"),
            fg=theme.c("text_2"),
            font=theme.font("small_strong"),
            anchor="w",
        )
        label.pack(side="left")
        count = tk.Label(
            row,
            text="",
            bg=theme.c("surface"),
            fg=theme.c("text_3"),
            font=theme.font("tiny"),
            anchor="w",
        )
        count.pack(side="left", padx=(theme.px(6), 0))
        info: dict[str, Any] = {
            "row": row,
            "chevron": chevron,
            "label": label,
            "count": count,
            "key": "",
        }
        for widget in (row, chevron, label, count):
            widget.bind("<Button-1>", lambda _e, i=info: self._toggle_group(i["key"]))
        return info

    def _update_header(self, info: dict[str, Any], key: str, label: str, count: int, collapsed: bool) -> None:
        info["key"] = key
        try:
            info["label"].configure(text=label)
            info["count"].configure(text=str(count))
            info["row"].grid_configure(pady=(self.theme.px(8), self.theme.px(2)))
        except tk.TclError:
            pass
        self._draw_chevron(info["chevron"], collapsed)

    def _draw_chevron(self, canvas: tk.Canvas, collapsed: bool) -> None:
        theme = self.theme
        try:
            canvas.delete("all")
            size = theme.px(10)
            if collapsed:
                points = (size * 0.34, size * 0.2, size * 0.34, size * 0.8, size * 0.72, size * 0.5)
            else:
                points = (size * 0.2, size * 0.34, size * 0.8, size * 0.34, size * 0.5, size * 0.72)
            canvas.create_polygon(points, fill=theme.c("text_3"), outline="")
        except tk.TclError:
            pass

    def _toggle_group(self, key: str) -> None:
        if not key:
            return
        if key in self._collapsed:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        self._render_list(animate=False)
        self._auto_select()

    # -- 更多 / 空状态 -------------------------------------------------

    def _more_row(self, remaining: int) -> tk.Frame:
        if self._more_frame is None:
            theme = self.theme
            self._more_frame = tk.Frame(self.list_body, bg=theme.c("surface"))
            self._more_label = tk.Label(
                self._more_frame,
                text="",
                bg=theme.c("surface_2"),
                fg=theme.c("text_2"),
                font=theme.font("small"),
                padx=theme.px(12),
                pady=theme.px(6),
                cursor="hand2",
            )
            self._more_label.pack(pady=(theme.px(8), theme.px(10)))
            self._more_label.bind("<Button-1>", lambda _e: self._load_more())
        if self._more_label is not None:
            self._more_label.configure(text=f"还有 {remaining} 条 · 继续加载")
        return self._more_frame

    def _message(self, title: str, hint: str, action: str, command: Optional[Callable[[], None]]) -> tk.Frame:
        theme = self.theme
        if self._message_frame is None:
            self._message_frame = tk.Frame(self.list_body, bg=theme.c("surface"))
            holder = tk.Frame(self._message_frame, bg=theme.c("surface"))
            holder.pack(fill="both", expand=True, pady=(theme.px(56), 0))
            self._message_title = tk.Label(
                holder,
                text="",
                bg=theme.c("surface"),
                fg=theme.c("text_2"),
                font=theme.font("strong"),
            )
            self._message_title.pack()
            self._message_hint = tk.Label(
                holder,
                text="",
                bg=theme.c("surface"),
                fg=theme.c("text_3"),
                font=theme.font("small"),
                wraplength=theme.px(280),
                justify="center",
            )
            self._message_hint.pack(pady=(theme.px(6), theme.px(12)))
            self._message_action = tk.Label(
                holder,
                text="",
                bg=theme.c("surface_3"),
                fg=theme.c("text"),
                font=theme.font("small"),
                padx=theme.px(12),
                pady=theme.px(5),
                cursor="hand2",
            )
            self._message_action.pack()
            self._message_action.bind("<Button-1>", lambda _e: self._run_message_action())
        self._message_command = command
        self._message_title.configure(text=title)
        self._message_hint.configure(text=hint)
        if action and command is not None:
            self._message_action.configure(text=action)
            if not self._message_action.winfo_ismapped():
                self._message_action.pack()
        else:
            self._message_action.pack_forget()
        return self._message_frame

    def _run_message_action(self) -> None:
        if self._message_command is not None:
            self._message_command()

    def _load_more(self) -> None:
        self._limit += PAGE_SIZE
        self._render_list(animate=False)
        self._place_indicator(animate=False)

    # -- 选中 ----------------------------------------------------------

    def _paint_selection(
        self,
        previous: Optional[dict[str, Any]],
        current: Optional[dict[str, Any]],
        animate: bool,
    ) -> None:
        theme = self.theme
        active = theme.c("accent_soft")
        idle = theme.c("surface")
        if not animate or previous is None or current is None or previous is current:
            for info, color in ((previous, idle), (current, active)):
                if info is not None:
                    self._paint_row(info, color)
            return

        def tick(t: float) -> None:
            eased = ease("out_cubic", t)
            self._paint_row(previous, blend(active, idle, eased))
            self._paint_row(current, blend(idle, active, eased))

        self._select_timeline.start(0.16, tick)

    def _paint_row(self, info: dict[str, Any], color: str) -> None:
        try:
            info["frame"].configure(bg=color)
            info["inner"].configure(bg=color)
            for key in ("title", "meta", "when"):
                info[key].configure(bg=color)
        except (tk.TclError, KeyError):
            pass

    def _place_indicator(self, animate: bool, row: Optional[dict[str, Any]] = None) -> None:
        theme = self.theme
        canvas = self._indicator_canvas
        if canvas is None:
            return
        info = row if row is not None else self._rows.get(self._selected_id)
        if info is None:
            try:
                canvas.place_forget()
            except tk.TclError:
                pass
            return
        try:
            top = info["frame"].winfo_y()
            height = info["frame"].winfo_height()
        except tk.TclError:
            return
        if height <= 1:
            return

        try:
            current = canvas.winfo_y()
            visible = bool(canvas.winfo_ismapped())
        except tk.TclError:
            return

        # 竖杠是页面刚建好时创建的，比后来复用的行还早，默认压在行下面。
        # 每次定位都提一次，保证它画在行的左边那条缝上而不是被盖住。
        try:
            canvas.lift()
        except tk.TclError:
            pass

        if not animate or not visible or abs(current - top) < 1:
            try:
                canvas.place(x=0, y=top, width=theme.px(3), height=height)
                canvas.coords(self._indicator, *round_points(0, 0, theme.px(3), height, theme.px(1.5)))
            except tk.TclError:
                pass
            return

        start_y = float(current)
        start_h = float(canvas.winfo_height())

        def tick(t: float) -> None:
            eased = ease("out_cubic", t)
            y = start_y + (top - start_y) * eased
            h = start_h + (height - start_h) * eased
            try:
                canvas.place_configure(y=int(round(y)), height=int(round(h)))
                canvas.coords(self._indicator, *round_points(0, 0, theme.px(3), h, theme.px(1.5)))
            except tk.TclError:
                pass

        self._indicator_timeline.start(0.2, tick)

    # -- 键盘 ----------------------------------------------------------

    def _step(self, direction: int) -> None:
        if not self._order:
            return
        try:
            index = self._order.index(self._selected_id)
        except ValueError:
            index = -1
        index = max(0, min(len(self._order) - 1, index + direction))
        record_id = self._order[index]
        self.select(record_id)
        info = self._rows.get(record_id)
        if info is not None:
            self._ensure_visible(info["frame"])

    def _ensure_visible(self, widget: tk.Widget) -> None:
        try:
            canvas = self.scroll.canvas
            total = max(1, self.list_body.winfo_height())
            view = canvas.winfo_height()
            top = widget.winfo_y()
            bottom = top + widget.winfo_height()
            start = canvas.canvasy(0)
            if top < start:
                canvas.yview_moveto(max(0.0, top / total))
            elif bottom > start + view:
                canvas.yview_moveto(max(0.0, (bottom - view) / total))
        except tk.TclError:
            pass

    def _open_selected(self) -> None:
        record = self._record_of(self._selected_id)
        if record is not None:
            self._open(record)

    def _open(self, record: codexdb.ThreadRecord) -> None:
        self._on_open(record)

    def _focus_search(self) -> None:
        try:
            self.search.entry.focus_set()
        except tk.TclError:
            pass

    def _clear_query(self) -> None:
        self._search_var.set("")
        self._apply_query()

    # -- 逐行淡入 ------------------------------------------------------

    def _fade_rows(self) -> None:
        """逐行淡入：只补间标题的颜色，不动背景。

        只在第一次加载时播。每次打字、每次切筛选都重播一遍，看起来就是
        "屏幕在闪"，而不是"新内容来了"。
        """

        theme = self.theme
        surface = theme.c("surface")
        text = theme.c("text")
        rows = []
        for index, record_id in enumerate(self._order[:STAGGER_ROWS]):
            info = self._rows.get(record_id)
            if info is None:
                continue
            rows.append((info, index * STAGGER_STEP))
        if not rows:
            return

        for info, _delay in rows:
            try:
                info["title"].configure(fg=surface)
            except tk.TclError:
                pass

        total = FADE_SECONDS + rows[-1][1]

        def tick(t: float) -> None:
            elapsed = t * total
            for info, delay in rows:
                local = 0.0 if total <= 0 else (elapsed - delay) / FADE_SECONDS
                local = 0.0 if local < 0 else 1.0 if local > 1 else local
                eased = ease("out_cubic", local)
                try:
                    info["title"].configure(fg=blend(surface, text, eased))
                except tk.TclError:
                    return

        self._fade.start(total, tick)
