# -*- coding: utf-8 -*-
"""执行流水线。

与 1.x 的关键区别：不再"点一下再睡一会儿然后祈祷成功"，而是每一步都验证。
粘贴后确认输入框像素真的变了，发送后确认输入框真的清空了；任一步没有
发生变化就重试一次，仍然失败则明确报错，而不是静默失败。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import apps, conversations, imaging, locator, ocr, templates, vision, winapi
from .vision import Rect


class AutomationError(RuntimeError):
    pass


@dataclass
class RunOutcome:
    ok: bool
    message: str
    duration_ms: int = 0
    detail: str = ""


class Runner:
    # 框画出来之后停一下再点。框和点击几乎是同时发生的，不停这一下，
    # 用户只看到窗口闪了一下，看不到"它认的是哪一个元素"。
    BOX_DWELL = 0.3
    # 浮层的落定动画（收拢 0.24 秒 + 闪 0.34 秒）走完，画面才算静止。
    # 取像素基线之前必须等这么久，否则基线里拍的是动画中间的一帧。
    OVERLAY_SETTLE = 0.64
    # 点击涟漪的存活时间（对齐 ui.overlay.PULSE_SECONDS）。点击之后立刻
    # 比对，看到的是自己画的那圈涟漪，不是应用的响应。
    OVERLAY_PULSE = 0.5

    def __init__(
        self,
        settings: dict[str, Any],
        log: Callable[[str, str], None],
        exclude_hwnd: int = 0,
        emit: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self.settings = settings
        self.log = log
        self.exclude_hwnd = int(exclude_hwnd or 0)
        self.emit = emit
        # 是否已经把内容交给目标应用。失败重试要靠它判断"重来一次会不会发两遍"：
        # 发送动作一旦发生，再重试就可能让同一个提示词发出去两次。
        self.sent = False

    # -- 浮层 ----------------------------------------------------------

    def _overlay(self, action: str, **payload: Any) -> bool:
        """浮层只是给人看的，任何异常都不能影响执行。返回有没有真的发出去。"""

        if self.emit is None:
            return False
        if not self.settings.get("show_overlay", True):
            return False
        try:
            self.emit("overlay", {"action": action, **payload})
            return True
        except Exception:
            return False

    def _overlay_hide(self) -> None:
        self._overlay("hide")

    def _overlay_show(self, frame: locator.Frame) -> None:
        self._overlay("show", rect=dict(frame.rect))

    def _overlay_box(self, rect: Rect, kind: str, label: str = "") -> None:
        self._overlay("box", rect=rect.as_dict(), kind=kind, label=label)

    def _overlay_progress(
        self, index: int, total: int, text: str, tone: str = "accent", detail: str = ""
    ) -> bool:
        return self._overlay(
            "progress", index=index, total=total, text=text, tone=tone, detail=detail
        )

    def _overlay_scan(self, rect: Rect, label: str = "识别中", tone: str = "warning") -> None:
        """标出"正在这一块里找"。

        识别一次要几百毫秒到几秒。这段时间里如果浮层什么都不画，用户看到的是
        一个静止的窗口，分不清"在算"还是"已经死了"。先给一块扫描区域，认出
        来之后再换成实框——找和找到是两件事，视觉上也要是两件事。
        """

        self._overlay("scan", rect=rect.as_dict(), label=label, tone=tone)

    def _overlay_quiet(self, seconds: float) -> None:
        """等浮层安静下来，再做像素比对。

        浮层画在目标窗口上方，截屏会把它一起截进来：角标收拢、落定那一下的
        闪、点击的涟漪，每一个都会让"这块像素变了"成立——哪怕应用本身什么都
        没做。以前这会让"粘贴校验"和"发送校验"把自己的动画当成应用的响应，
        明明没发出去也可能判成发了。所以取基线之前、开始比对之前，都先等这
        一段时间过去。没开浮层就没有这个问题，也就不必等。
        """

        if seconds <= 0:
            return
        if not self.settings.get("show_overlay", True):
            return
        time.sleep(seconds)

    def _overlay_pulse(self, point: tuple[int, int]) -> None:
        """点击的位置画一圈涟漪。点下去的那一下要看得见。"""

        self._overlay("pulse", point=(int(point[0]), int(point[1])))

    def _click(self, point: tuple[int, int]) -> None:
        """点一个屏幕坐标，顺手在浮层上留个回执。"""

        winapi.click(point)
        self._overlay_pulse(point)

    # -- 基础 ----------------------------------------------------------

    def _delay(self) -> float:
        try:
            value = float(self.settings.get("step_delay_seconds", 1.2))
        except (TypeError, ValueError):
            value = 1.2
        return max(0.3, min(10.0, value))

    def _locator(self) -> locator.Locator:
        return locator.Locator(self.settings, self.log)

    def _frame(self, hwnd: int) -> locator.Frame:
        frame = locator.grab(hwnd)
        if frame is None:
            # 拿不到画面的时候，原因比"读不到"重要得多：窗口被最小化、被别的
            # 程序整个盖住、还是已经关掉了，对应三种完全不同的处理方式。
            # 以前一律报"确认窗口没有被最小化"，用户照着做也没用。
            if not winapi.is_window(hwnd):
                raise AutomationError("目标窗口已经关闭。")
            if winapi.is_minimized(hwnd):
                raise AutomationError("目标窗口被最小化了，请先从任务栏把它恢复。")
            ratio = winapi.exposed_ratio(hwnd)
            if ratio < 0.5:
                raise AutomationError(
                    f"目标窗口被别的窗口挡住了（只露出 {int(ratio * 100)}%），"
                    "没有拿它的画面——挡住它的那个程序正在被截进来。"
                )
            raise AutomationError("无法读取目标窗口画面，请确认它没有停在一个空白页上。")
        return frame

    @staticmethod
    def _capture(rect: Rect):
        return imaging.capture_screen(rect.left, rect.top, rect.width, rect.height)

    def _wait_change(
        self,
        rect: Rect,
        baseline,
        timeout: float,
        threshold: float = 3.0,
        min_ratio: float = 0.0,
    ) -> tuple[bool, Any]:
        deadline = time.monotonic() + max(0.2, timeout)
        latest = baseline
        while time.monotonic() < deadline:
            current = self._capture(rect)
            if current is None:
                time.sleep(0.12)
                continue
            latest = current
            if min_ratio > 0:
                # 要求"变化面积足够大"，用来区分"面板真的换了"和"某个控件闪了一下"
                if vision.changed_ratio(baseline, current) >= min_ratio:
                    return True, current
            elif vision.looks_changed(baseline, current, threshold):
                return True, current
            time.sleep(0.12)
        return False, latest

    def _stable_baseline(self, rect: Rect, tries: int = 4):
        """等画面稳定下来再取基线，避免滚动动画的余波被当成"界面变了"。"""

        previous = self._capture(rect)
        for _ in range(max(1, tries)):
            time.sleep(0.25)
            current = self._capture(rect)
            if previous is not None and current is not None:
                if vision.changed_ratio(previous, current) < 0.004:
                    return current
            previous = current
        return previous

    def _locate_target(self) -> Optional[dict[str, Any]]:
        """按"锁定的窗口 → 锁定的标题 → 关键词"的顺序找目标。"""

        locked = int(self.settings.get("target_window_hwnd") or 0)
        if locked:
            info = winapi.window_by_hwnd(locked)
            if info is not None:
                return info
            self.log("warning", "锁定的目标窗口已经不在了，改回按关键词找。")
        title_lock = str(self.settings.get("target_window_title") or "").strip()
        if title_lock:
            info = winapi.find_window_by_title(title_lock, exclude_hwnd=self.exclude_hwnd)
            if info is not None:
                return info
            self.log("warning", f"没有标题含「{title_lock}」的窗口，改回按关键词找。")

        keywords = winapi.parse_keywords(str(self.settings.get("target_keywords") or ""))
        return winapi.find_target_window(
            keywords,
            exclude_hwnd=self.exclude_hwnd,
            title_hint=str(self.settings.get("target_title") or ""),
            process_hint=str(self.settings.get("target_process") or ""),
        )

    def _target_app(self) -> Optional[apps.AppSpec]:
        """目标应用的启动方式。填了自定义路径就按路径启动，否则按 Codex 处理。"""

        path = str(self.settings.get("target_path") or "").strip()
        if path:
            stem = Path(path).stem or "target"
            return apps.AppSpec(
                key="target",
                label=stem,
                process=stem,
                path=path,
                log=self.log,
            )
        return apps.codex_app(self.settings, self.log)

    def _find_window(self) -> dict[str, Any]:
        """找目标窗口；没开就按设置把它拉起来。

        用户要的是"到点就把消息发出去"，不该因为应用没打开就失败。所以这里
        先看有没有窗口，再退一步看进程在不在（收进托盘的窗口不可见），
        最后才启动一个新实例。
        """

        window = self._locate_target()
        if window is not None:
            return window

        if not bool(self.settings.get("auto_launch_target", True)):
            raise AutomationError("没有找到目标窗口。请先打开目标应用，或在设置中调整关键词。")

        spec = self._target_app()
        if spec is None:
            raise AutomationError("没有找到目标窗口，也没有可用的启动方式。")
        self.log("info", f"目标应用没有在前台可见，正在拉起 {spec.label}。")
        window = spec.ensure_visible()
        if window is None:
            raise AutomationError(
                f"没能把 {spec.label} 拉到前台，已停止以避免误输入。"
                "如果它需要管理员权限，请先手动启动一次。"
            )
        self.log("info", f"已前置 {spec.label}：{window.get('title') or ''}")
        return window

    def _activate(self, window: dict[str, Any]) -> int:
        hwnd = int(window["hwnd"])
        geometry = None
        if self.settings.get("restore_target_geometry", True):
            geometry = self.settings.get("target_geometry")
        if not winapi.ensure_foreground(hwnd, geometry):
            raise AutomationError("无法把目标窗口切到前台，已停止以避免误输入。")
        # 窗口大小是活的：用户随手拖一下，所有坐标都变。与其让识别算法去
        # 适应任意大小，不如在动手之前先把窗口摆成已知的形状。
        if self.settings.get("force_window_size", False):
            spec = str(self.settings.get("target_window_size") or "")
            if winapi.force_window_size(hwnd, spec):
                self.log("info", f"目标窗口已调成 {spec or '最大化'}。")
            else:
                self.log("warning", f"没能把目标窗口调成 {spec or '最大化'}，按当前大小继续。")
            if not winapi.ensure_foreground(hwnd):
                raise AutomationError("调整窗口大小之后没能把它留在前台。")
        return hwnd

    def _resolve_composer(self, hwnd: int) -> tuple[locator.Frame, Rect, str]:
        frame = self._frame(hwnd)
        # 先点亮浮层再去找：没亮的时候 scan()/box() 都是空操作，下面的过程
        # 就一点都看不见了。
        self._overlay_show(frame)
        self._overlay_scan(
            frame.to_screen(Rect(0, int(frame.height * 0.5), frame.width, frame.height)),
            "扫描输入框",
        )
        composer, source = self._locator().composer(frame)
        if composer is None:
            # 生成回答时输入框会被"停止"按钮顶掉，这时候报"重新识别目标"
            # 会把人引到错误的方向。先说清楚可能是哪种情况。
            raise AutomationError(
                "没有找到输入框：目标可能正在生成回复，或者界面还没加载完。"
                "等它停下来再试；如果一直这样，再到设置里重新识别目标。"
            )
        return frame, composer, source

    # -- 对话切换 ------------------------------------------------------

    def _sidebar_right(self, frame: locator.Frame, composer: Optional[Rect]) -> int:
        """左侧面板的右边界。识别不出来时按窗口宽度取一个保守值。"""

        limit = composer.left if composer is not None else 0
        try:
            edge = vision.detect_sidebar_right(frame.bitmap, limit)
        except Exception:
            edge = None
        if edge and edge > 160:
            return int(edge)
        return max(200, int(frame.width * 0.16))

    def _list_region(self, frame: locator.Frame, composer: Optional[Rect]) -> Rect:
        """侧边栏里"对话列表"所在的那块区域，去掉左侧图标栏。"""

        edge = self._sidebar_right(frame, composer)
        left = max(0, edge * 13 // 100)
        bottom = composer.top if composer is not None else int(frame.height * 0.88)
        return Rect(left, 0, edge, max(left + 60, bottom))

    def _list_rows(self, frame: locator.Frame, composer: Optional[Rect]) -> list[ocr.OcrLine]:
        """把侧边栏列表读成文字行，坐标换回客户区。"""

        region = self._list_region(frame, composer)
        crop = frame.bitmap.crop(region.left, region.top, region.right, region.bottom)
        rows: list[ocr.OcrLine] = []
        for line in ocr.recognize(crop):
            if line.width < 24:  # 图标被识别成的单个字符，跳过
                continue
            rows.append(
                ocr.OcrLine(
                    line.text,
                    region.left + line.left,
                    region.top + line.top,
                    line.width,
                    line.height,
                )
            )
        return rows

    def _chat_title_lines(self, frame: locator.Frame) -> list[ocr.OcrLine]:
        """消息区顶部显示的当前对话名。

        顶部这一条的高度跟着窗口走：小窗口里标题就在 140 像素上下，大窗口里
        也差不多，但高 DPI 下会整体下移。所以取"窗口高度的 18%，最少 180"，
        两边都能覆盖，又不会把下面的正文读进来。
        """

        edge = self._sidebar_right(frame, None)
        height = int(min(frame.height, max(180, frame.height * 0.18)))
        region = Rect(edge + 2, 0, min(frame.width, edge + 1200), height)
        crop = frame.bitmap.crop(region.left, region.top, region.right, region.bottom)
        lines = [line for line in ocr.recognize(crop) if line.width >= 40]
        return [
            ocr.OcrLine(line.text, region.left + line.left, region.top + line.top, line.width, line.height)
            for line in lines
        ]

    def _conversation_matches(self, frame: locator.Frame, title: str) -> bool:
        if not title:
            return True
        lines = self._chat_title_lines(frame)
        if ocr.find_line(lines, title, 0.9, strict=True) is not None:
            return True
        # 标题栏里那几个中文也可能被 OCR 整段丢掉（`PDF 渲染与 DOCX 处理`
        # 读成 `PDF DOCX`）。核对是只读的，放宽一点没有风险：认不出中文就
        # 按剩下的字认，总比把一次成功的切换判成失败、再点一次强。
        return any(self._dropped_match(line.text, title) >= 0.62 for line in lines)

    def _verify_conversation(self, hwnd: int, title: str, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            try:
                frame = self._frame(hwnd)
            except AutomationError:
                return False
            if self._conversation_matches(frame, title):
                return True
            time.sleep(0.45)
        return False

    def _composer_empty(self, hwnd: int, composer: Optional[Rect] = None) -> Optional[bool]:
        """输入框是否已经回到空状态。识别不了时返回 None。"""

        try:
            frame = self._frame(hwnd)
        except AutomationError:
            return None
        try:
            return self._locator().composer_is_empty(frame, composer)
        except Exception:
            return None

    def _composer_holds(self, hwnd: int, composer: Rect, content: str) -> bool:
        """输入框里是不是还留着刚粘进去的那段字。

        `composer_is_empty` 只认"占位符回来了"，而占位符是灰的，OCR 经常整个
        读不到——读不到就返回 False（还有内容），于是一条已经发出去的消息被
        判成失败，还会再补按一次 Enter。

        这里换个方向问：我们知道自己粘了什么。那段字还在框里，就说明真没发
        出去；不在了，就说明发出去了。短内容不做这个判断（几个字太容易撞上
        界面别处的文字），按"还有内容"处理，宁可保守。
        """

        key = ocr.normalize(content)
        if len(key) < 8:
            return True
        probe = key[: min(len(key), 24)]
        try:
            frame = self._frame(hwnd)
        except AutomationError:
            return True
        region = Rect(composer.left, composer.top, composer.right, composer.bottom)
        if region.height < 40 or region.width < 120:
            return True
        try:
            crop = frame.bitmap.crop(region.left, region.top, region.right, region.bottom)
            lines = ocr.recognize(crop)
        except Exception:
            return True
        if not lines:
            return True
        text = ocr.normalize(" ".join(line.text for line in lines))
        return self._ordered_overlap(probe, text) >= 0.6

    def _wait_send_ready(
        self,
        hwnd: int,
        composer: Rect,
        timeout: float = 20.0,
    ) -> tuple[locator.Frame, Rect, Optional[Rect], str]:
        """等目标应用生成完回答：发送位置从暂停键变回发送箭头。

        返回刷新后的画面、输入框、发送方块和它此刻的身份。等不到就把身份
        留在 pause，让调用方明确放弃这次发送，而不是去点那个暂停键。
        """

        deadline = time.time() + max(2.0, timeout)
        frame = self._frame(hwnd)
        slot, kind = self._locator().send_slot(frame, composer)
        while kind == vision.SEND_SLOT_PAUSE and time.time() < deadline:
            time.sleep(0.8)
            try:
                frame = self._frame(hwnd)
            except AutomationError:
                break
            grown, _ = self._locator().composer(frame)
            if grown is not None:
                composer = grown
            slot, kind = self._locator().send_slot(frame, composer)
        return frame, composer, slot, kind

    # 这里以前有一条"在侧边栏里翻列表找对话"的路径：回顶、逐屏下翻、点开
    # 「Show more」再翻。2.10 把它整条删掉了。
    #
    # 理由不是它慢，而是它永远不可能可靠：列表按项目分组、超过五条就折叠、
    # 历史长了要翻几十屏，而且每翻一屏都要重新截图识别——翻的过程中用户
    # 一动鼠标，列表就滚到别处去了。搜索不受这些影响：不依赖当前停在哪个
    # 对话上，也不改变列表的滚动位置。切对话只有这一条链路。

    # 搜索面板是**分组**的：命令在「Quick actions」下，对话在「Chats」下，
    # 文件在「Recent files」下。搜一个词往往三边都有结果，所以"认出一行字
    # 像目标标题"远远不够——必须先确定这一行属于哪个分组。
    SEARCH_HEADERS = frozenset(
        ocr.normalize(item)
        for item in (
            "Quick actions",
            "Chats",
            "Recent files",
            "快捷操作",
            "聊天",
            "最近文件",
        )
    )

    # 只有这些分组里装的是对话。切对话只在这里面找，文件、命令一律不碰。
    CHAT_SECTIONS = frozenset(ocr.normalize(item) for item in ("Chats", "聊天"))

    # 面板里可能出现的其它分组标题。它们不装对话，但用来划出「Chats」
    # 这个分组的下边界——不然文件行会被当成对话。
    OTHER_SECTIONS = frozenset(
        ocr.normalize(item)
        for item in (
            "Files",
            "Recent files",
            "Actions",
            "Text",
            "Projects",
            "Snippets",
            "Messages",
            "文件",
            "最近文件",
            "操作",
            "项目",
            "片段",
        )
    )

    # 面板宽度不能写死成窗口的某个比例。1920 宽的窗口上面板就有 1130 宽
    # （59%），比例卡在 0.22~0.78 时左边那条 20 像素正好把输入框和「All」
    # 挡在外面——面板明明开着，却被认成"没开"，于是又去点放大镜，把它关掉。
    #
    # 面板宽度是**有上限**的（约 1130 像素），所以窗口越窄，它的左边缘越靠左：
    # 1200 宽的窗口上面板从 72 像素开始，只占 6%。范围必须放得比这更宽，
    # 具体是哪一列交给"分组标题"和"筛选标签"去定，不靠比例猜。
    PANEL_SPAN = (0.02, 0.98)

    # 窗口最上面这一条是菜单栏和标题栏（File / Edit / View / Help）。
    # 它们的左边缘和面板内容一样是对齐的，不排掉的话「Help」会被当成搜索框，
    # 点下去点在菜单上。
    SEARCH_CHROME_BOTTOM = 56

    def _panel_span(self, frame: locator.Frame) -> tuple[float, float]:
        return frame.width * self.PANEL_SPAN[0], frame.width * self.PANEL_SPAN[1]

    def _panel_left(self, frame: locator.Frame) -> float:
        """面板左边缘不可能比这条线更左。

        两个来源，取更靠右的那个：

          · 认出来的侧边栏右边界。面板开在内容区上，一定在侧边栏右边；
          · 窗口宽度的 5%。面板是居中的，再窄的窗口也不会让面板压到最左边
            那条图标栏上（1200 宽时面板从 73 开始，占 6%）。

        侧边栏边界只在"它确实像侧边栏"时才用：面板打开后整个背景被压暗，
        `detect_panels` 会认不出侧边栏，反而把窗口右边框当成它（实测 1600 宽
        时返回 1595）。所以边界超过窗口宽度 30% 就当没认出来。1600 宽时侧边
        栏是 586（36.6%），刚好落在这一条外面——而那一档面板从 244 开始，用
        5% 的兜底正好，不会把面板自己排除掉。
        """

        low = self._panel_span(frame)[0]
        try:
            edge = vision.detect_sidebar_right(frame.bitmap) or 0
        except Exception:
            edge = 0
        if 120 < edge <= frame.width * 0.3:
            return max(low, edge + 8.0)
        return max(low, frame.width * 0.05)

    @staticmethod
    def _is_title(text: str, titles: frozenset[str]) -> bool:
        """这段文字是不是其中一个分组标题。

        后面最多容忍两个字符：OCR 会把「Chats」读成「Chats O」。再长就不认
        了——「ChatGPT can edit any file...」绝不能当成「Chat」这个标题。
        """

        key = ocr.normalize(text)
        if not key:
            return False
        for head in titles:
            if key == head:
                return True
            if key.startswith(head) and len(key) <= len(head) + 2:
                return True
        return False

    @staticmethod
    def _at_panel_left(line: ocr.OcrLine, column: int) -> bool:
        """这一行是不是贴着面板左边缘排的。

        面板底部那排筛选标签（All / Actions / Chats / Files / Text）是横着
        排开的一排，「All」贴着左边缘，后面的依次右移。分组标题永远贴着左
        边缘，靠这一点把"分组标题"和"筛选标签里那个同名标签"分开。
        """

        return column - 40 <= line.left <= column + 60

    def _panel_anchor(self, lines: list[ocr.OcrLine], frame: locator.Frame) -> Optional[ocr.OcrLine]:
        """面板最上面那个分组标题。

        取最靠上的一个：面板里「Quick actions」在「Chats」上面，认到哪个都
        能确定输入框的位置——输入框就在第一个分组标题的上面。

        三条限制都是被真实画面逼出来的：
          · 只在中段找，左边侧边栏和右边面板的文字一律不算；
          · 只在上半部分找：面板是垂直居中的，第一个分组标题一定在面板
            上半截里。这条同时把面板底部那排筛选标签排掉了——那排里也有
            一个「Chats」，认错它会把"输入框"算到面板底部去；
          · 标题后面最多容忍两个字符，见 _is_title。
        """

        low, high = self._panel_span(frame)
        low = self._panel_left(frame)
        ceiling = frame.height * 0.55
        inside = [
            line
            for line in lines
            if self.SEARCH_CHROME_BOTTOM < line.top < ceiling and low < line.left < high
        ]
        inside.sort(key=lambda line: line.top)
        for line in inside:
            if self._is_title(line.text, self.SEARCH_HEADERS):
                return line
        return None

    def _chat_section(
        self, lines: list[ocr.OcrLine], column: int
    ) -> Optional[ocr.OcrLine]:
        """面板里装对话的那个分组标题。

        贴着面板左边缘、认成「Chats」的那一行才算。底部筛选标签里也有一个
        「Chats」，它在面板中间偏右，不会被认成这里的分组标题。
        """

        for line in sorted(lines, key=lambda item: item.top):
            if line.top <= 40:
                continue
            if not self._at_panel_left(line, column):
                continue
            if self._is_title(line.text, self.CHAT_SECTIONS):
                return line
        return None

    def _section_bounds(
        self, lines: list[ocr.OcrLine], header: ocr.OcrLine, column: int, floor: int = 0
    ) -> tuple[int, int]:
        """一个分组的上边界和下边界。

        下一个贴着左边缘的标题就是它的结尾；最后一个分组没有下一个标题，
        就用面板底部那排筛选标签的位置收尾。两个都没有才按固定高度兜底。
        """

        titles = self.SEARCH_HEADERS | self.OTHER_SECTIONS
        ends = [
            line.top
            for line in lines
            if line.top > header.top + 6
            and self._at_panel_left(line, column)
            and self._is_title(line.text, titles)
        ]
        if floor > header.top:
            ends.append(floor)
        return header.top + 6, min(ends) if ends else header.top + 900

    # 面板底部那排筛选标签。面板一打开它就在，加载中、没结果时都在，
    # 所以它是"面板开着"最稳的证据。分组标题反而要等结果出来才有。
    FILTER_LABELS = frozenset(
        ocr.normalize(item)
        for item in ("All", "Actions", "Chats", "Files", "Text", "全部", "操作", "聊天", "文件", "文本")
    )

    def _filter_row(self, lines: list[ocr.OcrLine], frame: locator.Frame) -> list[ocr.OcrLine]:
        """面板底部那排筛选标签，从左到右。

        只认"同一行上至少三个标签、而且彼此挨着"的组合：单独一个「Chats」
        可能只是分组标题，三个以上横向排开才说明这是那排标签。

        按词算，不按行算。OCR 经常把挨得近的两个标签并成一段
        （`Files Text` 读成一行），按行匹配的话这一段既不是 Files 也不是
        Text，一排五个标签只剩两个能对上，整排就被丢掉了——面板于是被认成
        "没打开"。拆成词再比，合并与否都不影响。

        还要按"挨着"串成一串，不能只看纵向位置。范围放宽到 2% 之后，窗口
        右边那片对话正文里也可能有同一高度的行（实测 1600 宽时，正文里
        `codex-clipboard-28716eb8...` 那一行就和标签同高），只按 top 归组会
        把它拉进来，面板右边缘于是被算到 1532——框一下横跨整个窗口。
        标签之间只隔三十几像素，正文离得远，按间距一断就分开了。
        """

        low, high = self._panel_span(frame)
        candidates: list[tuple[ocr.OcrLine, int]] = []
        for line in lines:
            if not (low < line.left < high):
                continue
            tokens = [ocr.normalize(part) for part in line.text.split() if part.strip()]
            matched = [token for token in tokens if token in self.FILTER_LABELS]
            if matched:
                candidates.append((line, len(matched)))
        if not candidates:
            return []

        same_row: list[list[tuple[ocr.OcrLine, int]]] = []
        for item in sorted(candidates, key=lambda pair: pair[0].top):
            for group in same_row:
                if abs(item[0].top - group[0][0].top) <= 8:
                    group.append(item)
                    break
            else:
                same_row.append([item])

        best: list[tuple[ocr.OcrLine, int]] = []
        best_score = 0
        for group in same_row:
            group.sort(key=lambda pair: pair[0].left)
            chain: list[tuple[ocr.OcrLine, int]] = []
            for item in group:
                if chain and item[0].left - chain[-1][0].right > 120:
                    # 断开：这不是同一排标签，中间隔着一大片空白。
                    score = sum(count for _, count in chain)
                    if score > best_score:
                        best, best_score = chain, score
                    chain = []
                chain.append(item)
            score = sum(count for _, count in chain)
            if score > best_score:
                best, best_score = chain, score

        if best_score < 3:
            return []
        return [line for line, _ in best]

    def _panel_column(
        self, lines: list[ocr.OcrLine], frame: locator.Frame
    ) -> Optional[int]:
        """面板的左边缘对齐在哪一列。

        底部那排筛选标签最可靠：面板一打开它就在，加载中、没结果时都在，
        五个标签的第一个就贴着面板的左边缘。分组标题只在有结果时才有，
        拿它当备选。

        两个都没有就是"没开"。这里以前还有第三招——数哪一列最常被当作行的
        起点——但那招在侧边栏上永远成立（会话标题从上到下都对齐），于是
        面板关着也被认成开着，接着把侧边栏里的会话标题当搜索框，把词粘进了
        对话输入框。宁可说"没开"，让上层去按 Ctrl+K。
        """

        row = self._filter_row(lines, frame)
        if row:
            return row[0].left
        anchor = self._panel_anchor(lines, frame)
        if anchor is not None:
            return anchor.left
        return None

    def _search_input(self, hwnd: int) -> tuple[locator.Frame, Optional[Rect]]:
        """找搜索面板的输入框，返回它在客户区里的矩形。

        输入框是面板最上面那一行字，而且贴着面板的左边缘。所以先确定面板
        左边缘在哪一列，再取那一列里最靠上的一行。

        不靠占位文字认输入框：搜过一次之后词会留在框里，占位文字就没了，
        按"Search ChatGPT"去找会一直找不到——明明面板就在眼前。

        面板是居中的，所以右边缘按"和左边缘对称"算出来，比拿某一行的右端
        去量准：面板右边还压着对话正文，正文里那些长行会把框拉出去；底部
        那排标签也只铺到 "Text" 为止，量出来比面板窄一大截。

        实测（1600x1000）：面板左 244、右 1364，中心 804，正好是客户区中心。
        按左边缘镜像出来 1361，差 3 个像素。

        纵向先按排版**推**一个框，再拿识别到的字去**校**。反过来不行：输入框
        里那个词是高亮的蓝底白字，OCR 经常整个读不到，而下面第一行结果一定
        读得到。只看"这一列最靠上的一行"，认出来的就是结果行，框会整体掉到
        输入框下面去（实测掉到 248，真正的输入框在 144~200）。
        """

        frame = self._frame(hwnd)
        lines = ocr.recognize(frame.bitmap)
        column = self._panel_column(lines, frame)
        if column is None:
            return frame, None

        row = self._filter_row(lines, frame)
        floor = row[0].top - 40 if row else frame.height
        left_edge = column - 10
        right_edge = min(
            frame.width - 8,
            max(frame.width - left_edge, column + 320),
        )

        band = self._panel_box_guess(lines, frame, left_edge, right_edge, floor)
        column_lines = [
            line
            for line in lines
            if column - 24 <= line.left <= column + 48
            and self.SEARCH_CHROME_BOTTOM < line.top < floor
            # 分组标题（Chats / Quick actions）也贴着这一列，但它不是输入框。
            and not self._is_title(line.text, self.SEARCH_HEADERS | self.OTHER_SECTIONS)
        ]
        if band is not None:
            column_lines = [
                line
                for line in column_lines
                if band.top - 8 <= line.top <= band.bottom + 8
            ]

        if column_lines:
            first = min(column_lines, key=lambda line: line.top)
            return frame, Rect(
                left_edge + 12,
                max(0, first.top - 12),
                right_edge - 12,
                first.bottom + 12,
            )

        # 面板认出来了，但输入框那一行没认出来（字太小、光标在闪、或者刚清空）。
        # 这时候不该说"面板没开"——面板就开在那儿。按分组标题往上退一行，
        # 给一个够用的矩形：点它的左侧仍然会落进输入框。
        return frame, band

    def _panel_box_guess(
        self,
        lines: list[ocr.OcrLine],
        frame: locator.Frame,
        left_edge: float,
        right_edge: float,
        floor: float,
    ) -> Optional[Rect]:
        """没有输入框文字时，按分组标题的位置反推输入框那一行。

        面板的排版是固定的：输入框在最上面，往下隔一小段是第一个分组标题。
        所以"第一个标题再往上一段"就是输入框。这段距离是界面上固定的间距，
        跟着缩放走、不跟窗口大小走，所以按标题自己的高度算。

        从**底边**往上量，不从顶边往下量。底边（标题上方约一个标题高）在
        1200 和 1600 两个尺寸上都量得准，而输入框的高度会随窗口大小变（1600
        宽时 56 像素，1200 宽时 72），从顶边往下量总会差一截：实测 1200 宽
        时框的顶边掉到 89，真正的输入框从 58 就开始了。

        连标题都没有（还在加载）就按"面板垂直居中"反推：底部那排标签的位置
        就是面板的底边，把窗口高度减掉它，就是面板顶边。
        """

        low, high = self._panel_span(frame)
        low = self._panel_left(frame)
        headers = [
            line
            for line in lines
            if low < line.left < high
            and self.SEARCH_CHROME_BOTTOM < line.top < min(floor, frame.height * 0.7)
            and self._is_title(line.text, self.SEARCH_HEADERS)
        ]
        if headers:
            head = min(headers, key=lambda line: line.top)
            bottom = max(40.0, head.top - head.height)
            top = max(0.0, bottom - max(46.0, head.height * 3.7))
            return Rect(left_edge + 12, top, right_edge - 12, bottom)

        row = self._filter_row(lines, frame)
        if row:
            panel_bottom = row[0].top + row[0].height * 3
            top = max(0.0, frame.height - panel_bottom)
            return Rect(left_edge + 12, top, right_edge - 12, top + 56)
        if not lines:
            return None
        top = max(0, min(frame.height * 0.08, 160))
        return Rect(left_edge + 12, top, right_edge - 12, top + 56)

    def _panel_open(self, hwnd: int) -> tuple[locator.Frame, bool]:
        """面板此刻开没开。判据是面板自己的东西在不在，而不是"输入框认没认出来"。"""

        frame = self._frame(hwnd)
        lines = ocr.recognize(frame.bitmap)
        return frame, self._panel_column(lines, frame) is not None

    # 标题栏那一行（「Codex ⌄」+ 铃铛 + 放大镜）的垂直位置。窗口大小变了、
    # 缩放比例变了它都不动，所以先按这个固定范围扫，扫不到再用 OCR 认「Codex」。
    HEADER_BAND = (86, 118)

    def _magnifier_point(self, frame: locator.Frame) -> Optional[tuple[int, int]]:
        """侧边栏标题栏里那个放大镜的位置。

        不存坐标：标题栏里从左到右是「Codex ⌄」、铃铛、放大镜，右边还有
        内容区的图标。所以先找出侧边栏的右边界，再在这一段里找图标大小
        （10~30 像素宽）的连通块，最后一个就是放大镜。

        垂直位置不能按窗口高度算：菜单栏和标题栏都是固定像素高，窗口从
        3840x2115 缩到 2560x1410 时，按比例算出来的那条带会落在菜单栏和
        标题栏之间的空白上，一个图标都扫不到。改成先用固定范围，扫不到再
        用 OCR 认「Codex」——它和图标在同一行，认出来就知道这一行在哪。
        """

        right = vision.detect_sidebar_right(frame.bitmap) or 0
        if not (120 < right <= min(frame.width, 1000)):
            return None
        for top, bottom in self._header_bands(frame, right):
            point = self._icon_in_band(frame, right, top, bottom)
            if point is not None:
                return point
        return None

    def _header_bands(self, frame: locator.Frame, right: int) -> list[tuple[int, int]]:
        """标题栏那一行的几条候选横带，最准的排在最前面。

        不能只给一个固定像素范围：窗口可以是任意大小，同一个界面在 125% 和
        200% 缩放下标题栏的高度也不一样。所以三条路一起给：

          1. OCR 认出「Codex」——它和放大镜在同一行，认出来就直接对上了；
          2. 一个够宽的固定范围（覆盖常见的标题栏高度）；
          3. 按窗口高度取一条比例带，兜住特别大或特别小的窗口。
        """

        bands: list[tuple[int, int]] = []
        head = self._codex_row(frame, right)
        if head is not None:
            bands.append((max(0, head.top - 10), min(frame.height, head.bottom + 10)))
        top, bottom = self.HEADER_BAND
        bands.append((max(0, min(top, frame.height - 20)), min(bottom, frame.height)))
        proportional = (
            max(0, int(frame.height * 0.03)),
            min(frame.height, max(56, int(frame.height * 0.11))),
        )
        if proportional not in bands:
            bands.append(proportional)
        return bands

    def _codex_row(self, frame: locator.Frame, right: int) -> Optional[ocr.OcrLine]:
        """侧边栏标题栏里「Codex」那一行在哪。

        它的高度就是标题栏的高度，和窗口总高无关——窗口拉高十倍，它也不动。
        所以用它的位置当横带，比按比例算准。
        """

        try:
            height = min(frame.height, max(160, int(frame.height * 0.16)))
            strip = frame.bitmap.crop(0, 0, min(frame.width, max(120, right)), height)
            return ocr.find_text(ocr.recognize(strip), "Codex", 0.8)
        except Exception:
            return None

    @staticmethod
    def _icon_in_band(
        frame: locator.Frame, right: int, top: int, bottom: int
    ) -> Optional[tuple[int, int]]:
        """在一条横带里找图标大小的墨迹块，取最右边那个。

        宽度范围按"图标"来定，不按窗口比例：窗口拉到 3840 宽，标题栏里那个
        放大镜还是 18 像素——跟着窗口宽度放大的是内容，不是图标。范围取
        8~44，100% 到 200% 缩放都能覆盖。
        """

        if bottom <= top + 4:
            return None
        narrow, wide = 8, 44
        runs: list[tuple[int, int]] = []
        start = None
        for x in range(40, max(41, right - 6)):
            inked = any(frame.bitmap.lum(x, y) > 90 for y in range(top, bottom))
            if inked and start is None:
                start = x
            elif not inked and start is not None:
                runs.append((start, x))
                start = None
        if start is not None:
            runs.append((start, max(41, right - 6)))
        icons = [item for item in runs if narrow <= (item[1] - item[0]) <= wide]
        if not icons:
            return None
        left, end = icons[-1]
        return ((left + end) // 2, (top + bottom) // 2)

    def _section_rows(
        self, lines: list[ocr.OcrLine], top: int, bottom: int, column: int
    ) -> list[list[ocr.OcrLine]]:
        """把一个分组里的文字按"行"归并起来。

        OCR 经常把一行拆成好几段：行首图标一段、标题一段、右边还有项目名和
        快捷键一段（`0 20261107` + `32 Big LiquidGlass` + `henu classroom`）。
        按纵向位置叠在一起的就当成同一行。

        两边的都不要：右边是项目名和快捷键，太远会把标题淹掉；左边那一栏是
        原来的对话列表，标题长得一模一样——它不在面板里，点它等于没搜索。
        """

        inside = [
            line
            for line in lines
            if top < line.top < bottom and column - 60 <= line.left <= column + 200
        ]
        inside.sort(key=lambda line: (line.top, line.left))
        rows: list[list[ocr.OcrLine]] = []
        for line in inside:
            for row in rows:
                head = row[0]
                overlap = min(head.bottom, line.bottom) - max(head.top, line.top)
                if overlap >= 0.4 * min(head.height, line.height):
                    row.append(line)
                    break
            else:
                rows.append([line])
        for row in rows:
            row.sort(key=lambda item: item.left)
        return rows

    def _best_row(
        self,
        rows: list[list[ocr.OcrLine]],
        title: str,
        threshold: float = 0.75,
        reject_files: bool = True,
        dropped: bool = False,
    ) -> Optional[ocr.OcrLine]:
        """挑出最像目标标题的那一行，返回能点的那一块。

        一行的几段文字要合起来比：标题被拆成两段时，单独一段都不像，拼起来
        才像。但右边的项目名拼进去反而会把相似度拉低，所以按"从左边开始的
        前几段"逐个比，取最好的那个，不做整行硬拼。

        不要求一模一样：结果行前面常带着一个图标，OCR 会把它读成 "0" 或者
        一个字母（`0 20261021 18`）。阈值默认 0.75——低于这个就宁可认成没
        找到，也不乱点一条别的对话。调用方可以在"严格那档没找到"之后放宽到
        0.62 再试一次，这时候必须同时排除文件名：搜 `20261107` 时面板里既
        有对话也有 `2026-11-07-liquid-glass-c52.md`，两者的字面几乎一样，
        点错就打开了文件。

        `dropped=True` 是第三档，专门对付"OCR 把中文整段丢掉"：`PDF 渲染与
        DOCX 处理` 在结果行里读回来只有 `PDF DOCX`，按字面比永远对不上，
        而它明明就是目标。这一档只认"认出来的字能按顺序在标题里找到、而且
        一个都不多余"——反过来不成立就不算，避免点到别的对话上。
        """

        best: Optional[ocr.OcrLine] = None
        score = 0.0
        for row in rows:
            for take in range(1, len(row) + 1):
                head = row[:take]
                text = " ".join(item.text for item in head)
                if reject_files and self._looks_like_file(text, title):
                    continue
                value = (
                    self._dropped_match(text, title)
                    if dropped
                    else ocr.title_similarity(text, title)
                )
                if value <= score:
                    continue
                left = min(item.left for item in head)
                top = min(item.top for item in head)
                right = max(item.right for item in head)
                bottom = max(item.bottom for item in head)
                best = ocr.OcrLine(text, left, top, right - left, bottom - top)
                score = value
        return best if score >= threshold else None

    def _dropped_match(self, text: str, title: str) -> float:
        """OCR 掉字之后还能不能认出这是同一个标题，返回 0 或 0.62~0.9 的分数。"""

        key = ocr.normalize(title)
        flat = ocr.normalize(text)
        if not key or not flat:
            return 0.0
        shared = self._ordered_overlap(flat, key) * len(flat)
        # 认出来的字里最多容忍两个对不上的：结果行前面那个对话气泡图标会被
        # OCR 读成 "0" 或者一个字母（`O PDF DOCX`）。多出更多字，说明这是
        # 另一个标题，不是掉字。
        if len(flat) - shared > 2:
            return 0.0
        # 标题里有多少字被认出来了。掉一半以上就不认——太少了不足以确定身份。
        covered = shared / len(key)
        if covered < 0.5:
            return 0.0
        return 0.62 + 0.28 * covered

    # 结果行末尾带这些后缀的，是文件不是对话。
    FILE_SUFFIXES = (
        ".md", ".txt", ".json", ".py", ".ts", ".tsx", ".js", ".log", ".csv",
        ".pdf", ".png", ".jpg", ".jpeg", ".html", ".css", ".yaml", ".yml",
        ".sh", ".ps1", ".sql", ".zip", ".docx", ".xlsx", ".pptx", ".toml",
    )

    @classmethod
    def _looks_like_file(cls, text: str, title: str) -> bool:
        """这段文字像不像一个文件名。

        只有在标题本身也带同样的后缀时才不排除——真有一个对话就叫
        `notes.md` 的话，它仍然是对话。
        """

        body = text.strip().lower()
        if not body:
            return False
        wanted = title.strip().lower()
        for suffix in cls.FILE_SUFFIXES:
            if body.endswith(suffix):
                return not wanted.endswith(suffix)
        return False

    def _search_result(
        self, hwnd: int, title: str, timeout: float = 4.0
    ) -> tuple[locator.Frame, Optional[ocr.OcrLine]]:
        """在搜索面板「Chats」那个分组里找标题那一行。

        面板是分组的，同一个词会同时搜出对话、文件和命令。切对话只认
        「Chats」分组里的行——文件行长得再像也不行，点下去打开的是文件。

        分组要等结果出来才有，所以"没看到分组"不等于"没有结果"：可能还在
        加载。给它几秒，最后仍然没有「Chats」分组，才下结论说这个词在对话里
        没有结果。

        匹配分两档：先用 0.75 找"很像的"，找不到再放宽到 0.62 但排除文件名。
        放宽的那一档会写一行日志——用户看得见"这次是靠放宽才认出来的"，
        而不是以为一切如常。
        """

        deadline = time.monotonic() + max(1.0, timeout)
        frame = self._frame(hwnd)
        # 结果就在面板下半部分。先扫这一块，再逐轮读——否则用户看到的是一段
        # 完全静止的等待。
        self._overlay_show(frame)
        self._overlay_scan(
            frame.to_screen(
                Rect(0, int(frame.height * 0.08), frame.width, int(frame.height * 0.75))
            ),
            "匹配搜索结果",
        )
        chat: Optional[ocr.OcrLine] = None
        relaxed = False
        while True:
            frame = self._frame(hwnd)
            lines = ocr.recognize(frame.bitmap)
            column = self._panel_column(lines, frame)
            chat = self._chat_section(lines, column) if column is not None else None
            if chat is not None:
                row = self._filter_row(lines, frame)
                floor = row[0].top - 40 if row else 0
                top, bottom = self._section_bounds(lines, chat, column, floor)
                rows = self._section_rows(lines, top, bottom, column)
                hit = self._best_row(rows, title, threshold=0.75)
                if hit is not None:
                    return frame, hit
                hit = self._best_row(rows, title, threshold=0.62, reject_files=True)
                if hit is not None:
                    if not relaxed:
                        relaxed = True
                        self.log("info", f"「{title}」是按放宽的相似度认出来的：{hit.text}")
                    return frame, hit
                hit = self._best_row(
                    rows, title, threshold=0.62, reject_files=True, dropped=True
                )
                if hit is not None:
                    self.log("info", f"「{title}」的中文被 OCR 丢掉了，按剩下的字认出来：{hit.text}")
                    return frame, hit
            if time.monotonic() >= deadline:
                break
            time.sleep(0.6)

        if chat is None:
            self.log("info", f"搜索结果里没有对话分组，「{title}」不是一个对话。")
        return frame, None






    @staticmethod
    def _result_label(text: str, title: str) -> str:
        """结果行在浮层上显示什么。

        OCR 常把行首那个对话气泡图标读成一个字符（`0 20261021 18`）。去掉
        之后剩下的部分还能和标题对上，就按剩下的显示——否则原样显示，别
        为了好看把真正认到的内容改掉。
        """

        cleaned = text.strip()
        first, _, rest = cleaned.partition(" ")
        rest = rest.strip()
        if len(first) <= 2 and rest and ocr.title_similarity(rest, title) >= 0.9:
            return rest
        return cleaned

    def _open_search(
        self, hwnd: int, frame: locator.Frame, note: Optional[Callable[[str], None]] = None
    ) -> tuple[Optional[locator.Frame], Optional[Rect]]:
        """把搜索面板弄到"开着"的状态，返回面板所在画面和输入框的矩形。

        四条路，从便宜到贵，每条都以"面板自己的东西出现了"为准：

          1. 先看它是不是本来就开着（放大镜是开关，再点一下会把它关掉）；
          2. 点标题栏里的放大镜；
          3. 按 Ctrl+K——桌面版认这个键，而且面板打开后焦点直接在输入框里；
          4. 按几何位置点：侧边栏右边缘往左一点、标题栏那一行。

        以前只有第 2 条，而且"没认出输入框"就等于"面板没开"，于是连着点三次
        放大镜——第一下打开，第二下关掉，第三下又打开，最后报"面板没出现"。
        现在"面板开没开"和"输入框认没认出来"是两件事，分开判断。
        """

        if winapi.foreground_window() != int(hwnd):
            if not winapi.ensure_foreground(hwnd):
                self.log("warning", "目标窗口不在前台，没有去点放大镜。")
                return None, None
            frame = self._frame(hwnd)

        # 面板在标题栏那一带，先标出"在找它"，再开始认。
        self._overlay_show(frame)
        self._overlay_scan(
            frame.to_screen(Rect(0, 0, frame.width, int(frame.height * 0.62))), "查找搜索框"
        )

        fresh, box = self._search_input(hwnd)
        if box is not None:
            self._overlay_box(fresh.to_screen(box), "search", "搜索框")
            self.log("info", "搜索面板本来就开着。")
            return fresh, box

        if note is not None:
            note("打开搜索")

        point = self._magnifier_point(frame)
        if point is not None:
            self._overlay_box(
                frame.to_screen(Rect(point[0] - 14, point[1] - 14, point[0] + 14, point[1] + 14)),
                "search",
                "放大镜",
            )
            self._click(frame.point_to_screen(point))
            fresh, box = self._wait_panel(hwnd)
            if box is not None:
                self._overlay_box(fresh.to_screen(box), "search", "搜索框")
                self.log("info", "搜索面板已打开。")
                return fresh, box
            self.log("info", "点了放大镜，画面上还没看到面板。")
        else:
            self.log("warning", "标题栏里没找到放大镜。")

        # 键盘兜底。不依赖任何图标位置，窗口怎么改版都不影响。
        winapi.ctrl_k()
        fresh, box = self._wait_panel(hwnd, 1.4)
        if box is not None:
            self._overlay_box(fresh.to_screen(box), "search", "搜索框")
            self.log("info", "搜索面板已打开（Ctrl+K）。")
            return fresh, box

        # 再点一次：第一次点击有时只是把窗口激活，面板并不弹。
        if point is not None:
            self._click(frame.point_to_screen(point))
            fresh, box = self._wait_panel(hwnd)
            if box is not None:
                self._overlay_box(fresh.to_screen(box), "search", "搜索框")
                self.log("info", "搜索面板已打开（第二次点击）。")
                return fresh, box

        # 最后一招：按几何位置点。图标挪了位置、OCR 认不出来时还能试一次。
        guess = self._magnifier_guess(frame)
        if guess is not None and guess != point:
            self.log("info", "按几何位置再点一次放大镜。")
            self._click(frame.point_to_screen(guess))
            fresh, box = self._wait_panel(hwnd)
            if box is not None:
                self._overlay_box(fresh.to_screen(box), "search", "搜索框")
                return fresh, box

        self.log("warning", "四条路都试过了，搜索面板没有出现。")
        return None, None

    def _wait_panel(
        self, hwnd: int, timeout: float = 1.8
    ) -> tuple[Optional[locator.Frame], Optional[Rect]]:
        """等面板出现。以"面板自己的东西"为准，不等固定时间。"""

        deadline = time.monotonic() + max(0.4, timeout)
        frame: Optional[locator.Frame] = None
        while True:
            try:
                frame, box = self._search_input(hwnd)
            except AutomationError:
                return frame, None
            if box is not None:
                return frame, box
            if time.monotonic() >= deadline:
                return frame, None
            time.sleep(0.25)

    def _magnifier_guess(self, frame: locator.Frame) -> Optional[tuple[int, int]]:
        """放大镜大概在哪：侧边栏右边缘往左一点，标题栏那一行。

        只在图标真的认不出来时用。位置是相对的——跟着侧边栏走，不写死像素。
        """

        right = vision.detect_sidebar_right(frame.bitmap) or int(frame.width * 0.3)
        if right < 160:
            return None
        top, bottom = self.HEADER_BAND
        return (max(60, right - 46), (top + bottom) // 2)

    def _switch_conversation(
        self,
        hwnd: int,
        title: str,
        step: Optional[Callable[..., None]] = None,
    ) -> None:
        """切到一个对话。

        只走搜索这一条链路：点放大镜 → 输标题 → 在「Chats」分组里点结果 →
        核对标题栏。不去滚侧边栏列表——列表会折叠、会分组、会随历史增长而
        无限变长，翻列表既慢又永远翻不完；搜索是唯一的正解，而且它不依赖
        应用当前停在哪个对话上。
        """

        strict = bool(self.settings.get("strict_conversation", True))
        frame = self._frame(hwnd)
        self.log("info", f"切换对话：{title}")

        def note(text: str, tone: str = "accent") -> None:
            """换一下浮层上那行字，不占新的进度格。"""

            if step is None:
                return
            try:
                step(text, tone, bump=False)
            except TypeError:
                pass

        # 浮层要先亮起来。没亮的时候 box()/progress() 都是空操作，下面每一步
        # 画的框会悄悄丢掉——用户看到的就只剩"窗口自己动了几下"。
        self._overlay_show(frame)
        note("切换对话")

        fresh, box = self._open_search(hwnd, frame, note)
        if box is None or fresh is None:
            self._conversation_failed(hwnd, title, strict, "搜索面板打不开")
            return

        note("输入关键词")
        self._overlay_box(fresh.to_screen(box), "search", "搜索框")
        time.sleep(self.BOX_DWELL)
        # 点输入框靠左的位置，不点正中：面板右边还压着对话正文，框量得
        # 宽一点窄一点都可能，但贴着左边缘的这一段一定在输入框里。
        self._click(fresh.point_to_screen((box.left + 120, box.center[1])))
        time.sleep(0.3)
        winapi.ctrl_a()
        winapi.set_clipboard_text(title)
        winapi.ctrl_v()
        time.sleep(min(self._delay(), 1.2))
        if not self._input_has_title(hwnd, title, box):
            # 没粘进去就再粘一次。以前这里直接往下走，结果是"搜了个空"，
            # 然后报"搜索结果里没有这个对话"——把焦点问题说成了不存在。
            self.log("warning", "搜索框里没看到刚输入的关键词，重新粘贴一次。")
            winapi.ctrl_a()
            winapi.ctrl_v()
            time.sleep(min(self._delay(), 1.0))
            if not self._input_has_title(hwnd, title, box):
                # 两次都没进去，说明刚才认的那个"搜索框"不是搜索框。
                # 这时候绝不能往下点——点下去可能把关键词发到对话里。
                winapi.press_escape()
                self._conversation_failed(hwnd, title, strict, "关键词没有进入搜索框")
                return
        self.log("info", f"已把「{title}」输入搜索框。")

        # 结果就在输入框下面。认出哪一行是目标，框住它，再点——
        # 比按回车稳：回车落在"当前高亮的那一条"，而高亮在哪并不确定。
        note("选中结果")
        frame, hit = self._search_result(hwnd, title)
        if hit is not None:
            shown = self._result_label(hit.text, title)
            self._overlay_box(
                frame.to_screen(Rect(hit.left - 8, hit.top - 8, hit.right + 8, hit.bottom + 8)),
                "search",
                f"搜索结果 · {shown}",
            )
            self.log("info", f"搜索结果里认出了「{shown}」，点它。")
            time.sleep(self.BOX_DWELL)
            self._click(frame.point_to_screen(hit.center))
            time.sleep(min(self._delay(), 1.2))
            if self._verify_conversation(hwnd, title, 6.0):
                self.log("info", f"已切换到：{title}")
                return
            self.log("warning", f"点了搜索结果「{shown}」，标题栏没有变成「{title}」。")
        else:
            self.log("warning", f"搜索结果里没认出「{title}」这一条。")

        # 认不出结果时还有一条路：面板里第一条通常已经是高亮的，回车打开它，
        # 再用标题栏核对。核对不上就退回来，不会停在别的对话上。
        note("回车打开第一条", "warning")
        winapi.press_enter()
        time.sleep(min(self._delay(), 1.2))
        if self._verify_conversation(hwnd, title, 5.0):
            self.log("info", f"已切换到：{title}（回车打开第一条结果）")
            return
        self.log("warning", "回车打开的不是目标对话。")
        winapi.press_escape()
        self._conversation_failed(hwnd, title, strict, "搜索结果里没有这一条")

    def _conversation_failed(self, hwnd: int, title: str, strict: bool, reason: str) -> None:
        if strict:
            raise AutomationError(f"找不到对话「{title}」（{reason}），已停止发送。")
        self.log("warning", f"没能切换到「{title}」（{reason}），消息会发到当前对话。")

    def _input_has_title(
        self, hwnd: int, title: str, band: Optional[Rect] = None
    ) -> bool:
        """搜索框里是不是真的有这个关键词。

        比对按"按顺序能对上多少个字"算，不按整串相等：OCR 会读错一两个字符，
        更常见的是**把中文整段丢掉**。实测搜「PDF 渲染与 DOCX 处理」，输入框
        里读回来的是 `PDF DOCX`——词确实进去了，结果列表也出来了，但按"前
        八个字符"去比永远对不上，于是被当成"没粘进去"，重试一次再放弃。

        按顺序对上的比例够一半就算进去：`pdf渲染与docx处理` 和 `pdfdocx`
        有 7 个字符能按顺序对上，7/12 过线；而输错的关键词（上一次搜索留下
        的 `clarify say`）对上不到十分之一，过不了。

        认不出来时返回 True——宁可放过，也不要因为 OCR 抖动把一次正常的输入
        当成失败重来一遍。
        """

        try:
            frame = self._frame(hwnd)
        except AutomationError:
            return True
        lines = ocr.recognize(frame.bitmap)
        if not lines:
            return True
        key = ocr.normalize(title)
        if not key:
            return True

        # 只看输入框那一条横带。搜索结果的标题和关键词长得一样，不限制范围
        # 的话，上一次搜索留下的结果就能让"没粘进去"看起来像粘进去了。
        top = 0.0
        bottom = frame.height * 0.5
        if band is not None:
            top = max(0.0, band.top - 40)
            bottom = band.bottom + 40
        text = ocr.normalize(
            " ".join(line.text for line in lines if top <= line.top <= bottom)
        )
        if not text:
            return False
        if len(key) < 4:
            return key in text
        # 有拉丁词就先按词算：OCR 读拉丁字母很稳，中文经常整段丢掉。
        # `CueRelay 工具箱自检，这一段稍后会被清空` 里只有 `cuerelay` 会被读
        # 出来，按整串算比例只有三成，永远过不了线。
        words = [word for word in re.findall(r"[a-z0-9]{3,}", key)]
        if words:
            hits = sum(1 for word in words if word in text)
            if hits * 2 >= len(words):
                return True
        return self._ordered_overlap(key, text) >= 0.5

    @staticmethod
    def _ordered_overlap(key: str, text: str) -> float:
        """key 里有多少字符能按顺序在 text 里找到（最长公共子序列的比例）。"""

        if not key:
            return 0.0
        previous = [0] * (len(text) + 1)
        for left in key:
            current = [0]
            for index, right in enumerate(text, start=1):
                if left == right:
                    current.append(previous[index - 1] + 1)
                else:
                    current.append(max(current[index - 1], previous[index]))
            previous = current
        return previous[-1] / len(key)

    # -- 粘贴 ----------------------------------------------------------

    @staticmethod
    def _text_point(composer: Rect) -> tuple[int, int]:
        """输入框里"肯定落在文字区"的那个点。

        不能用正中：输入框这个矩形把底下那排控件（模型选择、麦克风、发送）
        一起框进来了，正中正好落在控件那一行上。点在那儿既不会让文字区拿到
        焦点，还可能点中麦克风——实测点出过「Voice chat couldn't start」，
        清空时也因为焦点不在文字区而失败。

        文字区在矩形的上半截，靠左四成：那里离所有控件都远。
        """

        return (
            composer.left + int(composer.width * 0.4),
            composer.top + int(composer.height * 0.28),
        )

    def _paste(self, hwnd: int, frame: locator.Frame, composer: Rect, retry: bool = False) -> None:
        point = self._text_point(composer)
        if retry:
            # 再往左下挪一点：第一次点可能只是把窗口激活，光标没进去。
            point = (point[0], min(composer.bottom - 8, point[1] + 12))
        self._click(frame.point_to_screen(point))
        time.sleep(min(self._delay(), 0.5))
        if winapi.foreground_window() != hwnd and not winapi.ensure_foreground(hwnd):
            raise AutomationError("目标窗口在粘贴前失去焦点。")
        winapi.ctrl_v()
        time.sleep(0.12)

    # -- 应用开关 ------------------------------------------------------

    def _run_launch(self, task: dict[str, Any]) -> RunOutcome:
        """定时启动：先把代理确保起来，再把目标拉到前台。"""

        started = time.monotonic()
        window = apps.start_codex(self.settings, self.log)
        duration = int((time.monotonic() - started) * 1000)
        if window is None:
            raise AutomationError("没能把目标应用拉到前台。")
        title = str(window.get("title") or "")
        message = f"已启动并前置{f'：{title}' if title else ''}"
        self.log("success", message)
        return RunOutcome(ok=True, message=message, duration_ms=duration)

    def _run_close(self, task: dict[str, Any]) -> RunOutcome:
        """定时关闭：真的退出，而不是收进托盘。"""

        started = time.monotonic()
        spec = apps.codex_app(self.settings, self.log)
        if spec.status() == "未运行":
            message = "目标应用本来就没有运行"
            self.log("info", message)
            return RunOutcome(ok=True, message=message, duration_ms=0)
        if not spec.close(force=True):
            raise AutomationError("关闭目标应用失败，进程还在运行。")
        duration = int((time.monotonic() - started) * 1000)
        message = f"已关闭 {spec.label}"
        self.log("success", message)
        return RunOutcome(ok=True, message=message, duration_ms=duration)

    # -- 主流程 --------------------------------------------------------

    def run_task(self, task: dict[str, Any]) -> RunOutcome:
        kind = str(task.get("kind") or "prompt")
        if kind == "launch":
            return self._run_launch(task)
        if kind == "close":
            return self._run_close(task)

        started = time.monotonic()
        content = str(task.get("content") or "")
        if not content.strip():
            raise AutomationError("提示词内容为空。")

        action = str(task.get("action") or "click")
        conversation = str(task.get("conversation_title") or "").strip()

        steps = 3
        if conversation:
            steps += 1
        if self.settings.get("verify_paste", True):
            steps += 1
        if self.settings.get("verify_send", True):
            steps += 1
        step_index = 0

        def advance(
            text: str, tone: str = "accent", bump: bool = True, detail: str = ""
        ) -> None:
            """推进一格进度。bump=False 只改文案，不占新的格数。"""

            nonlocal step_index
            if bump:
                step_index += 1
            self._overlay_progress(step_index, steps, text, tone, detail)

        old_clipboard = winapi.get_clipboard_text()
        try:
            saved_cursor = None
            try:
                saved_cursor = winapi.get_cursor_pos()
            except Exception:
                saved_cursor = None

            window = self._find_window()
            hwnd = self._activate(window)
            self.log("info", f"目标窗口：{window.get('title') or window.get('process_name')}")
            time.sleep(min(self._delay(), 0.6))

            if conversation:
                advance("切换对话", detail=f"搜索「{conversation}」")
                self._switch_conversation(hwnd, conversation, advance)

            advance("定位输入框", detail="扫描窗口")
            frame, composer, source = self._resolve_composer(hwnd)
            self.log("info", f"输入框定位：{source}")
            self._overlay_show(frame)
            self._overlay_box(
                frame.to_screen(composer),
                "composer",
                f"输入框 · {source} · {composer.width}×{composer.height}",
            )
            advance("定位输入框", detail=f"已锁定 · {source}", bump=False)
            locator.remember_composer(self.settings, frame, composer)

            screen_composer = frame.to_screen(composer)
            # 变量在最后一刻展开：定时任务里的 {{date}} 要的是"执行那天"，
            # 而不是当初保存任务那天。{{clipboard}} 用的是替换剪贴板之前的值。
            content = templates.expand(content, clipboard=old_clipboard or "")
            winapi.set_clipboard_text(content)

            advance("粘贴提示词", detail=f"{len(content)} 字")
            # 基线要在浮层静止之后取：浮层同样会被截进画面，框还在收拢、
            # 还在闪的时候拍下来，后面就会把自己的动画当成"输入框变了"。
            self._overlay_quiet(self.OVERLAY_SETTLE)
            baseline = self._capture(screen_composer)
            if baseline is None:
                raise AutomationError("无法读取输入框画面，请确认目标窗口没有被遮挡。")
            self._paste(hwnd, frame, composer)
            # 点击的涟漪正好落在输入框里，等它散掉再比对。
            self._overlay_quiet(self.OVERLAY_PULSE)
            verify_paste = bool(self.settings.get("verify_paste", True))
            if verify_paste:
                advance("校验粘贴", detail="比对输入框画面")
                changed, _ = self._wait_change(screen_composer, baseline, 4.0)
                if not changed:
                    self.log("warning", "输入框没有变化，重试粘贴。")
                    self._paste(hwnd, frame, composer, retry=True)
                    self._overlay_quiet(self.OVERLAY_PULSE)
                    changed, _ = self._wait_change(screen_composer, baseline, 4.0)
                if not changed:
                    raise AutomationError("粘贴后输入框没有变化，已停止发送。")
            else:
                time.sleep(min(self._delay(), 1.0))

            # 输入框可能因为多行内容而变高，发送前必须重新定位。
            fresh = self._frame(hwnd)
            grown, _ = self._locator().composer(fresh)
            if grown is None:
                grown = composer

            # 粘贴完必须确认输入框里真的有内容。空的时候同一个位置画的是麦克风，
            # 点下去会开始录音而不是发送——这正是之前"看起来发了其实没发"的原因。
            if self._composer_empty(hwnd, grown) is True:
                raise AutomationError("提示词没有进入输入框，已停止发送。")

            send_point = None
            slot_kind = vision.SEND_SLOT_UNKNOWN
            if action == "click":
                slot, slot_kind = self._locator().send_slot(fresh, grown)
                if slot_kind == vision.SEND_SLOT_PAUSE:
                    # 这个位置此刻是暂停键，说明目标应用还在生成回答。等它画回
                    # 发送箭头再点，否则这一下会把正在生成的回答掐掉。
                    advance("等待生成结束", "warning", bump=False)
                    self.log("info", "目标应用正在生成回答，等它结束再发送。")
                    fresh, grown, slot, slot_kind = self._wait_send_ready(hwnd, grown)
                if slot_kind == vision.SEND_SLOT_PAUSE:
                    raise AutomationError(
                        "目标应用一直在生成回答，发送位置此刻是暂停键，已跳过这次发送。"
                    )
                if slot_kind == vision.SEND_SLOT_MIC:
                    # 输入框空着的时候这个位置画的是麦克风。它会变回麦克风，
                    # 说明内容根本没进去——点下去会开语音通话，不是发送。
                    raise AutomationError(
                        "输入框里没有内容（发送位置还是麦克风），已停止发送。"
                    )
                if slot is not None:
                    send_point = slot.center
                elif not self._locator().recognition_enabled:
                    # 关掉了识别，只能退回校准锚点。
                    send_point = self._locator().send_point(fresh, grown)
                else:
                    self.log("warning", "没认出发送按钮，改用 Enter 发送。")

            self._overlay_show(fresh)
            self._overlay_box(
                fresh.to_screen(grown),
                "composer",
                f"输入框 · {grown.width}×{grown.height}",
            )
            send_region = fresh.to_screen(grown)

            advance("发送", detail="点击发送按钮" if send_point is not None else "按 Enter 发送")
            screen_send = None
            if send_point is not None:
                screen_send = fresh.point_to_screen(send_point)
                half = 22
                self._overlay_box(
                    vision.Rect(
                        screen_send[0] - half,
                        screen_send[1] - half,
                        screen_send[0] + half,
                        screen_send[1] + half,
                    ),
                    "send",
                    "发送按钮" if slot_kind != vision.SEND_SLOT_UNKNOWN else "发送位置",
                )
            # 发送按钮的框线就画在输入框里，落定那一下的闪也在里面：
            # 等它彻底静止再取基线，否则基线拍的是动画中间的一帧。
            self._overlay_quiet(self.OVERLAY_SETTLE)
            before_send = self._capture(send_region)
            # 从这里开始内容就交出去了：重试可能让同一个提示词发两遍。
            self.sent = True
            if screen_send is not None:
                self._click(screen_send)
            else:
                if action == "click":
                    self.log("warning", "未识别到发送按钮，改用 Enter 发送。")
                winapi.press_enter()

            verify_send = bool(self.settings.get("verify_send", True))
            if verify_send:
                advance("校验发送", detail="等待输入框清空")
                self._overlay_quiet(self.OVERLAY_PULSE)
                cleared, _ = self._wait_change(send_region, before_send, 6.0)
                if not cleared:
                    self.log("warning", "输入框没有清空，补按一次 Enter。")
                    winapi.press_enter()
                    self._overlay_quiet(self.OVERLAY_PULSE)
                    cleared, _ = self._wait_change(send_region, before_send, 4.0)
                # 像素变化只能说明"输入框动了"，点中麦克风开始录音也算变化。
                # 真正的判据是占位符回来——那才代表内容被送走了。
                #
                # 但占位符是灰色的，OCR 常常整个读不到。读不到时不能直接判成
                # "还有内容"：实测消息明明发出去了、对话里也能看到，却因为
                # 占位符没认出来被判成失败，接着又按一次 Enter。
                # 这时候拿"我们刚粘进去的那段字还在不在"来判——它比占位符硬：
                # 发出去了就一定不在了。
                if self._composer_empty(hwnd, grown) is False:
                    if self._composer_holds(hwnd, grown, content):
                        self.log("warning", "输入框里仍然有内容，补按一次 Enter。")
                        winapi.press_enter()
                        time.sleep(min(self._delay(), 1.2))
                    else:
                        self.log("info", "占位符没认出来，但粘进去的内容已经不在输入框里了，按已发送处理。")
                if self._composer_holds(hwnd, grown, content):
                    raise AutomationError(
                        "发送后输入框里仍有内容，消息没有发出去。"
                        "目标应用可能暂停了这个对话，或额度已经用尽。"
                    )
                if not cleared:
                    raise AutomationError("发送后输入框没有清空，消息可能没有发出去。")
            else:
                time.sleep(min(self._delay(), 1.0))

            elapsed = int((time.monotonic() - started) * 1000)
            self._overlay_progress(
                steps, steps, "已发送", "success", detail=f"用时 {elapsed / 1000:.1f} 秒"
            )
            self.log("success", f"已发送：{task.get('name') or '未命名任务'}")
            return RunOutcome(True, "已发送", elapsed)
        except Exception as exc:
            # 失败也要留个回执：卡片变红、写明为什么停，停一下再撤浮层。
            # 否则浮层"啪"地消失，用户分不清是成功了、还是断在了哪一步。
            reason = str(exc) or exc.__class__.__name__
            if self._overlay_progress(step_index, steps, "已停止", "danger", detail=reason[:46]):
                time.sleep(0.9)
            raise
        finally:
            if old_clipboard is not None:
                try:
                    winapi.set_clipboard_text(old_clipboard)
                except Exception:
                    pass
            if saved_cursor is not None:
                try:
                    winapi.move_cursor(saved_cursor)
                except Exception:
                    pass
            self._overlay_hide()

    # -- 工具箱用的公开入口 --------------------------------------------
    #
    # 下面这些方法不做完整流水线，只做"一件事"，供界面上的按钮直接调用。
    # 它们和 run_task 共用同一把窗口锁（由 Scheduler.run_exclusive 提供），
    # 所以不会和正在执行的任务抢鼠标键盘。

    def attach(self) -> tuple[dict[str, Any], int]:
        """找到（必要时拉起）目标窗口并切到前台。"""

        window = self._find_window()
        hwnd = self._activate(window)
        return window, hwnd

    def _rows_once(self, hwnd: int) -> tuple[locator.Frame, Optional[Rect], list[ocr.OcrLine]]:
        frame = self._frame(hwnd)
        composer, _ = self._locator().composer(frame)
        return frame, composer, self._list_rows(frame, composer)

    def sidebar_rows(self, hwnd: int, scrolls: int = 0) -> list[ocr.OcrLine]:
        """读侧边栏文字。scrolls > 0 时先回到顶部，再一屏一屏往下扫。

        返回的是"扫到过的所有行"的并集，每一行带着它当时那一刻的坐标——
        所以只适合用来认标题，不能拿去点。要点击请走 open_conversation，
        它会重新定位并校验。
        """

        frame, composer, rows = self._rows_once(hwnd)
        if scrolls <= 0:
            return rows

        collected: dict[str, ocr.OcrLine] = {}
        for row in rows:
            collected.setdefault(ocr.normalize(row.text), row)

        region = self._list_region(frame, composer)
        point = frame.point_to_screen(region.center)
        signature = tuple(row.text for row in rows)

        # 滚动之前先确认两件事：目标窗口在前台，而且那个点确实落在它的客户区
        # 里。滚轮是发给"鼠标底下那个窗口"的，窗口一旦被挡住，这一串滚动就会
        # 落到别人的界面上——用户看到的是自己的页面莫名其妙一直在往上翻。
        if winapi.foreground_window() != int(hwnd):
            raise AutomationError("目标窗口不在前台，已停止滚动，避免滚到别的窗口上。")
        if not (0 <= point[0] - frame.rect["left"] < frame.width
                and 0 <= point[1] - frame.rect["top"] < frame.height):
            raise AutomationError("侧边栏位置异常，已停止滚动。")

        # 先回到顶部：上一轮留下的滚动位置会让靠上的对话永远扫不到。
        for _ in range(6):
            winapi.scroll(point, -10)
            time.sleep(0.3)
            frame, composer, rows = self._rows_once(hwnd)
            for row in rows:
                collected.setdefault(ocr.normalize(row.text), row)
            current = tuple(row.text for row in rows)
            if current == signature:
                break
            signature = current

        for _ in range(max(1, scrolls)):
            winapi.scroll(point, 6)
            time.sleep(0.35)
            frame, composer, rows = self._rows_once(hwnd)
            for row in rows:
                collected.setdefault(ocr.normalize(row.text), row)
            current = tuple(row.text for row in rows)
            if current == signature:
                break
            signature = current

        return list(collected.values())

    # 判断侧边栏收没收起时看的那一竖条：图标栏大约 48~72 像素宽，展开后的
    # 侧边栏有 240 以上，所以 90~190 这一段在"收起"时是纯背景，在"展开"时
    # 一定压着标题文字。
    SIDEBAR_BAND = (90, 190)

    def sidebar_state(self, frame: locator.Frame) -> str:
        """侧边栏现在是展开还是收起：expanded / collapsed / unknown。

        以前是按"分隔线位置占窗口宽度的比例"判断，两个问题：分隔线经常检测
        不到，代码就退回一个按比例估的值（2560 宽时估成 409），于是收起状态
        被当成展开；窗口很宽时阈值也跟着水涨船高，展开的 300 像素侧边栏反而
        被判成收起。改成看那一竖条里有没有字，和窗口宽度无关。
        """

        left, right = self.SIDEBAR_BAND
        if frame.width <= right + 40:
            return "unknown"
        top = max(60, int(frame.height * 0.12))
        bottom = int(frame.height * 0.86)
        if bottom - top < 40:
            return "unknown"
        ratio = vision.ink_ratio(frame.bitmap, Rect(left, top, right, bottom), step=3)
        return "expanded" if ratio >= 0.012 else "collapsed"

    def sidebar_collapsed(self, frame: locator.Frame, composer: Optional[Rect] = None) -> bool:
        """侧边栏是不是收起了（保留给旧调用点，内部走 sidebar_state）。"""

        return self.sidebar_state(frame) == "collapsed"

    def ensure_sidebar(self, hwnd: int) -> bool:
        """保证侧边栏是展开的，展开不了就明确失败。

        收起时列表区里根本没有标题，OCR 只会从图标上读出几个乱码字符。与其
        把乱码当成对话列出来，不如先把它展开——这本来就是用户自己会做的事。

        三条路：先按 Ctrl+Shift+S（Codex 桌面版 View 菜单里的「Toggle Sidebar」），
        再按 Ctrl+B（网页版认这个），最后才去点标题栏那个方形开关。每一条都
        以"重新判断一次侧边栏状态"为准，不靠等固定时间。
        """

        frame = self._frame(hwnd)
        state = self.sidebar_state(frame)
        if state == "expanded":
            return True
        if state == "unknown":
            self.log("warning", "判断不出侧边栏是展开还是收起，按展开处理。")
            return True
        if winapi.foreground_window() != int(hwnd):
            # 点之前它必须在前台，否则这一下点到的是挡在它前面的程序。
            self.log("warning", "目标窗口不在前台，没有动侧边栏。")
            return False

        self.log("info", "侧边栏是收起的，正在展开。")
        # Ctrl+Shift+S 是 Codex 的「Toggle Sidebar」（View 菜单里写着）；
        # Ctrl+B 留给网页版。两个都试一遍再动鼠标。
        for shortcut, name in ((winapi.ctrl_shift_s, "Ctrl+Shift+S"), (winapi.ctrl_b, "Ctrl+B")):
            shortcut()
            # 展开有动画，0.6 秒未必够。给它两秒，看到就是看到了，别猜。
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                time.sleep(0.35)
                if self._sidebar_expanded(hwnd):
                    self.log("info", f"侧边栏已展开（{name}）。")
                    return True

        for point in self._sidebar_toggle_points(frame):
            winapi.click(frame.point_to_screen(point))
            time.sleep(0.5)
            if self._sidebar_expanded(hwnd):
                self.log("info", "侧边栏已展开。")
                return True
        self.log("warning", "没能展开侧边栏，请手动点一下标题栏左上角那个开关。")
        return False

    def _sidebar_expanded(self, hwnd: int) -> bool:
        try:
            return self.sidebar_state(self._frame(hwnd)) == "expanded"
        except AutomationError:
            return False

    # 标题栏里侧边栏开关的兜底坐标（150% 缩放、全宽窗口下量出来的）。
    # 正常情况下用 _sidebar_toggle_points 现场找，这里只是找不到时的最后手段。
    SIDEBAR_TOGGLE_HINT = (131, 27)

    def _sidebar_toggle_points(self, frame: locator.Frame) -> list[tuple[int, int]]:
        """标题栏里那几个图标的位置，从左往右，第三个就是侧边栏开关。

        标题栏固定是：返回、前进、侧边栏开关，然后是 File/Edit/View 菜单。
        不写死坐标：先按「Codex」那一行定位横带（窗口大小、缩放比例变了它
        也跟着走），再在这一行里数图标。万一图标没找到，退回量好的坐标。
        """

        width = min(frame.width, max(320, int(frame.width * 0.22)))
        inked: list[bool] = []
        groups: list[tuple[int, int]] = []
        band_top = 0
        band_bottom = 0
        for top, bottom in self._header_bands(frame, int(width)):
            if bottom - top < 10:
                continue
            inked = []
            for x in range(0, width):
                ratio = vision.ink_ratio(frame.bitmap, Rect(x, top, x + 1, bottom), step=1)
                inked.append(ratio >= 0.06)
            groups = []
            start = None
            for x, filled in enumerate(inked):
                if filled and start is None:
                    start = x
                elif not filled and start is not None:
                    groups.append((start, x))
                    start = None
            if start is not None:
                groups.append((start, len(inked)))
            # 太窄的是噪点，太宽的是菜单文字（File/Edit/View 一整个连成一片）。
            icons = [item for item in groups if 8 <= item[1] - item[0] <= 44]
            if len(icons) >= 3:
                band_top, band_bottom = top, bottom
                points = [
                    ((left + right) // 2, (top + bottom) // 2) for left, right in icons[:3]
                ]
                return [points[2]]
        if groups:
            points = [
                ((left + right) // 2, (band_top + band_bottom) // 2)
                for left, right in groups
                if 8 <= right - left <= 44
            ]
            return [self.SIDEBAR_TOGGLE_HINT] + points
        return [self.SIDEBAR_TOGGLE_HINT]

    def scan_conversations(self, hwnd: int, scrolls: int = 30) -> list[conversations.Conversation]:
        """把侧边栏里的对话标题读出来。

        默认往下翻 30 屏，但真正停下来的是"列表不再变化"——历史长的账号
        固定翻几屏根本到不了底，能不能扫全不该取决于这个数字。
        """

        if not self.ensure_sidebar(hwnd):
            raise AutomationError(
                "侧边栏是收起的，扫描不到对话标题。点一下标题栏左上角的侧边栏开关再试。"
            )
        return conversations.parse(self.sidebar_rows(hwnd, scrolls))

    def open_conversation(self, hwnd: int, title: str) -> None:
        """按标题切过去。找不到会按 strict_conversation 决定报错还是照发。"""

        def step(text: str, tone: str = "accent", bump: bool = False, detail: str = "") -> None:
            """工具箱里的切换也要有进度：这条链路一样要点、要等、要认。"""

            self._overlay_progress(2, 3, text, tone, detail)

        try:
            self._switch_conversation(hwnd, title, step)
            self._overlay_progress(3, 3, "已切换", "success", detail=f"当前对话 · {title}")
            time.sleep(0.8)
        except Exception as exc:
            if self._overlay_progress(2, 3, "已停止", "danger", detail=str(exc)[:46]):
                time.sleep(0.9)
            raise
        finally:
            # 工具箱里的按钮不经过 run_task，浮层得自己收，否则会一直挂在窗口上。
            self._overlay_hide()

    def new_conversation(self, hwnd: int) -> bool:
        """点侧边栏里的「New chat」。

        不走快捷键：不同版本的新建快捷键不一样，按错可能开出新窗口或弹菜单。
        点那一行至少是用户平时自己也在做的事。
        """

        frame, composer, rows = self._rows_once(hwnd)
        target = None
        for row in rows:
            stripped, _ = conversations.strip_icon(row.text)
            key = ocr.normalize(stripped)
            if key.startswith(("newchat", "新对话", "新建对话")):
                target = row
                break
        if target is None:
            self.log("warning", "侧边栏里没有找到「New chat」。")
            return False
        winapi.click(frame.point_to_screen(target.center))
        time.sleep(min(self._delay(), 1.2))
        return True

    def stop_generation(self, hwnd: int) -> bool:
        """停止生成。只在那个位置真的是暂停键时才点——空闲时它是发送箭头，
        点下去会把输入框里没写完的内容发出去。

        还要求输入框是**这一帧认出来的**，不能用上次存下的锚点：界面一变
        （比如弹出对话框、切到语音通话），锚点落的地方可能是旁边那个麦克风，
        点下去会开始录音——实测就踩过一次，界面上冒出「Voice chat couldn't
        start」。认不准就什么都不点，宁可让用户自己按停止。
        """

        frame = self._frame(hwnd)
        composer, source = self._locator().composer(frame)
        if composer is None or source not in ("识别", "缓存"):
            self.log("warning", f"没认出输入框（{source}），不敢点停止。")
            return False
        slot, kind = self._locator().send_slot(frame, composer)
        if slot is None or kind != vision.SEND_SLOT_PAUSE:
            self.log("info", "目标应用此刻没有在生成回答。")
            return False
        # 暂停键在输入框右下角。位置对不上说明认到的不是它。
        if slot.center[0] < composer.left + composer.width * 0.6:
            self.log("warning", "那个位置不像暂停键，没有去点。")
            return False
        winapi.click(frame.point_to_screen(slot.center))
        self.log("success", "已请求停止生成。")
        return True

    def clear_input(self, hwnd: int) -> bool:
        """清空输入框。空的时候不动手，免得在占位符上乱按。"""

        frame = self._frame(hwnd)
        composer, _ = self._locator().composer(frame)
        if composer is None:
            self.log("warning", "没认出输入框。")
            return False
        try:
            if self._locator().composer_is_empty(frame, composer) is True:
                return True
        except Exception:
            pass
        winapi.click(frame.point_to_screen(self._text_point(composer)))
        time.sleep(0.15)
        winapi.select_all_and_delete()
        time.sleep(0.2)
        return True

    def paste_only(self, content: str) -> RunOutcome:
        """只粘贴不发送，用于验证识别与输入框定位是否正常。"""

        started = time.monotonic()
        if not content.strip():
            raise AutomationError("提示词内容为空。")

        old_clipboard = winapi.get_clipboard_text()
        try:
            saved_cursor = None
            try:
                saved_cursor = winapi.get_cursor_pos()
            except Exception:
                saved_cursor = None

            window = self._find_window()
            hwnd = self._activate(window)
            time.sleep(min(self._delay(), 0.6))
            frame, composer, source = self._resolve_composer(hwnd)
            locator.remember_composer(self.settings, frame, composer)
            self._overlay_box(
                frame.to_screen(composer),
                "composer",
                f"输入框 · {source} · {composer.width}×{composer.height}",
            )
            self._overlay_progress(1, 3, "定位输入框", "accent", detail=f"已锁定 · {source}")

            screen_composer = frame.to_screen(composer)
            self._overlay_quiet(self.OVERLAY_SETTLE)
            baseline = self._capture(screen_composer)
            content = templates.expand(content, clipboard=old_clipboard or "")
            winapi.set_clipboard_text(content)
            self._overlay_progress(2, 3, "粘贴提示词", "accent", detail=f"{len(content)} 字")
            self._paste(hwnd, frame, composer)
            self._overlay_quiet(self.OVERLAY_PULSE)
            changed, _ = self._wait_change(screen_composer, baseline, 4.0)
            if not changed:
                raise AutomationError("粘贴后输入框没有变化，请检查识别结果。")

            elapsed = int((time.monotonic() - started) * 1000)
            self._overlay_progress(3, 3, "已粘贴", "success", detail=f"定位方式 · {source}")
            self.log("success", f"已粘贴到输入框（定位方式：{source}），未发送。")
            return RunOutcome(True, "已粘贴", elapsed)
        finally:
            if old_clipboard is not None:
                try:
                    winapi.set_clipboard_text(old_clipboard)
                except Exception:
                    pass
            if saved_cursor is not None:
                try:
                    winapi.move_cursor(saved_cursor)
                except Exception:
                    pass
            self._overlay_hide()
