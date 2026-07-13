"""Signed cap verdict — tamper-resistant server authority for the savings cap.

The auth server signs a compact verdict with its Ed25519 private key; the client
verifies with the pinned public key. Expiry is a SIGNED claim (server wall-clock)
*inside* the payload, so touching or editing a local file cannot forge or extend
it — any change breaks the signature. A missing / invalid / expired token yields
``None`` (no trustworthy verdict); the caller then fails CLOSED (dormant), so
blocking or spoofing the server can never produce "free forever."

Token format (compact, URL-safe): ``b64url(payload_json).b64url(signature)``.
Payload: ``{account_id, savings_over_cap, monthly_savings_usd, cap_usd,
issued_at, expires_at}``.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

# Pinned server public key (hex). Rotatable without a client rebuild via env.
# The real key is baked into the compiled MCP server (.so) at release.
_DEFAULT_PUBLIC_KEY_HEX = ""


def _public_key_hex() -> str:
    return (os.environ.get("LEMONCROW_CAP_PUBLIC_KEY") or _DEFAULT_PUBLIC_KEY_HEX).strip()


def is_configured() -> bool:
    """True when a public key is pinned, so signed verdicts can be verified."""
    return bool(_public_key_hex())


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def verify_cap_token(token: str, *, public_key_hex: str | None = None) -> dict[str, Any] | None:
    """Verify the Ed25519 signature and return the payload, or ``None``.

    Signature-only: does NOT check expiry (see :func:`cap_over_from_token`).
    Fail-safe: any malformed input / bad signature / missing key -> ``None``.
    """
    key_hex = (public_key_hex if public_key_hex is not None else _public_key_hex()).strip()
    if not key_hex or not token or "." not in token:
        return None
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        payload_b64, sig_b64 = token.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        signature = _b64url_decode(sig_b64)
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
        try:
            pub.verify(signature, payload_bytes)
        except InvalidSignature:
            return None
        payload = json.loads(payload_bytes)
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 — any parse/crypto error = untrusted
        return None


def cap_over_from_token(token: str | None, *, now: int, public_key_hex: str | None = None) -> bool | None:
    """Tri-state signed cap decision.

    - ``True``  — valid signature, not expired, over cap.
    - ``False`` — valid signature, not expired, under cap.
    - ``None``  — no trustworthy verdict (missing / invalid / expired). The
      caller treats ``None`` as fail-closed (dormant) for a hosted account.
    """
    if not token:
        return None
    payload = verify_cap_token(token, public_key_hex=public_key_hex)
    if payload is None:
        return None
    try:
        if now >= int(payload.get("expires_at", 0)):
            return None  # expired -> stale -> no verdict (caller fails closed)
    except (TypeError, ValueError):
        return None
    return bool(payload.get("savings_over_cap"))


def sign_cap_token(payload: dict[str, Any], *, private_key_hex: str) -> str:
    """Sign a verdict payload (Ed25519). Server-side / tests / local dev.

    The production signer is the auth server; this mirrors its format so the
    verify path is testable and a Python control plane can issue tokens if needed.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = priv.sign(payload_bytes)
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
