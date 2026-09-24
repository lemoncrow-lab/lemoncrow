from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import routing


def test_mcp_server_reexports_canonical_routing_primitives() -> None:
    assert mcp_server._normalize_model_id is routing.normalize_model_id
    assert mcp_server._provider_for_model is routing.provider_for_model
    assert mcp_server._task_text_from_args is routing.task_text_from_args
    assert mcp_server._route_outcome_calibration is routing.route_outcome_calibration
    assert mcp_server._restore_legacy_route is routing.restore_legacy_route


def test_provider_and_task_text_policy_are_transport_neutral() -> None:
    assert routing.provider_for_model("claude-sonnet-4-5") == "anthropic"
    assert routing.provider_for_model("gpt-5.6") == "openai"
    assert routing.provider_for_model("gemini-2.5-pro") == "google"
    assert routing.provider_for_model("mystery") == "unknown"
    assert routing.task_text_from_args({"query": "find x", "description": "then edit"}) == "find x\nthen edit"


def test_mcp_server_model_routing_wrappers_use_canonical_runtime() -> None:
    from lemoncrow.gateway.tools import model_routing

    assert "_canonical_model_recommendation_state" in __import__("inspect").getsource(
        mcp_server._model_recommendation_state
    )
    assert model_routing.ModelRoutingHooks is not None


def test_model_routing_hooks_late_bind_mcp_route_selector(monkeypatch) -> None:
    marker = object()

    def fake_selector(**_kwargs):
        return marker

    monkeypatch.setattr(mcp_server, "_select_owned_execution_route", fake_selector)
    hooks = mcp_server._model_routing_hooks()
    assert hooks.select_owned_execution_route is fake_selector
    assert hooks.select_owned_execution_route() is marker
