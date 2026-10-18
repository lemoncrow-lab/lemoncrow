"""Product telemetry for one ``lc`` process run.

A run typed by a user is a session (``session_start`` -> ``cli_command_invoked``
-> ``cli_command_completed`` -> ``session_end``). Runs spawned by daemons, hooks
or the statusline are not: they record a single ``cli_command_completed`` with
``background=True`` (the statusline only when it fails), so session counts and
durations describe people rather than render loops.

Command names are resolved against the registered click tree, so free text (a
``-p`` prompt, a path) is never recorded; failures carry the exception class,
never its message.
"""

from __future__ import annotations

import signal
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import click

# Spawned by daemons, hooks or the statusline -- never a user session.
_BACKGROUND_COMMANDS = frozenset({"worker", "servicectl", "background"})
# Maintenance duties (and the host-spawned stdio MCP server, which records its
# own session) are background only when no terminal is attached; typed by a
# user they stay interactive.
_UNATTENDED_COMMANDS = frozenset(
    {("import", ""), ("runtime", "index"), ("code", "prune"), ("session", "recall"), ("mcp", "")}
)
# Root options whose value is never the command.
_ROOT_OPTIONS_WITH_VALUES = frozenset({"--root", "-p"})


def _lemoncrow_version() -> str:
    try:
        return version("lemoncrow")
    except PackageNotFoundError:
        return "0.1.0"


def telemetry_names(group: click.Group, argv: list[str]) -> tuple[str, str]:
    """``(command, subcommand)`` resolved against ``group``'s registered tree."""
    tokens: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in _ROOT_OPTIONS_WITH_VALUES:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        tokens.append(token)
        if len(tokens) == 2:
            break
    if not tokens:
        return "root", ""
    command = group.commands.get(tokens[0])
    if command is None:
        return "unknown", ""
    subcommand = ""
    if len(tokens) > 1 and isinstance(command, click.Group) and tokens[1] in command.commands:
        subcommand = tokens[1].replace("-", "_")
    return tokens[0].replace("-", "_"), subcommand


def has_terminal() -> bool:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.isatty():
                return True
        except (OSError, ValueError):
            continue
    return False


def invocation_kind(argv: list[str], command: str, subcommand: str) -> str:
    """``interactive``, ``background`` or ``statusline`` (a tight render loop)."""
    if command in {"runtime", "rt"} and subcommand == "savings" and "--segment" in argv:
        return "statusline"
    if command in _BACKGROUND_COMMANDS:
        return "background"
    unattended = (command, subcommand) in _UNATTENDED_COMMANDS or (command, "") in _UNATTENDED_COMMANDS
    if unattended and not has_terminal():
        return "background"
    return "interactive"


def error_kind(exc: BaseException, exit_code: int) -> str:
    """Exception class name for a failed run -- never its message."""
    if isinstance(exc, SystemExit):
        # click turns a ClickException into sys.exit() inside its handler, so
        # the class that actually failed the run is the exit's context.
        cause = exc.__context__
        if cause is None or isinstance(cause, (SystemExit, click.exceptions.Exit)):
            return f"exit_{exit_code}"
        return type(cause).__name__
    return type(exc).__name__


def begin_cli_telemetry(
    command_name: str, *, subcommand: str = "", invocation: str = "interactive"
) -> tuple[str, float]:
    from lemoncrow.bench.mode import mode as _bench_mode
    from lemoncrow.core.foundation.identity import get_anon_id, new_session_id, platform_payload
    from lemoncrow.core.service.telemetry import emit_product, set_process_telemetry_session
    from lemoncrow.core.service.telemetry.banner import maybe_show_banner

    maybe_show_banner()
    if invocation != "interactive":
        set_process_telemetry_session("")
        return "", time.perf_counter()
    session_id = new_session_id()
    set_process_telemetry_session(session_id)
    payload = platform_payload()
    emit_product(
        "session_start",
        agent_host="cli",
        lemoncrow_version=_lemoncrow_version(),
        anon_id=get_anon_id(),
        session_id=session_id,
        bench_mode=_bench_mode().value,
        **payload,
    )
    emit_product(
        "cli_command_invoked",
        command_name=command_name,
        subcommand=subcommand,
        session_id=session_id,
        anon_id=get_anon_id(),
    )
    return session_id, time.perf_counter()


def finish_cli_telemetry(
    *,
    command_name: str,
    session_id: str,
    started_at: float,
    ok: bool,
    exit_reason: str,
    subcommand: str = "",
    invocation: str = "interactive",
    error_kind: str = "",
    exit_code: int | None = None,
) -> None:
    from lemoncrow.core.service.telemetry import emit_product
    from lemoncrow.core.service.telemetry.schema import bucket_duration_ms, bucket_duration_s

    if invocation == "statusline" and ok:
        return  # re-rendered every few seconds; only failures carry signal
    elapsed = max(0.0, time.perf_counter() - started_at)
    props: dict[str, Any] = {
        "command_name": command_name,
        "subcommand": subcommand,
        "duration_ms_bucket": bucket_duration_ms(elapsed * 1000),
        "ok": ok,
        "background": invocation != "interactive",
    }
    if session_id:
        props["session_id"] = session_id
    if not ok:
        props["error_kind"] = error_kind or exit_reason
    if exit_code is not None:
        props["exit_code"] = exit_code
    emit_product("cli_command_completed", **props)
    if not session_id:
        return
    emit_product(
        "session_end",
        session_id=session_id,
        duration_s_bucket=bucket_duration_s(elapsed),
        exit_reason=exit_reason,
    )


def emit_cli_interrupted(
    *,
    session_id: str,
    started_at: float,
    signum: int,
    command_name: str,
) -> None:
    from lemoncrow.core.service.telemetry import emit_product
    from lemoncrow.core.service.telemetry.schema import bucket_duration_s

    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    emit_product(
        "session_interrupted",
        session_id=session_id,
        signal=signal_name,
        elapsed_s_bucket=bucket_duration_s(max(0.0, time.perf_counter() - started_at)),
        last_phase=command_name,
    )


class CliRun:
    """Telemetry for one CLI process run, begun on construction."""

    def __init__(self, group: click.Group, argv: list[str]) -> None:
        self.command, self.subcommand = telemetry_names(group, argv)
        self.invocation = invocation_kind(argv, self.command, self.subcommand)
        self.session_id, self.started_at = begin_cli_telemetry(
            self.command, subcommand=self.subcommand, invocation=self.invocation
        )

    def finish(self, *, ok: bool, exit_reason: str, error_kind: str = "", exit_code: int | None = None) -> None:
        finish_cli_telemetry(
            command_name=self.command,
            subcommand=self.subcommand,
            session_id=self.session_id,
            started_at=self.started_at,
            ok=ok,
            exit_reason=exit_reason,
            invocation=self.invocation,
            error_kind=error_kind,
            exit_code=exit_code,
        )

    def finish_from(self, exc: BaseException) -> None:
        """Record how the run ended when the command raised (click always
        ends a standalone run with ``SystemExit``)."""
        if isinstance(exc, SystemExit):
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            self.finish(
                ok=code == 0,
                exit_reason="success" if code == 0 else "error",
                error_kind=error_kind(exc, code) if code != 0 else "",
                exit_code=code,
            )
        elif isinstance(exc, KeyboardInterrupt):
            self.finish(ok=False, exit_reason="interrupted", error_kind="KeyboardInterrupt")
        else:
            self.finish(ok=False, exit_reason="error", error_kind=error_kind(exc, 1))

    def interrupted(self, signum: int) -> None:
        if self.session_id:
            emit_cli_interrupted(
                session_id=self.session_id,
                started_at=self.started_at,
                signum=signum,
                command_name=self.command,
            )


__all__ = [
    "CliRun",
    "begin_cli_telemetry",
    "emit_cli_interrupted",
    "error_kind",
    "finish_cli_telemetry",
    "has_terminal",
    "invocation_kind",
    "telemetry_names",
]
