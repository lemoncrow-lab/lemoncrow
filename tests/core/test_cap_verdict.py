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


def _use_key(monkeypatch: pytest.MonkeyPatch, pub: str) -> None:
    # Inject the test public key by patching the compiled gate's accessor. There
    # is NO env override anymore (LEMONCROW_CAP_PUBLIC_KEY was a bypass — point it
    # at your own key, self-sign an under-cap verdict), so tests patch the fn.
    from lemoncrow.pro.capabilities import licensing_gate as _g

    monkeypatch.setattr(_g, "_public_key_hex", lambda: pub)


def test_cap_exhausted_trusts_signed_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: True)
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # signed over-cap -> dormant


def test_cap_exhausted_signed_under(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: True)
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False


def test_cap_exhausted_fail_closed_on_expired_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: True)
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=1), private_key_hex=priv)  # long expired
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # present but untrusted -> fail-CLOSED dormant


# --- free tier: ALWAYS token-gated (enforcement compiled in, no rollout flag) --
def test_free_under_cap_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False  # valid under-cap free verdict -> active


def test_free_over_cap_dormant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # signed over-cap -> dormant


def test_free_no_token_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Free + pinned key + no token (offline / never checked in) -> fail CLOSED
    # (built-in only). Enforcement is always on; no local-meter free ride.
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    assert pr.cap_exhausted(tmp_path) is True


def test_free_expired_token_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=1), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # expired -> untrusted -> fail-CLOSED


def test_free_local_meter_only_when_no_key_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Defensive: with NO pinned key (never true in a real build) verification is
    # impossible, so the gate falls to the local meter instead of bricking.
    from lemoncrow.pro.capabilities import licensing_gate as _g

    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    monkeypatch.setattr(_g, "_public_key_hex", lambda: "")
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
    _use_key(monkeypatch, pub)
    _seed_token(tmp_path, "not-a-real-signed-token")
    assert _gate.cap_exhausted(tmp_path) is True
