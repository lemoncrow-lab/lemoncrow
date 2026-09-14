"""`lc savings` is a permanent alias, and this file is the contract that says so.

The callers that matter are not in this repository. ``statusline.sh`` ships from
``bundle/`` into ``~/.claude`` and ``~/.codex``, and an installed copy from six
months ago will shell out to today's binary: it runs ``lc savings --segment``,
reads its configuration from ``LEMONCROW_STATUS*`` environment variables, and
parses raw stdout. Spec §5.1 therefore freezes the name, the flag, the env-var
contract and the stdout path. `lc usage` re-homing the surface is allowed to
hide ``savings`` from ``--help`` and nothing else.

Every assertion here is one of those four things.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result

from lemoncrow.gateway.cli import cli
from tests.helpers import init_store_at

# The full statusline contract, spec §5.1. Named here so a future rename has to
# delete a line from this list and explain itself.
_STATUSLINE_ENV = (
    "LEMONCROW_STATUS_SESSION_ID",
    "LEMONCROW_STATUS_HOST",
    "LEMONCROW_STATUSLINE_COST_USD",
    "LEMONCROW_STATUSLINE_LIVE_IN_TOK",
    "LEMONCROW_STATUSLINE_LIVE_CACHE_TOK",
    "LEMONCROW_STATUSLINE_LIVE_OUT_TOK",
    "LEMONCROW_STATUSLINE_CTX_PCT",
    "LEMONCROW_STATUSLINE_CTX_TOK",
    "LEMONCROW_STATUSLINE_NO_COLOR",
    "LEMONCROW_STATUS_WORKSPACE_ROOT",
    "LEMONCROW_STATUS_MODEL",
)


def _ctx() -> click.Context:
    return click.Context(cli, info_name="lc")


def _invoke(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), *args])


def test_savings_still_resolvable_after_rehome() -> None:
    assert cli.get_command(_ctx(), "savings") is not None


def test_savings_is_hidden_from_top_level_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    listed = {line.strip().split(" ", 1)[0] for line in result.output.splitlines() if line.startswith("  ")}
    assert "savings" not in listed
    assert "optimize" not in listed
    # The surface that replaced it in --help must actually be there.
    assert "usage" in listed


def test_savings_help_still_works_while_hidden() -> None:
    result = CliRunner().invoke(cli, ["savings", "--help"])

    assert result.exit_code == 0, result.output
    assert "--json" in result.output


def test_savings_segment_flag_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_STATUS_SESSION_ID", "statusline-session")
    monkeypatch.setenv("LEMONCROW_STATUS_HOST", "claude")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_COST_USD", "1.25")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_LIVE_IN_TOK", "1000")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_LIVE_CACHE_TOK", "200")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_LIVE_OUT_TOK", "300")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_CTX_PCT", "42")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_CTX_TOK", "84000")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_NO_COLOR", "1")

    result = _invoke(root, "savings", "--segment")

    assert result.exit_code == 0, result.output
    assert result.output != ""


def test_savings_segment_tolerates_malformed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A statusline must never be the thing that breaks a shell prompt."""

    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_STATUS_SESSION_ID", "statusline-session")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_COST_USD", "not-a-number")
    monkeypatch.setenv("LEMONCROW_STATUSLINE_CTX_PCT", "")

    result = _invoke(root, "savings", "--segment")

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output


def test_savings_segment_env_contract_is_unchanged() -> None:
    """Every variable in §5.1 must still be read by the segment branch."""

    source = Path("src/lemoncrow/gateway/cli/commands/savings.py").read_text(encoding="utf-8")
    for name in _STATUSLINE_ENV:
        assert name in source, f"statusline env var dropped: {name}"


def test_savings_segment_writes_raw_stdout_not_click_echo() -> None:
    """``click.echo`` strips ANSI off a non-TTY; the segment is colour-bearing."""

    source = Path("src/lemoncrow/gateway/cli/commands/savings.py").read_text(encoding="utf-8")
    assert "sys.stdout.write(" in source
    assert "sys.stdout.flush()" in source


def test_savings_json_still_works(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    result = _invoke(root, "savings", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "optimization" in payload
    assert "calls_avoided" in payload


def test_savings_detail_and_reset_still_registered() -> None:
    savings = cli.get_command(_ctx(), "savings")
    assert isinstance(savings, click.Group)

    ctx = click.Context(savings, info_name="savings")
    assert savings.get_command(ctx, "detail") is not None
    assert savings.get_command(ctx, "reset") is not None


def test_optimize_still_resolvable_at_top_level() -> None:
    """`lc optimize ...` keeps working -- four existing test files invoke it."""

    optimize = cli.get_command(_ctx(), "optimize")
    assert isinstance(optimize, click.Group)

    ctx = click.Context(optimize, info_name="optimize")
    for name in ("details", "apply", "run", "gate", "shadow", "compress-context"):
        assert optimize.get_command(ctx, name) is not None, f"lc optimize {name} disappeared"


def test_build_savings_payload_matches_the_cli_json(tmp_path: Path) -> None:
    """The §3 PR-7 extraction is behaviour-preserving, asserted rather than claimed."""

    from lemoncrow.gateway.cli.commands.savings import build_savings_payload

    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    result = _invoke(root, "savings", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == json.loads(json.dumps(build_savings_payload(root), default=str))
