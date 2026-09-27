"""Install and resolve the controlled LemonCode frontend binary."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

_RELEASE_API = "https://api.github.com/repos/lemoncrow-lab/lemoncode/releases/latest"
_CHECK_INTERVAL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class HostRelease:
    tag: str
    commit: str
    published_at: str
    asset_url: str
    checksum_url: str


def _platform_parts(platform_name: str | None = None, machine: str | None = None) -> tuple[str, str]:
    system = platform_name or platform.system().lower()
    os_name = {"linux": "linux", "darwin": "darwin"}.get(system)
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get((machine or platform.machine()).lower())
    if os_name is None or arch is None:
        raise click.ClickException(f"LemonCode has no managed host build for {system}/{machine or platform.machine()}")
    return os_name, arch


def _asset_name(platform_name: str | None = None, machine: str | None = None) -> str:
    os_name, arch = _platform_parts(platform_name, machine)
    return f"lemoncode-{os_name}-{arch}.tar.gz"


def host_binary_path(store_root: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return store_root / "bin" / f"lemoncode-host{suffix}"


def _metadata_path(store_root: Path) -> Path:
    return store_root / "bin" / "lemoncode-host.json"


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _source_binary() -> Path | None:
    try:
        os_name, arch = _platform_parts()
    except click.ClickException:
        return None
    repository = Path(__file__).resolve().parents[4]
    candidate = repository / "lemoncode" / "packages" / "opencode" / "dist"
    candidate = candidate / f"lemoncode-{os_name}-{arch}" / "bin" / "lemoncode"
    return candidate if _is_executable(candidate) else None


def resolve_host_binary(store_root: Path) -> str | None:
    override = os.environ.get("LEMONCODE_HOST_BIN", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not _is_executable(candidate):
            raise click.ClickException(f"LEMONCODE_HOST_BIN is not executable: {candidate}")
        return str(candidate.resolve())
    installed = host_binary_path(store_root)
    if _is_executable(installed):
        return str(installed)
    source = _source_binary()
    if source is not None:
        return str(source)
    return shutil.which("lemoncode-host")


def _read_metadata(store_root: Path) -> dict[str, Any]:
    try:
        value = json.loads(_metadata_path(store_root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_metadata(store_root: Path, value: dict[str, Any]) -> None:
    path = _metadata_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _latest_release() -> HostRelease:
    request = urllib.request.Request(
        _RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "lemoncode-host/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise click.ClickException(f"could not resolve the latest LemonCode host release: {exc}") from exc
    expected = _asset_name()
    urls = {
        item.get("name"): item.get("browser_download_url")
        for item in payload.get("assets", [])
        if isinstance(item, dict)
    }
    asset_url = urls.get(expected)
    checksum_url = urls.get(expected + ".sha256")
    if not isinstance(asset_url, str) or not isinstance(checksum_url, str):
        raise click.ClickException(
            f"release {payload.get('tag_name', '?')} does not contain {expected} and its checksum"
        )
    return HostRelease(
        tag=str(payload.get("tag_name", "")),
        commit=str(payload.get("target_commitish", "")),
        published_at=str(payload.get("published_at", "")),
        asset_url=asset_url,
        checksum_url=checksum_url,
    )


def _download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "lemoncode-host/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)


def _install_binary(store_root: Path, source: Path, metadata: dict[str, Any]) -> Path:
    target = host_binary_path(store_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".new")
    shutil.copyfile(source, temporary)
    temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(temporary, target)
    _write_metadata(store_root, metadata)
    return target


def install_host_release(store_root: Path, release: HostRelease | None = None) -> Path:
    selected = release or _latest_release()
    with tempfile.TemporaryDirectory(prefix="lemoncode-host-") as raw:
        temporary = Path(raw)
        archive = temporary / _asset_name()
        checksum = archive.with_name(archive.name + ".sha256")
        _download(selected.asset_url, archive)
        _download(selected.checksum_url, checksum)
        expected = checksum.read_text(encoding="utf-8").split()[0].lower()
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if not expected or not hmac.compare_digest(expected, actual):
            raise click.ClickException("LemonCode host checksum verification failed")
        with tarfile.open(archive, "r:gz") as bundle:
            matches = [
                member
                for member in bundle.getmembers()
                if member.isfile() and Path(member.name).name in {"lemoncode", "lemoncode.exe"}
            ]
            if len(matches) != 1:
                raise click.ClickException("LemonCode host archive must contain exactly one executable")
            stream = bundle.extractfile(matches[0])
            if stream is None:
                raise click.ClickException("could not read the LemonCode host executable")
            extracted = temporary / Path(matches[0].name).name
            with extracted.open("wb") as output:
                shutil.copyfileobj(stream, output)
        return _install_binary(
            store_root,
            extracted,
            {
                "source": "release",
                "tag": selected.tag,
                "commit": selected.commit,
                "published_at": selected.published_at,
                "sha256": actual,
                "checked_at": time.time(),
            },
        )


def build_host_from_source(store_root: Path, source_root: Path) -> Path:
    source = source_root.expanduser().resolve()
    package = source / "packages" / "opencode"
    if not (source / "package.json").is_file() or not package.is_dir():
        raise click.ClickException(f"not a LemonCode source checkout: {source}")
    bun = shutil.which("bun")
    if bun is None:
        raise click.ClickException("bun is required to build the LemonCode host")
    subprocess.run(
        [bun, "install", "--filter", "opencode", "--frozen-lockfile"],
        cwd=source,
        check=True,
        timeout=1800,
    )
    subprocess.run(
        [bun, "run", "script/build.ts", "--single", "--skip-embed-web-ui"],
        cwd=package,
        check=True,
        timeout=3600,
    )
    os_name, arch = _platform_parts()
    binary = package / "dist" / f"lemoncode-{os_name}-{arch}" / "bin" / "lemoncode"
    if not binary.is_file():
        raise click.ClickException(f"LemonCode build did not produce {binary}")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    return _install_binary(
        store_root,
        binary,
        {"source": "local", "commit": commit, "tag": f"local-{commit[:12]}", "checked_at": time.time()},
    )


def maybe_auto_update_host(store_root: Path) -> None:
    if os.environ.get("LEMONCODE_HOST_UPDATE", "auto").strip().lower() != "auto":
        return
    if not host_binary_path(store_root).is_file():
        return
    metadata = _read_metadata(store_root)
    if time.time() - float(metadata.get("checked_at", 0)) < _CHECK_INTERVAL_SECONDS:
        return
    try:
        release = _latest_release()
        if metadata.get("tag") != release.tag:
            click.echo(f"  ◇ Updating LemonCode host to {release.tag}...")
            install_host_release(store_root, release)
            click.echo("  ✓ LemonCode host updated.")
            return
        metadata["checked_at"] = time.time()
        metadata.pop("last_error", None)
        _write_metadata(store_root, metadata)
    except (click.ClickException, OSError, ValueError) as exc:
        metadata["checked_at"] = time.time()
        metadata["last_error"] = str(exc)
        _write_metadata(store_root, metadata)


def remove_host(store_root: Path) -> bool:
    removed = False
    for path in (host_binary_path(store_root), _metadata_path(store_root)):
        if path.exists():
            path.unlink()
            removed = True
    return removed


def host_status(store_root: Path) -> dict[str, Any]:
    installed = host_binary_path(store_root)
    return {
        "installed": _is_executable(installed),
        "installed_path": str(installed),
        "resolved_path": resolve_host_binary(store_root),
        "update_policy": os.environ.get("LEMONCODE_HOST_UPDATE", "auto"),
        "metadata": _read_metadata(store_root),
    }


__all__ = [
    "HostRelease",
    "build_host_from_source",
    "host_binary_path",
    "host_status",
    "install_host_release",
    "maybe_auto_update_host",
    "remove_host",
    "resolve_host_binary",
]
