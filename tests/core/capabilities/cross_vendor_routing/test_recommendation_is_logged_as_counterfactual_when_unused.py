from __future__ import annotations

from atelier.core.capabilities.cross_vendor_routing.configuration import (
    RouteConfig,
    save_route_config,
)
from atelier.gateway.adapters.mcp_server import _emit_model_recommendation
from atelier.infra.runtime import outcome_capture
from atelier.infra.runtime.run_ledger import RunLedger


def test_recommendation_is_logged_as_counterfactual_when_unused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_MODEL", "claude-sonnet-4.6")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["anthropic", "openai", "google"]))
    ledger = RunLedger(session_id="session-1", root=tmp_path)

    payload = _emit_model_recommendation("read", {"max_output_tokens": 200}, ledger)
    outcomes = outcome_capture.get_outcomes("session-1")["route_outcomes"]

    assert payload["configured"] is True
    assert payload["recommendation_followed"] is False
    assert outcomes[0]["recommendation_followed"] is False
    assert outcomes[0]["actual_model"] == "claude-sonnet-4.6"
    assert outcomes[0]["recommended_model"] == payload["model"]
