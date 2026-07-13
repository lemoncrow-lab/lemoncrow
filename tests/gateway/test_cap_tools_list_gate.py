"""Server-enforced Layer 2: tools/list advertises NO tools when the cap is exhausted."""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed(root: Path, *, over: bool) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(root), {"plan": "free", "savingsOverCap": over})


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    mcp_server._DORMANT_SNAPSHOT["value"] = None  # unfrozen -> lazy re-read per test


def _list(tmp_path: Path) -> list[dict]:
    from lemoncrow.gateway.adapters import mcp_server

    resp = mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert isinstance(resp, dict)
    return resp["result"]["tools"]


def test_tools_hidden_when_over_cap(tmp_path: Path) -> None:
    _seed(tmp_path, over=True)
    assert _list(tmp_path) == []


def test_tools_present_when_under_cap(tmp_path: Path) -> None:
    _seed(tmp_path, over=False)
    tools = _list(tmp_path)
    assert len(tools) > 0
    assert any(t["name"] in {"read", "code_search", "bash", "edit"} for t in tools)


def _snapshot(tmp_path: Path, *, over: bool) -> None:
    """Freeze the process dormant snapshot as `initialize` would."""
    from lemoncrow.gateway.adapters import mcp_server

    _seed(tmp_path, over=over)
    mcp_server._DORMANT_SNAPSHOT["value"] = None  # force re-read
    mcp_server._DORMANT_SNAPSHOT["value"] = mcp_server._snapshot_dormant()


def test_tools_call_hard_rejected_when_dormant(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    _snapshot(tmp_path, over=True)
    resp = mcp_server._handle(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "read", "arguments": {"path": "x"}}}
    )
    assert isinstance(resp, dict)
    assert resp["error"]["code"] == -32601
    assert "cap reached" in resp["error"]["message"].lower()


def test_freeze_grandfathers_when_started_under_cap(tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    # connected UNDER cap -> snapshot False; even if the meter later flips, the
    # frozen value holds for the connection (grandfather).
    _snapshot(tmp_path, over=False)
    _seed(tmp_path, over=True)  # meter moves after connect
    assert mcp_server._savings_dormant() is False  # frozen -> still active
    assert len(_list(tmp_path)) > 0
