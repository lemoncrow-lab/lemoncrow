from __future__ import annotations

from lemoncrow.pro.capabilities.code_context.evidence_resolution import propose_evidence_resolution
from lemoncrow.pro.capabilities.code_context.evidence_state import EvidenceState


def _state(**overrides) -> EvidenceState:
    values = {
        "status": "ambiguous",
        "confidence": 0.55,
        "reason_codes": ("low_rank_margin",),
        "top_paths": ("src/a.py", "src/b.py"),
        "evidence_refs": ("src/a.py:L10-L20",),
        "candidate_count": 2,
        "source_file_count": 0,
        "source_section_count": 0,
        "relationship_count": 0,
        "tokens_used": 300,
        "budget_tokens": 2000,
        "rank_margin": 0.05,
    }
    values.update(overrides)
    return EvidenceState(**values)


def test_ambiguous_missing_source_proposes_one_bounded_hydration_round() -> None:
    proposal = propose_evidence_resolution(_state(), mode="shadow")

    assert proposal.eligible is True
    assert proposal.action == "HYDRATE_SOURCE"
    assert proposal.target_path == "src/a.py"
    assert proposal.round_limit == 1
    assert proposal.token_budget == 800
    assert proposal.latency_budget_ms == 150


def test_ambiguous_with_source_but_no_relations_proposes_relation_expansion() -> None:
    proposal = propose_evidence_resolution(
        _state(source_file_count=1, source_section_count=2),
        mode="shadow",
    )

    assert proposal.eligible is True
    assert proposal.action == "EXPAND_RELATIONS"
    assert proposal.target_path == "src/a.py"


def test_sufficient_dark_absent_and_truncated_states_stop() -> None:
    cases = [
        _state(status="decisive"),
        _state(status="useful"),
        _state(status="dark"),
        _state(status="absent"),
        _state(truncated=True),
    ]

    for state in cases:
        proposal = propose_evidence_resolution(state, mode="shadow")
        assert proposal.eligible is False
        assert proposal.action == "STOP"


def test_off_mode_and_zero_budget_never_propose_work(monkeypatch) -> None:
    assert propose_evidence_resolution(_state(), mode="off").action == "STOP"

    monkeypatch.setenv("LEMONCROW_EVIDENCE_RESOLUTION_ROUNDS", "0")
    proposal = propose_evidence_resolution(_state(), mode="shadow")
    assert proposal.eligible is False
    assert proposal.action == "STOP"
    assert proposal.reason_codes == ("resolution_budget_exhausted",)


def test_enforce_request_stays_shadow_until_acceptance_gate() -> None:
    state = _state()
    proposal = propose_evidence_resolution(state, mode="enforce")
    event = proposal.to_runtime_event(state, session_id="s")

    assert proposal.mode == "shadow"
    assert proposal.requested_enforce is True
    assert event.mode == "shadow"
    assert event.kind == "retrieval.expand"
    assert event.proposed["action"] == "HYDRATE_SOURCE"
    assert event.actual == {"action": "STOP", "rounds": 0}
    assert "enforcement_not_qualified" in event.reason_codes


def test_experiment_mode_is_explicit_but_production_enforce_stays_shadow(monkeypatch) -> None:
    state = _state(source_file_count=1, source_section_count=2)

    accidental = propose_evidence_resolution(state, mode="experiment")
    monkeypatch.setenv("LEMONCROW_EVIDENCE_RESOLUTION_EXPERIMENT", "benchmark")
    experiment = propose_evidence_resolution(state, mode="experiment")
    production = propose_evidence_resolution(state, mode="enforce")

    assert accidental.mode == "shadow"
    assert accidental.experimental is False
    assert experiment.mode == "experiment"
    assert experiment.experimental is True
    assert experiment.action == "EXPAND_RELATIONS"
    assert production.mode == "shadow"
    assert production.experimental is False
    assert production.requested_enforce is True


def test_experiment_event_reports_actual_expansion_without_claiming_qualification(monkeypatch) -> None:
    state = _state(source_file_count=1, source_section_count=2)
    monkeypatch.setenv("LEMONCROW_EVIDENCE_RESOLUTION_EXPERIMENT", "benchmark")
    proposal = propose_evidence_resolution(state, mode="experiment")
    event = proposal.to_runtime_event(
        state,
        session_id="s",
        actual_action="EXPAND_RELATIONS",
        rounds=1,
        elapsed_ms=12,
    )

    assert event.mode == "enforce"
    assert event.policy_version == "1-experiment"
    assert event.actual == {"action": "EXPAND_RELATIONS", "rounds": 1}
    assert event.metrics["experimental"] is True
    assert event.metrics["elapsed_ms"] == 12
    assert "benchmark_experiment" in event.reason_codes
