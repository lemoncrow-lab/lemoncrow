"""Layer-1: lc MCP tools pass raw output through when the savings cap is exhausted."""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed_meter(root: Path, *, over: bool) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(root), {"plan": "free", "savingsOverCap": over})


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    from lemoncrow.gateway.adapters import mcp_server

    mcp_server._DORMANT_CACHE["at"] = 0.0
    mcp_server._DORMANT_CACHE["value"] = False


def test_dormant_true_when_over_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    _seed_meter(tmp_path, over=True)
    assert mcp_server._savings_dormant() is True


def test_dormant_false_when_under_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    _seed_meter(tmp_path, over=False)
    assert mcp_server._savings_dormant() is False


def test_auto_compact_passthrough_when_dormant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_AUTO_COMPACT_OUTPUT", "1")
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "2000")
    big = "def f():\n" + "\n\n\n".join(f"    x{i} = {i}  " for i in range(2000)) + "\n"
    _seed_meter(tmp_path, over=True)
    out = mcp_server._auto_compact_result_text(big, "read", {"path": "mod.py"})
    assert out == big  # dormant -> unchanged, no projection footer


def test_auto_compact_active_when_under_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_AUTO_COMPACT_OUTPUT", "1")
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "2000")
    big = "def f():\n" + "\n\n\n".join(f"    x{i} = {i}  " for i in range(2000)) + "\n"
    _seed_meter(tmp_path, over=False)
    out = mcp_server._auto_compact_result_text(big, "read", {"path": "mod.py"})
    assert "projection:python" in out  # under cap -> savings applied
