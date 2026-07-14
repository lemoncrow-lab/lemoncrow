from __future__ import annotations

from pathlib import Path

from lemoncrow.core.foundation.routing_models import AgentRequest, ContextBudgetPolicy
from lemoncrow.pro.capabilities.quality_router import (
    RoutingPolicyConfig,
    VerifierRequirementConfig,
    draft_route_decision,
    has_protected_file_match,
    load_routing_policy_config,
)


def _request(
    *,
    risk_level: str = "low",
    task_type: str = "docs",
    changed_files: list[str] | None = None,
) -> AgentRequest:
    return AgentRequest(
        session_id="run-1",
        user_goal="route this work",
        repo_root=".",
        task_type=task_type,  # type: ignore[arg-type]
        risk_level=risk_level,  # type: ignore[arg-type]
        changed_files=changed_files or [],
    )


def test_default_policy_loads_without_routing_toml(tmp_path: Path) -> None:
    config = load_routing_policy_config(tmp_path)

    assert config.models.cheap == ""
    assert config.models.mid == ""
    assert config.models.premium == ""
    assert "src/lemoncrow/core/foundation/**" in config.protected_file_patterns
    assert "state.change*" in config.high_risk_domain_patterns
    assert config.verifiers.high_risk == ["tests", "rubric"]

    budget = config.budget_policy(max_input_tokens=20_000)
    assert budget == ContextBudgetPolicy(max_input_tokens=20_000)


def test_protected_file_matching_uses_configured_globs() -> None:
    config = RoutingPolicyConfig(protected_file_patterns=["schema/**", "pyproject.toml"])

    assert has_protected_file_match(["./schema/product.sql"], config)
    assert has_protected_file_match(["pyproject.toml"], config)
    assert not has_protected_file_match(["src/lemoncrow/core/example.py"], config)


def test_protected_files_force_premium_escalation() -> None:
    config = RoutingPolicyConfig(
        protected_file_patterns=["src/lemoncrow/core/foundation/**"],
        verifiers=VerifierRequirementConfig(
            default=["unit"],
            protected_file=["review"],
            high_risk=[],
            low_confidence=[],
        ),
    )
    budget = ContextBudgetPolicy(
        max_input_tokens=20_000,
        cheap_model="cheap-local",
        mid_model="mid-local",
        premium_model="premium-local",
    )

    decision = draft_route_decision(
        request=_request(changed_files=["src/lemoncrow/core/foundation/routing_models.py"]),
        budget=budget,
        config=config,
        evidence_summary={"confidence": 0.95, "refs": ["trace:1"]},
    )

    assert decision.tier == "premium"
    assert decision.selected_model == "premium-local"
    assert decision.protected_file_match is True
    assert decision.escalation_trigger == "protected_file"
    assert decision.verifier_required == ["unit", "review"]
    assert decision.evidence_refs == ["trace:1"]


def test_high_risk_domain_forces_premium_and_rubric_verifier() -> None:
    config = RoutingPolicyConfig(
        high_risk_domain_patterns=["state.change*"],
        verifiers=VerifierRequirementConfig(
            default=["unit"],
            protected_file=[],
            high_risk=["rubric"],
            low_confidence=[],
        ),
    )
    budget = ContextBudgetPolicy(
        max_input_tokens=20_000,
        cheap_model="cheap-local",
        mid_model="mid-local",
        premium_model="premium-local",
    )

    decision = draft_route_decision(
        request=_request(risk_level="low", task_type="feature"),
        budget=budget,
        config=config,
        domain="state.change",
        evidence_summary={"confidence": 0.9},
    )

    assert decision.tier == "premium"
    assert decision.selected_model == "premium-local"
    assert decision.escalation_trigger == "high_risk"
    assert decision.verifier_required == ["unit", "rubric"]


def test_provider_neutral_toml_loading(tmp_path: Path) -> None:
    config_path = tmp_path / ".lemoncrow" / "routing.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
[models]
cheap = "local-small"
mid = "remote-balanced"
premium = "premium-review"

[thresholds]
cheap_context_ratio_max = 0.25
premium_context_ratio_min = 0.70
premium_evidence_confidence_max = 0.20
cheap_evidence_confidence_min = 0.75

protected_file_patterns = ["infra/**"]
high_risk_domain_patterns = ["custom.high.*"]

[verifiers]
default = ["unit"]
protected_file = ["approval"]
high_risk = ["rubric"]
low_confidence = ["evidence-review"]
""",
        encoding="utf-8",
    )

    config = load_routing_policy_config(tmp_path, path=config_path)
    budget = config.budget_policy(max_input_tokens=10_000, premium_call_budget=2)
    decision = draft_route_decision(
        request=_request(task_type="explain"),
        budget=budget,
        config=config,
        evidence_summary={"confidence": 0.8, "estimated_input_tokens": 1_000},
    )

    assert config.protected_file_patterns == ["infra/**"]
    assert config.high_risk_domain_patterns == ["custom.high.*"]
    assert budget.cheap_model == "local-small"
    assert budget.mid_model == "remote-balanced"
    assert budget.premium_model == "premium-review"
    assert budget.premium_call_budget == 2
    assert decision.tier == "cheap"
    assert decision.selected_model == "local-small"
