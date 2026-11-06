# -*- coding: utf-8 -*-
"""界面识别。

这里不使用任何模板文件，也不需要用户保存截图：算法直接在目标窗口的
客户区截图上寻找"输入框面板"和"发送按钮"，因此窗口缩放、DPI 变化、
会话切换导致的重排都不会让坐标失效。

三个核心判断：

1. 输入框是一块横向贯通的独立面板，上下各有一条方向相反的长横向边缘；
2. 发送按钮是输入框右下角区域里对比度最高、接近正方形的紧凑色块；
3. 粘贴/发送是否成功，用同一区域的像素变化来验证，而不是靠固定等待。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .imaging import Bitmap

Color = tuple[int, int, int]


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def offset(self, dx: int, dy: int) -> "Rect":
        return Rect(self.left + dx, self.top + dy, self.right + dx, self.bottom + dy)

    def as_dict(self) -> dict[str, int]:
        return {
            "left": int(self.left),
            "top": int(self.top),
            "right": int(self.right),
            "bottom": int(self.bottom),
        }

    @classmethod
    def from_dict(cls, value: object) -> Optional["Rect"]:
        if not isinstance(value, dict):
            return None
        try:
            rect = cls(
                int(value["left"]),
                int(value["top"]),
                int(value["right"]),
                int(value["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if rect.width <= 0 or rect.height <= 0:
            return None
        return rect

    def scale_to(self, from_size: tuple[int, int], to_size: tuple[int, int]) -> "Rect":
        """把旧窗口尺寸下的矩形按比例映射到新尺寸。"""

        from_w, from_h = max(1, int(from_size[0])), max(1, int(from_size[1]))
        to_w, to_h = max(1, int(to_size[0])), max(1, int(to_size[1]))
        if from_w == to_w and from_h == to_h:
            return self
        sx = to_w / from_w
        sy = to_h / from_h
        return Rect(
            int(round(self.left * sx)),
            int(round(self.top * sy)),
            int(round(self.right * sx)),
            int(round(self.bottom * sy)),
        )


# ---------------------------------------------------------------------------
# 颜色工具
# ---------------------------------------------------------------------------


def _dist(a: Color, b: Color) -> int:
    """最大通道差，比欧氏距离更抗压缩噪声。"""

    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def _modal(points: Sequence[Color]) -> tuple[Color, float]:
    """返回出现最多的颜色，以及落在该颜色附近的样本比例。"""

    if not points:
        return ((0, 0, 0), 0.0)
    buckets: dict[tuple[int, int, int], int] = {}
    for red, green, blue in points:
        key = (red // 8, green // 8, blue // 8)
        buckets[key] = buckets.get(key, 0) + 1
    key = max(buckets, key=lambda item: buckets[item])

    total_r = total_g = total_b = 0
    count = 0
    for red, green, blue in points:
        if (red // 8, green // 8, blue // 8) == key:
            total_r += red
            total_g += green
            total_b += blue
            count += 1
    if count == 0:
        return ((0, 0, 0), 0.0)
    color = (total_r // count, total_g // count, total_b // count)
    near = sum(1 for point in points if _dist(point, color) <= 6)
    return color, near / len(points)


def _spread_xs(width: int, low: float, high: float, count: int) -> list[int]:
    left = int(width * low)
    right = int(width * high)
    return _spread_between(left, right, count, width)


def _spread_between(left: int, right: int, count: int, width: int) -> list[int]:
    if right - left < count or count < 2:
        return []
    step = max(1, (right - left) // (count - 1))
    values = {min(width - 2, max(1, left + index * step)) for index in range(count)}
    return sorted(values)


# ---------------------------------------------------------------------------
# 面板检测
# ---------------------------------------------------------------------------


def _find_panel(
    bitmap: Bitmap,
    xs: Sequence[int],
    y_start: int,
    y_end: int,
    min_height: int,
    max_height: int,
    min_share: float = 0.50,
    min_delta: int = 6,
    limit: int = 5,
) -> list[tuple[int, int, Color]]:
    """在竖直方向找方向相反的长横向边缘，按可信度返回候选面板。

    只返回一个候选时，容易把"两块内容之间的空白"当成面板：
    空白区域同样均匀，上下也各有一条边缘。调用方需要再用
    _edge_support 校验这两条边缘是否真的横贯整个面板。
    """

    height = bitmap.height
    if len(xs) < 5:
        return []
    y_start = max(2, int(y_start))
    y_end = min(height - 2, int(y_end))
    if y_end - y_start < min_height:
        return []

    need = max(4, int(len(xs) * 0.55))
    edges: list[tuple[int, int, int]] = []
    for y in range(y_start, y_end + 1):
        up = 0
        down = 0
        for x in xs:
            delta = bitmap.lum(x, y) - bitmap.lum(x, y - 1)
            if delta >= 5:
                up += 1
            elif delta <= -5:
                down += 1
        if up >= down:
            strong, weak, sign = up, down, 1
        else:
            strong, weak, sign = down, up, -1
        # 长边缘在多数采样列上方向一致；文字行两个方向都有，会被过滤掉。
        if strong >= need and strong >= 2 * weak + 1:
            edges.append((y, sign, strong))

    if len(edges) < 2:
        return []
    if len(edges) > 28:
        edges = sorted(edges, key=lambda item: item[2], reverse=True)[:28]
        edges.sort(key=lambda item: item[0])

    found: list[tuple[float, int, int, Color]] = []
    for index in range(len(edges) - 1):
        y_top, sign_top, strength_top = edges[index]
        for later in range(index + 1, len(edges)):
            y_bottom, sign_bottom, strength_bottom = edges[later]
            if sign_top == sign_bottom:
                continue
            panel_height = y_bottom - y_top
            if panel_height < min_height or panel_height > max_height:
                continue

            step = max(3, panel_height // 8)
            points: list[Color] = []
            for y in range(y_top + 5, y_bottom - 4, step):
                for x in xs:
                    points.append(bitmap.rgb(x, y))
            if len(points) < 16:
                continue
            inside, share = _modal(points)
            if share < min_share:
                continue

            if y_top >= 6:
                outer_y = y_top - 4
            else:
                outer_y = min(height - 1, y_bottom + 4)
            outer, _ = _modal([bitmap.rgb(x, outer_y) for x in xs])
            if _dist(inside, outer) < min_delta:
                continue

            score = (
                strength_top
                + strength_bottom
                + min(panel_height, 240) * 0.12
                + (y_bottom / height) * 10.0
            )
            found.append((score, y_top, y_bottom, inside))

    if not found:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return [(top, bottom, inside) for _score, top, bottom, inside in found[: max(1, limit)]]


def _edge_support(bitmap: Bitmap, y: int, left: int, right: int) -> float:
    """某一行在 [left, right] 上真的存在横向边缘的比例。"""

    if y < 1 or right - left < 12:
        return 0.0
    step = max(1, (right - left) // 32)
    up = down = total = 0
    for x in range(left + 2, right - 2, step):
        delta = bitmap.lum(x, y) - bitmap.lum(x, y - 1)
        total += 1
        if delta >= 4:
            up += 1
        elif delta <= -4:
            down += 1
    if not total:
        return 0.0
    return max(up, down) / total


def _horizontal_extent(
    bitmap: Bitmap,
    y: int,
    base: Color,
    seed_x: Optional[int] = None,
    tolerance: int = 10,
    patience: int = 8,
) -> tuple[int, int]:
    """从 seed_x 向两侧扩展，得到面板的左右边界。"""

    width = bitmap.width
    if y < 0 or y >= bitmap.height:
        return (0, width - 1)
    center = width // 2 if seed_x is None else max(1, min(width - 2, int(seed_x)))

    left = center
    misses = 0
    for x in range(center - 1, -1, -1):
        if _dist(bitmap.rgb(x, y), base) > tolerance:
            misses += 1
            if misses >= patience:
                break
        else:
            misses = 0
            left = x

    right = center
    misses = 0
    for x in range(center + 1, width):
        if _dist(bitmap.rgb(x, y), base) > tolerance:
            misses += 1
            if misses >= patience:
                break
        else:
            misses = 0
            right = x

    return left, right


def _panel_extent(
    bitmap: Bitmap,
    top: int,
    bottom: int,
    base: Color,
    seed_x: Optional[int] = None,
) -> tuple[int, int]:
    """在多行上取面板左右边界。

    只扫一行容易被输入框里的按钮或长文本截断（发送按钮正好在右侧），
    因此取多行的最外侧结果：左边界取最小，右边界取最大。
    """

    span = max(1, bottom - top)
    rows = []
    for ratio in (0.12, 0.32, 0.50, 0.68, 0.88):
        y = top + int(span * ratio)
        rows.append(min(bitmap.height - 1, max(0, y)))

    lefts: list[int] = []
    rights: list[int] = []
    for y in rows:
        left, right = _horizontal_extent(bitmap, y, base, seed_x=seed_x)
        lefts.append(left)
        rights.append(right)
    if not lefts:
        return 0, bitmap.width - 1
    return min(lefts), max(rights)


# ---------------------------------------------------------------------------
# 对外识别接口
# ---------------------------------------------------------------------------


def detect_panel_around(
    bitmap: Bitmap,
    seed_x: int,
    seed_y: int,
    probe_y: int = 0,
    tolerance: int = 12,
    max_scan: int = 460,
) -> Optional[Rect]:
    """从一个确定位于面板内部的点出发，向四周扩展到面板边界。

    已知"某个像素属于输入框"时，这比在整窗上猜面板稳得多：
    输入框宽窄、偏左偏右、窗口几栏布局，都不影响这一步。
    """

    width, height = bitmap.width, bitmap.height
    if width < 40 or height < 40:
        return None
    seed_x = max(1, min(width - 2, int(seed_x)))
    seed_y = max(1, min(height - 2, int(seed_y)))
    base = bitmap.rgb(seed_x, seed_y)

    # 横向用一条"干净"的行来量：种子行可能正好穿过文字或按钮，字符间隙会让
    # 扩展提前停下。多试几行取最宽的那次——被挡住的行只会更窄，不会更宽。
    rows: list[int] = []
    for delta in (0, -8, 8, -16, 16, -26, 26, -38, 38):
        candidate = int(probe_y or seed_y) + delta
        if 1 <= candidate < height - 1:
            rows.append(candidate)
    left, right = seed_x, seed_x
    for row in rows:
        if _dist(bitmap.rgb(seed_x, row), base) > tolerance:
            continue  # 这一行已经不在面板里了
        row_left, row_right = _horizontal_extent(bitmap, row, base, seed_x=seed_x, tolerance=tolerance)
        if row_left < left:
            left = row_left
        if row_right > right:
            right = row_right

    top = seed_y
    for y in range(seed_y - 1, max(0, seed_y - max_scan), -1):
        if _dist(bitmap.rgb(seed_x, y), base) > tolerance:
            top = y + 1
            break

    bottom = seed_y
    for y in range(seed_y + 1, min(height - 1, seed_y + max_scan)):
        if _dist(bitmap.rgb(seed_x, y), base) > tolerance:
            bottom = y - 1
            break

    if right - left < 120 or bottom - top < 30:
        return None
    return Rect(left, top, right + 1, bottom + 1)


def detect_composer(bitmap: Bitmap) -> Optional[Rect]:
    """识别对话输入框。返回客户区坐标系下的矩形。"""

    width, height = bitmap.width, bitmap.height
    if width < 360 or height < 320:
        return None

    # 采样列必须落在消息区内部：右侧一旦展开代码面板，输入框就不再居中，
    # 按整窗宽度取样会有一半落在面板上，边缘检测自然失败。
    chat_left, chat_right = detect_chat_bounds(bitmap)
    span = max(80, chat_right - chat_left)
    xs = _spread_between(
        chat_left + int(span * 0.16),
        chat_right - int(span * 0.16),
        11,
        width,
    )
    if len(xs) < 5:
        return None

    candidates = _find_panel(
        bitmap,
        xs,
        int(height * 0.42),
        height - 2,
        min_height=34,
        # 输入框是个矮面板。上限放宽到 28% 是为了容纳多行内容，
        # 但不能更大：否则"整个下半屏的空白"会被当成候选，把真正的输入框挤掉。
        max_height=int(height * 0.28),
        # 候选按"高度加分"排序，长条空白排在前面，真正的输入框往往在名单末尾。
        # 这里必须多留一些候选，交给下面的边缘校验去筛。
        limit=24,
    )
    min_width = int(span * 0.34)
    scored: list[tuple[float, int, int, int, int]] = []
    for top, bottom, inside in candidates:
        if bottom < int(height * 0.55):
            continue
        left, right = _panel_extent(bitmap, top, bottom, inside)
        if right - left < min_width:
            continue
        # 输入框是消息区里的一块面板，不可能从窗口左边一直铺到右边。
        # 少了这道校验，"下半屏的空白"会被当成输入框，点击位置就全错了。
        if left < chat_left - 8 or right > chat_right + 8:
            continue
        # 真正的面板上下两条边缘必须横贯面板本身，否则只是内容块之间的空白。
        if _edge_support(bitmap, top, left, right) < 0.60:
            continue
        if _edge_support(bitmap, bottom, left, right) < 0.60:
            continue
        # 输入框贴着窗口底部，而且是个矮面板：底边越靠下越像，高度越大越不像。
        score = (bottom / max(1, height)) * 100.0 - (bottom - top) * 0.08
        scored.append((score, top, bottom, left, right))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    _score, top, bottom, left, right = scored[0]
    return Rect(left, top, right + 1, bottom)


def detect_send_button(bitmap: Bitmap, composer: Rect) -> Optional[tuple[int, int]]:
    """在输入框右下角寻找发送按钮，返回按钮中心的客户区坐标。

    发送按钮是输入框里最靠右、最靠下的那个"实心圆"。
    旁边常有一个麦克风（细线条图标，填充率低）和若干文字按钮，
    所以除了位置，还要看形状：必须是接近正圆、内部被填满的色块。
    """

    if composer.width < 140 or composer.height < 42:
        return None

    roi_width = max(110, min(200, int(composer.width * 0.22)))
    roi_height = max(56, min(130, int(composer.height * 0.75)))
    left = max(composer.left + 4, composer.right - roi_width)
    top = max(composer.top + 4, composer.bottom - roi_height)
    # 识别出的输入框可能比真实面板矮一点，ROI 往下、往右各多留一点余量，
    # 否则按钮下半截被切掉，会退而选中旁边的麦克风。
    right = min(bitmap.width - 2, composer.right + 8)
    bottom = min(bitmap.height - 2, composer.bottom + 12)
    width = right - left
    height = bottom - top
    if width < 30 or height < 30:
        return None

    sample_right = min(composer.right - 8, composer.left + max(140, composer.width // 2))
    background_points: list[Color] = []
    for y in range(composer.top + 6, composer.bottom - 5, 5):
        for x in range(composer.left + 8, max(composer.left + 10, sample_right), 6):
            background_points.append(bitmap.rgb(x, y))
    if len(background_points) < 20:
        return None
    background, _ = _modal(background_points)

    mask = bytearray(width * height)
    for row in range(height):
        source_y = top + row
        base = row * width
        for column in range(width):
            if _dist(bitmap.rgb(left + column, source_y), background) >= 34:
                mask[base + column] = 1

    seen = bytearray(width * height)
    best: Optional[tuple[float, int, int]] = None

    for index in range(width * height):
        if not mask[index] or seen[index]:
            continue
        stack = [index]
        seen[index] = 1
        min_x = max_x = index % width
        min_y = max_y = index // width
        area = 0
        while stack:
            current = stack.pop()
            row, column = divmod(current, width)
            area += 1
            if column < min_x:
                min_x = column
            if column > max_x:
                max_x = column
            if row < min_y:
                min_y = row
            if row > max_y:
                max_y = row
            if column > 0 and mask[current - 1] and not seen[current - 1]:
                seen[current - 1] = 1
                stack.append(current - 1)
            if column < width - 1 and mask[current + 1] and not seen[current + 1]:
                seen[current + 1] = 1
                stack.append(current + 1)
            if row > 0 and mask[current - width] and not seen[current - width]:
                seen[current - width] = 1
                stack.append(current - width)
            if row < height - 1 and mask[current + width] and not seen[current + width]:
                seen[current + width] = 1
                stack.append(current + width)

        box_width = max_x - min_x + 1
        box_height = max_y - min_y + 1
        if box_width < 12 or box_height < 12 or box_width > 76 or box_height > 76:
            continue
        aspect = box_width / box_height
        # 发送按钮是实心圆；麦克风、加号这些细线条图标过不了这两关。
        if aspect < 0.72 or aspect > 1.4:
            continue
        fill = area / (box_width * box_height)
        if fill < 0.5:
            continue
        # 贴住 ROI 边缘的色块多半是输入框圆角外侧的背景，不能当成按钮。
        if min_x <= 0 or max_x >= width - 1 or min_y <= 0 or max_y >= height - 1:
            continue

        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2
        dx = (width - 1 - center_x) / width
        dy = (height - 1 - center_y) / height
        score = (1 - dx) * 0.55 + (1 - dy) * 0.55 + fill * 0.8 + (1 - abs(aspect - 1)) * 0.4
        if best is None or score > best[0]:
            best = (score, left + int(center_x), top + int(center_y))

    if best is None:
        return None
    return best[1], best[2]


def detect_panels(bitmap: Bitmap) -> list[int]:
    """窗口里所有稳定的竖直分隔线（升序）。

    侧边栏、消息区、右侧代码面板之间各有一条 1px 的亮线，几乎每个纵向位置
    都存在，所以按"出现次数接近峰值的那几列"来判定。滚动条、输入框边这些
    只在一小段高度上出现的线，出现次数远低于峰值，会被过滤掉。
    """

    width, height = bitmap.width, bitmap.height
    if width < 240 or height < 120:
        return []
    step_y = max(2, height // 48)
    counts: dict[int, int] = {}
    for y in range(4, height - 4, step_y):
        row = [bitmap.lum(x, y) for x in range(0, width - 2)]
        for x in range(40, width - 2):
            here = row[x]
            # 分隔线可能是 1px 也可能是 2px（不同缩放比例下不一样），
            # 所以比的是"线的左侧"和"线的右侧"，而不是紧挨着的两列。
            if here - row[x - 1] >= 6 and here - row[x + 2] >= 6:
                counts[x] = counts.get(x, 0) + 1

    if not counts:
        return []
    peak = max(counts.values())
    if peak < max(3, (height // step_y) // 4):
        return []
    strong = sorted(x for x, hits in counts.items() if hits >= peak * 0.9)
    # 相邻两列常常是同一条线的抗锯齿残留，合并成一个
    merged: list[int] = []
    for x in strong:
        if merged and x - merged[-1] <= 2:
            continue
        merged.append(x)
    return merged


def detect_chat_bounds(bitmap: Bitmap) -> tuple[int, int]:
    """消息区的左右边界。左侧是侧边栏，右侧可能是代码面板。"""

    width = bitmap.width
    dividers = detect_panels(bitmap)
    left = 0
    for x in dividers:
        if x >= width * 0.09:
            left = x
            break
    right = width
    for x in dividers:
        if x > left + max(160, int(width * 0.06)):
            right = x
            break
    return left, right


def detect_sidebar_right(bitmap: Bitmap, limit: int = 0) -> Optional[int]:
    """侧边栏右边界的横坐标。找不到时返回 None，由调用方兜底。"""

    for x in detect_panels(bitmap):
        if limit and x >= limit:
            break
        if x >= bitmap.width * 0.09:
            return x
    return None


def detect_search_field(bitmap: Bitmap, sidebar_right: int) -> Optional[Rect]:
    """识别左侧边栏顶部的搜索框。找不到时返回 None，由锚点兜底。"""

    width, height = bitmap.width, bitmap.height
    limit = max(80, min(width - 20, int(sidebar_right)))
    xs = _spread_between(10, limit - 10, 9, width)
    if len(xs) < 5:
        return None

    candidates = _find_panel(
        bitmap,
        xs,
        3,
        int(height * 0.26),
        min_height=24,
        max_height=64,
        min_share=0.45,
        min_delta=5,
        limit=8,
    )
    if not candidates:
        return None

    # 搜索框永远在侧边栏最上方，所以按纵向位置取最靠上的候选。
    # 按打分排序会选中下面的会话行——它们同样是"一块均匀面板"。
    candidates.sort(key=lambda item: item[0])

    seed = (xs[0] + xs[-1]) // 2
    for top, bottom, inside in candidates:
        left, right = _panel_extent(bitmap, top, bottom, inside, seed_x=seed)
        if right - left < int(limit * 0.45):
            continue
        if _edge_support(bitmap, top, left, right) < 0.55:
            continue
        if _edge_support(bitmap, bottom, left, right) < 0.55:
            continue
        return Rect(left, top, right + 1, bottom)
    return None


# ---------------------------------------------------------------------------
# 结果校验
# ---------------------------------------------------------------------------


def mean_difference(first: Bitmap, second: Bitmap) -> float:
    """两张同尺寸截图的平均亮度差，用于判断界面是否发生变化。"""

    width = min(first.width, second.width)
    height = min(first.height, second.height)
    if width < 8 or height < 8:
        return 0.0
    step_x = max(1, width // 40)
    step_y = max(1, height // 20)
    total = 0
    count = 0
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            total += abs(first.lum(x, y) - second.lum(x, y))
            count += 1
    return total / max(1, count)


def changed_ratio(first: Bitmap, second: Bitmap, delta: int = 8) -> float:
    """明显发生变化的采样点占比。

    短消息只占输入框很小一块，平均亮度差会被大片背景稀释到阈值以下，
    于是"粘贴成功"被误判成"没粘贴上"，触发重复粘贴。改成看变化点的占比，
    一小行文字也能被稳定识别出来，而光标闪烁只影响个别点，不会误报。
    """

    width = min(first.width, second.width)
    height = min(first.height, second.height)
    if width < 8 or height < 8:
        return 0.0
    step_x = max(1, width // 100)
    step_y = max(1, height // 50)
    changed = 0
    count = 0
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            count += 1
            if abs(first.lum(x, y) - second.lum(x, y)) >= delta:
                changed += 1
    return changed / max(1, count)


def looks_changed(first: Optional[Bitmap], second: Optional[Bitmap], threshold: float = 3.0) -> bool:
    if first is None or second is None:
        return False
    # 0.5%：一行文字在一整块消息区里大约占 1%，而光标闪烁不到 0.1%。
    if changed_ratio(first, second) >= 0.005:
        return True
    return mean_difference(first, second) >= threshold
