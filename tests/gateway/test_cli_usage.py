"""``lc usage`` CLI surface: the views, the JSON contract, and the honesty rules.

Two of these tests are the product contract rather than plumbing.

``test_usage_local_model_renders_local_not_dollar_zero`` is the one that keeps
the whole read model worth having: a model with no rate card must never render
as ``$0.00``, because a busy local session shown as free is worse than no
report at all.

``test_usage_first_screen_has_no_counterfactual`` locks plan 2026-09-07 §4.2 --
the default screen reports what happened and nothing on it depends on a model
of what *could* have happened. That analysis lives behind `lc usage optimize`,
which a reader has to ask for.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
from click.testing import CliRunner, Result

from lemoncrow.gateway.cli import cli
from tests.helpers import init_store_at

_Call = tuple[str, int, int]
"""``(model, input_tokens, output_tokens)`` for one recorded call."""


def _ctx() -> click.Context:
    return click.Context(cli, info_name="lc")


def _invoke(root: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), *args])


def _write_run(
    root: Path,
    *,
    session_id: str,
    host: str = "claude",
    calls: Sequence[_Call] = (),
    workspace: str | None = "/home/dev/alpha",
    total_cost_usd: float = 0.0,
    minutes_ago: int = 30,
) -> Path:
    """Seed one ``run.json`` in the canonical partitioned layout.

    The timestamp is relative to *now* so the row lands inside the default
    ``--since 7d`` window without the test having to pin a clock.
    """

    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    snapshot: dict[str, Any] = {
        "session_id": session_id,
        "status": "completed",
        "created_at": started.isoformat(),
        "updated_at": started.isoformat(),
        "tool_call_count": 0,
        "telemetry": {},
        "cost": {
            "total_cost_usd": total_cost_usd,
            "calls": [
                {
                    "model": model,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
                for model, inp, out in calls
            ],
        },
        "events": [],
    }
    if workspace is not None:
        snapshot["workspace_path"] = workspace

    target = (
        root / "sessions" / started.strftime("%Y") / started.strftime("%m") / started.strftime("%d") / host / session_id
    )
    target.mkdir(parents=True, exist_ok=True)
    run_path = target / "run.json"
    run_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return run_path


# --------------------------------------------------------------------------- #
# registration                                                                #
# --------------------------------------------------------------------------- #


def test_usage_registered_and_help_exits_zero() -> None:
    ctx = _ctx()
    assert "usage" in cli.list_commands(ctx)
    assert cli.get_command(ctx, "usage") is not None

    result = CliRunner().invoke(cli, ["usage", "--help"])
    assert result.exit_code == 0, result.output
    assert "--by" in result.output
    assert "--since" in result.output


def test_usage_subcommands_are_registered() -> None:
    ctx = _ctx()
    usage = cli.get_command(ctx, "usage")
    assert isinstance(usage, click.Group)
    names = set(usage.list_commands(click.Context(usage, info_name="usage")))
    assert {"explain", "rows", "optimize"} <= names


def test_usage_help_lists_optimize_even_though_it_is_globally_hidden() -> None:
    """`optimize` is hidden at top level; the promoted spelling must still show.

    One Click object is registered twice, so the top-level ``hidden`` flag would
    otherwise erase `lc usage optimize` from this group's help and leave the
    headline optimisation surface undiscoverable.
    """

    usage_help = CliRunner().invoke(cli, ["usage", "--help"])
    top_help = CliRunner().invoke(cli, ["--help"])

    assert usage_help.exit_code == 0, usage_help.output
    listed = {line.strip().split(" ", 1)[0] for line in usage_help.output.splitlines() if line.startswith("  ")}
    assert {"explain", "rows", "optimize"} <= listed

    # ...and the top-level listing is unchanged by that.
    top_listed = {line.strip().split(" ", 1)[0] for line in top_help.output.splitlines() if line.startswith("  ")}
    assert "optimize" not in top_listed


# --------------------------------------------------------------------------- #
# default view / grouping                                                     #
# --------------------------------------------------------------------------- #


def test_usage_json_default_view(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-one", calls=[("claude-sonnet-4-5", 1000, 200)])

    result = _invoke(root, "usage", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert {"since", "by", "groups", "totals", "provenance_mix"} <= set(payload)
    assert payload["schema_version"] >= 1
    assert payload["by"] == "host"
    assert payload["totals"]["rows"] == 1


def test_usage_by_model_groups_correctly(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-one", calls=[("claude-sonnet-4-5", 1000, 200)])
    _write_run(root, session_id="s-two", calls=[("gpt-4o-mini", 800, 100)])

    result = _invoke(root, "usage", "--by", "model", "--json")

    assert result.exit_code == 0, result.output
    keys = {group["key"] for group in json.loads(result.output)["groups"]}
    assert keys == {"claude-sonnet-4-5", "gpt-4o-mini"}


def test_usage_by_project_uses_workspace_basename(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-one", calls=[("claude-sonnet-4-5", 10, 5)], workspace="/home/dev/alpha")
    _write_run(root, session_id="s-two", calls=[("claude-sonnet-4-5", 10, 5)], workspace="/srv/code/beta")

    result = _invoke(root, "usage", "--by", "project", "--json")

    assert result.exit_code == 0, result.output
    keys = {group["key"] for group in json.loads(result.output)["groups"]}
    assert keys == {"alpha", "beta"}


def test_usage_host_filter_narrows_the_report(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-one", host="claude", calls=[("claude-sonnet-4-5", 10, 5)])
    _write_run(root, session_id="s-two", host="codex", calls=[("gpt-4o-mini", 10, 5)])

    result = _invoke(root, "usage", "--host", "codex", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [group["key"] for group in payload["groups"]] == ["codex"]
    assert payload["filters"]["host"] == "codex"


def test_usage_bad_since_fails_cleanly(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"

    result = _invoke(root, "usage", "--since", "last-tuesday")

    assert result.exit_code != 0
    assert "duration must look like" in result.output
    assert "Traceback" not in result.output


def test_usage_empty_store_emits_a_value_not_an_error(tmp_path: Path) -> None:
    """A store with no sessions is a degraded path, and §6.5 says it emits."""

    root = tmp_path / ".lemoncrow"

    text = _invoke(root, "usage", "--no-color")
    payload = _invoke(root, "usage", "--json")

    assert text.exit_code == 0, text.output
    assert "no usage recorded" in text.output
    assert payload.exit_code == 0, payload.output
    assert json.loads(payload.output)["totals"]["rows"] == 0


# --------------------------------------------------------------------------- #
# the two product contracts                                                   #
# --------------------------------------------------------------------------- #


def test_usage_local_model_renders_local_not_dollar_zero(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-local", host="opencode", calls=[("ollama/qwen3-coder", 120_000, 4_000)])

    result = _invoke(root, "usage", "--no-color")

    assert result.exit_code == 0, result.output
    assert "local" in result.output
    assert "$0.00" not in result.output


def test_usage_zero_rate_local_model_still_renders_local(tmp_path: Path) -> None:
    """LiteLLM prices a few self-hosted ids at literally zero -- still not money.

    ``ollama/llama3`` resolves to a *known* rate card whose every rate is 0, so
    the row comes back priced at ``$0.00``. That is a rate-card artifact, and
    rendering it as a dollar amount is the same lie as rendering an unpriced
    row that way.
    """

    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-local", host="opencode", calls=[("ollama/llama3", 120_000, 4_000)])

    result = _invoke(root, "usage", "--no-color")

    assert result.exit_code == 0, result.output
    assert "local" in result.output
    assert "$0.00" not in result.output


def test_usage_local_model_json_reports_null_cost(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-local", host="opencode", calls=[("ollama/qwen3-coder", 120_000, 4_000)])

    result = _invoke(root, "usage", "--json")

    assert result.exit_code == 0, result.output
    totals = json.loads(result.output)["totals"]
    assert totals["unpriced_rows"] == 1
    assert totals["billed_usd"] == 0.0
    assert totals["total_tokens"] == 124_000


def test_usage_first_screen_has_no_counterfactual(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-one", calls=[("claude-sonnet-4-5", 5_000, 900)])

    result = _invoke(root, "usage", "--no-color")

    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    for counterfactual in ("potential optimization", "would have saved", "could have saved"):
        assert counterfactual not in lowered


# --------------------------------------------------------------------------- #
# explain                                                                     #
# --------------------------------------------------------------------------- #


def test_usage_explain_run(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="explain-me-1234", calls=[("claude-sonnet-4-5", 4_000, 700)])

    result = _invoke(root, "usage", "explain", "explain-me-1234", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["session_id"] == "explain-me-1234"
    assert payload["schema_version"] >= 1
    assert {entry["bucket"] for entry in payload["breakdown"]} >= {"fresh_input", "output", "subagents"}
    assert abs(sum(entry["share"] for entry in payload["breakdown"]) - 1.0) < 1e-6


def test_usage_explain_accepts_a_unique_prefix(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="explain-me-1234", calls=[("claude-sonnet-4-5", 4_000, 700)])

    result = _invoke(root, "usage", "explain", "explain-me", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["session_id"] == "explain-me-1234"


def test_usage_explain_renders_text(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="explain-me-1234", calls=[("claude-sonnet-4-5", 4_000, 700)])

    result = _invoke(root, "usage", "explain", "explain-me-1234", "--no-color")

    assert result.exit_code == 0, result.output
    assert "WHERE IT WENT" in result.output
    assert "fresh_input" in result.output


def test_usage_explain_unknown_run_errors(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"

    result = _invoke(root, "usage", "explain", "no-such-run", "--json")

    assert result.exit_code != 0
    assert "no-such-run" in result.output
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------- #
# rows                                                                        #
# --------------------------------------------------------------------------- #


def test_usage_rows_emits_canonical_rows(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="s-one", calls=[("claude-sonnet-4-5", 1000, 200)])

    result = _invoke(root, "usage", "rows", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 1
    row = payload["rows"][0]
    assert row["session_id"] == "s-one"
    assert row["cost_provenance"] in {"api_estimated", "provider_billed", "unknown"}
    assert row["started_at"].endswith("+00:00")


def test_usage_rows_respects_limit(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    for index in range(3):
        _write_run(root, session_id=f"s-{index}", calls=[("claude-sonnet-4-5", 100, 10)], minutes_ago=index + 1)

    result = _invoke(root, "usage", "rows", "--limit", "2", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["count"] == 2


# --------------------------------------------------------------------------- #
# optimize (the re-homed savings surface)                                     #
# --------------------------------------------------------------------------- #


def test_usage_optimize_carries_the_savings_payload(tmp_path: Path) -> None:
    """`lc usage optimize --json` must contain `lc savings --json`, verbatim.

    Spec §3 PR-7 asks for equality. It cannot be equality: `optimize` is one
    Click object registered under two parents, and `lc optimize --json` has a
    documented payload (``host``/``recommendations``/``trace_count``) that
    ``tests/gateway/test_cli_v2.py`` pins. Equality would break that test, so
    the savings dict is carried as an additive key instead -- one command, one
    behaviour, and the two reports still provably agree.
    """

    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    savings = _invoke(root, "savings", "--json")
    optimize = _invoke(root, "usage", "optimize", "--json")

    assert savings.exit_code == 0, savings.output
    assert optimize.exit_code == 0, optimize.output
    assert json.loads(optimize.output)["savings"] == json.loads(savings.output)


def test_usage_optimize_preserves_the_legacy_advisor_payload(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    through_usage = _invoke(root, "usage", "optimize", "--json")
    through_alias = _invoke(root, "optimize", "--json")

    assert through_usage.exit_code == 0, through_usage.output
    assert through_alias.exit_code == 0, through_alias.output
    payload = json.loads(through_usage.output)
    assert {"host", "recommendations", "trace_count", "advisor"} <= set(payload)
    assert json.loads(through_alias.output) == payload


def test_usage_optimize_detail_and_reset_registered() -> None:
    ctx = _ctx()
    usage = cli.get_command(ctx, "usage")
    assert isinstance(usage, click.Group)
    optimize = usage.get_command(click.Context(usage, info_name="usage"), "optimize")
    assert isinstance(optimize, click.Group)

    optimize_ctx = click.Context(optimize, info_name="optimize")
    assert optimize.get_command(optimize_ctx, "detail") is not None
    assert optimize.get_command(optimize_ctx, "reset") is not None


def test_usage_optimize_detail_runs(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    result = _invoke(root, "usage", "optimize", "detail", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "summary" in payload
    assert "operations" in payload


def test_usage_optimize_reset_runs(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))

    result = _invoke(root, "usage", "optimize", "reset", "--force")

    assert result.exit_code == 0, result.output
    assert "reset" in result.output
