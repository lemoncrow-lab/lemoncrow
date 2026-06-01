"""Aggregator entrypoint for relocated Atelier CLI command modules.

``register(cli)`` import-and-``add_command``s each extracted command module onto
the root ``cli`` group. It mirrors ``_register_swe_benchmark_group``'s resilient
try/except ``ModuleNotFoundError`` style so partial installs keep CLI startup
working. For Plan 25-01 this is an intentionally empty stub: no command groups
have moved yet. Later Phase 25 slices add their imports here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import click


def register(cli: click.Group) -> None:
    """Register relocated command modules onto the root ``cli`` group.

    Each future ``commands/<group>.py`` exports a top-level Click group; this
    function imports it and calls ``cli.add_command(...)``. Imports are wrapped
    in try/except ``ModuleNotFoundError`` so a missing optional module never
    breaks CLI startup (mirrors ``_register_swe_benchmark_group``).
    """
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

        # ``benchmark.py`` attaches the optional SWE group to ``benchmark_group``
        # at import time (resilient to ModuleNotFoundError), so registering the
        # group here preserves the original "SWE after benchmark_group" ordering.
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
        from .sessions import outcomes_group, runs_group, session_group

        cli.add_command(runs_group)
        cli.add_command(outcomes_group)
        cli.add_command(session_group)
    except ModuleNotFoundError:
        pass

    try:
        from .memory import memory_group_cli

        cli.add_command(memory_group_cli)
    except ModuleNotFoundError:
        pass
