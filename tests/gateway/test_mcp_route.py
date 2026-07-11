from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities.cross_vendor_routing.configuration import (
    RouteConfig,
    save_route_config,
)
from lemoncrow.core.capabilities.pricing import active_model
from lemoncrow.gateway.adapters.mcp_server import _handle


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
    root = tmp_path / ".lemoncrow"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_MODEL", "claude-sonnet-4.6")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setattr(
        "shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"claude", "codex", "copilot"} else None,
    )
    save_route_config(root, RouteConfig(enabled_vendors=["anthropic", "openai", "google"]))

    import lemoncrow.gateway.adapters.mcp_server as m

    m._current_ledger = None
    return root


def _last_model_recommendation_payload() -> dict[str, Any]:
    import lemoncrow.gateway.adapters.mcp_server as m

    assert m._current_ledger is not None
    matches = [event.payload for event in m._current_ledger.events if event.kind == "model_recommendation"]
    assert matches
    return matches[-1]


def test_local_tool_route_enforcement_is_advisory_by_default(
    mcp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemoncrow.gateway.adapters.mcp_server as m

    seen: dict[str, str] = {}

    def fake_handler(_: dict[str, Any]) -> dict[str, Any]:
        seen["active_model"] = active_model()
        return {"ok": True}

    monkeypatch.setitem(m.TOOLS["read"], "handler", fake_handler)
    monkeypatch.delenv("LEMONCROW_ENFORCE_ROUTE_MODEL", raising=False)

    response = _call("read", {"path": "/tmp/placeholder"})
    payload = _last_model_recommendation_payload()

    assert "result" in response
    assert seen["active_model"] == os.environ["LEMONCROW_MODEL"]
    assert payload["route_enforcement_active"] is False
    assert payload["wrapper_applied"] is False
    assert payload.get("wrapper_model") in (None, "")


def test_local_tool_route_enforcement_wraps_handler_with_recommended_model(
    mcp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemoncrow.gateway.adapters.mcp_server as m

    seen: dict[str, str] = {}

    def fake_handler(_: dict[str, Any]) -> dict[str, Any]:
        seen["active_model"] = active_model()
        return {"ok": True}

    monkeypatch.setitem(m.TOOLS["read"], "handler", fake_handler)
    monkeypatch.setenv("LEMONCROW_ENFORCE_ROUTE_MODEL", "1")

    response = _call("read", {"path": "/tmp/placeholder"})
    payload = _last_model_recommendation_payload()

    assert "result" in response
    assert payload["route_enforcement_active"] is True
    assert payload["wrapper_applied"] is True
    assert payload["wrapper_model"] == payload["model"]
    assert payload["executed_model_scope"] == "local_mcp_only"
    assert payload["recommendation_followed"] is True
    assert seen["active_model"] == payload["model"]
    assert os.environ["LEMONCROW_MODEL"] == "claude-sonnet-4.6"


def test_local_tool_route_enforcement_restores_model_after_error(
    mcp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemoncrow.gateway.adapters.mcp_server as m

    seen: dict[str, str] = {}

    def fake_handler(_: dict[str, Any]) -> dict[str, Any]:
        seen["active_model"] = active_model()
        raise RuntimeError("boom")

    monkeypatch.setitem(m.TOOLS["read"], "handler", fake_handler)
    monkeypatch.setenv("LEMONCROW_ENFORCE_ROUTE_MODEL", "1")

    response = _call("read", {"path": "/tmp/placeholder"})
    payload = _last_model_recommendation_payload()

    assert response["result"]["isError"] is True
    assert seen["active_model"] == payload["model"]
    assert payload["wrapper_applied"] is True
    assert os.environ["LEMONCROW_MODEL"] == "claude-sonnet-4.6"
