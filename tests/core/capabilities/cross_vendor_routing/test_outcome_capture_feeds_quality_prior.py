from __future__ import annotations

from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfig, save_route_config
from atelier.gateway.adapters.mcp_server import _emit_model_recommendation
from atelier.infra.runtime import outcome_capture
from atelier.infra.runtime.run_ledger import RunLedger


def test_outcome_capture_feeds_quality_prior(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_MODEL", "claude-sonnet-4.6")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["anthropic", "openai", "google"]))
    ledger = RunLedger(session_id="session-2", root=tmp_path)
    ledger.record_tool_call("read", {"path": "README.md"})
    ledger.errors_seen.append("timeout")

    _emit_model_recommendation("read", {"max_output_tokens": 300}, ledger)
    outcome = outcome_capture.get_outcomes("session-2")["route_outcomes"][0]

    assert outcome["recommended_vendor"] == "google"
    assert outcome["scored_state"]["turn_number"] >= 1
    assert outcome["scored_state"]["prior_errors"] == 1
