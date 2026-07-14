"""Cap gate helper (_savings_dormant) that drives tool exposure (tools/list).

Tool *behavior* is never changed by the cap — only whether the tool is exposed.
The exposure gate itself is covered by test_cap_tools_list_gate.py; here we pin
the helper's read of the persisted meter (fail-open, grandfather snapshot).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed_meter(root: Path, *, over: bool) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(root), {"plan": "free", "savingsOverCap": over})


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    from lemoncrow.gateway.adapters import mcp_server

    mcp_server._DORMANT_SNAPSHOT_BY_SESSION.clear()


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


def test_dormant_fail_open_when_meter_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    assert mcp_server._savings_dormant() is False  # no meter -> tools stay visible
