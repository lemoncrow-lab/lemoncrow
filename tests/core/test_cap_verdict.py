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


def _seed_token(root: Path, token: str) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, auth_state_path

    _write_json(auth_state_path(root), {"authenticated": True, "subscriptionStatus": {"plan": "pro", "capVerdictToken": token}})


def test_cap_exhausted_trusts_signed_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # signed over-cap -> dormant


def test_cap_exhausted_signed_under(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False


def test_cap_exhausted_fail_closed_on_expired_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    priv, pub = _keypair()
    monkeypatch.setenv("LEMONCROW_CAP_PUBLIC_KEY", pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=1), private_key_hex=priv)  # long expired
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # present but untrusted -> fail-CLOSED dormant


def test_cap_exhausted_local_fallback_without_token(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": True})
    assert pr.cap_exhausted(tmp_path) is True  # no token -> local meter
