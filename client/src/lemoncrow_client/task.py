"""``lc task``: one worktree and one ``agent/<id>`` branch per agent task, in one repository or several.

In a Git repository, ``start`` makes ``.worktrees/<id>`` in the main checkout on a new branch ``agent/<id>``. In a
folder of repositories (:mod:`.workspace_members`), ``--repos`` names them and each gets its own
``<repo>/.worktrees/<id>``. Each new worktree gets its Runtime Lite index overlay on the main checkout's index
(:func:`.lite.index.warm_overlay`); the overlay lives inside the worktree, so removing the worktree removes it.
``land`` rebases onto ``origin/main``, runs the checks (``lc verify``) and pushes ``HEAD`` to ``main``, trying again
only when ``main`` moved. ``finish`` removes worktrees and branches only when every commit is on ``origin/main``.
Nothing here deletes unlanded commits, uncommitted or untracked files, or remote branches.

Checks run once. When a repository's pre-push hook runs ``lc verify --pre-push`` (this repository's does), a
single-repository land lets the push run them, on exactly the pushed commits. Otherwise land runs ``lc verify``
before the push. A task across repositories needs every result before its first push, so it runs ``lc verify`` in
every repository first, then pushes providers before consumers (``lc workspace graph``).

With ``--server URL`` the task lives on a LemonCrow Workspaces server instead, and the verb goes to
:mod:`.task_server`. ``LEMONCROW_WORKSPACES_SERVER`` names that server for the tasks that live there: a verb that
names a task this machine started on it (its token file is under ``LEMONCROW_HOME``), and the verbs only a server
has. Everything else, ``start`` and ``list`` included, stays local; without a server, nothing here talks to one.
"""

from __future__ import annotations

import contextlib
import heapq
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from .environment import ENVIRONMENT_FILE, DefinitionError, load_environment
from .lite.index import WARM_OVERLAY_ERRORS, is_leftover_store, remove_leftover_store, warm_overlay
from .task_server import SERVER_ENV, SERVER_ONLY, has_record, server_task_main
from .verify import verify_main
from .workspace_members import discover_members, git_toplevel, main_checkout

__all__ = ["TASK_USAGE", "task_main"]

TASK_USAGE: Final[str] = """usage:
  lc task start ID [--repos A,B] [--base REF]  new worktree .worktrees/ID on branch agent/ID, from origin/main after
                                               a fetch (--base names another start point); in a folder of
                                               repositories, one worktree in each repository --repos names
  lc task land [ID]                            rebase on origin/main, run the checks, push HEAD to main: inside the
                                               task worktree, or with ID in the folder of a multi-repository task
  lc task finish ID                            remove a landed task's worktrees and branches (from outside them)
  lc task list                                 task branches, landed or not

IDs use letters, digits, '.', '_' and '-'. Nothing here deletes unlanded commits, uncommitted or untracked files,
or remote branches.

Tasks on a LemonCrow Workspaces server: add --server URL. With LEMONCROW_WORKSPACES_SERVER set instead, a verb goes
there only for a task this machine started there, or when only a server has it (verify, discard, preview, mcp). The
organization token comes from LEMONCROW_WORKSPACES_TOKEN; each task's own token stays in an owner-only file under
LEMONCROW_HOME. The server runs the task in the [toolchain] image of .lemoncrow/environment.toml; a start without
one, or with one it cannot pull, is refused with the fix: lc verify init --toolchain IMAGE, then commit and push.
  lc task start ID --server URL [--repos A,B] [--base REF] [--host claude|codex|opencode] [--model MODEL]
                                               start the task on the Workspaces server; --host registers MCP server
                                               lc-task-ID in that agent host; --host and --model name the agent
                                               (Review can require a reviewer of another model family)
  lc task claude ID --server URL [--repos A,B] [--base REF] [--model MODEL] [-- CLAUDE ARGUMENTS]
  lc task codex ID --server URL [--repos A,B] [--base REF] [--model MODEL] [-- CODEX ARGUMENTS]
                                               run Claude Code or Codex here on the task (started first when this
                                               machine has not): the task's tools only, its own file and shell
                                               tools off; land asks you first
  lc task mcp ID --server URL                  stdio MCP bridge to the task on the Workspaces server
  lc task verify ID --server URL               run the task's checks on the Workspaces server
  lc task land ID -m MESSAGE --server URL      commit, check, send to Review and push the task's work on the
                                               Workspaces server; prints Review's findings when it does not pass
  lc task finish ID --server URL               finish the task on the Workspaces server and forget its token
  lc task discard ID --server URL              discard the task's saved work on the Workspaces server
  lc task list --server URL                    tasks on the Workspaces server
  lc task preview ID NAME --server URL         a one-time link to the task's preview NAME on the Workspaces server

Hosted agents: the Workspaces server runs one of the agents its configuration names on the task, in a container of
its own, within the time and spend the configuration allows.
  lc task start ID --server URL [--run AGENT] --goal TEXT [--land]
                                               start the task with a hosted agent working it (without --run, the
                                               server's default agent); --land: the server lands the work when the
                                               agent stops (Review's findings go back to it)
  lc task run ID [AGENT] --goal TEXT [--land] --server URL
                                               give a live task to a hosted agent (a fresh run and budget)
  lc task watch ID --server URL                follow the hosted agent's work until its run ends
  lc task tell ID -m MESSAGE --server URL      another round for the hosted agent, the message its prompt
  lc task stop ID --server URL                 stop the hosted agent's round
  lc task attach ID --server URL [--host H] [--model M]
                                               take the task over: a new task token kept here (the hosted agent is
                                               stopped first; earlier tokens stop working)
"""

#: Options of ``lc task`` that take a value, so ``--server`` is never mistaken for one's value or the reverse.
_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--repos", "--base", "--host", "--model", "-m", "--message", "--run", "--goal"}
)

_REMOTE: Final[str] = "origin"
_MAIN: Final[str] = "main"
_UPSTREAM: Final[str] = f"{_REMOTE}/{_MAIN}"
_PREFIX: Final[str] = "agent/"
_WORKTREES: Final[str] = ".worktrees"
#: Push attempts; another one only when main moved in between.
_ATTEMPTS: Final[int] = 3
_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
_HOOK_VERIFY: Final[re.Pattern[str]] = re.compile(r"\bverify\s+--pre-push\b")


class _Stop(Exception):
    """A refusal or failure; ``code`` 2 is a usage error."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _git(cwd: Path, *args: str) -> str:
    done = _run(cwd, *args)
    if done.returncode != 0:
        raise _Stop(f"git {args[0]} failed in {cwd}: {done.stderr.strip() or f'exit {done.returncode}'}")
    return done.stdout.strip()


def _ok(cwd: Path, *args: str) -> bool:
    return _run(cwd, *args).returncode == 0


def _shown(cwd: Path, sink: TextIO, *args: str) -> bool:
    """Git with its output (and any hook's) shown, so the person can read why it stopped."""

    sink.flush()
    return subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, check=False).returncode == 0


@dataclass(frozen=True, slots=True)
class _Repo:
    #: Relative to the folder of repositories; ``.`` for a single repository.
    name: str
    #: The main checkout, which holds ``.worktrees/``.
    main: Path

    @property
    def label(self) -> str:
        return "" if self.name == "." else f"{self.name}: "

    def worktree(self, task_id: str) -> Path:
        return self.main / _WORKTREES / task_id

    def has_branch(self, branch: str) -> bool:
        return _ok(self.main, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")


@dataclass(frozen=True, slots=True)
class _Scope:
    #: The folder of repositories; ``None`` inside a repository.
    folder: Path | None
    repos: tuple[_Repo, ...]
    #: Inside a repository, the top level of the checkout the command runs in.
    top: Path | None = None


def _scope(cwd: Path) -> _Scope:
    top = git_toplevel(cwd)
    if top is not None:
        # The main checkout is always the first entry.
        first = _git(top, "worktree", "list", "--porcelain").splitlines()[0]
        return _Scope(None, (_Repo(".", Path(first.removeprefix("worktree "))),), top)
    members = discover_members(cwd).members
    repos = tuple(_Repo(member.relpath, main_checkout(member.root)) for member in members if member.parent is None)
    if not repos:
        raise _Stop("not inside a Git repository or a folder of repositories")
    return _Scope(cwd.resolve(), repos)


def _valid(task_id: str | None, usage: str) -> str:
    if task_id is None or not _ID.fullmatch(task_id):
        raise _Stop(f"usage: lc task {usage}  (ID: letters, digits, '.', '_', '-'; not starting with '.')", 2)
    return task_id


def _named(scope: _Scope, value: str) -> list[_Repo]:
    by_name = {repo.name: repo for repo in scope.repos}
    names = list(dict.fromkeys(name.strip().strip("/") for name in value.split(",") if name.strip()))
    unknown = [name for name in names if name not in by_name] or ([] if names else [value])
    if unknown:
        raise _Stop(f"not a repository in {scope.folder}: {', '.join(unknown)}; here: {', '.join(by_name)}", 2)
    return [by_name[name] for name in names]


def _task_repos(scope: _Scope, branch: str) -> list[_Repo]:
    repos = [repo for repo in scope.repos if repo.has_branch(branch)]
    if not repos:
        raise _Stop(f"no branch {branch}" + (f" in any repository in {scope.folder}" if scope.folder else ""))
    return repos


def _ignore_worktrees(main: Path, directory: Path) -> None:
    """Keep task worktrees out of the main checkout's ``git status``, and so out of its ``git add -A``."""

    if _ok(main, "check-ignore", "-q", str(directory.relative_to(main))):
        return
    exclude = Path(_git(main, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else main / exclude
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    # Kept after finish: the line only hides .worktrees/, which later tasks use again.
    exclude.write_text(f"{text}/{_WORKTREES}/\n", encoding="utf-8")


def _warm(repo: _Repo, directory: Path, sink: TextIO) -> None:
    """Build the worktree's index overlay now, so the agent's first search does not; never fails start."""

    try:
        report = warm_overlay(directory)
    except WARM_OVERLAY_ERRORS as exc:
        sink.write(f"lc task: {repo.label}index overlay not built ({exc}); the first search builds it\n")
        return
    if report is not None:
        sink.write(f"lc task: {repo.label}index overlay ready on the index of {repo.main}\n")


def _evict(directory: Path) -> bool:
    """Remove a leftover index where a worktree was (:func:`.lite.index.is_leftover_store`); whether there was one."""

    try:
        return remove_leftover_store(directory)
    except OSError as exc:
        raise _Stop(f"{directory} holds a leftover index that could not be removed: {exc}") from None


def _registered(main: Path, directory: Path) -> bool:
    """Whether Git has a worktree at ``directory``, there or deleted by hand."""

    want = directory.resolve()
    lines = _git(main, "worktree", "list", "--porcelain").splitlines()
    return any(Path(line.removeprefix("worktree ")).resolve() == want for line in lines if line.startswith("worktree "))


def _start(arguments: Sequence[str], sink: TextIO) -> int:
    task_id: str | None = None
    names: str | None = None
    base: str | None = None
    items = iter(arguments)
    for item in items:
        option = item.partition("=")[0]
        if option in {"--repos", "--base"}:
            value = item.partition("=")[2] if "=" in item else next(items, "")
            if not value or value.startswith("-"):
                raise _Stop(f"{option} needs a value", 2)
            names, base = (value, base) if option == "--repos" else (names, value)
        elif item.startswith("-") or task_id is not None:
            raise _Stop(f"unknown argument: {item}", 2)
        else:
            task_id = item
    task_id = _valid(task_id, "start ID [--repos A,B] [--base REF]")
    scope = _scope(Path.cwd())
    if scope.folder is None:
        if names is not None:
            raise _Stop("--repos names repositories in a folder of repositories; this is one repository", 2)
        repos = list(scope.repos)
    elif names is None:
        raise _Stop(f"name the repositories with --repos; here: {', '.join(repo.name for repo in scope.repos)}", 2)
    else:
        repos = _named(scope, names)

    # Every check before the first worktree, so a refusal creates nothing.
    branch = _PREFIX + task_id
    leftovers: list[Path] = []
    for repo in repos:
        directory = repo.worktree(task_id)
        if is_leftover_store(directory):
            leftovers.append(directory)
        elif directory.exists() or directory.is_symlink():
            raise _Stop(f"{directory} already exists")
        if repo.has_branch(branch):
            raise _Stop(f"{repo.label}branch {branch} already exists")
    for repo in repos:
        if base is None:
            _git(repo.main, "fetch", "--quiet", _REMOTE, _MAIN)
        elif not _ok(repo.main, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"):
            raise _Stop(f"{repo.label}{base} is not a commit")
    for directory in leftovers:
        _evict(directory)
    made: list[str] = []
    for repo in repos:
        directory = repo.worktree(task_id)
        done = _run(
            repo.main, "worktree", "add", "--quiet", "--no-track", "-b", branch, str(directory), base or _UPSTREAM
        )
        if done.returncode != 0:
            created = f"; already created: {', '.join(made)}" if made else ""
            raise _Stop(f"{repo.label}git worktree add failed: {done.stderr.strip()}{created}")
        _ignore_worktrees(repo.main, directory)
        made.append(str(directory))
        sink.write(f"lc task: work in {directory}\n")
    for repo in repos:
        _warm(repo, repo.worktree(task_id), sink)
    if scope.folder is None:
        sink.write("lc task: commit, then run 'lc task land' inside the worktree (it runs lc verify)\n")
    else:
        sink.write(f"lc task: commit in each worktree, then run 'lc task land {task_id}' in {scope.folder}\n")
    return 0


def _require_clean(tops: Sequence[tuple[str, Path]]) -> None:
    dirty = [label or str(top) for label, top in tops if _git(top, "status", "--porcelain")]
    if dirty:
        raise _Stop(f"commit your work first: the gate checks the working tree ({', '.join(dirty)})")


def _hook_verifies(top: Path) -> bool:
    """Whether Git's pre-push hook here runs ``lc verify --pre-push``, so the push itself runs the checks."""

    path = Path(_git(top, "rev-parse", "--git-path", "hooks/pre-push"))
    hook = path if path.is_absolute() else top / path
    try:
        if not hook.is_file() or not os.access(hook, os.X_OK):
            return False
        lines = hook.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(_HOOK_VERIFY.search(line) for line in lines if not line.lstrip().startswith("#"))


def _verify(top: Path, label: str, sink: TextIO) -> bool:
    """``lc verify`` on the commits ``HEAD`` adds to ``origin/main`` (the tree is clean, so exactly those)."""

    try:
        defined = load_environment(top) is not None
    except DefinitionError:
        defined = True  # lc verify reports it and fails
    if not defined:
        # The gate fails closed, as lc verify does: a repository without checks is not landed unchecked.
        sink.write(f"lc task: {label}no {ENVIRONMENT_FILE}: define the checks there before landing\n")
        return False
    with contextlib.chdir(top):
        passed = verify_main(["--base", _UPSTREAM], sink) == 0
    if not passed:
        sink.write(f"lc task: {label}checks failed; fix them, commit, and land again\n")
    return passed


def _rebase(top: Path, label: str, sink: TextIO) -> bool:
    """Fetch and rebase on ``origin/main``; False when there is nothing to land."""

    _git(top, "fetch", "--quiet", _REMOTE, _MAIN)
    if _git(top, "rev-list", "--count", f"{_UPSTREAM}..HEAD") == "0":
        return False
    if not _shown(top, sink, "rebase", "--quiet", _UPSTREAM):
        raise _Stop(f"{label}rebase stopped on a conflict: resolve it, run 'git rebase --continue', then land again")
    return True


def _push(top: Path, label: str, attempt: int, sink: TextIO) -> bool:
    """Push ``HEAD`` to ``main``; False when it failed because main moved and another attempt is left."""

    if _shown(top, sink, "push", _REMOTE, f"HEAD:refs/heads/{_MAIN}"):
        return True
    # Try again only when main moved under us; a failed gate or any other error stops here.
    _git(top, "fetch", "--quiet", _REMOTE, _MAIN)
    if _ok(top, "merge-base", "--is-ancestor", _UPSTREAM, "HEAD"):
        raise _Stop(f"{label}push failed; read the output above")
    if attempt >= _ATTEMPTS:
        raise _Stop(f"{label}main kept moving; tried {_ATTEMPTS} times")
    sink.write(f"lc task: {label}main moved, rebasing again\n")
    return False


def _land_one(top: Path, label: str, sink: TextIO, *, checked: bool, hook: bool) -> bool:
    """Push one repository, rebasing and checking again when main moved; False when nothing was left to land.

    ``checked``: rebased and checked already (the first phase of a task across repositories).
    """

    attempt = 0
    while True:
        attempt += 1
        if not checked:
            if not _rebase(top, label, sink):
                sink.write(f"lc task: {label}nothing to land\n")
                return False
            if not hook and not _verify(top, label, sink):
                raise _Stop(f"{label}not landed")
        if _push(top, label, attempt, sink):
            return True
        checked = False


def _siblings(main: Path, task_id: str) -> list[str]:
    """Other repositories in the folder around ``main`` with a worktree for the same task."""

    folder = main.parent
    if git_toplevel(folder) is not None:
        return []
    here = main.resolve()
    found: list[str] = []
    for member in discover_members(folder).members:
        other = main_checkout(member.root).resolve()
        if member.parent is None and other != here and (other / _WORKTREES / task_id).is_dir():
            found.append(member.relpath)
    return found


def _push_order(folder: Path, names: Sequence[str], sink: TextIO) -> list[str]:
    """``names`` with each repository after the ones it depends on (``lc workspace graph``), else in name order."""

    from .workspace_graph import build_graph

    graph = build_graph(folder)
    wanted = set(names)
    waiting = dict.fromkeys(names, 0)
    consumers: dict[str, set[str]] = {name: set() for name in names}
    for edge in graph.edges:  # source depends on target
        if edge.source in wanted and edge.target in wanted and edge.source != edge.target:
            if edge.source not in consumers[edge.target]:
                consumers[edge.target].add(edge.source)
                waiting[edge.source] += 1
    ready = sorted(name for name, count in waiting.items() if count == 0)
    order: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for consumer in consumers[name]:
            waiting[consumer] -= 1
            if waiting[consumer] == 0:
                heapq.heappush(ready, consumer)
    cycle = sorted(wanted - set(order))
    if cycle:
        sink.write(f"lc task: {', '.join(cycle)} depend on each other; pushing them in name order\n")
    if graph.truncated:
        sink.write(
            "lc task: note: the dependency graph is incomplete (lc workspace graph); the order may miss an edge\n"
        )
    return order + cycle


def _land(arguments: Sequence[str], sink: TextIO) -> int:
    if len(arguments) > 1 or any(item.startswith("-") for item in arguments):
        raise _Stop("usage: lc task land [ID]", 2)
    given = arguments[0] if arguments else None
    scope = _scope(Path.cwd())
    if scope.folder is None:
        assert scope.top is not None
        return _land_single(scope.top, scope.repos[0], given, sink)
    return _land_all(scope, _valid(given, "land ID  (in the folder of a multi-repository task)"), sink)


def _land_single(top: Path, repo: _Repo, given: str | None, sink: TextIO) -> int:
    head = _run(top, "symbolic-ref", "--short", "-q", "HEAD")
    branch = head.stdout.strip()
    if head.returncode != 0 or not branch:
        raise _Stop("detached HEAD: check out your agent/<id> branch")
    if not branch.startswith(_PREFIX):
        raise _Stop(f"land runs on an agent/<id> branch, not {branch}")
    task_id = branch.removeprefix(_PREFIX)
    if given is not None and given != task_id:
        raise _Stop(f"this worktree is on {branch}, not {_PREFIX}{given}", 2)
    others = _siblings(repo.main, task_id)
    if others:
        raise _Stop(
            f"{', '.join(others)} also have a worktree for {task_id}: land the whole task with "
            f"'lc task land {task_id}' in {repo.main.parent}. Nothing pushed."
        )
    _require_clean([("", top)])
    hook = _hook_verifies(top)
    if hook:
        sink.write("lc task: the pre-push hook runs the checks (lc verify --pre-push)\n")
    if not _land_one(top, "", sink, checked=False, hook=hook):
        return 0
    sink.write(
        f"lc task: landed {_git(top, 'rev-parse', '--short', 'HEAD')} on main. Next, from outside this worktree:\n"
    )
    sink.write(f"  lc task finish {task_id}\n")
    return 0


def _land_all(scope: _Scope, task_id: str, sink: TextIO) -> int:
    assert scope.folder is not None
    branch = _PREFIX + task_id
    repos = {repo.name: repo for repo in _task_repos(scope, branch)}
    tops: dict[str, Path] = {}
    for name, repo in repos.items():
        top = repo.worktree(task_id)
        if not top.is_dir() or _run(top, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() != branch:
            raise _Stop(f"{repo.label}{branch} is not checked out at {top}")
        tops[name] = top
    _require_clean(list(tops.items()))

    # Every repository is rebased and checked before the first push: one failure pushes nothing.
    # lc-debt: a repository whose pre-push hook runs lc verify checks again at push. Upgrade: M2's recorded verify
    # fingerprint lets the hook skip a tree that already passed.
    ready: list[str] = []
    try:
        for name, top in tops.items():
            if not _rebase(top, repos[name].label, sink):
                sink.write(f"lc task: {repos[name].label}nothing to land\n")
            elif not _verify(top, repos[name].label, sink):
                raise _Stop("every repository must pass first")
            else:
                ready.append(name)
    except _Stop as stop:
        raise _Stop(f"{stop} Nothing pushed.") from None
    if not ready:
        sink.write("lc task: nothing to land\n")
        return 0
    order = _push_order(scope.folder, ready, sink)
    sink.write(f"lc task: every repository passed; pushing {', '.join(order)}\n")
    landed: list[str] = []
    for index, name in enumerate(order):
        top = tops[name]
        try:
            if _land_one(top, repos[name].label, sink, checked=True, hook=_hook_verifies(top)):
                landed.append(name)
        except _Stop:
            sink.write(f"lc task: landed: {', '.join(landed) or 'none'}; not landed: {', '.join(order[index:])}\n")
            raise
    sink.write(f"lc task: landed {', '.join(landed)} on main. Next, in {scope.folder}:\n  lc task finish {task_id}\n")
    return 0


def _finish(arguments: Sequence[str], sink: TextIO) -> int:
    if len(arguments) != 1:
        raise _Stop("usage: lc task finish ID", 2)
    task_id = _valid(arguments[0], "finish ID")
    branch = _PREFIX + task_id
    repos = _task_repos(_scope(Path.cwd()), branch)
    here = Path.cwd().resolve()
    for repo in repos:
        directory = repo.worktree(task_id)
        if here.is_relative_to(directory.resolve()):
            raise _Stop(f"run finish from outside {directory}")
    # Every check before the first removal, so a refusal removes nothing.
    for repo in repos:
        _git(repo.main, "fetch", "--quiet", _REMOTE, _MAIN)
    unlanded = [repo for repo in repos if not _ok(repo.main, "merge-base", "--is-ancestor", branch, _UPSTREAM)]
    if unlanded:
        where = ", ".join(repo.name for repo in unlanded if repo.name != ".")
        raise _Stop(
            f"{branch} has commits that are not on {_UPSTREAM}{f' in {where}' if where else ''}; land them first. "
            "Nothing removed."
        )
    # A worktree deleted another way is still registered (git worktree remove forgets it), or a leftover index
    # stands in its place (removed below). Anything else at that path is not this task's to remove.
    registered = {repo.name: _registered(repo.main, repo.worktree(task_id)) for repo in repos}
    for repo in repos:
        directory = repo.worktree(task_id)
        there = directory.exists() or directory.is_symlink()
        checkout = registered[repo.name] and (directory / ".git").exists()
        if there and not checkout and not is_leftover_store(directory):
            raise _Stop(f"{directory} is not a Git worktree; move it away first. Nothing removed.")
    # Git status only where a checkout is: in a leftover it would report the main checkout.
    present = [repo.worktree(task_id) for repo in repos if (repo.worktree(task_id) / ".git").exists()]
    dirty = [str(directory) for directory in present if _git(directory, "status", "--porcelain")]
    if dirty:
        raise _Stop(f"{', '.join(dirty)} has uncommitted or untracked files. Nothing removed.")
    for repo in repos:
        directory = repo.worktree(task_id)
        if _evict(directory):
            sink.write(f"lc task: {repo.label}removed a leftover index in {directory}\n")
        if registered[repo.name]:
            # Refuses when the worktree has modified or untracked files. Ignored files (the index overlay,
            # dependencies, build output) are removed with it; one already deleted by hand is only forgotten.
            done = _run(repo.main, "worktree", "remove", str(directory))
            if done.returncode != 0:
                raise _Stop(f"{directory} not removed: {done.stderr.strip()}")
        # -D is safe: the ancestry check above proved every commit is on origin/main.
        _git(repo.main, "branch", "--quiet", "-D", branch)
        sink.write(f"lc task: {repo.label}removed {branch}\n")
    return 0


def _list(arguments: Sequence[str], sink: TextIO) -> int:
    if arguments:
        raise _Stop("usage: lc task list", 2)
    shown = False
    for repo in _scope(Path.cwd()).repos:
        branches = _git(repo.main, "for-each-ref", "--format=%(refname:short)", "refs/heads/agent/").splitlines()
        if not branches:
            continue
        if not _ok(repo.main, "fetch", "--quiet", _REMOTE, _MAIN):
            sink.write(f"lc task: {repo.label}fetch failed; showing local state\n")
        known = _ok(repo.main, "rev-parse", "--verify", "--quiet", f"{_UPSTREAM}^{{commit}}")
        for branch in branches:
            if not known:
                state = f"{_UPSTREAM} unknown"
            elif _ok(repo.main, "merge-base", "--is-ancestor", branch, _UPSTREAM):
                state = "landed"
            else:
                state = f"{_git(repo.main, 'rev-list', '--count', f'{_UPSTREAM}..{branch}')} commit(s) not landed"
            directory = repo.worktree(branch.removeprefix(_PREFIX))
            sink.write(f"{repo.label}{branch}  {state}  {directory if directory.is_dir() else '(no worktree)'}\n")
            shown = True
    if not shown:
        sink.write("lc task: no task branches\n")
    return 0


_ACTIONS: Final[dict[str, Callable[[Sequence[str], TextIO], int]]] = {
    "start": _start,
    "land": _land,
    "finish": _finish,
    "list": _list,
}


def _task_argument(arguments: Sequence[str]) -> str | None:
    """The first positional after the verb in ``arguments`` (the task ID), skipping options and their values."""

    items = iter(arguments[1:])
    for item in items:
        if item in _VALUE_OPTIONS:
            next(items, None)
        elif not item.startswith("-"):
            return item
    return None


def _lives_on(server: str, arguments: Sequence[str]) -> bool:
    """Whether ``lc task ARGUMENTS`` without ``--server`` belongs on ``server``: a verb only a server has, or one
    naming a task this machine started there. ``start`` never does: a new task lives on a server only by ``--server``.
    """

    if not arguments or arguments[0] == "start":
        return False
    if arguments[0] in SERVER_ONLY:
        return True
    task_id = _task_argument(arguments)
    return task_id is not None and has_record(server, task_id)


def _server_option(arguments: Sequence[str]) -> tuple[str | None, list[str]]:
    """The Workspaces server ``--server URL`` names (else ``LEMONCROW_WORKSPACES_SERVER``, for a task that lives
    there), and the other arguments."""

    server: str | None = None
    rest: list[str] = []
    items = iter(arguments)
    for item in items:
        if item == "--":  # what follows goes to the agent host (lc task claude|codex), untouched
            rest += [item, *items]
            break
        if item.partition("=")[0] == "--server":
            value = item.partition("=")[2] if "=" in item else next(items, "")
            if not value.strip():
                raise _Stop("--server needs the Workspaces server URL", 2)
            server = value
            continue
        rest.append(item)
        if item in _VALUE_OPTIONS:
            following = next(items, None)
            if following is not None:
                rest.append(following)
    if server is not None:
        return server, rest
    configured = os.environ.get(SERVER_ENV, "").strip()
    return (configured if configured and _lives_on(configured, rest) else None), rest


def task_main(arguments: Sequence[str], sink: TextIO) -> int:
    try:
        server, arguments = _server_option(arguments)
    except _Stop as stop:
        sink.write(f"lc task: {stop}\n")
        return stop.code
    if server is not None:
        return server_task_main(arguments, server, sink, usage=TASK_USAGE)
    if not arguments or any(item in {"-h", "--help", "help"} for item in arguments):
        sink.write(TASK_USAGE)
        return 0 if arguments else 2
    action = _ACTIONS.get(arguments[0])
    if action is None:
        if arguments[0] in SERVER_ONLY:
            sink.write(
                f"lc task: {arguments[0]} is for tasks on a Workspaces server: add --server URL or set {SERVER_ENV}\n"
            )
            return 2
        sink.write(f"lc task: unknown command: {arguments[0]}\n{TASK_USAGE}")
        return 2
    try:
        return action(arguments[1:], sink)
    except _Stop as stop:
        sink.write(f"lc task: {stop}\n")
        return stop.code
