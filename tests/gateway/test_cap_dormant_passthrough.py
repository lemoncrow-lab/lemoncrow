"""The MCP exposure helper evaluates the compiled cap authority live."""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed_meter(root: Path, *, over: bool) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(root), {"plan": "free", "savingsOverCap": over})


@pytest.fixture(autouse=True)
def _local_meter_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setattr(licensing_gate, "_public_key_hex", lambda: "")


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


def test_authority_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setattr(
        licensing_gate,
        "cap_exhausted",
        lambda _root: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    assert mcp_server._savings_dormant() is True
