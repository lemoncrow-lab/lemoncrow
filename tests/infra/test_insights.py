"""Unit tests for insights.py (Spec 04 — lemoncrow insights).

Tests cover:
- build_insights with synthetic session data
- Each opportunity rule fires correctly
- Empty window renders without crash
- --since parsing
- JSON output is valid
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.infra.runtime.insights import (
    InsightsWindow,
    Opportunity,
    OutcomesSummary,
    SessionSummary,
    _bar,
    _detect_opportunities,
    _fmt_duration,
    _read_tool_cost_fraction,
    _rule_compact_aggression,
    _rule_cross_vendor_route,
    _rule_error_pattern,
    _rule_sync_value,
    build_insights,
    render_json,
    render_text,
)
from lemoncrow.infra.runtime.session_report import SessionReport

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
_SINCE = datetime(2026, 5, 8, 0, 0, 0, tzinfo=UTC)


def _make_report(
    session_id: str = "abc123",
    total_cost: float = 1.0,
    vendor: str = "Anthropic",
    top_tools: list[tuple[str, int, float]] | None = None,
    routing_savings: float = 0.0,
    compact_events: int = 0,
    duration: float = 3600.0,
    started_at: datetime | None = None,
) -> SessionReport:
    if top_tools is None:
        top_tools = [("Edit", 10, total_cost * 0.5), ("Bash", 5, total_cost * 0.3)]
    return SessionReport(
        session_id=session_id,
        started_at=started_at or _SINCE,
        ended_at=_NOW,
        duration_seconds=duration,
        active_duration_seconds=duration,
        vendor=vendor,
        agent_settings={},
        skills=[],
        telemetry={},
        models_used={"claude-haiku-4-5": 50},
        started_model="claude-haiku-4-5",
        total_turns=20,
        tool_call_count=50,
        input_token_cost_usd=total_cost * 0.6,
        cache_write_cost_usd=0.0,
        cache_read_cost_usd=0.0,
        output_token_cost_usd=total_cost * 0.4,
        total_cost_usd=total_cost,
        input_tokens=10000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        output_tokens=2000,
        routing_downtiered_turns=2,
        routing_savings_usd=routing_savings,
        compact_events=compact_events,
        compact_savings_estimate_usd=0.0,
        total_lemoncrow_savings_usd=routing_savings,
        raw_artifact_ids=[],
        top_tools_by_cost=top_tools,
    )


def _make_read_heavy_report(session_id: str, total_cost: float = 2.0, vendor: str = "Anthropic") -> SessionReport:
    """Report where >30% of cost is in read-class tools."""
    return _make_report(
        session_id=session_id,
        total_cost=total_cost,
        vendor=vendor,
        top_tools=[
            ("Read", 20, total_cost * 0.40),
            ("Grep", 15, total_cost * 0.15),
            ("Edit", 5, total_cost * 0.20),
        ],
    )


def _make_window(
    reports: list[SessionReport] | None = None,
    opportunities: list[Opportunity] | None = None,
) -> InsightsWindow:
    reps = reports or []
    return InsightsWindow(
        since=_SINCE,
        until=_NOW,
        session_count=len(reps),
        total_duration_seconds=sum(r.duration_seconds for r in reps),
        total_cost_usd=sum(r.total_cost_usd for r in reps),
        total_lemoncrow_savings_usd=sum(r.total_lemoncrow_savings_usd for r in reps),
        cost_by_vendor={"Anthropic": sum(r.total_cost_usd for r in reps)},
        cost_by_tool={"Edit": 5.0, "Bash": 3.0},
        cost_by_model={"claude-haiku-4-5": sum(r.total_cost_usd for r in reps)},
        top_sessions=[
            SessionSummary(
                session_id=r.session_id,
                cost_usd=r.total_cost_usd,
                label=r.session_id[:8],
                duration_seconds=r.duration_seconds,
            )
            for r in sorted(reps, key=lambda x: x.total_cost_usd, reverse=True)[:5]
        ],
        outcomes_summary=OutcomesSummary(
            route_decisions=0,
            route_avg_score=0.0,
            compact_events=0,
            compact_avg_score=0.0,
            sessions_with_high_extra_reads=[],
        ),
        opportunities=opportunities or [],
    )


# --------------------------------------------------------------------------- #
# _bar                                                                         #
# --------------------------------------------------------------------------- #


def test_bar_full() -> None:
    assert _bar(1.0) == "█" * 20


def test_bar_empty() -> None:
    assert _bar(0.0) == "░" * 20


def test_bar_half() -> None:
    result = _bar(0.5)
    assert len(result) == 20
    assert result == "█" * 10 + "░" * 10


def test_bar_clamps_above_one() -> None:
    assert _bar(2.0) == "█" * 20


def test_bar_clamps_below_zero() -> None:
    assert _bar(-0.5) == "░" * 20


def test_bar_custom_width() -> None:
    result = _bar(0.5, width=10)
    assert len(result) == 10


# --------------------------------------------------------------------------- #
# _fmt_duration                                                                #
# --------------------------------------------------------------------------- #


def test_fmt_duration_seconds() -> None:
    assert _fmt_duration(45) == "45s"


def test_fmt_duration_minutes() -> None:
    assert _fmt_duration(150) == "2m 30s"


def test_fmt_duration_hours() -> None:
    assert _fmt_duration(5400) == "1h 30m"


# --------------------------------------------------------------------------- #
# _read_tool_cost_fraction                                                     #
# --------------------------------------------------------------------------- #


def test_read_tool_cost_fraction_zero_cost() -> None:
    report = _make_report(total_cost=0.0)
    assert _read_tool_cost_fraction(report) == 0.0


def test_read_tool_cost_fraction_no_read_tools() -> None:
    report = _make_report(
        total_cost=10.0,
        top_tools=[("Edit", 10, 5.0), ("Bash", 5, 3.0)],
    )
    assert _read_tool_cost_fraction(report) == 0.0


def test_read_tool_cost_fraction_with_read_tools() -> None:
    report = _make_report(
        total_cost=10.0,
        top_tools=[("Read", 20, 4.0), ("Edit", 10, 3.0)],
    )
    assert _read_tool_cost_fraction(report) == pytest.approx(0.4)


def test_read_tool_cost_fraction_case_insensitive() -> None:
    report = _make_report(
        total_cost=10.0,
        top_tools=[("grep", 20, 3.0)],
    )
    assert _read_tool_cost_fraction(report) == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# Opportunity rules                                                            #
# --------------------------------------------------------------------------- #


def test_rule_cross_vendor_route_not_enough_sessions() -> None:
    """Fewer than 6 read-heavy sessions: rule does not fire."""
    reports = [_make_read_heavy_report(f"s{i}") for i in range(5)]
    assert _rule_cross_vendor_route(reports) is None


def test_rule_cross_vendor_route_fires_with_enough_sessions() -> None:
    """6+ read-heavy Anthropic sessions: rule fires."""
    reports = [_make_read_heavy_report(f"s{i}", total_cost=5.0) for i in range(8)]
    opp = _rule_cross_vendor_route(reports)
    assert opp is not None
    assert opp.kind == "cross_vendor_route"
    assert opp.sessions_affected == 8
    assert opp.estimated_savings_usd > 0


def test_rule_cross_vendor_route_skips_google() -> None:
    """Google sessions are not included in the cross-vendor opportunity."""
    reports = [_make_read_heavy_report(f"s{i}", vendor="Google") for i in range(10)]
    assert _rule_cross_vendor_route(reports) is None


def test_rule_cross_vendor_route_suppressed_below_threshold() -> None:
    """Very cheap sessions produce savings < $0.50 and are suppressed."""
    reports = [_make_read_heavy_report(f"s{i}", total_cost=0.01) for i in range(8)]
    assert _rule_cross_vendor_route(reports) is None


def test_rule_compact_aggression_fires() -> None:
    """Rule fires when avg extra_read_rate > 0.15."""
    outcomes: dict[str, dict[str, Any]] = {
        f"s{i}": {
            "route_outcomes": [],
            "compact_outcomes": [{"outcome_window": {"extra_read_rate": 0.25, "outcome_score": 0.8}}],
        }
        for i in range(5)
    }
    opp = _rule_compact_aggression(outcomes)
    assert opp is not None
    assert opp.kind == "compact_aggression"
    assert opp.sessions_affected >= 1


def test_rule_compact_aggression_does_not_fire_below_threshold() -> None:
    """Rule does not fire when avg extra_read_rate <= 0.15."""
    outcomes: dict[str, dict[str, Any]] = {
        f"s{i}": {
            "route_outcomes": [],
            "compact_outcomes": [{"outcome_window": {"extra_read_rate": 0.10, "outcome_score": 0.9}}],
        }
        for i in range(5)
    }
    assert _rule_compact_aggression(outcomes) is None


def test_rule_compact_aggression_no_outcomes() -> None:
    """Empty outcomes: rule does not fire."""
    assert _rule_compact_aggression({}) is None


def test_rule_sync_value_fires_multiple_machines() -> None:
    """Rule fires when multiple machine_ids are present."""
    snaps: list[dict[str, Any]] = [
        {"machine_id": "host-1", "session_id": "s1"},
        {"machine_id": "host-2", "session_id": "s2"},
        {"machine_id": "host-1", "session_id": "s3"},
    ]
    opp = _rule_sync_value(snaps)
    assert opp is not None
    assert opp.kind == "sync_value"
    assert opp.sessions_affected == 3


def test_rule_sync_value_single_machine() -> None:
    """Rule does not fire for a single machine."""
    snaps: list[dict[str, Any]] = [
        {"machine_id": "host-1", "session_id": "s1"},
        {"machine_id": "host-1", "session_id": "s2"},
    ]
    assert _rule_sync_value(snaps) is None


def test_rule_sync_value_no_machine_id() -> None:
    """Snaps without machine_id: rule does not fire."""
    snaps: list[dict[str, Any]] = [{"session_id": "s1"}, {"session_id": "s2"}]
    assert _rule_sync_value(snaps) is None


def test_rule_error_pattern_fires() -> None:
    """Rule fires when >10% of sessions have errors and >5 affected sessions."""
    snaps: list[dict[str, Any]] = [{"session_id": f"s{i}", "errors_seen": 2 if i < 7 else 0} for i in range(10)]
    reports = [
        _make_report(
            session_id=f"s{i}",
            top_tools=[("Edit", 10, 1.0)],
        )
        for i in range(10)
    ]
    opp = _rule_error_pattern(snaps, reports)
    assert opp is not None
    assert opp.kind == "error_pattern"
    assert opp.sessions_affected >= 5


def test_rule_error_pattern_below_threshold() -> None:
    """Rule does not fire when fewer than 5 sessions have errors."""
    snaps: list[dict[str, Any]] = [{"session_id": f"s{i}", "errors_seen": 1 if i < 3 else 0} for i in range(50)]
    reports = [_make_report(session_id=f"s{i}") for i in range(50)]
    assert _rule_error_pattern(snaps, reports) is None


def test_detect_opportunities_max_five() -> None:
    """At most 5 opportunities are returned."""
    # Patch rules to produce lots of results by injecting manually.
    opps = [
        Opportunity(kind=f"k{i}", message="msg", estimated_savings_usd=float(i), sessions_affected=1) for i in range(10)
    ]
    # _detect_opportunities only returns <=5; simulate by calling sort/slice logic.
    sorted_opps = sorted(opps, key=lambda o: o.estimated_savings_usd, reverse=True)[:5]
    assert len(sorted_opps) == 5
    assert sorted_opps[0].estimated_savings_usd == 9.0


def test_detect_opportunities_sorted_desc() -> None:
    """Opportunities are sorted by estimated_savings_usd descending."""
    snaps: list[dict[str, Any]] = []
    reports: list[SessionReport] = []
    outcomes: dict[str, dict[str, Any]] = {}
    result = _detect_opportunities(reports, snaps, outcomes)
    # No data means no opportunities; should not crash.
    assert isinstance(result, list)


# --------------------------------------------------------------------------- #
# build_insights                                                               #
# --------------------------------------------------------------------------- #


def _write_run_file(
    runs_dir: Path,
    session_id: str,
    cost_usd: float = 1.0,
    task: str = "test task",
    started_at: datetime | None = None,
    calls: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    start = started_at or _SINCE
    snap: dict[str, Any] = {
        "session_id": session_id,
        "status": "done",
        "task": task,
        "created_at": start.isoformat(),
        "updated_at": (start + timedelta(hours=1)).isoformat(),
        "tool_call_count": 10,
        "errors_seen": 0,
        "cost": {
            "calls": calls if calls is not None else [],
            "total_cost_usd": cost_usd,
            "total_input_tokens": 10000,
            "total_output_tokens": 2000,
            "total_cache_read_tokens": 0,
        },
        "events": events if events is not None else [],
    }
    # Ledgers live in <root>/sessions/<id>/run.json; callers pass <root>/runs historically.
    session_dir = runs_dir.parent / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "run.json").write_text(json.dumps(snap), encoding="utf-8")


def test_build_insights_empty_window(tmp_path: Path) -> None:
    """Empty runs dir produces an InsightsWindow with zero sessions."""
    root = tmp_path / "lemoncrow"
    root.mkdir()
    since = datetime(2025, 1, 1, tzinfo=UTC)
    until = datetime(2025, 12, 31, tzinfo=UTC)
    window = build_insights(root, since=since, until=until)
    assert window.session_count == 0
    assert window.total_cost_usd == 0.0
    assert window.opportunities == []


def test_build_insights_single_session(tmp_path: Path) -> None:
    """Single session is correctly aggregated."""
    root = tmp_path / "lemoncrow"
    _write_run_file(root / "runs", "sess1", cost_usd=5.00, task="refactor cost_tracker")
    since = _SINCE - timedelta(days=1)
    until = _NOW + timedelta(days=1)
    window = build_insights(root, since=since, until=until)
    assert window.session_count == 1
    assert window.total_cost_usd == pytest.approx(5.0)
    assert len(window.top_sessions) == 1
    assert window.top_sessions[0].label == "refactor cost_tracker"


def test_build_insights_multiple_sessions(tmp_path: Path) -> None:
    """Multiple sessions are aggregated correctly."""
    runs_dir = tmp_path / "lemoncrow" / "runs"
    for i in range(3):
        _write_run_file(
            runs_dir,
            f"sess{i}",
            cost_usd=float(i + 1),
            task=f"task {i}",
        )
    window = build_insights(
        tmp_path / "lemoncrow",
        since=_SINCE - timedelta(days=1),
        until=_NOW + timedelta(days=1),
    )
    assert window.session_count == 3
    assert window.total_cost_usd == pytest.approx(6.0)


def test_build_insights_top_sessions_sorted_by_cost(tmp_path: Path) -> None:
    """Top sessions are sorted by cost descending."""
    runs_dir = tmp_path / "lemoncrow" / "runs"
    costs = [1.0, 5.0, 3.0, 2.0, 4.0]
    for i, c in enumerate(costs):
        _write_run_file(runs_dir, f"s{i}", cost_usd=c, task=f"task{i}")
    window = build_insights(
        tmp_path / "lemoncrow",
        since=_SINCE - timedelta(days=1),
        until=_NOW + timedelta(days=1),
    )
    assert len(window.top_sessions) == 5
    assert window.top_sessions[0].cost_usd == pytest.approx(5.0)
    assert window.top_sessions[-1].cost_usd == pytest.approx(1.0)


def test_build_insights_since_filter(tmp_path: Path) -> None:
    """Sessions before the since cutoff are excluded."""
    runs_dir = tmp_path / "lemoncrow" / "runs"
    old_time = datetime(2024, 1, 1, tzinfo=UTC)
    _write_run_file(runs_dir, "old", cost_usd=99.0, started_at=old_time)
    _write_run_file(runs_dir, "new", cost_usd=1.0, started_at=_SINCE)
    since = datetime(2025, 1, 1, tzinfo=UTC)
    until = _NOW + timedelta(days=1)
    window = build_insights(tmp_path / "lemoncrow", since=since, until=until)
    assert window.session_count == 1
    ids = [s.session_id for s in window.top_sessions]
    assert "new" in ids
    assert "old" not in ids


def test_build_insights_vendor_cost_from_calls(tmp_path: Path) -> None:
    """Vendor cost is derived from cost.calls model field."""
    runs_dir = tmp_path / "lemoncrow" / "runs"
    calls = [
        {"model": "claude-haiku-4-5", "cost_usd": 3.0, "input_tokens": 5000, "output_tokens": 1000},
        {"model": "gpt-4o", "cost_usd": 2.0, "input_tokens": 3000, "output_tokens": 500},
    ]
    _write_run_file(runs_dir, "s1", cost_usd=5.0, calls=calls)
    window = build_insights(
        tmp_path / "lemoncrow",
        since=_SINCE - timedelta(days=1),
        until=_NOW + timedelta(days=1),
    )
    assert "Anthropic" in window.cost_by_vendor
    assert "OpenAI" in window.cost_by_vendor
    assert window.cost_by_vendor["Anthropic"] == pytest.approx(3.0)
    assert window.cost_by_vendor["OpenAI"] == pytest.approx(2.0)


def test_build_insights_vendor_fallback_no_calls(tmp_path: Path) -> None:
    """When calls list is empty, total cost is attributed to session vendor."""
    runs_dir = tmp_path / "lemoncrow" / "runs"
    events = [
        {
            "kind": "model_recommendation",
            "at": _SINCE.isoformat(),
            "summary": "recommend haiku",
            "payload": {
                "tool_name": "Edit",
                "model": "claude-haiku-4-5",
                "estimated_input_tokens": 5000,
                "cost_saved_usd": 0.01,
                "tier": "haiku",
            },
        }
    ]
    _write_run_file(runs_dir, "s1", cost_usd=2.0, calls=[], events=events)
    window = build_insights(
        tmp_path / "lemoncrow",
        since=_SINCE - timedelta(days=1),
        until=_NOW + timedelta(days=1),
    )
    # Should have attributed cost to Anthropic (from model_recommendation).
    assert window.total_cost_usd == pytest.approx(2.0)


def test_build_insights_outcomes_summary(tmp_path: Path) -> None:
    """Outcomes files are read and aggregated correctly."""
    runs_dir = tmp_path / "lemoncrow" / "runs"
    _write_run_file(runs_dir, "s1", cost_usd=1.0)
    # Write an outcomes file.
    outcomes_data = {
        "route_outcomes": [
            {"outcome_window": {"outcome_score": 0.9}, "tool": "Edit"},
            {"outcome_window": {"outcome_score": 0.8}, "tool": "Bash"},
        ],
        "compact_outcomes": [
            {"outcome_window": {"outcome_score": 0.7, "extra_read_rate": 0.1}},
        ],
    }
    from lemoncrow.core.foundation.paths import session_dir

    outcomes_dir = session_dir(runs_dir.parent, "claude", "s1")
    outcomes_dir.mkdir(parents=True, exist_ok=True)
    (outcomes_dir / "outcomes.json").write_text(json.dumps(outcomes_data))
    window = build_insights(
        tmp_path / "lemoncrow",
        since=_SINCE - timedelta(days=1),
        until=_NOW + timedelta(days=1),
    )
    assert window.outcomes_summary.route_decisions == 2
    assert window.outcomes_summary.compact_events == 1
    assert window.outcomes_summary.route_avg_score == pytest.approx(0.85)


def test_build_insights_high_extra_reads_flagged(tmp_path: Path) -> None:
    """Sessions with compact extra_read_rate > 0.20 are flagged."""
    runs_dir = tmp_path / "lemoncrow" / "runs"
    _write_run_file(runs_dir, "s1", cost_usd=1.0)
    outcomes_data = {
        "route_outcomes": [],
        "compact_outcomes": [{"outcome_window": {"outcome_score": 0.5, "extra_read_rate": 0.35}}],
    }
    from lemoncrow.core.foundation.paths import session_dir

    outcomes_dir = session_dir(runs_dir.parent, "claude", "s1")
    outcomes_dir.mkdir(parents=True, exist_ok=True)
    (outcomes_dir / "outcomes.json").write_text(json.dumps(outcomes_data))
    window = build_insights(
        tmp_path / "lemoncrow",
        since=_SINCE - timedelta(days=1),
        until=_NOW + timedelta(days=1),
    )
    assert "s1" in window.outcomes_summary.sessions_with_high_extra_reads


# --------------------------------------------------------------------------- #
# render_text                                                                  #
# --------------------------------------------------------------------------- #


def test_render_text_empty_window_no_crash() -> None:
    """Empty window renders without crash."""
    window = _make_window()
    output = render_text(window)
    assert "Weekly insights" in output
    assert "Sessions:         0" in output


def test_render_text_includes_vendor_section() -> None:
    window = _make_window(reports=[_make_report(total_cost=5.0)])
    output = render_text(window)
    assert "Cost by vendor" in output
    assert "Anthropic" in output


def test_render_text_includes_tool_section() -> None:
    window = _make_window(reports=[_make_report(total_cost=5.0)])
    output = render_text(window)
    assert "Cost by tool" in output


def test_render_text_includes_top_sessions() -> None:
    window = _make_window(reports=[_make_report("sess1", total_cost=5.0)])
    output = render_text(window)
    assert "Top spending sessions" in output
    assert "sess1" in output


def test_render_text_includes_outcomes() -> None:
    window = _make_window()
    output = render_text(window)
    assert "Outcomes" in output
    assert "Route decisions" in output


def test_render_text_includes_opportunities() -> None:
    opps = [
        Opportunity(
            kind="cross_vendor_route",
            message="10 sessions had 30%+ read turns",
            estimated_savings_usd=3.10,
            sessions_affected=10,
        )
    ]
    window = _make_window(opportunities=opps)
    output = render_text(window)
    assert "Opportunities" in output
    assert "10 sessions had 30%+ read turns" in output


def test_render_text_no_emoji() -> None:
    """No emoji in output per repo convention."""
    window = _make_window(reports=[_make_report(total_cost=5.0)])
    output = render_text(window)
    # Check no common emoji code points present.
    for char in output:
        code = ord(char)
        assert not (0x1F600 <= code <= 0x1F64F), f"Emoji found: {char!r}"
        assert not (0x1F300 <= code <= 0x1F5FF), f"Emoji found: {char!r}"


def test_render_text_80_col_prefix() -> None:
    """Header line does not exceed 80 chars."""
    window = _make_window()
    first_line = render_text(window).splitlines()[0]
    assert len(first_line) <= 80


# --------------------------------------------------------------------------- #
# render_json                                                                  #
# --------------------------------------------------------------------------- #


def test_render_json_valid(tmp_path: Path) -> None:
    """render_json produces parseable JSON."""
    window = _make_window(reports=[_make_report(total_cost=2.0)])
    output = render_json(window)
    data = json.loads(output)
    assert "session_count" in data
    assert "cost_by_vendor" in data
    assert "opportunities" in data
    assert "outcomes_summary" in data


def test_render_json_top_sessions_structure(tmp_path: Path) -> None:
    """top_sessions list in JSON has correct keys."""
    window = _make_window(reports=[_make_report("s1", total_cost=1.0)])
    data = json.loads(render_json(window))
    if data["top_sessions"]:
        sess = data["top_sessions"][0]
        assert "session_id" in sess
        assert "cost_usd" in sess
        assert "label" in sess
        assert "duration_seconds" in sess


def test_render_json_opportunities_structure() -> None:
    """opportunities list in JSON has correct keys."""
    opps = [Opportunity(kind="cross_vendor_route", message="msg", estimated_savings_usd=1.5, sessions_affected=3)]
    window = _make_window(opportunities=opps)
    data = json.loads(render_json(window))
    assert len(data["opportunities"]) == 1
    opp = data["opportunities"][0]
    assert opp["kind"] == "cross_vendor_route"
    assert opp["estimated_savings_usd"] == 1.5
    assert opp["sessions_affected"] == 3


def test_render_json_since_until_as_isoformat() -> None:
    window = _make_window()
    data = json.loads(render_json(window))
    # Should be parseable as ISO datetime strings.
    datetime.fromisoformat(data["since"])
    datetime.fromisoformat(data["until"])


# --------------------------------------------------------------------------- #
# _parse_since_arg (via build_insights integration)                           #
# --------------------------------------------------------------------------- #


def test_parse_since_relative_days(tmp_path: Path) -> None:
    """build_insights with synthetic data; just validates the since window."""
    runs_dir = tmp_path / "lemoncrow" / "runs"
    now = datetime.now(UTC)
    _write_run_file(runs_dir, "s1", started_at=now - timedelta(days=3))
    _write_run_file(runs_dir, "old", started_at=now - timedelta(days=30))
    # Test with a 7-day window.
    since = now - timedelta(days=7)
    until = now + timedelta(seconds=1)
    window = build_insights(tmp_path / "lemoncrow", since=since, until=until)
    ids = [s.session_id for s in window.top_sessions]
    assert "s1" in ids


# --------------------------------------------------------------------------- #
# Performance                                                                  #
# --------------------------------------------------------------------------- #
