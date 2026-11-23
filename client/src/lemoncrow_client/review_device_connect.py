"""Review Companion one-time pairing, independent of Runtime/MCP.

A human issues a short-lived pairing code through Hosted Review. The machine
redeems it over outbound HTTPS, reads the code only from stdin, and stores
a separately revocable bearer + pinned Ed25519 signer in an owner-only file.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from .config import load_config
from .errors import ClientError
from .review_worker_client import WorkerBinding
from .transport import HttpTransport

_MAX_FILE = 16 * 1024
_ALLOWED = frozenset({"review_intelligence", "runtime_validation"})


def _path(repo: Path, review_state_dir: Path) -> Path:
    if review_state_dir.is_symlink():
        raise ValueError("Review state directory must not be redirected by symlink")
    identity = hashlib.sha256(os.fsencode(str(repo.resolve(strict=True)))).hexdigest()[:32]
    return review_state_dir / "devices" / f"{identity}.json"


def _private_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError("Review device state directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_mode & 0o077):
        raise ValueError("Review device state directory must be owner-only")


def _private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > _MAX_FILE:
        raise ValueError("Review machine credential file is not private and bounded")


def _wire_binding(raw: Mapping[str, Any], origin: str) -> dict[str, Any]:
    keys = raw.get("worker_signing_keys")
    if not isinstance(keys, list):
        raise ValueError("Review pairing did not supply signed worker key metadata")
    active = [
        key
        for key in keys
        if isinstance(key, dict) and key.get("status") == "active" and key.get("algorithm") == "Ed25519"
    ]
    if len(active) != 1:
        raise ValueError("Review pairing requires one active Ed25519 signing key")
    key = active[0]
    binding = WorkerBinding(
        origin=origin,
        org_id=str(raw.get("org_id") or ""),
        repo_id=str(raw.get("repo_id") or ""),
        worker_id=str(raw.get("worker_id") or ""),
        bearer=str(raw.get("machine_token") or ""),
        trusted_key_id=str(key.get("key_id") or ""),
        trusted_public_key_hex=str(key.get("public_key_hex") or ""),
    )
    classes, expiry = raw.get("execution_classes"), raw.get("expires_at")
    if (
        not isinstance(classes, list)
        or not classes
        or len(classes) > 2
        or any(not isinstance(item, str) or item not in _ALLOWED for item in classes)
        or len(set(classes)) != len(classes)
        or raw.get("kind") not in {"laptop", "vps", "sandbox"}
        or not isinstance(expiry, (int, float))
        or isinstance(expiry, bool)
        or not time.time() < float(expiry) <= time.time() + 31 * 86400
    ):
        raise ValueError("Review pairing response has invalid scope, lifetime or device kind")
    return {
        "version": 1,
        "origin": binding.origin,
        "org_id": binding.org_id,
        "repo_id": binding.repo_id,
        "worker_id": binding.worker_id,
        "kind": raw["kind"],
        "execution_classes": sorted(classes),
        "expires_at": float(expiry),
        "machine_token": binding.bearer,
        "trusted_key_id": binding.trusted_key_id,
        "trusted_public_key_hex": binding.trusted_public_key_hex,
    }


def save_pairing(repo: Path, review_state_dir: Path, raw: Mapping[str, Any], origin: str) -> Path:
    """Atomic owner-only replace. Never logs or returns the bearer secret."""
    document = _wire_binding(raw, origin)
    destination = _path(repo, review_state_dir)
    _private_directory(destination.parent)
    if destination.is_symlink():
        raise ValueError("Review device credential destination is a symlink")
    if destination.exists():
        _private_file(destination)
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_FILE:
        raise ValueError("Review machine credential record exceeds file limit")
    fd, temp = tempfile.mkstemp(prefix=".device-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, destination)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return destination


def load_pairing(repo: Path, review_state_dir: Path, *, expected_origin: str = "") -> Mapping[str, Any] | None:
    destination = _path(repo, review_state_dir)
    if destination.parent.is_symlink():
        raise ValueError("Review machine credential state is redirected by symlink")
    try:
        _private_file(destination)
    except FileNotFoundError:
        return None
    raw = json.loads(destination.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("Review machine credential file has an invalid format")
    binding = WorkerBinding(
        origin=str(raw.get("origin") or ""),
        org_id=str(raw.get("org_id") or ""),
        repo_id=str(raw.get("repo_id") or ""),
        worker_id=str(raw.get("worker_id") or ""),
        bearer=str(raw.get("machine_token") or ""),
        trusted_key_id=str(raw.get("trusted_key_id") or ""),
        trusted_public_key_hex=str(raw.get("trusted_public_key_hex") or ""),
    )
    classes = raw.get("execution_classes")
    if (
        not isinstance(classes, list)
        or not classes
        or any(not isinstance(item, str) or item not in _ALLOWED for item in classes)
        or classes != sorted(set(classes))
    ):
        raise ValueError("Review machine credential is outside supported execution classes")
    if expected_origin and binding.origin != expected_origin.rstrip("/"):
        raise ValueError("Review machine credential was paired with a different hosted origin")
    if not isinstance(raw.get("expires_at"), (int, float)) or float(raw["expires_at"]) <= time.time():
        raise ValueError("Review machine credential expired; request a new pairing")
    if binding.repo_id != raw["repo_id"]:
        raise ValueError("Review machine credential identity mismatch")
    return raw


def connect_main(
    arguments: list[str],
    *,
    sink: TextIO,
    input_source: TextIO | None = None,
) -> int:
    if arguments in (["--help"], ["-h"], ["help"]):
        sink.write(
            "Usage: lcr connect [--status]\n"
            "Obtain a one-time pairing code through Hosted Review and paste it via stdin.\n"
            "This does not reuse the Authward user login or start a Workspaces session.\n"
        )
        return 0
    if arguments not in ([], ["--status"]):
        sink.write("Unknown connect argument; see lcr connect --help.\n")
        return 2
    try:
        config = load_config(product="review")
        if arguments == ["--status"]:
            stored = load_pairing(config.repo_root, config.review_state_dir, expected_origin=config.url)
            if stored is None:
                sink.write("No paired Review Companion for this worktree.\n")
                return 1
            sink.write(
                f"Review machine {stored['worker_id']} · {stored['repo_id']} · "
                f"{', '.join(stored['execution_classes'])} · expires {int(stored['expires_at'])}\n"
            )
            return 0
        sink.write("Paste the one-time Review pairing code and press Enter:\n")
        sink.flush()
        code = (input_source if input_source is not None else sys.stdin).readline().strip()
        if not code.startswith("lcrp.") or len(code) > 720:
            raise ValueError("Review pairing code is missing or malformed")
        transport = HttpTransport(config.url, timeout_s=min(20.0, config.request_timeout_s))
        try:
            response = transport.post("/v1/review-devices/redeem", body={"pairing_code": code}).require()
        finally:
            transport.close()
        path = save_pairing(config.repo_root, config.review_state_dir, response, config.url)
        sink.write(f"Review Companion paired for this worktree (owner-only credentials in {path}).\n")
        return 0
    except (ValueError, OSError, ClientError) as exc:
        sink.write(f"Review connection refused: {type(exc).__name__}: {exc}\n")
        return 1
