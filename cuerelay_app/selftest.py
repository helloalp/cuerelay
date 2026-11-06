# -*- coding: utf-8 -*-
"""无界面自检。

运行 `python cuerelay.py --check` 可以验证识别算法、调度推进与数据迁移，
不需要打开窗口，也不需要目标应用正在运行。
"""

from __future__ import annotations

import datetime as _dt
import json
import tempfile
from pathlib import Path
from typing import Callable, List, Tuple

from . import ocr, vision
from .config import parse_datetime
from .imaging import Bitmap
from .store import TaskStore, advance_next_run_past_now, duplicate_groups
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


TESTS: List[Tuple[str, Callable[[], None]]] = [
    ("颜色众数", test_modal_color),
    ("输入框识别", test_composer_detection),
    ("输入框识别（含文字）", test_composer_with_text_lines),
    ("发送按钮识别", test_send_button_detection),
    ("发送按钮避开麦克风", test_send_button_ignores_thin_icon),
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
    ("批量删除", test_delete_many),
    ("预览图生成", test_png_preview),
    ("浮层事件翻译", test_overlay_events),
    ("设置布尔纠正", test_settings_bool_coercion),
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
