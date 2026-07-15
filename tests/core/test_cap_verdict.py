"""Signed cap verdict: verify/round-trip, tamper rejection, expiry fail-closed."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.core.capabilities.licensing import cap_verdict as cv


def _keypair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization

    priv_hex = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    ).hex()
    pub_hex = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv_hex, pub_hex


def _payload(*, over: bool, expires: int) -> dict:
    return {
        "account_id": "acct_1",
        "savings_over_cap": over,
        "monthly_savings_usd": 42.0,
        "cap_usd": 20.0,
        "issued_at": 1000,
        "expires_at": expires,
    }


def test_roundtrip_over_and_under() -> None:
    priv, pub = _keypair()
    tok_over = cv.sign_cap_token(_payload(over=True, expires=2000), private_key_hex=priv)
    tok_under = cv.sign_cap_token(_payload(over=False, expires=2000), private_key_hex=priv)
    assert cv.cap_over_from_token(tok_over, now=1500, public_key_hex=pub) is True
    assert cv.cap_over_from_token(tok_under, now=1500, public_key_hex=pub) is False


def test_expired_token_is_none_fail_closed() -> None:
    priv, pub = _keypair()
    tok = cv.sign_cap_token(_payload(over=False, expires=2000), private_key_hex=priv)
    assert cv.cap_over_from_token(tok, now=2000, public_key_hex=pub) is None  # at expiry
    assert cv.cap_over_from_token(tok, now=9999, public_key_hex=pub) is None  # past


def test_tampered_payload_rejected() -> None:
    priv, pub = _keypair()
    tok = cv.sign_cap_token(_payload(over=True, expires=2000), private_key_hex=priv)
    # flip the payload half (b64url) -> signature no longer matches
    _head, sig = tok.split(".", 1)
    forged = cv._b64url_encode(b'{"savings_over_cap":false,"expires_at":9999999999}') + "." + sig
    assert cv.verify_cap_token(forged, public_key_hex=pub) is None
    assert cv.cap_over_from_token(forged, now=1500, public_key_hex=pub) is None


def test_wrong_key_rejected() -> None:
    priv, _ = _keypair()
    _, other_pub = _keypair()
    tok = cv.sign_cap_token(_payload(over=True, expires=2000), private_key_hex=priv)
    assert cv.cap_over_from_token(tok, now=1500, public_key_hex=other_pub) is None


def test_missing_or_garbage_token() -> None:
    _, pub = _keypair()
    assert cv.cap_over_from_token(None, now=1, public_key_hex=pub) is None
    assert cv.cap_over_from_token("", now=1, public_key_hex=pub) is None
    assert cv.cap_over_from_token("garbage", now=1, public_key_hex=pub) is None


def test_no_pinned_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    priv, _ = _keypair()
    tok = cv.sign_cap_token(_payload(over=True, expires=2000), private_key_hex=priv)
    # empty public key -> cannot verify -> None (not a silent pass)
    assert cv.cap_over_from_token(tok, now=1500, public_key_hex="") is None


# --- cap_exhausted integration (signed token beats local) -------------------
import time as _t  # noqa: E402
from pathlib import Path  # noqa: E402

from lemoncrow.core.capabilities import plugin_runtime as pr  # noqa: E402
from lemoncrow.core.capabilities.licensing import entitlements  # noqa: E402


def _seed_token(root: Path, token: str) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, auth_state_path

    _write_json(
        auth_state_path(root), {"authenticated": True, "subscriptionStatus": {"plan": "pro", "capVerdictToken": token}}
    )


def test_cap_exhausted_trusts_signed_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: True)  # signed-token grace requires established pro
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # signed over-cap -> dormant


def test_cap_exhausted_signed_under(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: True)
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False


def test_cap_exhausted_fail_closed_on_expired_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: True)
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=1), private_key_hex=priv)  # long expired
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # present but untrusted -> fail-CLOSED dormant


def test_cap_exhausted_local_fallback_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": True})
    assert pr.cap_exhausted(tmp_path) is True  # no token -> local meter


def test_free_machine_gets_no_signed_token_grace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A never-pro machine must NOT ride a still-valid signed token offline. Even
    # with a validly-signed UNDER-cap token (which would say "active"), a free
    # machine ignores it and falls to the local meter -> over local cap ->
    # dormant. Proves the 24 h TTL grace is gated on established pro.
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": True})
    assert pr.cap_exhausted(tmp_path) is True  # local meter used, token grace NOT applied


# --- free-tier token enforcement (rollout flag LEMONCROW_FREE_ENFORCED) ------
def test_free_enforced_under_cap_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    monkeypatch.setenv("LEMONCROW_FREE_ENFORCED", "1")
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False  # valid under-cap free verdict -> active


def test_free_enforced_over_cap_dormant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    monkeypatch.setenv("LEMONCROW_FREE_ENFORCED", "1")
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # signed over-cap -> dormant


def test_free_enforced_no_token_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    monkeypatch.setenv("LEMONCROW_FREE_ENFORCED", "1")
    _priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    # No token seeded: offline / never-checked-in free machine -> built-in only.
    assert pr.cap_exhausted(tmp_path) is True


def test_free_enforced_expired_token_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    monkeypatch.setenv("LEMONCROW_FREE_ENFORCED", "1")
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=1), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # expired -> untrusted -> fail-CLOSED


def test_free_enforced_off_by_default_uses_local_meter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Default (flag unset): a no-token free machine uses the local meter and is
    # NOT bricked. This is the pre-rollout safety net.
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    monkeypatch.delenv("LEMONCROW_FREE_ENFORCED", raising=False)
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": False})
    assert pr.cap_exhausted(tmp_path) is False  # local meter, under cap -> active


def test_free_enforced_but_not_configured_uses_local_meter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Flag ON but no pinned key -> cannot verify -> local meter (never brick when
    # verification is impossible).
    from lemoncrow.pro.capabilities import licensing_gate as _gate

    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    monkeypatch.setenv("LEMONCROW_FREE_ENFORCED", "1")
    monkeypatch.setattr(_gate, "_public_key_hex", lambda: "")
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": False})
    assert pr.cap_exhausted(tmp_path) is False


# --- Compiled-gate wiring: the .so is the single authority -------------------
from lemoncrow.pro.capabilities import licensing_gate as _gate  # noqa: E402


def test_cap_verdict_reexports_the_compiled_gate() -> None:
    # The open module is a thin surface over the compiled gate, not a 2nd impl:
    # the verify path + pinned key ship as .so and cannot be edited in source.
    assert cv.verify_cap_token is _gate.verify_cap_token
    assert cv.cap_over_from_token is _gate.cap_over_from_token
    assert cv.is_configured is _gate.is_configured


def test_plugin_runtime_delegates_to_the_compiled_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Patching the compiled gate flips plugin_runtime's result -> pr.cap_exhausted
    # is a delegator to the .so, not a stale copy of the decision logic.
    monkeypatch.setattr(_gate, "cap_exhausted", lambda _root: True)
    assert pr.cap_exhausted(tmp_path) is True
    monkeypatch.setattr(_gate, "cap_exhausted", lambda _root: False)
    assert pr.cap_exhausted(tmp_path) is False


def test_gate_fail_closed_when_pinned_key_present_but_token_untrusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Established-pro machine, pinned key, garbage/forged token -> no trustworthy
    # verdict -> the compiled gate reports dormant (fail-CLOSED). Blocking or
    # spoofing the server cannot yield "free forever" for a pro machine.
    monkeypatch.setattr(entitlements, "is_pro", lambda: True)
    _, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    _seed_token(tmp_path, "not-a-real-signed-token")
    assert _gate.cap_exhausted(tmp_path) is True
