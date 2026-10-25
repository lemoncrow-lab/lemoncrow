"""Hand the model-backed review commands to the Review engine the installer put beside this client.

``lc review --ai``, ``lc review hook install|run``, ``lc review init-action`` and ``lc review config`` need a model
route, the repository's rules and git hooks. They live in the Review engine (``lemoncrow.pro.capabilities.review.
local_cli``), which this stdlib-only client never imports: it starts the engine's own interpreter on them, so an
installed ``lc`` is one command with one help. No engine, no delegation: a hosted or client-only install says so.

Nothing is installed or fetched here, and nothing runs unless the person typed one of those commands.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, TextIO

__all__ = ["ENGINE_COMMANDS", "engine_python", "model_route_hint", "needs_engine", "run_engine", "run_settings"]

#: The module the engine's interpreter runs: ``<arguments typed after lc review>`` go straight to it.
_ENGINE_MODULE: Final[str] = "lemoncrow.pro.capabilities.review.local_cli"
#: ``lc settings ...``: the same interpreter, the settings half of the engine (an API key, a remembered agent).
_SETTINGS_MODULE: Final[str] = "lemoncrow.pro.capabilities.review.local_cli.settings_cli"
#: Named by the hook script and by ``hook install``: the ``lc`` that was typed, so a hook calls the right one.
_LC_ENV: Final[str] = "LEMONCROW_LC_BIN"
#: Subcommands that always run in the engine. ``hook`` does too, except the agent-checkpoint events below.
ENGINE_COMMANDS: Final[frozenset[str]] = frozenset({"init-action", "config"})
_CHECKPOINT_EVENTS: Final[frozenset[str]] = frozenset({"after-edit", "after-checkpoint", "on-exit"})
_API_KEYS: Final[tuple[str, ...]] = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def _state_dir(environment: Mapping[str, str]) -> Path:
    home = environment.get("LEMONCROW_HOME", "").strip()
    return Path(home).expanduser() if home else Path(environment.get("HOME", str(Path.home()))) / ".lemoncrow"


def engine_python(environment: Mapping[str, str] | None = None) -> Path | None:
    """The engine's interpreter, or ``None`` when this install has no local engine."""

    environment = os.environ if environment is None else environment
    configured = environment.get("LEMONCROW_REVIEW_ENGINE_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else _state_dir(environment) / "review" / "engine"
    candidate = root / "bin" / "python"
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def needs_engine(arguments: Sequence[str]) -> bool:
    """Whether ``lc review ARGUMENTS`` is one of the commands the engine runs.

    ``--ai`` is the only switch that makes a review call a model: ``--agent`` or ``--pr`` alone never do, so a
    person is never billed by a flag that merely sounds like it.
    """

    if not arguments:
        return False
    if arguments[0] in ENGINE_COMMANDS:
        return True
    if arguments[0] == "hook":
        return len(arguments) < 2 or arguments[1] not in _CHECKPOINT_EVENTS
    return "--ai" in arguments


def _own_lc(environment: Mapping[str, str]) -> str:
    """The ``lc`` this process was started as, else the installed one, so a git hook calls the same program."""

    invoked = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if invoked is not None and invoked.name == "lc" and invoked.is_file():
        return os.path.abspath(invoked)
    configured = environment.get("LEMONCROW_BIN_DIR", "").strip()
    installed = (Path(configured).expanduser() if configured else _state_dir(environment) / "bin") / "lc"
    return str(installed) if installed.is_file() and os.access(installed, os.X_OK) else ""


def run_settings(arguments: Sequence[str], sink: TextIO) -> int:
    """Run ``lc settings ARGUMENTS`` in the engine's interpreter; its exit status is ours."""

    return run_engine(arguments, sink, module=_SETTINGS_MODULE, command="lc settings")


def run_engine(arguments: Sequence[str], sink: TextIO, *, module: str = _ENGINE_MODULE, command: str = "") -> int:
    """Run ``lc review ARGUMENTS`` (or, with *module*, another engine command) in the engine's interpreter; its exit
    status is ours."""

    python = engine_python()
    if python is None:
        from .cli import _install_hint

        needs = command or f"lc review {' '.join(arguments[:2])}"
        sink.write(f"{needs} needs the local Review engine, which this install does not have.\n")
        sink.write(f"Install it with: {_install_hint('review')}\n")
        return 2
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    environment["PYTHONNOUSERSITE"] = "1"
    environment.setdefault("LEMONCROW_ROOT", str(_state_dir(os.environ)))
    lc = _own_lc(os.environ)
    if lc:
        environment[_LC_ENV] = lc
    argv = [str(python), "-m", module, *arguments]
    try:
        if sink is sys.stdout:
            return subprocess.run(argv, env=environment, check=False).returncode
        completed = subprocess.run(argv, env=environment, capture_output=True, text=True, check=False)
    except OSError as exc:
        sink.write(f"could not start the Review engine ({exc}). Re-run the installer to repair it.\n")
        return 2
    sink.write(completed.stdout)
    if completed.stderr:
        sink.write(completed.stderr)
    return completed.returncode


def model_route_hint(environment: Mapping[str, str] | None = None) -> str:
    """One line for after a plain ``lc review``: how to get the AI review, or how to set it up. Spends nothing."""

    environment = os.environ if environment is None else environment
    if engine_python(environment) is None:
        return ""
    if any(environment.get(name, "").strip() for name in _API_KEYS):
        return "AI review of this change in the terminal: lc review --ai"
    return "AI review in the terminal: export ANTHROPIC_API_KEY=..., then lc review --ai"
