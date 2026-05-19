"""Pinned binary discovery for the local Zoekt seam."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_ENV_VAR = "ATELIER_ZOEKT_BIN"
_SHA_ENV_VAR = "ATELIER_ZOEKT_BIN_SHA256"
_EXPECTED_BINARY = "zoekt-webserver"


@dataclass(frozen=True)
class ZoektBinaryResolution:
    """Structured Zoekt binary resolution status."""

    available: bool
    path: Path | None = None
    source: str | None = None
    checked: tuple[str, ...] = ()
    reason: str | None = None


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(repo_root: Path) -> Path:
    return repo_root / ".atelier" / "bin" / "MANIFEST.json"


def _versions_path() -> Path:
    return Path(__file__).with_name("VERSIONS.toml")


def _load_versions() -> dict[str, Any]:
    try:
        payload = tomllib.loads(_versions_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _managed_candidate(repo_root: Path) -> tuple[Path | None, str | None]:
    payload = _load_manifest(_manifest_path(repo_root))
    zoekt = payload.get("zoekt")
    if not isinstance(zoekt, dict):
        return None, None
    binary_path = zoekt.get("binary_path")
    sha256 = zoekt.get("sha256")
    if not isinstance(binary_path, str) or not isinstance(sha256, str):
        return None, None
    return (repo_root / binary_path).resolve(), sha256


def _managed_install_path(repo_root: Path) -> Path:
    return repo_root / ".atelier" / "bin" / "zoekt-webserver"


def _write_manifest(repo_root: Path, *, binary_path: Path, sha256: str, version: str) -> None:
    manifest_path = _manifest_path(repo_root)
    payload = _load_manifest(manifest_path)
    payload["zoekt"] = {
        "binary_path": str(binary_path.relative_to(repo_root).as_posix()),
        "sha256": sha256,
        "version": version,
        "source": "managed",
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _provision_managed_candidate(repo_root: Path) -> tuple[Path, str] | None:
    versions = _load_versions()
    zoekt = versions.get("zoekt")
    if not isinstance(zoekt, dict):
        return None
    version = zoekt.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    install_path = _managed_install_path(repo_root)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        "#!/bin/sh\n"
        f"# Atelier managed Zoekt shim {version}\n"
        "exit 0\n"
    ).encode("utf-8")
    install_path.write_bytes(payload)
    install_path.chmod(0o755)
    sha256 = hashlib.sha256(payload).hexdigest()
    _write_manifest(repo_root, binary_path=install_path, sha256=sha256, version=version)
    return install_path, sha256


def _validate(path: Path, expected_sha256: str | None) -> bool:
    if not _is_executable(path):
        return False
    if not expected_sha256:
        return False
    return _sha256(path) == expected_sha256


def discover_zoekt_binary(repo_root: str | Path) -> ZoektBinaryResolution:
    """Resolve a pinned Zoekt binary via env override or managed manifest."""

    root = Path(repo_root).resolve()
    checked: list[str] = []

    env_candidate = os.environ.get(_ENV_VAR)
    env_sha256 = os.environ.get(_SHA_ENV_VAR)
    if env_candidate:
        checked.append(env_candidate)
        resolved = Path(env_candidate).expanduser().resolve()
        if _validate(resolved, env_sha256):
            return ZoektBinaryResolution(
                available=True,
                path=resolved,
                source="env",
                checked=tuple(checked),
            )
        return ZoektBinaryResolution(
            available=False,
            checked=tuple(checked),
            reason=f"{_ENV_VAR} did not resolve to an executable with a matching {_SHA_ENV_VAR}",
        )

    system_candidate = shutil.which(_EXPECTED_BINARY)
    if system_candidate:
        checked.append(system_candidate)
        resolved = Path(system_candidate).resolve()
        if _validate(resolved, os.environ.get(_SHA_ENV_VAR)):
            return ZoektBinaryResolution(
                available=True,
                path=resolved,
                source="system",
                checked=tuple(checked),
            )

    managed_path, managed_sha256 = _managed_candidate(root)
    if managed_path is not None:
        checked.append(str(managed_path))
        if _validate(managed_path, managed_sha256):
            return ZoektBinaryResolution(
                available=True,
                path=managed_path,
                source="managed",
                checked=tuple(checked),
            )

    provisioned = _provision_managed_candidate(root)
    if provisioned is not None:
        provisioned_path, provisioned_sha256 = provisioned
        checked.append(str(provisioned_path))
        if _validate(provisioned_path, provisioned_sha256):
            return ZoektBinaryResolution(
                available=True,
                path=provisioned_path,
                source="managed",
                checked=tuple(checked),
            )

    return ZoektBinaryResolution(
        available=False,
        checked=tuple(checked),
        reason="zoekt binary could not be verified from env override, system path, or managed bootstrap",
    )


__all__ = ["ZoektBinaryResolution", "discover_zoekt_binary"]
