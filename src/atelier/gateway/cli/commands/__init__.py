"""Aggregator entrypoint for relocated Atelier CLI command modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import click


def register(cli: click.Group) -> None:
    """Register relocated command modules onto the root ``cli`` group."""
    try:
        from .admin import (
            audit_group,
            deprecate,
            detect_loop_cmd,
            doctor_cmd,
            env_group,
            governance_group,
            init,
            insights_cmd,
            login_cmd,
            logout_cmd,
            loop_report_cmd,
            plugin_settings_group,
            quarantine,
            reset_cmd,
            share_cmd,
            status_cmd,
            team_group,
            tool_report_cmd,
            uninstall,
        )

        cli.add_command(init)
        cli.add_command(uninstall)
        cli.add_command(env_group)
        cli.add_command(deprecate)
        cli.add_command(quarantine)
        cli.add_command(login_cmd)
        cli.add_command(logout_cmd)
        cli.add_command(status_cmd)
        cli.add_command(share_cmd)
        cli.add_command(plugin_settings_group)
        cli.add_command(cast("click.Command", detect_loop_cmd))
        cli.add_command(loop_report_cmd)
        cli.add_command(tool_report_cmd)
        cli.add_command(doctor_cmd)
        cli.add_command(reset_cmd)
        cli.add_command(team_group)
        cli.add_command(governance_group)
        cli.add_command(audit_group)
        cli.add_command(insights_cmd)
    except ModuleNotFoundError:
        pass

    try:
        from .blocks import (
            add_block,
            block_group,
            domain_group,
            import_style_guide_cmd,
            list_blocks_cmd,
            reembed,
            report_cmd,
        )

        cli.add_command(cast("click.Command", reembed))
        cli.add_command(cast("click.Command", add_block))
        cli.add_command(domain_group)
        cli.add_command(report_cmd)
        cli.add_command(import_style_guide_cmd)
        cli.add_command(block_group)
        cli.add_command(list_blocks_cmd)
    except ModuleNotFoundError:
        pass

    try:
        from .telemetry import telemetry_group

        cli.add_command(telemetry_group)
    except ModuleNotFoundError:
        pass

    try:
        from .letta import letta_group

        cli.add_command(letta_group)
    except ModuleNotFoundError:
        pass

    try:
        from .openmemory import openmemory_group

        cli.add_command(openmemory_group)
    except ModuleNotFoundError:
        pass

    try:
        from .stack import stack_group

        cli.add_command(stack_group)
    except ModuleNotFoundError:
        pass

    try:
        from .servicectl import logs_cmd, service_group, servicectl_group, worker_group

        cli.add_command(service_group)
        cli.add_command(worker_group)
        cli.add_command(servicectl_group)
        cli.add_command(logs_cmd)
    except ModuleNotFoundError:
        pass

    try:
        from .background import background_group, systemd_alias_group

        cli.add_command(background_group)
        cli.add_command(systemd_alias_group)
    except ModuleNotFoundError:
        pass

    try:
        from .tools import tool_mode, tools_group

        cli.add_command(tool_mode)
        cli.add_command(tools_group)
    except ModuleNotFoundError:
        pass

    try:
        from .savings import (
            external_report_cmd,
            external_status_cmd,
            optimize_group,
            savings_cmd,
            savings_detail,
            savings_reset,
        )

        cli.add_command(savings_cmd)
        cli.add_command(optimize_group)
        cli.add_command(external_status_cmd)
        cli.add_command(external_report_cmd)
        cli.add_command(savings_detail)
        cli.add_command(savings_reset)
    except ModuleNotFoundError:
        pass

    try:
        from .benchmark import benchmark_group

        cli.add_command(benchmark_group)
    except ModuleNotFoundError:
        pass

    try:
        from .code import code_group, zoekt_group

        cli.add_command(code_group)
        cli.add_command(zoekt_group)
    except ModuleNotFoundError:
        pass

    try:
        from .route import proof_group, route_public_group

        cli.add_command(route_public_group)
        cli.add_command(proof_group)
    except ModuleNotFoundError:
        pass

    try:
        from .hosts import claude, codex, copilot, gemini, global_import, opencode

        cli.add_command(copilot)
        cli.add_command(claude)
        cli.add_command(codex)
        cli.add_command(opencode)
        cli.add_command(gemini)
        cli.add_command(global_import)
    except ModuleNotFoundError:
        pass

    try:
        from .lessons import (
            analyze_failures_cmd,
            checkpoint,
            eval_,
            eval_from_cluster,
            failure,
            ledger,
            lesson,
        )

        cli.add_command(ledger)
        cli.add_command(checkpoint)
        cli.add_command(failure)
        cli.add_command(lesson)
        cli.add_command(analyze_failures_cmd)
        cli.add_command(eval_)
        cli.add_command(eval_from_cluster)
    except ModuleNotFoundError:
        pass

    try:
        from .sessions import outcomes_group, runs_group, session_group

        cli.add_command(runs_group)
        cli.add_command(outcomes_group)
        cli.add_command(session_group)
    except ModuleNotFoundError:
        pass

    try:
        from .swarm import swarm_group

        cli.add_command(swarm_group)
    except ModuleNotFoundError:
        pass

    try:
        from .memory import memory_group_cli

        cli.add_command(memory_group_cli)
    except ModuleNotFoundError:
        pass
