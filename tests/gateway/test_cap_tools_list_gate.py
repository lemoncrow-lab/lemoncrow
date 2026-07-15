"""Server-enforced cap gate for live tools/list and tools/call boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed(root: Path, *, over: bool) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(root), {"plan": "free", "savingsOverCap": over})


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(licensing_gate, "_public_key_hex", lambda: "")


def _list() -> list[dict]:
    from lemoncrow.gateway.adapters import mcp_server

    resp = mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert isinstance(resp, dict)
    return resp["result"]["tools"]


def test_tools_hidden_when_over_cap(tmp_path: Path) -> None:
    _seed(tmp_path, over=True)
    assert _list() == []


def test_tools_present_when_under_cap(tmp_path: Path) -> None:
    _seed(tmp_path, over=False)
    tools = _list()
    assert len(tools) > 0
    assert any(t["name"] in {"read", "code_search", "bash", "edit"} for t in tools)


def test_tools_call_hard_rejected_when_dormant(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    _seed(tmp_path, over=True)
    resp = mcp_server._handle(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "read", "arguments": {"path": "x"}}}
    )
    assert isinstance(resp, dict)
    assert resp["error"]["code"] == -32601
    assert "cap reached" in resp["error"]["message"].lower()


def test_crossing_cap_applies_without_reconnect(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    _seed(tmp_path, over=False)
    mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert len(_list()) > 0

    _seed(tmp_path, over=True)
    assert _list() == []

    _seed(tmp_path, over=False)
    assert len(_list()) > 0
