"""Open-source runtime: the MCP tool surface is NEVER gated or hidden.

The former savings-cap dormancy gate on tools/list and tools/call was removed
(see docs/maintenance-mode-transition.md). Every tool is always advertised and
callable, regardless of any legacy over-cap subscription state left on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed_legacy_over_cap(root: Path) -> None:
    # A leftover "over cap" flag from a legacy install must have NO effect.
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(root), {"plan": "free", "savingsOverCap": True})


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))


def _list() -> list[dict]:
    from lemoncrow.gateway.adapters import mcp_server

    resp = mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert isinstance(resp, dict)
    return resp["result"]["tools"]


def test_tools_always_listed(tmp_path: Path) -> None:
    tools = _list()
    assert len(tools) > 0
    assert any(t["name"] in {"read", "code_search", "bash", "edit"} for t in tools)


def test_tools_listed_even_with_legacy_over_cap_state(tmp_path: Path) -> None:
    _seed_legacy_over_cap(tmp_path)
    tools = _list()
    assert len(tools) > 0
    assert any(t["name"] in {"read", "code_search", "bash", "edit"} for t in tools)


def test_tools_call_never_rejected_by_cap(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    _seed_legacy_over_cap(tmp_path)
    resp = mcp_server._handle(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "read", "arguments": {"path": "x"}}}
    )
    assert isinstance(resp, dict)
    # Never the old "anonymous savings cap reached" rejection.
    assert "cap reached" not in str(resp).lower()


def test_crossing_legacy_cap_state_has_no_effect(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert len(_list()) > 0
    _seed_legacy_over_cap(tmp_path)
    assert len(_list()) > 0
