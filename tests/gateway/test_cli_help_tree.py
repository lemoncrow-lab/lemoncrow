"""Recursive ``--help`` command-tree renderer and invariant guard (QBL-CLI-04).

This module provides ``render_help_tree()``, a deterministic, pure helper that
walks the entire Click command tree (groups, subgroups, and hidden commands)
and renders each command's help text. Later Phase 25 extraction slices use this
helper to capture a live *pre-edit* snapshot and compare it against a live
*post-edit* snapshot in the same (possibly dirty) working tree -- proving the
CLI surface stays byte-identical without committing a fixture generated from
unreproducible WIP.

registrations match the ordering used by ``tests/gateway/test_cli.py``.
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from atelier.gateway.cli import cli


def _walk(cmd: click.Command, path: list[str], out: list[str]) -> None:
    """Append ``cmd``'s help text, then recurse into subcommands sorted by name.

    Uses ``Group.list_commands`` + ``get_command`` (the same traversal as the
    existing ``help`` command) so the walk is deterministic. ``list_commands``
    includes hidden commands, so this walk also covers ``stack run``,
    ``servicectl run``, and the ``systemd`` group.
    """
    ctx = click.Context(cmd, info_name=path[-1] if path else cmd.name)
    out.append(f"{' '.join(path)}\n{cmd.get_help(ctx)}")
    if isinstance(cmd, click.Group):
        for name in sorted(cmd.list_commands(ctx)):
            sub = cmd.get_command(ctx, name)
            if sub is not None:
                _walk(sub, [*path, name], out)


def render_help_tree() -> str:
    """Render the full recursive ``--help`` tree as a deterministic string."""
    out: list[str] = []
    _walk(cli, ["atelier"], out)
    return "\n\n=====\n\n".join(out)


def _resolve(path: tuple[str, ...]) -> click.Command | None:
    """Resolve a (possibly hidden) command path via ``get_command`` recursion."""
    command: click.Command = cli
    for token in path:
        if not isinstance(command, click.Group):
            return None
        ctx = click.Context(command, info_name=token)
        nxt = command.get_command(ctx, token)
        if nxt is None:
            return None
        command = nxt
    return command


def test_full_help_tree_renders_deterministically() -> None:
    """The renderer must be stable within a single process (idempotent)."""
    assert render_help_tree() == render_help_tree()


def test_help_tree_contains_expected_public_groups() -> None:
    """Core public groups must appear in the rendered tree."""
    tree = render_help_tree()
    for group in ("benchmark", "stack", "servicectl", "ledger"):
        assert f"atelier {group}\n" in tree, f"missing group: {group}"


def test_help_tree_includes_hidden_command_paths() -> None:
    """Hidden commands are walked too -- assert their explicit paths exist."""
    tree = render_help_tree()
    for hidden_path in ("atelier stack run\n", "atelier servicectl run\n", "atelier systemd\n"):
        assert hidden_path in tree, f"missing hidden path: {hidden_path!r}"

    # And they must be directly resolvable via get_command recursion.
    assert _resolve(("stack", "run")) is not None
    assert _resolve(("servicectl", "run")) is not None
    assert _resolve(("systemd",)) is not None


def test_help_tree_excludes_mcp_only_entries() -> None:
    """MCP-tool-only *commands* must never appear as top-level entries.

    The suppressed *commands* (``context``/``rescue``/``verify``/``read``/
    ``edit``/``search``) have no live equivalent, so their dev registration is
    dropped entirely. The suppressed dev *groups* ``route``/``memory`` share a
    name with a live ``@cli.group`` (PATTERNS hazard 5), so the live groups DO
    appear -- they are covered by ``test_cli_mcp_only.py``.
    """
    tree = render_help_tree()
    for mcp_only in (
        "atelier context\n",
        "atelier rescue\n",
        "atelier verify\n",
        "atelier read\n",
        "atelier edit\n",
        "atelier search\n",
    ):
        assert mcp_only not in tree, f"leaked MCP-only command: {mcp_only!r}"

    # The live ``route``/``memory`` groups must remain present.
    assert "atelier route\n" in tree
    assert "atelier memory\n" in tree


def test_top_level_help_still_succeeds() -> None:
    """`atelier --help` must still exit 0 on the rendered CLI."""
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
