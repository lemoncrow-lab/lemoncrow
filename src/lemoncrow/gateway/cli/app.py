"""Product-neutral LemonCrow command shell."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import click

from lemoncrow import __version__ as lemoncrow_version
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.gateway.cli import style
from lemoncrow.gateway.cli.commands import register as _register_command_modules
from lemoncrow.gateway.cli.commands._dev import (
    MCP_TOOL_ONLY_COMMANDS,
    MCP_TOOL_ONLY_GROUPS,
    _DummyGroup,
)
from lemoncrow.gateway.cli.commands._dev import (
    dev_command as _module_dev_command,
)
from lemoncrow.gateway.cli.commands._dev import (
    dev_group as _module_dev_group,
)
from lemoncrow.gateway.cli.commands.admin import _project_root
from lemoncrow.gateway.cli.run_telemetry import CliRun
from lemoncrow.gateway.cli.run_telemetry import begin_cli_telemetry as _begin_cli_telemetry
from lemoncrow.gateway.cli.run_telemetry import emit_cli_interrupted as _emit_cli_interrupted
from lemoncrow.gateway.cli.run_telemetry import finish_cli_telemetry as _finish_cli_telemetry

DEFAULT_ROOT = default_store_root()


def _lemoncrow_version() -> str:
    try:
        return version("lemoncrow")
    except PackageNotFoundError:
        return "0.1.0"


def _cli_command_name(argv: list[str]) -> str:
    skip_next = False
    # Root options that take a value: their value is never the command (a
    # `-p "prompt"` used to be recorded as the command name).
    options_with_values = {"--root", "-p"}
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


def _dev_command(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return _module_dev_command(name, **kwargs)


def _dev_group(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
    return _module_dev_group(name, **kwargs)


_AUTO_INSTALL_SENTINEL = "_LEMONCROW_UV_SYNC_RETRY"


class ProductShellGroup(click.Group):
    """Root help grouped by LemonCrow product ownership."""

    context_class = style.Context

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        with style.styled_errors():
            return super().make_context(info_name, args, parent, **extra)

    def invoke(self, ctx: click.Context) -> Any:
        with style.styled_errors():
            return super().invoke(ctx)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        from lemoncrow.gateway.cli.product_ownership import RUNTIME_NAMESPACED_ONLY_COMMANDS

        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as error:
            # Runtime commands live under `lc rt`; an old habit or a stale hint
            # (`lc init`, `lc debt`) gets the exact spelling instead of a dead end.
            name = args[0] if args else ""
            if name not in RUNTIME_NAMESPACED_ONLY_COMMANDS:
                raise
            raise style.Redirect(
                f"No such command '{name}'.",
                "It is a Workspaces command:",
                " ".join(["lc", "rt", *args]),
                ctx=error.ctx,
            ) from None

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        from lemoncrow.gateway.cli.product_ownership import VISIBLE_COMMAND_OWNERSHIP, ProductOwner

        groups: dict[str, list[tuple[str, str]]] = {
            "Work": [],
            "Try with your agent": [],
            "Products": [],
            "Setup & system": [],
            "Other": [],
        }
        work_commands = {"review", "code", "run", "session", "context", "usage"}
        engine_shortcuts = {"with"}
        visible = [
            (name, command)
            for name in self.list_commands(ctx)
            if (command := self.get_command(ctx, name)) is not None and not command.hidden
        ]
        for name, help_text in style.short_help_rows(formatter, visible):
            owner = VISIBLE_COMMAND_OWNERSHIP.get(name)
            if name in work_commands:
                group = "Work"
            elif name in engine_shortcuts:
                group = "Try with your agent"
            elif name == "runtime":
                group = "Products"
            elif owner is ProductOwner.CORE:
                group = "Setup & system"
            else:
                group = "Other"
            groups[group].append((name, help_text))

        for title, rows in groups.items():
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)


_ORIENTATION: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Work",
        (
            ("lc review", "Review the current change (shortcut: lcr)"),
            ("lc code", "Reserved for the Hosted coding agent; not available yet"),
            ("lc rt map", "Browse this repository's code: folders, symbols, imports"),
            ("lc session", "Cost and savings for each agent session"),
            ("lc rt dashboard", "Spend and savings over the last 7 days"),
            ("lc run", "Explain past runs from their recorded traces"),
            ("lc with claude", "Try LemonCrow inside claude, codex or opencode"),
        ),
    ),
    (
        "Set up",
        (
            ("lc status", "What is installed and enabled"),
            ("lc rt", "Workspaces: LemonCrow's tools inside your coding agents"),
            ("lc doctor", "Check this installation"),
        ),
    ),
)


def _render_root_orientation() -> None:
    click.echo(style.title(f"LemonCrow {lemoncrow_version}"))
    click.echo(style.dim("  Review code changes and power your coding agents."))
    from lemoncrow.gateway.cli.home import repository_lines

    for line in repository_lines():
        click.echo(line)
    for section, entries in _ORIENTATION:
        click.echo("")
        click.echo(f"  {style.heading(section)}")
        for name, summary in entries:
            click.echo(f"    {style.command(name.ljust(16))} {summary}")
    click.echo("")
    click.echo(
        f"  {style.accent('→')} {style.command('lc --help')} {style.dim('lists every command ·')} "
        f"{style.command('lc help <command>')} {style.dim('explains one')}"
    )


@click.group(
    cls=ProductShellGroup,
    # Help fills the terminal; click otherwise stops at 80 columns.
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": sys.maxsize},
    invoke_without_command=True,
)
@click.version_option(version=lemoncrow_version, prog_name="lemoncrow")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=DEFAULT_ROOT,
    show_default=True,
    help="LemonCrow state directory.",
)
@click.pass_context
def cli(ctx: click.Context, root: Path) -> None:
    """LemonCrow — review code changes and power coding agents."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root
    if ctx.get_parameter_source("root") not in (None, click.ParameterSource.DEFAULT):
        from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

        set_active_store_root(root)
        ctx.call_on_close(lambda: set_active_store_root(None))
    if ctx.invoked_subcommand is None:
        _render_root_orientation()


@cli.command("help", context_settings={"ignore_unknown_options": True})
@click.argument("command_path", nargs=-1)
@click.pass_context
def help_cmd(ctx: click.Context, command_path: tuple[str, ...]) -> None:
    """Show help for LemonCrow or a specific command path."""
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
        command_ctx = command.context_class(command, info_name=token, parent=command_ctx)

    click.echo(command.get_help(command_ctx))


_register_command_modules(cli)
style.adopt(cli)


def _argv_for_program(prog_name: str, argv: list[str]) -> list[str]:
    """Translate permanent symlink entrypoints onto the canonical command tree."""
    if prog_name == "lemoncrow-mcp":
        return ["runtime", "mcp", *argv]
    if prog_name == "lcr":
        return ["review", *argv]
    return argv


def main() -> None:
    # The full distribution also installs `lcr`. Its historical Click review
    # command cannot parse Review-owned watch/connect/pending/device/task verbs.
    # Route those subcommands *before* Click consumes their options; otherwise
    # `lcr pending` or `lcr task run` becomes a Review capture argument.
    # `lc review` and ordinary full CLI commands keep their existing behavior.
    if Path(sys.argv[0]).name == "lcr" and sys.argv[1:2] in (
        ["watch"],
        ["connect"],
        ["pending"],
        ["device"],
        ["task"],
        ["companion"],
    ):
        from lemoncrow_client.review import review_main

        raise SystemExit(review_main(sys.argv[1:], sink=sys.stdout))

    # The terminal verbs belong to the Review client; the Click command would read each one as a revision.
    program = Path(sys.argv[0]).name
    review_arguments = (
        sys.argv[1:]
        if program == "lcr"
        else sys.argv[2:] if program in {"lc", "lemoncrow"} and sys.argv[1:2] == ["review"] else []
    )
    if review_arguments:
        from lemoncrow_client.review_verbs import VERBS

        if review_arguments[0] in VERBS:
            from lemoncrow_client.review import review_main

            raise SystemExit(review_main(review_arguments, sink=sys.stdout))

    argv = _argv_for_program(program, sys.argv[1:])

    if argv[:1] == ["dev"]:
        # Development/MCP bootstrap must not record a run in installed state.
        cli(args=argv, obj={})
        return

    if not os.environ.get(_AUTO_INSTALL_SENTINEL) and not getattr(sys, "frozen", False):
        from lemoncrow.gateway.cli.commands._shared import _IMPORT_FAILED

        if _IMPORT_FAILED:
            os.environ[_AUTO_INSTALL_SENTINEL] = "1"
            project_root = Path.cwd().resolve()
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
                        [sys.executable, "-m", "lemoncrow.gateway.cli", *sys.argv[1:]],
                    )
                else:
                    click.echo(
                        f"`uv sync` failed (exit code {result.returncode}); cannot auto-install dependencies.",
                        err=True,
                    )

    command_name = _cli_command_name(argv)
    run = CliRun(cli, argv)
    old_handlers: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        run.interrupted(signum)
        previous = old_handlers.get(signum)
        if callable(previous):
            previous(signum, frame)
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handler)

    try:
        try:
            cli(args=argv, obj={"_telemetry_session_id": run.session_id, "_telemetry_command_name": command_name})
        except BaseException as exc:
            run.finish_from(exc)
            raise
        else:
            run.finish(ok=True, exit_reason="success", exit_code=0)
    finally:
        from lemoncrow.core.service.telemetry import shutdown_otel
        from lemoncrow.gateway.cli.update_footer import print_update_footer

        print_update_footer(command_name, argv)
        shutdown_otel()


__all__ = [
    "MCP_TOOL_ONLY_COMMANDS",
    "MCP_TOOL_ONLY_GROUPS",
    "_DummyGroup",
    "_begin_cli_telemetry",
    "_cli_command_name",
    "_dev_command",
    "_dev_group",
    "_emit_cli_interrupted",
    "_finish_cli_telemetry",
    "_lemoncrow_version",
    "_project_root",
    "_telemetry_session",
    "cli",
    "help_cmd",
    "main",
]
