"""Aggregator entrypoint for relocated LemonCrow CLI command modules."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import click


# Set LEMONCROW_SHOW_ALL=1 to reveal internal/hidden commands in ``lc --help``.
_SHOW_ALL = os.environ.get("LEMONCROW_SHOW_ALL") == "1"


def _h(cmd: Any) -> Any:
    """Mark a click command/group hidden (used for internal commands).

    Hidden commands stay fully runnable and ``lc <cmd> --help`` still
    prints their help; they are only dropped from the top-level ``--help``
    listing. Set ``LEMONCROW_SHOW_ALL=1`` to reveal them all.
    """
    if not _SHOW_ALL and cmd is not None:
        cmd.hidden = True
    return cmd


def register(cli: click.Group) -> None:
    """Register relocated command modules onto the root ``cli`` group."""
    from lemoncrow.gateway.cli.commands._shared import _IMPORT_FAILED

    from .development import dev_command

    cli.add_command(_h(dev_command))

    try:
        from . import admin as admin_commands

        cli.add_command(admin_commands.init)
        cli.add_command(admin_commands.uninstall)
        _h(admin_commands.env_group)  # internal validation
        cli.add_command(admin_commands.env_group)
        cli.add_command(admin_commands.plugin_settings_group)
        doctor_cmd = getattr(admin_commands, "doctor_cmd", None)
        if doctor_cmd is not None:
            cli.add_command(cast("click.Command", doctor_cmd))
        reset_cmd = getattr(admin_commands, "reset_cmd", None)
        if reset_cmd is not None:
            _h(cast("click.Command", reset_cmd))
            cli.add_command(cast("click.Command", reset_cmd))
        _h(admin_commands.audit_group)
        cli.add_command(admin_commands.audit_group)

    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .hosted_auth import auth_group

        cli.add_command(auth_group)
    except ImportError:
        _IMPORT_FAILED = True
    try:
        from .agents_skills import agent_group, install_group, skill_group

        cli.add_command(agent_group)
        cli.add_command(skill_group)
        cli.add_command(install_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from . import admin as _admin_cmds
        from .audit import audit_bash_cmd, audit_context_cmd

        _admin_cmds.audit_group.add_command(audit_context_cmd)
        _admin_cmds.audit_group.add_command(audit_bash_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .telemetry import telemetry_group

        cli.add_command(telemetry_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .service_worker import service_group, worker_group

        cli.add_command(service_group)
        cli.add_command(worker_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .map import map_cmd

        cli.add_command(map_cmd)  # mounted under `lc rt` with the other Runtime commands
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from . import admin as admin_commands
        from .tools import tools_group

        tools_group.add_command(admin_commands.tool_report_cmd, name="report")
        _h(tools_group)
        cli.add_command(tools_group)

        # 'lc runtime mcp' starts the stdio MCP server (replaces the legacy standalone binary)
        try:
            from .mcp import mcp_group

            cli.add_command(mcp_group)
        except (ModuleNotFoundError, ImportError):
            _IMPORT_FAILED = True
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .local_savings import (
            local_savings_auto,
            local_savings_disable,
            local_savings_doctor,
            local_savings_enable,
            local_savings_learn,
            local_savings_status,
            local_savings_undo,
        )
        from .runtime import runtime_group
        from .savings import savings_cmd

        # Keep existing aggregate savings/accounting semantics; Local Savings
        # is an additive product control surface under the same command.
        for command in (
            local_savings_enable,
            local_savings_status,
            local_savings_doctor,
            local_savings_auto,
            local_savings_undo,
            local_savings_learn,
            local_savings_disable,
        ):
            savings_cmd.add_command(command)
        runtime_group.add_command(savings_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .debt import debt_cmd

        cli.add_command(debt_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .review import review_cmd

        # `lc review` is a headline product surface — visible in --help.
        cli.add_command(review_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .runtime import runtime_group

        cli.add_command(runtime_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .workspaces_server import workspaces_group

        cli.add_command(workspaces_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .product_status import product_status_cmd

        cli.add_command(product_status_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .model import model_group

        cli.add_command(model_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .work import work_group

        cli.add_command(work_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .context_doctor import context_group

        cli.add_command(context_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .savings import optimize_group as _optimize_group
        from .usage import usage_group

        # `lc usage` is the canonical accounting surface (plan 2026-09-07 §4.2).
        usage_group.add_command(_optimize_group, name="optimize")
        cli.add_command(usage_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .update import update_cmd

        cli.add_command(update_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .benchmark import benchmark_group

        cli.add_command(benchmark_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .knowledge import knowledge_group

        _h(knowledge_group)
        cli.add_command(knowledge_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .router import router_daemon_group

        _h(router_daemon_group)
        cli.add_command(router_daemon_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .db import db_group

        _h(db_group)
        cli.add_command(db_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .code import code_group, host_group, runtime_index_cmd, zoekt_group

        _h(code_group.commands.get("train"))  # [EXPERIMENTAL] embedder finetune — internal
        cli.add_command(code_group)
        cli.add_command(runtime_index_cmd)
        cli.add_command(host_group)
        _h(zoekt_group)  # search-backend infra used transparently by code_search
        cli.add_command(zoekt_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .route import route_public_group

        _h(route_public_group)
        cli.add_command(route_public_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .lessons import checkpoint, ledger

        cli.add_command(_h(ledger))
        cli.add_command(_h(checkpoint))
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .recall import recall_group
        from .replay import replay_cmd
        from .sessions import runs_group, session_group

        _h(runs_group)
        cli.add_command(runs_group)
        session_group.add_command(recall_group)
        # replay reconstructs a past session and marks LemonCrow short-circuits:
        # `lc session replay`.
        session_group.add_command(replay_cmd)
        cli.add_command(session_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .trace import trace_group

        cli.add_command(trace_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .swarm import swarm_group

        cli.add_command(swarm_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .run import run_group

        # `lc run explain` is user-facing; the unlaunched start/resume/report stay hidden in run.py.
        cli.add_command(run_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .memory import memory_group_cli

        _h(memory_group_cli)
        cli.add_command(memory_group_cli)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        import click as _click

        from . import admin as admin_commands

        @_click.group("dashboard", invoke_without_command=True)
        @_click.pass_context
        def dashboard_group(ctx: _click.Context) -> None:
            """Spend and savings over the last 7 days, and the latest runs.

            lc rt dashboard open opens the web dashboard when LemonCrow
            Review's local server is running.
            """
            if ctx.invoked_subcommand is None:
                from lemoncrow.gateway.cli import style
                from lemoncrow.gateway.cli.sessions_ui import overview_lines
                from lemoncrow.infra.runtime.dashboard_url import discover_dashboard_url

                root = ctx.obj["root"]
                with style.working("Adding up sessions…"):
                    lines = overview_lines(
                        root, width=style.terminal_width(80), browser=discover_dashboard_url(root) is not None
                    )
                _click.echo("\n".join(lines))

        @dashboard_group.command("open")
        @_click.option(
            "--port", default=None, type=int, help="Exact frontend port; otherwise discover the running dashboard"
        )
        @_click.pass_context
        def dashboard_open_cmd(ctx: _click.Context, port: int | None) -> None:
            """Open the web dashboard served by LemonCrow Review's local server."""
            from pathlib import Path

            from lemoncrow_client.browser import launch_browser_url

            from lemoncrow.gateway.cli import style
            from lemoncrow.gateway.cli.commands.product_status import _review_installed
            from lemoncrow.infra.runtime.dashboard_url import discover_dashboard_url

            frontend_url = discover_dashboard_url(ctx.obj["root"], requested_port=port)
            if frontend_url is None:
                if port is not None:
                    message, text, command = f"Nothing answers on port {port}.", "Find the server with", "lc doctor"
                elif _review_installed(Path(ctx.obj["root"])):
                    message, text, command = "LemonCrow's local server isn't running.", "See why with", "lc doctor"
                else:
                    message = "The web dashboard comes with LemonCrow Review; Workspaces Lite has none."
                    text, command = "The same numbers are in the terminal:", "lc rt dashboard"
                _click.echo(f"{style.mark('fail')} {message}", err=True)
                _click.echo(style.next_step(text, command), err=True)
                if command == "lc rt dashboard":
                    _click.echo(style.next_step("Sessions one by one:", "lc session"), err=True)
                    _click.echo(style.next_step("The code map:", "lc rt map"), err=True)
                ctx.exit(1)
            from lemoncrow.gateway.cli.commands.review import arm_local_browser_url

            url = f"{frontend_url.rstrip('/')}/"
            _click.echo(f"{style.accent('◆')} Opening the LemonCrow dashboard: {url}")
            arm_local_browser_url(url)
            if not launch_browser_url(url):
                _click.echo(f"(could not open a browser; open {url} manually.)", err=True)

        cli.add_command(dashboard_group)
        # The old dashboard-style `status` alias was retired when `lc status`
        # became the product orientation surface. Analytics live at `lc dashboard`.
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        import click as _click

        @_click.command("completions")
        @_click.argument("shell", type=_click.Choice(["zsh", "bash", "fish"]))
        def completions_cmd(shell: str) -> None:
            """Print shell completion script.

            \b
            # zsh:  echo 'eval "$(lc completions zsh)"'  >> ~/.zshrc
            # bash: echo 'eval "$(lc completions bash)"' >> ~/.bashrc
            # fish: lc completions fish > ~/.config/fish/completions/lemoncrow.fish
            """
            # Derived from the live CLI group so the list can never go stale.
            entries = _completion_entries(cli)
            _click.echo(_render_completion_script(shell, entries))

        cli.add_command(completions_cmd)
    except ImportError:
        _IMPORT_FAILED = True

    # `lc with <host>` is the low-friction trial path. It adds LemonCrow MCP
    # only to that child process; it never persists host registration.
    try:
        from .host_trial import with_group

        cli.add_command(with_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    # Runtime is a product namespace, not a second implementation tree. Mount
    # each visible Runtime-owned command object under `lc runtime` after the
    # root registry is complete so canonical and compatibility spellings cannot
    # drift in options, behavior or help.
    try:
        from .runtime import runtime_group

        _mount_runtime_product_commands(cli, runtime_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True


def _mount_runtime_product_commands(cli: click.Group, runtime_group: click.Group) -> None:
    import copy

    from lemoncrow.gateway.cli.product_ownership import (
        COMMAND_OWNERSHIP,
        PRODUCT_ALIASES,
        RUNTIME_ENGINE_SHORTCUTS,
        RUNTIME_PROMOTED_COMMANDS,
        ProductOwner,
    )

    # Runtime ownership does not imply an extra word for frequent user jobs.
    # Promoted commands remain first-class root commands and the exact same
    # Click command objects are also mounted beneath `lc runtime`. Less-frequent
    # Runtime machinery is moved into the namespace and removed from root.
    for name, owner in COMMAND_OWNERSHIP.items():
        if owner is not ProductOwner.RUNTIME or name in {"runtime", *PRODUCT_ALIASES}:
            continue
        if name in RUNTIME_ENGINE_SHORTCUTS:
            continue
        if name in RUNTIME_PROMOTED_COMMANDS:
            command = cli.commands.get(name)
            if command is not None and name not in runtime_group.commands:
                runtime_group.add_command(command, name=name)
            continue
        command = cli.commands.pop(name, None)
        if command is None or name in runtime_group.commands:
            continue
        runtime_group.add_command(command, name=name)

    # `lc rt ...` is a deliberate shorthand for the Runtime product namespace.
    # Keep it out of root help/completion so `runtime` remains the one canonical
    # product name and we do not recreate a second command hierarchy.
    rt_alias = copy.copy(runtime_group)
    rt_alias.hidden = True
    cli.add_command(rt_alias, name="rt")


def _completion_entries(cli: click.Group) -> list[tuple[str, str]]:
    """(name, one-line description) for every visible top-level command."""
    import click as _click

    ctx = _click.Context(cli, info_name="lc")
    entries: list[tuple[str, str]] = []
    for name in cli.list_commands(ctx):
        cmd = cli.get_command(ctx, name)
        if cmd is None or cmd.hidden:
            continue
        desc = cmd.get_short_help_str(limit=60).replace("'", "").replace(":", " ")
        entries.append((name, desc))
    return entries


def _render_completion_script(shell: str, entries: list[tuple[str, str]]) -> str:
    names = " ".join(name for name, _ in entries)
    if shell == "zsh":
        lines = "\n".join(f"        '{name}:{desc}'" for name, desc in entries)
        return (
            "\n#compdef lc lemoncrow\n_lemoncrow() {\n    local -a commands\n    commands=(\n"
            f"{lines}\n"
            "    )\n    _describe 'lc commands' commands\n}\ncompdef _lemoncrow lc lemoncrow\n"
        )
    if shell == "bash":
        return (
            "\n_lemoncrow_completions() {\n"
            '    local cur="${COMP_WORDS[COMP_CWORD]}"\n'
            f'    local commands="{names} --help --version"\n'
            '    COMPREPLY=($(compgen -W "${commands}" -- "${cur}"))\n'
            "}\ncomplete -F _lemoncrow_completions lc lemoncrow\n"
        )
    lines = "\n".join(
        f"complete -c {prog} -n '__fish_use_subcommand' -a {name} -d '{desc}'"
        for prog in ("lc", "lemoncrow")
        for name, desc in entries
    )
    headers = "\n".join(f"complete -c {prog} -f" for prog in ("lc", "lemoncrow"))
    return f"\n{headers}\n{lines}\n"
