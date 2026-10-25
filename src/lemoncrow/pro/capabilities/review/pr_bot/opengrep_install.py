#!/usr/bin/env python3
"""Install the pinned Opengrep release for the Action's security scan. Standard library only.

The Action runs this before anything else is installed, in the two steps around its cache step::

    python3 opengrep_install.py pin       # outputs: dir, key           (no network)
    python3 opengrep_install.py install   # outputs: bin, note          (verifies, downloads when it must)

Whatever happens, it exits 0: a scanner that cannot be installed never fails the review, the review only says
"security scan unavailable" with the ``note`` this prints (``LEMONCROW_OPENGREP_NOTE`` in the review step).

Trust. A release is pinned by tag *and* by the SHA-256 of each platform's asset (:data:`PINS`). Nothing is run until the
file on disk hashes to the pinned value, whether it was just downloaded or came out of the cache, so a poisoned cache
or a moved release tag cannot change what runs. A pin that is not a real 64-hex digest (``TODO(checksum)``) is
never installed: it is reported as unavailable. Opengrep publishes no checksum file; the pins below were computed from
the release assets and checked against their Sigstore signatures (see :data:`PINS`).

To move to a newer release: pick the tag, download each asset and its ``.sig`` and ``.cert`` from
``https://github.com/opengrep/opengrep/releases/download/<tag>/``, check the signature, hash the asset, and change
:data:`VERSION` and every digest in one commit. ``tests/gateway/test_review_security_scan.py`` fails if a digest is
neither 64 hex characters nor the ``TODO(checksum)`` marker.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: The release tag. Opengrep 1.x ships one self-extracting executable per platform; 2.x is still pre-release.
VERSION = "v1.32.0"
RELEASE_URL = "https://github.com/opengrep/opengrep/releases/download/{version}/{asset}"
CHECKSUM_TODO = "TODO(checksum)"
#: Refuse a download larger than this (the real assets are about 47 MB).
MAX_BYTES = 200 * 1024 * 1024
FETCH_TIMEOUT_S = 120
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Environment variables the downloaded binary may see when it is first run (the same list the scan uses).
_ENV_ALLOW = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"})


@dataclass(frozen=True)
class Pin:
    """One platform's release asset and the SHA-256 of its bytes (or :data:`CHECKSUM_TODO`)."""

    asset: str
    sha256: str


#: ``(system, machine)`` -> asset and digest, for ``VERSION``.
#:
#: Provenance of these digests: each asset was downloaded from
#: ``https://github.com/opengrep/opengrep/releases/download/v1.32.0/<asset>`` over TLS and hashed with SHA-256. Its
#: ``<asset>.sig`` verified (ECDSA, SHA-256) against the public key of its ``<asset>.cert``, whose subject alternative
#: name is ``https://github.com/opengrep/opengrep/.github/workflows/rolling-release.yml@refs/heads/release-v1.32.0``,
#: issued by ``sigstore-intermediate``. The certificate chain to the Sigstore root was not verified (no ``cosign`` in the
#: environment that computed them), and Opengrep publishes no checksum file to compare against.
PINS: dict[tuple[str, str], Pin] = {
    ("linux", "x86_64"): Pin(
        "opengrep_manylinux_x86", "7360d60174880bd9214f2dfbe439b7bce82b7a3d44cfe945ba833c85cc8bda75"
    ),
    ("linux", "aarch64"): Pin(
        "opengrep_manylinux_aarch64", "dc6229319c0d68948733053786a5614873947e5ec1d4669cc0e94ae9038cb99d"
    ),
    ("darwin", "x86_64"): Pin("opengrep_osx_x86", "7dfb68184b7da2895a56e86bc88192fe2b9414993026f5100bacccb4764d19f4"),
    ("darwin", "arm64"): Pin("opengrep_osx_arm64", "e161746226bccef5b85c57611946fa21dc3b00a2dc2591343d6160af99397e2f"),
}

Fetch = Callable[[str, Path], None]


def is_sha256(value: str) -> bool:
    return _SHA256.fullmatch(value) is not None


def platform_key(system: str, machine: str) -> tuple[str, str]:
    """``(system, machine)`` normalised the way :data:`PINS` is keyed (``Linux``/``amd64`` -> ``linux``/``x86_64``)."""

    kind = system.strip().lower()
    arch = machine.strip().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64" if kind == "linux" else "arm64"}.get(arch, arch)
    if kind == "darwin" and arch == "aarch64":
        arch = "arm64"
    return kind, arch


def pin_for(system: str, machine: str) -> tuple[Pin | None, str]:
    """The pin to install on this platform, or ``(None, why not)``."""

    key = platform_key(system, machine)
    pin = PINS.get(key)
    if pin is None:
        return None, f"no Opengrep release is pinned for {key[0]} {key[1]}"
    if not is_sha256(pin.sha256):
        return None, f"the Opengrep checksum for {key[0]} {key[1]} is not pinned yet ({CHECKSUM_TODO})"
    return pin, ""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_https(url: str, destination: Path) -> None:
    """Download *url* to *destination* (HTTPS only, bounded in size and time)."""

    if not url.startswith("https://"):
        raise ValueError("only https downloads are allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "lemoncrow-review-action"})
    written = 0
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response, destination.open("wb") as handle:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            written += len(chunk)
            if written > MAX_BYTES:
                raise ValueError(f"the download is over {MAX_BYTES // (1024 * 1024)} MB")
            handle.write(chunk)


def smoke_test(binary: Path, env: Mapping[str, str]) -> str:
    """``""`` when the binary runs and reports the pinned version, else why not."""

    try:
        done = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=120, env=dict(env), check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the binary did not run ({type(exc).__name__})"
    if done.returncode != 0 or VERSION.lstrip("v") not in done.stdout:
        return f"the binary did not report {VERSION}"
    return ""


@dataclass(frozen=True)
class Installed:
    """``path`` is the verified binary, or ``""`` with ``note`` saying why there is none."""

    path: str = ""
    note: str = ""
    downloaded: bool = False


def install(
    directory: Path,
    *,
    system: str,
    machine: str,
    fetch: Fetch = fetch_https,
    env: Mapping[str, str] | None = None,
) -> Installed:
    """The pinned binary in *directory*, verified; downloaded first when it is not there (or does not verify)."""

    pin, why = pin_for(system, machine)
    if pin is None:
        return Installed(note=why)
    target = directory / VERSION / "opengrep"
    # The smoke test sees the same short allow-list the scan does, never the job's secrets.
    environment = {key: value for key, value in (os.environ if env is None else env).items() if key in _ENV_ALLOW}
    if target.is_file() and sha256_of(target) == pin.sha256:
        problem = smoke_test(target, environment)
        return Installed(path=str(target)) if not problem else Installed(note=problem)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix="download-") as scratch:
        partial = Path(scratch) / pin.asset
        try:
            fetch(RELEASE_URL.format(version=VERSION, asset=pin.asset), partial)
        except Exception as exc:
            return Installed(note=f"the download failed ({type(exc).__name__})")
        if sha256_of(partial) != pin.sha256:
            return Installed(note=f"checksum mismatch for {pin.asset}: the download was not installed")
        partial.chmod(0o755)
        problem = smoke_test(partial, environment)
        if problem:
            return Installed(note=problem)
        os.replace(partial, target)
    return Installed(path=str(target), downloaded=True)


def _output(name: str, value: str) -> None:
    """Append ``name=value`` to ``$GITHUB_OUTPUT`` (single line: a newline in a value would forge another output)."""

    line = f"{name}={' '.join(value.split())}\n"
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line)
    else:
        sys.stdout.write(line)


def cache_directory(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "lemoncrow-opengrep"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else ""
    system, machine = platform.system(), platform.machine()
    directory = cache_directory(os.environ)
    pin, _why = pin_for(system, machine)
    if command == "pin":
        # The cache key names the release and the exact digest, so a re-pin starts from an empty cache.
        _output("dir", str(directory))
        _output("key", f"opengrep-{VERSION}-{pin.sha256[:16]}-{pin.asset}" if pin else "")
        return 0
    if command == "install":
        try:
            result = install(directory, system=system, machine=machine)
        except Exception as exc:  # the review must not fail because the scanner could not be installed
            result = Installed(note=f"the install failed ({type(exc).__name__})")
        _output("bin", result.path)
        _output("note", result.note)
        if result.note:
            sys.stdout.write(f"::warning title=LemonCrow security scan::security scan unavailable: {result.note}\n")
        elif result.downloaded:
            sys.stdout.write(f"Installed Opengrep {VERSION} ({pin.asset if pin else ''})\n")
        return 0
    sys.stderr.write("usage: opengrep_install.py pin|install\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
