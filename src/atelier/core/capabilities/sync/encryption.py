"""Encryption helpers for cross-machine sync blobs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PASSPHRASE_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"\d")
_SYMBOL_RE = re.compile(r"[^A-Za-z0-9]")


class InvalidPassphraseError(ValueError):
    """Raised when sync decryption fails because the passphrase is wrong."""


def validate_passphrase_strength(passphrase: str, *, allow_weak: bool = False) -> None:
    if allow_weak:
        return
    reasons: list[str] = []
    if len(passphrase) < 12:
        reasons.append("at least 12 characters")
    categories = sum(
        [
            1 if _PASSPHRASE_RE.search(passphrase) else 0,
            1 if _DIGIT_RE.search(passphrase) else 0,
            1 if _SYMBOL_RE.search(passphrase) else 0,
        ]
    )
    if categories < 3:
        reasons.append("letters, numbers, and symbols")
    if reasons:
        raise ValueError(f"Weak passphrase. Use {' and '.join(reasons)} or pass --allow-weak.")


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        maxmem=0,
        dklen=32,
    )


def encrypt_bytes(plaintext: bytes, passphrase: str, *, aad: bytes | None = None) -> dict[str, str]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, aad)
    return {
        "algorithm": "aes-256-gcm+scrypt",
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_bytes(envelope: dict[str, Any], passphrase: str, *, aad: bytes | None = None) -> bytes:
    salt = base64.b64decode(str(envelope["salt_b64"]))
    nonce = base64.b64decode(str(envelope["nonce_b64"]))
    ciphertext = base64.b64decode(str(envelope["ciphertext_b64"]))
    key = _derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise InvalidPassphraseError(
            "Sync decryption failed. The passphrase is incorrect or the blob is corrupt."
        ) from exc


def encrypt_json(payload: dict[str, Any], passphrase: str, *, aad: bytes | None = None) -> bytes:
    envelope = encrypt_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"), passphrase, aad=aad
    )
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")


def decrypt_json(blob: bytes, passphrase: str, *, aad: bytes | None = None) -> dict[str, Any]:
    envelope = json.loads(blob.decode("utf-8"))
    plaintext = decrypt_bytes(envelope, passphrase, aad=aad)
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("decrypted sync blob must be a JSON object")
    return data


__all__ = [
    "InvalidPassphraseError",
    "decrypt_bytes",
    "decrypt_json",
    "encrypt_bytes",
    "encrypt_json",
    "validate_passphrase_strength",
]
