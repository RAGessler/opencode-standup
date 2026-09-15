#!/usr/bin/env python3
"""
opencode-standup

A daily command-center for opencode sessions across all your repos:
  - Standup tab: sessions grouped by project for a chosen time scope
    (Today / Last workday / This week / All time), with cost & token
    rollups (including subagent spend).
  - Todo tab: outstanding (non-completed) todo items across all sessions,
    so you can see what's left to pick back up without hunting repo by repo.

Actions (see the in-app help with `?`):
  e  rename the selected task/session (via opencode's REST API)
  x  archive the selected task/session (via opencode's REST API)
  A  toggle showing archived sessions
  u  unarchive the selected (archived) session -- direct DB write, since
     opencode's API does not currently expose an "unarchive" operation
  o  open/resume the selected session in the real opencode TUI
  1-4  switch time scope (Standup tab)
  [ ]  switch tabs
  r  refresh
  q  quit
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.tree import TreeNode

DEFAULT_DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")


# --------------------------------------------------------------------------
# Time scopes
# --------------------------------------------------------------------------


class ScopeKind(Enum):
    TODAY = "today"
    LAST_WORKDAY = "last_workday"
    WEEK = "week"
    ALL = "all"
    CUSTOM = "custom"


SCOPE_LABELS = {
    ScopeKind.TODAY: "Today",
    ScopeKind.LAST_WORKDAY: "Last workday",
    ScopeKind.WEEK: "This week",
    ScopeKind.ALL: "All time",
    ScopeKind.CUSTOM: "Custom date",
}


def last_workday(today: date) -> date:
    weekday = today.weekday()  # Monday=0 ... Sunday=6
    if weekday == 0:
        delta = 3
    elif weekday == 6:
        delta = 2
    else:
        delta = 1
    return today - timedelta(days=delta)


def day_bounds_ms(day: date) -> tuple[int, int]:
    """Return [start, end) epoch-ms bounds for the given local calendar day."""
    start_dt = datetime(day.year, day.month, day.day)
    end_dt = start_dt + timedelta(days=1)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


@dataclass
class Scope:
    kind: ScopeKind
    custom_date: date | None = None

    def label(self) -> str:
        if self.kind == ScopeKind.CUSTOM and self.custom_date:
            return self.custom_date.strftime("%A, %B %d, %Y")
        return SCOPE_LABELS[self.kind]

    def bounds_ms(self) -> tuple[int, int]:
        today = date.today()
        if self.kind == ScopeKind.TODAY:
            return day_bounds_ms(today)
        if self.kind == ScopeKind.LAST_WORKDAY:
            return day_bounds_ms(last_workday(today))
        if self.kind == ScopeKind.WEEK:
            monday = today - timedelta(days=today.weekday())
            start_ms, _ = day_bounds_ms(monday)
            _, end_ms = day_bounds_ms(today)
            return start_ms, end_ms
        if self.kind == ScopeKind.CUSTOM and self.custom_date:
            return day_bounds_ms(self.custom_date)
        # ALL
        return 0, int(datetime(2999, 1, 1).timestamp() * 1000)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class SessionRow:
    id: str
    parent_id: str | None
    project_id: str
    directory: str
    title: str
    agent: str | None
    time_created: int
    time_archived: int | None
    cost: float
    tokens_input: int
    tokens_output: int
    tokens_reasoning: int
    tokens_cache_read: int
    tokens_cache_write: int
    additions: int
    deletions: int
    files: int


@dataclass
class TodoItem:
    session_id: str
    content: str
    status: str
    priority: str
    position: int


@dataclass
class TaskTotals:
    cost: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_reasoning: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    additions: int = 0
    deletions: int = 0
    files: int = 0
    subagent_count: int = 0

    def add(self, s: SessionRow, is_root: bool) -> None:
        self.cost += s.cost
        self.tokens_input += s.tokens_input
        self.tokens_output += s.tokens_output
        self.tokens_reasoning += s.tokens_reasoning
        self.tokens_cache_read += s.tokens_cache_read
        self.tokens_cache_write += s.tokens_cache_write
        self.additions += s.additions
        self.deletions += s.deletions
        self.files += s.files
        if not is_root:
            self.subagent_count += 1

    @property
    def total_tokens(self) -> int:
        return (
            self.tokens_input
            + self.tokens_output
            + self.tokens_reasoning
            + self.tokens_cache_read
            + self.tokens_cache_write
        )


@dataclass
class Task:
    root: SessionRow
    totals: TaskTotals = field(default_factory=TaskTotals)


@dataclass
class ProjectGroup:
    name: str
    tasks: list[Task] = field(default_factory=list)

    @property
    def totals(self) -> TaskTotals:
        agg = TaskTotals()
        for t in self.tasks:
            agg.cost += t.totals.cost
            agg.tokens_input += t.totals.tokens_input
            agg.tokens_output += t.totals.tokens_output
            agg.tokens_reasoning += t.totals.tokens_reasoning
            agg.tokens_cache_read += t.totals.tokens_cache_read
            agg.tokens_cache_write += t.totals.tokens_cache_write
            agg.additions += t.totals.additions
            agg.deletions += t.totals.deletions
            agg.files += t.totals.files
            agg.subagent_count += t.totals.subagent_count
        return agg


@dataclass
class TodoProjectGroup:
    name: str
    # session -> list of todo items
    sessions: list[tuple[SessionRow, list[TodoItem]]] = field(default_factory=list)


# --------------------------------------------------------------------------
# DB access (reads are always via a read-only connection)
# --------------------------------------------------------------------------


def _ro_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_sessions(db_path: str) -> list[SessionRow]:
    conn = _ro_connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, parent_id, project_id, directory, title, agent, time_created,
                   time_archived, cost, tokens_input, tokens_output, tokens_reasoning,
                   tokens_cache_read, tokens_cache_write,
                   summary_additions, summary_deletions, summary_files
            FROM session
            """
        ).fetchall()
    finally:
        conn.close()

    return [
        SessionRow(
            id=r["id"],
            parent_id=r["parent_id"],
            project_id=r["project_id"],
            directory=r["directory"],
            title=r["title"] or "(untitled session)",
            agent=r["agent"],
            time_created=r["time_created"],
            time_archived=r["time_archived"],
            cost=r["cost"] or 0.0,
            tokens_input=r["tokens_input"] or 0,
            tokens_output=r["tokens_output"] or 0,
            tokens_reasoning=r["tokens_reasoning"] or 0,
            tokens_cache_read=r["tokens_cache_read"] or 0,
            tokens_cache_write=r["tokens_cache_write"] or 0,
            additions=r["summary_additions"] or 0,
            deletions=r["summary_deletions"] or 0,
            files=r["summary_files"] or 0,
        )
        for r in rows
    ]


def load_project_names(db_path: str) -> dict[str, str]:
    conn = _ro_connect(db_path)
    try:
        rows = conn.execute("SELECT id, worktree, name FROM project").fetchall()
    finally:
        conn.close()
    return {
        r["id"]: (r["name"] or os.path.basename(r["worktree"].rstrip("/")) or r["worktree"])
        for r in rows
    }


def load_open_todos(db_path: str) -> list[TodoItem]:
    conn = _ro_connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT session_id, content, status, priority, position
            FROM todo
            WHERE status NOT IN ('completed', 'cancelled')
            ORDER BY session_id, position
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        TodoItem(
            session_id=r["session_id"],
            content=r["content"],
            status=r["status"],
            priority=r["priority"],
            position=r["position"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# Direct-DB fallback: unarchive
#
# opencode's REST API supports archiving a session (PATCH .../session/:id
# with {"time": {"archived": <ms>}}) but does not currently expose a
# supported way to clear that field again. Since this is a deliberate,
# narrow, reversible write to a single column (and opencode's sqlite db is
# in WAL mode, so a short write transaction is safe alongside a running
# opencode instance), we fall back to a direct UPDATE for this one action.
# --------------------------------------------------------------------------


def unarchive_session(db_path: str, session_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 3000")
        conn.execute(
            "UPDATE session SET time_archived = NULL, time_updated = ? WHERE id = ?",
            (int(time.time() * 1000), session_id),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# opencode server lifecycle + REST calls (used for rename / archive)
# --------------------------------------------------------------------------


class OpencodeServerError(RuntimeError):
    pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpencodeServer:
    """Lazily-started, on-demand `opencode serve` instance used only for
    mutating actions (rename / archive) so they go through opencode's
    supported REST API instead of writing to the database directly."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.base_url: str | None = None

    def ensure_started(self) -> str:
        if self.proc is not None and self.proc.poll() is None and self.base_url:
            return self.base_url

        if shutil.which("opencode") is None:
            raise OpencodeServerError(
                "opencode CLI not found on PATH; rename/archive are unavailable."
            )

        port = find_free_port()
        self.proc = subprocess.Popen(
            ["opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.base_url = f"http://127.0.0.1:{port}"

        deadline = time.time() + 8
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base_url}/global/health", timeout=0.5) as resp:
                    if resp.status == 200:
                        return self.base_url
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.2)

        self.stop()
        raise OpencodeServerError(f"Timed out waiting for opencode server to start: {last_err}")

    def patch_session(self, session_id: str, body: dict) -> dict:
        base_url = self.ensure_started()
        url = f"{base_url}/session/{session_id}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="PATCH", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OpencodeServerError(f"opencode API error ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise OpencodeServerError(f"Could not reach opencode server: {exc}") from exc

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.base_url = None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def build_report(
    sessions: list[SessionRow],
    project_names: dict[str, str],
    scope: Scope,
    include_archived: bool = False,
) -> list[ProjectGroup]:
    start_ms, end_ms = scope.bounds_ms()

    by_id = {s.id: s for s in sessions}
    children: dict[str, list[str]] = defaultdict(list)
    for s in sessions:
        if s.parent_id:
            children[s.parent_id].append(s.id)

    top_level = [
        s
        for s in sessions
        if s.parent_id is None
        and start_ms <= s.time_created < end_ms
        and (include_archived or s.time_archived is None)
    ]

    groups: dict[str, ProjectGroup] = {}

    for root in top_level:
        totals = TaskTotals()
        stack = [root.id]
        seen: set[str] = set()
        while stack:
            sid = stack.pop()
            if sid in seen:
                continue
            seen.add(sid)
            node = by_id.get(sid)
            if node is None:
                continue
            totals.add(node, is_root=(sid == root.id))
            stack.extend(children.get(sid, []))

        task = Task(root=root, totals=totals)

        project_label = (
            project_names.get(root.project_id)
            or os.path.basename(root.directory.rstrip("/"))
            or root.directory
        )

        group = groups.setdefault(project_label, ProjectGroup(name=project_label))
        group.tasks.append(task)

    ordered = sorted(groups.values(), key=lambda g: g.totals.cost, reverse=True)
    for g in ordered:
        g.tasks.sort(key=lambda t: t.root.time_created)
    return ordered


def build_todo_report(
    sessions: list[SessionRow],
    todos: list[TodoItem],
    project_names: dict[str, str],
    include_archived: bool = False,
) -> list[TodoProjectGroup]:
    by_id = {s.id: s for s in sessions}
    todos_by_session: dict[str, list[TodoItem]] = defaultdict(list)
    for t in todos:
        todos_by_session[t.session_id].append(t)

    groups: dict[str, TodoProjectGroup] = {}
    for session_id, items in todos_by_session.items():
        session = by_id.get(session_id)
        if session is None:
            continue
        if not include_archived and session.time_archived is not None:
            continue
        project_label = (
            project_names.get(session.project_id)
            or os.path.basename(session.directory.rstrip("/"))
            or session.directory
        )
        group = groups.setdefault(project_label, TodoProjectGroup(name=project_label))
        group.sessions.append((session, sorted(items, key=lambda i: i.position)))

    ordered = sorted(groups.values(), key=lambda g: g.name.lower())
    for g in ordered:
        g.sessions.sort(key=lambda pair: pair[0].time_created, reverse=True)
    return ordered


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


def fmt_cost(v: float) -> str:
    return f"${v:,.4f}" if v < 1 else f"${v:,.2f}"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%b %d")


PRIORITY_COLOR = {"high": "red", "medium": "yellow", "low": "dim"}


# --------------------------------------------------------------------------
# Modal screens
# --------------------------------------------------------------------------


class RenameModal(ModalScreen[str | None]):
    CSS = """
    RenameModal {
        align: center middle;
    }
    #dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, current_title: str) -> None:
        super().__init__()
        self.current_title = current_title

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("Rename session (Enter to save, Escape to cancel)")
            yield Input(value=self.current_title, id="rename-input")

    def on_mount(self) -> None:
        self.query_one("#rename-input", Input).focus()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    CSS = """
    ConfirmModal {
        align: center middle;
    }
    #dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #buttons {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    #buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self.message)
            with Container(id="buttons"):
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no", variant="primary")

    @on(Button.Pressed, "#yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "escape" or event.key == "n":
            self.dismiss(False)
        elif event.key == "y" or event.key == "enter":
            self.dismiss(True)


HELP_TEXT = """\
[b]opencode-standup — keybindings[/b]

[b]Global[/b]
  q          Quit
  r          Refresh current tab
  [ / ]      Previous / next tab
  ?          Show this help

[b]Standup tab — time scope[/b]
  1          Today
  2          Last workday
  3          This week
  4          All time

[b]Actions (select a task/session row first)[/b]
  o          Open/resume this session in the real opencode TUI
  e          Rename this session
  x          Archive this session (hides it from view)
  A          Toggle showing archived sessions
  u          Unarchive the selected session (only when archived
             sessions are shown; this is a direct database write,
             since opencode's API has no unarchive endpoint yet)

Press any key to close this help.
"""


class HelpModal(ModalScreen[None]):
    CSS = """
    HelpModal {
        align: center middle;
    }
    #dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(HELP_TEXT)

    def on_key(self, event) -> None:
        self.dismiss(None)

    def on_click(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# Textual app
# --------------------------------------------------------------------------


class StandupApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #summary, #todo-summary {
        height: auto;
        padding: 1 2;
        border: round $accent;
        margin: 1 2 0 2;
    }
    #tree-container, #todo-tree-container {
        height: 1fr;
        margin: 1 2;
    }
    Tree {
        background: $surface;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_report", "Refresh"),
        ("e", "rename", "Rename"),
        ("x", "archive", "Archive"),
        ("A", "toggle_archived", "Show archived"),
        ("u", "unarchive", "Unarchive"),
        ("o", "open_session", "Open"),
        ("1", "set_scope_today", "Today"),
        ("2", "set_scope_last_workday", "Last workday"),
        ("3", "set_scope_week", "Week"),
        ("4", "set_scope_all", "All"),
        ("[", "prev_tab", "Prev tab"),
        ("]", "next_tab", "Next tab"),
        ("question_mark", "show_help", "Help"),
    ]

    def __init__(self, db_path: str, scope: Scope):
        super().__init__()
        self.db_path = db_path
        self.scope = scope
        self.include_archived = False
        self.groups: list[ProjectGroup] = []
        self.todo_groups: list[TodoProjectGroup] = []
        self.server = OpencodeServer()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="standup-tab"):
            with TabPane("Standup", id="standup-tab"):
                yield Static(id="summary")
                with VerticalScroll(id="tree-container"):
                    yield Tree("opencode standup", id="report-tree")
            with TabPane("Todo", id="todo-tab"):
                yield Static(id="todo-summary")
                with VerticalScroll(id="todo-tree-container"):
                    yield Tree("open todos", id="todo-tree")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "opencode standup"
        self.load_and_render()

    def on_unmount(self) -> None:
        self.server.stop()

    # -- data loading ------------------------------------------------------

    def load_and_render(self) -> None:
        sessions = load_sessions(self.db_path)
        project_names = load_project_names(self.db_path)
        todos = load_open_todos(self.db_path)

        self.groups = build_report(sessions, project_names, self.scope, self.include_archived)
        self.todo_groups = build_todo_report(sessions, todos, project_names, self.include_archived)

        self.render_summary()
        self.render_tree()
        self.render_todo_summary()
        self.render_todo_tree()

    # -- Standup tab rendering ----------------------------------------------

    def render_summary(self) -> None:
        scope_label = self.scope.label()
        grand = TaskTotals()
        task_count = 0
        for g in self.groups:
            t = g.totals
            grand.cost += t.cost
            grand.tokens_input += t.tokens_input
            grand.tokens_output += t.tokens_output
            grand.tokens_reasoning += t.tokens_reasoning
            grand.tokens_cache_read += t.tokens_cache_read
            grand.tokens_cache_write += t.tokens_cache_write
            grand.additions += t.additions
            grand.deletions += t.deletions
            grand.files += t.files
            grand.subagent_count += t.subagent_count
            task_count += len(g.tasks)

        widget = self.query_one("#summary", Static)
        archived_note = "  [dim](showing archived)[/dim]" if self.include_archived else ""
        header_line = f"[b]{scope_label}[/b]{archived_note}"

        if not self.groups:
            widget.update(f"{header_line}\n\n[dim]No opencode sessions found for this scope.[/dim]")
            return

        lines = [
            header_line,
            "",
            f"[b]Total cost:[/b] {fmt_cost(grand.cost)}    "
            f"[b]Tasks:[/b] {task_count}    "
            f"[b]Projects:[/b] {len(self.groups)}    "
            f"[b]Subagent calls:[/b] {grand.subagent_count}",
            f"[b]Tokens[/b]  in: {fmt_tokens(grand.tokens_input)}  "
            f"out: {fmt_tokens(grand.tokens_output)}  "
            f"reasoning: {fmt_tokens(grand.tokens_reasoning)}  "
            f"cache-read: {fmt_tokens(grand.tokens_cache_read)}  "
            f"cache-write: {fmt_tokens(grand.tokens_cache_write)}  "
            f"[b](total: {fmt_tokens(grand.total_tokens)})[/b]",
            f"[b]Diff:[/b] +{grand.additions} / -{grand.deletions} across {grand.files} files",
        ]
        widget.update("\n".join(lines))

    def render_tree(self) -> None:
        tree = self.query_one("#report-tree", Tree)
        tree.clear()
        tree.root.expand()
        tree.show_root = False

        auto_collapse = self.scope.kind == ScopeKind.ALL and sum(len(g.tasks) for g in self.groups) > 15

        for g in self.groups:
            t = g.totals
            label = (
                f"[b]{g.name}[/b]  —  {len(g.tasks)} task(s), {fmt_cost(t.cost)}, "
                f"{fmt_tokens(t.total_tokens)} tokens"
            )
            project_node: TreeNode = tree.root.add(label, expand=not auto_collapse)
            for task in g.tasks:
                tt = task.totals
                time_str = (
                    fmt_time(task.root.time_created)
                    if self.scope.kind in (ScopeKind.TODAY, ScopeKind.LAST_WORKDAY, ScopeKind.CUSTOM)
                    else f"{fmt_date(task.root.time_created)} {fmt_time(task.root.time_created)}"
                )
                sub_note = f", {tt.subagent_count} subagent call(s)" if tt.subagent_count else ""
                diff_note = (
                    f", +{tt.additions}/-{tt.deletions} ({tt.files} files)"
                    if (tt.additions or tt.deletions)
                    else ""
                )
                archived_note = " [dim](archived)[/dim]" if task.root.time_archived else ""
                task_label = (
                    f"[cyan]{time_str}[/cyan]  {task.root.title}{archived_note}  "
                    f"[green]{fmt_cost(tt.cost)}[/green]  "
                    f"[dim]{fmt_tokens(tt.total_tokens)} tokens{sub_note}{diff_note}[/dim]"
                )
                project_node.add_leaf(task_label, data=task.root)

    # -- Todo tab rendering --------------------------------------------------

    def render_todo_summary(self) -> None:
        total_items = sum(len(items) for g in self.todo_groups for _, items in g.sessions)
        total_sessions = sum(len(g.sessions) for g in self.todo_groups)
        widget = self.query_one("#todo-summary", Static)
        archived_note = "  [dim](showing archived)[/dim]" if self.include_archived else ""
        if not self.todo_groups:
            widget.update(f"[b]Open todos[/b]{archived_note}\n\n[dim]Nothing outstanding. Nice.[/dim]")
            return
        widget.update(
            f"[b]Open todos[/b]{archived_note}\n\n"
            f"[b]{total_items}[/b] item(s) across [b]{total_sessions}[/b] session(s) "
            f"in [b]{len(self.todo_groups)}[/b] project(s)"
        )

    def render_todo_tree(self) -> None:
        tree = self.query_one("#todo-tree", Tree)
        tree.clear()
        tree.root.expand()
        tree.show_root = False

        for g in self.todo_groups:
            item_count = sum(len(items) for _, items in g.sessions)
            project_node: TreeNode = tree.root.add(
                f"[b]{g.name}[/b]  —  {item_count} item(s)", expand=True
            )
            for session, items in g.sessions:
                archived_note = " [dim](archived)[/dim]" if session.time_archived else ""
                session_node = project_node.add(
                    f"{session.title}{archived_note}  [dim]({fmt_date(session.time_created)})[/dim]",
                    expand=True,
                    data=session,
                )
                for item in items:
                    color = PRIORITY_COLOR.get(item.priority, "white")
                    session_node.add_leaf(
                        f"[{color}]●[/{color}] {item.content}  [dim]({item.priority})[/dim]",
                        data=session,
                    )

    # -- helpers -------------------------------------------------------------

    def _active_tree(self) -> Tree:
        tabbed = self.query_one(TabbedContent)
        if tabbed.active == "todo-tab":
            return self.query_one("#todo-tree", Tree)
        return self.query_one("#report-tree", Tree)

    def _selected_session(self) -> SessionRow | None:
        tree = self._active_tree()
        node = tree.cursor_node
        if node is None or node.data is None:
            return None
        return node.data

    def _reload_scope_selector_label(self) -> None:
        # header/summary re-render already reflects scope; nothing extra needed
        pass

    # -- actions: scope switching ---------------------------------------------

    def action_set_scope_today(self) -> None:
        self.scope = Scope(ScopeKind.TODAY)
        self.load_and_render()

    def action_set_scope_last_workday(self) -> None:
        self.scope = Scope(ScopeKind.LAST_WORKDAY)
        self.load_and_render()

    def action_set_scope_week(self) -> None:
        self.scope = Scope(ScopeKind.WEEK)
        self.load_and_render()

    def action_set_scope_all(self) -> None:
        self.scope = Scope(ScopeKind.ALL)
        self.load_and_render()

    # -- actions: tabs ---------------------------------------------------------

    def action_prev_tab(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "todo-tab" if tabbed.active == "standup-tab" else "standup-tab"

    def action_next_tab(self) -> None:
        self.action_prev_tab()  # only two tabs; prev/next are equivalent

    # -- actions: refresh / help -------------------------------------------------

    def action_refresh_report(self) -> None:
        self.load_and_render()

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    # -- actions: rename / archive / unarchive / open --------------------------

    def action_rename(self) -> None:
        session = self._selected_session()
        if session is None:
            self.notify("Select a task/session first.", severity="warning")
            return

        def handle_result(new_title: str | None) -> None:
            if not new_title or new_title == session.title:
                return
            try:
                self.server.patch_session(session.id, {"title": new_title})
            except OpencodeServerError as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.notify(f"Renamed to “{new_title}”.")
            self.load_and_render()

        self.push_screen(RenameModal(session.title), handle_result)

    def action_archive(self) -> None:
        session = self._selected_session()
        if session is None:
            self.notify("Select a task/session first.", severity="warning")
            return
        if session.time_archived:
            self.notify("Already archived.", severity="warning")
            return

        def handle_result(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self.server.patch_session(session.id, {"time": {"archived": int(time.time() * 1000)}})
            except OpencodeServerError as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.notify("Archived.")
            self.load_and_render()

        self.push_screen(ConfirmModal(f"Archive “{session.title}”?"), handle_result)

    def action_toggle_archived(self) -> None:
        self.include_archived = not self.include_archived
        self.notify("Showing archived sessions." if self.include_archived else "Hiding archived sessions.")
        self.load_and_render()

    def action_unarchive(self) -> None:
        if not self.include_archived:
            self.notify("Press 'A' to show archived sessions first.", severity="warning")
            return
        session = self._selected_session()
        if session is None:
            self.notify("Select a task/session first.", severity="warning")
            return
        if not session.time_archived:
            self.notify("This session isn't archived.", severity="warning")
            return
        unarchive_session(self.db_path, session.id)
        self.notify("Unarchived.")
        self.load_and_render()

    def action_open_session(self) -> None:
        session = self._selected_session()
        if session is None:
            self.notify("Select a task/session first.", severity="warning")
            return
        if shutil.which("opencode") is None:
            self.notify("opencode CLI not found on PATH.", severity="error")
            return

        with self.suspend():
            subprocess.run(["opencode", session.directory, "--session", session.id])

        self.load_and_render()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A daily command-center for opencode sessions across your repos."
    )
    parser.add_argument(
        "--date",
        dest="explicit_date",
        default=None,
        help="Start on a specific date, format YYYY-MM-DD, instead of the default scope.",
    )
    parser.add_argument(
        "--days-ago",
        dest="days_ago",
        type=int,
        default=None,
        help="Start on the day N days ago (0 = today, 1 = yesterday), instead of the default scope.",
    )
    parser.add_argument(
        "--scope",
        dest="scope",
        choices=["today", "last-workday", "week", "all"],
        default=None,
        help="Start on a specific scope. Defaults to 'today'.",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=os.environ.get("OPENCODE_STANDUP_DB", DEFAULT_DB_PATH),
        help=f"Path to opencode's sqlite db (default: {DEFAULT_DB_PATH}).",
    )
    args = parser.parse_args()

    db_path = os.path.expanduser(args.db_path)
    if not Path(db_path).exists():
        raise SystemExit(f"opencode database not found at: {db_path}")

    if args.explicit_date:
        scope = Scope(ScopeKind.CUSTOM, datetime.strptime(args.explicit_date, "%Y-%m-%d").date())
    elif args.days_ago is not None:
        scope = Scope(ScopeKind.CUSTOM, date.today() - timedelta(days=args.days_ago))
    elif args.scope:
        scope = Scope(
            {
                "today": ScopeKind.TODAY,
                "last-workday": ScopeKind.LAST_WORKDAY,
                "week": ScopeKind.WEEK,
                "all": ScopeKind.ALL,
            }[args.scope]
        )
    else:
        scope = Scope(ScopeKind.TODAY)

    app = StandupApp(db_path=db_path, scope=scope)
    app.run()


if __name__ == "__main__":
    main()
