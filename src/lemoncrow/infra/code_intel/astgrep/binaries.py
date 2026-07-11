"""Explicit ast-grep binary discovery and managed bootstrap helpers."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import stat
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_ENV_VAR = "LEMONCROW_AST_GREP_BIN"
_EXPECTED_BINARY = "ast-grep"
_MANAGED_VERSION = "0.42.2"


@dataclass(frozen=True)
class ManagedAstGrepAsset:
    """Pinned ast-grep artifact metadata."""

    archive_name: str
    url: str
    sha256: str


@dataclass(frozen=True)
class AstGrepBinaryResolution:
    """Structured ast-grep availability status."""

    available: bool
    path: Path | None = None
    source: str | None = None
    checked: tuple[str, ...] = ()
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": "tool_unavailable",
            "tool": "ast-grep",
            "expected_binary": _EXPECTED_BINARY,
            "message": self.reason or "ast-grep is unavailable",
            "checked": list(self.checked),
            "hint": f"Set {_ENV_VAR} to an executable ast-grep binary or allow the managed bootstrap path.",
        }


_MANAGED_ASSETS: dict[str, ManagedAstGrepAsset] = {
    "Darwin-arm64": ManagedAstGrepAsset(
        archive_name="app-aarch64-apple-darwin.zip",
        url="https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-aarch64-apple-darwin.zip",
        sha256="9f1522db1f7174ab0cba5a6d1df1861f9b92803fac407988177c28f744bd0f94",
    ),
    "Darwin-x86_64": ManagedAstGrepAsset(
        archive_name="app-x86_64-apple-darwin.zip",
        url="https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-x86_64-apple-darwin.zip",
        sha256="6652401a9b98f7c8c528f969d34e2a42d2cb60f29fc4dc569209d16c29702d9c",
    ),
    "Linux-aarch64": ManagedAstGrepAsset(
        archive_name="app-aarch64-unknown-linux-gnu.zip",
        url="https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-aarch64-unknown-linux-gnu.zip",
        sha256="a68d7645d49dbd97b423cc8a64f7839fe5541eedf0b4bb4ab79f4ba5d53f0376",
    ),
    "Linux-x86_64": ManagedAstGrepAsset(
        archive_name="app-x86_64-unknown-linux-gnu.zip",
        url="https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-x86_64-unknown-linux-gnu.zip",
        sha256="52aef3ed330a5fb1d9f399b83285bfcf47d92401249803f62711573e83cb47ae",
    ),
}


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _reject_reason(path: str) -> str | None:
    name = Path(path).name
    if name == "sg":
        return "resolved binary is the Linux `sg` group-switch utility, not ast-grep"
    return None


def _resolve_candidate(candidate: str) -> Path | None:
    expanded = Path(candidate).expanduser()
    if expanded.name == candidate:
        resolved = shutil.which(candidate)
        if not resolved:
            return None
        expanded = Path(resolved)
    try:
        return expanded.resolve()
    except OSError:
        return None


def _platform_key() -> str:
    machine = platform.machine().lower()
    normalized = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64"}.get(machine, machine)
    return f"{platform.system()}-{normalized}"


def _managed_install_root(repo_root: Path) -> Path:
    return repo_root / ".lemoncrow" / "bin" / "ast-grep" / _MANAGED_VERSION / _platform_key()


def _manifest_path(repo_root: Path) -> Path:
    return repo_root / ".lemoncrow" / "bin" / "MANIFEST.json"


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ast-grep": {}}
    try:
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {"ast-grep": {}}


def _write_manifest(repo_root: Path, asset: ManagedAstGrepAsset, binary_path: Path) -> None:
    manifest_path = _manifest_path(repo_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_manifest(manifest_path)
    payload.setdefault("ast-grep", {})
    payload["ast-grep"][_platform_key()] = {
        "version": _MANAGED_VERSION,
        "archive_name": asset.archive_name,
        "url": asset.url,
        "sha256": asset.sha256,
        "binary_path": str(binary_path.relative_to(repo_root)),
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _download_managed_asset(asset: ManagedAstGrepAsset) -> bytes:
    with urllib.request.urlopen(asset.url, timeout=60) as response:
        data = cast(bytes, response.read())
    digest = hashlib.sha256(data).hexdigest()
    if digest != asset.sha256:
        raise ValueError(f"checksum mismatch for {asset.archive_name}")
    return data


def bootstrap_managed_astgrep(
    repo_root: str | Path,
    *,
    downloader: Callable[[ManagedAstGrepAsset], bytes] | None = None,
) -> AstGrepBinaryResolution:
    """Install the pinned managed ast-grep binary for the current platform."""

    root = Path(repo_root).resolve()
    asset = _MANAGED_ASSETS.get(_platform_key())
    if asset is None:
        return AstGrepBinaryResolution(
            available=False,
            checked=(),
            reason=f"no managed ast-grep asset is pinned for platform {_platform_key()}",
        )

    target = _managed_install_root(root) / _EXPECTED_BINARY
    if _is_executable(target):
        return AstGrepBinaryResolution(available=True, path=target, source="managed", checked=(str(target),))

    download = downloader or _download_managed_asset
    try:
        archive_bytes = download(asset)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            member = next(
                (name for name in archive.namelist() if Path(name).name == _EXPECTED_BINARY),
                None,
            )
            if member is None:
                raise ValueError(f"{asset.archive_name} does not contain {_EXPECTED_BINARY}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _write_manifest(root, asset, target)
        return AstGrepBinaryResolution(available=True, path=target, source="managed", checked=(str(target),))
    except (OSError, ValueError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        return AstGrepBinaryResolution(
            available=False,
            checked=(asset.url,),
            reason=f"managed ast-grep bootstrap failed: {exc}",
        )


def discover_astgrep_binary(
    repo_root: str | Path,
    *,
    allow_bootstrap: bool = False,
    downloader: Callable[[ManagedAstGrepAsset], bytes] | None = None,
) -> AstGrepBinaryResolution:
    """Resolve ast-grep via env override, exact binary discovery, then optional bootstrap."""

    root = Path(repo_root).resolve()
    checked: list[str] = []

    env_candidate = os.environ.get(_ENV_VAR)
    if env_candidate:
        checked.append(env_candidate)
        reason = _reject_reason(env_candidate)
        resolved = _resolve_candidate(env_candidate)
        if reason:
            return AstGrepBinaryResolution(available=False, checked=tuple(checked), reason=reason)
        if resolved is not None and _is_executable(resolved):
            return AstGrepBinaryResolution(available=True, path=resolved, source="env", checked=tuple(checked))

    exact_candidate = shutil.which(_EXPECTED_BINARY)
    if exact_candidate:
        checked.append(exact_candidate)
        reason = _reject_reason(exact_candidate)
        resolved = _resolve_candidate(exact_candidate)
        if reason:
            return AstGrepBinaryResolution(available=False, checked=tuple(checked), reason=reason)
        if resolved is not None and _is_executable(resolved):
            return AstGrepBinaryResolution(
                available=True,
                path=resolved,
                source="system",
                checked=tuple(checked),
            )

    if allow_bootstrap:
        managed = bootstrap_managed_astgrep(root, downloader=downloader)
        if managed.available:
            return AstGrepBinaryResolution(
                available=True,
                path=managed.path,
                source=managed.source,
                checked=tuple([*checked, *managed.checked]),
            )
        checked.extend(managed.checked)
        return AstGrepBinaryResolution(available=False, checked=tuple(checked), reason=managed.reason)

    return AstGrepBinaryResolution(
        available=False,
        checked=tuple(checked),
        reason="ast-grep could not be resolved from env override or exact binary discovery",
    )


__all__ = [
    "AstGrepBinaryResolution",
    "ManagedAstGrepAsset",
    "bootstrap_managed_astgrep",
    "discover_astgrep_binary",
]
