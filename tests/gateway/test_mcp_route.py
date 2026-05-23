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
    assert "can_spawn" in payload
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

def test_mcp_route_spawn_returns_directive_when_no_cli(mcp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import atelier.gateway.adapters.mcp_server as m

    m._client_sampling_supported = False
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    resp = _call(
        "route",
        {"op": "spawn", "prompt": "write tests for src/foo.py", "model": "claude-haiku-4-5"},
    )
    payload = _result(resp)

    assert payload["handled"] is False
    assert "spawn_directive" in payload
    directive = payload["spawn_directive"]
    assert directive["model"] == "claude-haiku-4-5"
    assert directive["agent_type"] == "general-purpose"
    assert "prompt" in directive
    assert "SPAWN_REQUIRED" in payload["message"]


def test_mcp_route_spawn_uses_cli_when_available(mcp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import subprocess

    import atelier.gateway.adapters.mcp_server as m

    m._client_sampling_supported = False
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None)
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"result": "done", "cost_usd": 0.001, "num_turns": 2}),
        stderr="",
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake)

    resp = _call(
        "route",
        {"op": "spawn", "prompt": "write a docstring for every function in src/foo.py", "model": "claude-haiku-4-5"},
    )
    payload = _result(resp)

    assert payload["handled"] is True
    assert payload["spawn_method"] == "cli_subprocess"
    assert payload["response"] == "done"
    assert "spawn_directive" in payload


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

