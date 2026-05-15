"""Unit tests for outcome_capture (Spec 01 — Feedback Loop Foundation)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atelier.infra.runtime.outcome_capture import (
    FileStateWriter,
    _compact_score,
    _pending_compact,
    _pending_route,
    _route_score,
    advance,
    close_session,
    get_outcomes,
    load_outcomes_from_state,
    schedule_compact,
    schedule_route,
    summarise_outcomes,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_session_id() -> str:
    import uuid

    return uuid.uuid4().hex


def _clear_session(session_id: str) -> None:
    _pending_route.pop(session_id, None)
    _pending_compact.pop(session_id, None)


def _default_scored_state() -> dict[str, Any]:
    return {"turn_number": 1, "prior_errors": 0, "session_phase": "exploration"}


# --------------------------------------------------------------------------- #
# Score formula tests                                                          #
# --------------------------------------------------------------------------- #


class TestRouteScore:
    def test_perfect_score(self) -> None:
        assert _route_score(0, 0, 0) == 1.0

    def test_retry_penalty(self) -> None:
        score = _route_score(retries_same_tool=1, model_errors_in_window=0, extra_reads=0)
        assert abs(score - 0.6) < 1e-6

    def test_model_errors_penalty(self) -> None:
        score = _route_score(retries_same_tool=0, model_errors_in_window=2, extra_reads=0)
        assert abs(score - 0.7) < 1e-6

    def test_extra_reads_penalty(self) -> None:
        score = _route_score(retries_same_tool=0, model_errors_in_window=0, extra_reads=5)
        assert abs(score - 0.8) < 1e-6

    def test_all_penalties_combined(self) -> None:
        score = _route_score(retries_same_tool=1, model_errors_in_window=2, extra_reads=5)
        # 1.0 - 0.4 - 0.3 - 0.2 = 0.1
        assert abs(score - 0.1) < 1e-6

    def test_clamped_at_zero(self) -> None:
        # Max penalty: 0.4 + 0.3*min(1,10/2)=0.3 + 0.2*min(1,100/5)=0.2 = 0.9
        # Minimum achievable score with this formula is 1.0 - 0.9 = 0.1
        score = _route_score(retries_same_tool=1, model_errors_in_window=10, extra_reads=100)
        assert abs(score - 0.1) < 1e-6

    def test_clamped_at_one(self) -> None:
        assert _route_score(0, 0, 0) == 1.0


class TestCompactScore:
    def test_perfect_score(self) -> None:
        assert _compact_score(0.0, 0.0, 0) == 1.0

    def test_positive_error_drift_penalty(self) -> None:
        score = _compact_score(0.5, 0.0, 0)
        assert abs(score - 0.0) < 1e-6

    def test_negative_error_drift_no_penalty(self) -> None:
        score = _compact_score(-0.5, 0.0, 0)
        assert score == 1.0

    def test_extra_read_rate_penalty(self) -> None:
        score = _compact_score(0.0, 1.0, 0)
        assert abs(score - 0.5) < 1e-6

    def test_must_keep_violation_penalty(self) -> None:
        score = _compact_score(0.0, 0.0, 1)
        assert score == 0.0

    def test_clamped_at_zero(self) -> None:
        score = _compact_score(10.0, 10.0, 5)
        assert score == 0.0


# --------------------------------------------------------------------------- #
# schedule_route tests                                                         #
# --------------------------------------------------------------------------- #


class TestScheduleRoute:
    def test_schedule_creates_pending_entry(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        decision_id = schedule_route(
            session_id=sid,
            tool="Edit",
            recommended_tier="cheap",
            recommended_model="claude-haiku-4-5",
            recommendation_followed=True,
            scored_state=_default_scored_state(),
        )
        outcomes = get_outcomes(sid)
        assert len(outcomes["route_outcomes"]) == 1
        entry = outcomes["route_outcomes"][0]
        assert entry["decision_id"] == decision_id
        assert entry["tool"] == "Edit"
        assert entry["recommended_tier"] == "cheap"
        assert entry["outcome_window"] is None  # still pending
        _clear_session(sid)

    def test_schedule_returns_unique_ids(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        ids = [
            schedule_route(
                session_id=sid,
                tool="Edit",
                recommended_tier="cheap",
                recommended_model="claude-haiku-4-5",
                recommendation_followed=True,
                scored_state=_default_scored_state(),
            )
            for _ in range(5)
        ]
        assert len(set(ids)) == 5
        _clear_session(sid)


# --------------------------------------------------------------------------- #
# advance / window-fill tests                                                  #
# --------------------------------------------------------------------------- #


class TestAdvanceRoute:
    def test_fills_window_after_five_turns(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        schedule_route(
            session_id=sid,
            tool="Edit",
            recommended_tier="cheap",
            recommended_model="claude-haiku-4-5",
            recommendation_followed=True,
            scored_state=_default_scored_state(),
        )
        for _ in range(5):
            advance(sid, tool_name="Edit")
        outcomes = get_outcomes(sid)
        window = outcomes["route_outcomes"][0]["outcome_window"]
        assert window is not None
        assert window["turns_observed"] == 5
        assert "outcome_score" in window
        assert 0.0 <= window["outcome_score"] <= 1.0
        _clear_session(sid)

    def test_does_not_fill_before_five_turns(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        schedule_route(
            session_id=sid,
            tool="Edit",
            recommended_tier="cheap",
            recommended_model="claude-haiku-4-5",
            recommendation_followed=True,
            scored_state=_default_scored_state(),
        )
        for _ in range(4):
            advance(sid, tool_name="Edit")
        outcomes = get_outcomes(sid)
        assert outcomes["route_outcomes"][0]["outcome_window"] is None
        _clear_session(sid)

    def test_error_increments_model_errors(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        schedule_route(
            session_id=sid,
            tool="Edit",
            recommended_tier="cheap",
            recommended_model="claude-haiku-4-5",
            recommendation_followed=True,
            scored_state=_default_scored_state(),
        )
        for _ in range(4):
            advance(sid, tool_name="Edit")
        advance(sid, tool_name="Edit", is_error=True)
        window = get_outcomes(sid)["route_outcomes"][0]["outcome_window"]
        assert window is not None
        assert window["model_errors_in_window"] == 1
        _clear_session(sid)

    def test_session_ending_early_fills_window(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        schedule_route(
            session_id=sid,
            tool="Edit",
            recommended_tier="cheap",
            recommended_model="claude-haiku-4-5",
            recommendation_followed=True,
            scored_state=_default_scored_state(),
        )
        advance(sid, tool_name="Edit")
        advance(sid, tool_name="Edit")
        close_session(sid)
        window = get_outcomes(sid)["route_outcomes"][0]["outcome_window"]
        assert window is not None
        assert window["turns_observed"] == 2  # less than 5 — still valid
        _clear_session(sid)

    def test_multiple_overlapping_outcomes(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        # Schedule two route outcomes at different times
        schedule_route(
            session_id=sid,
            tool="Edit",
            recommended_tier="cheap",
            recommended_model="claude-haiku-4-5",
            recommendation_followed=True,
            scored_state=_default_scored_state(),
        )
        advance(sid, tool_name="Read")
        advance(sid, tool_name="Read")
        advance(sid, tool_name="Read")
        # Second decision mid-stream
        schedule_route(
            session_id=sid,
            tool="Bash",
            recommended_tier="medium",
            recommended_model="claude-sonnet-4-5",
            recommendation_followed=False,
            scored_state={**_default_scored_state(), "turn_number": 4},
        )
        advance(sid, tool_name="Read")
        advance(sid, tool_name="Read")
        # First should be filled (5 turns), second has only 2 turns
        outcomes = get_outcomes(sid)
        assert outcomes["route_outcomes"][0]["outcome_window"] is not None
        assert outcomes["route_outcomes"][1]["outcome_window"] is None
        _clear_session(sid)


# --------------------------------------------------------------------------- #
# Compact advance tests                                                        #
# --------------------------------------------------------------------------- #


class TestAdvanceCompact:
    def test_fills_compact_window_after_ten_turns(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        schedule_compact(
            session_id=sid,
            trigger="utilisation_threshold",
            tokens_before=180_000,
            tokens_after=95_000,
            must_keep_keywords=["migration"],
        )
        for _ in range(10):
            advance(sid, tool_name="Edit")
        outcomes = get_outcomes(sid)
        window = outcomes["compact_outcomes"][0]["outcome_window"]
        assert window is not None
        assert window["turns_observed"] == 10
        assert 0.0 <= window["outcome_score"] <= 1.0
        _clear_session(sid)

    def test_compact_early_session_end(self) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        schedule_compact(
            session_id=sid,
            trigger="utilisation_threshold",
            tokens_before=180_000,
            tokens_after=95_000,
            must_keep_keywords=[],
        )
        for _ in range(3):
            advance(sid, tool_name="Edit")
        close_session(sid)
        window = get_outcomes(sid)["compact_outcomes"][0]["outcome_window"]
        assert window is not None
        assert window["turns_observed"] == 3
        _clear_session(sid)


# --------------------------------------------------------------------------- #
# Empty session                                                                #
# --------------------------------------------------------------------------- #


class TestEmptySession:
    def test_advance_on_empty_session_is_noop(self) -> None:
        sid = _make_session_id()
        advance(sid, tool_name="Edit")
        outcomes = get_outcomes(sid)
        assert outcomes["route_outcomes"] == []
        assert outcomes["compact_outcomes"] == []

    def test_close_session_on_empty_is_noop(self) -> None:
        sid = _make_session_id()
        close_session(sid)
        outcomes = get_outcomes(sid)
        assert outcomes["route_outcomes"] == []
        assert outcomes["compact_outcomes"] == []


# --------------------------------------------------------------------------- #
# FileStateWriter tests                                                        #
# --------------------------------------------------------------------------- #


class TestFileStateWriter:
    def test_writes_and_reads_outcomes(self, tmp_path: Path) -> None:
        sid = _make_session_id()
        _clear_session(sid)
        writer = FileStateWriter(tmp_path / f"{sid}_outcomes.json")
        schedule_route(
            session_id=sid,
            tool="Edit",
            recommended_tier="cheap",
            recommended_model="claude-haiku-4-5",
            recommendation_followed=True,
            scored_state=_default_scored_state(),
            writer=writer,
        )
        assert (tmp_path / f"{sid}_outcomes.json").exists()
        for _ in range(5):
            advance(sid, tool_name="Edit", writer=writer)
        loaded = load_outcomes_from_state(tmp_path / f"{sid}_outcomes.json")
        assert len(loaded["route_outcomes"]) == 1
        assert loaded["route_outcomes"][0]["outcome_window"] is not None
        _clear_session(sid)

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        data = load_outcomes_from_state(tmp_path / "nonexistent_outcomes.json")
        assert data == {"route_outcomes": [], "compact_outcomes": []}

    def test_writer_merges_with_existing_state(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"existing_key": "value"}), encoding="utf-8")
        writer = FileStateWriter(path)
        writer.write({"new_key": 42})
        content = json.loads(path.read_text("utf-8"))
        assert content["existing_key"] == "value"
        assert content["new_key"] == 42


# --------------------------------------------------------------------------- #
# summarise_outcomes tests                                                     #
# --------------------------------------------------------------------------- #


class TestSummariseOutcomes:
    def test_empty_returns_empty_list(self) -> None:
        assert summarise_outcomes({"route_outcomes": [], "compact_outcomes": []}) == []

    def test_pending_entries_excluded(self) -> None:
        outcomes: dict[str, list[dict[str, Any]]] = {
            "route_outcomes": [
                {
                    "kind": "route",
                    "tool": "Edit",
                    "outcome_window": None,
                }
            ],
            "compact_outcomes": [],
        }
        assert summarise_outcomes(outcomes) == []

    def test_aggregates_by_kind_and_tool(self) -> None:
        outcomes: dict[str, list[dict[str, Any]]] = {
            "route_outcomes": [
                {"kind": "route", "tool": "Edit", "outcome_window": {"outcome_score": 0.8}},
                {"kind": "route", "tool": "Edit", "outcome_window": {"outcome_score": 0.6}},
                {"kind": "route", "tool": "Bash", "outcome_window": {"outcome_score": 1.0}},
            ],
            "compact_outcomes": [
                {
                    "kind": "compact",
                    "trigger": "utilisation_threshold",
                    "outcome_window": {"outcome_score": 0.9},
                }
            ],
        }
        result = summarise_outcomes(outcomes)
        assert len(result) == 3
        edit_row = next(r for r in result if r["kind"] == "route" and r["tool"] == "Edit")
        assert edit_row["count"] == 2
        assert abs(edit_row["avg_outcome_score"] - 0.7) < 1e-4
        bash_row = next(r for r in result if r["kind"] == "route" and r["tool"] == "Bash")
        assert bash_row["count"] == 1
        assert bash_row["avg_outcome_score"] == 1.0
        compact_row = next(r for r in result if r["kind"] == "compact")
        assert compact_row["avg_outcome_score"] == 0.9
