# -*- coding: utf-8 -*-
"""工具箱那几项在真窗口上过一遍。

只做**不留痕迹**的动作：读列表、粘贴再清空、空闲时点停止生成（应该拒绝）。
不新建对话、不关闭应用——那两项会改动用户自己的东西，逻辑由自检覆盖。

用法：
    python tools/probe_toolbox.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cuerelay_app import automation, conversations, ocr, winapi  # noqa: E402

DATA = ROOT / "cuerelay_data.json"
MARK = "CueRelay 工具箱自检，这一段稍后会被清空。"


def main() -> int:
    import json

    winapi.set_dpi_awareness()
    settings = json.loads(DATA.read_text(encoding="utf-8")).get("settings", {})
    logs: list[str] = []
    runner = automation.Runner(settings, lambda level, message: logs.append(f"[{level}] {message}"), None)
    window = runner._locate_target()
    if window is None:
        print("没有找到目标窗口")
        return 1
    hwnd = int(window["hwnd"])
    winapi.ensure_foreground(hwnd)
    time.sleep(0.6)

    results: list[tuple[str, bool, str]] = []

    # 1. 读侧边栏对话列表
    try:
        rows = runner.scan_conversations(hwnd, scrolls=0)
        titles = [item.title for item in rows]
        # 侧边栏可能停在中间某一段，不要求出现某个特定标题，只要求读得出来
        results.append(("读取对话列表", len(titles) >= 3, f"{len(titles)} 条：{titles[:3]}"))
    except Exception as exc:
        results.append(("读取对话列表", False, f"{exc.__class__.__name__}: {exc}"))

    # 2. 切到目标对话（走搜索）
    try:
        runner.open_conversation(hwnd, "Clarify Say")
        ok = runner._verify_conversation(hwnd, "Clarify Say", 5.0)
        results.append(("切换到 Clarify Say", ok, "标题栏已对上" if ok else "标题栏没变"))
    except Exception as exc:
        results.append(("切换到 Clarify Say", False, f"{exc.__class__.__name__}: {exc}"))

    # 3. 只粘贴不发送，再清空
    try:
        # paste_only 自己已经按"输入框像素变了"验过一遍，验不过会抛异常。
        runner.paste_only(MARK)
        time.sleep(0.6)
        frame, composer, source = runner._resolve_composer(hwnd)
        # 关键词核对要限定在输入框那一条横带里：输入框在窗口底部，
        # 不传 band 的话默认只看上半屏，永远对不上。这一步只当参考——
        # OCR 读小字偶尔会漏，漏了不代表没粘进去。
        seen = runner._input_has_title(hwnd, MARK, composer)
        cleared = runner.clear_input(hwnd)
        time.sleep(0.5)
        gone = runner._composer_empty(hwnd, composer)
        results.append(
            ("粘贴后清空", cleared and gone is not False,
             f"粘贴=成功（OCR{'看到' if seen else '没看到'}关键词） 清空={'成功' if cleared else '失败'} 空={gone} ({source})"),
        )
    except Exception as exc:
        results.append(("粘贴后清空", False, f"{exc.__class__.__name__}: {exc}"))

    # 4. 空闲时点"停止生成"——必须拒绝，否则会把没写完的内容发出去
    try:
        stopped = runner.stop_generation(hwnd)
        results.append(("空闲时停止生成", stopped is False, "拒绝了（正确）" if stopped is False else "点了，危险"))
    except Exception as exc:
        results.append(("空闲时停止生成", False, f"{exc.__class__.__name__}: {exc}"))

    # 5. 片段与变量展开（纯本地，不碰窗口）
    try:
        from cuerelay_app import templates

        text = templates.expand("日期 {{date}} 名字 {{clipboard}}", clipboard="CueRelay")
        ok = "{{" not in text and "CueRelay" in text
        results.append(("片段变量展开", ok, text[:40]))
    except Exception as exc:
        results.append(("片段变量展开", False, f"{exc.__class__.__name__}: {exc}"))

    print(f"窗口 {hwnd}：{window.get('title')}")
    print(f"{'项目':<22}{'结果':<6}说明")
    failures = 0
    for name, ok, note in results:
        failures += 0 if ok else 1
        print(f"{name:<22}{'通过' if ok else '失败':<6}{note}")
    print("")
    for line in logs[-6:]:
        print(line)
    print(f"\n共 {len(results)} 项，失败 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
