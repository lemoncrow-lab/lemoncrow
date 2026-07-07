"""Tests for the learned routing-confidence layer (T9).

Fully synthetic and headless: no training, no GPU, no network.  Outcome tables
are built from in-memory rows (or a temp SQLite file using the persisted
``route_decision`` / ``verification_envelope`` schema), and the router is driven
through its public ``score`` API.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atelier.core.capabilities.model_routing.router import ModelRouter
from atelier.core.capabilities.model_routing.success_predictor import (
    DEFAULT_MIN_SAMPLES,
    DEFAULT_THRESHOLD,
    LEARNED_ROUTING_ENV_VAR,
    OutcomeRow,
    RouteFeatures,
    SuccessTable,
    build_success_table,
    features_from_state,
    learned_routing_enabled,
    learned_routing_threshold,
    load_outcome_rows,
    outcome_row_from_persisted,
    p_weak_succeeds,
)
from atelier.infra.storage.migrations import read_migration

# --------------------------------------------------------------------------- #
# Helpers: synthetic outcome rows / tables                                     #
# --------------------------------------------------------------------------- #

# The step-down scenario used throughout: an `edit` + "fix a typo" turn with
# trivial complexity signals.  Heuristic tier routing baselines this `medium`
# and (absent the learned gate) silently steps it down to `cheap`.
_STEP_DOWN_TOOL = "edit"
_STEP_DOWN_TEXT = "fix a typo"
_STEP_DOWN_STATE = {"prior_errors": 0, "refs": [], "changed_files": ["only.py"], "task_size_chars": 20}


def _features_for_step_down() -> RouteFeatures:
    return features_from_state(_STEP_DOWN_TOOL, _STEP_DOWN_TEXT, _STEP_DOWN_STATE)


def _table_with(features: RouteFeatures, *, successes: int, total: int) -> SuccessTable:
    rows = [OutcomeRow(features=features, success=True) for _ in range(successes)]
    rows += [OutcomeRow(features=features, success=False) for _ in range(total - successes)]
    return build_success_table(rows)


# --------------------------------------------------------------------------- #
# Flag + threshold convention                                                  #
# --------------------------------------------------------------------------- #


def test_learned_routing_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    assert learned_routing_enabled({}) is False
    assert learned_routing_enabled(None) is False


def test_learned_routing_opt_in_via_state_or_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    assert learned_routing_enabled({"learned_routing": True}) is True
    monkeypatch.setenv(LEARNED_ROUTING_ENV_VAR, "1")
    assert learned_routing_enabled({}) is True


def test_threshold_default_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_LEARNED_ROUTING_THRESHOLD", raising=False)
    assert learned_routing_threshold({}) == DEFAULT_THRESHOLD
    # State override wins.
    assert learned_routing_threshold({"learned_routing_threshold": 0.5}) == 0.5
    # Env override applies when no state override.
    monkeypatch.setenv("ATELIER_LEARNED_ROUTING_THRESHOLD", "0.75")
    assert learned_routing_threshold({}) == 0.75
    # Out-of-range / unparseable falls back to default.
    assert learned_routing_threshold({"learned_routing_threshold": 1.5}) == 0.75
    monkeypatch.setenv("ATELIER_LEARNED_ROUTING_THRESHOLD", "nope")
    assert learned_routing_threshold({}) == DEFAULT_THRESHOLD


# --------------------------------------------------------------------------- #
# Calibrated probabilities from a synthetic outcome table                      #
# --------------------------------------------------------------------------- #


def test_calibrated_probability_from_synthetic_table() -> None:
    feats = _features_for_step_down()
    table = _table_with(feats, successes=18, total=20)
    p = p_weak_succeeds(feats, table=table, min_samples=20)
    assert p == pytest.approx(0.9)


def test_probabilities_track_bucket_frequencies() -> None:
    feats = _features_for_step_down()
    assert p_weak_succeeds(feats, table=_table_with(feats, successes=20, total=20), min_samples=20) == 1.0
    assert p_weak_succeeds(feats, table=_table_with(feats, successes=10, total=20), min_samples=20) == 0.5
    assert p_weak_succeeds(feats, table=_table_with(feats, successes=0, total=20), min_samples=20) == 0.0


def test_buckets_are_independent() -> None:
    good = features_from_state("edit", "fix a typo", {"prior_errors": 0, "changed_files": ["a.py"]})
    risky = features_from_state("edit", "fix a typo", {"prior_errors": 5, "changed_files": ["a.py"]})
    assert good.key() != risky.key()
    rows = [OutcomeRow(good, True) for _ in range(20)] + [OutcomeRow(risky, False) for _ in range(20)]
    table = build_success_table(rows)
    assert p_weak_succeeds(good, table=table, min_samples=20) == 1.0
    assert p_weak_succeeds(risky, table=table, min_samples=20) == 0.0


# --------------------------------------------------------------------------- #
# No-data -> None -> heuristic fallback                                         #
# --------------------------------------------------------------------------- #


def test_no_table_returns_none() -> None:
    assert p_weak_succeeds(_features_for_step_down(), table=None, min_samples=20) is None


def test_empty_table_returns_none() -> None:
    assert p_weak_succeeds(_features_for_step_down(), table=build_success_table([]), min_samples=20) is None


def test_min_sample_guard_returns_none_below_threshold() -> None:
    feats = _features_for_step_down()
    # 19 rows < default 20-row guard -> uncertain.
    table = _table_with(feats, successes=19, total=19)
    assert p_weak_succeeds(feats, table=table, min_samples=20) is None
    # Exactly at the guard -> a probability is returned.
    table_ok = _table_with(feats, successes=19, total=20)
    assert p_weak_succeeds(feats, table=table_ok, min_samples=20) == pytest.approx(0.95)


def test_unknown_bucket_returns_none() -> None:
    feats = _features_for_step_down()
    table = _table_with(feats, successes=20, total=20)
    other = features_from_state("agent", "design an end-to-end migration", {"prior_errors": 0})
    assert p_weak_succeeds(other, table=table, min_samples=20) is None


def test_router_no_table_falls_back_to_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Learned flag on but no calibrated table -> heuristic step-down stands."""
    monkeypatch.setenv(LEARNED_ROUTING_ENV_VAR, "1")
    monkeypatch.setenv("ATELIER_TIER_ROUTING", "1")
    rec = ModelRouter().score(_STEP_DOWN_TOOL, _STEP_DOWN_TEXT, dict(_STEP_DOWN_STATE))
    assert rec is not None
    # No table -> p_weak None -> the learned gate holds the baseline (no
    # heuristic downgrade is authorised without evidence).
    assert rec.p_weak_succeeds is None
    assert rec.tier == "medium"
    assert any("heuristic fallback" in r for r in rec.reasons)


# --------------------------------------------------------------------------- #
# Downgrade only above threshold                                               #
# --------------------------------------------------------------------------- #


def _step_down_state_with_table(table: SuccessTable) -> dict:
    state = dict(_STEP_DOWN_STATE)
    state["tier_routing"] = True
    state["learned_routing"] = True
    state["learned_routing_table"] = table
    return state


def test_downgrade_allowed_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv("ATELIER_TIER_ROUTING", raising=False)
    feats = _features_for_step_down()
    table = _table_with(feats, successes=20, total=20)  # p_weak = 1.0 >= 0.9
    rec = ModelRouter().score(_STEP_DOWN_TOOL, _STEP_DOWN_TEXT, _step_down_state_with_table(table))
    assert rec is not None
    assert rec.p_weak_succeeds == 1.0
    assert rec.tier == "cheap"
    assert any("allow step down" in r for r in rec.reasons)
    assert any("step down" in r and "medium -> cheap" in r for r in rec.reasons)


def test_downgrade_blocked_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv("ATELIER_TIER_ROUTING", raising=False)
    feats = _features_for_step_down()
    table = _table_with(feats, successes=10, total=20)  # p_weak = 0.5 < 0.9
    rec = ModelRouter().score(_STEP_DOWN_TOOL, _STEP_DOWN_TEXT, _step_down_state_with_table(table))
    assert rec is not None
    assert rec.p_weak_succeeds == 0.5
    # Below threshold -> hold the baseline; NO downgrade.
    assert rec.tier == "medium"
    assert not any("step down" in r for r in rec.reasons)
    assert any("< threshold" in r for r in rec.reasons)


def test_threshold_override_changes_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv("ATELIER_TIER_ROUTING", raising=False)
    feats = _features_for_step_down()
    table = _table_with(feats, successes=10, total=20)  # p_weak = 0.5
    state = _step_down_state_with_table(table)
    state["learned_routing_threshold"] = 0.4  # 0.5 >= 0.4 now -> downgrade
    rec = ModelRouter().score(_STEP_DOWN_TOOL, _STEP_DOWN_TEXT, state)
    assert rec is not None
    assert rec.tier == "cheap"
    assert any("allow step down" in r for r in rec.reasons)


# --------------------------------------------------------------------------- #
# Safety floor preserved                                                        #
# --------------------------------------------------------------------------- #


def test_learned_layer_never_steps_up_or_skips_floor_for_hard_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with a perfect weak-success table, genuinely hard work is not downgraded."""
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv("ATELIER_TIER_ROUTING", raising=False)
    # Architectural agent task with errors baselines expensive; escalation/error
    # signals mean the step-down branch is never reached -> floor holds.
    feats = features_from_state("Agent", "design an end-to-end migration plan", {"prior_errors": 3})
    table = _table_with(feats, successes=100, total=100)  # p_weak = 1.0
    state = {
        "tier_routing": True,
        "learned_routing": True,
        "learned_routing_table": table,
        "prior_errors": 3,
    }
    rec = ModelRouter().score("Agent", "design an end-to-end migration plan", state)
    assert rec is not None
    assert rec.tier == "expensive"
    assert "opus" in rec.model


def test_learned_downgrade_clamps_to_one_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """A confidently-weak expensive baseline drops only ONE level (floor), not to cheap."""
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv("ATELIER_TIER_ROUTING", raising=False)
    # An agent tool in the execution phase with an open-ended output target
    # baselines expensive; the complexity signals are all trivial (-> cheap), so
    # the heuristic clamp allows a single step expensive -> medium (never cheap).
    tool, text = "agent", "do it"
    floor_state = {
        "session_phase": "execution",
        "output_target": "comprehensive full deep thorough",
        "prior_errors": 0,
        "changed_files": [],
        "task_size_chars": 10,
    }
    feats = features_from_state(tool, text, floor_state)
    table = _table_with(feats, successes=100, total=100)  # p_weak = 1.0
    state = dict(floor_state)
    state.update({"tier_routing": True, "learned_routing": True, "learned_routing_table": table})
    rec = ModelRouter().score(tool, text, state)
    assert rec is not None
    # Learned layer authorised the step, but the floor still clamps to one level.
    assert rec.tier == "medium"
    assert any("allow step down" in r for r in rec.reasons)
    assert any("expensive -> medium" in r for r in rec.reasons)


# --------------------------------------------------------------------------- #
# Flag-off byte-identical                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "text", "state"),
    [
        (_STEP_DOWN_TOOL, _STEP_DOWN_TEXT, _STEP_DOWN_STATE),
        ("read", "explain this function briefly", {"prior_errors": 0}),
        ("agent", "design an end-to-end migration plan", {"prior_errors": 3}),
        ("edit", "implement a new feature", {"changed_files": ["a.py", "b.py"]}),
    ],
)
def test_flag_off_is_identical_with_or_without_table(
    monkeypatch: pytest.MonkeyPatch, tool: str, text: str, state: dict
) -> None:
    """Flag off: routing decision is byte-identical whether a table is present or not."""
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    monkeypatch.setenv("ATELIER_TIER_ROUTING", "1")  # heuristic step-down active

    feats = features_from_state(tool, text, state)
    table = _table_with(feats, successes=0, total=100)  # would block every downgrade IF consulted

    baseline = ModelRouter().score(tool, text, dict(state))
    # Same call but with a (poison) table present and the flag OFF -> ignored.
    with_table_state = dict(state)
    with_table_state["learned_routing_table"] = table
    with_table = ModelRouter().score(tool, text, with_table_state)

    assert baseline is not None and with_table is not None
    # Learned layer is off -> p_weak not computed, table ignored.
    assert baseline.p_weak_succeeds is None
    assert with_table.p_weak_succeeds is None
    # Core routing outputs identical.
    assert baseline.tier == with_table.tier
    assert baseline.model == with_table.model
    assert baseline.route_tier == with_table.route_tier
    assert baseline.reasons == with_table.reasons
    assert not any("learned_routing" in r for r in baseline.reasons)


def test_to_dict_and_emit_include_p_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LEARNED_ROUTING_ENV_VAR, raising=False)
    monkeypatch.delenv("ATELIER_TIER_ROUTING", raising=False)
    feats = _features_for_step_down()
    table = _table_with(feats, successes=20, total=20)
    captured: list[dict] = []
    router = ModelRouter()
    rec = router.recommend(
        _STEP_DOWN_TOOL,
        _STEP_DOWN_TEXT,
        _step_down_state_with_table(table),
        route_decision_sink=captured.append,
    )
    assert rec is not None
    assert rec.to_dict()["p_weak_succeeds"] == 1.0
    assert captured and "p_weak_succeeds" in captured[0]
    assert captured[0]["p_weak_succeeds"] == 1.0


# --------------------------------------------------------------------------- #
# Persisted SQLite store (read-only) -> table                                  #
# --------------------------------------------------------------------------- #


def _make_routing_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atelier.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(read_migration("v2_004_routing.sql"))
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_outcome(
    conn: sqlite3.Connection,
    *,
    rd_id: str,
    task: str,
    step_type: str,
    outcome: str,
    changed_files: str = "[]",
    escalation: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO route_decision
            (id, session_id, step_index, step_type, risk_level, tier, confidence,
             reason, escalation_trigger, created_at)
        VALUES (?, 's1', 0, ?, 'low', 'cheap', 0.5, ?, ?, '2026-01-01T00:00:00Z')
        """,
        (rd_id, step_type, f"risk=low, task={task}, tier=cheap", escalation),
    )
    conn.execute(
        """
        INSERT INTO verification_envelope
            (id, route_decision_id, session_id, changed_files, outcome, created_at)
        VALUES (?, ?, 's1', ?, ?, '2026-01-01T00:00:00Z')
        """,
        (f"ve-{rd_id}", rd_id, changed_files, outcome),
    )


def test_load_outcome_rows_from_persisted_store(tmp_path: Path) -> None:
    db_path = _make_routing_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        for i in range(15):
            _insert_outcome(conn, rd_id=f"rd-pass-{i}", task="test", step_type="edit", outcome="pass")
        for i in range(5):
            _insert_outcome(conn, rd_id=f"rd-fail-{i}", task="test", step_type="edit", outcome="fail")
        # warn rows are ambiguous and must be dropped entirely.
        for i in range(7):
            _insert_outcome(conn, rd_id=f"rd-warn-{i}", task="test", step_type="edit", outcome="warn")
        conn.commit()
    finally:
        conn.close()

    rows = load_outcome_rows(db_path)
    # 15 pass + 5 fail = 20; the 7 warn rows are excluded.
    assert len(rows) == 20
    table = build_success_table(rows)
    # All rows share one bucket: verb=edit (task=test), tool=edit, none, xs.
    feats = RouteFeatures(verb="edit", tool_type="edit", prior_error="none", size_bucket="xs")
    assert p_weak_succeeds(feats, table=table, min_samples=20) == pytest.approx(15 / 20)


def test_load_outcome_rows_missing_tables_returns_empty(tmp_path: Path) -> None:
    # A fresh DB with no routing schema -> graceful empty (router stays heuristic).
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert load_outcome_rows(empty) == []


def test_persisted_row_reduction_maps_outcomes() -> None:
    pass_row = outcome_row_from_persisted(
        outcome="pass",
        reason="risk=low, task=feature, tier=cheap",
        step_type="edit",
        escalation_trigger=None,
        changed_files_count=1,
    )
    assert pass_row is not None and pass_row.success is True
    assert pass_row.features.verb == "design"  # "feature" is a design-class verb
    fail_row = outcome_row_from_persisted(
        outcome="escalate",
        reason="task=debug",
        step_type="debug",
        escalation_trigger="repeated_failures",
        changed_files_count=0,
    )
    assert fail_row is not None and fail_row.success is False
    assert fail_row.features.prior_error == "many"
    # warn / not_run -> dropped.
    assert (
        outcome_row_from_persisted(
            outcome="warn",
            reason="task=test",
            step_type="edit",
            escalation_trigger=None,
            changed_files_count=0,
        )
        is None
    )


def test_default_min_samples_is_twenty() -> None:
    assert DEFAULT_MIN_SAMPLES == 20
