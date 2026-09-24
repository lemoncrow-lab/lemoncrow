from __future__ import annotations

from pathlib import Path

from lemoncrow.core.runtime import LemonCrowRuntimeCore
from lemoncrow.infra.runtime.run_ledger import RunLedger


def _runtime(tmp_path: Path) -> LemonCrowRuntimeCore:
    root = tmp_path / ".lemoncrow"
    return LemonCrowRuntimeCore(root)


def test_route_decide_low_risk_routes_cheap(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)

    decision = rt.route_decide(
        user_goal="Summarize changelog wording",
        repo_root=".",
        task_type="docs",
        risk_level="low",
        changed_files=["README.md"],
        step_type="plan",
        evidence_summary={"confidence": 0.95, "estimated_input_tokens": 200},
    )

    assert decision.tier == "cheap"
    assert decision.escalation_trigger is None


def test_route_decide_high_risk_routes_premium(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)

    decision = rt.route_decide(
        user_goal="Modify publish pipeline",
        repo_root=".",
        task_type="feature",
        risk_level="high",
        changed_files=["src/service/publish.py"],
        step_type="plan",
        evidence_summary={"confidence": 0.90, "estimated_input_tokens": 600},
    )

    assert decision.tier == "premium"
    assert decision.escalation_trigger in {"high_risk", "protected_file", "context_pressure"}


def test_route_decide_repeated_failure_forces_premium(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    ledger = RunLedger(root=tmp_path / ".lemoncrow", session_id="run-repeat")
    ledger.repeated_failures.append("same-error-signature")

    decision = rt.route_decide(
        user_goal="Adjust docs heading",
        repo_root=".",
        task_type="docs",
        risk_level="low",
        changed_files=["docs/notes.md"],
        step_type="plan",
        session_id=ledger.session_id,
        evidence_summary={"confidence": 0.95, "estimated_input_tokens": 180},
        ledger=ledger,
    )

    assert decision.tier == "premium"
    assert decision.escalation_trigger == "repeated_failure"


def test_route_decide_accepts_code_context_evidence_state(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.code_context.evidence_state import EvidenceState

    rt = _runtime(tmp_path)
    evidence = EvidenceState(
        status="dark",
        confidence=0.0,
        reason_codes=("channel_dark:zoekt",),
        evidence_refs=("src/service.py:L10-L20",),
        candidate_count=0,
    )

    decision = rt.route_decide(
        user_goal="Explain the service path",
        repo_root=".",
        task_type="feature",
        risk_level="low",
        step_type="plan",
        evidence_summary=evidence,  # type: ignore[arg-type]
    )

    assert decision.tier == "mid"
    assert decision.escalation_trigger is None
    assert "src/service.py:L10-L20" in decision.evidence_refs
    assert "retrieval=dark" in decision.reason


def _code_evidence(status: str, confidence: float):
    from lemoncrow.pro.capabilities.code_context.evidence_state import EvidenceState

    return EvidenceState(
        status=status,
        confidence=confidence,
        reason_codes=(f"state:{status}",),
        evidence_refs=("src/service.py:L10-L20",),
        candidate_count=0 if status in {"dark", "absent"} else 2,
    )


def test_absent_retrieval_does_not_buy_a_premium_model(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)

    decision = rt.route_decide(
        user_goal="Explain the service path",
        repo_root=".",
        task_type="feature",
        risk_level="low",
        step_type="plan",
        evidence_summary=_code_evidence("absent", 0.0),
    )

    assert decision.tier == "mid"
    assert decision.escalation_trigger is None
    assert "retrieval=absent" in decision.reason


def test_ambiguous_low_confidence_can_escalate_when_retrieval_is_live(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)

    decision = rt.route_decide(
        user_goal="Implement the uncertain service change",
        repo_root=".",
        task_type="feature",
        risk_level="low",
        step_type="plan",
        evidence_summary=_code_evidence("ambiguous", 0.0),
    )

    assert decision.tier == "premium"
    assert decision.escalation_trigger == "low_evidence_confidence"


def test_high_risk_still_escalates_when_retrieval_is_dark(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)

    decision = rt.route_decide(
        user_goal="Change the production authorization path",
        repo_root=".",
        task_type="feature",
        risk_level="high",
        step_type="plan",
        evidence_summary=_code_evidence("dark", 0.0),
    )

    assert decision.tier == "premium"
    assert decision.escalation_trigger in {"high_risk", "protected_file"}


def test_route_decision_is_emitted_to_existing_ledger(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    ledger = RunLedger(root=tmp_path / ".lemoncrow", session_id="route-session")

    decision = rt.route_decide(
        user_goal="Explain the service path",
        repo_root=".",
        task_type="feature",
        risk_level="low",
        step_type="plan",
        evidence_summary=_code_evidence("dark", 0.0),
        ledger=ledger,
    )

    event = next(item for item in ledger.events if item.summary == "runtime_decision:route.model")
    payload = event.payload["runtime_decision"]
    assert payload["kind"] == "route.model"
    assert payload["policy"] == "quality-router"
    assert payload["actual"]["tier"] == decision.tier
    assert payload["metrics"]["retrieval_status"] == "dark"
    assert "retrieval:dark" in payload["reason_codes"]
