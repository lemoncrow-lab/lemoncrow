"""``lc workspace status|branch|commit|push|pr`` (P2 of docs/planning/2026-11-01-multi-repo-workspace.md).

The targets are the members of a workspace root (:func:`.workspace_members.discover_members`). Every action takes
``--root DIR`` and ``--only REPO,...`` (root-relative member paths). ``commit`` and ``push`` go deepest submodule
first, so a repository that records a submodule's commit is only committed or pushed after that commit is. Nothing is
forced, and nothing leaves the machine unless ``push`` or ``pr`` runs.

Exit codes: 0 every member ok, 1 some member failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn, TextIO

from .workspace_members import WorkspaceRepo, discover_members, head_commit, recorded_commits, resolve_root

__all__ = ["run"]

_GIT_TIMEOUT_S: Final[float] = 30.0
_PUSH_TIMEOUT_S: Final[float] = 180.0
_GH_TIMEOUT_S: Final[float] = 90.0
#: lc-debt: fixed worker count for per-member steps; make it a flag if roots grow past the 64-member bound.
_POOL_SIZE: Final[int] = 4
_RELATED_HEADING: Final[str] = "## Related pull requests"
_RELATED_SECTION: Final[re.Pattern[str]] = re.compile(r"(?:^|\n)## Related pull requests\n.*\Z", re.DOTALL)
_FALLBACK_DEFAULTS: Final[frozenset[str]] = frozenset({"main", "master"})


class _Usage(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    """An argument parser that writes to the sink and reports its exit code instead of exiting."""

    def __init__(self, sink: TextIO, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._sink = sink

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            self._sink.write(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            self._sink.write(message)
        raise _Usage(status)

    def error(self, message: str) -> NoReturn:
        self._sink.write(self.format_usage())
        self._sink.write(f"{self.prog}: error: {message}\n")
        raise _Usage(2)


@dataclass(frozen=True, slots=True)
class _Run:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def reason(self) -> str:
        """The most telling line of a failed command's output."""

        lines = [line.strip() for line in (self.err or self.out).splitlines() if line.strip()]
        for line in lines:
            if line.startswith(("fatal:", "error:")):
                return line
        return lines[-1] if lines else f"exit {self.code}"


def _exec(args: Sequence[str], cwd: Path, timeout: float, env: dict[str, str] | None = None) -> _Run:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=None if env is None else {**os.environ, **env},
        )
    except subprocess.TimeoutExpired:
        return _Run(124, "", f"timed out after {timeout:g}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return _Run(127, "", str(exc))
    return _Run(completed.returncode, completed.stdout, completed.stderr)


def _git(root: Path, *args: str, timeout: float = _GIT_TIMEOUT_S, env: dict[str, str] | None = None) -> _Run:
    return _exec(["git", *args], root, timeout, env)


@dataclass(slots=True)
class _Status:
    path: str
    parent: str | None
    #: ``None`` when HEAD is detached.
    branch: str | None = None
    commit: str = ""
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    conflicts: int = 0
    #: Submodules only: the checkout's HEAD differs from the commit the parent records.
    drift: bool | None = None
    recorded: str | None = None
    error: str | None = None

    @property
    def dirty(self) -> bool:
        return (self.staged + self.unstaged + self.untracked + self.conflicts) > 0

    @property
    def changed(self) -> bool:
        return self.dirty or (self.ahead or 0) > 0 or self.drift is True

    def as_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "parent": self.parent,
            "branch": self.branch,
            "detached": self.branch is None and self.error is None,
            "commit": self.commit,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "staged": self.staged,
            "unstaged": self.unstaged,
            "untracked": self.untracked,
            "conflicts": self.conflicts,
            "drift": self.drift,
            "recorded": self.recorded,
            "changed": self.changed,
            "error": self.error,
        }

    def line(self) -> str:
        if self.error is not None:
            return f"{self.path}: error: {self.error}"
        head = f"detached @{self.commit[:7]}" if self.branch is None else self.branch
        if self.upstream is None:
            track = "no upstream"
        elif self.ahead is None:
            track = f"{self.upstream} (gone)"
        else:
            track = f"{self.upstream}, ahead {self.ahead} behind {self.behind}"
        counts = [f"{n} {label}" for n, label in self._counts() if n]
        parts = [f"{head} ({track})", ", ".join(counts) if counts else "clean"]
        if self.drift is True:
            parts.append(f"drifted: HEAD {self.commit[:7]}, parent records {(self.recorded or '?')[:7]}")
        elif self.drift is False:
            parts.append("in sync with parent")
        return f"{self.path}: " + "; ".join(parts)

    def _counts(self) -> list[tuple[int, str]]:
        return [
            (self.staged, "staged"),
            (self.unstaged, "unstaged"),
            (self.untracked, "untracked"),
            (self.conflicts, "conflicted"),
        ]


def _parse_status(status: _Status, output: str) -> None:
    tokens = output.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if token.startswith("# "):
            key, _space, value = token[2:].partition(" ")
            if key == "branch.oid":
                status.commit = "" if value == "(initial)" else value
            elif key == "branch.head":
                status.branch = None if value == "(detached)" else value
            elif key == "branch.upstream":
                status.upstream = value
            elif key == "branch.ab":
                ahead, _space, behind = value.partition(" ")
                status.ahead = int(ahead.lstrip("+") or 0)
                status.behind = int(behind.lstrip("-") or 0)
            continue
        kind = token[0]
        if kind in "12":
            xy = token[2:4]
            status.staged += xy[0] != "."
            status.unstaged += xy[1] != "."
            if kind == "2":
                index += 1  # the original path follows its own NUL
        elif kind == "u":
            status.conflicts += 1
        elif kind == "?":
            status.untracked += 1


@dataclass(frozen=True, slots=True)
class _Scope:
    root: Path
    #: Every member, in discovery order (parents before their submodules).
    members: tuple[WorkspaceRepo, ...]
    #: The members the command acts on (all of them unless ``--only``).
    chosen: tuple[WorkspaceRepo, ...]
    explicit: bool
    truncated: bool

    def by_path(self) -> dict[str, WorkspaceRepo]:
        return {member.relpath: member for member in self.members}

    def ancestors(self, relpath: str) -> list[str]:
        by_path = self.by_path()
        found: list[str] = []
        parent = by_path[relpath].parent
        while parent is not None and parent in by_path and parent not in found:
            found.append(parent)
            parent = by_path[parent].parent
        return found

    def depth(self, relpath: str) -> int:
        return len(self.ancestors(relpath))

    def deepest_first(self, members: Sequence[WorkspaceRepo]) -> list[WorkspaceRepo]:
        return sorted(members, key=lambda member: (-self.depth(member.relpath), member.relpath))


def _status_of(member: WorkspaceRepo, scope: _Scope) -> _Status:
    status = _Status(path=member.relpath, parent=member.parent)
    # ``dirty`` keeps a submodule's own changes out of its parent's counts while still showing a moved pointer.
    result = _git(member.root, "status", "--porcelain=v2", "--branch", "-z", "--ignore-submodules=dirty")
    if not result.ok:
        status.error = result.reason
        return status
    _parse_status(status, result.out)
    if member.parent is not None:
        parent = scope.by_path().get(member.parent)
        if parent is not None:
            sub = member.root.relative_to(parent.root).as_posix()
            recorded = recorded_commits(parent.root, [sub]).get(sub)
            head = status.commit or head_commit(member.root)
            status.recorded = recorded
            if recorded is not None and head:
                status.drift = recorded != head
    return status


def _parallel[T](items: Sequence[T], function: Callable[[T], Any]) -> list[Any]:
    if len(items) <= 1:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=_POOL_SIZE) as pool:
        return list(pool.map(function, items))


def _collect(scope: _Scope, members: Sequence[WorkspaceRepo]) -> dict[str, _Status]:
    statuses = _parallel(list(members), lambda member: _status_of(member, scope))
    return {status.path: status for status in statuses}


@dataclass(slots=True)
class _Outcome:
    path: str
    #: ``ok``, ``skipped`` or ``failed``.
    state: str
    message: str
    url: str | None = None
    #: Pull-request body the related-pull-requests section is appended to.
    body: str = ""
    created: bool = False

    def line(self) -> str:
        if self.state == "failed":
            return f"{self.path}: FAILED: {self.message}"
        if self.state == "skipped":
            return f"{self.path}: skipped, {self.message}"
        return f"{self.path}: {self.message}"

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": self.path, "state": self.state, "message": self.message}
        if self.url is not None:
            payload["url"] = self.url
        return payload


def _ok(path: str, message: str) -> _Outcome:
    return _Outcome(path, "ok", message)


def _skip(path: str, message: str) -> _Outcome:
    return _Outcome(path, "skipped", message)


def _fail(path: str, message: str) -> _Outcome:
    return _Outcome(path, "failed", message)


def _emit(action: str, scope: _Scope, outcomes: Sequence[_Outcome], as_json: bool, sink: TextIO) -> int:
    failed = any(outcome.state == "failed" for outcome in outcomes)
    if as_json:
        payload = {
            "action": action,
            "root": str(scope.root),
            "truncated": scope.truncated,
            "results": [outcome.as_json() for outcome in outcomes],
        }
        sink.write(json.dumps(payload, indent=2) + "\n")
    else:
        if scope.truncated:
            sink.write("note: more repositories than the member bound; only the first are listed\n")
        for outcome in outcomes:
            sink.write(outcome.line() + "\n")
        if not outcomes:
            sink.write(f"nothing to {action}\n")
    return 1 if failed else 0


def _build_parser(action: str, sink: TextIO) -> _Parser:
    parser = _Parser(sink, prog=f"lc workspace {action}")
    parser.add_argument("--root", default=None, metavar="DIR", help="workspace root (default: the current folder)")
    parser.add_argument("--only", action="append", default=[], metavar="REPO,...", help="root-relative members")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    if action == "branch":
        parser.add_argument("name", metavar="NAME")
    elif action == "commit":
        parser.add_argument("-m", "--message", required=True)
    elif action == "pr":
        parser.add_argument("--title", default=None)
        parser.add_argument("--body", default=None)
        parser.add_argument("--draft", action="store_true")
        parser.add_argument("--base", default=None, metavar="BRANCH")
    return parser


def _scope(namespace: argparse.Namespace, sink: TextIO) -> _Scope:
    root = resolve_root(Path(namespace.root) if namespace.root else Path.cwd())
    if not root.is_dir():
        sink.write(f"lc workspace: not a folder: {root}\n")
        raise _Usage(2)
    membership = discover_members(root)
    if not membership.members:
        sink.write(f"lc workspace: no Git repositories under {root}\n")
        raise _Usage(2)
    names: list[str] = []
    for entry in namespace.only:
        for name in entry.split(","):
            clean = name.strip().strip("/")
            if clean.startswith("./"):
                clean = clean[2:]
            if clean:
                names.append(clean)
    members = membership.members
    known = {member.relpath for member in members}
    unknown = [name for name in names if name not in known]
    if unknown:
        sink.write(f"lc workspace: unknown repository: {', '.join(unknown)}\n")
        sink.write(f"members: {', '.join(member.relpath for member in members)}\n")
        raise _Usage(2)
    chosen = tuple(member for member in members if member.relpath in set(names)) if names else members
    return _Scope(root, members, chosen, bool(names), membership.truncated)


def _default_targets(scope: _Scope, statuses: dict[str, _Status]) -> list[WorkspaceRepo]:
    """``--only`` members, else the changed ones plus every repository holding a changed submodule."""

    if scope.explicit:
        return list(scope.chosen)
    paths: set[str] = set()
    for member in scope.chosen:
        if statuses[member.relpath].changed:
            paths.add(member.relpath)
            paths.update(scope.ancestors(member.relpath))
    return [member for member in scope.chosen if member.relpath in paths]


def _blocked_by(scope: _Scope, relpath: str, failed: set[str]) -> str | None:
    """A failed target below ``relpath`` (its submodule), if any."""

    for path in sorted(failed):
        if relpath in scope.ancestors(path):
            return path
    return None


# --- status -----------------------------------------------------------------------------------------------------


def _status(scope: _Scope, namespace: argparse.Namespace, sink: TextIO) -> int:
    statuses = _collect(scope, scope.chosen)
    rows = [statuses[member.relpath] for member in scope.chosen]
    failed = any(row.error is not None for row in rows)
    if namespace.json:
        payload = {
            "root": str(scope.root),
            "truncated": scope.truncated,
            "members": [row.as_json() for row in rows],
        }
        sink.write(json.dumps(payload, indent=2) + "\n")
    else:
        if scope.truncated:
            sink.write("note: more repositories than the member bound; only the first are listed\n")
        for row in rows:
            sink.write(row.line() + "\n")
    return 1 if failed else 0


# --- branch -----------------------------------------------------------------------------------------------------


def _switch(member: WorkspaceRepo, status: _Status, name: str) -> _Outcome:
    path = member.relpath
    if status.error is not None:
        return _fail(path, status.error)
    if status.branch == name:
        return _skip(path, f"already on {name}")
    exists = _git(member.root, "show-ref", "--verify", "--quiet", f"refs/heads/{name}").ok
    if not exists:
        result = _git(member.root, "switch", "-c", name)
        return _ok(path, f"created and switched to {name}") if result.ok else _fail(path, result.reason)
    if status.branch is None:
        # Switching away from a detached HEAD would strand its commits unless the branch already contains them.
        contained = _git(member.root, "merge-base", "--is-ancestor", "HEAD", f"refs/heads/{name}").ok
        if not contained:
            return _fail(path, f"detached HEAD has commits that {name} lacks; switching would strand them")
    result = _git(member.root, "switch", name)
    return _ok(path, f"switched to {name}") if result.ok else _fail(path, result.reason)


def _branch(scope: _Scope, namespace: argparse.Namespace, sink: TextIO) -> int:
    name: str = namespace.name
    if name.startswith("-") or not name.strip():
        sink.write(f"lc workspace branch: invalid branch name: {name!r}\n")
        return 2
    statuses = _collect(scope, scope.chosen)
    targets = _default_targets(scope, statuses)
    outcomes = _parallel(targets, lambda member: _switch(member, statuses[member.relpath], name))
    return _emit("switch", scope, outcomes, namespace.json, sink)


# --- commit -----------------------------------------------------------------------------------------------------


def _on_a_remote(member: WorkspaceRepo) -> bool:
    """Whether HEAD is contained in some remote-tracking branch, so a parent recording it records a reachable commit."""

    listed = _git(member.root, "branch", "-r", "--contains", "HEAD", "--format=%(refname)")
    return listed.ok and bool(listed.out.strip())


def _commit_one(member: WorkspaceRepo, status: _Status, message: str) -> _Outcome:
    path = member.relpath
    if status.error is not None:
        return _fail(path, status.error)
    if status.branch is None:
        if not status.dirty and _on_a_remote(member):
            # A clean submodule checked out at a commit its remote already has (`git submodule update`, a pull):
            # nothing to commit here, and the repository holding it may record that commit.
            return _skip(path, f"nothing to commit, detached @{status.commit[:7]} is on a remote branch")
        return _fail(path, f"refused, detached HEAD @{status.commit[:7]}; run `lc workspace branch NAME` first")
    added = _git(member.root, "add", "-A")
    if not added.ok:
        return _fail(path, added.reason)
    staged = _git(member.root, "diff", "--cached", "--quiet")
    if staged.code == 0:
        return _skip(path, "nothing to commit")
    if staged.code != 1:
        return _fail(path, staged.reason)
    committed = _git(member.root, "commit", "-q", "-m", message)
    if not committed.ok:
        return _fail(path, committed.reason)
    short = _git(member.root, "rev-parse", "--short", "HEAD").out.strip()
    return _ok(path, f"committed {short} on {status.branch}")


def _commit(scope: _Scope, namespace: argparse.Namespace, sink: TextIO) -> int:
    statuses = _collect(scope, scope.chosen)
    targets = scope.deepest_first(_default_targets(scope, statuses))
    outcomes: list[_Outcome] = []
    failed: set[str] = set()
    for member in targets:
        blocker = _blocked_by(scope, member.relpath, failed)
        if blocker is not None:
            # Committing now would record the submodule's old commit; leave the repository for a rerun.
            outcome = _skip(member.relpath, f"submodule {blocker} was not committed, so its new commit is not recorded")
            failed.add(member.relpath)
        else:
            outcome = _commit_one(member, statuses[member.relpath], namespace.message)
            if outcome.state == "failed":
                failed.add(member.relpath)
        outcomes.append(outcome)
    code = _emit("commit", scope, outcomes, namespace.json, sink)
    return 1 if failed else code


# --- push -------------------------------------------------------------------------------------------------------


def _remote_of(member: WorkspaceRepo, branch: str | None) -> str:
    if branch is not None:
        configured = _git(member.root, "config", "--get", f"branch.{branch}.remote")
        if configured.ok and configured.out.strip():
            return configured.out.strip()
    return "origin"


def _has_remote(member: WorkspaceRepo, remote: str) -> bool:
    return _git(member.root, "config", "--get", f"remote.{remote}.url").ok


def _needs_push(status: _Status) -> bool:
    return status.upstream is None or status.ahead is None or status.ahead > 0


def _push_one(member: WorkspaceRepo, status: _Status) -> _Outcome:
    path = member.relpath
    if status.error is not None:
        return _fail(path, status.error)
    if status.branch is None:
        return _skip(path, "detached HEAD")
    if not _needs_push(status):
        return _skip(path, "upstream has every commit")
    remote = _remote_of(member, status.branch)
    if not _has_remote(member, remote):
        return _skip(path, f"no remote {remote!r}")
    # No --force: a rejected push is a failure for the person to resolve.
    result = _git(
        member.root,
        "push",
        "-u",
        remote,
        "HEAD",
        timeout=_PUSH_TIMEOUT_S,
        env={"GIT_TERMINAL_PROMPT": "0"},
    )
    if not result.ok:
        return _fail(path, result.reason)
    return _ok(path, f"pushed {status.branch} to {remote}")


def _push(scope: _Scope, namespace: argparse.Namespace, sink: TextIO) -> int:
    statuses = _collect(scope, scope.chosen)
    outcomes: list[_Outcome] = []
    failed: set[str] = set()
    # lc-debt: sequential, since a parent's push needs its submodules pushed first; push same-depth members together.
    for member in scope.deepest_first(scope.chosen):
        status = statuses[member.relpath]
        blocker = _blocked_by(scope, member.relpath, failed)
        if blocker is not None and status.branch is not None and _needs_push(status):
            outcome = _skip(
                member.relpath, f"submodule {blocker} was not pushed, so the commit it records is not on its remote"
            )
            failed.add(member.relpath)
        else:
            outcome = _push_one(member, status)
            if outcome.state == "failed":
                failed.add(member.relpath)
        outcomes.append(outcome)
    code = _emit("push", scope, outcomes, namespace.json, sink)
    return 1 if failed else code


# --- pr ---------------------------------------------------------------------------------------------------------


def _default_branch(member: WorkspaceRepo, remote: str) -> str:
    head = _git(member.root, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    if head.ok and head.out.strip():
        value = head.out.strip()
        return value[len(remote) + 1 :] if value.startswith(f"{remote}/") else value
    for candidate in sorted(_FALLBACK_DEFAULTS):
        if _git(member.root, "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{candidate}").ok:
            return candidate
    return ""


def _gh(gh: str, member: WorkspaceRepo, *args: str) -> _Run:
    return _exec([gh, *args], member.root, _GH_TIMEOUT_S, {"GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"})


def _existing_pr(gh: str, member: WorkspaceRepo) -> tuple[str, str] | None:
    """``(url, body)`` of the open pull request for the member's branch."""

    view = _gh(gh, member, "pr", "view", "--json", "url,body,state")
    if not view.ok:
        return None
    try:
        data = json.loads(view.out)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("state", "OPEN") != "OPEN" or not isinstance(data.get("url"), str):
        return None
    body = data.get("body")
    return str(data["url"]), body if isinstance(body, str) else ""


def _ensure_pr(gh: str, member: WorkspaceRepo, status: _Status, namespace: argparse.Namespace) -> _Outcome:
    path = member.relpath
    if status.error is not None:
        return _fail(path, status.error)
    if status.branch is None:
        return _skip(path, "detached HEAD")
    remote = _remote_of(member, status.branch)
    default = _default_branch(member, remote)
    if status.branch == default or (not default and status.branch in _FALLBACK_DEFAULTS):
        return _skip(path, f"on its default branch {status.branch}")
    if status.branch == namespace.base:
        return _skip(path, f"on the base branch {status.branch}")
    if status.upstream is None or status.ahead is None or status.ahead > 0:
        return _skip(path, "branch not pushed; run `lc workspace push`")
    base = namespace.base or default
    if base:
        ref = f"refs/remotes/{remote}/{base}"
        if _git(member.root, "show-ref", "--verify", "--quiet", ref).ok:
            count = _git(member.root, "rev-list", "--count", f"{ref}..HEAD").out.strip()
            if count == "0":
                return _skip(path, f"no commits beyond {base}")
    existing = _existing_pr(gh, member)
    if existing is not None:
        return _Outcome(path, "ok", f"reused {existing[0]}", url=existing[0], body=existing[1])
    title = namespace.title or _git(member.root, "log", "-1", "--format=%s").out.strip() or status.branch
    body = namespace.body if namespace.body is not None else ""
    args = ["pr", "create", "--title", title, "--body", body]
    if namespace.base:
        args += ["--base", namespace.base]
    if namespace.draft:
        args.append("--draft")
    created = _gh(gh, member, *args)
    if not created.ok:
        return _fail(path, created.reason)
    urls = [line.strip() for line in created.out.splitlines() if line.strip().startswith(("http://", "https://"))]
    if not urls:
        return _fail(path, "gh created no pull request URL")
    return _Outcome(path, "ok", f"created {urls[-1]}", url=urls[-1], body=body, created=True)


def _with_related(body: str, others: Sequence[tuple[str, str]]) -> str:
    base = _RELATED_SECTION.sub("", body).rstrip()
    listing = "\n".join(f"- {path}: {url}" for path, url in others)
    section = f"{_RELATED_HEADING}\n{listing}\n"
    return f"{base}\n\n{section}" if base else section


def _link(gh: str, scope: _Scope, outcomes: Sequence[_Outcome]) -> None:
    """Add the related-pull-requests list to every pull request once all of them exist."""

    linked = [outcome for outcome in outcomes if outcome.url is not None]
    if len(linked) < 2:
        return
    by_path = scope.by_path()

    def update(outcome: _Outcome) -> None:
        others = [(other.path, str(other.url)) for other in linked if other is not outcome]
        body = _with_related(outcome.body, others)
        if body.rstrip() == outcome.body.rstrip():
            return
        result = _gh(gh, by_path[outcome.path], "pr", "edit", str(outcome.url), "--body", body)
        if result.ok:
            outcome.message += "; linked related pull requests"
        else:
            outcome.state = "failed"
            outcome.message += f"; updating related pull requests failed: {result.reason}"

    _parallel(linked, update)


def _pr(scope: _Scope, namespace: argparse.Namespace, sink: TextIO) -> int:
    gh = shutil.which("gh")
    if gh is None:
        sink.write(
            "lc workspace pr: the GitHub CLI (gh) is not on PATH; install it and run `gh auth login`. Nothing was changed.\n"
        )
        return 2
    statuses = _collect(scope, scope.chosen)
    outcomes: list[_Outcome] = _parallel(
        list(scope.chosen), lambda member: _ensure_pr(gh, member, statuses[member.relpath], namespace)
    )
    _link(gh, scope, outcomes)
    return _emit("open", scope, outcomes, namespace.json, sink)


_ACTIONS: Final[dict[str, Callable[[_Scope, argparse.Namespace, TextIO], int]]] = {
    "status": _status,
    "branch": _branch,
    "commit": _commit,
    "push": _push,
    "pr": _pr,
}


def run(action: str, arguments: Sequence[str], sink: TextIO) -> int:
    handler = _ACTIONS.get(action)
    if handler is None:
        sink.write(f"unknown workspace command: {action}\n")
        return 2
    try:
        namespace = _build_parser(action, sink).parse_args(list(arguments))
        if action == "pr" and shutil.which("gh") is None:
            # Checked before any repository is read, so a missing gh changes and reads nothing.
            return handler(_Scope(Path(), (), (), False, False), namespace, sink)
        return handler(_scope(namespace, sink), namespace, sink)
    except _Usage as usage:
        return usage.code
