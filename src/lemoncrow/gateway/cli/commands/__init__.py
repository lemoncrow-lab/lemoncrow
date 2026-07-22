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

    try:
        from . import admin as admin_commands

        cli.add_command(admin_commands.init)
        cli.add_command(admin_commands.uninstall)
        _h(admin_commands.env_group)  # internal validation
        cli.add_command(admin_commands.env_group)
        cli.add_command(admin_commands.account_group)
        # status_cmd is registered as 'dashboard' later (with 'status' as hidden alias)
        _h(admin_commands.share_cmd)
        cli.add_command(admin_commands.share_cmd)
        cli.add_command(admin_commands.plugin_settings_group)
        doctor_cmd = getattr(admin_commands, "doctor_cmd", None)
        if doctor_cmd is not None:
            cli.add_command(cast("click.Command", doctor_cmd))
        reset_cmd = getattr(admin_commands, "reset_cmd", None)
        if reset_cmd is not None:
            _h(cast("click.Command", reset_cmd))
            cli.add_command(cast("click.Command", reset_cmd))
        _h(admin_commands.team_group)
        cli.add_command(admin_commands.team_group)
        _h(admin_commands.governance_group)
        cli.add_command(admin_commands.governance_group)
        _h(admin_commands.audit_group)
        cli.add_command(admin_commands.audit_group)
        _h(admin_commands.insights_cmd)
        cli.add_command(admin_commands.insights_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .agents_skills import agent_group, install_group, skill_group, stale_nudge_cmd

        cli.add_command(agent_group)
        cli.add_command(skill_group)
        cli.add_command(install_group)
        _h(stale_nudge_cmd)  # internal plumbing for statusline/opencode nudge, not interactive
        cli.add_command(stale_nudge_cmd)
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
        from .playbooks import (
            add_playbook,
            domain_group,
            import_style_guide_cmd,
            list_blocks_cmd,
            playbook_group,
            reembed,
            report_cmd,
        )

        _h(domain_group)
        cli.add_command(domain_group)
        _h(report_cmd)
        cli.add_command(report_cmd)
        _h(playbook_group)
        cli.add_command(playbook_group)
        _h(list_blocks_cmd)
        cli.add_command(list_blocks_cmd)
        _h(add_playbook)
        cli.add_command(cast("click.Command", add_playbook))
        _h(import_style_guide_cmd)
        cli.add_command(import_style_guide_cmd)
        _h(reembed)
        cli.add_command(cast("click.Command", reembed))
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .telemetry import telemetry_group

        cli.add_command(telemetry_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .servicectl import service_group, servicectl_group, worker_group

        cli.add_command(service_group)
        cli.add_command(worker_group)
        cli.add_command(servicectl_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .stack import stack_group

        cli.add_command(stack_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .map import map_cmd

        _h(map_cmd)  # compatibility shortcut; Map is a tab in `lc dashboard open`
        cli.add_command(map_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    # ── hidden internal commands (used by dev.sh, not user-facing) ───────────
    try:
        from .background import background_group

        cli.add_command(background_group, name="background")
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .background import systemd_alias_group

        cli.add_command(systemd_alias_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from . import admin as admin_commands
        from .tools import tool_mode, tools_group

        tools_group.add_command(tool_mode, name="mode")
        tools_group.add_command(admin_commands.tool_report_cmd, name="report")
        _h(tools_group)
        cli.add_command(tools_group)

        # 'lc mcp' starts the stdio MCP server (replaces the legacy standalone binary)
        try:
            from .mcp import mcp_group

            cli.add_command(mcp_group)
        except (ModuleNotFoundError, ImportError):
            _IMPORT_FAILED = True
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .savings import (
            optimize_group,
            savings_cmd,
        )

        # savings is the headline metric — kept visible in --help.
        cli.add_command(savings_cmd)
        _h(optimize_group)  # advanced tuning advisor — internal
        cli.add_command(optimize_group)
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
        from .chatgpt import chatgpt_group

        cli.add_command(chatgpt_group)
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
        from .defaults import defaults_group

        _h(defaults_group)
        cli.add_command(defaults_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .code import code_group, zoekt_group

        _h(code_group.commands.get("train"))  # [EXPERIMENTAL] embedder finetune — internal
        cli.add_command(code_group)
        _h(zoekt_group)  # search-backend infra used transparently by code_search
        cli.add_command(zoekt_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .perf import perf_group

        _h(perf_group)
        cli.add_command(perf_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .route import proof_group, route_public_group

        _h(route_public_group)
        cli.add_command(route_public_group)
        _h(proof_group)
        cli.add_command(proof_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .hosts import global_import

        _h(global_import)
        cli.add_command(global_import)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .eval import eval_
        from .lessons import (
            checkpoint,
            ledger,
            lesson,
        )

        _h(ledger)
        cli.add_command(ledger)
        _h(checkpoint)
        cli.add_command(checkpoint)
        _h(lesson)
        cli.add_command(lesson)
        _h(eval_)
        cli.add_command(eval_)
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
        from .swarm import swarm_group

        cli.add_command(swarm_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        from .run import run_group

        _h(run_group)  # owned coding sessions — undocumented; hidden until launched
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
        from .letta import letta_group

        _h(letta_group)
        cli.add_command(letta_group)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        import click as _click

        from . import admin as admin_commands

        @_click.group("dashboard", invoke_without_command=True)
        @_click.pass_context
        def dashboard_group(ctx: _click.Context) -> None:
            """Show the LemonCrow spend & savings dashboard.

            Run with no arguments for the terminal rollup (last 7 days + recent
            runs). Use ``lc dashboard open`` for the browser analytics UI.
            """
            if ctx.invoked_subcommand is None:
                from lemoncrow.core.capabilities.reporting.dashboard import render_overview

                _click.echo(render_overview(ctx.obj["root"]))

        @dashboard_group.command("open")
        @_click.option(
            "--port", default=None, type=int, help="Exact frontend port; otherwise discover the running dashboard"
        )
        @_click.pass_context
        def dashboard_open_cmd(ctx: _click.Context, port: int | None) -> None:
            """Open the already-running LemonCrow web UI."""
            import webbrowser

            from lemoncrow.infra.runtime.dashboard_url import discover_dashboard_url

            frontend_url = discover_dashboard_url(ctx.obj["root"], requested_port=port)
            if frontend_url is None:
                requested = f" on port {port}" if port is not None else ""
                _click.echo(
                    f"  LemonCrow dashboard is not running{requested}.\n\n"
                    f"  Start it once:\n"
                    f"    lc stack start\n\n"
                    f"  Then run: lc dashboard open"
                )
                return
            url = f"{frontend_url.rstrip('/')}/"
            _click.echo(f"  ◆ Opening LemonCrow dashboard: {url}")
            webbrowser.open(url)

        cli.add_command(dashboard_group)
        # Keep 'status' as a hidden alias for backward compatibility
        _h(admin_commands.status_cmd)
        cli.add_command(admin_commands.status_cmd)
    except (ModuleNotFoundError, ImportError):
        _IMPORT_FAILED = True

    try:
        import click as _click

        @_click.command("serve-openai")
        @_click.option(
            "--port", default=8790, show_default=True, help="Port to listen on (8787 is the LemonCrow service port)"
        )
        @_click.option(
            "--host",
            default="127.0.0.1",
            show_default=True,
            help="Bind address (default loopback-only; the gateway runs an auto-approving agent)",
        )
        @_click.option("--project-root", default=None, help="Project root directory")
        @_click.option(
            "--no-yolo",
            is_flag=True,
            default=False,
            help="Require manual approval for tool calls (default: auto-approve)",
        )
        def serve_openai_cmd(port: int, host: str, project_root: str | None, no_yolo: bool) -> None:
            """Start the OpenAI-compatible chat completions gateway.

            Any TUI that supports custom OpenAI-compatible endpoints can connect.

            \b
            OpenCode  (opencode.json):
              "provider": {"lemoncrow": {"npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://localhost:8787/v1", "apiKey": "local"}}}

            Crush  (crush.json):
              "providers": {"lemoncrow": {"type": "openai-compat",
                "base_url": "http://localhost:8787/v1", "api_key": "local"}}

            Codex  (~/.codex/config.toml):
              [model_providers.lemoncrow]
              base_url = "http://localhost:8787/v1"
              wire_api = "chat"
            """
            from lemoncrow.gateway.openai_gateway.serve import serve

            serve(port=port, host=host, project_root=project_root, yolo=not no_yolo)

        serve_openai_cmd.hidden = True  # internal: integrated into lc service
        cli.add_command(serve_openai_cmd, name="serve-openai")
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

    try:
        from .project import project_cmd

        _h(project_cmd)
        cli.add_command(project_cmd, name="project")
    except ImportError:
        pass


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
