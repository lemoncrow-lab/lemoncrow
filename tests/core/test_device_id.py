"""Stable device identity, including container/CI propagation via env.

Kept separate from test_licensing.py, whose autouse fixture replaces
``load_or_create_device_id`` wholesale — these tests exercise the real one.
"""

from __future__ import annotations

import pytest

from lemoncrow.core.capabilities.licensing import store


def test_device_id_env_override_wins_with_a_real_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Containers forward the host's device id ALONGSIDE its auth token (the
    one the account, plan token, and cap verdict are bound to); the
    container's own /etc/machine-id must not be consulted or the plan
    degrades to free inside every benchmark run."""
    monkeypatch.setenv(store.AUTH_TOKEN_ENV_VAR, "real-account-token")
    monkeypatch.setenv(store.DEVICE_ID_ENV_VAR, "abcdef123456")
    assert store.load_or_create_device_id() == "abcdef123456"


def test_device_id_env_override_ignored_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """SECURITY: with no auth token, the caller is the anonymous identity --
    and the server derives that identity's account_id FROM the device id
    (sha256), not from anything account-keyed. Honoring the override here
    would let anyone mint an unlimited stream of fresh $50 anonymous caps
    just by exporting one env var, no root/hardware change required (unlike
    the OS machine-id it would otherwise bypass). Must always fall through
    to the real OS-derived id in this case."""
    monkeypatch.delenv(store.AUTH_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv(store.DEVICE_ID_ENV_VAR, "abcdef123456")
    assert store.load_or_create_device_id() != "abcdef123456"


def test_device_id_env_override_rejects_malformed_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(store.AUTH_TOKEN_ENV_VAR, "real-account-token")
    for bad in ("has spaces", "x" * 65, "abc", "semi;colon", ""):
        monkeypatch.setenv(store.DEVICE_ID_ENV_VAR, bad)
        assert store.load_or_create_device_id() != bad


def test_device_id_is_stable_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(store.DEVICE_ID_ENV_VAR, raising=False)
    first = store.load_or_create_device_id()
    assert first == store.load_or_create_device_id()
    assert len(first) >= 4


def test_anonymous_binding_ignores_device_id_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SECURITY: the anonymous cap verdict binds to the NON-overridable machine
    id, so a signed anon verdict can't be replayed on another machine by
    exporting LEMONCROW_DEVICE_ID (plus a throwaway token to satisfy the
    authenticated override gate)."""
    import hashlib

    from lemoncrow.pro.capabilities.licensing_gate import _anonymous_device_hash

    monkeypatch.delenv(store.AUTH_TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(store.DEVICE_ID_ENV_VAR, raising=False)
    expected = hashlib.sha256(store.stable_machine_device_id().encode("utf-8")).hexdigest()

    # Attacker activates the override with a junk token + a forged device id.
    monkeypatch.setenv(store.AUTH_TOKEN_ENV_VAR, "junk-not-a-real-token")
    monkeypatch.setenv(store.DEVICE_ID_ENV_VAR, "deadbeef1234")
    assert store.load_or_create_device_id() == "deadbeef1234"  # override active on the auth path
    # ...but the anonymous binding stays pinned to the real machine id.
    assert _anonymous_device_hash() == expected
