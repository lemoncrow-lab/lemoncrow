"""Offline-core acceptance (maintenance-mode transition).

With all outbound (non-loopback) network access blocked and no account
credentials, the core local chokepoints succeed, entitlements are unlocked, the
cap gate is never dormant, local identity works, and telemetry (off by default)
emits nothing over the wire. See docs/maintenance-mode-transition.md.
"""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture()
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block every outbound network path except loopback (local services)."""

    def _blocked_urlopen(*args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected outbound urlopen call")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen)

    orig_connect = socket.socket.connect

    def _connect(self: socket.socket, address: object, *a: object, **k: object) -> object:
        host = address[0] if isinstance(address, tuple) else ""
        if host in ("127.0.0.1", "::1", "localhost", ""):
            return orig_connect(self, address, *a, **k)  # type: ignore[arg-type]
        raise AssertionError(f"unexpected outbound socket connect to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _connect)


def test_licensing_chokepoints_work_offline(_no_network: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)

    from lemoncrow.core.capabilities import licensing
    from lemoncrow.pro.capabilities.licensing_gate import cap_exhausted, resolve_cap_verdict

    assert licensing.is_pro() is True
    assert licensing.has_feature("code_search") is True
    licensing.require("optimizer")  # never raises
    verdict = resolve_cap_verdict(tmp_path)
    assert verdict.dormant is False
    assert cap_exhausted(tmp_path) is False


def test_local_identity_works_offline(_no_network: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))

    from lemoncrow.core.capabilities.licensing import store
    from lemoncrow.core.foundation.identity import get_anon_id, reset_anon_id
    from lemoncrow.core.foundation.legacy_migration import run_startup_migrations

    anon = get_anon_id()
    assert anon and anon == get_anon_id()  # stable, local
    assert reset_anon_id() != anon  # resettable
    assert len(store.load_or_create_device_id()) >= 4  # random-local, no network
    run_startup_migrations(tmp_path)  # migration is offline


def test_telemetry_off_by_default_and_emits_nothing_offline(
    _no_network: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_TELEMETRY_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_CONFIG", str(tmp_path / "t.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "tid"))

    from lemoncrow.core.service.telemetry import emit_product
    from lemoncrow.core.service.telemetry.config import remote_enabled

    assert remote_enabled() is False
    # Writes to the local SQLite store only; the _no_network guard proves no
    # outbound call happens.
    emit_product("cli_command_invoked", command_name="x", session_id="s", anon_id="a")


def test_public_rollup_blocked_when_telemetry_off(_no_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TELEMETRY", "0")
    from lemoncrow.core.service.telemetry.public_rollup import publish_public_savings_rollup

    assert (
        publish_public_savings_rollup(
            session_id="s", saved_usd=1.0, tokens_saved=1, calls_avoided=1, turn_count=1, source="claude"
        )
        is False
    )
