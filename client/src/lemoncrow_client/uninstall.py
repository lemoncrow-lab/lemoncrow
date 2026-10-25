"""``lc uninstall``: say what the installer added, remove it, and ask before anything that is yours.

    lc uninstall review|all [--dry-run] [--yes] [--purge]

The plan is built once, from what is on disk, and the same plan is printed, previewed (``--dry-run``) and executed, so a
dry run cannot list something the real run does not remove, or the reverse. It has three parts:

* **Installed by LemonCrow**: the commands, the Review client and engine (a Python environment), the installer's scripts and
  Reader, the carried review results (a cache), the background server's process files, the lines the installer added to
  your shell startup files (``~/.bashrc``, ``~/.profile`` ...) and the git hooks ``lc review hook install`` wrote. A hook
  is found by its marker line, in the repositories ``lc review hook install`` listed in ``installed-hooks.txt`` and in the
  one you are standing in; it calls ``lc``, so it goes with ``lc`` and no repository keeps a hook for a command that is gone.
  Removed after one question (or ``--yes``).
* **Yours**: the settings file (it can hold an API key you saved), the reviews you opened in the Reader, the telemetry id of
  the full CLI, a hosted sign-in. Each group is asked about; ``--yes`` keeps them and ``--purge`` removes them.
* **In your repositories**: for the repository you are standing in, the ``.lemoncrow`` folder and the small files a review
  left. Asked about, never removed by ``--yes``.

It never runs ``npm``, never touches coding-agent settings except through ``lc rt disable`` when Runtime is removed too, and
never deletes a folder it does not recognise: what it leaves is listed.

Standard library only, and every module it needs is imported before the first file is removed, because the Python
environment it runs from is one of the things it deletes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

__all__ = ["USAGE", "Step", "build_plan", "main"]

USAGE: Final[str] = "usage: lc uninstall review|runtime|all [--dry-run] [--yes] [--purge]\n"

_BLOCK_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("# >>> lemoncrow path setup >>>", "# <<< lemoncrow path setup <<<"),
    ("# >>> lemoncrow >>>", "# <<< lemoncrow <<<"),
)
_HOOK_MARKER: Final[str] = "# lemoncrow-review-hook"
_PROOF_FILE: Final[str] = ".lemoncrow-local-fs-proof"
HOOK_RECORD: Final[str] = "installed-hooks.txt"
#: The commands the Review install links into the bin directory.
_REVIEW_LINKS: Final[tuple[str, ...]] = ("lc", "lcr", "lemoncrow-review-client")
_PROFILES: Final[tuple[str, ...]] = (".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshenv", ".zprofile")


@dataclass(frozen=True)
class Step:
    """One thing the uninstall does. ``group`` is ``installed``, ``yours`` or ``repo``."""

    group: str
    action: str
    text: str
    path: Path | None = None
    #: A second line for the person: why it is there, or what is in it.
    note: str = ""
    #: Which question covers it when it is asked about: ``data`` or ``repo``.
    ask: str = ""


# --------------------------------------------------------------------------- #
# what is on disk
# --------------------------------------------------------------------------- #


def _tilde(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def _size(path: Path) -> int:
    if path.is_symlink() or not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _human(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(size // 1024, 1)} KB" if size else "empty"
    return f"{size / (1024 * 1024):.0f} MB"


def _git(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _profiles(user_home: Path, env: Mapping[str, str]) -> list[Path]:
    """Every shell startup file the installer may have written to: it picks by ``$SHELL``, and a later shell reads others."""

    zdot = Path(env.get("ZDOTDIR") or user_home)
    names = [user_home / name for name in _PROFILES]
    names += [zdot / ".zshrc", zdot / ".zshenv", zdot / ".zprofile", user_home / ".config" / "fish" / "config.fish"]
    seen: dict[Path, None] = {}
    for path in names:
        seen.setdefault(path, None)
    return list(seen)


def _block_bounds(text: str) -> tuple[int, int] | None:
    """``(start, end)`` offsets of the installer's block in *text* (end after its newline), or ``None``."""

    for start_marker, end_marker in _BLOCK_MARKERS:
        start = text.find(start_marker)
        if start < 0:
            continue
        end = text.find(end_marker, start)
        if end < 0:
            return None  # an incomplete block is not ours to cut
        end += len(end_marker)
        if text[end : end + 1] == "\n":
            end += 1
        return start, end
    return None


def _owns_link(link: Path, home: Path) -> bool:
    """A command link that points into this LemonCrow home (never a foreign program of the same name)."""

    if not link.is_symlink():
        return False
    try:
        target = Path(os.path.realpath(link))
        return target.is_relative_to(Path(os.path.realpath(home)))
    except (OSError, ValueError):
        return False


def _recorded_hooks(home: Path) -> list[Path]:
    try:
        lines = (home / HOOK_RECORD).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [Path(line.strip()) for line in lines if line.strip()]


def _is_our_hook(path: Path) -> bool:
    try:
        return path.is_file() and _HOOK_MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def build_plan(
    product: str, *, env: Mapping[str, str], cwd: Path, home: Path | None = None, user_home: Path | None = None
) -> list[Step]:
    """Every step the uninstall of *product* (``review`` or ``all``) would take, from what is on disk now."""

    user = user_home or Path(env.get("HOME") or Path.home())
    state = home or Path(env.get("LEMONCROW_HOME", "").strip() or user / ".lemoncrow")
    bin_dir = Path(env.get("LEMONCROW_BIN_DIR", "").strip() or state / "bin")
    runtime_present = (state / "runtime").is_dir()
    keep_shared = (
        product == "review" and runtime_present
    )  # Runtime still needs the scripts, the shell lines, the server
    steps: list[Step] = _hook_steps(state, cwd, user)

    def tilde(path: Path) -> str:
        return _tilde(path, user)

    if product == "all" and runtime_present:
        steps.append(
            Step(
                "installed",
                "runtime",
                "Runtime: its coding-agent entries (claude, codex, opencode, pi) and its client",
                state / "runtime",
                f"{tilde(state / 'runtime')} ({_human(_size(state / 'runtime'))}); the same as `lc uninstall runtime`",
            )
        )
    if keep_shared:
        steps.append(
            Step(
                "installed",
                "review_script",
                "Review: its client and engine, and its commands (Runtime keeps working)",
                state / "review",
                f"{tilde(state / 'review')} ({_human(_size(state / 'review'))}); the background server moves to Runtime",
            )
        )
    server_script = state / "install" / "scripts" / "local_server.sh"
    if not keep_shared and server_script.is_file() and (state / "server").is_dir():
        steps.append(Step("installed", "stop_server", "Stop the background Review server", server_script))
    if not keep_shared:
        links = [bin_dir / name for name in _REVIEW_LINKS if _owns_link(bin_dir / name, state)]
        links += (
            [bin_dir / "lemoncrow-runtime-client"]
            if product == "all" and _owns_link(bin_dir / "lemoncrow-runtime-client", state)
            else []
        )
        if links:
            steps.append(
                Step(
                    "installed",
                    "unlink",
                    "The commands " + ", ".join(link.name for link in links),
                    links[0],
                    f"in {tilde(bin_dir)}",
                )
            )
        review = state / "review"
        if review.exists():
            steps.append(
                Step(
                    "installed",
                    "remove",
                    "The Review client and engine",
                    review,
                    f"{tilde(review)} ({_human(_size(review))})",
                )
            )
    cache = state / "review-local-ai"
    if cache.exists():
        steps.append(
            Step(
                "installed",
                "remove",
                "Carried review results (a cache)",
                cache,
                f"{tilde(cache)} ({_human(_size(cache))})",
            )
        )
    if not keep_shared:
        for name, what in (
            ("install", "The installer's scripts and the Reader"),
            ("shell-env.sh", "The shell setup file"),
        ):
            target = state / name
            if target.exists():
                steps.append(Step("installed", "remove", what, target, f"{tilde(target)} ({_human(_size(target))})"))
        token = state / "token"
        if token.exists():
            steps.append(Step("installed", "remove", "The local server's token", token, tilde(token)))
        server = state / "server"
        if server.is_dir():
            leftovers = [child for child in server.iterdir() if child.name != "data"]
            if leftovers:
                steps.append(
                    Step(
                        "installed",
                        "remove_children",
                        "The background server's log and process files",
                        server,
                        f"{tilde(server)}, everything but data/",
                    )
                )
        for profile in _profiles(user, env):
            try:
                text = profile.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _block_bounds(text) is not None:
                steps.append(
                    Step(
                        "installed",
                        "rc_block",
                        f"The lines the installer added to {tilde(profile)}",
                        profile,
                        "the 'lemoncrow path setup' block, nothing else in the file",
                    )
                )

        # Yours: asked about, one question for the group.
        config_dir = Path(env.get("XDG_CONFIG_HOME") or user / ".config") / "lemoncrow"
        data_items = [
            (state / "plugin_settings.json", "Your settings", "includes any API key you saved with lc settings"),
            (state / "server" / "data", "The reviews you opened in the Reader", "marks, comments and snapshots"),
            (state / "account", "Your hosted sign-in", "credentials from lc auth login"),
            (state / "telemetry.db", "Local usage counters", "written by the full CLI"),
            (config_dir, "The full CLI's telemetry id and notice", "a source checkout's lc only"),
        ]
        for path, what, note in data_items:
            if path.exists():
                steps.append(
                    Step("yours", "remove", what, path, f"{tilde(path)} ({_human(_size(path))}); {note}", ask="data")
                )

    steps += _repository_steps(state, cwd, user)
    return steps


def _repo_top(cwd: Path) -> Path | None:
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def _hook_steps(state: Path, cwd: Path, user: Path) -> list[Step]:
    """The hooks ``lc review hook install`` wrote, and the list it kept of them: installed by LemonCrow, so removed with it.

    A hook is ours when it carries the marker line, wherever it is: in a repository the installer listed, or in the one
    *cwd* is in. A listed path that is gone, or that someone has since replaced with their own hook, is not touched.
    """

    seen: dict[Path, None] = {}
    for hook in _recorded_hooks(state):
        if _is_our_hook(hook):
            seen.setdefault(hook, None)
    repo = _repo_top(cwd)
    if repo is not None:
        hooks_dir = _git(repo, "rev-parse", "--git-path", "hooks")
        if hooks_dir:
            base = Path(hooks_dir) if Path(hooks_dir).is_absolute() else repo / hooks_dir
            for name in ("pre-push", "pre-commit"):
                if _is_our_hook(base / name):
                    seen.setdefault(base / name, None)
    steps = [
        Step("installed", "remove", "A git hook written by lc review hook install", hook, _tilde(hook, user))
        for hook in seen
    ]
    record = state / HOOK_RECORD
    if record.exists():
        # Its own bookkeeping goes with the hooks it lists.
        steps.append(Step("installed", "remove", "The list of hooks it keeps", record, _tilde(record, user)))
    return steps


def _repository_steps(state: Path, cwd: Path, user: Path) -> list[Step]:
    steps: list[Step] = []
    repo = _repo_top(cwd)
    if repo is None:
        return steps
    proof = repo / _PROOF_FILE
    if proof.exists():
        steps.append(Step("repo", "remove", "A marker file a review left", proof, _tilde(proof, user), ask="repo"))
        exclude = _git(repo, "rev-parse", "--git-path", "info/exclude")
        if exclude:
            path = Path(exclude) if Path(exclude).is_absolute() else repo / exclude
            if path.is_file() and f"/{_PROOF_FILE}" in path.read_text(encoding="utf-8", errors="replace").splitlines():
                steps.append(
                    Step(
                        "repo",
                        "exclude_line",
                        "The line it added to .git/info/exclude",
                        path,
                        f"/{_PROOF_FILE}",
                        ask="repo",
                    )
                )
    folder = repo / ".lemoncrow"
    if folder.exists():
        if _git(repo, "ls-files", "--", ".lemoncrow"):
            # review.yaml is committed to this repository: it is the team's file, not something LemonCrow left. Only
            # the index it built inside is offered.
            index = folder / "workspace"
            if index.exists():
                steps.append(
                    Step(
                        "repo",
                        "remove",
                        "The workspace index in this repository's .lemoncrow folder (the committed files there stay)",
                        index,
                        f"{_tilde(index, user)} ({_human(_size(index))})",
                        ask="repo",
                    )
                )
        else:
            steps.append(
                Step(
                    "repo",
                    "remove",
                    "The .lemoncrow folder here (the workspace index; it may hold your review.yaml)",
                    folder,
                    f"{_tilde(folder, user)} ({_human(_size(folder))})",
                    ask="repo",
                )
            )
    return steps


# --------------------------------------------------------------------------- #
# doing it
# --------------------------------------------------------------------------- #


def _remove_block(path: Path) -> str:
    """Cut the installer's block out of *path*; returns what happened, in words."""

    text = path.read_text(encoding="utf-8")
    bounds = _block_bounds(text)
    if bounds is None:
        return "left unchanged (the block is incomplete)"
    start, end = bounds
    kept = text[:start].rstrip("\n") + ("\n" if text[:start].strip() else "") + text[end:].lstrip("\n")
    path.write_text(kept, encoding="utf-8")
    return "done"


def _remove_exclude_line(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [line for line in lines if line != f"/{_PROOF_FILE}"]
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return "done"


def _delete(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _prune_empty(state: Path) -> None:
    """Remove the folders the installer made once nothing is left in them (never one it did not make)."""

    for folder in (state / "server", state / "bin", state):
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            continue


def _bash(script: Path, *args: str, env: Mapping[str, str]) -> int:
    return subprocess.run(["bash", str(script), *args], env=dict(env), check=False).returncode


def _execute(step: Step, *, env: Mapping[str, str], state: Path, sink: TextIO) -> str:
    try:
        if step.action == "stop_server" and step.path is not None:
            _bash(step.path, "stop", env={**env, "LEMONCROW_HOME": str(state)})
        elif step.action in {"remove", "unlink"} and step.path is not None:
            if step.action == "unlink":
                for name in (*_REVIEW_LINKS, "lemoncrow-runtime-client"):
                    link = step.path.parent / name
                    if _owns_link(link, state):
                        link.unlink(missing_ok=True)
            else:
                _delete(step.path)
        elif step.action == "remove_children" and step.path is not None:
            for child in step.path.iterdir():
                if child.name != "data":
                    _delete(child)
        elif step.action == "rc_block" and step.path is not None:
            return _remove_block(step.path)
        elif step.action == "exclude_line" and step.path is not None:
            return _remove_exclude_line(step.path)
        elif step.action in {"runtime", "review_script"}:
            wrapper = state / "install" / "scripts" / "uninstall_product.sh"
            if not wrapper.is_file():
                return f"skipped: {wrapper} is missing"
            code = _bash(
                wrapper, "runtime" if step.action == "runtime" else "review", env={**env, "LEMONCROW_HOME": str(state)}
            )
            return "done" if code == 0 else f"failed (exit {code})"
    except OSError as exc:
        return f"failed ({exc})"
    return "done"


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def _lines(steps: Sequence[Step], *, indent: str = "  ") -> list[str]:
    out: list[str] = []
    for step in steps:
        out.append(f"{indent}{step.text}")
        if step.note:
            out.append(f"{indent}  {step.note}")
    return out


def _ask_default(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def main(
    arguments: Sequence[str],
    sink: TextIO,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    ask: Callable[[str], bool] | None = None,
    interactive: bool | None = None,
) -> int:
    """``lc uninstall review|all ARGUMENTS``; ``0`` done (or previewed), ``2`` a usage problem or nothing was confirmed."""

    environment = dict(os.environ if env is None else env)
    args = list(arguments)
    product = args.pop(0) if args and args[0] in {"review", "all"} else ""
    flags = {"--dry-run", "--yes", "--purge"}
    unknown = [item for item in args if item not in flags]
    if not product or unknown:
        if unknown:
            sink.write(f"unknown option: {unknown[0]}\n")
        sink.write(USAGE)
        return 2
    dry, purge = "--dry-run" in args, "--purge" in args
    yes = "--yes" in args or purge
    tty = sys.stdin.isatty() if interactive is None else interactive
    asker = ask or _ask_default
    here = cwd or Path.cwd()
    user = Path(environment.get("HOME") or Path.home())
    state = Path(environment.get("LEMONCROW_HOME", "").strip() or user / ".lemoncrow")

    steps = build_plan(product, env=environment, cwd=here, home=state, user_home=user)
    installed = [step for step in steps if step.group == "installed"]
    yours = [step for step in steps if step.group != "installed"]
    if not steps:
        sink.write("Nothing to remove: LemonCrow Review is not installed here.\n")
        return 0

    asking = tty and not yes  # a person at the terminal, who will be asked about what is theirs
    also = {"data"} if purge else set()  # --purge answers for the machine's data; a repository's folder is asked
    purged = [step for step in yours if step.ask in also]
    rest = [step for step in yours if step.ask not in also]
    title = "LemonCrow uninstall" + (" (dry run: nothing will be changed)" if dry else "")
    sink.write(f"{title}\n\n")
    if installed:
        sink.write("Removes what the installer added:\n")
        sink.write("\n".join(_lines(installed)) + "\n\n")
    if purged:
        sink.write("With --purge, also removes (yours):\n")
        sink.write("\n".join(_lines(purged)) + "\n\n")
    if rest:
        if dry:
            heading = "Would ask before removing (yours, or in your repositories):"
        elif asking:
            heading = "Asks before removing (yours, or in your repositories):"
        else:
            heading = (
                "Keeps (yours, or in your repositories; --purge removes the first kind, never a repository's folder):"
            )
        sink.write(heading + "\n")
        sink.write("\n".join(_lines(rest)) + "\n\n")
    sink.write("Does not touch: npm packages, coding-agent settings, or the code in your repositories.\n")
    if dry:
        sink.write(
            "Dry run: nothing was changed. --yes skips the questions and keeps your data; --purge removes it too.\n"
        )
        return 0
    if not tty and not yes:
        sink.write(
            "\nNot a terminal, so nothing was removed. Add --yes to remove the first list (your data stays), or --dry-run.\n"
        )
        return 2

    if installed and not yes and not asker("\nRemove the items under 'Removes what the installer added'?"):
        sink.write("Nothing was removed.\n")
        return 0
    sink.write("\n")
    failed = 0
    for step in installed:
        outcome = _execute(step, env=environment, state=state, sink=sink)
        sink.write(f"  {step.text}: {outcome}\n")
        failed += outcome.startswith("failed")

    groups: dict[str, list[Step]] = {}
    for step in yours:
        groups.setdefault(step.ask, []).append(step)
    questions = {
        "data": "Also delete your settings, saved reviews and sign-in?",
        "repo": "Also remove the LemonCrow files in this repository?",
    }
    kept: list[Step] = []
    for key, group in groups.items():
        approved = purge and key != "repo"  # --purge answers for the machine's data; a repository's folder is asked
        if not approved and tty and not yes:
            approved = asker(f"\n{questions[key]}")
        if not approved:
            kept.extend(group)
            continue
        for step in group:
            outcome = _execute(step, env=environment, state=state, sink=sink)
            sink.write(f"  {step.text}: {outcome}\n")
            failed += outcome.startswith("failed")

    _prune_empty(state)
    sink.write("\n")
    if kept and asking:
        sink.write("Kept, as you answered (delete them yourself, or run this again):\n")
        sink.write("\n".join(_lines(kept)) + "\n")
    elif kept:
        sink.write("Kept, as listed above. To remove your data too: lc uninstall all --purge\n")
    elif state.is_dir():
        left = sorted(child.name for child in state.iterdir())
        if left:
            sink.write(f"Left in {_tilde(state, user)} (not created by the installer): {', '.join(left)}\n")
    sink.write(
        "Open a new terminal so the shell forgets the lc command.\n"
        if not failed
        else "Some steps failed; see above.\n"
    )
    return 1 if failed else 0
