"""``lc verify``: run the checks in ``.lemoncrow/environment.toml`` that a change touches.

A check runs when the change touches one of its ``paths`` (Git pathspecs, matched by Git itself; a deletion counts).
Checks run one after another from the repository root, in the working tree, with ``$LC_CHANGED`` naming a file that
lists the changed paths that still exist, one per line; a check's ``timeout`` stops it (and every process it started)
and fails it. Exit 0 when every selected check passed or nothing changed, 1 when a check failed, 2 for a usage, Git
or definition error.

After running, ``lc verify`` records the environment fingerprint each check ran under, and how it ended, in
``<git common dir>/lemoncrow/verify/<HEAD>.json`` (``environment.write_verify_record``), where a later local Review
of that commit compares it with its own.

As the pre-push hook (``--pre-push``), Git's ref lines on stdin name exactly the pushed commits; a repository that uses
Git LFS keeps its upload step, because a custom hooks path replaces the hook LFS installs.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from .environment import (
    ENVIRONMENT_FILE,
    Check,
    DefinitionError,
    Environment,
    Fingerprint,
    load_environment,
    write_verify_record,
)
from .verify_hook import hook_main

__all__ = ["VERIFY_USAGE", "verify_main"]

VERIFY_USAGE: Final[str] = """usage:
  lc verify [--base REF] [--list]   checks touched by HEAD plus uncommitted and untracked work, against the
                                    merge base of HEAD and REF (default origin/main; the root commit without one)
  lc verify --all [--list]          every check, as if every tracked file changed
  lc verify --pre-push [REMOTE URL] the pre-push hook: checks exactly the pushed commits (Git's ref lines on stdin)
  lc verify hook install|uninstall  write or remove that hook for this repository (lc verify hook --help)
  lc verify init [--toolchain IMAGE] [--print]
                                    write .lemoncrow/environment.toml from the checks this repository already
                                    runs, or pin its [toolchain] (lc verify init --help)

--list prints the ids of the checks that would run and runs nothing. Checks come from .lemoncrow/environment.toml.
"""

_DEFAULT_BASE: Final[str] = "origin/main"


class _GitError(Exception):
    pass


def _git(top: Path, *args: str, stdin: str | None = None) -> str:
    completed = subprocess.run(["git", *args], cwd=top, input=stdin, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise _GitError(completed.stderr.strip() or f"git {args[0]} exited {completed.returncode}")
    return completed.stdout


def _paths(output: str) -> list[str]:
    return [path for path in output.split("\0") if path]


def _is_zero(sha: str) -> bool:
    return set(sha) <= {"0"}


class _Change:
    """What changed: every tracked file, the working tree against a base, or a set of pushed commit ranges."""

    def __init__(self, top: Path, *, every: bool, base: str | None, ranges: Sequence[tuple[str, str]]) -> None:
        self._top, self._every, self._base, self._ranges = top, every, base, tuple(ranges)

    def paths(self, specs: Sequence[str]) -> list[str]:
        top = self._top
        if self._every:
            return _paths(_git(top, "ls-files", "-z", "--", *specs))
        if self._base is not None:
            tracked = _paths(_git(top, "diff", "--name-only", "-z", self._base, "--", *specs))
            return tracked + _paths(_git(top, "ls-files", "-o", "--exclude-standard", "-z", "--", *specs))
        found: list[str] = []
        for base, tip in self._ranges:
            found += _paths(_git(top, "diff", "--name-only", "-z", base, tip, "--", *specs))
        return found


def _base_of(top: Path, rev: str, other: str = _DEFAULT_BASE) -> str:
    try:
        return _git(top, "merge-base", rev, other).strip()
    except _GitError:
        roots = _git(top, "rev-list", "--max-parents=0", rev).split()
        if not roots:
            raise
        return roots[-1]


def _push_ranges(top: Path, ref_lines: str, sink: TextIO | None) -> list[tuple[str, str]]:
    """One (base, tip) per pushed branch; tags and deletions are skipped, a new branch starts at its merge base."""

    head = _git(top, "rev-parse", "HEAD").strip()
    ranges: list[tuple[str, str]] = []
    for line in ref_lines.splitlines():
        local_ref, local_sha, _remote_ref, remote_sha = ([*line.split(), "", "", "", ""])[:4]
        if not local_sha or local_ref.startswith("refs/tags/") or _is_zero(local_sha):
            continue
        known = not _is_zero(remote_sha)
        if known:
            try:
                _git(top, "cat-file", "-e", f"{remote_sha}^{{commit}}")
            except _GitError:
                known = False
        ranges.append((remote_sha if known else _base_of(top, local_sha), local_sha))
        if local_sha != head and sink is not None:
            sink.write(f"lc verify: warning: pushing {local_sha} but checks run on HEAD's working tree\n")
    return ranges


def _lfs_pre_push(top: Path, hook_args: Sequence[str], ref_lines: str, sink: TextIO) -> int:
    try:
        attributes = (top / ".gitattributes").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    if "filter=lfs" not in attributes:
        return 0
    if shutil.which("git-lfs") is None:
        sink.write("lc verify: git-lfs is required in this repository\n")
        return 2
    sink.flush()
    return subprocess.run(["git", "lfs", "pre-push", *hook_args], cwd=top, input=ref_lines, text=True).returncode


def _parse(arguments: Sequence[str]) -> tuple[bool, bool, bool, str | None, list[str]] | str:
    every = listing = pre_push = False
    base: str | None = None
    hook_args: list[str] = []
    items = iter(arguments)
    for item in items:
        if item == "--all":
            every = True
        elif item == "--list":
            listing = True
        elif item == "--pre-push":
            pre_push = True
        elif item == "--base" or item.startswith("--base="):
            base = item.partition("=")[2] if "=" in item else next(items, "")
            if not base or base.startswith("-"):
                return "--base needs a revision"
        elif pre_push and not item.startswith("-"):
            hook_args.append(item)  # Git passes the remote's name and URL to the hook
        else:
            return f"unknown argument: {item}"
    if pre_push and (every or base is not None):
        return "--pre-push checks the pushed commits; it takes neither --all nor --base"
    if every and base is not None:
        return "--all checks every file; it takes no --base"
    return every, listing, pre_push, base, hook_args


def verify_main(arguments: Sequence[str], sink: TextIO, *, refs: TextIO | None = None) -> int:
    if list(arguments[:1]) == ["hook"]:
        return hook_main(arguments[1:], sink)
    if list(arguments[:1]) == ["init"]:
        from .verify_init import init_main  # reaches a registry only with --toolchain; kept off lc verify's path

        return init_main(arguments[1:], sink)
    if any(item in {"-h", "--help", "help"} for item in arguments):
        sink.write(VERIFY_USAGE)
        return 0
    parsed = _parse(arguments)
    if isinstance(parsed, str):
        sink.write(f"lc verify: {parsed}\n{VERIFY_USAGE}")
        return 2
    every, listing, pre_push, base_ref, hook_args = parsed
    try:
        top = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    except _GitError as exc:
        sink.write(f"lc verify: not inside a Git repository: {exc}\n")
        return 2
    try:
        environment = load_environment(top)
    except DefinitionError as exc:
        sink.write(f"lc verify: {exc}\n")
        return 2
    if environment is None:
        sink.write(f"lc verify: {ENVIRONMENT_FILE} is missing; define the checks there\n")
        return 2

    ref_lines = ""
    if pre_push:
        stream = refs if refs is not None else sys.stdin
        ref_lines = "" if stream.isatty() else stream.read()
    try:
        if ref_lines:
            lfs = 0 if listing else _lfs_pre_push(top, hook_args, ref_lines, sink)
            if lfs != 0:
                return lfs
            change = _Change(
                top, every=False, base=None, ranges=_push_ranges(top, ref_lines, None if listing else sink)
            )
        elif every:
            change = _Change(top, every=True, base=None, ranges=())
        elif base_ref is not None:
            change = _Change(top, every=False, base=_git(top, "merge-base", "HEAD", base_ref).strip(), ranges=())
        else:
            change = _Change(top, every=False, base=_base_of(top, "HEAD"), ranges=())
        changed = sorted(set(change.paths(["."])))
        selected = [check for check in environment.checks if change.paths(check.paths)]
        dirty = bool(_git(top, "status", "--porcelain").strip())
    except _GitError as exc:
        sink.write(f"lc verify: git failed: {exc}\n")
        return 2

    if listing:
        sink.writelines(f"{check.id}\n" for check in selected)
        return 0
    if dirty:
        sink.write("lc verify: note: uncommitted changes are included in the checks\n")
    if not changed:
        sink.write("lc verify: no changed files, nothing to check\n")
        return 0
    return _run(top, environment, selected, changed, pre_push=pre_push, sink=sink, dirty=dirty)


def _run_check(check: Check, top: Path, env: dict[str, str]) -> str:
    """``passed``, ``failed`` or ``timed_out``; a timeout stops the check's whole process group."""

    # Its own process group (setpgid, never setsid): a timeout stops the whole tree, and the check stays in this
    # session, so it dies with it.
    process = subprocess.Popen(["sh", "-c", check.run], cwd=top, stdin=subprocess.DEVNULL, env=env, process_group=0)
    try:
        code = process.wait(timeout=check.timeout)
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
        return "timed_out"
    except BaseException:
        # Ctrl-C reaches the check's own process group only through us: stop it before leaving.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    return "passed" if code == 0 else "failed"


def _record(top: Path, results: dict[str, tuple[Fingerprint, str]], *, dirty: bool, sink: TextIO) -> None:
    """Leave the fingerprints for a later Review; a failure to write is a note, never a failed verify."""

    try:
        head = _git(top, "rev-parse", "HEAD").strip()
        common = Path(_git(top, "rev-parse", "--git-common-dir").strip())
        write_verify_record(common if common.is_absolute() else top / common, head, results, dirty=dirty)
    except (_GitError, OSError) as exc:
        sink.write(f"lc verify: note: the environment fingerprints were not recorded: {exc}\n")


def _run(
    top: Path,
    environment: Environment,
    selected: Sequence[Check],
    changed: Sequence[str],
    *,
    pre_push: bool,
    sink: TextIO,
    dirty: bool = False,
) -> int:
    # lc-debt: checks run on the working tree, not the pushed commit's tree; push from a clean tree for an exact
    # result. A Workspaces task sandbox (M4) checks out the exact tree instead.
    failed: list[str] = []
    results: dict[str, tuple[Fingerprint, str]] = {}

    def read_lockfile(path: str) -> bytes | None:
        try:
            return (top / path).read_bytes()
        except OSError:
            return None

    with tempfile.TemporaryDirectory(prefix="lc-verify-") as scratch:
        listing = Path(scratch) / "changed"
        existing = [path for path in changed if os.path.exists(top / path)]
        listing.write_text("".join(f"{path}\n" for path in existing), encoding="utf-8")
        env = {**os.environ, "LC_CHANGED": str(listing)}
        for check in selected:
            sink.write(f"lc verify: [{check.id}] $ {check.run}\n")
            if check.needs:
                sink.write(f"lc verify: [{check.id}] needs {', '.join(check.needs)}; lc verify does not start them\n")
            sink.flush()
            status = _run_check(check, top, env)
            if status == "timed_out":
                sink.write(f"lc verify: [{check.id}] stopped after its timeout of {check.timeout} s\n")
            if status != "passed":
                failed.append(check.id)
            results[check.id] = (environment.fingerprint(check, read_lockfile), status)
    if results:
        _record(top, results, dirty=dirty, sink=sink)
    if failed:
        sink.write(f"lc verify: FAILED: {', '.join(failed)}\n")
        if pre_push:
            sink.write("lc verify: fix the failures; only a person may bypass with --no-verify.\n")
        return 1
    sink.write(f"lc verify: {len(selected)} check(s) passed\n")
    return 0
