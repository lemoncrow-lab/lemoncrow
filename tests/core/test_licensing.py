"""Tests for the open-core licensing / entitlement layer (OAuth-only)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.core.capabilities import licensing
from lemoncrow.core.capabilities.licensing import cap_verdict as cv
from lemoncrow.core.capabilities.licensing import entitlements, store
from lemoncrow.pro.capabilities import licensing_gate as _gate

# Test signing keypair — the gate's pinned public key is patched to _PUB_HEX in
# the autouse fixture, so plan tokens signed here verify. Pro is signed-only now.
_PRIV = Ed25519PrivateKey.generate()
_PRIV_HEX = _PRIV.private_bytes(
    serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
).hex()
_PUB_HEX = _PRIV.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _plan_token(plan: str) -> str:
    import time

    return cv.sign_cap_token(
        {
            "v": 2,
            "typ": "plan",
            "plan": plan,
            "account_id": "u_1",
            "device_id": "device_1",
            "issued_at": int(time.time()),
            "expires_at": int(time.time()) + 3600,
        },
        private_key_hex=_PRIV_HEX,
    )


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(_gate, "_public_key_hex", lambda: _PUB_HEX)
    monkeypatch.setattr(store, "load_or_create_device_id", lambda: "device_1")
    entitlements.reload()
    yield
    entitlements.reload()


def _sign_in(monkeypatch: pytest.MonkeyPatch, *, plan: str, email: str = "dev@example.com") -> None:
    monkeypatch.setenv("LEMONCROW_AUTH_TOKEN", "session-token")
    store.save_auth_user(
        {
            "user_id": "u_1",
            "device_id": "device_1",
            "email": email,
            "plan": plan,
            "plan_token": _plan_token(plan),
        }
    )
    entitlements.reload()


def test_signed_out_locks_pro_features() -> None:
    assert licensing.is_pro() is False
    assert licensing.has_feature("optimizer") is False
    # Non-Pro capabilities are always allowed.
    assert licensing.has_feature("search") is True
    with pytest.raises(licensing.FeatureLocked):
        licensing.require("optimizer")
    st = licensing.status()
    assert st.licensed is False and st.reason == "not signed in" and st.source == "none"


def test_pro_plan_unlocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="pro")
    assert licensing.is_pro() is True
    assert licensing.has_feature("optimizer") is True
    licensing.require("optimizer")  # does not raise
    st = licensing.status()
    assert st.valid and st.plan == "pro" and st.email == "dev@example.com"
    assert st.source == "env"


def test_free_plan_stays_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="free")
    assert licensing.is_pro() is False
    assert licensing.has_feature("optimizer") is False
    assert licensing.has_feature("source_projection") is True
    assert licensing.has_feature("unknown_paid_typo") is False
    assert licensing.status().reason == "signed in on the free plan"


def test_lite_plan_has_only_lite_features(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="lite")
    assert licensing.is_pro() is True
    assert licensing.has_feature("code_search") is True
    assert licensing.has_feature("session_recall") is True
    assert licensing.has_feature("optimizer") is True
    assert licensing.has_feature("context_engine") is False
    assert licensing.has_feature("cross_vendor_memory") is False
    assert licensing.has_feature("savings_dashboard") is False
    assert licensing.has_feature("governance") is False


def test_pro_does_not_inherit_enterprise_features(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="pro")
    assert licensing.has_feature("model_routing") is True
    assert licensing.has_feature("swarm") is True
    assert licensing.has_feature("large_repo") is False
    assert licensing.has_feature("shared_context") is False
    assert licensing.has_feature("governance") is False


def test_enterprise_plan_unlocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="enterprise")
    assert licensing.is_pro() is True
    assert licensing.has_feature("governance") is True


def test_fetch_populates_cache_when_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_AUTH_TOKEN", "session-token")

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "user_id": "u_1",
                    "device_id": "device_1",
                    "email": "d@e.com",
                    "plan": "pro",
                    "plan_token": _plan_token("pro"),
                }
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    entitlements.reload()
    assert licensing.is_pro() is True
    cached: dict[str, Any] | None = store.load_auth_user()
    assert cached is not None and cached["plan"] == "pro"


def test_offline_without_cache_stays_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_AUTH_TOKEN", "session-token")

    def _offline(*args: object, **kwargs: object) -> object:
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", _offline)
    entitlements.reload()
    assert licensing.is_pro() is False
    assert "could not verify" in licensing.status().reason


def test_refresh_plan_picks_up_fresh_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pay → use immediately: a locked check re-fetches live past the 6 h cache."""
    _sign_in(monkeypatch, plan="free")
    assert licensing.is_pro() is False

    # The purchase lands server-side while the fresh disk cache still says free.
    class _Resp:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "user_id": "u_1",
                    "device_id": "device_1",
                    "email": "dev@example.com",
                    "plan": "pro",
                    "plan_token": _plan_token("pro"),
                }
            ).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    licensing.refresh_plan()
    assert licensing.is_pro() is True


def test_license_grants_scoped_features() -> None:
    lic = licensing.License(license_id="1", email="e", plan="pro", features=("model_routing",))
    assert lic.grants("model_routing") is True
    assert lic.grants("optimizer") is False


def test_pro_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_PRO_URL", raising=False)
    assert licensing.pro_url() == "https://lemoncrow.com/pro"
    monkeypatch.setenv("LEMONCROW_PRO_URL", "https://buy.example.com/pro")
    assert licensing.pro_url() == "https://buy.example.com/pro"


def test_login_declined_marker_round_trips() -> None:
    assert store.is_login_declined() is False
    store.mark_login_declined()
    assert store.is_login_declined() is True
    store.clear_login_declined()
    assert store.is_login_declined() is False


def test_save_auth_token_clears_declined_marker() -> None:
    """`lc account login` / `lc init` (without --no-login) un-declines a prior --no-login."""
    store.mark_login_declined()
    assert store.is_login_declined() is True
    store.save_auth_token("a-token")
    assert store.is_login_declined() is False
