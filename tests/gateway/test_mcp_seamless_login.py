"""MCP-surface tests: `_try_seamless_login` must respect an explicit
`lc init --no-login` (store.mark_login_declined) and never pop a browser
tab for an install that opted out."""

from __future__ import annotations

from pathlib import Path

import pytest

import lemoncrow.gateway.adapters.mcp_server as mcp_server
from lemoncrow.core.capabilities.licensing import store


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)


def test_declined_marker_skips_browser_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.mark_login_declined()

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("run_oauth_login must not run after an explicit --no-login")

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.licensing.oauth_flow.run_oauth_login",
        _fail_if_called,
    )
    assert mcp_server._try_seamless_login(tmp_path) is False
    # Declining never even touches the per-attempt cooldown marker.
    assert not (tmp_path / ".login_attempted_at").exists()


def test_no_declined_marker_still_attempts_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.capabilities.licensing.oauth_flow import OAuthLoginResult

    called = {"n": 0}

    def _fake_login(*args: object, **kwargs: object) -> OAuthLoginResult:
        called["n"] += 1
        return OAuthLoginResult(token="t", email="e@example.com", plan="free", plan_verified=True, device_id="d")

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.licensing.oauth_flow.run_oauth_login",
        _fake_login,
    )
    assert mcp_server._try_seamless_login(tmp_path) is True
    assert called["n"] == 1
