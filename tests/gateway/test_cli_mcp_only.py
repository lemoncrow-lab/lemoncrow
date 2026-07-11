"""MCP-tool-only suppression regression coverage (QBL-CLI-04, T-25-01).

These commands/groups are intentionally exposed only as MCP tools and must
never leak into the public Click CLI tree. The dev registration helpers
(`_dev_command`/`_dev_group`) drop names in ``MCP_TOOL_ONLY_COMMANDS`` /
``MCP_TOOL_ONLY_GROUPS`` at import time via ``_DummyGroup``.

Hazard 5 (PATTERNS): ``memory`` and ``route`` are *duplicate* names -- the
suppressed dev ``@_dev_group("memory")`` / ``@_dev_group("route")`` share a name
with a live ``@cli.group`` that MUST remain resolvable. This test locks both
halves so a future relocation cannot silently leak dev commands or remove the
live groups.

registrations are exercised (matching ``tests/gateway/test_cli.py``).
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.app import MCP_TOOL_ONLY_COMMANDS, MCP_TOOL_ONLY_GROUPS

# Dev-only subcommands of the suppressed @_dev_group("memory") / @_dev_group("route").
# These have no live equivalent and must be absent from the resolvable tree.
# NOTE: ``recall`` is a live user-facing command (`lemon memory recall <query>`),
# so it is intentionally excluded here -- only the raw MCP-block primitives are dev-only.
_DEV_ONLY_MEMORY_SUBCOMMANDS = ("upsert", "get", "archive")
_DEV_ONLY_ROUTE_SUBCOMMANDS = ("decide",)


def _root_ctx() -> click.Context:
    return click.Context(cli, info_name="lemon")


def test_mcp_only_commands_absent_from_top_level() -> None:
    """context/rescue/verify/read/edit/search must not register on the CLI."""
    ctx = _root_ctx()
    top_level = set(cli.list_commands(ctx))
    for name in MCP_TOOL_ONLY_COMMANDS:
        assert name not in top_level, f"MCP-only command leaked: {name}"
        assert cli.get_command(ctx, name) is None, f"MCP-only command resolvable: {name}"


def test_mcp_only_commands_absent_from_help_output() -> None:
    """The suppressed commands must not appear in `lemon --help`."""
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    for name in MCP_TOOL_ONLY_COMMANDS:
        # Match a command listing line: leading whitespace then the exact name.
        assert not any(
            line.strip().split(" ", 1)[0] == name for line in lines
        ), f"MCP-only command listed in --help: {name}"


def test_mcp_only_groups_have_live_equivalents() -> None:
    """memory/route are in MCP_TOOL_ONLY_GROUPS but the live groups must resolve."""
    assert set(MCP_TOOL_ONLY_GROUPS) == {"memory", "route"}
    ctx = _root_ctx()
    for name in MCP_TOOL_ONLY_GROUPS:
        live = cli.get_command(ctx, name)
        assert live is not None, f"live group missing: {name}"
        assert isinstance(live, click.Group), f"live {name} is not a Group"

    # The live groups are resolvable end-to-end via --help.
    for name in MCP_TOOL_ONLY_GROUPS:
        result = CliRunner().invoke(cli, [name, "--help"])
        assert result.exit_code == 0, f"`lemon {name} --help` failed:\n{result.output}"


def test_dev_only_subcommands_suppressed_on_live_groups() -> None:
    """The dev memory/route subcommands must NOT appear on the live groups."""
    ctx = _root_ctx()

    memory = cli.get_command(ctx, "memory")
    assert isinstance(memory, click.Group)
    memory_ctx = click.Context(memory, info_name="memory")
    memory_subs = set(memory.list_commands(memory_ctx))
    for sub in _DEV_ONLY_MEMORY_SUBCOMMANDS:
        assert sub not in memory_subs, f"dev memory subcommand leaked: {sub}"

    route = cli.get_command(ctx, "route")
    assert isinstance(route, click.Group)
    route_ctx = click.Context(route, info_name="route")
    route_subs = set(route.list_commands(route_ctx))
    for sub in _DEV_ONLY_ROUTE_SUBCOMMANDS:
        assert sub not in route_subs, f"dev route subcommand leaked: {sub}"
