"""Pinned managed Pi frontend lifecycle for ``lc code --engine pi``."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import click

PINNED_PI_TAG = "v0.84.2"
PINNED_PI_COMMIT = "914cf1472e715297caa30db4b9535d534a9eb718"
PINNED_PI_PUBLISHED_AT = "2026-08-14T10:14:32Z"
_RELEASE_BASE = f"https://github.com/earendil-works/pi/releases/download/{PINNED_PI_TAG}"
_ASSET_SHA256: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "c996e888b7f7dce44bcf24f69176ac646c44139d3916bd49a6b28e5a8c5e3a65",
    ("darwin", "x64"): "808cf02a93cd601d3ea05d47dc15c45074b120ac81decc8644cd3e40a35824e6",
    ("linux", "arm64"): "d15372da9e4b4c5fef9fd15bed76d7f5f1720dd39fe7cde0ec62e5b65ad63ef1",
    ("linux", "x64"): "906fbe787fd225c4ac624fe7ebd5b1d55a60e0f5c7ef51795d231564f9ee1c13",
}
_REQUIRED_HELP_FLAGS: tuple[str, ...] = (
    "--offline",
    "--no-tools",
    "--no-context-files",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-approve",
    "--session-dir",
    "--provider",
    "--model",
    "--extension",
)


@dataclass(frozen=True)
class HostRelease:
    tag: str
    commit: str
    published_at: str
    asset_name: str
    asset_url: str
    checksum: str


def _platform_parts() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"linux": "linux", "darwin": "darwin"}.get(system)
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if os_name is None or arch is None or (os_name, arch) not in _ASSET_SHA256:
        raise click.ClickException(f"managed Pi is not available for {platform.system()} {platform.machine()}")
    return os_name, arch


def pinned_release() -> HostRelease:
    os_name, arch = _platform_parts()
    asset_name = f"pi-{os_name}-{arch}.tar.gz"
    return HostRelease(
        tag=PINNED_PI_TAG,
        commit=PINNED_PI_COMMIT,
        published_at=PINNED_PI_PUBLISHED_AT,
        asset_name=asset_name,
        asset_url=f"{_RELEASE_BASE}/{asset_name}",
        checksum=_ASSET_SHA256[(os_name, arch)],
    )


def managed_host_binary(store_root: Path) -> Path:
    return Path(store_root).expanduser() / "bin" / "pi-host"


def managed_bundle_dir(store_root: Path) -> Path:
    return Path(store_root).expanduser() / "hosts" / "pi" / "runtime" / PINNED_PI_TAG


def _metadata_path(store_root: Path) -> Path:
    return Path(store_root).expanduser() / "hosts" / "pi-host.json"


def _read_metadata(store_root: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(_metadata_path(store_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_metadata(store_root: Path, payload: dict[str, object]) -> None:
    path = _metadata_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "lemoncrow-pi-host"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise click.ClickException(f"could not download Pi release asset: {exc}") from exc


def _install_bytes(store_root: Path, release: HostRelease, archive: bytes) -> Path:
    digest = hashlib.sha256(archive).hexdigest()
    if digest != release.checksum:
        raise click.ClickException(
            f"Pi checksum mismatch for {release.asset_name}: expected {release.checksum}, got {digest}"
        )

    target = managed_host_binary(store_root)
    bundle_target = managed_bundle_dir(store_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    bundle_target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pi-stage-", dir=bundle_target.parent) as temp_name:
        staging = Path(temp_name)
        archive_path = staging / release.asset_name
        archive_path.write_bytes(archive)
        try:
            with tarfile.open(archive_path, mode="r:gz") as bundle:
                for member in bundle.getmembers():
                    member_path = Path(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise click.ClickException(f"unsafe path in Pi release archive: {member.name}")
                bundle.extractall(staging, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise click.ClickException(f"could not unpack Pi release: {exc}") from exc

        extracted = staging / "pi"
        executable = extracted / "pi"
        if not executable.is_file():
            raise click.ClickException("Pi release archive did not contain pi/pi")
        executable.chmod(executable.stat().st_mode | 0o111)
        validate_host_binary(executable)

        backup = bundle_target.with_name(f"{bundle_target.name}.{os.getpid()}.old")
        if backup.exists():
            shutil.rmtree(backup)
        if bundle_target.exists():
            bundle_target.replace(backup)
        try:
            extracted.replace(bundle_target)
        except Exception:
            if backup.exists() and not bundle_target.exists():
                backup.replace(bundle_target)
            raise
        finally:
            if backup.exists():
                shutil.rmtree(backup)

    installed_executable = bundle_target / "pi"
    temp_target = target.with_suffix(f".{os.getpid()}.tmp")
    temp_target.write_text(
        "#!/bin/sh\n" f'exec {shlex.quote(str(installed_executable))} "$@"\n',
        encoding="utf-8",
    )
    temp_target.chmod(0o755)
    temp_target.replace(target)
    _write_metadata(
        store_root,
        {
            "tag": release.tag,
            "commit": release.commit,
            "published_at": release.published_at,
            "asset": release.asset_name,
            "sha256": release.checksum,
            "bundle_path": str(bundle_target),
            "installed_at": time.time(),
            "source": "release",
        },
    )
    return target


def install_host_release(store_root: Path) -> Path:
    release = pinned_release()
    return _install_bytes(store_root, release, _download_bytes(release.asset_url))


def build_host_from_source(store_root: Path, source_dir: Path) -> Path:
    source_dir = Path(source_dir).expanduser().resolve()
    package_json = source_dir / "package.json"
    if not package_json.is_file():
        raise click.ClickException(f"Pi source checkout is missing package.json: {source_dir}")
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        raise click.ClickException("building Pi from source requires node and npm")
    try:
        subprocess.run([npm, "install"], cwd=source_dir, check=True)
        subprocess.run([npm, "run", "build"], cwd=source_dir, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"Pi source build failed with status {exc.returncode}") from exc
    entry = source_dir / "packages" / "coding-agent" / "dist" / "cli.js"
    if not entry.is_file():
        raise click.ClickException(f"Pi build did not produce {entry}")

    target = managed_host_binary(store_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(
        "#!/bin/sh\n" f'exec {shlex.quote(node)} {shlex.quote(str(entry))} "$@"\n',
        encoding="utf-8",
    )
    tmp.chmod(0o755)
    tmp.replace(target)
    try:
        validate_host_binary(target)
    except click.ClickException:
        target.unlink(missing_ok=True)
        raise
    _write_metadata(
        store_root,
        {
            "tag": "source",
            "commit": PINNED_PI_COMMIT,
            "source_path": str(source_dir),
            "installed_at": time.time(),
            "source": "local-build",
        },
    )
    return target


def resolve_host_binary(store_root: Path) -> str | None:
    override = os.environ.get("LEMONCROW_PI_HOST_BIN", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise click.ClickException(f"LEMONCROW_PI_HOST_BIN does not exist: {candidate}")
        if not os.access(candidate, os.X_OK):
            raise click.ClickException(f"LEMONCROW_PI_HOST_BIN is not executable: {candidate}")
        return str(candidate)
    candidate = managed_host_binary(store_root)
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def validate_host_binary(executable: str | Path) -> None:
    """Fail closed when a Pi binary lacks a managed-mode safety control."""
    candidate = Path(executable).expanduser()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise click.ClickException(f"Pi host is not executable: {candidate}")
    try:
        completed = subprocess.run(
            [str(candidate), "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise click.ClickException(f"could not inspect Pi host controls: {exc}") from exc
    if completed.returncode != 0:
        raise click.ClickException(f"Pi --help failed with status {completed.returncode}")
    help_text = f"{completed.stdout}\n{completed.stderr}"
    missing = [flag for flag in _REQUIRED_HELP_FLAGS if flag not in help_text]
    if missing:
        raise click.ClickException("Pi host lacks required managed-mode controls: " + ", ".join(missing))


def host_update_policy() -> str:
    value = os.environ.get("LEMONCROW_PI_HOST_UPDATE", "pinned").strip().lower()
    return value if value in {"pinned", "auto", "off"} else "pinned"


def maybe_auto_update_host(store_root: Path) -> None:
    if host_update_policy() != "auto":
        return
    metadata = _read_metadata(store_root)
    if resolve_host_binary(store_root) is None or not metadata or metadata.get("tag") != PINNED_PI_TAG:
        install_host_release(store_root)


def remove_host(store_root: Path) -> bool:
    removed = False
    for path in (managed_host_binary(store_root), _metadata_path(store_root)):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            pass
    runtime_root = Path(store_root).expanduser() / "hosts" / "pi" / "runtime"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
        removed = True
    return removed


def host_status(store_root: Path) -> dict[str, object]:
    resolved = resolve_host_binary(store_root)
    return {
        "installed": managed_host_binary(store_root).is_file(),
        "resolved_path": resolved,
        "update_policy": host_update_policy(),
        "pinned_tag": PINNED_PI_TAG,
        "metadata": _read_metadata(store_root),
    }
