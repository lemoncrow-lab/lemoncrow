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

_ENV_VAR = "LEMONCROW_ZOEKT_BIN"
_SHA_ENV_VAR = "LEMONCROW_ZOEKT_BIN_SHA256"
_MODE_ENV_VAR = "LEMONCROW_ZOEKT_MODE"
_DOCKER_BINARY = "docker"
_LOCAL_REQUIRED = ("zoekt", "zoekt-index", "zoekt-git-index", "zoekt-webserver")
_VALID_MODES = frozenset({"off", "installed", "managed"})
# Common Go binary directories that users install into but that are absent from
# PATH when LemonCrow runs as an MCP server (launched by Claude Desktop / Code
# without a user shell environment).
_GO_BIN_PROBE_DIRS = (
    Path.home() / "go" / "bin",  # default: go install writes here
    Path("/usr/local/go/bin"),  # system-wide Go toolchain
    Path("/usr/local/bin"),  # Homebrew on Intel mac / typical Linux
    Path("/opt/homebrew/bin"),  # Homebrew on Apple Silicon
)


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


def zoekt_mode() -> str:
    """Return the configured Zoekt policy.

    Defaults to ``off``: search is lexical-only unless the operator explicitly
    opts in via ``LEMONCROW_ZOEKT_MODE=installed`` (use Zoekt's regex/trigram
    index when the binaries are already on PATH or pinned via
    ``LEMONCROW_ZOEKT_BIN``, falling back silently to native search otherwise) or
    ``LEMONCROW_ZOEKT_MODE=managed`` (bootstrap via Docker). This keeps the
    default free of any Zoekt runtime cost or install requirement.
    """
    mode = os.environ.get(_MODE_ENV_VAR, "off").strip().lower()
    return mode if mode in _VALID_MODES else "off"


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(repo_root: Path) -> Path:
    return repo_root / ".lemoncrow" / "bin" / "MANIFEST.json"


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
    """Resolve Zoekt according to the configured installed/managed/off policy."""

    root = Path(repo_root).resolve()
    checked: list[str] = []
    mode = zoekt_mode()
    if mode == "off":
        return ZoektBinaryResolution(
            available=False,
            reason=f"{_MODE_ENV_VAR}=off",
        )

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

    local_paths: dict[str, str] = {}
    for name in _LOCAL_REQUIRED:
        found = shutil.which(name)
        if found:
            checked.append(found)
            local_paths[name] = found
    if all(name in local_paths for name in _LOCAL_REQUIRED):
        return ZoektBinaryResolution(
            available=True,
            path=Path(local_paths["zoekt"]).resolve(),
            source="system-local",
            checked=tuple(checked),
            runtime="binary",
        )

    # shutil.which only searches PATH.  When LemonCrow runs as an MCP server
    # (launched by an IDE without a full user shell) PATH omits Go's default
    # install directory (~/go/bin).  Re-run which() with an augmented PATH
    # that includes common Go binary directories so that `go install`-ed
    # binaries are found even in restricted environments.
    # Only applies in "installed" mode -- "managed" mode explicitly requests
    # Docker; skipping the probe preserves that intent.
    if mode != "managed":
        for go_dir in _GO_BIN_PROBE_DIRS:
            if not go_dir.is_dir():
                continue
            go_probe: dict[str, str] = {}
            for name in _LOCAL_REQUIRED:
                candidate = go_dir / name
                if _is_executable(candidate):
                    go_probe[name] = str(candidate)
            if all(name in go_probe for name in _LOCAL_REQUIRED):
                for p in go_probe.values():
                    checked.append(p)
                return ZoektBinaryResolution(
                    available=True,
                    path=(go_dir / "zoekt").resolve(),
                    source="go-bin",
                    checked=tuple(checked),
                    runtime="binary",
                )

    if mode != "managed":
        return ZoektBinaryResolution(
            available=False,
            checked=tuple(checked),
            reason="zoekt is not installed; managed Docker bootstrap is disabled",
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


__all__ = ["ZoektBinaryResolution", "discover_zoekt_binary", "zoekt_mode"]
