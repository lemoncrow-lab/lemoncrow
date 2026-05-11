from __future__ import annotations

from atelier.core.capabilities.quality_router.verifier import verify_route
from atelier.core.foundation.models import ValidationResult


def test_routing_verifier_pass_outcome() -> None:
    envelope = verify_route(
        route_decision_id="rd-1",
        session_id="run-1",
        changed_files=["README.md"],
        validation_results=[ValidationResult(name="pytest", passed=True, detail="ok")],
        rubric_status="pass",
        required_verifiers=["tests", "rubric"],
        human_accepted=True,
    )

    assert envelope.outcome == "pass"
    assert envelope.rubric_status == "pass"
    assert "failed=" not in envelope.compressed_evidence


def test_routing_verifier_warn_for_missing_required_verification() -> None:
    envelope = verify_route(
        route_decision_id="rd-2",
        session_id="run-2",
        changed_files=["src/app.py"],
        validation_results=[ValidationResult(name="pytest", passed=True, detail="ok")],
        rubric_status="not_run",
        required_verifiers=["tests", "rubric"],
        human_accepted=True,
    )

    assert envelope.outcome == "warn"
    assert "missing=" in envelope.compressed_evidence


def test_routing_verifier_fail_for_failed_validation() -> None:
    envelope = verify_route(
        route_decision_id="rd-3",
        session_id="run-3",
        changed_files=["src/service.py"],
        validation_results=[ValidationResult(name="pytest", passed=False, detail="failure")],
        rubric_status="pass",
        required_verifiers=["tests"],
        human_accepted=True,
    )

    assert envelope.outcome == "fail"
    assert "failed=pytest" in envelope.compressed_evidence


def test_routing_verifier_escalate_for_protected_failure_and_repeated_signatures() -> None:
    envelope = verify_route(
        route_decision_id="rd-4",
        session_id="run-4",
        changed_files=["src/atelier/core/foundation/models.py"],
        validation_results=[ValidationResult(name="pytest", passed=False, detail="failure")],
        rubric_status="pass",
        required_verifiers=["tests", "rubric"],
        protected_file_match=True,
        repeated_failure_signatures=["sig:1"],
        diff_line_count=1300,
        human_accepted=False,
    )

    assert envelope.outcome == "escalate"
    assert "escalate=" in envelope.compressed_evidence
    assert "repeated_failures=1" in envelope.compressed_evidence
