"""Tests for prospective model routing recommendations."""

from __future__ import annotations

from lemoncrow.pro.capabilities.model_routing import ModelRouter


def test_model_router_routes_read_explain_to_cheap() -> None:
    rec = ModelRouter().score("read", "explain this function briefly", {"prior_errors": 0})

    assert rec.tier == "cheap"
    assert "haiku" in rec.model


def test_model_router_routes_agent_design_to_expensive() -> None:
    rec = ModelRouter().score("Agent", "design an end-to-end migration plan", {"prior_errors": 3})

    assert rec.tier == "expensive"
    assert "opus" in rec.model


def test_model_router_preserves_cache_affinity_model() -> None:
    rec = ModelRouter().score(
        "read",
        "show the latest output",
        {"prior_errors": 0, "cache_affinity_model": "claude-sonnet-4.6"},
    )

    assert rec.model == "claude-sonnet-4.6"
    assert rec.cache_affinity_model == "claude-sonnet-4.6"


def test_model_router_ignores_affinity_when_task_scores_expensive() -> None:
    # An architectural task with many errors scores expensive.  A cheap cache-affinity
    # model must not override that — the agent needs the stronger model.
    rec = ModelRouter().score(
        "Agent",
        "design an end-to-end migration strategy",
        {"prior_errors": 3, "cache_affinity_model": "claude-haiku-4-5"},
    )

    assert rec.tier == "expensive"
    assert "opus" in rec.model
    assert any("ignored" in r for r in rec.reasons)


def test_model_router_expensive_default_is_current_opus() -> None:
    rec = ModelRouter().score("Agent", "design the architecture", {})

    assert rec.model == "claude-opus-4-7"


def test_model_router_prefers_explicit_workflow_phase() -> None:
    rec = ModelRouter().score(
        "read",
        "show the latest output",
        {"prior_errors": 0, "workflow_step": "execution", "turn_number": 0, "recent_tool_calls": []},
    )

    assert any("explicit execution" in reason for reason in rec.reasons)
