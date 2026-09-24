"""Tools that run on the developer's machine, and nothing else.

The execution matrix decides *where* a tool runs; this package is the *here*
side of it. Two rules shape every module in it:

**Nothing in this package reaches the network.** Only
:mod:`lemoncrow_client.transport` does. A local tool that wanted to fetch
something would be a second egress path, and the audit test exists to catch
exactly that.

**Nothing here downloads or installs a program.** A tool whose implementation
is an external binary -- ``ast-grep`` for :mod:`.astgrep`, ``git`` for
:mod:`.vcs` -- reports ``local_tool_unavailable`` with the program's name when
it is absent. That refusal is the feature: an install step a security review
never approved is how the current product got rejected.

An executor is a plain callable ``(LocalContext, arguments) -> LocalResult``.
The routing table names it by string, this package binds the name, and
``tests/test_routing.py`` asserts the two sets are equal -- so an executor
nobody routes to, or a route with no executor, fails the build.

No executor may launch another LemonCrow executable or MCP server. The legacy
``mcp`` proxy and ``agent``/``workflow`` delegation executors are deliberately
absent from this package; the host gets exactly one LemonCrow stdio process.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import ClientConfig
from ..errors import AgentAction, ClientError, ErrorCode
from ..kit.output_delta import RunHistory

__all__ = [
    "LocalContext",
    "LocalResult",
    "SessionMemory",
    "SyncTarget",
    "executor_for",
    "executor_names",
    "remap_aliases",
    "text_result",
]


class SyncTarget(Protocol):
    """What a local tool may ask of the remote session.

    Narrow on purpose: the local tools can push content and ask for the current
    revision, and that is all. Anything wider and a local tool would start
    making its own protocol decisions.
    """

    @property
    def online(self) -> bool: ...

    @property
    def view_revision(self) -> int: ...

    def push_paths(self, paths: tuple[str, ...]) -> int: ...

    def resync(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LocalResult:
    """What a local executor produced."""

    content: tuple[Mapping[str, Any], ...]
    is_error: bool = False
    #: Repository-relative paths this call wrote. The blob service pushes them
    #: and commits an overlay revision before the tool result is returned.
    changed_paths: tuple[str, ...] = ()
    degraded: bool = False
    degraded_reason: str = ""
    structured: Mapping[str, Any] | None = None


@dataclass(slots=True)
class SessionMemory:
    """What local executors remember between calls of one session.

    The remote MCP hosts many sessions in one process, so per-session state
    lives here and never in a module global, where one session would see
    another's.
    """

    #: Files the ``edit`` tool created; the test-contract guard lets a session
    #: rewrite a test it wrote itself.
    created_files: set[str] = field(default_factory=set)
    #: Output digests behind ``bash``'s ``unchanged`` rerun marker.
    command_runs: RunHistory = field(default_factory=RunHistory)


@dataclass(frozen=True, slots=True)
class LocalContext:
    """Everything a local executor is allowed to see."""

    config: ClientConfig
    repo_root: Path
    sync: SyncTarget | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    memory: SessionMemory = field(default_factory=SessionMemory)

    def resolve(self, relative: str) -> Path:
        """Resolve a repository-relative path, refusing every way out of it.

        A tool argument is agent-supplied text. ``..`` and an absolute path are
        refused before the join, and the resolved result is checked for
        containment afterwards, because a symlink anywhere along the way can
        still point outside the worktree.
        """
        candidate = relative.strip()
        if not candidate:
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "path must not be empty", action=AgentAction.FIX_REQUEST)
        path = Path(candidate)
        if path.is_absolute():
            try:
                resolved = path.resolve()
            except OSError as exc:
                raise ClientError(
                    ErrorCode.PAYLOAD_INVALID,
                    f"path is not resolvable: {candidate}",
                    action=AgentAction.FIX_REQUEST,
                ) from exc
        else:
            if any(segment == ".." for segment in path.parts):
                raise ClientError(
                    ErrorCode.PAYLOAD_INVALID,
                    "path must not contain '..' segments",
                    action=AgentAction.FIX_REQUEST,
                )
            resolved = (self.repo_root / path).resolve()
        root = self.repo_root.resolve()
        if not resolved.is_relative_to(root):
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                "path is outside the worktree this session opened",
                details={"path": candidate},
                action=AgentAction.FIX_REQUEST,
            )
        return resolved

    def resolve_read(self, spelled: str) -> Path:
        """Resolve a read target, allowing only explicitly absolute escapes.

        Repository-relative paths retain the normal containment and symlink
        checks. An absolute path is local-only authority supplied by the host;
        callers must never upload it into the server View.
        """
        candidate = spelled.strip()
        if not candidate:
            raise ClientError(ErrorCode.PAYLOAD_INVALID, "path must not be empty", action=AgentAction.FIX_REQUEST)
        path = Path(candidate)
        if not path.is_absolute():
            return self.resolve(candidate)
        try:
            return path.resolve()
        except OSError as exc:
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                f"path is not resolvable: {candidate}",
                action=AgentAction.FIX_REQUEST,
            ) from exc

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.repo_root.resolve()).as_posix()


def text_result(text: str, *, is_error: bool = False, **kwargs: Any) -> LocalResult:
    return LocalResult(content=({"type": "text", "text": text},), is_error=is_error, **kwargs)


def remap_aliases(arguments: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    """Old argument names to current ones; the current name wins, as in the main package."""
    remapped = dict(arguments)
    for old, new in aliases.items():
        if old in arguments and new not in arguments:
            remapped[new] = remapped.pop(old)
    return remapped


Executor = Callable[[LocalContext, Mapping[str, Any]], LocalResult]


def _build() -> Mapping[str, Executor]:
    from . import astgrep, editing, indexing, search, shell, sqltool, vcs

    return {
        "bash": shell.run_bash,
        "edit": editing.run_edit,
        "grep": search.run_grep,
        "read_from_disk": search.read_from_disk,
        "blame": vcs.run_blame,
        "scan": astgrep.run_scan,
        "codemod": astgrep.run_codemod,
        "sql": sqltool.run_sql,
        "index": indexing.run_index,
    }


_CACHE: dict[str, Mapping[str, Executor]] = {}


def _executors() -> Mapping[str, Executor]:
    cached = _CACHE.get("executors")
    if cached is None:
        cached = _build()
        _CACHE["executors"] = cached
    return cached


def executor_names() -> frozenset[str]:
    return frozenset(_executors())


def executor_for(name: str) -> Executor:
    executor = _executors().get(name)
    if executor is None:
        raise ClientError(
            ErrorCode.INTERNAL,
            f"no local executor named {name!r}",
            details={"executor": name},
            action=AgentAction.ABANDON,
        )
    return executor
