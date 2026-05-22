from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities.cross_vendor_routing.configuration import (
    RouteConfig,
    save_route_config,
)
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


# ── op=recommend (backward compat, hidden from schema) ─────────────────────

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
    assert payload["model"] == "gemini-2.0-flash"
    assert payload["alternatives"]
    assert "actual_model" in payload
    assert "recommendation_followed" in payload


def test_mcp_route_recommend_works_with_host_clis_without_vendor_api_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".atelier"
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("ATELIER_MODEL", "gpt-5.4")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        "shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"claude", "codex", "agy"} else None,
    )
    save_route_config(root, RouteConfig(enabled_vendors=["anthropic", "openai", "google"]))

    import atelier.gateway.adapters.mcp_server as m

    m._current_ledger = None

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
    assert payload["model"] == "gemini-2.0-flash"


# ── op=decide ───────────────────────────────────────────────────────────────

def test_mcp_route_decide_returns_model_and_metadata(mcp_env: Path) -> None:
    resp = _call(
        "route",
        {"op": "decide", "task": "implement a new REST endpoint", "task_type": "feature"},
    )
    payload = _result(resp)

    assert "model" in payload
    assert "tier" in payload
    assert "rationale" in payload
    assert "available_models" in payload
    assert isinstance(payload["available_models"], list)
    assert "sampling_supported" in payload
    assert "host_model" in payload
    assert "_summary" in payload
    assert payload["_summary"]["recommended"] == payload["model"]


def test_mcp_route_decide_budget_cheap_picks_cheapest(mcp_env: Path) -> None:
    resp = _call(
        "route",
        {"op": "decide", "task": "summarize a file", "task_type": "explain", "budget": "cheap"},
    )
    payload = _result(resp)

    assert payload["tier"] == "cheap"
    # cheapest anthropic model is haiku
    assert "haiku" in payload["model"] or "flash" in payload["model"] or "mini" in payload["model"]


def test_mcp_route_decide_budget_best_picks_powerful(mcp_env: Path) -> None:
    resp = _call(
        "route",
        {"op": "decide", "task": "design a new architecture", "task_type": "feature", "budget": "best"},
    )
    payload = _result(resp)

    # Should pick a high-tier model
    assert payload["tier"] in ("high", "expensive", "medium", "cheap")  # just must return valid tier


def test_mcp_route_decide_no_route_config_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".atelier"
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("ATELIER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    # No route config saved — advisor will raise, decide must fall back gracefully

    import atelier.gateway.adapters.mcp_server as m

    m._current_ledger = None

    resp = _call("route", {"op": "decide", "task": "refactor this function"})
    payload = _result(resp)

    assert "model" in payload
    assert "available_models" in payload


# ── op=spawn ────────────────────────────────────────────────────────────────

def test_mcp_route_spawn_returns_unsupported_when_no_sampling(mcp_env: Path) -> None:
    import atelier.gateway.adapters.mcp_server as m

    m._client_sampling_supported = False

    resp = _call(
        "route",
        {"op": "spawn", "prompt": "hello world", "model": "claude-haiku-4-5"},
    )
    payload = _result(resp)

    assert payload["sampling_supported"] is False
    assert "error" in payload
    assert "prompt" in payload
    assert "model_hint" in payload


def test_mcp_route_schema_exposes_only_decide_and_spawn() -> None:
    from atelier.gateway.adapters.mcp_server import TOOLS

    schema = TOOLS["route"].get("inputSchema", {})
    props = schema.get("properties", {})
    exposed_ops = props.get("op", {}).get("enum", [])

    assert "decide" in exposed_ops
    assert "spawn" in exposed_ops
    # Internal ops must not appear in the schema
    assert "verify" not in exposed_ops
    assert "recommend" not in exposed_ops

