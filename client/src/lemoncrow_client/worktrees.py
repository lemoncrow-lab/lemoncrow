"""Path-bearing tool arguments select the linked worktree a call is about.

The thin client serves exactly one repository. project_id therefore remains
outside this module: the dispatcher rejects it because project registration is a
persistent-gateway concern.

Worktree selection instead uses paths the tool already carries. Only checkouts
reported by git worktree list are eligible. Relative paths follow the session's
active linked worktree; an explicit path into another checkout switches context.
Calls that do not identify a checkout reuse the active worktree.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .errors import AgentAction, ClientError, ErrorCode
from .kit.read_path import split_file_opts

__all__ = ["LinkedWorktree", "WorktreeCatalog", "WorktreeRoute"]

#: How long git worktree list is trusted. Worktrees are added by hand, so a
#: stale answer only ever delays routing to a brand-new worktree by this long.
_CATALOG_TTL_S: Final[float] = 15.0
_GIT_TIMEOUT_S: Final[float] = 3.0
_PATH_FIELDS: Final[tuple[str, ...]] = ("path", "cwd")


@dataclass(frozen=True, slots=True)
class LinkedWorktree:
    root: Path
    branch: str

    @property
    def name(self) -> str:
        return self.root.name

    def describe(self) -> str:
        return f"{self.name} ({self.branch})" if self.branch else self.name


@dataclass(frozen=True, slots=True)
class WorktreeRoute:
    """Resolved checkout plus arguments as that checkout should receive them."""

    worktree: LinkedWorktree | None
    arguments: Mapping[str, Any]
    #: True only when one of this call's paths selected a registered checkout.
    explicit: bool = False
    #: True when the call inherited a linked worktree without selecting one.
    sticky: bool = False


class WorktreeCatalog:
    """The checkouts of this repository, with path-only routing semantics."""

    __slots__ = ("_at", "_entries", "_lock", "_repo_root")

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root.resolve()
        self._entries: tuple[LinkedWorktree, ...] = ()
        self._at = float("-inf")
        self._lock = threading.Lock()

    def entries(self) -> tuple[LinkedWorktree, ...]:
        """Every checkout git worktree list reports, this session's included."""
        with self._lock:
            now = time.monotonic()
            if now - self._at >= _CATALOG_TTL_S:
                self._entries = _git_worktrees(self._repo_root)
                self._at = now
            return self._entries

    def others(self) -> tuple[LinkedWorktree, ...]:
        return tuple(entry for entry in self.entries() if entry.root != self._repo_root)

    def containing(self, path: Path) -> LinkedWorktree | None:
        """Return the linked worktree containing path, if any."""
        registered, worktree = self._checkout_of(path if path.is_absolute() else self._repo_root / path)
        return worktree if registered else None

    def route(
        self,
        arguments: Mapping[str, Any],
        *,
        active: LinkedWorktree | None = None,
    ) -> WorktreeRoute:
        """Resolve a call using only registered checkout paths.

        Paths that belong to a checkout select it explicitly. A relative path is
        interpreted in the active linked worktree unless it explicitly spells a
        different linked worktree from the repository root. Paths outside every
        registered checkout do not change checkout state.

        A call whose registered paths span more than one checkout is rejected:
        running it in either session would answer for the wrong tree.
        """
        targets: dict[Path, LinkedWorktree | None] = {}
        for raw in _path_values(arguments):
            if not raw:
                continue
            registered, found = self._target_of(raw, active)
            if not registered:
                continue
            targets.setdefault(found.root if found else self._repo_root, found)

        if len(targets) > 1:
            raise _spans(targets)

        if targets:
            chosen = next(iter(targets.values()))
            explicit = True
        else:
            chosen = active
            explicit = False

        if chosen is None:
            # An explicit path back into the primary checkout still needs the
            # same rebasing as a linked worktree. Otherwise an absolute main
            # path leaks into index scopes such as code_search(paths=...) and
            # matches no repository-relative rows.
            rebased = (
                self._rebase(arguments, LinkedWorktree(root=self._repo_root, branch="")) if targets else dict(arguments)
            )
        else:
            rebased = self._rebase(arguments, chosen)
        return WorktreeRoute(
            worktree=chosen,
            arguments=rebased,
            explicit=explicit,
            sticky=not explicit and chosen is not None,
        )

    def _checkout_of(self, path: Path) -> tuple[bool, LinkedWorktree | None]:
        """Return (is registered checkout path, linked worktree-or-main)."""
        resolved = _resolve(path)
        best: LinkedWorktree | None = None
        for entry in self.entries():
            if resolved == entry.root or entry.root in resolved.parents:
                if best is None or len(entry.root.parts) > len(best.root.parts):
                    best = entry
        if best is None:
            return False, None
        return True, None if best.root == self._repo_root else best

    def _target_of(self, raw: str, active: LinkedWorktree | None) -> tuple[bool, LinkedWorktree | None]:
        path = Path(split_file_opts(raw)[0]).expanduser()
        if path.is_absolute():
            return self._checkout_of(path)
        if active is not None:
            # A repo-relative spelling such as .lc-worktrees/other/pkg wins.
            # Ordinary relative paths stay relative to the active worktree.
            registered, spelled = self._checkout_of(self._repo_root / path)
            if registered and spelled is not None:
                return True, spelled
            return self._checkout_of(active.root / path)
        return self._checkout_of(self._repo_root / path)

    def _rebase(self, arguments: Mapping[str, Any], worktree: LinkedWorktree) -> dict[str, Any]:
        """Make paths that name worktree relative to its own session root."""
        rebased: dict[str, Any] = dict(arguments)

        def fix(raw: str) -> str:
            return self._relative_to(raw, worktree)

        for key in _PATH_FIELDS:
            if isinstance(rebased.get(key), str) and rebased[key]:
                rebased[key] = fix(rebased[key])
        if "paths" in rebased:
            rebased["paths"] = _map_strings(rebased["paths"], fix)
        if "files" in rebased:
            rebased["files"] = _map_entries(rebased["files"], fix)
        if isinstance(rebased.get("edits"), Sequence) and not isinstance(rebased["edits"], (str, bytes)):
            rebased["edits"] = [_map_entry(edit, fix) for edit in rebased["edits"]]
        return rebased

    def _relative_to(self, raw: str, worktree: LinkedWorktree) -> str:
        head, suffix = _split_suffix(raw)
        path = Path(head).expanduser()
        if not path.is_absolute():
            candidate = _resolve(self._repo_root / path)
            if candidate != worktree.root and worktree.root not in candidate.parents:
                return raw  # already relative to the active worktree
            path = candidate
        resolved = _resolve(path)
        if resolved != worktree.root and worktree.root not in resolved.parents:
            return raw
        relative = PurePosixPath(*resolved.relative_to(worktree.root).parts) if resolved != worktree.root else "."
        return f"{relative}{suffix}"


def _spans(targets: Mapping[Path, LinkedWorktree | None]) -> ClientError:
    names = sorted({found.describe() if found else "this checkout" for found in targets.values()})
    return ClientError(
        ErrorCode.LOCAL_TOOL_UNAVAILABLE,
        "one call spans several worktrees",
        details={"detail": "split the call per worktree: " + ", ".join(names)},
        action=AgentAction.FIX_REQUEST,
    )


def _split_suffix(raw: str) -> tuple[str, str]:
    head = split_file_opts(raw)[0]
    return head, raw[len(head) :]


def _path_values(arguments: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _PATH_FIELDS:
        value = arguments.get(key)
        if isinstance(value, str):
            values.append(value)
    values.extend(_strings(arguments.get("paths")))
    for entry in _entries(arguments.get("files")):
        values.append(_entry_path(entry))
    for entry in _entries(arguments.get("edits")):
        values.append(_entry_path(entry))
    return values


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [item for item in value if isinstance(item, str)]
    return []


def _entries(value: object) -> list[object]:
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return list(value)
    return []


def _entry_path(entry: object) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Mapping):
        for key in ("path", "file_path", "filePath"):
            found = entry.get(key)
            if isinstance(found, str):
                return found
    return ""


def _map_strings(value: Any, fix: Any) -> Any:
    if isinstance(value, str):
        return fix(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [fix(item) if isinstance(item, str) else item for item in value]
    return value


def _map_entry(entry: Any, fix: Any) -> Any:
    if isinstance(entry, str):
        return fix(entry)
    if isinstance(entry, Mapping):
        mapped = dict(entry)
        for key in ("path", "file_path", "filePath"):
            if isinstance(mapped.get(key), str):
                mapped[key] = fix(mapped[key])
        return mapped
    return entry


def _map_entries(value: Any, fix: Any) -> Any:
    if isinstance(value, (str, Mapping)):
        return _map_entry(value, fix)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_map_entry(entry, fix) for entry in value]
    return value


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(path.absolute())


def _git_worktrees(repo_root: Path) -> tuple[LinkedWorktree, ...]:
    try:
        completed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    found: list[LinkedWorktree] = []
    root: Path | None = None
    branch = ""
    for line in [*completed.stdout.decode("utf-8", "replace").splitlines(), ""]:
        if line.startswith("worktree "):
            root, branch = _resolve(Path(line[len("worktree ") :])), ""
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif not line and root is not None:
            found.append(LinkedWorktree(root=root, branch=branch))
            root = None
    return tuple(found)
