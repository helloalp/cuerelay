# -*- coding: utf-8 -*-
"""提示词里的变量。

定时任务常常要带上"今天"：日报、日志总结、站会记录。日期写死等于每天
手动改一遍，所以这里支持几个占位符，在执行的那一刻才展开。

展开只做一次，发生在内容进剪贴板之前；展开后的文本就是最终发出去的文本。
认不出的占位符原样保留——写错一个词，不该让整条提示词变成空白。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_PATTERN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}", re.IGNORECASE)

# 界面上提示用的那一行。和下面的实现必须对得上，所以放在同一个文件里。
HELP = "{{date}}  {{time}}  {{datetime}}  {{weekday}}  {{clipboard}}"


def expand(text: str, now: Optional[_dt.datetime] = None, clipboard: str = "") -> str:
    """展开占位符。没有占位符时原样返回，不做任何拷贝。"""

    if "{{" not in text:
        return text
    moment = now or _dt.datetime.now()

    def replace(match: "re.Match[str]") -> str:
        key = match.group(1).lower()
        if key == "date":
            return moment.strftime("%Y-%m-%d")
        if key == "time":
            return moment.strftime("%H:%M")
        if key == "datetime":
            return moment.strftime("%Y-%m-%d %H:%M")
        if key == "weekday":
            return WEEKDAYS[moment.weekday()]
        if key == "clipboard":
            return clipboard
        return match.group(0)

    return _PATTERN.sub(replace, text)
