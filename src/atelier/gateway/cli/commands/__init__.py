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
