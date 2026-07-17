"""MCP-surface tests: the open-source runtime NEVER opens a browser or solicits
login automatically. `_try_seamless_login` is disabled and always returns False
without touching the OAuth flow or writing an attempt marker, and account
activation is never required for auto-init / code-index warm."""

from __future__ import annotations

from pathlib import Path

import pytest

import lemoncrow.gateway.adapters.mcp_server as mcp_server
from lemoncrow.core.capabilities.licensing import store


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)


def test_seamless_login_never_opens_a_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("run_oauth_login must never run automatically in the OSS runtime")

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.licensing.oauth_flow.run_oauth_login",
        _fail_if_called,
    )
    assert mcp_server._try_seamless_login(tmp_path) is False
    assert not (tmp_path / ".login_attempted_at").exists()


def test_declined_marker_also_skips_browser_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.mark_login_declined()

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("run_oauth_login must not run")

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.licensing.oauth_flow.run_oauth_login",
        _fail_if_called,
    )
    assert mcp_server._try_seamless_login(tmp_path) is False
    assert not (tmp_path / ".login_attempted_at").exists()


def test_account_activation_never_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-init / code-index warm proceed without any account or login attempt."""

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("seamless login must not be attempted")

    monkeypatch.setattr(mcp_server, "_try_seamless_login", _fail_if_called)
    assert mcp_server._ensure_account_activated(tmp_path) is True
