# -*- coding: utf-8 -*-
"""把侧边栏读到的文字行整理成「对话列表」。

OCR 只会给出一行行文字和坐标，而侧边栏里混着菜单栏、分区标题、项目名和按钮。
这里用位置和已知文案把它们分开：

- 菜单栏在最上面（File / Edit / View / Help），y 很小；
- 分区标题（Pinned / Projects / Chats）是一行纯文字，靠已知文案识别；
- 项目名带一个文件夹图标，OCR 常把它读成一个字母前缀；
- 项目下面的对话标题会缩进。

判断不可能百分之百准，所以这份列表只用来「导航」：点错了最多是展开或收起
一个项目，不会发出任何消息。真正的跳转仍由 `Runner._switch_conversation`
按标题严格匹配并校验标题栏，走的是同一条已经验证过的路径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from . import ocr

# 分区标题。同一段文字既可能是分区名，也可能是某个对话的标题，
# 所以只在「它站在分区名该在的位置」时才算分区。
SECTION_LABELS: dict[str, tuple[str, ...]] = {
    "pinned": ("pinned", "置顶"),
    "projects": ("projects", "项目"),
    "chats": ("chats", "recents", "recent", "history", "最近", "历史"),
}

# 侧边栏里的固定文案：按钮、标题、菜单。它们不是对话。
CHROME_LABELS = frozenset(
    {
        "file",
        "edit",
        "view",
        "help",
        "window",
        "codex",
        "new chat",
        "new",
        "search",
        "search chats",
        "settings",
        "archived",
        "show more",
        "show less",
        "see more",
        "see all",
        "新对话",
        "新建对话",
        "搜索",
        "设置",
        "显示更多",
    }
)

MENU_LABELS = frozenset({"file", "edit", "view", "help", "window"})

# 比对一律走 ocr.normalize（去空白与标点、转小写），所以集合也要按同样规则归一化，
# 否则 "New chat" 和 "newchat" 永远匹配不上。
_CHROME_KEYS = frozenset(ocr.normalize(item) for item in CHROME_LABELS)
_MENU_KEYS = frozenset(ocr.normalize(item) for item in MENU_LABELS)

# 菜单栏的高度上限（客户区坐标）。菜单文字一定在这条线以上。
MENU_BAND = 60

# 图标被 OCR 读成字母时，通常是一个大写字母加一个空格。
_ICON_PREFIX = re.compile(r"^([A-Z])[ \u00a0]+(\S.*)$")


@dataclass(frozen=True)
class Conversation:
    """侧边栏里的一条记录。title 是去掉图标前缀后用来显示的文字。"""

    title: str
    raw: str
    section: str
    top: int

    @property
    def key(self) -> str:
        return ocr.normalize(self.raw)


def section_of(text: str) -> Optional[str]:
    folded = ocr.normalize(text)
    if not folded:
        return None
    for key, labels in SECTION_LABELS.items():
        for label in labels:
            if folded == ocr.normalize(label):
                return key
    return None


def strip_icon(text: str) -> tuple[str, bool]:
    """去掉 OCR 读出来的图标字母。

    `B helloalp_sms` 里的 B 是文件夹图标。只有「一个大写字母 + 空格 + 后面还有
    至少四个字符」才当成图标——单个字母开头的正常标题（比如 `I built a thing`）
    也会被误伤，但这条列表只用于显示和跳转，跳转按标题严格匹配，
    误伤一个显示文字不会把消息发错地方。
    """

    match = _ICON_PREFIX.match(text.strip())
    if match:
        rest = match.group(2).strip()
        if len(rest) >= 4:
            return rest, True
    return text.strip(), False


def is_chrome(text: str) -> bool:
    folded = ocr.normalize(text)
    if not folded:
        return True
    if folded in _CHROME_KEYS:
        return True
    # `C New chat`：图标字母 + 固定文案，整体也是按钮。
    stripped, had_icon = strip_icon(text)
    return had_icon and ocr.normalize(stripped) in _CHROME_KEYS


def parse(rows: Iterable[ocr.OcrLine], top_skip: int = MENU_BAND) -> list[Conversation]:
    """把侧边栏的行整理成对话列表，按屏幕位置从上到下。"""

    items = sorted(rows, key=lambda row: (row.top, row.left))
    found: list[Conversation] = []
    section = "chats"
    seen: set[str] = set()

    for row in items:
        text = row.text.strip()
        if not text:
            continue
        # 菜单栏只出现在最上面，用位置再确认一次，避免把标题恰好叫 "View" 的对话删掉。
        if row.top < top_skip and ocr.normalize(text) in _MENU_KEYS:
            continue
        if row.width < 24:
            continue

        label = section_of(text)
        if label is not None and not strip_icon(text)[1]:
            section = label
            continue
        if is_chrome(text):
            continue

        title, _had_icon = strip_icon(text)
        if not title:
            continue
        key = ocr.normalize(text)
        if key in seen:
            continue
        seen.add(key)
        found.append(Conversation(title=title, raw=text, section=section, top=row.top))

    return found


def titles(conversations: Iterable[Conversation]) -> list[str]:
    return [item.title for item in conversations]
