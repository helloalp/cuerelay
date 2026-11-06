# -*- coding: utf-8 -*-
"""Codex 本地对话库：直接从磁盘读，不去看界面。

以前「扫描对话」是截屏 + OCR 读侧边栏，要窗口在前台、侧边栏展开、列表滚六屏，
读到的还只是当前这一屏里显示出来的标题。慢、脆、而且永远读不全。

Codex 把对话都存在自己目录下的 SQLite 里（`state_*.sqlite` 的 `threads` 表，
正文在 `thread_history_*.sqlite`）。那才是唯一的事实来源：标题、目录、项目、
时间、模型、token、归档状态、是不是子代理，全都在里面，而且一次读完 966 条
只要几十毫秒。

这个模块只做只读解析，不写任何东西，也不依赖 Tk：

- 库开着（WAL 里有未合并的页）时，先试只读连接；失败就把库文件复制到临时目录
  再读——Codex 正在写的时候不能因为读不了就当"没有对话"。
- 表结构会随版本变，所以列名是"有就用、没有就跳过"，不是写死的 SELECT。
- 没有 state 库（纯 CLI 用户）时回退去解析 `sessions/**/rollout-*.jsonl`。
- 项目归属按 `cwd` 匹配 `project_roots`，取最长前缀；匹配不上就是「无项目」。
  这一版 Codex 的 `threads.project_id` 全是 NULL，不能指望它。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# 子代理对话在总数里占大头（本机 966 条里有 577 条），默认不显示。
KIND_USER = "user"
KIND_AGENT = "agent"
KIND_SUBAGENT = "subagent"
KIND_FORK = "fork"

# 读 rollout 文件补标题时最多翻多少个文件。261 条空标题里绝大多数是子代理，
# 补它们没有意义，所以只补用户对话，并且封顶。
ROLLOUT_FILL_LIMIT = 40
ROLLOUT_READ_LINES = 400

_WS = re.compile(r"\s+")
_MD_PREFIX = re.compile(r"^[#>\-*\s]+")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def codex_home() -> Path:
    """Codex 的数据目录。优先 CODEX_HOME，其次 ~/.codex。"""

    env = str(os.environ.get("CODEX_HOME") or "").strip().strip('"')
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".codex"


def tidy_path(value: Any) -> str:
    """把 Windows 上见过的几种路径写法收敛成一种。

    Codex 存的是 `\\\\?\\E:\\aitools\\helloalp_web`（扩展长度前缀），偶尔带引号、
    尾部反斜杠或正斜杠。它们指的是同一个目录，不统一就永远匹配不上项目根。
    """

    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if text.startswith("\\\\?\\"):
        text = text[4:]
    elif text.startswith("//?/"):
        text = text[4:]
    text = text.replace("/", "\\")
    if len(text) > 2:
        text = text.rstrip("\\")
    return text


def _fold(value: Any) -> str:
    return tidy_path(value).lower()


def is_under(path: Any, root: Any) -> bool:
    """path 是否在 root 里面（大小写不敏感，按路径段比较）。"""

    child = _fold(path)
    parent = _fold(root)
    if not child or not parent:
        return False
    if child == parent:
        return True
    return child.startswith(parent + "\\")


_WORKTREE = re.compile(r"\\\.codex\\worktrees\\[^\\]+\\([^\\]+)$", re.IGNORECASE)


def project_of(cwd: Any, roots: Sequence[tuple[str, str]]) -> str:
    """按最长前缀匹配项目。roots 需要按路径长度从长到短排好。

    兜底还有一条：Codex 给子代理开的工作副本在
    `~/.codex/worktrees/<hash>/<项目名>`，它不在任何项目根下面，但目录名就是
    项目名。没有这条，几百条代理对话会全部掉进「无项目」。
    """

    for root, name in roots:
        if is_under(cwd, root):
            return name
    match = _WORKTREE.search(tidy_path(cwd))
    return match.group(1) if match else ""


def _one_line(text: Any, limit: int = 200) -> str:
    """把多行提示词压成一行：列表里只需要一个能认出来的开头。"""

    raw = str(text or "")
    if not raw.strip():
        return ""
    for line in raw.splitlines():
        cleaned = _MD_PREFIX.sub("", line).strip()
        if cleaned:
            raw = cleaned
            break
    collapsed = _WS.sub(" ", raw).strip()
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadRecord:
    """一条对话。字段全部来自本地库，没有一条是猜的。"""

    id: str
    title: str
    full_title: str
    subtitle: str
    project: str
    cwd: str
    created: float
    updated: float
    tokens: int = 0
    turns: int = 0
    model: str = ""
    archived: bool = False
    pinned: bool = False
    kind: str = KIND_USER
    agent: str = ""
    parent: str = ""
    origin: str = ""

    # -- 派生 ----------------------------------------------------------

    @property
    def is_subagent(self) -> bool:
        return self.kind == KIND_SUBAGENT

    @property
    def is_loose(self) -> bool:
        """没有归到任何项目里的对话。"""

        return not self.project

    @property
    def search_text(self) -> str:
        return " ".join(
            part
            for part in (
                self.title,
                self.subtitle,
                self.project,
                self.cwd,
                self.id,
                self.model,
                self.agent,
            )
            if part
        ).lower()

    def score(self, tokens: Sequence[str]) -> int:
        """每个词都要命中，命中位置越"靠前"分越高。

        标题里命中比目录里命中重要得多：搜 `helloalp` 时，用户想先看到
        标题里带 helloalp 的对话，而不是所有工作目录在 helloalp_web 下的。
        """

        if not tokens:
            return 0
        title = self.title.lower()
        subtitle = self.subtitle.lower()
        project = self.project.lower()
        cwd = self.cwd.lower()
        ident = self.id.lower()
        total = 0
        for token in tokens:
            if token in title:
                total += 8 if title.startswith(token) else 6
            elif token in subtitle:
                total += 3
            elif token in project:
                total += 3
            elif token in ident:
                total += 4
            elif token in cwd:
                total += 1
            else:
                return 0
        return total

    def matches(self, tokens: Sequence[str]) -> bool:
        return self.score(tokens) > 0 if tokens else True


@dataclass
class Library:
    """一次扫描的结果。"""

    threads: list[ThreadRecord] = field(default_factory=list)
    home: str = ""
    source: str = ""
    scanned_at: float = 0.0
    elapsed_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    signature: tuple = ()

    # -- 统计 ----------------------------------------------------------

    def counts(self) -> dict[str, int]:
        total = len(self.threads)
        archived = sum(1 for item in self.threads if item.archived)
        subagents = sum(1 for item in self.threads if item.is_subagent)
        loose = sum(1 for item in self.threads if item.is_loose and not item.archived)
        projects = {item.project for item in self.threads if item.project and not item.archived}
        return {
            "total": total,
            "active": total - archived,
            "archived": archived,
            "subagents": subagents,
            "loose": loose,
            "projects": len(projects),
        }

    def tokens(self) -> int:
        return sum(item.tokens for item in self.threads if item.tokens > 0)

    def project_counts(self, include_subagents: bool = False, archived: bool = False) -> list[tuple[str, int]]:
        """[(项目名, 条数)]，按最近活跃排序。空项目名不在里面。"""

        latest: dict[str, float] = {}
        counts: dict[str, int] = {}
        for item in self.threads:
            if item.archived != archived or not item.project:
                continue
            if item.is_subagent and not include_subagents:
                continue
            counts[item.project] = counts.get(item.project, 0) + 1
            latest[item.project] = max(latest.get(item.project, 0.0), item.updated)
        return sorted(counts.items(), key=lambda pair: latest.get(pair[0], 0.0), reverse=True)


def search(threads: Iterable[ThreadRecord], query: str) -> list[ThreadRecord]:
    """按词搜，每个词都要命中；结果按相关度、再按时间排。"""

    tokens = [token.lower() for token in str(query or "").split() if token]
    items = list(threads)
    if not tokens:
        return items
    scored = [(item.score(tokens), item) for item in items]
    hits = [(score, item) for score, item in scored if score > 0]
    hits.sort(key=lambda pair: (-pair[0], -pair[1].updated))
    return [item for _score, item in hits]


# ---------------------------------------------------------------------------
# 读库
# ---------------------------------------------------------------------------


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + str(path).replace("\\", "/").replace(" ", "%20") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=4.0)
    con.row_factory = sqlite3.Row
    return con


def _copy_for_read(path: Path) -> tuple[sqlite3.Connection, str]:
    """库被独占或者 WAL 需要恢复时，复制一份到临时目录再读。

    先复制 -wal 再复制主库：反过来的话，主库复制完到 wal 复制完之间新写入的
    页会指向一个不存在的位置。顺序反了最多读到稍旧一点的数据，不会读到坏页。
    """

    folder = tempfile.mkdtemp(prefix="cuerelay-codex-")
    target = Path(folder) / path.name
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            try:
                shutil.copy2(side, target.with_name(target.name + suffix))
            except OSError:
                pass
    shutil.copy2(path, target)
    con = sqlite3.connect(str(target), timeout=4.0)
    con.row_factory = sqlite3.Row
    return con, folder


def _open_any(path: Path) -> tuple[sqlite3.Connection, str]:
    try:
        con = _open_readonly(path)
        con.execute("select count(*) from sqlite_master").fetchone()
        return con, ""
    except sqlite3.Error:
        return _copy_for_read(path)


def _close(con: sqlite3.Connection, folder: str) -> None:
    try:
        con.close()
    except sqlite3.Error:
        pass
    if folder:
        shutil.rmtree(folder, ignore_errors=True)


def _latest(home: Path, pattern: str) -> Optional[Path]:
    """`state_5.sqlite` 比 `state_4.sqlite` 新，按数字后缀取最大的。"""

    best: Optional[Path] = None
    best_version = -1
    for item in home.glob(pattern):
        if not item.is_file():
            continue
        match = re.search(r"_(\d+)\.sqlite$", item.name)
        version = int(match.group(1)) if match else 0
        if version > best_version:
            best, best_version = item, version
    return best


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f'pragma table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _column(available: set[str], *names: str, alias: str) -> str:
    for name in names:
        if name in available:
            return f'"{name}" as "{alias}"'
    return f'NULL as "{alias}"'


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1e11:  # 毫秒
        number /= 1000.0
    return number


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _kind_of(thread_source: str, source: str) -> tuple[str, str, str]:
    """(类别, 代理名, 父对话 id)。"""

    raw = f"{thread_source} {source}".lower()
    agent = ""
    parent = ""
    if source.startswith("{"):
        try:
            payload = json.loads(source)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            spawn = (payload.get("subagent") or {})
            if isinstance(spawn, dict):
                agent = _text(spawn.get("thread_spawn", {}).get("agent_nickname") if isinstance(spawn.get("thread_spawn"), dict) else spawn.get("agent_nickname"))
                detail = spawn.get("thread_spawn")
                if isinstance(detail, dict):
                    parent = _text(detail.get("parent_thread_id"))
                    agent = _text(detail.get("agent_nickname")) or agent
    if "subagent" in raw or parent:
        return KIND_SUBAGENT, agent, parent
    if "forked" in raw:
        return KIND_FORK, agent, parent
    if "agent_created" in raw:
        return KIND_AGENT, agent, parent
    return KIND_USER, agent, parent


def _project_roots(con: sqlite3.Connection, tables: set[str]) -> list[tuple[str, str]]:
    """[(根路径, 项目名)]，按路径长度从长到短，保证取到最具体的那个项目。"""

    if "project_roots" not in tables:
        return []
    names: dict[str, str] = {}
    if "projects" in tables:
        columns = _columns(con, "projects")
        name_col = _column(columns, "name", alias="name")
        try:
            for row in con.execute(f"select id, {name_col} from projects"):
                names[str(row["id"])] = _text(row["name"])
        except sqlite3.Error:
            names = {}
    roots: list[tuple[str, str]] = []
    try:
        rows = con.execute("select project_id, path from project_roots").fetchall()
    except sqlite3.Error:
        return []
    for row in rows:
        path = tidy_path(row["path"])
        if not path:
            continue
        project_id = str(row["project_id"])
        name = names.get(project_id) or Path(path).name
        roots.append((path, name))
    roots.sort(key=lambda pair: len(pair[0]), reverse=True)
    return roots


def _read_state(path: Path) -> tuple[list[dict[str, Any]], list[tuple[str, str]], dict[str, str]]:
    con, folder = _open_any(path)
    try:
        tables = {str(row[0]) for row in con.execute("select name from sqlite_master where type='table'")}
        if "threads" not in tables:
            raise RuntimeError("state 库里没有 threads 表")
        columns = _columns(con, "threads")
        project_names: dict[str, str] = {}
        if "projects" in tables:
            try:
                for row in con.execute("select id, name from projects"):
                    project_names[str(row["id"])] = _text(row["name"])
            except sqlite3.Error:
                pass
        sql = (
            "select "
            + ", ".join(
                [
                    _column(columns, "id", alias="id"),
                    _column(columns, "title", alias="title"),
                    _column(columns, "name", alias="name"),
                    _column(columns, "preview", alias="preview"),
                    _column(columns, "first_user_message", alias="first_user"),
                    _column(columns, "cwd", alias="cwd"),
                    _column(columns, "rollout_path", alias="rollout"),
                    _column(columns, "project_id", alias="project_id"),
                    _column(columns, "created_at", "created_at_ms", alias="created"),
                    _column(columns, "updated_at", "updated_at_ms", alias="updated"),
                    _column(columns, "tokens_used", alias="tokens"),
                    _column(columns, "model", alias="model"),
                    _column(columns, "archived", alias="archived"),
                    _column(columns, "is_pinned", alias="pinned"),
                    _column(columns, "source", alias="source"),
                    _column(columns, "thread_source", alias="thread_source"),
                    _column(columns, "originator", alias="originator"),
                    _column(columns, "agent_nickname", alias="agent_nickname"),
                    _column(columns, "created_at_ms", alias="created_ms"),
                    _column(columns, "updated_at_ms", alias="updated_ms"),
                ]
            )
            + " from threads"
        )
        rows = [dict(row) for row in con.execute(sql)]
        roots = _project_roots(con, tables)
        return rows, roots, project_names
    finally:
        _close(con, folder)


def _turn_counts(path: Optional[Path]) -> dict[str, int]:
    """每个对话有多少轮。读不到就返回空——轮次是锦上添花，不是必需的。"""

    if path is None or not path.exists():
        return {}
    con = None
    folder = ""
    try:
        con, folder = _open_any(path)
        return {
            str(row[0]): int(row[1])
            for row in con.execute("select thread_id, count(*) from thread_turns group by thread_id")
        }
    except sqlite3.Error:
        return {}
    finally:
        if con is not None:
            _close(con, folder)


def _rollout_title(path: str) -> tuple[str, str, str]:
    """从 rollout 文件里补标题。返回 (标题, 首条用户消息, 目录)。"""

    if not path:
        return "", "", ""
    file = Path(tidy_path(path))
    if not file.exists():
        return "", "", ""
    first_user = ""
    cwd = ""
    try:
        with file.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index > ROLLOUT_READ_LINES:
                    break
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                kind = payload.get("type")
                body = payload.get("payload") or {}
                if kind == "session_meta":
                    cwd = _text(body.get("cwd"))
                    continue
                if kind != "response_item":
                    continue
                if body.get("type") != "message" or body.get("role") != "user":
                    continue
                for part in body.get("content") or []:
                    text = _text(part.get("text") if isinstance(part, dict) else "")
                    if text:
                        first_user = text
                        break
                if first_user:
                    break
    except OSError:
        return "", "", ""
    return _one_line(first_user), first_user, cwd


def _read_sessions(home: Path, roots: list[tuple[str, str]]) -> list[ThreadRecord]:
    """没有 state 库时的回退：直接解析 sessions 下的 rollout 文件。"""

    records: list[ThreadRecord] = []
    files: list[Path] = []
    for folder in ("sessions", "archived_sessions"):
        base = home / folder
        if base.exists():
            files.extend(base.rglob("*.jsonl"))
    files.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    for file in files:
        meta: dict[str, Any] = {}
        first_user = ""
        try:
            with file.open("r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    if index > ROLLOUT_READ_LINES:
                        break
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    body = payload.get("payload") or {}
                    if payload.get("type") == "session_meta":
                        meta = body
                        continue
                    if payload.get("type") != "response_item":
                        continue
                    if body.get("type") == "message" and body.get("role") == "user":
                        for part in body.get("content") or []:
                            text = _text(part.get("text") if isinstance(part, dict) else "")
                            if text:
                                first_user = text
                                break
                    if first_user:
                        break
        except OSError:
            continue
        ident = _text(meta.get("id") or meta.get("session_id")) or file.stem
        cwd = _text(meta.get("cwd"))
        stamp = 0.0
        raw_time = _text(meta.get("timestamp"))
        if raw_time:
            try:
                stamp = time.mktime(time.strptime(raw_time[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                stamp = 0.0
        if not stamp:
            try:
                stamp = file.stat().st_mtime
            except OSError:
                stamp = 0.0
        kind, agent, parent = _kind_of(_text(meta.get("thread_source")), _text(meta.get("source")))
        title = _one_line(first_user) or file.stem
        records.append(
            ThreadRecord(
                id=ident,
                title=title,
                full_title=first_user or title,
                subtitle="",
                project=project_of(cwd, roots),
                cwd=tidy_path(cwd),
                created=stamp,
                updated=stamp,
                kind=kind,
                agent=agent,
                parent=parent,
                origin=_text(meta.get("originator")),
            )
        )
    return records


def _record(row: dict[str, Any], roots: list[tuple[str, str]], project_names: dict[str, str]) -> ThreadRecord:
    ident = _text(row.get("id"))
    cwd = tidy_path(row.get("cwd"))
    project = ""
    project_id = _text(row.get("project_id"))
    if project_id and project_names.get(project_id):
        project = project_names[project_id]
    if not project:
        project = project_of(cwd, roots)

    created = _num(row.get("created")) or _num(row.get("created_ms"))
    updated = _num(row.get("updated")) or _num(row.get("updated_ms")) or created

    first_user = _text(row.get("first_user"))
    preview = _text(row.get("preview"))
    name = _text(row.get("name"))
    raw_title = _text(row.get("title"))
    kind, agent, parent = _kind_of(_text(row.get("thread_source")), _text(row.get("source")))

    display = _one_line(name) or _one_line(raw_title) or _one_line(first_user) or _one_line(preview)
    # 详情里要展示的是"用户当初到底说了什么"，不是 Codex 给的那句短标题。
    # 这两者混在一起的时候，详情面板会把标题再念一遍，等于什么也没说。
    full = first_user or raw_title or preview or name
    subtitle = ""
    for candidate in (first_user, preview, raw_title):
        line = _one_line(candidate, 220)
        if line and line != display:
            subtitle = line
            break

    return ThreadRecord(
        id=ident,
        title=display,
        full_title=full or display,
        subtitle=subtitle,
        project=project,
        cwd=cwd,
        created=created,
        updated=updated,
        tokens=_int(row.get("tokens")),
        model=_text(row.get("model")),
        archived=bool(_int(row.get("archived"))),
        pinned=bool(_int(row.get("pinned"))),
        kind=kind,
        agent=agent or _text(row.get("agent_nickname")),
        parent=parent,
        origin=_text(row.get("originator")),
    )


def _fill_titles(records: list[ThreadRecord], raw: dict[str, dict[str, Any]], turns: dict[str, int]) -> list[ThreadRecord]:
    """给标题空着的用户对话补一个标题。

    261 条空标题里大部分是子代理，补了也没人看；真正需要补的是那些
    `name` 和 `title` 都空着、但确实是用户开的对话——它们只能从 rollout
    文件的第一条用户消息里捞。封顶 40 个文件，不让一次扫描变成 IO 风暴。
    """

    filled = 0
    output: list[ThreadRecord] = []
    for item in records:
        if item.title:
            output.append(_with_turns(item, turns))
            continue
        if filled >= ROLLOUT_FILL_LIMIT:
            output.append(_with_turns(item, turns))
            continue
        row = raw.get(item.id) or {}
        title, first_user, cwd = _rollout_title(_text(row.get("rollout")))
        filled += 1
        if not title:
            output.append(_with_turns(item, turns))
            continue
        output.append(
            _with_turns(
                replace(
                    item,
                    title=title,
                    full_title=first_user or title,
                    project=item.project or project_of(cwd, []),
                    cwd=item.cwd or tidy_path(cwd),
                ),
                turns,
            )
        )
    return output


def _with_turns(item: ThreadRecord, turns: dict[str, int]) -> ThreadRecord:
    count = turns.get(item.id, 0)
    return item if count == item.turns else replace(item, turns=count)


def _ensure_title(item: ThreadRecord) -> ThreadRecord:
    """空标题给一个能读的占位，而不是让列表里出现一行空白。"""

    if item.title:
        return item
    if item.kind == KIND_SUBAGENT:
        return replace(item, title=f"{item.agent} · 子代理" if item.agent else "子代理对话")
    if item.kind == KIND_AGENT:
        return replace(item, title="代理创建的对话")
    return replace(item, title="未命名对话")


def _sort(records: list[ThreadRecord]) -> list[ThreadRecord]:
    """置顶在前，其余按最近活跃。时间相同的按标题，保证顺序稳定。"""

    return sorted(records, key=lambda item: (not item.pinned, -item.updated, item.title.lower()))


def scan(home: Optional[Path] = None) -> Library:
    """扫一次。纯文件读取，几十毫秒，可以直接在界面线程外跑。"""

    started = time.perf_counter()
    base = Path(home) if home is not None else codex_home()
    warnings: list[str] = []

    if not base.exists():
        return Library(
            home=str(base),
            source="",
            scanned_at=time.time(),
            elapsed_ms=0,
            warnings=[f"没有找到 Codex 数据目录：{base}"],
        )

    state = _latest(base, "state_*.sqlite")
    history = _latest(base, "thread_history_*.sqlite")
    turns = _turn_counts(history)

    if state is not None:
        try:
            raw_rows, roots, project_names = _read_state(state)
        except Exception as exc:  # noqa: BLE001 - 任何读库失败都要能回退
            warnings.append(f"{state.name} 读不出来（{exc}），改用 sessions 目录。")
            raw_rows, roots, project_names = [], [], {}
        if raw_rows:
            records = [_record(row, roots, project_names) for row in raw_rows]
            raw = {_text(row.get("id")): row for row in raw_rows}
            records = _fill_titles(records, raw, turns)
            records = [_ensure_title(item) for item in records]
            library = Library(
                threads=_sort(records),
                home=str(base),
                source=state.name,
                scanned_at=time.time(),
                warnings=warnings,
            )
            library.elapsed_ms = int((time.perf_counter() - started) * 1000)
            library.signature = _signature(state, len(records))
            return library
        if not warnings:
            warnings.append(f"{state.name} 里没有对话记录。")

    records = [_ensure_title(item) for item in _read_sessions(base, [])]
    if not records:
        warnings.append("没有找到任何 Codex 对话记录。")
    library = Library(
        threads=_sort(records),
        home=str(base),
        source="sessions",
        scanned_at=time.time(),
        warnings=warnings,
    )
    library.elapsed_ms = int((time.perf_counter() - started) * 1000)
    library.signature = ("sessions", len(records))
    return library


def is_stale(library: Optional[Library]) -> bool:
    """库文件变过没有。只 stat 一次，用来决定"进页面要不要重读"。

    Codex 一边聊一边写库，所以"上次读过"不等于"现在还是最新的"。每次都重读
    也不过几十毫秒，但能用一次 stat 免掉的时候没必要重读。
    """

    if library is None or not library.signature:
        return True
    name = str(library.signature[0])
    if not name or name == "sessions":
        return False
    path = Path(library.home) / name
    try:
        stat = path.stat()
    except OSError:
        return True
    return (stat.st_mtime_ns, stat.st_size) != (library.signature[1], library.signature[2])


def _signature(path: Path, count: int) -> tuple:
    try:
        stat = path.stat()
        return (path.name, stat.st_mtime_ns, stat.st_size, count)
    except OSError:
        return (path.name, 0, 0, count)
