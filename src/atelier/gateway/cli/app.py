"""CLI for the Atelier reasoning runtime.

Designed to be readable when piped into another tool. All commands that
return data accept ``--json`` to emit machine-parseable output.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import click

from atelier import __version__ as atelier_version
from atelier.core.foundation.paths import default_store_root
from atelier.gateway.cli.commands import register as _register_command_modules
from atelier.gateway.cli.commands._dev import (
    MCP_TOOL_ONLY_COMMANDS,
    MCP_TOOL_ONLY_GROUPS,
    _DummyGroup,
)
from atelier.gateway.cli.commands._dev import (
    dev_command as _module_dev_command,
)
from atelier.gateway.cli.commands._dev import (
    dev_group as _module_dev_group,
)
from atelier.gateway.cli.commands.admin import _project_root
from atelier.gateway.cli.commands.hosts import (
    _IMPORT_PROGRESS_HANDLER_FLAG,
    _IMPORT_PROGRESS_LOGGER,
    _ensure_import_progress_logging,
)

DEFAULT_ROOT = default_store_root()


def _atelier_version() -> str:
    try:
        return version("atelier")
    except PackageNotFoundError:
        return "0.1.0"


def _cli_command_name(argv: list[str]) -> str:
    skip_next = False
    options_with_values = {"--root"}
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in options_with_values:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token.replace("-", "_")
    return "root"


def _telemetry_session(ctx: click.Context) -> str | None:
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    value = obj.get("_telemetry_session_id")
    return value if isinstance(value, str) else None


def _begin_cli_telemetry(command_name: str) -> tuple[str, float]:
    from atelier.bench.mode import mode as _bench_mode
    from atelier.core.foundation.identity import get_anon_id, new_session_id, platform_payload
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.banner import maybe_show_banner

    maybe_show_banner()
    session_id = new_session_id()
    payload = platform_payload()
    emit_product(
        "session_start",
        agent_host="cli",
        atelier_version=_atelier_version(),
        anon_id=get_anon_id(),
        session_id=session_id,
        bench_mode=_bench_mode().value,
        **payload,
    )
    emit_product(
        "cli_command_invoked",
        command_name=command_name,
        session_id=session_id,
        anon_id=get_anon_id(),
    )
    return session_id, time.perf_counter()


def _finish_cli_telemetry(
    *,
    command_name: str,
    session_id: str,
    started_at: float,
    ok: bool,
    exit_reason: str,
) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import bucket_duration_ms, bucket_duration_s

    elapsed = max(0.0, time.perf_counter() - started_at)
    emit_product(
        "cli_command_completed",
        command_name=command_name,
        session_id=session_id,
        duration_ms_bucket=bucket_duration_ms(elapsed * 1000),
        ok=ok,
    )
    emit_product(
        "session_end",
        session_id=session_id,
        duration_s_bucket=bucket_duration_s(elapsed),
        exit_reason=exit_reason,
    )


def _emit_cli_interrupted(
    *,
    session_id: str,
    started_at: float,
    signum: int,
    command_name: str,
) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import bucket_duration_s

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


def _dev_command(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return _module_dev_command(name, **kwargs)


def _dev_group(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
    return _module_dev_group(name, **kwargs)


# ── Auto-install fallback ─────────────────────────────────────────────────
# When a command module import fails during registration (ImportError), the
# CLI checks _IMPORT_FAILED at startup and runs ``uv sync`` to install deps
# before dispatching the user's command.

_AUTO_INSTALL_SENTINEL = "_ATELIER_UV_SYNC_RETRY"


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(version=atelier_version, prog_name="atelier")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=DEFAULT_ROOT,
    show_default=True,
    help="Atelier runtime data directory.",
)
@click.option(
    "-p",
    "--print",
    "print_prompts",
    metavar="PROMPT",
    multiple=True,
    help="Run one or more prompts, print only final answers, and exit. Reads piped stdin as context.",
)
@click.pass_context
def cli(ctx: click.Context, root: Path, print_prompts: tuple[str, ...]) -> None:
    """Atelier - Agent Reasoning Runtime."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root
    if print_prompts:
        if ctx.invoked_subcommand is not None:
            raise click.ClickException("--print cannot be combined with a subcommand")
        from atelier.gateway.cli.commands.run import _run_print_session

        stdin_text = ""
        try:
            if not sys.stdin.isatty():
                stdin_text = sys.stdin.read()
        except OSError:
            stdin_text = ""
        prompts = [prompt.strip() for prompt in print_prompts if prompt.strip()]
        if stdin_text.strip():
            stdin_context = f"<stdin>\n{stdin_text.rstrip()}\n</stdin>"
            prompts = [f"{prompt}\n\n{stdin_context}" if prompt else stdin_text.rstrip() for prompt in prompts]
        if not prompts:
            raise click.ClickException("--print requires a prompt or piped stdin")
        if len(prompts) == 1:
            prompt = prompts[0]
        else:
            prompt_blocks = "\n\n".join(
                f'<prompt index="{index}">\n{prompt}\n</prompt>' for index, prompt in enumerate(prompts, start=1)
            )
            prompt = (
                "Answer each prompt independently. Keep the original order. "
                "Separate final answers with a line containing exactly three hyphens (`---`).\n\n"
                f"<prompts>\n{prompt_blocks}\n</prompts>"
            )
        final_text = _run_print_session(prompt, root=root)
        if final_text:
            click.echo(final_text)
        return
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("help", context_settings={"ignore_unknown_options": True})
@click.argument("command_path", nargs=-1)
@click.pass_context
def help_cmd(ctx: click.Context, command_path: tuple[str, ...]) -> None:
    """Show help for Atelier or a specific command path."""
    root_ctx = ctx.parent
    if root_ctx is None:
        click.echo(cli.get_help(ctx))
        return

    if not command_path:
        click.echo(root_ctx.get_help())
        return

    command: click.Command = cli
    command_ctx = root_ctx
    for token in command_path:
        if not isinstance(command, click.Group):
            raise click.ClickException(f"{command_ctx.command_path} has no subcommands")
        next_command = command.get_command(command_ctx, token)
        if next_command is None:
            raise click.ClickException(f"unknown command: {' '.join(command_path)}")
        command = next_command
        command_ctx = click.Context(command, info_name=token, parent=command_ctx)

    click.echo(command.get_help(command_ctx))


_register_command_modules(cli)


def main() -> None:
    # Handle calling conventions (symlink for backward compat)
    argv = sys.argv[1:]
    prog_name = Path(sys.argv[0]).name
    if prog_name == "atelierd":
        argv = ["background", "service", *argv]
    elif prog_name == "atelier-mcp":
        argv = ["mcp", *argv]

    # ── Auto-install: if a command module failed to import, run uv sync ────────
    if not os.environ.get(_AUTO_INSTALL_SENTINEL) and not getattr(sys, "frozen", False):
        from atelier.gateway.cli.commands._shared import _IMPORT_FAILED

        if _IMPORT_FAILED:
            os.environ[_AUTO_INSTALL_SENTINEL] = "1"
            project_root = Path.cwd().resolve()
            # Walk up for pyproject.toml
            for parent in [project_root, *list(project_root.parents)]:
                if (parent / "pyproject.toml").is_file():
                    project_root = parent
                    break
            else:
                project_root = None
            if project_root and shutil.which("uv"):
                click.echo("Missing dependencies detected. Running `uv sync` to install them…", err=True)
                result = subprocess.run(["uv", "sync"], cwd=str(project_root), check=False)
                if result.returncode == 0:
                    os.execv(
                        sys.executable,
                        [sys.executable, "-m", "atelier.gateway.cli", *sys.argv[1:]],
                    )
                else:
                    click.echo(
                        f"`uv sync` failed (exit code {result.returncode}); cannot auto-install dependencies.",
                        err=True,
                    )

    command_name = _cli_command_name(argv)
    session_id, started_at = _begin_cli_telemetry(command_name)
    old_handlers: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        _emit_cli_interrupted(
            session_id=session_id,
            started_at=started_at,
            signum=signum,
            command_name=command_name,
        )
        previous = old_handlers.get(signum)
        if callable(previous):
            previous(signum, frame)
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handler)

    try:
        try:
            cli(args=argv, obj={"_telemetry_session_id": session_id, "_telemetry_command_name": command_name})
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=code == 0,
                exit_reason="success" if code == 0 else "error",
            )
            raise
        except KeyboardInterrupt:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=False,
                exit_reason="interrupted",
            )
            raise
        except BaseException:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=False,
                exit_reason="error",
            )
            raise
        else:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=True,
                exit_reason="success",
            )
    finally:
        from atelier.core.service.telemetry import shutdown_otel

        shutdown_otel()


__all__ = [
    "MCP_TOOL_ONLY_COMMANDS",
    "MCP_TOOL_ONLY_GROUPS",
    "_IMPORT_PROGRESS_HANDLER_FLAG",
    "_IMPORT_PROGRESS_LOGGER",
    "_DummyGroup",
    "_atelier_version",
    "_begin_cli_telemetry",
    "_cli_command_name",
    "_dev_command",
    "_dev_group",
    "_emit_cli_interrupted",
    "_ensure_import_progress_logging",
    "_finish_cli_telemetry",
    "_project_root",
    "_telemetry_session",
    "cli",
    "help_cmd",
    "main",
]
