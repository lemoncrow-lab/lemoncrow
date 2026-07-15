"""Shared test helpers - reusable across test files without conftest import hacks."""

from __future__ import annotations

import functools
from pathlib import Path


def grant_oauth_pro(
    monkeypatch: object,
    *,
    plan: str = "pro",
    email: str = "dev@example.com",
) -> None:
    """Simulate a signed-in OAuth session on a paid plan (no disk, no network)."""
    import time

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from lemoncrow.core.capabilities.licensing import cap_verdict, entitlements, store
    from lemoncrow.pro.capabilities import licensing_gate

    private_key = Ed25519PrivateKey.generate()
    private_hex = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ).hex()
    public_hex = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        .hex()
    )
    device_id = "test-device"
    now = int(time.time())
    plan_token = cap_verdict.sign_cap_token(
        {
            "v": 2,
            "typ": "plan",
            "account_id": "u_test",
            "device_id": device_id,
            "plan": plan,
            "issued_at": now,
            "expires_at": now + 3600,
        },
        private_key_hex=private_hex,
    )
    cap_token = cap_verdict.sign_cap_token(
        {
            "v": 2,
            "typ": "cap",
            "account_id": "u_test",
            "device_id": device_id,
            "plan": plan,
            "savings_over_cap": False,
            "monthly_savings_usd": 0.0,
            "cap_usd": None,
            "issued_at": now,
            "expires_at": now + 3600,
        },
        private_key_hex=private_hex,
    )

    monkeypatch.setattr(licensing_gate, "_public_key_hex", lambda: public_hex)  # type: ignore[attr-defined]
    monkeypatch.setattr(store, "load_or_create_device_id", lambda: device_id)  # type: ignore[attr-defined]
    monkeypatch.setattr(licensing_gate, "_cap_verdict_token", lambda _root: cap_token)  # type: ignore[attr-defined]
    monkeypatch.setattr(store, "load_auth_token", lambda: "test-session-token")  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        store,
        "load_auth_user",
        lambda: {
            "user_id": "u_test",
            "device_id": device_id,
            "email": email,
            "plan": plan,
            "plan_token": plan_token,
        },
    )
    entitlements.reload()


def python_script_with_development_cap(script: Path) -> list[str]:
    """Run a hook script with the compiled gate mocked to no-key dev mode."""

    import sys

    bootstrap = (
        "import runpy,sys;"
        "from lemoncrow.pro.capabilities import licensing_gate;"
        "licensing_gate._public_key_hex=lambda:'';"
        "script=sys.argv[1];sys.argv=[script,*sys.argv[2:]];"
        "runpy.run_path(script,run_name='__main__')"
    )
    return [sys.executable, "-c", bootstrap, str(script)]


def deny_oauth(monkeypatch: object) -> None:
    """Force the signed-out state regardless of the developer's real ~/.lemoncrow."""
    from lemoncrow.core.capabilities.licensing import entitlements, store

    monkeypatch.setattr(store, "load_auth_token", lambda: None)  # type: ignore[attr-defined]
    entitlements.reload()


@functools.cache
def init_store_at(root_str: str) -> None:
    """Initialize lemoncrow at *root_str*. Cached so repeated inits for the
    same path are no-ops (saves ~1-2 s per redundant call).

    Caller must pass a **string** (not a Path) so lru_cache can hash it.
    """
    from lemoncrow.infra.storage.factory import create_store

    create_store(Path(root_str)).init()
