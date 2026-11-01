"""Session-scoped stdio MCP server for Runtime Lite.

This is a real execution boundary, not a thin-client configuration mode. It
does not load endpoint settings, credentials, RemoteSession, Views, upload code,
or network transport. Its only lifecycle is stdin/stdout: EOF ends the process.

``initialize`` answers immediately. The repository index is brought current on
one background thread owned by this process (a cold build of a large repository
can take minutes, far past a host's connect timeout). Index-backed tools wait
briefly for it and otherwise report build progress; file reads, grep, edit and
bash never wait.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

from ..errors import ClientError, error_block
from ..localtools import LocalContext, LocalResult, SessionMemory
from ..localtools import astgrep as local_astgrep
from ..localtools import editing as local_editing
from ..localtools import search as local_search
from ..localtools import shell as local_shell
from ..localtools import vcs as local_vcs
from ..localtools import web as local_web
from ..localtools.shell import close_background
from ..trace import ClientTraceWriter, new_trace_id
from ..workspace_members import (
    MAX_REPOSITORIES,
    WorkspaceCatalog,
    WorkspaceRepo,
    discover_members,
    head_commit,
    head_label,
    is_repository,
    recorded_commits,
    resolve_root,
)
from ..worktrees import LinkedWorktree, WorktreeCatalog, _path_values
from .index import LiteIndex, LiteIndexProgress, LiteIndexReport, LiteIndexUpgradeRequired, ProgressCallback
from .policy import LITE_VISIBLE_TOOLS, LOCAL_LITE_TOOLS
from .semantic import LiteSemanticClient, fuse_ranked_paths, is_semantic_intent_query

SERVER_NAME: Final[str] = "lemoncrow-runtime-lite"
SERVER_VERSION: Final[str] = "1"
_PROTOCOL_VERSION: Final[str] = "2025-06-18"
_MUTATING_OR_UNKNOWN_TOOLS: Final[frozenset[str]] = frozenset({"edit", "codemod", "bash"})
_SERIAL_TOOLS: Final[frozenset[str]] = frozenset({"edit", "codemod"})
_MAX_CONCURRENT_CALLS: Final[int] = 8
_MAX_CONCURRENT_INDEXED_CALLS: Final[int] = 2
_MAX_WORKTREE_INDEXES: Final[int] = 8
#: Tools answered from the index; ``read`` joins them only for ``symbol=``.
_INDEX_TOOLS: Final[frozenset[str]] = frozenset({"code_search", "relations", "graph"})
#: How long an index-backed call waits for the background build before
#: reporting progress instead (small repositories finish inside it).
_INDEX_WAIT_S: Final[float] = 20.0
#: How long EOF waits for a cancelled background build to roll back.
_PREPARE_STOP_WAIT_S: Final[float] = 5.0
#: Ranked files that keep inline source, as in one repository's search.
_SNIPPET_FILES: Final[int] = 3
#: Members named one by one in the session instructions of a root with members.
_INSTRUCTION_REPOS: Final[int] = 40
#: Member indexes built at once, so one large repository does not hold the rest back.
_BUILD_WORKERS: Final[int] = 2
_LOCAL_EXECUTORS: Final[dict[str, Any]] = {
    "bash": local_shell.run_bash,
    "edit": local_editing.run_edit,
    "grep": local_search.run_grep,
    "scan": local_astgrep.run_scan,
    "codemod": local_astgrep.run_codemod,
    "blame": local_vcs.run_blame,
    "web_fetch": local_web.run_web_fetch,
}


def _workspace_root(environment: Mapping[str, str]) -> Path:
    explicit = environment.get("LEMONCROW_WORKSPACE_ROOT", "").strip()
    # A folder inside a work tree is served as that repository (its top
    # level), never as a folder in grep mode next to an unused index.
    root = resolve_root(Path(explicit) if explicit else Path.cwd())
    if not root.is_dir():
        raise RuntimeError(f"Runtime Lite workspace is not a directory: {root}")
    return root


def _state_root(environment: Mapping[str, str]) -> Path:
    explicit = environment.get("LEMONCROW_HOME", "").strip()
    return Path(explicit).expanduser().resolve() if explicit else (Path.home() / ".lemoncrow").resolve()


@dataclass(frozen=True, slots=True)
class _LiteConfig:
    state_dir: Path

    @property
    def runtime_state_dir(self) -> Path:
        return self.state_dir / "runtime"


def _local_config(_repo_root: Path, environment: Mapping[str, str]) -> _LiteConfig:
    """Construct local executor context without any endpoint/account fields."""

    return _LiteConfig(state_dir=_state_root(environment))


@dataclass(slots=True)
class _LiteWorkspace:
    root: Path
    environment: dict[str, str]
    index: LiteIndex
    memory: SessionMemory
    context: LocalContext


@dataclass(slots=True)
class _RepoIndex:
    """One member (or nested checkout) of a root: its own index and root."""

    repo: WorkspaceRepo
    index: LiteIndex
    #: Rooted at the checkout, for tools that must run inside it (blame).
    context: LocalContext
    ready: Future[Any] | None = None


def _round_robin(groups: Sequence[Sequence[Any]]) -> list[Any]:
    """Interleave per-member lists so no repository crowds out the rest."""

    merged: list[Any] = []
    for depth in range(max((len(group) for group in groups), default=0)):
        merged.extend(group[depth] for group in groups if depth < len(group))
    return merged


def _display(repo: WorkspaceRepo) -> str:
    return "the root repository" if repo.relpath == "." else repo.relpath


def _listing(items: Sequence[str], cap: int = _INSTRUCTION_REPOS) -> str:
    shown = ", ".join(items[:cap])
    return shown + (f", and {len(items) - cap} more" if len(items) > cap else "")


class _BuildQueue:
    """Session-owned builders for member indexes; members a call names go first.

    A few workers rather than one lane, so one large repository does not hold
    the others back. ``close`` cancels queued builds; a running one stops at
    its next progress report (``LiteMcp._prepare_repo``).
    """

    def __init__(self, run: Callable[[_RepoIndex], object], *, workers: int) -> None:
        self._run = run
        self._workers = workers
        self._threads: list[threading.Thread] = []
        self._urgent: deque[_RepoIndex] = deque()
        self._queued: deque[_RepoIndex] = deque()
        self._cond = threading.Condition()
        self._closed = False

    def submit(self, state: _RepoIndex, *, urgent: bool) -> Future[Any]:
        """Queue the member's build once; a failed or cancelled build is queued again."""

        with self._cond:
            if self._closed:
                raise RuntimeError("Runtime Lite is shutting down")
            future = state.ready
            if future is None or (future.done() and (future.cancelled() or future.exception() is not None)):
                future = Future()
                state.ready = future
                (self._urgent if urgent else self._queued).append(state)
            elif urgent and not (future.running() or future.done()):
                # Jump the queue; the entry left behind is skipped when reached.
                self._urgent.append(state)
            else:
                return future
            if len(self._threads) < self._workers:
                thread = threading.Thread(target=self._work, name=f"lc-lite-build-{len(self._threads)}", daemon=True)
                self._threads.append(thread)
                thread.start()
            self._cond.notify()
            return future

    def _claim_locked(self) -> tuple[_RepoIndex, Future[Any]] | None:
        if self._closed:
            return None
        for lane in (self._urgent, self._queued):
            while lane:
                state = lane.popleft()
                future = state.ready
                if (
                    future is not None
                    and not (future.running() or future.done())
                    and future.set_running_or_notify_cancel()
                ):
                    return state, future
        return None

    def _work(self) -> None:
        while True:
            with self._cond:
                claimed = self._claim_locked()
                while claimed is None:
                    if self._closed:
                        return
                    self._cond.wait()
                    claimed = self._claim_locked()
            state, future = claimed
            try:
                result = self._run(state)
            except Exception as exc:  # delivered to every caller waiting on the member
                future.set_exception(exc)
            else:
                future.set_result(result)

    def close(self, timeout: float) -> None:
        with self._cond:
            self._closed = True
            for state in (*self._urgent, *self._queued):
                if state.ready is not None:
                    state.ready.cancel()
            self._urgent.clear()
            self._queued.clear()
            self._cond.notify_all()
            threads = list(self._threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))


class _PrepareStopped(Exception):
    """Raised from the progress callback to abandon a background build at EOF."""


_LITE_SCHEMA_OVERRIDES: Final[dict[str, dict[str, Any]]] = {
    "bash": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "cwd": {"type": "string"},
            "timeout": {"type": "integer", "default": 120},
            "bg": {"type": "boolean", "default": False},
            "id": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "action": {
                "type": "string",
                "enum": ["poll", "status", "kill", "update"],
            },
        },
    },
    "edit": {
        "type": "object",
        "required": ["edits"],
        "additionalProperties": False,
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                        "replace": {"type": "boolean"},
                    },
                },
            }
        },
    },
    "read": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "object", "additionalProperties": True},
                    ]
                },
            },
            "symbol": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
        },
    },
    "code_search": {
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "paths": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 32},
        },
    },
    "relations": {
        "type": "object",
        "required": ["symbol"],
        "additionalProperties": False,
        "properties": {
            "symbol": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["self", "usages", "imports", "callers", "callees"],
                "default": "usages",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
    },
    "graph": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["map", "imports", "workspace", "impact"], "default": "map"},
            "path": {"type": "string", "default": ""},
            "paths": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
        },
    },
    "blame": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string"},
            "include_churn": {"type": "boolean", "default": True},
        },
    },
}


def _surface() -> list[dict[str, Any]]:
    """Return the explicit Lite allowlist using shared schemas where appropriate."""

    surface_path = Path(__file__).resolve().parents[1] / "toolsurface.json"
    payload = json.loads(surface_path.read_text(encoding="utf-8"))
    source = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(source, dict):
        raise RuntimeError("Runtime tool surface is not a name-keyed object")

    tools: list[dict[str, Any]] = []
    for name in LOCAL_LITE_TOOLS:
        if name not in LITE_VISIBLE_TOOLS:
            continue
        raw = source.get(name)
        if not isinstance(raw, dict):
            raise RuntimeError(f"Runtime Lite tool missing shared schema: {name}")
        schema = _LITE_SCHEMA_OVERRIDES.get(name, raw.get("inputSchema"))
        if not isinstance(schema, dict):
            raise RuntimeError(f"Runtime Lite tool has invalid schema: {name}")
        description = str(raw.get("description") or "")
        if name == "relations":
            description = (
                "Basic local symbol relationships from the Runtime Lite index. "
                "kind=self|usages|imports is exact/basic; callers/callees are bounded lexical approximations."
            )
        elif name == "graph":
            description = (
                "Basic local repository map. kind=map summarizes files by symbol/import counts; "
                "kind=imports lists parsed imports. kind=workspace lists dependency edges between this root's "
                "repositories (submodules, local paths, packages, imports, proto, OpenAPI, HTTP routes, events) "
                "with evidence and confidence; kind=impact lists the repositories a change reaches "
                "(paths: root-relative changed files; none = working-tree changes). "
                "Advanced graph/lineage analytics are hosted."
            )
        tools.append({"name": name, "description": description, "inputSchema": schema})
    return tools


class LiteMcp:
    def __init__(self, *, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or os.environ)
        self.repo_root = _workspace_root(self.environment)
        self._git_workspace = is_repository(self.repo_root)
        # A root with members (a folder of repositories, or a repository with
        # checked-out submodules) answers from each member's own index. A plain
        # repository, and a folder holding none, are served exactly as before.
        membership = discover_members(self.repo_root)
        self._workspace: WorkspaceCatalog | None = (
            WorkspaceCatalog(self.repo_root, membership) if membership.members and membership.multi else None
        )
        self._repos: dict[Path, _RepoIndex] = {}
        self._repos_lock = threading.Lock()
        self._repo_progress: dict[str, LiteIndexProgress] = {}
        self._builds = _BuildQueue(self._prepare_repo, workers=_BUILD_WORKERS) if self._workspace is not None else None
        self._catalog = WorktreeCatalog(self.repo_root)
        self._children: OrderedDict[Path, _LiteWorkspace] = OrderedDict()
        self._children_lock = threading.Lock()
        # One session-owned refresh lane validates/updates warm WAL generations.
        # ThreadPoolExecutor is deliberate: unlike a detached raw thread, close()
        # shuts it down and joins all work before the MCP session exits.
        self._refresh_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lc-lite-refresh")
        self._refresh_futures: dict[Path, Future[Any]] = {}
        self._refresh_futures_lock = threading.Lock()
        self._main = self._new_workspace(self.repo_root)
        self._semantic = LiteSemanticClient(self.environment)
        # Keep the original public attributes for compatibility with tests and
        # callers that inspect the primary checkout directly.
        self.index = self._main.index
        self.memory = self._main.memory
        self.context = self._main.context
        self.tools = _surface()
        # One stdio process serves one host session; its calls are the local
        # usage record (same schema as the full client's trace, site "lite").
        self.session_id = f"lite_{secrets.token_hex(8)}"
        self._traces = ClientTraceWriter(_state_root(self.environment) / "runtime", env=self.environment)
        self._prepare_thread: threading.Thread | None = None
        self._prepare_error: Exception | None = None
        self._prepare_progress: LiteIndexProgress | None = None
        self._prepare_stop = threading.Event()
        self._semantic_prepare_thread: threading.Thread | None = None
        self._semantic_prepare_root: Path | None = None

    def _new_workspace(self, root: Path) -> _LiteWorkspace:
        environment = dict(self.environment)
        environment["LEMONCROW_WORKSPACE_ROOT"] = str(root)
        memory = SessionMemory()
        context = LocalContext(
            config=_local_config(root, environment),
            repo_root=root,
            sync=None,
            environment=environment,
            memory=memory,
        )
        return _LiteWorkspace(
            root=root,
            environment=environment,
            index=LiteIndex(root, stale_while_refresh=True),
            memory=memory,
            context=context,
        )

    def _schedule_refresh(self, workspace: _LiteWorkspace) -> None:
        if not self._git_workspace:
            return
        self._schedule_index_refresh(workspace.root, workspace.index)

    def _schedule_index_refresh(self, root: Path, index: LiteIndex) -> None:
        if not index.refresh_needed():
            return
        root = root.resolve()
        with self._refresh_futures_lock:
            existing = self._refresh_futures.get(root)
            if existing is not None and not existing.done():
                return
            if existing is not None:
                try:
                    existing.result()
                except Exception:
                    pass
            self._refresh_futures[root] = self._refresh_executor.submit(index.refresh_if_needed)

    def _worktree_workspace(self, worktree: LinkedWorktree) -> _LiteWorkspace:
        root = worktree.root.resolve()
        with self._children_lock:
            workspace = self._children.pop(root, None)
            if workspace is not None:
                self._children[root] = workspace
                return workspace

        # Opening/adopting an index may need Git and SQLite work. Keep it out of
        # the global child-worktree lock so one subagent entering a new checkout
        # cannot stall unrelated routed calls. Concurrent creators are harmless:
        # SQLite serializes a cold writer and the loser is discarded below.
        candidate = self._new_workspace(root)
        candidate.index.adopt_prepared()

        evicted: _LiteWorkspace | None = None
        discard: _LiteWorkspace | None = None
        with self._children_lock:
            workspace = self._children.pop(root, None)
            if workspace is None:
                workspace = candidate
            else:
                discard = candidate
            self._children[root] = workspace
            while len(self._children) > _MAX_WORKTREE_INDEXES:
                _old_root, evicted = self._children.popitem(last=False)

        if discard is not None:
            close_background(discard.memory)
        if evicted is not None:
            close_background(evicted.memory)
        self._start_semantic_prepare(root)
        return workspace

    # -- a root with member repositories ------------------------------------ #

    def _require_workspace(self) -> WorkspaceCatalog:
        workspace = self._workspace
        if workspace is None:
            raise RuntimeError("Runtime Lite is not serving member repositories")
        return workspace

    def _repo_index(self, repo: WorkspaceRepo) -> _RepoIndex:
        """The session's state for one checkout, created on first use."""

        with self._repos_lock:
            state = self._repos.get(repo.root)
            if state is None:
                if repo.root == self.repo_root and self._git_workspace:
                    # The root repository is a member and keeps the main index.
                    state = _RepoIndex(repo=repo, index=self._main.index, context=self._main.context)
                else:
                    environment = dict(self.environment)
                    environment["LEMONCROW_WORKSPACE_ROOT"] = str(repo.root)
                    context = LocalContext(
                        config=_local_config(repo.root, environment),
                        repo_root=repo.root,
                        sync=None,
                        environment=environment,
                        memory=self.memory,
                    )
                    state = _RepoIndex(repo=repo, index=LiteIndex(repo.root, stale_while_refresh=True), context=context)
                self._repos[repo.root] = state
            return state

    def _ensure_prepared(self, state: _RepoIndex, *, urgent: bool) -> Future[Any]:
        builds = self._builds
        if builds is None:
            raise RuntimeError("Runtime Lite is not serving member repositories")
        return builds.submit(state, urgent=urgent)

    def _prepare_repo(self, state: _RepoIndex) -> LiteIndexReport:
        name = state.repo.relpath

        def observe(progress: LiteIndexProgress) -> None:
            self._repo_progress[name] = progress
            if self._prepare_stop.is_set():
                raise _PrepareStopped()

        try:
            return state.index.adopt_prepared(progress=observe)
        finally:
            self._repo_progress.pop(name, None)

    def _await_repos(
        self, states: Sequence[_RepoIndex], *, urgent: bool
    ) -> tuple[list[_RepoIndex], list[_RepoIndex], list[tuple[_RepoIndex, Exception]]]:
        """Wait (bounded, shared deadline) for the members' indexes.

        Returns the ready members, the ones still building and the ones whose
        build failed. Callers answer from the ready ones and name the rest, so
        a partial answer is never presented as a complete one. ``urgent``
        (the call named these members) moves them ahead of queued builds.
        """

        futures = [(state, self._ensure_prepared(state, urgent=urgent)) for state in states]
        deadline = time.monotonic() + _INDEX_WAIT_S
        ready: list[_RepoIndex] = []
        building: list[_RepoIndex] = []
        failed: list[tuple[_RepoIndex, Exception]] = []
        for state, future in futures:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                building.append(state)
            except Exception as exc:
                failed.append((state, exc))
            else:
                ready.append(state)
        return ready, building, failed

    def _resolve_spelling(self, raw: str) -> Path | None:
        """Absolute path a tool argument names, without refusing (for bookkeeping)."""

        from ..kit.read_path import split_file_opts

        head = split_file_opts(raw)[0].strip()
        if not head:
            return None
        path = Path(head).expanduser()
        try:
            return (path if path.is_absolute() else self.repo_root / path).resolve()
        except (OSError, RuntimeError):
            return None

    def _workspace_targets(
        self, paths: object
    ) -> tuple[dict[Path, tuple[_RepoIndex, list[str] | None]], list[str], bool]:
        """Map ``paths`` onto checkouts.

        Returns ``{checkout root: (state, scopes)}`` where ``scopes`` is
        ``None`` for the whole checkout; the spellings that lie in no checkout
        and contain no member (a non-Git root's own files); and whether the
        member bound may have left members out of the requested area. A path
        reaches its own checkout (nearest ``.git``) plus every member below it.
        """

        workspace = self._require_workspace()
        membership = workspace.membership()
        if isinstance(paths, str):
            spellings = [paths]
        elif isinstance(paths, (list, tuple)):
            spellings = [str(path) for path in paths]
        else:
            spellings = []
        spellings = [spelling for spelling in spellings if spelling.strip()]
        targets: dict[Path, tuple[_RepoIndex, list[str] | None]] = {}
        loose: list[str] = []
        if not spellings:
            for member in membership.members:
                targets[member.root] = (self._repo_index(member), None)
            return targets, loose, membership.truncated

        def include(repo: WorkspaceRepo, scope: str | None) -> None:
            state = self._repo_index(repo)
            current = targets.get(repo.root)
            if scope is None:
                targets[repo.root] = (state, None)
            elif current is None:
                targets[repo.root] = (state, [scope])
            elif current[1] is not None:
                current[1].append(scope)

        truncated = False
        for raw in spellings:
            resolved = self.context.resolve(raw)
            checkout = workspace.checkout_for(resolved)
            contained = workspace.members_under(resolved)
            if checkout is not None:
                scope = resolved.relative_to(checkout.root).as_posix()
                include(checkout, None if scope == "." else scope)
            for member in contained:
                include(member, None)
            if checkout is None and not contained:
                loose.append(raw)
            truncated = truncated or (membership.truncated and resolved.is_dir())
        return targets, loose, truncated

    def _workspace_notes(
        self,
        building: Sequence[_RepoIndex],
        failed: Sequence[tuple[_RepoIndex, Exception]],
        *,
        truncated: bool,
    ) -> list[str]:
        notes: list[str] = []
        if building:
            parts: list[str] = []
            for state in building:
                seen = self._repo_progress.get(state.repo.relpath)
                if seen is not None and seen.phase == "scan" and seen.total:
                    parts.append(f"{_display(state.repo)} ({seen.done:,}/{seen.total:,} files)")
                else:
                    parts.append(_display(state.repo))
            notes.append(
                "[lc:index_building] Still indexing, so not searched yet: "
                + ", ".join(parts)
                + ". Retry shortly for an answer that covers them.\nretryable=true action=retry"
            )
        for state, exc in failed:
            notes.append(f"[lc:lite_failed] {_display(state.repo)} was not searched: {exc}")
        if truncated:
            notes.append(
                f"[lc:workspace_partial] Membership stopped at its bound ({MAX_REPOSITORIES} repositories); "
                "repositories past it were not searched. Pass paths=<repository> to reach one."
            )
        return notes

    @staticmethod
    def _coverage(
        ready: Sequence[_RepoIndex],
        building: Sequence[_RepoIndex],
        failed: Sequence[tuple[_RepoIndex, Exception]],
        *,
        truncated: bool,
    ) -> dict[str, Any]:
        return {
            "searched": [state.repo.relpath for state in ready],
            "building": [state.repo.relpath for state in building],
            "failed": [state.repo.relpath for state, _exc in failed],
            "truncated": truncated,
            "complete": not (building or failed or truncated),
        }

    def _workspace_search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "")
        if not query.strip():
            raise ValueError("code_search requires a non-empty query")
        requested = arguments.get("limit", arguments.get("max_files", 8))
        limit = max(1, min(32, requested)) if isinstance(requested, int) and not isinstance(requested, bool) else 8
        targets, loose, truncated = self._workspace_targets(arguments.get("paths"))
        ready, building, failed = self._await_repos(
            [state for state, _scopes in targets.values()], urgent=bool(arguments.get("paths"))
        )

        items: list[dict[str, Any]] = []
        tails: list[list[str]] = []
        related: list[list[dict[str, Any]]] = []
        for state in ready:
            repo = state.repo
            payload = state.index.search(query, paths=targets[repo.root][1], limit=limit)
            for raw in payload.get("files", []) or []:
                if isinstance(raw, Mapping):
                    item = dict(raw)
                    item["path"] = repo.prefixed(str(item.get("path") or ""))
                    item["repo"] = repo.relpath
                    items.append(item)
            tails.append([repo.prefixed(str(path)) for path in payload.get("candidate_files", []) or []])
            related.append(
                [
                    {**dict(symbol), "path": repo.prefixed(str(symbol.get("path") or ""))}
                    for symbol in payload.get("related_symbols", []) or []
                    if isinstance(symbol, Mapping)
                ]
            )
            self._schedule_index_refresh(repo.root, state.index)

        # lc-debt: per-member scores are merged as if comparable. Every member
        # uses the same deterministic fusion formula, but its BM25 terms are
        # relative to that member's corpus; upgrade path is rank-normalized
        # fusion or root-wide term statistics.
        items.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item["path"])))
        files = items[:limit]
        for rank, item in enumerate(files, start=1):
            if rank > _SNIPPET_FILES:
                item.pop("source", None)
        candidate_cap = min(32, max(16, limit * 3)) - len(files)
        candidates: list[str] = []
        for path in [*(str(item["path"]) for item in items[limit:]), *_round_robin(tails)]:
            if len(candidates) >= candidate_cap:
                break
            if path not in candidates:
                candidates.append(path)
        payload = {
            "query": query,
            "files": files,
            "candidate_files": candidates,
            "related_symbols": _round_robin(related)[:12],
            "lite": True,
            "workspace": self._coverage(ready, building, failed, truncated=truncated),
        }

        blocks: list[Mapping[str, Any]] = []
        notes = self._workspace_notes(building, failed, truncated=truncated)
        if notes:
            blocks.append({"type": "text", "text": "\n".join(notes)})
        if ready:
            blocks.append({"type": "text", "text": self._search_markdown(payload)})
        for scope in loose:
            # A path in no repository (a non-Git root's own files) has no index.
            blocks.append({"type": "text", "text": f"[no git: literal grep {scope}]"})
            local = local_search.run_grep(
                self.context,
                {"regex": re.escape(query), "path": scope, "file_limit": limit, "i": True},
            )
            blocks.extend(local.content)
        if not blocks:
            blocks.append({"type": "text", "text": "No member repositories to search."})
        return self._success(blocks, structured=payload, is_error=not ready and not loose)

    def _workspace_relations(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        symbol = str(arguments.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("relations requires symbol")
        kind = str(arguments.get("kind") or "usages")
        requested = arguments.get("limit", 20)
        limit = max(1, min(100, requested)) if isinstance(requested, int) and not isinstance(requested, bool) else 20
        targets, _loose, truncated = self._workspace_targets(None)
        ready, building, failed = self._await_repos([state for state, _scopes in targets.values()], urgent=False)
        groups: list[list[dict[str, Any]]] = []
        quality: object = None
        for state in ready:
            found = state.index.relations(symbol, kind=kind, limit=limit)
            quality = found.get("relation_quality", quality)
            groups.append(
                [
                    {**dict(item), "path": state.repo.prefixed(str(item.get("path") or "")), "repo": state.repo.relpath}
                    for item in found.get("items", []) or []
                    if isinstance(item, Mapping)
                ]
            )
            self._schedule_index_refresh(state.repo.root, state.index)
        payload: dict[str, Any] = {
            "symbol": symbol,
            "kind": kind,
            "items": _round_robin(groups)[:limit],
            "lite": True,
            "workspace": self._coverage(ready, building, failed, truncated=truncated),
        }
        if quality is not None:
            payload["relation_quality"] = quality
        text = "\n".join(
            [
                *self._workspace_notes(building, failed, truncated=truncated),
                json.dumps(payload, ensure_ascii=False, indent=2),
            ]
        )
        return self._success(({"type": "text", "text": text},), structured=payload, is_error=not ready)

    def _workspace_graph(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(arguments.get("kind") or "map")
        path = str(arguments.get("path") or "")
        requested = arguments.get("limit", 50)
        limit = max(1, min(200, requested)) if isinstance(requested, int) and not isinstance(requested, bool) else 50
        targets, loose, truncated = self._workspace_targets(path or None)
        ready, building, failed = self._await_repos([state for state, _scopes in targets.values()], urgent=bool(path))
        groups: list[list[dict[str, Any]]] = []
        for state in ready:
            scopes = targets[state.repo.root][1]
            found = state.index.repo_map(kind=kind, path=scopes[0] if scopes else "", limit=limit)
            groups.append(
                [
                    {**dict(item), "path": state.repo.prefixed(str(item.get("path") or ""))}
                    for item in found.get("items", []) or []
                    if isinstance(item, Mapping)
                ]
            )
        if kind == "imports":
            items = _round_robin(groups)[:limit]
        else:
            merged = [item for group in groups for item in group]
            merged.sort(key=lambda item: (-int(item.get("symbols") or 0), -int(item.get("imports") or 0), item["path"]))
            items = merged[:limit]
        payload = {
            "kind": "imports" if kind == "imports" else "map",
            "items": items,
            "lite": True,
            "workspace": self._coverage(ready, building, failed, truncated=truncated),
        }
        notes = self._workspace_notes(building, failed, truncated=truncated)
        notes.extend(f"[lc:git_required] {scope} is in no repository of this root; it has no index." for scope in loose)
        text = "\n".join([*notes, json.dumps(payload, ensure_ascii=False, indent=2)])
        return self._success(({"type": "text", "text": text},), structured=payload, is_error=not ready)

    def _dependency_graph(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """``graph kind=workspace|impact``: dependency edges between this root's repositories, or a change's reach."""

        from .. import workspace_graph  # loaded on first use: session start pays nothing

        if arguments.get("kind") == "workspace":
            graph = workspace_graph.build_graph(self.repo_root)
            return self._success(
                ({"type": "text", "text": workspace_graph.render_graph(graph)},), structured=graph.to_dict()
            )
        raw = arguments.get("paths")
        paths = [raw] if isinstance(raw, str) else [str(item) for item in raw or ()]
        changed: dict[str, list[str]] | None = None
        outside: tuple[str, ...] = ()
        if paths:
            changed, outside = workspace_graph.changes_for_paths(self.repo_root, paths)
        report = workspace_graph.impact(self.repo_root, changed)
        payload = {**report.to_dict(), "outside": list(outside)}
        notes = "".join(f"[lc:workspace_partial] not in a repository of this root: {path}\n" for path in outside)
        return self._success(
            ({"type": "text", "text": notes + workspace_graph.render_impact(report)},), structured=payload
        )

    def _workspace_read(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        symbol = arguments.get("symbol")
        names = [symbol] if isinstance(symbol, str) else [str(item) for item in symbol or ()]
        targets, _loose, truncated = self._workspace_targets(None)
        ready, building, failed = self._await_repos([state for state, _scopes in targets.values()], urgent=False)
        definitions: list[dict[str, Any]] = []
        for state in ready:
            for definition in state.index.exact_symbols(names):
                definitions.append(
                    {**definition, "path": state.repo.prefixed(str(definition["path"])), "repo": state.repo.relpath}
                )
        result = self._read(self._main, arguments, definitions=definitions[:32])
        notes = self._workspace_notes(building, failed, truncated=truncated)
        if notes:
            result["content"] = [{"type": "text", "text": "\n".join(notes)}, *result["content"]]
        return result

    def _workspace_blame(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        workspace = self._require_workspace()
        resolved = self.context.resolve(str(arguments.get("path") or ""))
        checkout = workspace.checkout_for(resolved)
        if checkout is None:
            return self._success(
                (
                    {
                        "type": "text",
                        "text": "[lc:git_required] blame needs a path inside one of this root's repositories.",
                    },
                ),
                is_error=True,
            )
        state = self._repo_index(checkout)
        result = local_vcs.run_blame(
            state.context, {**arguments, "path": resolved.relative_to(checkout.root).as_posix()}
        )
        return self._success(result.content, structured=result.structured, is_error=result.is_error)

    def _mark_workspace_dirty(self, name: str, arguments: Mapping[str, Any]) -> None:
        """Make a mutation visible to the next search of the checkouts it touched."""

        workspace = self._require_workspace()
        with self._repos_lock:
            opened = dict(self._repos)
        if name == "edit":
            touched: set[Path] = set()
            for raw in _path_values({"edits": arguments.get("edits")}):
                resolved = self._resolve_spelling(raw)
                checkout = workspace.checkout_for(resolved) if resolved is not None else None
                if checkout is not None:
                    touched.add(checkout.root)
            states = [opened[root] for root in touched if root in opened]
        else:
            # bash and codemod can write anywhere under the root.
            states = list(opened.values())
        for state in states:
            state.index.mark_dirty()

    def _names_root_checkout(self, arguments: Mapping[str, Any]) -> bool:
        """Whether a path of this call may lie in a checkout of the root repository.

        Paths into submodule members skip worktree routing: their ``.git`` file
        would otherwise make every such call re-list the root's worktrees.
        """

        workspace = self._require_workspace()
        for raw in _path_values(arguments):
            if not raw.strip():
                continue
            resolved = self._resolve_spelling(raw)
            checkout = workspace.checkout_for(resolved) if resolved is not None else None
            if checkout is None or checkout.parent is None:
                return True
        return False

    def _call_in_workspace(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self._git_workspace and self._names_root_checkout(arguments):
            # Linked worktrees of the root repository route as they always have.
            routed = self._catalog.route(arguments, active=None)
            if routed.worktree is not None:
                worktree = self._worktree_workspace(routed.worktree)
                result = self._call_workspace(worktree, name, routed.arguments)
                self._schedule_refresh(worktree)
                return self._with_worktree_header(result, routed.worktree)
            arguments = routed.arguments
        if name == "code_search":
            return self._workspace_search(arguments)
        if name == "relations":
            return self._workspace_relations(arguments)
        if name == "graph":
            return self._workspace_graph(arguments)
        if name == "read" and arguments.get("symbol"):
            return self._workspace_read(arguments)
        if name == "blame":
            return self._workspace_blame(arguments)
        # Files, edits and shell commands run against the root, so one call can
        # read or change several members.
        result = self._call_workspace(self._main, name, arguments)
        if name in _MUTATING_OR_UNKNOWN_TOOLS:
            self._mark_workspace_dirty(name, arguments)
        return result

    def _submodule_drift(self, members: Sequence[WorkspaceRepo]) -> set[str]:
        """Submodule members checked out at a commit other than the one their parent records."""

        by_parent: dict[str, list[WorkspaceRepo]] = {}
        for member in members:
            if member.parent is not None:
                by_parent.setdefault(member.parent, []).append(member)
        drifted: set[str] = set()
        for parent, submodules in by_parent.items():
            parent_root = self.repo_root / parent
            spelled = [member.root.relative_to(parent_root).as_posix() for member in submodules]
            recorded = recorded_commits(parent_root, spelled)
            for member, path in zip(submodules, spelled, strict=True):
                want, have = recorded.get(path, ""), head_commit(member.root)
                if want and have and want != have:
                    drifted.add(member.relpath)
        return drifted

    def _workspace_instructions(self) -> str:
        membership = self._require_workspace().membership()
        members = membership.members
        shown = members[:_INSTRUCTION_REPOS]
        drifted = self._submodule_drift(shown)
        names: list[str] = []
        for member in shown:
            details = [label] if (label := head_label(member.root)) else []
            if member.parent is not None:
                details.append(f"submodule of {member.parent}")
                if member.relpath in drifted:
                    details.append(f"not at the commit {member.parent} records")
            names.append(f"{member.relpath} ({', '.join(details)})" if details else member.relpath)
        listed = ", ".join(names) + (f", and {len(members) - len(shown)} more" if len(members) > len(shown) else "")
        if membership.git_root:
            parts = [
                "Local-only Runtime Lite for this Git repository (listed as .) and its checked-out submodules: "
                f"{len(members)} repositories, each with its own history and index: {listed}.",
                "Paths are relative to the repository root, including paths into submodules.",
            ]
        else:
            parts = [
                "Local-only Runtime Lite for a folder of Git repositories (the folder itself is not one): "
                f"{len(members)} repositories, each with its own history and index: {listed}.",
                "Paths are relative to this folder (<repository>/<path>); its own loose files are grep-only.",
            ]
        if membership.other_checkouts:
            others = [
                f"{checkout} (worktree of {member}, {head_label(self.repo_root / checkout) or 'unknown HEAD'})"
                for checkout, member in membership.other_checkouts
            ]
            parts.append(
                f"Other checkouts of these repositories, searched only when a path names them: {_listing(others)}."
            )
        if membership.unchecked:
            parts.append(f"Submodules not checked out (not searched): {_listing(list(membership.unchecked))}.")
        if membership.broken:
            parts.append(f"Skipped, Git directory missing: {_listing(list(membership.broken))}.")
        if membership.truncated:
            parts.append(f"Membership stopped at {MAX_REPOSITORIES} repositories; paths into others still work.")
        parts.append(
            "code_search without paths searches every listed repository; paths=<repository or folder> narrows it. "
            "Run git with bash cwd=<repository>; `lc workspace status|branch|commit|push|pr` does the same step in "
            "every changed repository at once (submodules first, one linked pull request per repository). "
            "graph kind=workspace shows which repositories depend on which; kind=impact shows which ones the "
            "uncommitted changes (or paths=<changed files>) reach."
        )
        if any(member.parent is not None for member in members):
            parts.append(
                "For a submodule: create a branch in it (submodules usually sit on a detached HEAD), commit and "
                "push there first, then commit the updated submodule in the repository that declares it."
            )
        return " ".join(parts) + " "

    def prepare(self, *, progress: ProgressCallback | None = None) -> dict[str, object]:
        if self._workspace is not None:
            states = [self._repo_index(member) for member in self._workspace.membership().members]
            futures = [(state, self._ensure_prepared(state, urgent=False)) for state in states]
            reports = {state.repo.relpath: future.result().to_dict() for state, future in futures}
            return {"ok": True, "mode": "workspace", "repositories": reports}
        if not self._git_workspace:
            return {"ok": True, "mode": "grep", "reason": "not a Git repository"}
        # Initialize only adopts the last durable WAL generation: proving it fresh
        # (git status) or refreshing it here would make a short-lived MCP session
        # pay for repository freshness even when it never used an indexed tool.
        # The first real tool call serves this generation and then schedules the
        # single session-owned refresh lane. With no generation yet, this builds
        # one, which start_prepare runs off the request path.
        return self.index.adopt_prepared(progress=progress).to_dict()

    def start_prepare(self) -> None:
        """Bring the main index current off the request path (once per process)."""

        if self._workspace is not None:
            for member in self._workspace.membership().members:
                self._ensure_prepared(self._repo_index(member), urgent=False)
            return
        if not self._git_workspace or self._prepare_thread is not None:
            return
        thread = threading.Thread(target=self._prepare_in_background, name="lite-index-prepare", daemon=True)
        self._prepare_thread = thread
        thread.start()

    def _prepare_in_background(self) -> None:
        try:
            self.prepare(progress=self._observe_prepare)
            self._start_semantic_prepare(self.repo_root)
        except _PrepareStopped:
            return
        except Exception as exc:  # surfaced by the next index-backed call
            self._prepare_error = exc

    def _start_semantic_prepare(self, root: Path, *, force: bool = False) -> None:
        if not self._semantic.active or self._semantic.ready(root):
            return
        thread = self._semantic_prepare_thread
        if thread is not None and thread.is_alive():
            return
        adopted = not force and self._semantic.adopt_prepared(root)
        self._semantic_prepare_root = root.resolve()
        thread = threading.Thread(
            target=self._prepare_semantic_in_background,
            args=(self._semantic_prepare_root, force, adopted),
            name="lite-semantic-prepare",
            daemon=True,
        )
        self._semantic_prepare_thread = thread
        thread.start()

    def _prepare_semantic_in_background(self, root: Path, force: bool, adopted: bool) -> None:
        try:
            if adopted:
                self._semantic.warm(root)
            else:
                self._semantic.prepare(root, force=force)
        finally:
            self._semantic_prepare_root = None

    def _observe_prepare(self, observed: LiteIndexProgress) -> None:
        self._prepare_progress = observed
        if self._prepare_stop.is_set():
            raise _PrepareStopped()

    def _needs_index(self, name: str, arguments: Mapping[str, Any]) -> bool:
        return name in _INDEX_TOOLS or (name == "read" and bool(arguments.get("symbol")))

    def _await_index(self) -> dict[str, Any] | None:
        """Wait briefly for the background build; report its progress rather than stall the host."""

        thread = self._prepare_thread
        if thread is not None:
            thread.join(_INDEX_WAIT_S)
            if thread.is_alive():
                return self._success(({"type": "text", "text": self._building_message()},), is_error=True)
        error, self._prepare_error = self._prepare_error, None
        if error is not None:
            raise error
        return None

    def _building_message(self) -> str:
        seen = self._prepare_progress
        if seen is None or seen.phase == "seed":
            detail = "starting"
        elif seen.phase == "lock":
            detail = "waiting for another lc process that is indexing this repository"
        elif seen.phase == "scan" and seen.total:
            detail = (
                f"{seen.done:,}/{seen.total:,} files ({seen.done * 100 // seen.total}%), {seen.elapsed_s:.0f}s elapsed"
            )
        elif seen.phase == "scan":
            detail = f"{seen.done:,} files walked, {seen.elapsed_s:.0f}s elapsed"
        else:
            detail = f"finishing, {seen.elapsed_s:.0f}s elapsed"
        return (
            f"[lc:index_building] Runtime Lite is building this repository's index in the background ({detail}). "
            "code_search, relations, graph and read symbol= need it: retry shortly. "
            "read files, grep, edit and bash work now.\n"
            "retryable=true action=retry"
        )

    def close(self) -> None:
        thread = self._prepare_thread
        if thread is not None and thread.is_alive():
            self._prepare_stop.set()
            thread.join(_PREPARE_STOP_WAIT_S)
        builds = self._builds
        if builds is not None:
            # Queued member builds are cancelled; a running one stops at its
            # next progress report and rolls back, bounded like the main build.
            self._prepare_stop.set()
            builds.close(_PREPARE_STOP_WAIT_S)
        self._refresh_executor.shutdown(wait=True, cancel_futures=False)
        self._semantic.close()
        semantic_thread = self._semantic_prepare_thread
        if semantic_thread is not None and semantic_thread.is_alive():
            semantic_thread.join(_PREPARE_STOP_WAIT_S)
        close_background(self.memory)
        with self._children_lock:
            children = list(self._children.values())
            self._children.clear()
        for workspace in children:
            close_background(workspace.memory)

    @staticmethod
    def _success(
        content: Sequence[Mapping[str, Any]],
        *,
        structured: Mapping[str, Any] | None = None,
        is_error: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"content": list(content), "isError": is_error}
        if structured is not None:
            result["structuredContent"] = dict(structured)
        return result

    @staticmethod
    def _with_worktree_header(result: dict[str, Any], worktree: LinkedWorktree) -> dict[str, Any]:
        rendered = dict(result)
        blocks = [dict(block) for block in rendered.get("content", []) if isinstance(block, Mapping)]
        prefix = f"[worktree {worktree.name}]"
        for index, block in enumerate(blocks):
            value = block.get("text")
            if block.get("type") == "text" and isinstance(value, str):
                block["text"] = f"{prefix}\n{value}" if value else prefix
                blocks[index] = block
                rendered["content"] = blocks
                return rendered
        blocks.insert(0, {"type": "text", "text": prefix})
        rendered["content"] = blocks
        return rendered

    def _run_local(self, workspace: _LiteWorkspace, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        executor = _LOCAL_EXECUTORS[name]
        result: LocalResult = executor(workspace.context, arguments)
        if name in _MUTATING_OR_UNKNOWN_TOOLS:
            workspace.index.mark_dirty()
        return self._success(result.content, structured=result.structured, is_error=result.is_error)

    def _confine_read_files(self, workspace: _LiteWorkspace, files: object) -> None:
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            raise ValueError("read files must be an array")
        from ..kit.read_path import split_file_opts

        for raw in files:
            if isinstance(raw, Mapping):
                spelled = str(raw.get("path") or raw.get("file_path") or raw.get("filePath") or "")
            else:
                spelled = str(raw)
            path, _line_range, _full, _head, _tail, _summary, _outline = split_file_opts(spelled)
            workspace.context.resolve(path)

    def _read(
        self,
        workspace: _LiteWorkspace,
        arguments: Mapping[str, Any],
        *,
        definitions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        blocks: list[Mapping[str, Any]] = []
        structured: dict[str, Any] = {}
        symbol = arguments.get("symbol")
        if symbol:
            names = [symbol] if isinstance(symbol, str) else [str(item) for item in symbol]
            if definitions is None:
                definitions = workspace.index.exact_symbols(names)
            structured["symbols"] = definitions
            for definition in definitions:
                path = str(definition["path"])
                line = int(definition["line"])
                start = max(1, line - 3)
                end = line + 24
                local = local_search.read_from_disk(
                    workspace.context,
                    {"files": [f"{path}:L{start}-L{end}"]},
                )
                blocks.extend(local.content)
        files = arguments.get("files")
        if files:
            self._confine_read_files(workspace, files)
            local = local_search.read_from_disk(workspace.context, {"files": files})
            blocks.extend(local.content)
        if not blocks and not structured:
            local = local_search.read_from_disk(workspace.context, arguments)
            blocks.extend(local.content)
        return self._success(blocks, structured=structured or None)

    @staticmethod
    def _search_markdown(payload: Mapping[str, Any]) -> str:
        sections: list[str] = []
        for item in payload.get("files", []) or []:
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path") or "")
            start = int(item.get("line") or 1)
            end = int(item.get("end_line") or start)
            sections.append(f"## {path}:L{start}-L{end}")
            source = str(item.get("source") or "")
            if source:
                sections.append(source)
            else:
                sections.append(f"→ read {path}:L{start}-L{end}")
        related = payload.get("related_symbols") or []
        if related:
            sections.append("related_symbols:")
            for item in related:
                if isinstance(item, Mapping):
                    sections.append(f"- {item.get('path')}:L{item.get('line')} {item.get('kind')} {item.get('name')}")
        candidates = payload.get("candidate_files") or []
        if candidates:
            sections.append("candidate_files: " + ", ".join(str(path) for path in candidates))
        rendered = "\n".join(sections) if sections else "No indexed matches."
        # The model should see the ranked evidence, not an unbounded dump.
        # Full source remains addressable through read path:Lx-Ly.
        max_chars = 12_000
        if len(rendered) > max_chars:
            rendered = (
                rendered[:max_chars].rsplit("\n", 1)[0]
                + "\n[lc:output_trimmed] More matches omitted; read the cited paths or narrow the query."
            )
        return rendered

    def _call_without_git(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        notice = "[no git: literal grep]"
        if name == "code_search":
            import re

            query = str(arguments.get("query") or "")
            if not query.strip():
                raise ValueError("code_search requires a non-empty query")
            paths = arguments.get("paths") or ["."]
            scopes = [paths] if isinstance(paths, str) else list(paths)
            requested = arguments.get("limit", 8)
            limit = max(1, min(32, requested)) if isinstance(requested, int) and not isinstance(requested, bool) else 8
            blocks: list[Mapping[str, Any]] = [{"type": "text", "text": notice}]
            for scope in scopes:
                self.context.resolve(str(scope))
                local = local_search.run_grep(
                    self.context,
                    {"regex": re.escape(query), "path": str(scope), "file_limit": limit, "i": True},
                )
                blocks.extend(local.content)
            return self._success(blocks, structured={"mode": "grep", "reason": "not a Git repository"})
        if name in {"relations", "graph", "blame"} or (name == "read" and arguments.get("symbol")):
            return self._success(
                (
                    {
                        "type": "text",
                        "text": (
                            f"[lc:git_required] {name} requires a Git repository and its index. "
                            "This directory is in grep fallback mode; use code_search and read files instead."
                        ),
                    },
                ),
                is_error=True,
            )
        return self._call_workspace(self._main, name, arguments)

    def _call_workspace(
        self,
        workspace: _LiteWorkspace,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if name in _LOCAL_EXECUTORS:
            return self._run_local(workspace, name, arguments)
        if name == "read":
            return self._read(workspace, arguments)
        if name == "code_search":
            query = str(arguments.get("query") or "")
            paths = arguments.get("paths")
            requested_limit = arguments.get("limit", arguments.get("max_files", 8))
            limit = (
                int(requested_limit)
                if isinstance(requested_limit, int) and not isinstance(requested_limit, bool)
                else 8
            )
            scoped_paths = paths if isinstance(paths, (str, list, tuple)) else None
            payload = workspace.index.search(query, paths=scoped_paths, limit=limit)
            if self._semantic.active and is_semantic_intent_query(query):
                semantic_matches = []
                semantic_thread = self._semantic_prepare_thread
                preparing = semantic_thread is not None and semantic_thread.is_alive()
                if self._semantic.ready(workspace.root):
                    semantic_matches = self._semantic.search(
                        workspace.root,
                        query,
                        paths=scoped_paths,
                        limit=100,
                    )
                elif not preparing:
                    if self._semantic.adopt_prepared(workspace.root):
                        semantic_matches = self._semantic.search(
                            workspace.root,
                            query,
                            paths=scoped_paths,
                            limit=100,
                        )
                    else:
                        self._start_semantic_prepare(workspace.root)
                if semantic_matches:
                    semantic_by_path = {match.path: match for match in semantic_matches}
                    semantic_order = [match.path for match in semantic_matches]
                    lexical_items = [dict(item) for item in payload.get("files", []) if isinstance(item, Mapping)]
                    lexical_by_path = {str(item.get("path") or ""): item for item in lexical_items}
                    lexical_order = [str(item.get("path") or "") for item in lexical_items]
                    lexical_order.extend(str(path) for path in payload.get("candidate_files", []) or [])
                    lexical_order = [path for path in lexical_order if path]
                    fused = fuse_ranked_paths(lexical_order, semantic_order)
                    rendered: list[dict[str, Any]] = []
                    remaining_chars = 12_000
                    for rank, path in enumerate(fused[:limit], start=1):
                        item = dict(lexical_by_path.get(path, {}))
                        item.setdefault("path", path)
                        channels = set(item.get("channels") or ())
                        semantic_match = semantic_by_path.get(path)
                        if semantic_match is not None:
                            channels.add("semantic")
                        item["channels"] = sorted(channels)
                        item.setdefault("score", 0.0)
                        if semantic_match is not None and path not in lexical_by_path:
                            item["line"] = semantic_match.line
                            item["end_line"] = semantic_match.end_line
                        else:
                            item.setdefault("line", 1)
                            item.setdefault("end_line", int(item.get("line") or 1))
                        if "source" not in item and rank <= 3 and remaining_chars > 0:
                            snippet, start_line, end_line = workspace.index._snippet(
                                path,
                                query,
                                int(item.get("line") or 1),
                            )
                            if snippet:
                                snippet = snippet[:remaining_chars]
                                remaining_chars -= len(snippet)
                                item["source"] = snippet
                                item["line"] = start_line
                                item["end_line"] = end_line
                        rendered.append(item)
                    payload = dict(payload)
                    payload["files"] = rendered
                    payload["candidate_files"] = fused[limit:32]
                    payload["semantic"] = {
                        "enabled": True,
                        "provider": "potion-code-16M-v2-int8",
                        "fused": True,
                    }
            result = self._success(
                ({"type": "text", "text": self._search_markdown(payload)},),
                structured=payload,
            )
            result["_lc_metrics"] = workspace.index.timing_snapshot()
            return result
        if name == "relations":
            symbol = str(arguments.get("symbol") or "").strip()
            if not symbol:
                raise ValueError("relations requires symbol")
            kind = str(arguments.get("kind") or "usages")
            limit = arguments.get("limit", 20)
            payload = workspace.index.relations(
                symbol,
                kind=kind,
                limit=int(limit) if isinstance(limit, int) and not isinstance(limit, bool) else 20,
            )
            return self._success(
                ({"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)},),
                structured=payload,
            )
        if name == "graph":
            kind = str(arguments.get("kind") or "map")
            path = str(arguments.get("path") or "")
            limit = arguments.get("limit", 50)
            payload = workspace.index.repo_map(
                kind=kind,
                path=path,
                limit=int(limit) if isinstance(limit, int) and not isinstance(limit, bool) else 50,
            )
            return self._success(
                ({"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)},),
                structured=payload,
            )
        raise AssertionError(f"unhandled Runtime Lite tool: {name}")

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        queue_wait_ms: float = 0.0,
    ) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        result = dict(self._call(name, arguments))
        raw_metrics = result.pop("_lc_metrics", {})
        metrics = dict(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
        metrics["queue_wait_ms"] = max(0.0, queue_wait_ms)
        self._record(name, arguments, started_at, started, result, metrics=metrics)
        return result

    def _record(
        self,
        name: str,
        arguments: Mapping[str, Any],
        started_at: datetime,
        started: float,
        result: Mapping[str, Any],
        *,
        metrics: Mapping[str, int | float],
    ) -> None:
        if not self._traces.enabled:
            return
        try:
            self._traces.record(
                trace_id=new_trace_id(),
                tool=name,
                arguments=arguments,
                started_at=started_at,
                duration_ms=round((time.perf_counter() - started) * 1000),
                site="lite",
                is_error=bool(result.get("isError")),
                degraded=False,
                degraded_reason="",
                output_chars=sum(
                    len(str(block.get("text") or ""))
                    for block in result.get("content") or ()
                    if isinstance(block, Mapping)
                ),
                session_id=self.session_id,
                view_revision=None,
                execution_metrics=metrics,
            )
        except Exception:  # a usage record must never fail a tool call
            return

    def _call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in LOCAL_LITE_TOOLS:
            return {
                "content": [{"type": "text", "text": f"[lc:tool_unknown] Runtime Lite does not expose {name!r}"}],
                "isError": True,
            }
        try:
            if name == "graph" and arguments.get("kind") in {"workspace", "impact"}:
                # Cross-repository edges come from the members' files, not an index.
                return self._dependency_graph(arguments)
            if self._workspace is not None:
                return self._call_in_workspace(name, arguments)
            if not self._git_workspace:
                return self._call_without_git(name, arguments)
            # There is deliberately no process-global "active worktree": several
            # subagents can share this MCP process, so sticky routing would let one
            # caller silently redirect another caller's relative/pathless request.
            routed = self._catalog.route(arguments, active=None)
            if routed.worktree is None and self._needs_index(name, routed.arguments):
                pending = self._await_index()
                if pending is not None:
                    return pending
            workspace = self._main if routed.worktree is None else self._worktree_workspace(routed.worktree)
            result = self._call_workspace(workspace, name, routed.arguments)
            self._schedule_refresh(workspace)
            if routed.worktree is not None:
                result = self._with_worktree_header(result, routed.worktree)
            return result
        except ClientError as exc:
            return self._success((error_block(exc),), is_error=True)
        except LiteIndexUpgradeRequired as exc:
            return self._success(
                ({"type": "text", "text": f"[lc:upgrade_required] {exc}\nretryable=false action=restart_host"},),
                is_error=True,
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            return {
                "content": [{"type": "text", "text": f"[lc:lite_failed] {exc}"}],
                "isError": True,
            }

    def answers_concurrently(self, message: Mapping[str, Any]) -> bool:
        if message.get("method") != "tools/call" or message.get("id") is None:
            return False
        params = message.get("params")
        if not isinstance(params, Mapping):
            return False
        name = str(params.get("name") or "")
        return name not in _SERIAL_TOOLS

    def handle(
        self,
        message: Mapping[str, Any],
        *,
        queue_wait_ms: float = 0.0,
    ) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if request_id is None:
            return None
        if method == "initialize":
            self.start_prepare()
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        (
                            self._workspace_instructions()
                            if self._workspace is not None
                            else (
                                "Local-only Runtime Lite for this Git worktree. "
                                if self._git_workspace
                                else "Local-only Runtime Lite. No Git: code_search uses literal grep; Git/index tools unavailable. "
                            )
                        )
                        + "No hosted connection, account, upload, daemon, or listening port is used."
                    ),
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools}}
        if method == "tools/call":
            params = message.get("params")
            params = params if isinstance(params, Mapping) else {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, Mapping) else {}
            result = self.call(name, arguments, queue_wait_ms=queue_wait_ms)
            # Model-facing boundary: ``content`` already carries the curated text.
            # Hosts such as Claude Code show ``structuredContent`` instead when it
            # is present, so the raw payload (scores, channels, generation) would
            # replace the Markdown. Programmatic callers keep it via ``call()``.
            result.pop("structuredContent", None)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }


def serve(*, source: TextIO | None = None, sink: TextIO | None = None) -> int:
    """Serve JSON-RPC over stdio with bounded concurrent tool dispatch."""

    source = source or sys.stdin
    sink = sink or sys.stdout
    server = LiteMcp()
    write_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_CALLS, thread_name_prefix="lc-lite-call")
    indexed_pool = ThreadPoolExecutor(
        max_workers=_MAX_CONCURRENT_INDEXED_CALLS,
        thread_name_prefix="lc-lite-index",
    )
    pending: set[Future[None]] = set()

    def emit(response: Mapping[str, Any]) -> None:
        with write_lock:
            sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sink.flush()

    def answer(message: Mapping[str, Any], enqueued_at: float) -> None:
        try:
            response = server.handle(
                message,
                queue_wait_ms=(time.perf_counter() - enqueued_at) * 1000.0,
            )
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": f"internal error: {type(exc).__name__}"},
            }
        if response is not None:
            emit(response)

    def drain() -> None:
        if not pending:
            return
        for future in tuple(pending):
            future.result()
        pending.clear()

    try:
        for raw in source:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, Mapping):
                    raise ValueError("JSON-RPC message must be an object")
            except (ValueError, json.JSONDecodeError) as exc:
                emit(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": str(exc)},
                    }
                )
                continue

            # Indexed tools use their own small pool: SQLite/FTS throughput
            # improves with a little overlap but falls sharply when every
            # subagent runs a CPU/SQLite-heavy search at once. Cheap local tools
            # keep the wider pool so reads and long bash calls do not block them.
            if server.answers_concurrently(message):
                pending = {future for future in pending if not future.done()}
                params = message.get("params")
                name = str(params.get("name") or "") if isinstance(params, Mapping) else ""
                executor = indexed_pool if name in _INDEX_TOOLS else pool
                pending.add(executor.submit(answer, message, time.perf_counter()))
                continue

            if message.get("method") == "tools/call":
                drain()
            response = server.handle(message)
            if response is not None:
                emit(response)
        drain()
    finally:
        indexed_pool.shutdown(wait=True, cancel_futures=False)
        pool.shutdown(wait=True, cancel_futures=False)
        server.close()
    return 0


__all__ = ["SERVER_NAME", "LiteMcp", "serve"]
