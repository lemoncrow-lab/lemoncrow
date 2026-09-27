"""``lc run explain``: what it is allowed to claim, and what it must refuse to.

The product claim is not "it finds the cause" -- it is "every line it prints is
a record you can go and open, and every source it could not read is named". So
the coverage weight sits on the two ways that claim breaks: inventing a signal
(a healthy event becoming evidence, a timestamp becoming "now", an unmatched
error filed under the nearest plausible category), and hiding a gap (a missing
trace or an evicted event window passing for silence).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from lemoncrow.core.foundation.history_store import HistoryStore
from lemoncrow.core.foundation.models import RepeatedFailure, Trace
from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands.run import run_group
from lemoncrow.pro.capabilities.run_explain.attribution import (
    UNRESOLVED_REASON,
    explain_run_attribution,
    render_attribution,
)
from lemoncrow.pro.capabilities.run_explain.models import ATTRIBUTION_CATEGORY_VALUES

_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OTHER = "ffffffff-1111-2222-3333-444444444444"
_AT = "2026-09-07T12:00:00+00:00"


# ----- fixtures ------------------------------------------------------------ #


def _event(kind: str, summary: str, payload: dict[str, Any] | None = None, at: str = _AT) -> dict[str, Any]:
    return {"kind": kind, "at": at, "summary": summary, "payload": payload or {}}


def _ledger(
    root: Path,
    events: list[dict[str, Any]],
    *,
    session_id: str = _SESSION,
    status: str = "done",
    host: str = "claude",
) -> Path:
    """Seed a ``run.json`` shaped exactly like ``RunLedger.snapshot()``."""

    directory = session_dir(root, host, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    run_file = directory / "run.json"
    run_file.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "agent": host,
                "task": "wire the router",
                "status": status,
                "created_at": "2026-09-07T11:00:00+00:00",
                "updated_at": "2026-09-07T13:00:00+00:00",
                "events": events,
            }
        ),
        encoding="utf-8",
    )
    return run_file


def _trace(
    root: Path,
    *,
    session_id: str = _SESSION,
    errors: tuple[str, ...] = (),
    repeated: tuple[RepeatedFailure, ...] = (),
) -> Trace:
    trace = Trace(
        id=f"trace-{session_id}",
        session_id=session_id,
        agent="claude",
        domain="code",
        task="wire the router",
        status="success",
        errors_seen=list(errors),
        repeated_failures=list(repeated),
        host="claude",
        model="claude-opus-5",
        created_at=datetime(2026, 9, 7, 11, 0, tzinfo=UTC),
    )
    store = HistoryStore(root)
    store.init()
    store.record_trace(trace)
    return trace


def _invoke(root: Path, *args: str) -> Any:
    return CliRunner().invoke(cli, ["--root", str(root), "run", "explain", *args], catch_exceptions=False)


def _ctx() -> click.Context:
    return click.Context(cli, info_name="lc")


def _categories(report: Any) -> set[str]:
    return {signal.category for signal in report.signals}


# ----- registration -------------------------------------------------------- #


def test_run_explain_registered() -> None:
    ctx = _ctx()
    assert "run" in cli.list_commands(ctx)
    group = cli.get_command(ctx, "run")
    assert isinstance(group, click.Group)
    assert group.get_command(click.Context(group, info_name="run"), "explain") is not None

    result = CliRunner().invoke(cli, ["run", "explain", "--help"])
    assert result.exit_code == 0, result.output
    assert "--limit" in result.output
    assert "--json" in result.output


def test_run_group_visible_but_start_hidden() -> None:
    """`lc run` graduates to the visible surface; the unlaunched trio does not."""

    ctx = _ctx()
    group = cli.get_command(ctx, "run")
    assert isinstance(group, click.Group)
    assert group.hidden is False

    sub_ctx = click.Context(run_group, info_name="run")
    for name in ("start", "resume", "report"):
        command = run_group.get_command(sub_ctx, name)
        assert command is not None, name
        assert command.hidden is True, name

    explain = run_group.get_command(sub_ctx, "explain")
    assert explain is not None
    assert explain.hidden is False


# ----- the event -> category table ----------------------------------------- #


def test_explain_buckets_failed_command_as_shell(tmp_path: Path) -> None:
    _ledger(
        tmp_path,
        [
            _event("command_result", "pytest -q", {"ok": False, "error_signature": "3 failed"}),
        ],
    )
    report = explain_run_attribution(tmp_path, _SESSION)

    assert _categories(report) == {"shell"}
    signal = report.signals[0]
    assert "pytest -q" in signal.summary
    assert "3 failed" in signal.summary
    assert signal.evidence_path.endswith("run.json#events[0]")


def test_explain_buckets_rate_limit_as_provider(tmp_path: Path) -> None:
    _ledger(tmp_path, [])
    _trace(tmp_path, errors=("429 rate limit exceeded, retry in 30s",))
    report = explain_run_attribution(tmp_path, _SESSION)

    provider = [signal for signal in report.signals if signal.category == "provider"]
    assert len(provider) == 1
    assert "429" in provider[0].summary
    assert provider[0].evidence_path == f"trace:trace-{_SESSION}#errors_seen[0]"


def test_explain_buckets_context_length_as_model(tmp_path: Path) -> None:
    _ledger(tmp_path, [])
    _trace(tmp_path, errors=("prompt is too long: context length exceeded",))
    report = explain_run_attribution(tmp_path, _SESSION)

    assert [signal.category for signal in report.signals] == ["model"]


def test_explain_buckets_repeated_failures_as_model(tmp_path: Path) -> None:
    _ledger(tmp_path, [])
    _trace(
        tmp_path,
        repeated=(RepeatedFailure(signature="ImportError: no module named x", count=4),),
    )
    report = explain_run_attribution(tmp_path, _SESSION)

    model = [signal for signal in report.signals if signal.category == "model"]
    assert len(model) == 1
    assert "4x" in model[0].summary
    assert model[0].evidence_path.endswith("#repeated_failures[0]")
    assert model[0].at is not None


def test_explain_buckets_watchdog_rubric_and_validation(tmp_path: Path) -> None:
    """One ledger exercising the lemoncrow / policy / repository rows at once."""

    _ledger(
        tmp_path,
        [
            _event("watchdog_alert", "[high] loop: same edit 5x", {"severity": "high"}),
            _event("rubric_run", "Rubric ship status: blocked", {"status": "blocked"}),
            _event("validation", "typecheck", {"results": [{"name": "mypy", "passed": False}]}),
            _event("test_result", "pytest=fail", {"test_id": "pytest", "passed": False}),
        ],
    )
    report = explain_run_attribution(tmp_path, _SESSION)

    assert _categories(report) == {"lemoncrow", "policy", "repository"}
    assert dict(report.category_counts)["repository"] == 2


def test_explain_buckets_route_decision_as_provider(tmp_path: Path) -> None:
    _ledger(tmp_path, [_event("route_decision", "auto route for workflow", {"mode": "auto"})])
    report = explain_run_attribution(tmp_path, _SESSION)

    assert [signal.category for signal in report.signals] == ["provider"]


def test_unrecognised_failure_is_unknown_not_the_nearest_category(tmp_path: Path) -> None:
    """A recorded failure with no table row stays ``unknown`` -- it is not guessed."""

    _ledger(tmp_path, [_event("agent_message", "give up", {"status": "failed"})])
    _trace(tmp_path, errors=("something went sideways",))
    report = explain_run_attribution(tmp_path, _SESSION)

    assert _categories(report) == {"unknown"}
    assert len(report.signals) == 2


def test_healthy_events_never_become_signals(tmp_path: Path) -> None:
    """A passing command, a passing test and an edit are not evidence of anything."""

    _ledger(
        tmp_path,
        [
            _event("command_result", "pytest -q", {"ok": True, "error_signature": ""}),
            _event("test_result", "pytest=pass", {"test_id": "pytest", "passed": True}),
            _event("file_edit", "edit:src/app.py", {"path": "src/app.py", "event": "edit"}),
            _event("rubric_run", "Rubric ship status: pass", {"status": "pass"}),
            _event("tool_call", "Read", {"output_chars": 120}),
        ],
    )
    report = explain_run_attribution(tmp_path, _SESSION)

    assert report.signals == ()


# ----- the honesty rules --------------------------------------------------- #


def test_explain_never_asserts_blame_without_evidence(tmp_path: Path) -> None:
    """A clean run names no category at all -- in the record or in the render."""

    _ledger(tmp_path, [_event("command_result", "pytest -q", {"ok": True})])
    report = explain_run_attribution(tmp_path, _SESSION)

    assert report.signals == ()
    assert report.category_counts == ()

    rendered = render_attribution(report)
    for category in ATTRIBUTION_CATEGORY_VALUES:
        assert category.upper() not in rendered, category
    assert "no failure evidence was recorded" in rendered


def test_explain_reports_unresolved_sources(tmp_path: Path) -> None:
    """No trace in the store is a named gap, not silence."""

    _ledger(tmp_path, [])
    report = explain_run_attribution(tmp_path, _SESSION)

    assert "trace" in report.unresolved
    rendered = render_attribution(report)
    assert "NOT SEEN" in rendered
    assert UNRESOLVED_REASON["trace"] in rendered


def test_missing_ledger_is_a_named_source_not_a_category(tmp_path: Path) -> None:
    """A session known only to the trace store still reports; the gap is named."""

    _ledger(tmp_path, [], session_id=_OTHER)
    _trace(tmp_path, session_id=_SESSION, errors=("429 too many requests",))
    report = explain_run_attribution(tmp_path, _SESSION)

    assert "run_ledger" in report.unresolved
    assert report.status == "unknown"
    assert "host" not in _categories(report)


def test_explain_flags_truncated_events(tmp_path: Path) -> None:
    """At the retention cap the early window is gone and the report says so."""

    from lemoncrow.pro.capabilities.run_explain.attribution import _retention_cap

    cap = _retention_cap()
    _ledger(tmp_path, [_event("tool_call", f"call {index}") for index in range(cap)])
    report = explain_run_attribution(tmp_path, _SESSION)

    assert "events_truncated" in report.unresolved
    assert UNRESOLVED_REASON["events_truncated"] in render_attribution(report)


def test_short_ledger_is_not_flagged_as_truncated(tmp_path: Path) -> None:
    _ledger(tmp_path, [_event("tool_call", "call")])
    assert "events_truncated" not in explain_run_attribution(tmp_path, _SESSION).unresolved


def test_event_timestamps_are_the_recorded_ones(tmp_path: Path) -> None:
    """The signal's ``at`` is the event's own -- never the moment of the report.

    ``RunLedger.load`` rebuilds each event without its ``at``, so it would stamp
    every signal with "now"; this is the guard that keeps ``run.json`` read as
    plain JSON.
    """

    recorded = "2026-01-02T03:04:05+00:00"
    _ledger(tmp_path, [_event("command_result", "make", {"ok": False}, at=recorded)])
    report = explain_run_attribution(tmp_path, _SESSION)

    assert report.signals[0].at == recorded


def test_unclosed_ledger_is_a_host_signal(tmp_path: Path) -> None:
    _ledger(tmp_path, [_event("tool_call", "Read")], status="running")
    report = explain_run_attribution(tmp_path, _SESSION)

    assert _categories(report) == {"host"}
    assert report.ended_at is None


def test_closed_ledger_has_an_end_and_no_host_signal(tmp_path: Path) -> None:
    _ledger(tmp_path, [_event("tool_call", "Read")], status="done")
    report = explain_run_attribution(tmp_path, _SESSION)

    assert report.ended_at == "2026-09-07T13:00:00+00:00"
    assert "host" not in _categories(report)


# ----- collapsing, limits, ordering ---------------------------------------- #


def test_duplicate_failures_collapse_into_one_counted_signal(tmp_path: Path) -> None:
    _ledger(tmp_path, [_event("command_result", "pytest -q", {"ok": False}) for _ in range(30)])
    report = explain_run_attribution(tmp_path, _SESSION)

    assert len(report.signals) == 1
    assert report.signals[0].count == 30
    assert report.signals[0].evidence_path.endswith("#events[0]")
    assert dict(report.category_counts)["shell"] == 30


def test_limit_trims_display_but_counts_stay_true(tmp_path: Path) -> None:
    """Trimming must never shrink the totals, or truncation becomes invisible."""

    events = [_event("command_result", f"cmd-{index}", {"ok": False}) for index in range(9)]
    _ledger(tmp_path, events)
    report = explain_run_attribution(tmp_path, _SESSION, limit_per_category=3)

    assert len(report.signals) == 3
    assert dict(report.category_counts)["shell"] == 9
    assert "showing 3 of 9" in render_attribution(report)


def test_signals_are_sorted_deterministically(tmp_path: Path) -> None:
    _ledger(
        tmp_path,
        [
            _event("watchdog_alert", "loop"),
            _event("command_result", "b-cmd", {"ok": False}),
            _event("command_result", "a-cmd", {"ok": False}),
            _event("command_result", "a-cmd", {"ok": False}),
            _event("rubric_run", "blocked", {"status": "blocked"}),
        ],
    )
    report = explain_run_attribution(tmp_path, _SESSION)
    keys = [(signal.category, -signal.count, signal.summary) for signal in report.signals]

    assert keys == sorted(keys)
    assert [signal.category for signal in report.signals] == ["lemoncrow", "policy", "shell", "shell"]
    assert explain_run_attribution(tmp_path, _SESSION).to_dict() == report.to_dict()


# ----- cost reuse ---------------------------------------------------------- #


def test_cost_block_is_usage_explain_verbatim(tmp_path: Path) -> None:
    """One accounting path: the block is ``usage.explain_run``'s payload, unedited."""

    from lemoncrow.pro.capabilities.usage.explain import explain_run

    _ledger(tmp_path, [_event("command_result", "make", {"ok": False})])
    report = explain_run_attribution(tmp_path, _SESSION)

    assert report.cost == explain_run(tmp_path, _SESSION)
    assert report.cost["schema_version"] == 1
    assert "cost" not in report.unresolved


# ----- degradation --------------------------------------------------------- #


def test_corrupt_ledger_degrades_instead_of_raising(tmp_path: Path) -> None:
    run_file = _ledger(tmp_path, [])
    run_file.write_text("{not json", encoding="utf-8")

    report = explain_run_attribution(tmp_path, _SESSION)
    assert "run_ledger" in report.unresolved
    assert report.signals == ()


def test_hostile_event_shapes_degrade(tmp_path: Path) -> None:
    directory = session_dir(tmp_path, "claude", _SESSION)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(
        json.dumps({"session_id": _SESSION, "status": 7, "events": ["oops", {"payload": "nope"}, {"kind": ""}]}),
        encoding="utf-8",
    )

    report = explain_run_attribution(tmp_path, _SESSION)
    assert report.signals == ()
    assert render_attribution(report)


def test_unknown_run_id_raises_value_error(tmp_path: Path) -> None:
    _ledger(tmp_path, [])
    with pytest.raises(ValueError, match="no run found"):
        explain_run_attribution(tmp_path, "nope")


# ----- CLI ----------------------------------------------------------------- #


def test_explain_json_shape(tmp_path: Path) -> None:
    _ledger(tmp_path, [_event("command_result", "pytest -q", {"ok": False})])
    result = _invoke(tmp_path, _SESSION, "--json")
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    for key in ("schema_version", "session_id", "signals", "category_counts", "cost", "unresolved"):
        assert key in payload, key
    assert payload["schema_version"] == 1
    assert payload["signals"][0]["category"] == "shell"
    assert payload["category_counts"] == [["shell", 1]]


def test_explain_accepts_a_prefix(tmp_path: Path) -> None:
    _ledger(tmp_path, [_event("command_result", "pytest -q", {"ok": False})])
    result = _invoke(tmp_path, _SESSION[:8])
    assert result.exit_code == 0, result.output
    assert _SESSION in result.output


def test_explain_without_a_run_id_uses_the_latest_ledger(tmp_path: Path) -> None:
    _ledger(tmp_path, [_event("command_result", "pytest -q", {"ok": False})])
    result = _invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert _SESSION in result.output


def test_explain_with_no_store_is_a_clean_error(tmp_path: Path) -> None:
    result = _invoke(tmp_path)
    assert result.exit_code != 0
    assert "no run ledger found" in result.output


def test_explain_unknown_id_is_a_click_error(tmp_path: Path) -> None:
    _ledger(tmp_path, [])
    result = _invoke(tmp_path, "does-not-exist")
    assert result.exit_code != 0
    assert "no run found" in result.output
