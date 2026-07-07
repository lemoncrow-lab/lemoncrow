from __future__ import annotations

import pytest
from pydantic import ValidationError

from atelier.core.foundation.models import ValidationResult
from atelier.core.foundation.routing_models import (
    AgentRequest,
    ContextBudgetPolicy,
    RouteDecision,
    RoutingEvalSummary,
    VerificationEnvelope,
)


def test_agent_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AgentRequest(
            user_goal="fix it",
            repo_root=".",
            task_type="debug",
            risk_level="low",
            unexpected=True,  # type: ignore[call-arg]
        )


def test_route_decision_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        RouteDecision(
            session_id="run-1",
            step_index=0,
            step_type="plan",
            risk_level="medium",
            tier="mid",
            confidence=1.5,
            reason="observable reason",
        )


def test_verification_envelope_stores_observed_results() -> None:
    result = ValidationResult(name="pytest", passed=True, detail="ok")
    envelope = VerificationEnvelope(
        route_decision_id="rd-1",
        session_id="run-1",
        changed_files=["src/example.py"],
        validation_results=[result],
        outcome="pass",
    )
    assert envelope.validation_results == [result]
    assert envelope.rubric_status == "not_run"


def test_routing_models_default_uuid7_ids() -> None:
    request = AgentRequest(
        user_goal="fix it",
        repo_root=".",
        task_type="debug",
        risk_level="low",
    )
    decision = RouteDecision(
        session_id="run-1",
        step_index=0,
        step_type="plan",
        risk_level="medium",
        tier="mid",
        confidence=0.5,
        reason="observable reason",
    )
    envelope = VerificationEnvelope(route_decision_id=decision.id, session_id="run-1", outcome="pass")
    summary = RoutingEvalSummary(
        session_id="run-1",
        cost_per_accepted_patch=0.2,
        premium_call_rate=0.1,
        cheap_success_rate=0.8,
        escalation_success_rate=0.7,
        routing_regression_rate=0.0,
    )
    policy = ContextBudgetPolicy(max_input_tokens=1000)

    assert request.id.startswith("req-")
    assert decision.id.startswith("rd-")
    assert envelope.id.startswith("ve-")
    assert policy.cache_policy == "prefer_cache"
    assert summary.session_id == "run-1"
