"""Managed runtime discovery for the Zoekt search seam."""

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
_DOCKER_BINARY = "docker"


@dataclass(frozen=True)
class ZoektBinaryResolution:
    """Structured Zoekt runtime resolution status."""

    available: bool
    path: Path | None = None
    source: str | None = None
    checked: tuple[str, ...] = ()
    reason: str | None = None
    runtime: str = "binary"
    image_ref: str | None = None


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


def _managed_candidate(repo_root: Path) -> tuple[str | None, str | None]:
    payload = _load_manifest(_manifest_path(repo_root))
    zoekt = payload.get("zoekt")
    if not isinstance(zoekt, dict):
        return None, None
    image_ref = zoekt.get("image_ref")
    version = zoekt.get("version")
    if not isinstance(image_ref, str) or not isinstance(version, str):
        return None, None
    return image_ref, version


def _write_manifest(repo_root: Path, *, image_ref: str, version: str) -> None:
    manifest_path = _manifest_path(repo_root)
    payload = _load_manifest(manifest_path)
    payload["zoekt"] = {
        "image_ref": image_ref,
        "version": version,
        "source": "managed",
        "runtime": "docker",
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _provision_managed_candidate(repo_root: Path) -> tuple[str, str] | None:
    versions = _load_versions()
    zoekt = versions.get("zoekt")
    if not isinstance(zoekt, dict):
        return None
    version = zoekt.get("version")
    image_ref = zoekt.get("image_ref")
    if not isinstance(version, str) or not version.strip() or not isinstance(image_ref, str) or not image_ref.strip():
        return None
    _write_manifest(repo_root, image_ref=image_ref, version=version)
    return image_ref, version


def _validate(path: Path, expected_sha256: str | None) -> bool:
    if not _is_executable(path):
        return False
    if not expected_sha256:
        return False
    return _sha256(path) == expected_sha256


def discover_zoekt_binary(repo_root: str | Path) -> ZoektBinaryResolution:
    """Resolve a pinned Zoekt runtime via env override or managed manifest."""

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
                runtime="binary",
            )
        return ZoektBinaryResolution(
            available=False,
            checked=tuple(checked),
            reason=f"{_ENV_VAR} did not resolve to an executable with a matching {_SHA_ENV_VAR}",
        )

    docker_binary = shutil.which(_DOCKER_BINARY)
    if docker_binary:
        checked.append(docker_binary)

    managed_image, managed_version = _managed_candidate(root)
    if managed_image is not None and managed_version is not None and docker_binary:
        checked.append(managed_image)
        return ZoektBinaryResolution(
            available=True,
            path=Path(docker_binary).resolve(),
            source="managed",
            checked=tuple(checked),
            runtime="docker",
            image_ref=managed_image,
        )

    provisioned = _provision_managed_candidate(root)
    if provisioned is not None and docker_binary:
        provisioned_image, _provisioned_version = provisioned
        checked.append(provisioned_image)
        return ZoektBinaryResolution(
            available=True,
            path=Path(docker_binary).resolve(),
            source="managed",
            checked=tuple(checked),
            runtime="docker",
            image_ref=provisioned_image,
        )

    return ZoektBinaryResolution(
        available=False,
        checked=tuple(checked),
        reason="zoekt runtime could not be verified from env override or managed docker bootstrap",
    )


__all__ = ["ZoektBinaryResolution", "discover_zoekt_binary"]
