"""Explicit ast-grep binary discovery and managed bootstrap helpers."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import platform
import stat
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from lemoncrow_client.kit.astgrep import ASTGREP_BINARY, AstGrepBinaryResolution, discover_astgrep

_EXPECTED_BINARY = ASTGREP_BINARY
_MANAGED_VERSION = "0.45.3"


@dataclass(frozen=True)
class ManagedAstGrepAsset:
    """Pinned ast-grep artifact metadata."""

    archive_name: str
    url: str
    sha256: str


_MANAGED_ASSETS: dict[str, ManagedAstGrepAsset] = {
    "Darwin-arm64": ManagedAstGrepAsset(
        archive_name="app-aarch64-apple-darwin.zip",
        url="https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-aarch64-apple-darwin.zip",
        sha256="6d2279dea5bea2ad79c66ea93f5fe54ba926e398a8a26de76c56db68fe59eac6",
    ),
    "Darwin-x86_64": ManagedAstGrepAsset(
        archive_name="app-x86_64-apple-darwin.zip",
        url="https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-x86_64-apple-darwin.zip",
        sha256="b2ffd26f42810340326a9e8a084bdc3647a8795c1a3f21fc06bd7bef3c7c5b2c",
    ),
    "Linux-aarch64": ManagedAstGrepAsset(
        archive_name="app-aarch64-unknown-linux-gnu.zip",
        url="https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-aarch64-unknown-linux-gnu.zip",
        sha256="b39cfbc58da4b869a88b8a4bc57bd5deb0d24541e704cf7c257da7b53ec81c8f",
    ),
    "Linux-x86_64": ManagedAstGrepAsset(
        archive_name="app-x86_64-unknown-linux-gnu.zip",
        url="https://github.com/ast-grep/ast-grep/releases/download/0.45.3/app-x86_64-unknown-linux-gnu.zip",
        sha256="f8ac830881339d1edee6b2652f54798c0f4da5a827f2db38a08ee31117783ce8",
    ),
}


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


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


@dataclass
class _BootstrapAttempts:
    """Per-install-root serialisation, plus the outcome of the attempt in flight.

    ``lock`` is what stops a single `lc review` from downloading the same ~20MB
    archive three times: its detectors run on a thread pool and several reach
    discovery together, all missing the same existence check.

    ``epoch``/``outcome`` are what stop that serialisation from multiplying a
    *failure*. A caller that queued behind an attempt which has now finished
    without producing a binary already has its answer, and repeating the attempt
    only repeats its cost -- on an offline machine that is a full
    ``urlopen(timeout=60)`` per detector instead of one for the pool. Only an
    attempt that completed while this caller was waiting is adopted; any later
    call starts a fresh one, so nothing is cached across a review.

    ``outcome_downloader`` narrows that further to attempts that ran the *same*
    fetch. ``review.impact._astgrep_ready`` probes with a downloader that refuses
    outright, and a refusal is not an answer for a caller that would really have
    downloaded.
    """

    lock: threading.Lock
    epoch: int = 0
    outcome: AstGrepBinaryResolution | None = None
    outcome_downloader: Callable[[ManagedAstGrepAsset], bytes] | None = None


# Keyed by install root so unrelated repos still bootstrap in parallel.
_BOOTSTRAP_ATTEMPTS: dict[str, _BootstrapAttempts] = {}
_BOOTSTRAP_ATTEMPTS_GUARD = threading.Lock()
# The cross-process lockfile is advisory on purpose, and is held around the
# install alone -- the download happens before it, so an availability probe that
# refuses to download never creates one. Publishing the binary is milliseconds of
# work, so a lockfile older than the stale threshold has lost its holder to a
# crash or a kill -9. The wait budget MUST stay above that threshold: a waiter
# polls for the reclaim, so a budget below it can never reach it and every later
# bootstrap in the checkout burns the whole budget and then leaves the dead
# lockfile in place. Failing to take the lock at all just means installing
# unlocked, which is still safe -- the install is an os.replace() of a fully
# written temp file.
_BOOTSTRAP_LOCK_STALE_SECONDS = 30.0
_BOOTSTRAP_LOCK_WAIT_SECONDS = 60.0
_BOOTSTRAP_LOCK_POLL_SECONDS = 0.05


def _bootstrap_attempts(install_root: Path) -> _BootstrapAttempts:
    with _BOOTSTRAP_ATTEMPTS_GUARD:
        return _BOOTSTRAP_ATTEMPTS.setdefault(str(install_root), _BootstrapAttempts(lock=threading.Lock()))


@contextlib.contextmanager
def _bootstrap_file_lock(target: Path) -> Iterator[None]:
    """Hold an O_EXCL lockfile beside *target* while the binary is published.

    Cross-process companion to :class:`_BootstrapAttempts`: two `lc` processes in
    the same checkout would otherwise unzip and publish over each other, and the
    second would redo an install the first had just finished. Best effort by
    design -- see the timing constants above for why never blocking forever is
    worth more here than guaranteed mutual exclusion.
    """

    lock_path = target.with_name(f"{target.name}.lock")
    deadline = time.monotonic() + _BOOTSTRAP_LOCK_WAIT_SECONDS
    handle: int | None = None
    while handle is None:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # Another process is installing. Stop waiting the moment its result
            # appears, when the wait budget runs out, or when the lockfile is old
            # enough that no live holder could still own it.
            if _is_executable(target) or time.monotonic() >= deadline:
                break
            try:
                abandoned = time.time() - lock_path.stat().st_mtime > _BOOTSTRAP_LOCK_STALE_SECONDS
            except OSError:
                # The lockfile went away (or cannot be stat'ed) between the two
                # calls. Retry the create -- but through the sleep below, never
                # in a tight loop that pins a core until the deadline.
                abandoned = False
            if abandoned:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass  # another waiter reclaimed it first -- retry the create
                except OSError:
                    # The stale lock cannot be removed: a read-only or sticky
                    # install root, or a lock path that is not a file at all.
                    # Retrying cannot change any of that -- the create would
                    # raise FileExistsError, the age check would say
                    # "abandoned" again and the unlink would fail again, at
                    # full speed until the wait budget expired. Fail the lock
                    # honestly instead and install unlocked, exactly as the
                    # unusable-lockfile branch below already does.
                    break
                # The lock is gone, so the create below is progress, not a spin:
                # it either succeeds or meets a *fresh* lockfile, which the age
                # check sends to the sleep.
                continue
            time.sleep(_BOOTSTRAP_LOCK_POLL_SECONDS)
        except OSError:
            break  # unwritable install root, no O_EXCL support: proceed unlocked
    try:
        yield
    finally:
        if handle is not None:
            os.close(handle)
            with contextlib.suppress(OSError):
                lock_path.unlink()


def _install_binary(target: Path, payload: bytes) -> None:
    """Publish *payload* at *target* so no reader can ever see it half-written.

    Writing straight to the final path opens a window in which the binary exists
    but is only partly there, and a concurrent bootstrap can mark that truncated
    file executable -- after which every later run trusts it. A failed write left
    the same wreckage behind permanently. Write a sibling temp file, make it
    executable, then rename: a reader sees either nothing or a complete binary,
    and a failure takes its temp file with it.
    """

    temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    # 0o666 hands the read/write bits to the process umask, exactly as the
    # write_bytes() this replaced did -- mkstemp's fixed 0600, or a fixed 0755,
    # would publish the same permissions on every host and quietly widen the
    # binary to group and other on one whose umask (0o077) said otherwise.
    handle = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        # Executable for everyone the umask left able to read it: 0o711 under
        # umask 0o077, 0o755 under 0o022, which is what the old chmod produced.
        temp_path.chmod(temp_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(temp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


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
    installed = AstGrepBinaryResolution(available=True, path=target, source="managed", checked=(str(target),))
    if _is_executable(target):
        return installed

    download = downloader or _download_managed_asset
    attempts = _bootstrap_attempts(target.parent)
    with _BOOTSTRAP_ATTEMPTS_GUARD:
        queued_at = attempts.epoch
    with attempts.lock:
        # Outside the lock the check above is only a fast path: concurrent
        # detectors all pass it together. Repeat it here so the waiters adopt the
        # winner's install instead of downloading and rewriting it again.
        if _is_executable(target):
            return installed
        with _BOOTSTRAP_ATTEMPTS_GUARD:
            ran_the_same_fetch = attempts.epoch != queued_at and attempts.outcome_downloader is download
            concurrent = attempts.outcome if ran_the_same_fetch else None
        if concurrent is not None:
            # An attempt finished while this caller was queued behind it and left
            # no binary. Its answer is this caller's answer: running the same
            # download again would just buy the same failure at the same price.
            return concurrent
        outcome = _run_bootstrap(root, asset, target, installed, download)
        with _BOOTSTRAP_ATTEMPTS_GUARD:
            attempts.epoch += 1
            attempts.outcome = outcome
            attempts.outcome_downloader = download
        return outcome


def _run_bootstrap(
    root: Path,
    asset: ManagedAstGrepAsset,
    target: Path,
    installed: AstGrepBinaryResolution,
    download: Callable[[ManagedAstGrepAsset], bytes],
) -> AstGrepBinaryResolution:
    """Fetch and publish the managed binary; the caller holds the per-root lock.

    The download comes first and touches nothing on disk. That ordering is
    load-bearing: ``review.impact._astgrep_ready`` calls the bootstrap purely as
    an availability probe, with a downloader that refuses, and a probe that had
    already created ``.lemoncrow/bin/<version>/<platform>/`` (or a lockfile
    beside it) would leave a dirty checkout behind every read-only review.
    """

    try:
        archive_bytes = download(asset)
    except (OSError, ValueError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        return AstGrepBinaryResolution(
            available=False,
            checked=(asset.url,),
            reason=f"managed ast-grep bootstrap failed: {exc}",
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return AstGrepBinaryResolution(
            available=False,
            checked=(str(target),),
            reason=f"managed ast-grep bootstrap failed: {exc}",
        )
    with _bootstrap_file_lock(target):
        if _is_executable(target):
            return installed
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                member = next(
                    (name for name in archive.namelist() if Path(name).name == _EXPECTED_BINARY),
                    None,
                )
                if member is None:
                    raise ValueError(f"{asset.archive_name} does not contain {_EXPECTED_BINARY}")
                _install_binary(target, archive.read(member))
            _write_manifest(root, asset, target)
            return installed
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
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

    def bootstrap(root: Path) -> AstGrepBinaryResolution:
        return bootstrap_managed_astgrep(root, downloader=downloader)

    return discover_astgrep(repo_root, bootstrap=bootstrap if allow_bootstrap else None)


__all__ = [
    "ManagedAstGrepAsset",
    "bootstrap_managed_astgrep",
    "discover_astgrep_binary",
]
