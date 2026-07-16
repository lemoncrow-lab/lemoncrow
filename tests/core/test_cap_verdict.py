"""Signed cap verdict: verify/round-trip, tamper rejection, expiry fail-closed."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.core.capabilities.licensing import cap_verdict as cv

_TYPESCRIPT_TEST_SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
_TYPESCRIPT_FREE_TOKEN = (
    "eyJ2IjoyLCJ0eXAiOiJjYXAiLCJhY2NvdW50X2lkIjoiYWNjdF9yZXZpZXciLCJkZXZpY2VfaWQiOiJkZXZpY2Vf"
    "cmV2aWV3IiwicGxhbiI6ImZyZWUiLCJzYXZpbmdzX292ZXJfY2FwIjpmYWxzZSwibW9udGhseV9zYXZpbmdzX3Vz"
    "ZCI6OTk5OSwiY2FwX3VzZCI6bnVsbCwiaXNzdWVkX2F0IjoxODAwMDAwMDAwLCJleHBpcmVzX2F0IjoxODAwMDI4"
    "ODAwfQ.pZU8v48JPhLGUi7Z3FLe9LzAkMgnNoAizj3XUMb0i9xnLbIhLa-aIiO05fINS39ZIfy7AHyRMoYXNIs8SJIeCw"
)
_TYPESCRIPT_ANONYMOUS_TOKEN = (
    "eyJ2IjoyLCJ0eXAiOiJjYXAiLCJhY2NvdW50X2lkIjoiYW5vbjpyZXZpZXciLCJkZXZpY2VfaWQiOiJhbm9ueW1v"
    "dXNfZGV2aWNlX3JldmlldyIsInBsYW4iOiJhbm9ueW1vdXMiLCJzYXZpbmdzX292ZXJfY2FwIjp0cnVlLCJtb250"
    "aGx5X3NhdmluZ3NfdXNkIjo1NSwiY2FwX3VzZCI6NTAsImlzc3VlZF9hdCI6MTgwMDAwMDAwMCwiZXhwaXJlc19h"
    "dCI6MTgwMDAyODgwMH0.Rv1w0cSvi3k188RAkC880mAyoVRmNCMh6JhVR7_N5es6OlwOxUB6pv5Jv1ba0KDEcZ"
    "qax6JeMemJEWGg-hdTBg"
)


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
        "v": 2,
        "typ": "cap",
        "account_id": "acct_1",
        "device_id": "device_1",
        "plan": "free",
        "savings_over_cap": over,
        "monthly_savings_usd": 42.0,
        "cap_usd": 20.0,
        "issued_at": expires - 3600 if expires > 100_000 else min(1000, expires - 1),
        "expires_at": expires,
    }


def _cap_over(
    token: str | None,
    *,
    now: int,
    public_key_hex: str,
    plan: str = "free",
) -> bool | None:
    return cv.cap_over_from_token(
        token,
        now=now,
        account_id="acct_1",
        device_id="device_1",
        plan=plan,
        public_key_hex=public_key_hex,
    )


def test_roundtrip_over_and_under() -> None:
    priv, pub = _keypair()
    tok_over = cv.sign_cap_token(_payload(over=True, expires=2000), private_key_hex=priv)
    tok_under = cv.sign_cap_token(_payload(over=False, expires=2000), private_key_hex=priv)
    assert _cap_over(tok_over, now=1500, public_key_hex=pub) is True
    assert _cap_over(tok_under, now=1500, public_key_hex=pub) is False


@pytest.mark.parametrize(
    ("token", "account_id", "device_id", "plan", "over", "cap"),
    [
        (_TYPESCRIPT_FREE_TOKEN, "acct_review", "device_review", "free", False, None),
        (_TYPESCRIPT_ANONYMOUS_TOKEN, "anon:review", "anonymous_device_review", "anonymous", True, 50.0),
    ],
)
def test_typescript_issuer_tokens_verify_in_python(
    token: str,
    account_id: str,
    device_id: str,
    plan: str,
    over: bool,
    cap: float | None,
) -> None:
    from cryptography.hazmat.primitives import serialization

    from lemoncrow.pro.capabilities import licensing_gate

    public_hex = (
        Ed25519PrivateKey.from_private_bytes(_TYPESCRIPT_TEST_SEED)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )
    payload = licensing_gate._cap_payload(
        token,
        now=1_800_000_001,
        account_id=account_id,
        device_id=device_id,
        plan=plan,
        public_key_hex=public_hex,
    )
    assert payload is not None
    assert payload["savings_over_cap"] is over
    assert payload["cap_usd"] == cap


def test_expired_token_is_none_fail_closed() -> None:
    priv, pub = _keypair()
    tok = cv.sign_cap_token(_payload(over=False, expires=2000), private_key_hex=priv)
    assert _cap_over(tok, now=2000, public_key_hex=pub) is None
    assert _cap_over(tok, now=9999, public_key_hex=pub) is None


def test_tampered_payload_rejected() -> None:
    priv, pub = _keypair()
    tok = cv.sign_cap_token(_payload(over=True, expires=2000), private_key_hex=priv)
    # flip the payload half (b64url) -> signature no longer matches
    _head, sig = tok.split(".", 1)
    forged = cv._b64url_encode(b'{"savings_over_cap":false,"expires_at":9999999999}') + "." + sig
    assert cv.verify_cap_token(forged, public_key_hex=pub) is None
    assert _cap_over(forged, now=1500, public_key_hex=pub) is None


def test_wrong_key_rejected() -> None:
    priv, _ = _keypair()
    _, other_pub = _keypair()
    tok = cv.sign_cap_token(_payload(over=True, expires=2000), private_key_hex=priv)
    assert _cap_over(tok, now=1500, public_key_hex=other_pub) is None


def test_missing_or_garbage_token() -> None:
    _, pub = _keypair()
    assert _cap_over(None, now=1, public_key_hex=pub) is None
    assert _cap_over("", now=1, public_key_hex=pub) is None
    assert _cap_over("garbage", now=1, public_key_hex=pub) is None


def test_no_pinned_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    priv, _ = _keypair()
    tok = cv.sign_cap_token(_payload(over=True, expires=2000), private_key_hex=priv)
    assert _cap_over(tok, now=1500, public_key_hex="") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("v", 1),
        ("typ", "plan"),
        ("account_id", "acct_other"),
        ("device_id", "device_other"),
        ("plan", "pro"),
        ("savings_over_cap", "false"),
    ],
)
def test_cap_token_schema_and_binding_fail_closed(field: str, value: object) -> None:
    private, public = _keypair()
    payload = _payload(over=False, expires=2000)
    payload[field] = value
    token = cv.sign_cap_token(payload, private_key_hex=private)
    assert _cap_over(token, now=1500, public_key_hex=public) is None


def test_plan_token_cannot_be_reused_as_cap_verdict() -> None:
    private, public = _keypair()
    payload = _payload(over=False, expires=2000)
    payload["typ"] = "plan"
    token = cv.sign_cap_token(payload, private_key_hex=private)
    assert _cap_over(token, now=1500, public_key_hex=public) is None


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


def test_authenticated_account_ignores_legacy_signed_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False  # verified account -> uncapped core


def test_cap_exhausted_signed_under(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False


def test_cap_exhausted_fail_closed_on_expired_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=1), private_key_hex=priv)  # long expired
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # present but untrusted -> fail-CLOSED dormant


# --- free tier: ALWAYS token-gated (enforcement compiled in, no rollout flag) --
def test_free_under_cap_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=False, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False  # valid under-cap free verdict -> active


def test_free_over_cap_verdict_remains_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is False  # signed-in Free is uncapped


def test_anonymous_over_cap_verdict_is_dormant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import hashlib

    from lemoncrow.core.capabilities.licensing import store

    stable_id = "stable-anonymous-machine"
    device_hash = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()
    monkeypatch.setattr(entitlements, "current_identity", lambda: None)
    monkeypatch.setattr(store, "stable_machine_device_id", lambda: stable_id)
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    payload = _payload(over=True, expires=int(_t.time()) + 3600)
    payload.update(
        account_id="anon:test-account",
        device_id=device_hash,
        plan="anonymous",
        cap_usd=50.0,
    )
    tok = cv.sign_cap_token(payload, private_key_hex=priv)
    _seed_token(tmp_path, tok)
    verdict = _gate.resolve_cap_verdict(tmp_path)
    assert verdict.dormant is True
    assert verdict.verified is True
    assert verdict.plan == "anonymous"
    assert verdict.reason == "signed_anonymous"
    assert verdict.server_cap_usd == 50.0


def test_configured_machine_without_token_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "current_identity", lambda: None)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)

    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": False})
    assert pr.cap_exhausted(tmp_path) is True


def test_free_expired_token_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
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
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
    _, pub = _keypair()
    _use_key(monkeypatch, pub)
    _seed_token(tmp_path, "not-a-real-signed-token")
    assert _gate.cap_exhausted(tmp_path) is True


# --- resolve_cap_verdict: the single central authority everything else reads --


def test_resolve_cap_verdict_signed_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "current_identity", lambda: ("acct_1", "device_1", "pro"))
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, auth_state_path

    payload = _payload(over=False, expires=int(_t.time()) + 3600)
    payload["plan"] = "pro"  # _payload() defaults to "free"; bind to the identity's plan
    tok = cv.sign_cap_token(payload, private_key_hex=priv)
    _write_json(
        auth_state_path(tmp_path),
        {"authenticated": True, "subscriptionStatus": {"plan": "pro", "capVerdictToken": tok}},
    )
    verdict = _gate.resolve_cap_verdict(tmp_path)
    assert verdict == _gate.CapVerdict(
        dormant=False,
        verified=True,
        plan="pro",
        reason="signed",
        server_saved_usd=42.0,
        server_cap_usd=None,
    )


def test_resolve_cap_verdict_no_token_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entitlements, "current_identity", lambda: None)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    verdict = _gate.resolve_cap_verdict(tmp_path)
    assert verdict == _gate.CapVerdict(dormant=True, verified=False, plan=None, reason="no_token")


def test_resolve_cap_verdict_local_fallback_when_unconfigured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_gate, "_public_key_hex", lambda: "")  # no key pinned -> dev fallback
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": False})
    verdict = _gate.resolve_cap_verdict(tmp_path)
    assert verdict == _gate.CapVerdict(dormant=False, verified=False, plan="free", reason="local_fallback")


def test_resolve_cap_verdict_gate_error_fails_closed_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, pub = _keypair()
    _use_key(monkeypatch, pub)
    monkeypatch.setattr(entitlements, "current_identity", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    verdict = _gate.resolve_cap_verdict(tmp_path)
    assert verdict == _gate.CapVerdict(dormant=True, verified=False, plan=None, reason="gate_error")


# --- compute_usage_meter picks up the same central verdict --------------------


def test_compute_usage_meter_ignores_legacy_over_cap_for_verified_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Older servers may briefly return a signed Free verdict carrying the old
    # cap. A verified account remains active and uncapped during rollout.
    monkeypatch.setattr(entitlements, "current_identity", lambda: ("acct_1", "device_1", "free"))
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)

    sub = pr.compute_usage_meter(tmp_path, subscription={"plan": "free"})
    assert sub["savingsOverCap"] is False
    assert sub["monthlySavingsCapInUsd"] is None
    assert sub["capVerdictVerified"] is True
    assert sub["capVerdictReason"] == "signed"


def test_compute_usage_meter_self_heals_from_wiped_local_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The exact "someone deleted sessions/**/savings.jsonl" scenario: no local
    # ledger exists at all (aggregate_window_savings sums to 0), yet a fresh
    # signed verdict is present. The DISPLAYED saved-so-far figure must come
    # from the server's own accumulated total (account_id-keyed server-side,
    # never a client-writable file) -- not silently reset to $0 just because
    # the local mirror is gone.
    monkeypatch.setattr(entitlements, "current_identity", lambda: ("acct_1", "device_1", "free"))
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    payload = _payload(over=False, expires=int(_t.time()) + 3600)
    payload["monthly_savings_usd"] = 17.5  # the server's true total
    payload["cap_usd"] = 20.0
    tok = cv.sign_cap_token(payload, private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert not (tmp_path / "sessions").exists()  # local ledger genuinely absent

    sub = pr.compute_usage_meter(tmp_path, subscription={"plan": "free"})
    assert sub["monthlySavingsInUsd"] == 17.5
    assert sub["monthlySavingsCapInUsd"] is None
    assert sub["savingsRemainingUsd"] is None
    assert sub["savingsOverCap"] is False


def test_compute_usage_meter_unverified_dormant_when_configured_and_no_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(entitlements, "current_identity", lambda: None)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)

    sub = pr.compute_usage_meter(tmp_path, subscription={"plan": "free"})
    assert sub["savingsOverCap"] is True
    assert sub["capVerdictVerified"] is False
    assert sub["capVerdictReason"] == "no_token"


def test_compute_usage_meter_untouched_when_unconfigured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_gate, "_public_key_hex", lambda: "")  # matches conftest's default test posture
    sub = pr.compute_usage_meter(tmp_path, subscription={"plan": "free"})
    assert sub["savingsOverCap"] is False  # 0 local savings, under any cap
    assert "capVerdictVerified" not in sub
    assert "capVerdictReason" not in sub


# --- `lc account cap` wording: honest about "no verified credential" ----------


def test_account_cap_cli_distinguishes_unverified_from_reached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from lemoncrow.gateway.cli import cli

    monkeypatch.setattr(entitlements, "current_identity", lambda: None)  # logged out, no token anywhere
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)

    root = tmp_path / ".lemoncrow"
    result = CliRunner().invoke(cli, ["--root", str(root), "account", "cap"])
    assert result.exit_code == 0, result.output
    assert "status: dormant (no verified credential" in result.output
    assert "status: reached" not in result.output
    assert "status: active" not in result.output
