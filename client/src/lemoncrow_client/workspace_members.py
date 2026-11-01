"""Member repositories of a workspace root (Runtime Lite sessions, ``lc workspace``, Review impact).

A session root is served together with its member repositories:

* a Git repository: the repository itself plus its checked-out submodules,
  recursively (paths read from ``.gitmodules``);
* a folder that is not a Git repository: its immediate children that are Git
  repositories, each plus its checked-out submodules. Nothing deeper is
  scanned; a non-Git child folder is not explored.

Each member keeps its own Runtime Lite index in its own checkout, so a session
started inside a member and one started at the root share that index. Nothing
is written in a non-Git root. Every path the model sees is relative to the
root, so a search result can be passed straight to ``read`` or ``edit``.

A linked worktree is never a separate member: the root's children are grouped
by Git common directory and one checkout represents the repository; the others
are listed and stay reachable by path through their own index. Membership
reads only files (``.git``, ``HEAD``, ``commondir``, ``.gitmodules``); the one
subprocess here (``recorded_commits``) runs for session instructions only.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = [
    "MAX_REPOSITORIES",
    "Membership",
    "WorkspaceCatalog",
    "WorkspaceRepo",
    "discover_members",
    "git_toplevel",
    "head_commit",
    "head_label",
    "is_repository",
    "recorded_commits",
    "resolve_root",
]

#: Members one root indexes and searches without being named.
MAX_REPOSITORIES: Final[int] = 64
#: How long a membership read is trusted; a repository cloned later joins after this.
_CATALOG_TTL_S: Final[float] = 30.0
_GIT_TIMEOUT_S: Final[float] = 2.0
_SECTION: Final[re.Pattern[str]] = re.compile(r'^\[\s*submodule\s+"(?:[^"\\]|\\.)*"\s*\]$')
_PATH_KEY: Final[re.Pattern[str]] = re.compile(r"^path\s*=\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WorkspaceRepo:
    """One checkout: a member of the root, or a nested one a path reached."""

    root: Path
    #: POSIX path relative to the session root; ``.`` for the root itself.
    relpath: str
    #: For a submodule member, the relpath of the repository that declares it.
    parent: str | None = None

    def prefixed(self, path: str) -> str:
        """Turn a checkout-relative path into a root-relative one."""

        clean = path.strip().strip("/")
        if clean in {"", "."}:
            return self.relpath
        return clean if self.relpath == "." else f"{self.relpath}/{clean}"


@dataclass(frozen=True, slots=True)
class Membership:
    members: tuple[WorkspaceRepo, ...]
    #: The root is itself a Git repository (its first member is ``.``).
    git_root: bool
    #: ``(checkout, member)``: other checkouts of a member's repository (worktrees).
    other_checkouts: tuple[tuple[str, str], ...] = ()
    #: Declared submodules that are not checked out.
    unchecked: tuple[str, ...] = ()
    #: Worktrees or submodules whose Git directory is missing.
    broken: tuple[str, ...] = ()
    #: True when the member bound stopped the read; ``members`` is then incomplete.
    truncated: bool = False
    _by_root: dict[Path, WorkspaceRepo] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._by_root.update((member.root, member) for member in self.members)

    @property
    def multi(self) -> bool:
        """Whether this root needs member routing (a plain repository does not)."""

        return not self.git_root or len(self.members) > 1

    def member(self, root: Path) -> WorkspaceRepo | None:
        return self._by_root.get(root)


def _has_git_marker(path: Path) -> bool:
    # A ``.git`` directory is a clone; a ``.git`` file is a linked worktree or a
    # submodule checkout.
    return (path / ".git").exists()


def is_repository(path: Path) -> bool:
    """A checkout whose ``.git`` leads to a Git directory with a ``HEAD``.

    A stray ``.git`` folder (left by a tool, holding no repository) does not
    make its folder a repository: a folder of clones with one stays a folder.
    """

    gitdir = _git_dir(path) if _has_git_marker(path) else None
    return gitdir is not None and (gitdir / "HEAD").is_file()


def _git_dir(checkout: Path) -> Path | None:
    """The checkout's Git directory; ``None`` when it is missing (stale)."""

    marker = checkout / ".git"
    try:
        if marker.is_dir():
            return marker.resolve()
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip())
    if not gitdir.is_absolute():
        gitdir = checkout / gitdir
    return gitdir.resolve() if gitdir.is_dir() else None


def _common_dir(gitdir: Path) -> tuple[Path, bool]:
    """``(common directory, is a linked worktree)`` of a Git directory."""

    try:
        text = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return gitdir, False
    common = Path(text)
    return (common if common.is_absolute() else gitdir / common).resolve(), True


def _read_head(gitdir: Path) -> str:
    try:
        return (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def head_label(checkout: Path) -> str:
    """The checked-out branch (or short detached commit), read from files only.

    Session instructions name every member; a ``git`` subprocess per member
    would put dozens of process launches on the handshake.
    """

    gitdir = _git_dir(checkout)
    head = _read_head(gitdir) if gitdir is not None else ""
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/") :]
    if head.startswith("ref: "):
        return head[len("ref: ") :]
    return f"detached {head[:12]}" if head else ""


def head_commit(checkout: Path) -> str:
    """The commit the checkout's HEAD points at, from files only (``""`` if unknown)."""

    gitdir = _git_dir(checkout)
    if gitdir is None:
        return ""
    head = _read_head(gitdir)
    if not head.startswith("ref: "):
        return head
    ref = head[len("ref: ") :]
    common, _linked = _common_dir(gitdir)
    for base in (gitdir, common):
        try:
            return (base / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    try:
        packed = (common / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in packed.splitlines():
        sha, _space, name = line.partition(" ")
        if name == ref:
            return sha
    return ""


def recorded_commits(repo: Path, paths: list[str]) -> dict[str, str]:
    """The submodule commits ``repo``'s index records for ``paths`` (one ``git`` call)."""

    if not paths:
        return {}
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--stage", "-z", "--", *paths],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    recorded: dict[str, str] = {}
    for entry in completed.stdout.decode("utf-8", "replace").split("\0"):
        meta, _tab, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and fields[0] == "160000":
            recorded[path] = fields[1]
    return recorded


def _submodule_paths(checkout: Path) -> list[str]:
    """Submodule paths declared in the checkout's ``.gitmodules``, sorted."""

    try:
        text = (checkout / ".gitmodules").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    paths: set[str] = set()
    in_submodule = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.startswith("["):
            in_submodule = _SECTION.match(stripped) is not None
            continue
        match = _PATH_KEY.match(stripped) if in_submodule else None
        if match is None:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        value = value.strip().strip("/")
        if value:
            paths.add(value)
    return sorted(paths)


class _Builder:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.members: list[WorkspaceRepo] = []
        self.unchecked: list[str] = []
        self.broken: list[str] = []
        self.truncated = False

    def relpath(self, path: Path) -> str:
        return "." if path == self.base else path.relative_to(self.base).as_posix()

    def add(self, checkout: Path, parent: str | None) -> None:
        """Add a member, then its checked-out submodules, depth first."""

        if len(self.members) >= MAX_REPOSITORIES:
            self.truncated = True
            return
        member = WorkspaceRepo(root=checkout, relpath=self.relpath(checkout), parent=parent)
        self.members.append(member)
        for declared in _submodule_paths(checkout):
            path = checkout / declared
            try:
                if path.is_symlink():
                    continue
                resolved = path.resolve()
            except (OSError, RuntimeError):
                continue
            if checkout not in resolved.parents:
                continue  # ``..`` or absolute: never outside the declaring checkout
            if not (resolved / ".git").exists():
                self.unchecked.append(self.relpath(resolved))
            elif _git_dir(resolved) is None:
                self.broken.append(self.relpath(resolved))
            else:
                self.add(resolved, member.relpath)


def git_toplevel(cwd: Path) -> Path | None:
    """The top level of the work tree holding ``cwd``, or ``None`` outside one."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout.strip()
    return Path(output).expanduser().resolve() if completed.returncode == 0 and output else None


def resolve_root(path: Path) -> Path:
    """The workspace root a folder stands for: a folder inside a work tree is that repository's top level."""

    root = path.expanduser().resolve()
    if root.is_dir() and not is_repository(root):
        root = git_toplevel(root) or root
    return root


def discover_members(root: Path) -> Membership:
    """The member repositories of ``root`` (see the module docstring)."""

    base = root.resolve()
    builder = _Builder(base)
    if is_repository(base):
        builder.add(base, None)
        return Membership(
            members=tuple(builder.members),
            git_root=True,
            unchecked=tuple(builder.unchecked),
            broken=tuple(builder.broken),
            truncated=builder.truncated,
        )

    try:
        with os.scandir(base) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError:
        entries = []
    # Checkouts grouped by repository (Git common directory), in name order.
    groups: dict[Path, list[tuple[Path, Path, bool]]] = {}
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        child = Path(entry.path)
        if not _has_git_marker(child):
            continue
        gitdir = _git_dir(child)
        if gitdir is None or not is_repository(child):
            builder.broken.append(builder.relpath(child))
            continue
        common, linked = _common_dir(gitdir)
        groups.setdefault(common, []).append((child, gitdir, not linked))

    chosen: dict[Path, Path] = {}
    others: list[tuple[str, str]] = []
    for common, checkouts in groups.items():
        main = next((child for child, _gitdir, is_main in checkouts if is_main), None)
        if main is None:
            # A bare repository with worktrees: its default branch, else the first.
            default = _read_head(common)
            main = next(
                (child for child, gitdir, _is_main in checkouts if default and _read_head(gitdir) == default),
                checkouts[0][0],
            )
        for child, _gitdir, _is_main in checkouts:
            if child == main:
                chosen[child] = child
            else:
                others.append((builder.relpath(child), builder.relpath(main)))
    for entry in entries:
        child = Path(entry.path)
        if child in chosen:
            builder.add(child.resolve(), None)
    return Membership(
        members=tuple(builder.members),
        git_root=False,
        other_checkouts=tuple(sorted(others)),
        unchecked=tuple(builder.unchecked),
        broken=tuple(sorted(builder.broken)),
        truncated=builder.truncated,
    )


class WorkspaceCatalog:
    """The members of one session root, re-read on a short TTL."""

    __slots__ = ("_at", "_lock", "_membership", "root")

    def __init__(self, root: Path, membership: Membership | None = None) -> None:
        self.root = root.resolve()
        self._lock = threading.Lock()
        self._membership = membership if membership is not None else discover_members(self.root)
        self._at = time.monotonic()

    def membership(self, *, refresh: bool = False) -> Membership:
        with self._lock:
            now = time.monotonic()
            if refresh or now - self._at >= _CATALOG_TTL_S:
                self._membership = discover_members(self.root)
                self._at = now
            return self._membership

    def checkout_for(self, path: Path) -> WorkspaceRepo | None:
        """The checkout containing ``path`` (resolved, inside the root), if any.

        The nearest ``.git`` wins, so a submodule, a nested worktree or an
        undeclared nested clone answers from its own index. A member is
        returned as listed; a non-member checkout is returned without a parent.
        """

        if path != self.root and self.root not in path.parents:
            return None
        membership = self.membership()
        for directory in (path, *path.parents):
            member = membership.member(directory)
            if member is not None:
                return member
            if is_repository(directory):
                relpath = "." if directory == self.root else directory.relative_to(self.root).as_posix()
                return WorkspaceRepo(root=directory, relpath=relpath)
            if directory == self.root:
                return None
        return None

    def members_under(self, path: Path) -> tuple[WorkspaceRepo, ...]:
        """Members strictly below ``path``."""

        return tuple(member for member in self.membership().members if path in member.root.parents)
