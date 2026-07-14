"""Signed cap verdict — client verify surface (delegates to the compiled gate).

The tamper-resistant verify path and the pinned Ed25519 public key live in the
COMPILED ``lemoncrow.pro.capabilities.licensing_gate`` (ships as ``.so`` only).
This open module re-exports that surface so existing importers and tests keep a
stable path, and retains :func:`sign_cap_token` — the server-format mirror used
by the auth server / tests / local dev. Signing needs the Ed25519 *private* key,
which never ships to clients, so keeping it in open source leaks nothing: without
the private key you cannot forge a token the compiled verifier will trust.
"""

from __future__ import annotations

import json
from typing import Any

from lemoncrow.pro.capabilities.licensing_gate import (
    _b64url_encode as _b64url_encode,
)
from lemoncrow.pro.capabilities.licensing_gate import (
    cap_over_from_token as cap_over_from_token,
)
from lemoncrow.pro.capabilities.licensing_gate import (
    is_configured as is_configured,
)
from lemoncrow.pro.capabilities.licensing_gate import (
    verify_cap_token as verify_cap_token,
)

__all__ = [
    "cap_over_from_token",
    "is_configured",
    "sign_cap_token",
    "verify_cap_token",
]


def sign_cap_token(payload: dict[str, Any], *, private_key_hex: str) -> str:
    """Sign a verdict payload (Ed25519). Server-side / tests / local dev.

    Mirrors the auth server's signer so the compiled verify path is testable and
    a Python control plane can issue tokens if needed.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = priv.sign(payload_bytes)
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
