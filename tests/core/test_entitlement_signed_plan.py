"""Signed entitlement: pro is granted only by a valid signed plan token.

Closes the forge-pro bypass (edit auth.json / `account login --dev` at a self-run
localhost server) once _REQUIRE_SIGNED_PLAN is on.
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.core.capabilities.licensing import cap_verdict as cv
from lemoncrow.core.capabilities.licensing import entitlements as ent
from lemoncrow.pro.capabilities import licensing_gate as g


def _keypair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_hex = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    ).hex()
    pub_hex = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv_hex, pub_hex


def _use_key(monkeypatch: pytest.MonkeyPatch, pub: str) -> None:
    monkeypatch.setattr(g, "_public_key_hex", lambda: pub)


def _plan_token(priv: str, plan: str, *, expires: int) -> str:
    return cv.sign_cap_token({"plan": plan, "account_id": "acct_1", "expires_at": expires}, private_key_hex=priv)


# --- plan_from_token (compiled gate) ----------------------------------------
def test_plan_from_token_valid() -> None:
    priv, pub = _keypair()
    tok = _plan_token(priv, "pro", expires=2000)
    assert g.plan_from_token(tok, now=1500, public_key_hex=pub) == "pro"


def test_plan_from_token_expired_is_none() -> None:
    priv, pub = _keypair()
    tok = _plan_token(priv, "pro", expires=2000)
    assert g.plan_from_token(tok, now=2000, public_key_hex=pub) is None


def test_plan_from_token_wrong_key_is_none() -> None:
    priv, _ = _keypair()
    _, other_pub = _keypair()
    tok = _plan_token(priv, "pro", expires=2000)
    assert g.plan_from_token(tok, now=1500, public_key_hex=other_pub) is None


def test_plan_from_token_missing_is_none() -> None:
    _, pub = _keypair()
    assert g.plan_from_token(None, now=1, public_key_hex=pub) is None
    assert g.plan_from_token("garbage", now=1, public_key_hex=pub) is None


# --- _entitled_plan (rollout semantics) -------------------------------------
def test_signed_pro_overrides_unsigned_free(monkeypatch: pytest.MonkeyPatch) -> None:
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = _plan_token(priv, "pro", expires=int(time.time()) + 3600)
    # Even if the unsigned field lies "free", the SIGNED token wins.
    assert ent._entitled_plan({"plan": "free", "plan_token": tok}) == "pro"


def test_forged_pro_no_token_rollout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # Phase 1 (default): no signed token -> unsigned plan is still honoured, so a
    # pre-rollout pro session keeps working (the bypass is NOT yet closed).
    monkeypatch.setattr(ent, "_REQUIRE_SIGNED_PLAN", False)
    assert ent._entitled_plan({"plan": "pro"}) == "pro"


def test_forged_pro_no_token_enforced_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    # Phase 2: signed-only. An unsigned "pro" (forged auth.json / --dev localhost)
    # can no longer grant pro.
    monkeypatch.setattr(ent, "_REQUIRE_SIGNED_PLAN", True)
    assert ent._entitled_plan({"plan": "pro"}) == "free"


def test_invalid_token_never_trusts_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    # A token is present but doesn't verify (forged/expired) -> distrust and drop
    # to free, regardless of the rollout switch or the unsigned claim.
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    monkeypatch.setattr(ent, "_REQUIRE_SIGNED_PLAN", False)
    assert ent._entitled_plan({"plan": "pro", "plan_token": "forged.token"}) == "free"


def test_expired_signed_token_drops_to_free(monkeypatch: pytest.MonkeyPatch) -> None:
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = _plan_token(priv, "pro", expires=1)  # long expired
    assert ent._entitled_plan({"plan": "pro", "plan_token": tok}) == "free"
