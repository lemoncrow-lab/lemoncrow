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
