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
    _establish(root)  # having a token means the machine is established


def _establish(root: Path) -> None:
    # Mark the machine as "established free" (has received a server token before),
    # so the gate token-gates it instead of using the local meter.
    (root / ".cap_established").touch()


def _use_key(monkeypatch: pytest.MonkeyPatch, pub: str) -> None:
    # Inject the test public key by patching the compiled gate's accessor. There
    # is NO env override anymore (LEMONCROW_CAP_PUBLIC_KEY was a bypass — point it
    # at your own key, self-sign an under-cap verdict), so tests patch the fn.
    from lemoncrow.pro.capabilities import licensing_gate as _g

    monkeypatch.setattr(_g, "_public_key_hex", lambda: pub)


def test_cap_exhausted_trusts_signed_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # signed over-cap -> dormant


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


def test_free_over_cap_dormant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        entitlements,
        "current_identity",
        lambda: ("acct_1", "device_1", "free"),
    )
    priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    tok = cv.sign_cap_token(_payload(over=True, expires=int(_t.time()) + 3600), private_key_hex=priv)
    _seed_token(tmp_path, tok)
    assert pr.cap_exhausted(tmp_path) is True  # signed over-cap -> dormant


def test_free_established_no_token_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Established free machine (has checked in before) + no valid token now
    # (offline / expired-away) -> fail CLOSED (built-in only).
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    _establish(tmp_path)
    assert pr.cap_exhausted(tmp_path) is True


def test_free_never_established_uses_local_meter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A fresh machine that has NEVER received a token (new install / dev / CI) is
    # NOT bricked: it falls to the local meter (active under cap), never fail-
    # closed. This is what keeps un-provisioned environments working.
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": False})
    assert pr.cap_exhausted(tmp_path) is False  # not established -> local meter -> active


def test_free_grace_over_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Never established BUT session history older than the grace window -> the box
    # has had time to check in -> fail CLOSED without a valid token. Ties the
    # grace to session-file age, so faking "fresh" means deleting all sessions.
    import os

    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    sess = tmp_path / "sessions" / "s1"
    sess.mkdir(parents=True)
    f = sess / "savings.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    old = _t.time() - (49 * 3600)  # older than the 48h grace
    os.utime(f, (old, old))
    assert pr.cap_exhausted(tmp_path) is True  # grace over + no token -> fail closed


def test_free_recent_sessions_within_grace_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Recent session history (within grace), never established -> local meter.
    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    sess = tmp_path / "sessions" / "s1"
    sess.mkdir(parents=True)
    (sess / "savings.jsonl").write_text("{}\n", encoding="utf-8")  # fresh mtime
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": False})
    assert pr.cap_exhausted(tmp_path) is False  # within grace -> local meter -> active


def test_free_fresh_install_with_old_claude_history_not_bricked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # REGRESSION: a fresh LemonCrow install (no sessions/ ledger) on a box with a
    # long pre-existing ~/.claude history must NOT be treated as long-lived and
    # bricked. Grace is keyed to LemonCrow's own install age, not host CLI history
    # (essentially every real free user has old host transcripts).
    import os

    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    proj = tmp_path / "claude_home" / "projects" / "-home-user-proj"
    proj.mkdir(parents=True)
    tr = proj / "11111111-2222-3333-4444-555555555555.jsonl"
    tr.write_text('{"type":"user"}\n', encoding="utf-8")
    old = _t.time() - (49 * 3600)  # old host history, but NOT LemonCrow's
    os.utime(tr, (old, old))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home"))
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": False})
    assert pr.cap_exhausted(tmp_path) is False  # fresh lc install -> local meter -> active


def test_free_fresh_install_with_old_codex_history_not_bricked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Same regression, with old Codex history under $CODEX_HOME.
    import os

    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    sdir = tmp_path / "codex_home" / "sessions" / "2026" / "01" / "01"
    sdir.mkdir(parents=True)
    f = sdir / "rollout-2026-01-01T00-00-00-abc.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    old = _t.time() - (49 * 3600)
    os.utime(f, (old, old))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": False})
    assert pr.cap_exhausted(tmp_path) is False  # fresh lc install -> local meter -> active


def test_free_grace_over_from_lemoncrow_sessions_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Grace IS over once LemonCrow's OWN ledger is older than the window (this
    # install has run >48h without a server token) -> fail CLOSED.
    import os

    monkeypatch.setattr(entitlements, "is_pro", lambda: False)
    _priv, pub = _keypair()
    _use_key(monkeypatch, pub)
    sess = tmp_path / "sessions" / "s1"
    sess.mkdir(parents=True)
    ledger = sess / "savings.jsonl"
    ledger.write_text("{}\n", encoding="utf-8")
    old = _t.time() - (49 * 3600)
    os.utime(ledger, (old, old))
    assert pr.cap_exhausted(tmp_path) is True  # own ledger past grace + no token -> fail closed


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
