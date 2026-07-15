"""Versioned account/device-bound signed plan entitlement tests."""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.core.capabilities.licensing import cap_verdict as cv
from lemoncrow.core.capabilities.licensing import entitlements as ent
from lemoncrow.pro.capabilities import licensing_gate as gate


def _keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_hex = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ).hex()
    public_hex = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return private_hex, public_hex


def _use_key(monkeypatch: pytest.MonkeyPatch, public_hex: str) -> None:
    monkeypatch.setattr(gate, "_public_key_hex", lambda: public_hex)
    monkeypatch.setattr(ent.store, "load_or_create_device_id", lambda: "device_1")


def _plan_token(
    private_hex: str,
    plan: str,
    *,
    expires: int,
    issued: int = 1000,
    account_id: str = "acct_1",
    device_id: str = "device_1",
    token_type: str = "plan",
    version: int = 2,
) -> str:
    return cv.sign_cap_token(
        {
            "v": version,
            "typ": token_type,
            "plan": plan,
            "account_id": account_id,
            "device_id": device_id,
            "issued_at": issued,
            "expires_at": expires,
        },
        private_key_hex=private_hex,
    )


def _verify(token: str | None, public_hex: str, *, now: int = 1500) -> str | None:
    return gate.plan_from_token(
        token,
        now=now,
        account_id="acct_1",
        device_id="device_1",
        public_key_hex=public_hex,
    )


def test_plan_from_token_valid() -> None:
    private, public = _keypair()
    assert _verify(_plan_token(private, "pro", expires=2000), public) == "pro"


def test_plan_from_token_expired_is_none() -> None:
    private, public = _keypair()
    assert _verify(_plan_token(private, "pro", expires=2000), public, now=2000) is None


def test_plan_from_token_wrong_key_is_none() -> None:
    private, _ = _keypair()
    _, other_public = _keypair()
    assert _verify(_plan_token(private, "pro", expires=2000), other_public) is None


def test_plan_from_token_missing_is_none() -> None:
    _, public = _keypair()
    assert _verify(None, public, now=1) is None
    assert _verify("garbage", public, now=1) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_id": "acct_other"},
        {"device_id": "device_other"},
        {"token_type": "cap"},
        {"version": 1},
        {"plan": "attacker"},
    ],
)
def test_plan_token_binding_and_schema_fail_closed(overrides: dict[str, object]) -> None:
    private, public = _keypair()
    kwargs: dict[str, object] = {"expires": 2000, **overrides}
    token = _plan_token(private, str(kwargs.pop("plan", "pro")), **kwargs)  # type: ignore[arg-type]
    assert _verify(token, public) is None


def test_plan_token_rejects_excessive_lifetime() -> None:
    private, public = _keypair()
    token = _plan_token(private, "pro", issued=1000, expires=1000 + 9 * 3600 + 1)
    assert _verify(token, public, now=1500) is None


def test_signed_pro_overrides_unsigned_free(monkeypatch: pytest.MonkeyPatch) -> None:
    now = int(time.time())
    private, public = _keypair()
    _use_key(monkeypatch, public)
    token = _plan_token(private, "pro", issued=now, expires=now + 3600)
    assert (
        ent._entitled_plan(
            {
                "user_id": "acct_1",
                "device_id": "device_1",
                "plan": "free",
                "plan_token": token,
            }
        )
        == "pro"
    )


def test_forged_pro_without_token_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    _, public = _keypair()
    _use_key(monkeypatch, public)
    assert ent._entitled_plan({"user_id": "acct_1", "device_id": "device_1", "plan": "pro"}) == "free"


def test_copied_plan_token_on_another_device_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    private, public = _keypair()
    _use_key(monkeypatch, public)
    token = _plan_token(private, "pro", issued=now, expires=now + 3600)
    assert (
        ent._entitled_plan(
            {
                "user_id": "acct_1",
                "device_id": "device_other",
                "plan": "pro",
                "plan_token": token,
            }
        )
        == "free"
    )


def test_invalid_token_never_trusts_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    _, public = _keypair()
    _use_key(monkeypatch, public)
    assert (
        ent._entitled_plan(
            {
                "user_id": "acct_1",
                "device_id": "device_1",
                "plan": "pro",
                "plan_token": "forged.token",
            }
        )
        == "free"
    )


def test_expired_signed_token_drops_to_free(monkeypatch: pytest.MonkeyPatch) -> None:
    private, public = _keypair()
    _use_key(monkeypatch, public)
    token = _plan_token(private, "pro", issued=0, expires=1)
    assert (
        ent._entitled_plan(
            {
                "user_id": "acct_1",
                "device_id": "device_1",
                "plan": "pro",
                "plan_token": token,
            }
        )
        == "free"
    )
