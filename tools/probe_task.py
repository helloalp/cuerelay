# -*- coding: utf-8 -*-
"""端到端：真的建一条定时消息，等它自己到点发出去，再回读画面确认。

这是唯一能证明"核心链路可用"的测法。单独测切换、测粘贴都只覆盖一段——
定时消息把识别、切对话、粘贴、发送、回读、执行记录串成一条，中间任何一环
坏了，这里就发不出去或者发错地方。

用独立的临时数据文件，不碰用户自己的任务和历史。

用法：
    python tools/probe_task.py                 # 60 秒后触发
    python tools/probe_task.py --delay 20      # 20 秒后触发
    python tools/probe_task.py --keep          # 失败也保留临时数据文件
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cuerelay_app import automation, config, ocr, scheduler, store as store_module, winapi  # noqa: E402

DATA = ROOT / "cuerelay_data.json"
TEMP = ROOT / ".probe_task.json"
TARGET = "Clarify Say"


def main() -> int:
    winapi.set_dpi_awareness()
    delay = 60
    if "--delay" in sys.argv:
        delay = int(sys.argv[sys.argv.index("--delay") + 1])
    keep = "--keep" in sys.argv

    raw = json.loads(DATA.read_text(encoding="utf-8"))
    TEMP.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    data = store_module.TaskStore(TEMP)
    data.load()
    # 探针自己的任务和历史，别混进用户的记录里。
    data.data["tasks"] = []
    data.data["history"] = []
    data._save_locked()

    stamp = _dt.datetime.now().strftime("%H%M%S")
    content = f"CueRelay 定时自检 {stamp}：看到这条说明定时消息链路是通的。"
    run_at = config.now_local() + _dt.timedelta(seconds=delay)
    task = data.add(
        {
            "name": f"定时自检 {stamp}",
            "kind": "message",
            "content": content,
            "conversation_title": TARGET,
            "schedule": "once",
            "next_run": config.iso_datetime(run_at),
            "enabled": True,
        }
    )
    print(f"任务 {task['id']}：{run_at:%H:%M:%S} 触发，发到「{TARGET}」")
    print(f"内容：{content}")

    events: list[str] = []

    def emit(kind: str, payload) -> None:
        if kind == "log":
            level, message = payload
            events.append(f"[{level}] {message}")
            print(f"  {level:<8}{message}")
        elif kind == "prepare":
            # 界面上这一步是"把浮层让开"，探针没有浮层，直接放行。
            try:
                payload.set()
            except Exception:
                pass

    engine = scheduler.Scheduler(data, emit, exclude_hwnd=0)
    engine.start()

    deadline = time.time() + delay + 180
    while time.time() < deadline:
        time.sleep(1.0)
        current = data.get(task["id"]) or {}
        # 跑完之后状态是 waiting（成功）或 failed；`last_run` 有值说明真的跑过
        # 一轮，不是"还没到点"。
        if current.get("last_run") and current.get("status") in ("waiting", "failed", "expired"):
            if not engine.is_busy():
                break
    engine.stop()

    final = data.get(task["id"]) or {}
    history = [item for item in data.data.get("history", []) if item.get("task_id") == task["id"]]
    print("")
    print(f"任务状态：{final.get('status')}  执行 {final.get('run_count')} 次  "
          f"耗时 {final.get('last_duration_ms')} ms  错误：{final.get('last_error') or '无'}")
    for item in history:
        print(f"执行记录：{item.get('at')} ok={item.get('ok')} {item.get('duration_ms')}ms {item.get('message') or ''}")

    ok = final.get("status") == "waiting" and bool(history) and all(item.get("ok") for item in history)

    # 回读画面：消息真的出现在对话里了吗
    print("")
    runner = automation.Runner(data.settings(), lambda level, message: None, None)
    window = runner._locate_target()
    if window is None:
        print("回读失败：找不到目标窗口")
        return 1
    winapi.ensure_foreground(int(window["hwnd"]))
    time.sleep(0.6)
    # 新消息在对话最下面。窗口可能停在上次看的位置，先滚到底再看——
    # 滚动条在中间时，"画面里没有"不代表"没发出去"。
    frame = runner._frame(int(window["hwnd"]))
    winapi.scroll(frame.point_to_screen((frame.width // 2, int(frame.height * 0.55))), -12)
    time.sleep(0.8)
    frame = runner._frame(int(window["hwnd"]))
    # 整帧识别会漏字：3840 宽的画面里，气泡里那行小字在整帧 OCR 里读不出来，
    # 同一块区域单独裁出来再识别就很清楚。核对只看对话那一条竖带。
    left = int(frame.width * 0.34)
    right = int(frame.width * 0.82)
    lines = ocr.recognize(frame.bitmap.crop(left, 60, right, int(frame.height * 0.9)))
    flat = ocr.normalize(" ".join(line.text for line in lines))
    # 只看能读得到的部分：OCR 读拉丁字母稳，中文经常整段丢掉，
    # 所以按"时间戳数字"和 "cuerelay" 分别找，不要求整句对上。
    # 只看能读得到的部分：中文经常整段丢掉，数字偶尔也会读错一位，
    # 所以认 "cuerelay" 这个词就够了——这个对话里只有自检消息会带它。
    seen = "cuerelay" in flat or ocr.normalize(stamp) in flat
    print(f"画面里能看到这条消息：{'是' if seen else '否'}")
    if not seen:
        for line in lines[-14:]:
            print(f"  {line.top:5d} {line.text[:60]}")

    if not keep:
        TEMP.unlink(missing_ok=True)
    print("")
    # 判据放在"执行记录"上：那是程序自己的结论（切换核对 + 粘贴核对 + 发送
    # 核对都过了才会写 ok）。画面回读只当旁证——气泡是蓝底白字，OCR 时灵时不灵，
    # 拿它当判据会把成功的发送判成失败。
    print(f"结论：{'通过' if ok else '未通过'}（画面旁证：{'看得到' if seen else '没读到'}）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
