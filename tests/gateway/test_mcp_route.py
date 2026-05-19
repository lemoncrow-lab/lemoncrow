from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfig, save_route_config
from atelier.gateway.adapters.mcp_server import _handle


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    req: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    resp = _handle(req)
    assert isinstance(resp, dict)
    return resp


def _result(resp: dict[str, Any]) -> dict[str, Any]:
    assert "result" in resp, resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert isinstance(payload, dict)
    return payload


@pytest.fixture()
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".atelier"
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("ATELIER_MODEL", "claude-sonnet-4.6")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    save_route_config(root, RouteConfig(enabled_vendors=["anthropic", "openai", "google"]))

    import atelier.gateway.adapters.mcp_server as m

    m._current_ledger = None
    return root


def test_mcp_route_recommend_returns_cross_vendor_payload(mcp_env: Path) -> None:
    resp = _call(
        "route",
        {
            "op": "recommend",
            "tool_name": "read",
            "task_text": "find the failing test",
            "session_state": {"expected_input_tokens": 1200, "expected_output_tokens": 200, "turn_number": 1},
        },
    )
    payload = _result(resp)

    assert payload["configured"] is True
    assert payload["vendor"] == "google"
    assert payload["model"] == "gemini-flash"
    assert payload["alternatives"]
    assert "actual_model" in payload
    assert "recommendation_followed" in payload
