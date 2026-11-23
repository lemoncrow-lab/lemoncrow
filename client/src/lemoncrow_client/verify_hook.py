"""``lc verify hook install|uninstall``: make ``lc verify --pre-push`` a repository's pre-push hook.

The hook is a few lines of shell around ``lc verify --pre-push``. It calls the ``lc`` that wrote it (the first ``lc`` on
the PATH only if that file is gone). It is the gate, so it fails closed: a missing ``lc`` stops the push and names the
file to delete. A hook that exists and is not this one is left alone unless ``--force``; installing twice writes nothing
the second time. Its path goes into ``installed-hooks.txt`` under the LemonCrow home, so ``lc uninstall all`` finds it.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, TextIO

from .environment import ENVIRONMENT_FILE, DefinitionError, load_environment

__all__ = ["HOOK_MARKER", "HOOK_USAGE", "hook_main", "hook_script"]

#: Marks a hook as written here. ``uninstall.py`` keeps the same prefix to find it.
HOOK_MARKER_PREFIX: Final[str] = "# lemoncrow-verify-hook"
HOOK_MARKER: Final[str] = f"{HOOK_MARKER_PREFIX} v1"
HOOK_RECORD: Final[str] = "installed-hooks.txt"
_LC_ENV: Final[str] = "LEMONCROW_LC_BIN"

HOOK_USAGE: Final[str] = """usage:
  lc verify hook install [--print] [--force]   write the pre-push hook that runs `lc verify --pre-push`
  lc verify hook uninstall                     remove that hook

The hook goes where Git runs this repository's pre-push hook (.git/hooks, or core.hooksPath). --print shows it and
writes nothing; --force replaces a pre-push hook that is not this one.
"""


def _lc_path(env: Mapping[str, str]) -> str:
    named = env.get(_LC_ENV, "").strip()
    if named and os.path.isfile(named) and os.access(named, os.X_OK):
        return os.path.abspath(named)
    invoked = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if invoked is not None and invoked.name in {"lc", "lemoncrow"} and invoked.is_file():
        return os.path.abspath(invoked)
    return ""


def hook_script(lc_path: str) -> str:
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        "# Written by `lc verify hook install`. Runs the checks in .lemoncrow/environment.toml that the pushed\n"
        "# commits touch and stops the push when one fails or lc cannot run them.\n"
        "# Remove: `lc verify hook uninstall`, or delete this file.\n"
        f"LC={shlex.quote(lc_path)}\n"
        'if [ -z "$LC" ] || [ ! -x "$LC" ]; then LC=$(command -v lc 2>/dev/null) || LC=""; fi\n'
        'if [ -z "$LC" ]; then\n'
        '  echo "lc verify: lc was not found, so the checks cannot run. To push without them, delete $0" >&2\n'
        "  exit 1\n"
        "fi\n"
        'exec "$LC" verify --pre-push "$@"\n'
    )


def _git(top: Path, *args: str) -> str | None:
    completed = subprocess.run(["git", *args], cwd=top, capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


def _read(path: Path) -> str | None:
    """The hook's text; ``None`` when there is none, ``""`` when it cannot be read."""

    if not os.path.lexists(path):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _record(path: Path, env: Mapping[str, str]) -> None:
    """Best effort: a hook that cannot be recorded still works, and ``lc uninstall`` also looks in the current repository."""

    home = env.get("LEMONCROW_HOME", "").strip() or str(Path(env.get("HOME") or Path.home()) / ".lemoncrow")
    record = Path(home) / HOOK_RECORD
    try:
        known = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
        if str(path) not in known:
            record.parent.mkdir(parents=True, exist_ok=True)
            with record.open("a", encoding="utf-8") as handle:
                handle.write(f"{path}\n")
    except OSError:
        pass


def _install(top: Path, path: Path, env: Mapping[str, str], *, force: bool, show: bool, sink: TextIO) -> int:
    try:
        if load_environment(top) is None:
            sink.write(f"lc verify: {ENVIRONMENT_FILE} is missing; define the checks before installing the hook\n")
            return 2
    except DefinitionError as exc:
        sink.write(f"lc verify: {exc}\n")
        return 2
    script = hook_script(_lc_path(env))
    if show:
        sink.write(script)
        return 0
    existing = _read(path)
    if existing is not None and existing != script and HOOK_MARKER_PREFIX not in existing and not force:
        sink.write(
            f"lc verify: {path} already exists and is not this hook, so it was left alone. "
            'Add `lc verify --pre-push "$@"` to it, or use --force to replace it\n'
        )
        return 2
    if existing != script:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if existing is None:
                # Exclusive: never clobbers a hook written meanwhile, or a broken link.
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(script)
            else:
                path.write_text(script, encoding="utf-8")
        except OSError as exc:
            sink.write(f"lc verify: could not write {path}: {exc}\n")
            return 2
    os.chmod(path, 0o755)
    _record(path, env)
    state = "unchanged" if existing == script else "installed" if existing is None else "replaced"
    sink.write(f"lc verify: pre-push hook {state}: {path}\n")
    return 0


def _uninstall(path: Path, sink: TextIO) -> int:
    existing = _read(path)
    if existing is None:
        sink.write(f"lc verify: no pre-push hook at {path}; nothing to remove\n")
        return 0
    if HOOK_MARKER_PREFIX not in existing:
        sink.write(f"lc verify: {path} is not this hook, so it was left alone\n")
        return 2
    path.unlink()
    sink.write(f"lc verify: removed {path}\n")
    return 0


def hook_main(
    arguments: Sequence[str], sink: TextIO, *, env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> int:
    action, flags = (arguments[0], set(arguments[1:])) if arguments else ("", set())
    if action in {"-h", "--help", "help"}:
        sink.write(HOOK_USAGE)
        return 0
    allowed = {"install": {"--print", "--force"}, "uninstall": set()}.get(action)
    if allowed is None or not flags <= allowed or len(flags) != len(arguments) - 1:
        sink.write(HOOK_USAGE)
        return 2
    where = cwd or Path.cwd()
    top_text = _git(where, "rev-parse", "--show-toplevel")
    if not top_text:
        sink.write(f"lc verify: {where} is not inside a Git working tree\n")
        return 2
    top = Path(top_text)
    found = _git(top, "rev-parse", "--git-path", "hooks/pre-push")
    if not found:
        sink.write("lc verify: git could not name the hooks directory\n")
        return 2
    path = Path(found) if Path(found).is_absolute() else top / found
    if action == "uninstall":
        return _uninstall(path, sink)
    return _install(
        top, path, os.environ if env is None else env, force="--force" in flags, show="--print" in flags, sink=sink
    )
