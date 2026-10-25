"""``lc review hook``: run the local AI review from a git hook.

``lc review hook install`` writes a small ``pre-push`` (or ``pre-commit``) hook that calls ``lc review hook run``; the
hook script holds almost no logic of its own, so the range of a push, the exit codes and the fail-open rules are the
tested code below and not shell. It never overwrites a hook that exists unless ``--force`` is given, and installing
twice writes nothing the second time.

``lc review hook run --pre-push`` reads the ref updates git passes a pre-push hook on standard input and reviews what
each one would publish: the commits on the pushed branch the remote does not have. ``--pre-commit`` reviews the staged
changes. Both run ``lc review --ai`` (:mod:`lemoncrow.pro.capabilities.review.pr_bot.local`) and exit with its codes:
0 clean, 1 an open finding at or above ``--fail-on``, 2 an incomplete review under ``--strict``, 3 a usage error.

A hook must never stop a push because LemonCrow broke. A review that could not be done (no key, a provider error, a
revision that cannot be resolved) never blocks without ``--strict``, and neither does a crash of ``lc`` itself, a
missing ``lc`` or an installation that lost a file: those exit 1 like a finding would, so the script cannot tell them
apart by the status. It asks ``hook run`` to answer "stop" with a code nothing else produces
(:data:`STOP_ENV`, :data:`STOP_CODE`) and lets everything else through with a one-line notice.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

HOOK_KINDS = ("pre-push", "pre-commit")
#: The line that marks a hook as ours; ``install`` tells its own hook from someone else's by it.
HOOK_MARKER_PREFIX = "# lemoncrow-review-hook"
#: The marker of the script written today. v1 called a bare ``lc`` from the PATH and let any crash stop the push.
HOOK_MARKER = f"{HOOK_MARKER_PREFIX} v2"
SKIP_ENV = "LEMONCROW_REVIEW_SKIP"
#: The hook script sets this to ask ``hook run`` to answer "stop the push" with :data:`STOP_CODE` instead of 1 or 2.
STOP_ENV = "LEMONCROW_HOOK_STOP"
#: Outside what a crash (1), a usage error (2, 3), a missing command (126, 127) or a signal (128 and up) exit with.
STOP_CODE = 87
#: The installed client sets this to its own path before it hands a command to the engine, so the hook calls it.
LC_ENV = "LEMONCROW_LC_BIN"
ZERO = "0" * 40
_MAX_STDIN = 256 * 1024
_MAX_UPDATES = 32
#: Where a new branch is compared from, first one that exists: the remote's default branch, then the usual names.
_DEFAULT_BRANCHES = ("HEAD", "main", "master")


def resolve_lc_path() -> str:
    """The absolute path of the ``lc`` that is installing the hook, or ``""`` when it cannot be told.

    The installed client names itself in :data:`LC_ENV`; a source checkout's ``uv run lc`` is ``sys.argv[0]``. A bare
    ``lc`` from the PATH is the fallback inside the script, because it may be another install's.
    """

    named = os.environ.get(LC_ENV, "").strip()
    if named and os.path.isfile(named) and os.access(named, os.X_OK):
        return os.path.abspath(named)
    invoked = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if invoked is not None and invoked.name in {"lc", "lemoncrow"} and invoked.is_file():
        return os.path.abspath(invoked)
    return ""


def hook_script(kind: str, *, fail_on: str = "p0", strict: bool = False, depth: str = "auto", lc_path: str = "") -> str:
    """The text of the hook: POSIX shell around ``lc review hook run`` that fails open.

    It calls *lc_path* (the ``lc`` that wrote it), else the first ``lc`` on the PATH. It stops the push only when
    ``hook run`` answers with :data:`STOP_CODE`; any other status (a crash, a missing file, a usage error) lets it
    through and says so on one line.
    """

    if kind not in HOOK_KINDS:
        raise ValueError(f"unknown hook {kind!r}")
    options = f"--{kind} --fail-on {fail_on} --depth {depth}" + (" --strict" if strict else "")
    what = "what this push would publish" if kind == "pre-push" else "the staged changes"
    verb = "push" if kind == "pre-push" else "commit"
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER} ({kind})\n"
        f"# Written by `lc review hook install`. Reviews {what} on your own model key, and stops the {verb}\n"
        f"# while a finding at or above `{fail_on}` is open. It never stops the {verb} because LemonCrow broke: a missing\n"
        "# or crashing lc, no model key, a provider error or a spent budget let it through"
        + (" (--strict also stops it when the review is incomplete)" if strict else "")
        + ".\n"
        f"# Skip once: {SKIP_ENV}=1 git {verb}   Remove: delete this file. Change it: `lc review hook install --force`.\n"
        f'if [ -n "${{{SKIP_ENV}:-}}" ]; then exit 0; fi\n'
        f"LC={shlex.quote(lc_path)}\n"
        'if [ -z "$LC" ] || [ ! -x "$LC" ]; then LC=$(command -v lc 2>/dev/null) || LC=""; fi\n'
        'if [ -z "$LC" ]; then\n'
        f'  echo "lemoncrow: lc was not found; not reviewing this {verb}." >&2\n'
        "  exit 0\n"
        "fi\n"
        f'{STOP_ENV}={STOP_CODE} "$LC" review hook run {options} "$@"\n'
        "code=$?\n"
        f'if [ "$code" -eq {STOP_CODE} ]; then exit 1; fi\n'
        'if [ "$code" -ne 0 ]; then\n'
        f'  echo "lemoncrow: the review could not run (lc exited $code); not blocking this {verb}." >&2\n'
        "fi\n"
        "exit 0\n"
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def hook_path(repo_root: Path, kind: str) -> Path:
    """Where git runs *kind* from for this repository (``core.hooksPath`` and worktrees included)."""

    found = _git(repo_root, "rev-parse", "--git-path", f"hooks/{kind}")
    if found.returncode != 0 or not found.stdout.strip():
        raise click.ClickException(f"{repo_root} is not inside a Git repository")
    path = Path(found.stdout.strip())
    return path if path.is_absolute() else repo_root / path


def _repo_root(repo_root: Path | None) -> Path:
    where = (repo_root or Path.cwd()).expanduser()
    top = _git(where, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise click.ClickException(f"{where} is not inside a Git repository")
    return Path(top.stdout.strip())


def _is_ours(text: str) -> bool:
    return HOOK_MARKER_PREFIX in text


_LC_LINE = re.compile(r"(?m)^LC=.*$")


def _upgradable(existing: str, script: str) -> bool:
    """Our hook of an older version (v1 let a crash of ``lc`` stop the push), or the same hook for another ``lc`` path."""

    if HOOK_MARKER not in existing:
        return True
    return _LC_LINE.sub("LC=", existing) == _LC_LINE.sub("LC=", script)


def install_hook(path: Path, script: str, *, force: bool) -> str:
    """Write *script* to *path*; ``installed``, ``unchanged`` or ``replaced``. Raises ``ClickException`` rather than
    overwrite a different hook without *force*."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = None
    except (OSError, UnicodeDecodeError) as exc:
        if not force:
            raise click.ClickException(f"{path} exists and cannot be read ({exc}); use --force to replace it") from exc
        existing = ""
    if existing == script:
        os.chmod(path, 0o755)
        return "unchanged"
    if existing is not None and not force and _is_ours(existing) and _upgradable(existing, script):
        force = True  # our own hook, only older or pointing at another lc: refreshing it loses nothing of the person's
    if existing is not None and not force:
        if _is_ours(existing):
            raise click.ClickException(
                f"{path} is a LemonCrow review hook with other options; use --force to replace it"
            )
        raise click.ClickException(
            f"{path} already exists and is not a LemonCrow hook, so it was left alone. Use --print to see the line "
            "to add to it, or --force to replace it"
        )
    if existing is None:
        try:
            with path.open("x", encoding="utf-8") as handle:  # exclusive: never clobbers a hook written meanwhile
                handle.write(script)
        except FileExistsError as exc:
            raise click.ClickException(f"{path} appeared while installing (or is a broken link); left alone") from exc
    else:
        path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)
    return "installed" if existing is None else "replaced"


def record_installed_hook(path: Path) -> None:
    """Remember where a hook went, in ``<root>/installed-hooks.txt``, so ``lc uninstall`` can list every one of them.

    Best effort: a hook that cannot be recorded still works, and ``lc uninstall`` also looks in the repository it runs in.
    """

    from lemoncrow.core.foundation.paths import default_store_root

    try:
        record = default_store_root() / "installed-hooks.txt"
        known = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
        if str(path) not in known:
            record.parent.mkdir(parents=True, exist_ok=True)
            with record.open("a", encoding="utf-8") as handle:
                handle.write(f"{path}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# what a push publishes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RefUpdate:
    """One line git writes to a pre-push hook: ``<local ref> <local sha> <remote ref> <remote sha>``."""

    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str


@dataclass(frozen=True)
class PushRange:
    """The commits one ref update would publish, as a base and a head, or why there is nothing to review."""

    update: RefUpdate
    base: str = ""
    head: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        return self.update.remote_ref or self.update.local_ref


def parse_updates(text: str) -> list[RefUpdate]:
    rows: list[RefUpdate] = []
    for line in text.splitlines()[:_MAX_UPDATES]:
        parts = line.split()
        if len(parts) == 4:
            rows.append(RefUpdate(*parts))
    return rows


def _resolves(repo: Path, rev: str) -> str:
    found = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return found.stdout.strip() if found.returncode == 0 else ""


def _merge_base(repo: Path, left: str, right: str) -> str:
    found = _git(repo, "merge-base", left, right)
    return found.stdout.strip() if found.returncode == 0 else ""


def plan_push(repo: Path, update: RefUpdate, remote: str = "origin") -> PushRange:
    """What *update* publishes. A deletion and a tag publish nothing to review; an existing branch is reviewed from
    what the remote has (their merge base, when history was rewritten); a new branch from where it leaves the remote's
    default branch, else from its own parent."""

    if update.local_sha == ZERO:
        return PushRange(update, note="deleting a branch: nothing to review")
    if update.remote_ref.startswith("refs/tags/") or update.local_ref.startswith("refs/tags/"):
        return PushRange(update, note="a tag: nothing to review")
    head = _resolves(repo, update.local_sha)
    if not head:
        return PushRange(update, note=f"{update.local_sha[:12]} is not a commit in this repository")
    if update.remote_sha != ZERO:
        if _resolves(repo, update.remote_sha):
            base = _merge_base(repo, update.remote_sha, head)
            if base == head:
                return PushRange(update, note="the remote already has these commits")
            if base:
                return PushRange(update, base, head)
        # The remote has a commit this clone has never fetched: treat it as a new branch.
    names = [f"refs/remotes/{remote}/{name}" for name in _DEFAULT_BRANCHES]
    names += [f"refs/remotes/origin/{name}" for name in _DEFAULT_BRANCHES if remote != "origin"]
    names += ["refs/heads/main", "refs/heads/master"]
    for name in names:
        if name in (update.local_ref, update.remote_ref):
            continue  # a branch is not the base of itself
        tip = _resolves(repo, name)
        base = _merge_base(repo, tip, head) if tip else ""
        if base == head:
            return PushRange(update, note=f"{name.rsplit('/', 1)[-1]} already has these commits")
        if base:
            return PushRange(update, base, head)
    parent = _resolves(repo, f"{head}~1")
    if parent:
        return PushRange(
            update, parent, head, "no default branch to compare with: reviewing only the last commit of the push"
        )
    return PushRange(update, note="the first commit of a repository: nothing to compare it with")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def _store_root(ctx: click.Context) -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    obj = ctx.find_root().obj or {}
    return Path(obj.get("root") or default_store_root())


@click.group("hook")
def hook_group() -> None:
    """Review from a git hook: `install` writes the hook, `run` is what it calls."""


_KIND_OPTIONS: Any = [
    click.option("--pre-push", "kind", flag_value="pre-push", default=True, help="The pre-push hook (the default)."),
    click.option(
        "--pre-commit", "kind", flag_value="pre-commit", help="The pre-commit hook, which reviews what is staged."
    ),
    click.option(
        "--fail-on",
        type=click.Choice(["none", "p0", "p1"]),
        default="p0",
        show_default=True,
        help="Exit 1 (stop the push or commit) while a finding at or above this severity is open.",
    ),
    click.option(
        "--strict",
        is_flag=True,
        help="Exit 2 when the review is incomplete (no model route, a spent budget, risk signals not examined) instead of letting it through.",
    ),
    click.option(
        "--depth",
        type=click.Choice(["auto", "thorough", "deep"]),
        default="auto",
        show_default=True,
        help="Review depth; the hard model-call cap applies at every depth.",
    ),
]


def _kind_options(command: Any) -> Any:
    for option in reversed(_KIND_OPTIONS):
        command = option(command)
    return command


@hook_group.command("install")
@_kind_options
@click.option("--print", "print_only", is_flag=True, help="Print the hook script and write nothing.")
@click.option(
    "--force", is_flag=True, help="Replace a hook that already exists (without it, none is ever overwritten)."
)
@click.option(
    "--repo-root", type=click.Path(path_type=Path, file_okay=False), default=None, help="Repository. Default: here."
)
def hook_install(
    kind: str, fail_on: str, strict: bool, depth: str, print_only: bool, force: bool, repo_root: Path | None
) -> None:
    """Install the review hook (pre-push by default), or print it with --print.

    The hook runs `lc review --ai` on what is about to be pushed (or committed) with your own model key and exits with
    its codes: 0 clean, 1 a finding is open, 2 incomplete (--strict only), 3 usage error. It never overwrites a hook
    that exists without --force, and running this twice changes nothing.
    """

    lc_path = resolve_lc_path()
    script = hook_script(kind, fail_on=fail_on, strict=strict, depth=depth, lc_path=lc_path)
    if print_only:
        click.echo(script, nl=False)
        return
    root = _repo_root(repo_root)
    path = hook_path(root, kind)
    outcome = install_hook(path, script, force=force)
    record_installed_hook(path)
    if outcome == "unchanged":
        click.echo(f"Already installed: {path} (nothing changed)")
        return
    verb = "push" if kind == "pre-push" else "commit"
    click.echo(f"{'Replaced' if outcome == 'replaced' else 'Installed'} the {kind} review hook: {path}")
    click.echo(
        f"  It stops the {verb} while a finding at or above {fail_on} is open, and never because LemonCrow broke."
    )
    click.echo(f"  It calls {lc_path or 'the first `lc` on your PATH'}.")
    click.echo(f"  Skip once with {SKIP_ENV}=1, or git's --no-verify. Remove it by deleting the file.")


class _HookUsageError(click.UsageError):
    """A hook command line that cannot be used: exit 3, so it is not taken for a finding (1) or an incomplete review (2)."""

    exit_code = 3


class _HookRunCommand(click.Command):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as exc:
            raise _HookUsageError(exc.message, ctx=exc.ctx) from exc


def _stop_code() -> int:
    """The status the hook script asked "stop" to be answered with (:data:`STOP_ENV`), or 0 when it did not."""

    try:
        return max(0, int(os.environ.get(STOP_ENV, "") or 0))
    except ValueError:
        return 0


def _stdin_is_terminal() -> bool:
    return sys.stdin.isatty()


@hook_group.command("run", cls=_HookRunCommand)
@_kind_options
@click.option(
    "--repo-root", type=click.Path(path_type=Path, file_okay=False), default=None, help="Repository. Default: here."
)
@click.argument("git_args", nargs=-1)
@click.pass_context
def hook_run(
    ctx: click.Context,
    kind: str,
    fail_on: str,
    strict: bool,
    depth: str,
    repo_root: Path | None,
    git_args: tuple[str, ...],
) -> None:
    """What the installed hook calls: review the push (stdin carries git's ref updates) or the staged changes."""

    from lemoncrow.pro.capabilities.review.pr_bot import local

    if os.environ.get(SKIP_ENV):
        return
    root = _repo_root(repo_root)
    store = _store_root(ctx)
    jobs: list[tuple[str, local.LocalOptions]] = []
    plans: list[PushRange] = []
    if kind == "pre-commit":
        jobs.append(
            (
                "the staged changes",
                local.LocalOptions(root, store, staged=True, depth=depth, fail_on=fail_on, strict=strict),
            )
        )
    else:
        if _stdin_is_terminal():
            raise _HookUsageError("the pre-push hook reads git's ref updates on standard input; git provides them")
        remote = (
            git_args[0]
            if git_args and not git_args[0].startswith(("-", "/")) and "://" not in git_args[0]
            else "origin"
        )
        plans = [plan_push(root, row, remote) for row in parse_updates(sys.stdin.read(_MAX_STDIN))]
        for plan in plans:
            if not plan.base:
                if plan.note:
                    click.echo(f"LemonCrow review: {plan.label}: {plan.note}.", err=True)
                continue
            options = local.LocalOptions(
                root,
                store,
                base=plan.base,
                head=plan.head,
                depth=depth,
                fail_on=fail_on,
                strict=strict,
                label=plan.label,
            )
            jobs.append((f"{plan.label} ({plan.base[:7]}..{plan.head[:7]})", options))
    codes: list[int] = []
    for title, options in jobs:
        click.echo(f"LemonCrow review of {title}…", err=True)
        codes.append(_review_one(options))
    code = _combine(codes)
    if code == local.EXIT_FOUND:
        click.echo(
            f"LemonCrow review: stopped by an open finding at or above {fail_on}. "
            f"Fix it, or skip once with {SKIP_ENV}=1 (git --no-verify skips every hook).",
            err=True,
        )
    elif code == local.EXIT_INCOMPLETE:
        click.echo("LemonCrow review: stopped because the review is incomplete and --strict is set.", err=True)
    if code in (local.EXIT_FOUND, local.EXIT_INCOMPLETE):
        ctx.exit(_stop_code() or code)  # the hook script asks for a code that cannot be taken for a crash
    if code:
        ctx.exit(code)


def _review_one(options: Any) -> int:
    """One review, printed to stderr. Anything that is not a finding is an incomplete review, never a crash."""

    from lemoncrow.pro.capabilities.review.pr_bot import local

    try:
        report = local.run_local_review(options, progress=lambda message: click.echo(f"  {message}…", err=True))
    except local.LocalReviewError as exc:
        click.echo(f"LemonCrow review could not run: {exc}", err=True)
        return local.EXIT_INCOMPLETE if options.strict else local.EXIT_CLEAN
    except Exception as exc:  # a hook must not read an internal error as a finding (exit 1)
        click.echo(f"LemonCrow review failed unexpectedly ({type(exc).__name__}: {exc}); not blocking.", err=True)
        return local.EXIT_INCOMPLETE if options.strict else local.EXIT_CLEAN
    plain = bool(os.environ.get("NO_COLOR"))
    click.echo(
        local.render_text(report, style=None if plain else click.style),
        nl=False,
        err=True,
        color=False if plain else None,
    )
    return report.exit_code


def _combine(codes: list[int]) -> int:
    """One exit status for several ref updates: an open finding first, then a usage error, then an incomplete review."""

    for wanted in (1, 3, 2):
        if wanted in codes:
            return wanted
    return 0


__all__ = [
    "HOOK_MARKER",
    "STOP_CODE",
    "STOP_ENV",
    "PushRange",
    "RefUpdate",
    "hook_group",
    "hook_path",
    "hook_script",
    "install_hook",
    "parse_updates",
    "plan_push",
    "record_installed_hook",
    "resolve_lc_path",
]
