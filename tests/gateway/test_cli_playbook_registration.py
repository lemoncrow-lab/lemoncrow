"""Regression: playbook curation commands must be registered on the CLI.

Previously ``cli/commands/__init__.py:register()`` imported only ``domain_group``
and ``report_cmd`` from ``playbooks.py``, leaving ``playbook_group``,
``list-playbooks``, ``add-playbook``, ``import-style-guide`` and ``reembed``
defined and ``__all__``-exported but never added to the root ``cli`` group, so
they were unreachable from the command line.
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from atelier.gateway.cli import cli

_EXPECTED_TOP_LEVEL = (
    "playbook",
    "list-playbooks",
    "add-playbook",
    "import-style-guide",
    "reembed",
)


def _ctx() -> click.Context:
    return click.Context(cli, info_name="atelier")


def test_playbook_curation_commands_registered() -> None:
    ctx = _ctx()
    top_level = set(cli.list_commands(ctx))
    for name in _EXPECTED_TOP_LEVEL:
        assert name in top_level, f"playbook command not registered: {name}"
        assert cli.get_command(ctx, name) is not None, f"playbook command unresolvable: {name}"


def test_playbook_group_exposes_subcommands() -> None:
    ctx = _ctx()
    group = cli.get_command(ctx, "playbook")
    assert isinstance(group, click.Group)
    sub_ctx = click.Context(group, info_name="playbook")
    subs = set(group.list_commands(sub_ctx))
    assert {"list", "add", "extract"} <= subs, f"playbook subcommands missing: {sorted(subs)}"


def test_playbook_commands_help_succeeds() -> None:
    runner = CliRunner()
    for args in (
        ["playbook", "--help"],
        ["list-playbooks", "--help"],
        ["add-playbook", "--help"],
        ["import-style-guide", "--help"],
        ["reembed", "--help"],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, f"`atelier {' '.join(args)}` failed:\n{result.output}"


def test_existing_playbook_registrations_intact() -> None:
    """domain group + report command must still resolve (no regression)."""
    ctx = _ctx()
    assert isinstance(cli.get_command(ctx, "domain"), click.Group)
    assert cli.get_command(ctx, "report") is not None
