# -*- coding: utf-8 -*-
"""无界面自检。

运行 `python cuerelay.py --check` 可以验证识别算法、调度推进与数据迁移，
不需要打开窗口，也不需要目标应用正在运行。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Tuple

from . import apps, conversations, hotkey, ocr, templates, vision, winapi
from .config import now_local, parse_datetime
from .imaging import Bitmap
from .scheduler import Scheduler
from .store import (
    CLOSE_ACTION_LABELS,
    CLOSE_ACTIONS,
    HISTORY_LIMIT,
    TaskStore,
    advance_next_run_past_now,
    content_snippet,
    duplicate_groups,
    is_lifecycle,
    normalize_close_action,
    normalize_snippet,
    normalize_task,
)
from .ui.palette import Command, filter_commands
from .vision import Rect

Color = Tuple[int, int, int]


def make_bitmap(
    width: int,
    height: int,
    background: Color,
    panels: List[Tuple[Tuple[int, int, int, int], Color]],
) -> Bitmap:
    """生成测试用位图；矩形使用 [left, right) / [top, bottom) 半开区间。"""

    background_row = bytes((background[2], background[1], background[0], 255)) * width
    rows = []
    for y in range(height):
        row = bytearray(background_row)
        for (left, top, right, bottom), fill in panels:
            if top <= y < bottom:
                pixel = bytes((fill[2], fill[1], fill[0], 255))
                row[left * 4 : right * 4] = pixel * (right - left)
        rows.append(bytes(row))
    return Bitmap(width, height, b"".join(rows))


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


def test_modal_color() -> None:
    points = [(40, 40, 40)] * 8 + [(200, 200, 200)] * 2
    color, share = vision._modal(points)
    assert color == (40, 40, 40), color
    assert abs(share - 0.8) < 1e-6, share


def test_composer_detection() -> None:
    bitmap = make_bitmap(
        760,
        560,
        (30, 30, 30),
        [((150, 430, 611, 500), (46, 46, 46))],
    )
    rect = vision.detect_composer(bitmap)
    assert rect is not None, "没有识别到输入框"
    assert abs(rect.left - 150) <= 2, rect
    assert abs(rect.right - 611) <= 2, rect
    assert abs(rect.top - 430) <= 2, rect
    assert abs(rect.bottom - 500) <= 2, rect


def test_composer_with_text_lines() -> None:
    """输入框里有多行文字时仍然要识别成一块面板。"""

    panels = [((150, 430, 611, 500), (46, 46, 46))]
    for index in range(3):
        top = 444 + index * 14
        panels.append(((170, top, 520, top + 7), (210, 210, 210)))
    bitmap = make_bitmap(760, 560, (30, 30, 30), panels)
    rect = vision.detect_composer(bitmap)
    assert rect is not None, "有文字时没有识别到输入框"
    assert abs(rect.top - 430) <= 3, rect
    assert abs(rect.bottom - 500) <= 3, rect


def test_send_button_detection() -> None:
    bitmap = make_bitmap(
        760,
        560,
        (30, 30, 30),
        [
            ((150, 430, 611, 500), (46, 46, 46)),
            ((560, 456, 586, 482), (235, 235, 235)),
        ],
    )
    composer = vision.detect_composer(bitmap)
    assert composer is not None
    point = vision.detect_send_button(bitmap, composer)
    assert point is not None, "没有识别到发送按钮"
    assert abs(point[0] - 572) <= 3, point
    assert abs(point[1] - 468) <= 3, point


def test_send_slot_icon_state() -> None:
    """发送位置会换图标：有内容时是发送箭头，生成回答时是暂停，空的时候是麦克风。

    三种状态的外壳尺寸和填充率完全一样，只看外壳会把暂停键当成发送按钮点下去，
    结果是掐断正在生成的回答；空输入框则会把麦克风当成发送按钮，点下去开始录音。
    """

    accent = (59, 130, 246)
    glyph = (250, 250, 250)
    square = (560, 456, 586, 482)
    panel = ((150, 430, 611, 500), (46, 46, 46))

    def build(extra: list) -> Bitmap:
        return make_bitmap(760, 560, (30, 30, 30), [panel, (square, accent)] + extra)

    # 发送箭头：顶部人字形横条 + 中间竖杆，中间列有内容。
    arrow = build([((563, 462, 583, 466), glyph), ((571, 462, 576, 476), glyph)])
    composer = vision.detect_composer(arrow)
    assert composer is not None, "箭头用例的输入框没识别出来"
    slot = vision.detect_send_slot(arrow, composer)
    assert slot is not None, "没有识别到发送方块"
    assert vision.send_slot_kind(arrow, slot) == vision.SEND_SLOT_SEND

    # 暂停：两根竖条，中间必然留空。
    pause = build([((563, 463, 569, 476), glyph), ((577, 463, 583, 476), glyph)])
    composer = vision.detect_composer(pause)
    assert composer is not None, "暂停用例的输入框没识别出来"
    slot = vision.detect_send_slot(pause, composer)
    assert slot is not None, "没有识别到暂停方块"
    assert vision.send_slot_kind(pause, slot) == vision.SEND_SLOT_PAUSE

    # 竖条画得粗一点、缝窄一点，也必须认成暂停。
    tight = build([((564, 463, 572, 476), glyph), ((574, 463, 582, 476), glyph)])
    composer = vision.detect_composer(tight)
    assert composer is not None
    slot = vision.detect_send_slot(tight, composer)
    assert slot is not None
    assert vision.send_slot_kind(tight, slot) == vision.SEND_SLOT_PAUSE, "窄缝的暂停被漏判了"

    # 麦克风：细长条，填充率低，根本不该被当成可点的按钮。
    mic = make_bitmap(
        760,
        560,
        (30, 30, 30),
        [panel, ((570, 458, 578, 484), glyph)],
    )
    composer = vision.detect_composer(mic)
    assert composer is not None, "麦克风用例的输入框没识别出来"
    assert vision.detect_send_slot(mic, composer) is None, "麦克风不能被当成发送按钮"


def test_change_detection() -> None:
    empty = make_bitmap(320, 80, (40, 40, 40), [])
    filled = make_bitmap(320, 80, (40, 40, 40), [((20, 20, 300, 60), (220, 220, 220))])
    assert vision.mean_difference(empty, filled) > 10
    assert vision.looks_changed(empty, filled)
    assert not vision.looks_changed(empty, make_bitmap(320, 80, (40, 40, 40), []))


def test_sparse_change_detection() -> None:
    """一小行文字也必须算"变了"。

    输入框很大、消息很短时，平均亮度差会被背景稀释到阈值以下，
    于是粘贴被误判成失败并重复粘贴，最终发出去的是两遍内容。
    """

    empty = make_bitmap(740, 100, (42, 44, 48), [])
    panels = [((18, 18, 300, 34), (230, 230, 230))]
    one_line = make_bitmap(740, 100, (42, 44, 48), panels)
    assert vision.changed_ratio(empty, one_line) > 0.02, vision.changed_ratio(empty, one_line)
    assert vision.looks_changed(empty, one_line), vision.mean_difference(empty, one_line)

    # 光标闪烁只影响个别采样点，不能当成"内容变化"。
    caret = make_bitmap(740, 100, (42, 44, 48), [((18, 18, 20, 34), (230, 230, 230))])
    assert not vision.looks_changed(empty, caret), vision.changed_ratio(empty, caret)

    # 大区域里只有一行字：用来判断"对话到底切过去了没有"。
    big_empty = make_bitmap(1100, 640, (30, 31, 34), [])
    big_line = make_bitmap(1100, 640, (30, 31, 34), [((350, 90, 650, 108), (226, 226, 230))])
    assert vision.looks_changed(big_empty, big_line), vision.changed_ratio(big_empty, big_line)
    assert not vision.looks_changed(big_empty, big_empty)


def test_full_window_layout() -> None:
    """整窗识别：输入框、发送按钮、搜索框都要落在正确位置。

    这里特意在输入框上方放了几段消息卡片。它们之间的空白同样均匀、
    上下也有边缘，曾经被误判成"输入框"，所以这条用例用来防止回归。
    """

    panels: List[Tuple[Tuple[int, int, int, int], Color]] = [
        ((0, 0, 250, 720), (26, 26, 26)),  # 侧边栏
        ((24, 60, 226, 96), (46, 46, 46)),  # 搜索框
        ((280, 600, 1060, 690), (47, 47, 47)),  # 输入框
        ((1000, 635, 1045, 675), (255, 255, 255)),  # 发送按钮
    ]
    for index in range(3):
        top = 140 + index * 130
        panels.append(((300, top, 780, top + 40), (42, 42, 42)))
        panels.append(((300, top + 48, 700, top + 88), (38, 38, 38)))
    bitmap = make_bitmap(1100, 720, (33, 33, 33), panels)

    composer = vision.detect_composer(bitmap)
    assert composer is not None, "整窗识别没有找到输入框"
    assert abs(composer.top - 600) <= 2, composer
    assert abs(composer.bottom - 690) <= 2, composer
    assert abs(composer.left - 280) <= 2, composer
    assert abs(composer.right - 1060) <= 2, composer

    point = vision.detect_send_button(bitmap, composer)
    assert point is not None, "整窗识别没有找到发送按钮"
    assert abs(point[0] - 1022) <= 3, point
    assert abs(point[1] - 655) <= 3, point

    search = vision.detect_search_field(bitmap, composer.left)
    assert search is not None, "整窗识别没有找到搜索框"
    assert abs(search.top - 60) <= 2, search
    assert abs(search.bottom - 96) <= 2, search


def test_panel_layout() -> None:
    """三栏布局下，消息区边界和输入框都必须落在正确位置。

    右侧展开代码面板时，输入框不再居中；如果还按整窗宽度取样，
    采样列会有一半落到面板上，输入框就识别不出来。
    """

    panels: List[Tuple[Tuple[int, int, int, int], Color]] = [
        ((0, 0, 40, 600), (26, 26, 26)),  # 图标栏
        ((40, 0, 240, 600), (28, 28, 28)),  # 侧边栏
        ((240, 0, 242, 600), (52, 52, 54)),  # 侧边栏分隔线
        ((900, 0, 902, 600), (52, 52, 54)),  # 消息区 / 代码面板分隔线
        ((300, 470, 800, 560), (48, 48, 48)),  # 输入框（偏左，不居中）
        ((760, 500, 792, 532), (60, 90, 220)),  # 发送按钮
    ]
    bitmap = make_bitmap(1000, 600, (24, 24, 24), panels)

    dividers = vision.detect_panels(bitmap)
    assert 240 in dividers and 900 in dividers, dividers

    left, right = vision.detect_chat_bounds(bitmap)
    assert left == 240, (left, right)
    assert right == 900, (left, right)
    assert vision.detect_sidebar_right(bitmap) == 240

    composer = vision.detect_composer(bitmap)
    assert composer is not None, "三栏布局下没有识别到输入框"
    assert abs(composer.left - 300) <= 2, composer
    assert abs(composer.right - 800) <= 2, composer

    point = vision.detect_send_button(bitmap, composer)
    assert point is not None, "三栏布局下没有识别到发送按钮"
    assert abs(point[0] - 776) <= 3, point


def test_send_button_ignores_thin_icon() -> None:
    """麦克风这种细线条图标不能被当成发送按钮。

    真机上曾经出现过：识别出的输入框比实际矮一点，发送按钮下半截被 ROI 切掉，
    于是退而选中了左边的麦克风，一点就开始录音。
    """

    panels: List[Tuple[Tuple[int, int, int, int], Color]] = [
        ((150, 430, 611, 500), (46, 46, 46)),
        ((520, 456, 528, 482), (200, 200, 200)),  # 麦克风：窄条
        ((560, 456, 586, 482), (60, 90, 220)),  # 发送：实心圆
    ]
    bitmap = make_bitmap(760, 560, (30, 30, 30), panels)
    composer = vision.Rect(150, 430, 611, 500)
    point = vision.detect_send_button(bitmap, composer)
    assert point is not None, "没有识别到发送按钮"
    assert abs(point[0] - 572) <= 3, point

    # 输入框识别得偏矮时，仍然要选中发送按钮，而不是麦克风
    short = vision.Rect(150, 430, 611, 490)
    point = vision.detect_send_button(bitmap, short)
    assert point is not None, "输入框偏矮时没有识别到发送按钮"
    assert abs(point[0] - 572) <= 3, point


def test_panel_around_seed() -> None:
    """从一个已知属于输入框的点出发，能扩出整个输入框。"""

    bitmap = make_bitmap(
        800,
        600,
        (24, 24, 24),
        [((200, 470, 700, 560), (48, 48, 48))],
    )
    rect = vision.detect_panel_around(bitmap, 220, 500)
    assert rect is not None, "没有从种子点扩出面板"
    assert abs(rect.left - 200) <= 2, rect
    assert abs(rect.right - 700) <= 2, rect
    assert abs(rect.top - 470) <= 2, rect
    assert abs(rect.bottom - 560) <= 2, rect


def test_title_matching_is_strict() -> None:
    """对话标题只能"完全一致"或"一方包含另一方"。

    两个只差几个数字的标题字符相似度能到 0.9，用编辑距离会把消息发错对话。
    """

    assert ocr.title_similarity("Clarify say", "Clarify say") == 1.0
    assert ocr.title_similarity("B 20261103 31 Big LiquidGlass", "20261103 31 Big LiquidGlass") > 0.9
    assert ocr.title_similarity("Helloalp Read 20261012 1", "Helloalp Read 20261010 1") == 0.0
    assert ocr.title_similarity("Helloalp Website 20261023 1", "Helloalp Resume 20261105 1") == 0.0

    lines = [
        ocr.OcrLine("Helloalp Read 20261010 1", 10, 10, 200, 20),
        ocr.OcrLine("Helloalp Read 20261012 1", 10, 40, 200, 20),
    ]
    found = ocr.find_line(lines, "Helloalp Read 20261012 1", 0.9, strict=True)
    assert found is not None and found.text == "Helloalp Read 20261012 1", found
    assert ocr.find_line(lines, "Helloalp Read 20261011 1", 0.9, strict=True) is None


def test_placeholder_matching_is_lenient() -> None:
    """占位符允许 OCR 出错：with 常被读成 vvith。"""

    assert ocr.similarity("work vvith Codex", "Work with Codex") > 0.62
    assert ocr.find_text([ocr.OcrLine("work vvith Codex", 10, 10, 150, 20)], "Work with Codex", 0.62)
    # 但"Reset usage"这种短词不能算成 "Message"
    assert ocr.similarity("Reset usage", "Message") < 0.78


def test_rect_scaling() -> None:
    rect = Rect(100, 200, 500, 260)
    scaled = rect.scale_to((1000, 800), (2000, 800))
    assert scaled.left == 200 and scaled.right == 1000, scaled
    assert scaled.top == 200 and scaled.bottom == 260, scaled


def test_schedule_advance() -> None:
    now = _dt.datetime(2026, 11, 6, 22, 0, 0)
    daily = {"schedule": "daily", "next_run": "2026-11-06T21:00:00"}
    result = advance_next_run_past_now(daily, now)
    assert result == _dt.datetime(2026, 11, 7, 21, 0, 0), result

    interval = {"schedule": "interval", "next_run": "2026-11-06T21:00:00", "interval_minutes": 30}
    result = advance_next_run_past_now(interval, now)
    assert result == _dt.datetime(2026, 11, 6, 22, 30, 0), result

    once = {"schedule": "once", "next_run": "2026-11-06T21:00:00"}
    assert advance_next_run_past_now(once, now) is None


def test_store_migration() -> None:
    legacy = {
        "version": 1,
        "settings": {
            "always_on_top": False,
            "restore_target_geometry": True,
            "step_delay_seconds": 1.5,
            "input_anchor": {"rx": 0.5, "ry": 0.9, "screen_x": 800, "screen_y": 900},
            "send_anchor": {"rx": 0.9, "ry": 0.9, "edge_mode": "bottom-right", "right_offset": 40, "bottom_offset": 40},
            "conversation_search_anchor": None,
            "target_geometry": {"left": 0, "top": 0, "width": 1600, "height": 1000, "maximized": False},
            "target_title": "Codex",
            "target_process": "Codex.exe",
        },
        "tasks": [
            {
                "id": "abc123",
                "name": "睡前提示词",
                "content": "hello",
                "conversation_title": "测试对话",
                "schedule": "daily",
                "next_run": "2026-11-06T23:00:00",
                "interval_minutes": 60,
                "action": "button",
                "enabled": True,
                "status": "等待中",
                "last_run": None,
                "last_error": "",
            }
        ],
    }
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cuerelay_data.json"
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        store = TaskStore(path)
        settings = store.settings()
        assert settings["always_on_top"] is False
        assert settings["anchor_composer"]["space"] == "window", settings["anchor_composer"]
        assert settings["anchor_send"]["space"] == "window"
        assert settings["target_title"] == "Codex"

        tasks = store.tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task["action"] == "click", task["action"]
        assert task["status"] in ("waiting", "expired"), task["status"]
        assert task["name"] == "睡前提示词"

        reloaded = TaskStore(path)
        assert len(reloaded.tasks()) == 1
        assert reloaded.settings()["anchor_composer"]["ry"] == 0.9


def test_time_parsing() -> None:
    assert parse_datetime("2026-11-06T23:00:00") == _dt.datetime(2026, 11, 6, 23, 0, 0)
    assert parse_datetime(None) is None
    assert parse_datetime("不是时间") is None


def test_duplicate_groups() -> None:
    """同名同内容的重复任务要能合并，且保留最近修改的一条。"""

    tasks = [
        {"id": "a", "content": "继续", "name": "睡前", "updated_at": "2026-11-01T10:00:00"},
        {"id": "b", "content": " 继续 ", "name": "睡前", "updated_at": "2026-11-05T10:00:00"},
        {"id": "c", "content": "继续", "name": "睡前", "updated_at": "2026-11-03T10:00:00"},
        {"id": "d", "content": "say hi", "name": "睡前", "updated_at": "2026-11-02T10:00:00"},
        {"id": "e", "content": "say  hi", "name": "其它", "updated_at": "2026-11-04T10:00:00"},
        {"id": "f", "content": "独一无二", "name": "单独", "updated_at": "2026-11-04T10:00:00"},
        {"id": "g", "content": "", "name": "空", "updated_at": "2026-11-04T10:00:00"},
    ]
    groups = duplicate_groups(tasks)
    assert len(groups) == 2, [group["content"] for group in groups]

    first = groups[0]
    assert first["content"] == "继续"
    assert first["keep"]["id"] == "b", first["keep"]
    assert sorted(task["id"] for task in first["extra"]) == ["a", "c"]

    second = groups[1]
    assert second["content"] == "say hi"
    assert second["keep"]["id"] == "e"
    assert [task["id"] for task in second["extra"]] == ["d"]

    # 空内容不参与判重，避免把一堆空任务合成一组。
    assert all(group["content"] for group in groups)


def test_row_meta() -> None:
    """列表行右侧的短标签：失败的行不能只是重复一遍「失败」。"""

    from .ui.app import App

    meta = App._row_meta
    now = now_local()
    stamp = (now - _dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

    # 有执行记录的失败任务显示失败时间。
    failed_with_time = meta({"status": "failed", "last_run": stamp, "enabled": False})
    assert failed_with_time and "失败" not in failed_with_time, failed_with_time

    # 没有执行记录的失败任务留空，分组名和红点已经说明问题。
    assert meta({"status": "failed", "last_run": None, "enabled": False}) == ""

    # 等待中的任务照旧显示计划时间。
    waiting = meta({"status": "waiting", "enabled": True, "next_run": stamp, "schedule": "once"})
    assert waiting and "失败" not in waiting, waiting


def test_delete_many() -> None:
    with tempfile.TemporaryDirectory() as folder:
        store = TaskStore(Path(folder) / "cuerelay_data.json")
        for index in range(4):
            store.add({"name": f"任务{index}", "content": f"内容{index}"})
        ids = [str(task["id"]) for task in store.tasks()]
        assert store.delete_many([ids[0], ids[2], "不存在的id"]) == 2
        assert len(store.tasks()) == 2
        assert store.delete_many([]) == 0
        assert len(TaskStore(Path(folder) / "cuerelay_data.json").tasks()) == 2


def test_png_preview() -> None:
    bitmap = make_bitmap(40, 20, (10, 20, 30), [])
    data, width, height = bitmap.to_png(100)
    assert width == 40 and height == 20
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), data[:8]
    assert data[12:16] == b"IHDR"
    stored_width = int.from_bytes(data[16:20], "big")
    stored_height = int.from_bytes(data[20:24], "big")
    assert (stored_width, stored_height) == (40, 20), (stored_width, stored_height)
    assert data.endswith(b"IEND\xaeB`\x82"), data[-8:]

    # 超过尺寸上限时按整数倍缩小，保证预览不会撑爆对话框。
    big = make_bitmap(900, 700, (10, 20, 30), [])
    small, small_width, small_height = big.to_png(400, 300)
    assert small_width == 300 and small_height == 233, (small_width, small_height)
    assert small.startswith(b"\x89PNG\r\n\x1a\n")


class _RecordingOverlay:
    """只记录调用，不碰 Tk，这样自检不需要显示器。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def show(self, rect: dict) -> None:
        self.calls.append(("show", rect))

    def hide(self) -> None:
        self.calls.append(("hide",))

    def box(self, rect: dict, kind: str, label: str) -> None:
        self.calls.append(("box", rect, kind, label))

    def clear_boxes(self) -> None:
        self.calls.append(("clear",))

    def progress(self, index: int, total: int, text: str, tone: str) -> None:
        self.calls.append(("progress", index, total, text, tone))


def test_overlay_events() -> None:
    """浮层事件翻译必须容错：线程发来的脏数据不能把执行带崩。"""

    from .ui.overlay import apply_event

    sink = _RecordingOverlay()
    rect = {"left": 10, "top": 20, "width": 300, "height": 200}
    apply_event(sink, {"action": "show", "rect": rect})
    apply_event(sink, {"action": "progress", "index": "2", "total": "5", "text": "校验粘贴", "tone": "success"})
    apply_event(
        sink,
        {"action": "box", "rect": {"left": 1, "top": 2, "right": 3, "bottom": 4}, "kind": "composer", "label": "输入框"},
    )
    apply_event(sink, {"action": "clear"})
    apply_event(sink, {"action": "hide"})

    # 脏载荷：None、缺字段、未知动作，都应该被安静地忽略。
    apply_event(sink, None)
    apply_event(sink, "not a dict")
    apply_event(sink, {"action": "progress", "index": None, "total": None})
    apply_event(sink, {"action": "不存在的动作"})

    assert sink.calls[0] == ("show", rect), sink.calls[0]
    assert sink.calls[1] == ("progress", 2, 5, "校验粘贴", "success"), sink.calls[1]
    assert sink.calls[2][0] == "box" and sink.calls[2][2] == "composer", sink.calls[2]
    assert sink.calls[3] == ("clear",), sink.calls[3]
    assert sink.calls[4] == ("hide",), sink.calls[4]
    assert sink.calls[5] == ("progress", 0, 1, "", "accent"), sink.calls[5]
    assert len(sink.calls) == 6, sink.calls


def test_settings_bool_coercion() -> None:
    """手改过的配置文件里 "false" 不能被当成 True。"""

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cuerelay_data.json"
        path.write_text(
            json.dumps(
                {"version": 2, "settings": {"show_overlay": "false", "verify_send": 0}, "tasks": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        settings = TaskStore(path).settings()
        assert settings["show_overlay"] is False, settings["show_overlay"]
        assert settings["verify_send"] is False, settings["verify_send"]
        assert settings["verify_paste"] is True, settings["verify_paste"]
        assert settings["strict_conversation"] is True


def test_msix_launch_target() -> None:
    """Codex 是 MSIX 包，启动要走 shell:AppsFolder，包族名从安装路径反推。

    安装目录名是 `<Name>_<Version>_<Arch>__<PublisherId>`，包族名是
    `<Name>_<PublisherId>`。这里不能按 `_` 简单切：Name 里有点，版本段自己带下划线。
    """

    path = (
        r"C:\Program Files\WindowsApps"
        r"\OpenAI.Codex_26.1029.7803.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
    )
    assert apps.family_from_install_path(path) == "OpenAI.Codex_2p2nqsd0c76g0"
    assert apps.aumid_from_install_path(path) == "OpenAI.Codex_2p2nqsd0c76g0!App"
    # 自定义 AppId 要跟着走，否则会启动到包的默认入口。
    assert apps.aumid_from_install_path(path, "Codex") == "OpenAI.Codex_2p2nqsd0c76g0!Codex"
    # 不是 MSIX 布局的路径不能瞎猜：猜出来的 AUMID 指向别的应用，比启动失败更糟。
    assert apps.family_from_install_path(r"C:\Program Files\v2rayN\v2rayN.exe") == ""
    assert apps.family_from_install_path("") == ""
    assert apps.family_from_install_path(r"D:\tools\some_app_1.0\app.exe") == ""
    # 只有分隔符没有发布者的残缺目录名也要放过。
    assert apps.family_from_install_path(r"D:\x\Foo_1.0__\a.exe") == ""


def test_task_kind_coercion() -> None:
    """类型可能来自手改过的 JSON，脏值必须落回「发送提示词」。

    猜错方向很危险：把一条普通提示词任务当成「关闭应用」，执行时会把目标应用关掉。
    """

    assert normalize_task({"kind": "launch"})["kind"] == "launch"
    assert normalize_task({"kind": " 关闭 "})["kind"] == "close"
    assert normalize_task({"kind": "Start"})["kind"] == "launch"
    assert normalize_task({"kind": "乱写"})["kind"] == "prompt"
    assert normalize_task({})["kind"] == "prompt"

    launch = {"kind": "launch", "content": ""}
    assert is_lifecycle(launch) is True
    assert is_lifecycle({"kind": "prompt", "content": "hi"}) is False
    # 开关类任务没有提示词，列表那一行要写类型名，否则每条都是「（空内容）」。
    assert content_snippet(launch) == "启动应用"
    assert content_snippet({"kind": "close", "content": ""}) == "关闭应用"
    assert content_snippet({"kind": "prompt", "content": "   "}) == "（空内容）"
    assert content_snippet({"kind": "prompt", "content": "写" * 60}).endswith("…")


def test_conversation_parse() -> None:
    """侧边栏那一堆文字里，只有对话标题该留下。

    这里用的是真实窗口读出来的行（菜单栏、应用名、New chat、分区标题、项目名、
    缩进的对话、Show more），不是编出来的理想数据。
    """

    rows = [
        ocr.OcrLine("File", 185, 20, 30, 16),
        ocr.OcrLine("Edit", 252, 20, 34, 16),
        ocr.OcrLine("View", 321, 20, 44, 16),
        ocr.OcrLine("Help", 401, 20, 42, 21),
        ocr.OcrLine("Codex", 103, 95, 78, 20),
        ocr.OcrLine("C New chat", 105, 158, 120, 18),
        ocr.OcrLine("Pinned", 103, 222, 64, 16),
        ocr.OcrLine("Clarify say", 102, 274, 94, 21),
        ocr.OcrLine("Projects", 103, 342, 74, 21),
        ocr.OcrLine("B helloalp_sms", 104, 392, 152, 22),
        ocr.OcrLine("20261103 31 Big LiquidGlass", 138, 487, 265, 21),
        ocr.OcrLine("20261103 30", 138, 581, 119, 15),
        ocr.OcrLine("Show more", 139, 718, 104, 16),
    ]
    parsed = conversations.parse(rows)
    titles = conversations.titles(parsed)
    assert titles == [
        "Clarify say",
        "helloalp_sms",
        "20261103 31 Big LiquidGlass",
        "20261103 30",
    ], titles
    assert parsed[0].section == "pinned"
    assert parsed[1].section == "projects"


def test_conversation_chrome_filter() -> None:
    """固定文案不能被当成对话；正常标题不能因为长得像就被删。"""

    assert conversations.is_chrome("New chat")
    assert conversations.is_chrome("C New chat")
    assert conversations.is_chrome("Show more")
    assert not conversations.is_chrome("New chat 的整理结果")
    # 只是包含菜单词、并不等于菜单词的标题要留下。
    rows = [ocr.OcrLine("View the logs", 140, 420, 120, 18)]
    assert [item.title for item in conversations.parse(rows)] == ["View the logs"]

    assert conversations.strip_icon("B helloalp_sms") == ("helloalp_sms", True)
    assert conversations.strip_icon("Clarify say") == ("Clarify say", False)
    # 单字母开头的正常标题也会被剥掉图标，这条规则只影响显示，不影响跳转。
    assert conversations.strip_icon("A B") == ("A B", False)


def test_snippet_store() -> None:
    """片段的名字可以从内容推出来，但内容不能被改。"""

    with tempfile.TemporaryDirectory() as folder:
        store = TaskStore(Path(folder) / "cuerelay_data.json")
        item = store.add_snippet({"content": "  把这段总结成三点  "})
        assert item["name"] == "把这段总结成三点"
        assert item["content"] == "  把这段总结成三点  ", "内容不能顺手 trim，缩进可能是格式的一部分"

        store.touch_snippet(item["id"])
        store.touch_snippet(item["id"])
        assert store.get_snippet(item["id"])["use_count"] == 2

        assert store.replace_snippet(item["id"], {"name": "改名", "content": "新内容"})
        assert store.get_snippet(item["id"])["name"] == "改名"
        assert store.delete_snippet(item["id"])
        assert store.snippets() == []
        assert not store.delete_snippet("不存在")

        long_item = store.add_snippet({"content": "x" * 80})
        assert long_item["name"].endswith("…") and len(long_item["name"]) <= 25


def test_history_cap_and_finish_run() -> None:
    """执行记录要封顶，而且 finish_run 必须同时更新任务和记录。"""

    with tempfile.TemporaryDirectory() as folder:
        store = TaskStore(Path(folder) / "cuerelay_data.json")
        task = store.add({"name": "记录测试", "content": "hi"})
        store.finish_run(task, False, "失败原因", 1500, attempt=1)

        history = store.history()
        assert len(history) == 1
        assert history[0]["ok"] is False
        assert history[0]["attempt"] == 1
        assert history[0]["task_name"] == "记录测试"
        updated = store.get(task["id"])
        assert updated["status"] == "failed"
        assert updated["last_error"] == "失败原因"

        for index in range(HISTORY_LIMIT + 20):
            store.add_history({"task_id": "x", "task_name": f"第 {index} 条", "ok": True})
        items = store.history()
        assert len(items) == HISTORY_LIMIT, len(items)
        # 新的在前：最后写进去的那条应该是第一条。
        assert items[0]["task_name"] == f"第 {HISTORY_LIMIT + 19} 条"


def test_retry_bounds() -> None:
    """重试次数与间隔来自输入框，必须夹到安全区间。"""

    assert normalize_task({"retry_limit": 99})["retry_limit"] == 5
    assert normalize_task({"retry_limit": -3})["retry_limit"] == 0
    assert normalize_task({"retry_limit": "2"})["retry_limit"] == 2
    assert normalize_task({})["retry_limit"] == 0
    assert normalize_task({"retry_delay": 1})["retry_delay"] == 5
    assert normalize_task({"retry_delay": 99999})["retry_delay"] == 600


def test_bool_settings_are_real_bools() -> None:
    """手改配置留下的字符串 "false" 不能把开关打开。"""

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cuerelay_data.json"
        path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "settings": {"auto_launch_target": "false", "launch_v2ray_first": "0"},
                    "tasks": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        settings = TaskStore(path).settings()
        assert settings["auto_launch_target"] is False
        assert settings["launch_v2ray_first"] is False
        assert settings["show_overlay"] is True


def test_palette_filter() -> None:
    """命令面板：空输入全给，多个词要同时命中，前缀命中的排前面。"""

    items = [
        Command(label="打开：任务", group="视图"),
        Command(label="新建任务", group="任务"),
        Command(label="切换对话：Clarify say", group="对话", keywords="clarify"),
    ]
    assert len(filter_commands(items, "")) == 3
    assert [item.label for item in filter_commands(items, "新建")] == ["新建任务"]
    assert [item.label for item in filter_commands(items, "视图 任务")] == ["打开：任务"]
    # 关键词也要能搜到
    assert [item.label for item in filter_commands(items, "clarify")] == ["切换对话：Clarify say"]
    assert filter_commands(items, "不存在的东西") == []

    # 前缀命中排前面：打「任务」时，「任务」视图应该压过「执行：任务A」。
    ranked = filter_commands([Command(label="执行：任务A"), Command(label="任务")], "任务")
    assert ranked[0].label == "任务"


def test_autostart_command() -> None:
    """开机启动的命令行要能被 Run 项直接执行。

    路径里带空格是常态（Program Files 下面），所以两个参数都要带引号；
    用 python.exe 会在每次开机时弹一个黑色控制台。
    """

    command = apps.autostart_command()
    assert command.count('"') == 4, command
    assert "cuerelay.py" in command
    assert "pythonw" in command.lower(), command


def test_tidy_path() -> None:
    """用户粘贴的路径常常带引号或前后空格。"""

    assert apps.tidy_path('  "C:/tools/v2rayN.exe"  ') == "C:/tools/v2rayN.exe"
    assert apps.tidy_path("  'D:/x.exe' ") == "D:/x.exe"
    assert apps.tidy_path("") == ""


def test_template_expand() -> None:
    """提示词里的变量在最后一刻展开。

    定时任务里的 {{date}} 要的是"执行那天"，所以这里不能提前替换；认不出的
    占位符必须原样留下——写错一个词不该让整条提示词变成空白。
    """

    moment = _dt.datetime(2026, 11, 7, 9, 5)  # 周六
    text = "总结 {{date}} 的日志，现在是 {{ time }}，{{weekday}}。"
    out = templates.expand(text, now=moment)
    assert out == "总结 2026-11-07 的日志，现在是 09:05，周六。", out
    assert templates.expand("{{datetime}}", now=moment) == "2026-11-07 09:05"
    # 占位符大小写不敏感，内部空格容忍
    assert templates.expand("{{ DATE }}", now=moment) == "2026-11-07"
    # 剪贴板内容原样嵌入
    assert templates.expand("看看：{{clipboard}}", now=moment, clipboard="abc") == "看看：abc"
    # 认不出的原样保留
    assert templates.expand("{{unknown}}", now=moment) == "{{unknown}}"
    # 没有占位符时不做任何改动（包括不该被当成模板的普通花括号）
    assert templates.expand("普通的 {花括号}", now=moment) == "普通的 {花括号}"


def test_scheduler_pause() -> None:
    """暂停拦的是"按时间自己触发"，不是所有执行。

    用一个假 store 造一个已经到点的任务：暂停时 _scan 不该碰它，恢复后
    应该正好触发一次。
    """

    due = now_local() - _dt.timedelta(seconds=5)

    class FakeStore:
        def index(self):
            return [("t1", due, True)]

        def setting(self, key, default=None):
            return default

    scheduler = Scheduler(FakeStore(), lambda _event, _payload: None)
    calls: list = []
    scheduler.trigger = lambda *args, **kwargs: calls.append(args) or True  # type: ignore[assignment]

    scheduler.set_paused(True)
    assert scheduler.is_paused() is True
    scheduler._scan()
    assert calls == [], "暂停时不该触发任务"

    scheduler.set_paused(False)
    scheduler._scan()
    assert len(calls) == 1, f"恢复后应该触发一次，实际 {len(calls)} 次"


def test_close_action() -> None:
    """关闭动作只认四个值。

    这个字段来自手改的配置文件，写错一个词不该让程序起不来——认不出就按
    默认（收进托盘）处理。
    """

    assert normalize_close_action("tray") == "tray"
    assert normalize_close_action("QUIT") == "quit"
    assert normalize_close_action("  minimize ") == "minimize"
    assert normalize_close_action("ask") == "ask"
    assert normalize_close_action("关掉") == "tray"
    assert normalize_close_action(None) == "tray"
    assert set(CLOSE_ACTION_LABELS) == set(CLOSE_ACTIONS)


def test_hotkey_register() -> None:
    """真的注册一次全局热键再注销。

    这条测的不是"能不能按到"，而是线程能不能起来、注册结果能不能同步回来、
    能不能干净退出。用 Ctrl+Alt+Shift+F12：没人会占这个组合。万一真被占了
    （或者不是 Windows），要给出原因而不是假装成功。
    """

    fired = threading.Event()
    key = hotkey.GlobalHotkey(
        fired.set,
        mods=hotkey.MOD_CONTROL | hotkey.MOD_ALT | hotkey.MOD_SHIFT | hotkey.MOD_NOREPEAT,
        vk=0x7B,  # VK_F12
    )
    ok, reason = key.start()
    if not ok:
        assert reason, "注册失败却没给原因"
        assert key.active is False
        return
    assert key.active is True
    # 重复 start 不该再注册一次，也不该报错。
    again, _ = key.start()
    assert again is True
    key.stop()
    assert key.active is False
    # 没起来过的实例也要能安全 stop。
    hotkey.GlobalHotkey(fired.set).stop()


def test_topmost_scan() -> None:
    """置顶排查的两条底线：坏句柄不能炸，自己不能被报成嫌疑人。

    "造一个置顶窗口再修掉"的完整验证在 tools/probe_topmost.py——那要开窗口，
    自检保持无窗口，只验纯逻辑和真实环境下的扫描结果。
    """

    # 空句柄、野句柄：一律当成"没问题"，不能抛异常。
    assert winapi.is_topmost(0) is False
    assert winapi.is_tool_window(0) is False
    assert winapi.window_class(0) == ""
    assert winapi.window_pid(0) == 0
    assert winapi.repair_window(0) == {
        "topmost": False,
        "restored": False,
        "repainted": False,
    }
    assert winapi.repair_window(0xDEAD)["topmost"] is False

    # 外壳的置顶窗口本来就该在最上层，名单里不能出现它们。
    assert "Shell_TrayWnd" in winapi.SHELL_CLASSES
    assert "Progman" in winapi.SHELL_CLASSES

    found = winapi.topmost_windows(exclude_pid=os.getpid())
    assert isinstance(found, list)
    for item in found:
        assert item["hwnd"], "扫出来的条目没有句柄"
        assert item["class_name"] not in winapi.SHELL_CLASSES, "外壳窗口不该进名单"
        assert item["pid"] != os.getpid(), "不能把自己报成嫌疑人"
        assert winapi.is_topmost(item["hwnd"]), "名单里的窗口应该确实处于置顶"
    areas = [
        (item["rect"] or {}).get("width", 0) * (item["rect"] or {}).get("height", 0)
        for item in found
    ]
    assert areas == sorted(areas, reverse=True), "名单应按覆盖面积从大到小"

    foreground = winapi.foreground_window()
    if foreground:
        assert winapi.window_pid(foreground) > 0
        assert winapi.window_class(foreground), "前台窗口应该有类名"


def test_panels_near_right_edge() -> None:
    """分隔线贴到窗口右边界时不能越界。

    detect_panels 里要比较 `row[x + 2]`，如果整行只取到 width - 2，
    最右边那几个 x 就会 IndexError。全黑画面时 `and` 短路把它掩盖住了，
    一旦右边缘附近真有一条亮线就会炸——这正是窗口拉宽后识别崩掉的原因。
    """

    width, height = 400, 300
    data = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            value = 255 if x in (380, 381) else 20
            index = (y * width + x) * 4
            data[index] = data[index + 1] = data[index + 2] = value
            data[index + 3] = 255
    bitmap = Bitmap(width, height, bytes(data))
    assert vision.detect_panels(bitmap) == [380]
    assert vision.detect_sidebar_right(bitmap) == 380


TESTS: List[Tuple[str, Callable[[], None]]] = [
    ("颜色众数", test_modal_color),
    ("输入框识别", test_composer_detection),
    ("输入框识别（含文字）", test_composer_with_text_lines),
    ("发送按钮识别", test_send_button_detection),
    ("发送按钮避开麦克风", test_send_button_ignores_thin_icon),
    ("发送位置图标状态", test_send_slot_icon_state),
    ("整窗识别", test_full_window_layout),
    ("三栏布局", test_panel_layout),
    ("面板扩展", test_panel_around_seed),
    ("标题严格匹配", test_title_matching_is_strict),
    ("占位符宽松匹配", test_placeholder_matching_is_lenient),
    ("界面变化检测", test_change_detection),
    ("短消息变化检测", test_sparse_change_detection),
    ("矩形缩放", test_rect_scaling),
    ("调度推进", test_schedule_advance),
    ("旧数据迁移", test_store_migration),
    ("时间解析", test_time_parsing),
    ("重复任务归组", test_duplicate_groups),
    ("列表行标签", test_row_meta),
    ("批量删除", test_delete_many),
    ("预览图生成", test_png_preview),
    ("浮层事件翻译", test_overlay_events),
    ("设置布尔纠正", test_settings_bool_coercion),
    ("MSIX 启动目标", test_msix_launch_target),
    ("任务类型纠正", test_task_kind_coercion),
    ("侧边栏对话解析", test_conversation_parse),
    ("侧边栏固定文案", test_conversation_chrome_filter),
    ("片段存储", test_snippet_store),
    ("执行记录", test_history_cap_and_finish_run),
    ("重试参数", test_retry_bounds),
    ("布尔设置纠正", test_bool_settings_are_real_bools),
    ("命令面板过滤", test_palette_filter),
    ("右边界分隔线", test_panels_near_right_edge),
    ("开机自启命令", test_autostart_command),
    ("全局热键注册", test_hotkey_register),
    ("关闭动作", test_close_action),
    ("调度暂停", test_scheduler_pause),
    ("提示词变量", test_template_expand),
    ("置顶窗口排查", test_topmost_scan),
    ("路径清理", test_tidy_path),
]


def run(verbose: bool = True) -> int:
    failures = 0
    for name, test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            if verbose:
                print(f"[失败] {name}: {exc}")
        except Exception as exc:  # pragma: no cover - 只在自检时使用
            failures += 1
            if verbose:
                print(f"[异常] {name}: {exc.__class__.__name__}: {exc}")
        else:
            if verbose:
                print(f"[通过] {name}")
    if verbose:
        total = len(TESTS)
        print("")
        print(f"共 {total} 项，失败 {failures} 项。")
    return failures
