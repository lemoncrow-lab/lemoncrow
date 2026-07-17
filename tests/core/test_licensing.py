"""Open-source runtime: the licensing / entitlement layer is always unlocked.

The former OAuth-gated, signed-plan verification was neutralized (see
docs/maintenance-mode-transition.md). ``is_pro`` / ``has_feature`` / ``require``
now resolve locally to "granted" for every plan and every sign-in state, with no
network fetch, and ``licensing.pro_url`` was removed entirely. These tests pin
that unlocked contract so a regression back into gating is caught.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.core.capabilities import licensing
from lemoncrow.core.capabilities.licensing import cap_verdict as cv
from lemoncrow.core.capabilities.licensing import entitlements, store
from lemoncrow.pro.capabilities import licensing_gate as _gate

# Test signing keypair — retained for the sign-in helper below. The runtime no
# longer verifies these (everything is unlocked locally), but signing a token
# keeps the helper realistic for callers that still stash a plan token on disk.
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


def test_signed_out_is_fully_unlocked() -> None:
    """Signed out is unlocked now — no feature is gated locally and require never raises."""
    assert licensing.is_pro() is True
    assert licensing.has_feature("optimizer") is True
    assert licensing.has_feature("search") is True
    assert licensing.has_feature("session_recall") is True
    assert licensing.has_feature("swarm") is True
    assert licensing.require("optimizer") is None  # never raises FeatureLocked
    st = licensing.status()
    assert st.licensed is True and st.valid is True and st.plan == "oss"


def test_signed_in_pro_is_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="pro")
    assert licensing.is_pro() is True
    assert licensing.has_feature("optimizer") is True
    assert licensing.require("optimizer") is None  # never raises
    assert licensing.has_feature("governance") is True  # pro now has everything
    st = licensing.status()
    assert st.valid is True and st.plan == "oss"


def test_free_plan_is_also_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="free")
    assert licensing.is_pro() is True
    assert licensing.has_feature("optimizer") is True
    assert licensing.has_feature("source_projection") is True
    assert licensing.has_feature("session_recall") is True
    assert licensing.has_feature("swarm") is True
    assert licensing.has_feature("unknown_paid_typo") is True  # every name is granted
    assert licensing.status().plan == "oss"


def test_legacy_lite_plan_is_fully_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="lite")
    assert licensing.is_pro() is True
    assert licensing.has_feature("code_search") is True
    assert licensing.has_feature("session_recall") is True
    assert licensing.has_feature("optimizer") is True
    assert licensing.has_feature("context_engine") is True
    assert licensing.has_feature("cross_vendor_memory") is True
    assert licensing.has_feature("savings_dashboard") is True
    assert licensing.has_feature("governance") is True


def test_every_plan_now_inherits_all_features(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="pro")
    assert licensing.has_feature("model_routing") is True
    assert licensing.has_feature("cross_vendor_routing") is True
    assert licensing.has_feature("swarm") is True
    assert licensing.has_feature("large_repo") is True
    assert licensing.has_feature("shared_context") is True
    assert licensing.has_feature("governance") is True


def test_enterprise_plan_unlocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(monkeypatch, plan="enterprise")
    assert licensing.is_pro() is True
    assert licensing.has_feature("governance") is True
    assert licensing.has_feature("model_routing") is True
    assert licensing.has_feature("cross_vendor_routing") is True


def test_reload_does_no_network_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """entitlements.reload() is a no-op: no /api/auth/me fetch, still unlocked."""
    monkeypatch.setenv("LEMONCROW_AUTH_TOKEN", "session-token")

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("the OSS runtime must never fetch entitlements over the network")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    entitlements.reload()
    assert licensing.is_pro() is True
    assert store.load_auth_user() is None  # nothing was fetched or cached


def test_offline_never_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Being offline can never lock features — entitlements are local-only."""
    monkeypatch.setenv("LEMONCROW_AUTH_TOKEN", "session-token")

    def _offline(*args: object, **kwargs: object) -> object:
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", _offline)
    entitlements.reload()
    assert licensing.is_pro() is True
    assert licensing.has_feature("optimizer") is True


def test_refresh_plan_is_a_noop_and_stays_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """refresh_plan() is a no-op; a free sign-in is already fully unlocked."""
    _sign_in(monkeypatch, plan="free")
    assert licensing.is_pro() is True
    licensing.refresh_plan()  # no-op
    assert licensing.is_pro() is True


def test_license_grants_scoped_features() -> None:
    lic = licensing.License(license_id="1", email="e", plan="pro", features=("model_routing",))
    assert lic.grants("model_routing") is True
    assert lic.grants("optimizer") is False


def test_pro_url_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The purchase-URL indirection (licensing.pro_url) was removed entirely."""
    monkeypatch.delenv("LEMONCROW_PRO_URL", raising=False)
    assert not hasattr(licensing, "pro_url")
    assert not hasattr(entitlements, "pro_url")


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
