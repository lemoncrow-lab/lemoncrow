"""Lease-scoped immutable source materialization for explicitly approved Review agents.

The hosted Review worker API owns the allowed source manifest and content access.
This module checks the exact claimed Snapshot context and *all* selected bytes
before they enter a fresh local read-only tree. No mutable Git checkout, archive
extraction, host symlink, untrusted path, raw agent instruction or provider
credential is accepted as source authority. A partial tree is never exposed.

The manifest itself comes from the authenticated, live HTTPS worker endpoint;
this is NOT a separately signed or offline-verifiable source-tree attestation.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .review_worker_client import ReviewWorkerClient, WorkerLease

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MODES = frozenset({0o100644, 0o100755})
_DENIED_SEGMENTS = frozenset(
    {
        ".git",
        ".ssh",
        ".aws",
        ".claude",
        ".lemoncrow",
        ".gnupg",
        ".config",
        ".docker",
        ".kube",
        ".terraform",
        ".venv",
        "node_modules",
        "__pycache__",
        ".idea",
        ".vscode",
    }
)
_DENIED_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".gitconfig",
        ".bash_history",
        "id_rsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
    }
)
_DENIED_EXTENSIONS = (".pem", ".key", ".p12", ".pfx", ".jks")


class AgentSnapshotUnavailable(ValueError):
    """Never recover by reading the operator's mutable checkout."""


@dataclass(frozen=True, slots=True)
class AgentSnapshotLimits:
    max_files: int = 5_000
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 128 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(
            type(value) is not int
            for value in (
                self.max_files,
                self.max_file_bytes,
                self.max_total_bytes,
            )
        ):
            raise ValueError("Revision limits must be bounded integers")
        if not (
            1 <= self.max_files <= 20_000
            and 1 <= self.max_file_bytes <= 16 * 1024 * 1024
            and self.max_file_bytes <= self.max_total_bytes <= 512 * 1024 * 1024
        ):
            raise ValueError("Revision limits exceed the standalone client security cap")


@dataclass(frozen=True, slots=True)
class AgentSnapshotManifest:
    side: str
    files: tuple[tuple[str, str, int, int], ...]
    total_bytes: int
    snapshot_id: str


def _safe_source_path(raw: Any) -> PurePosixPath:
    if not isinstance(raw, str) or not raw:
        raise AgentSnapshotUnavailable("source path is malformed or oversized")
    try:
        encoded_length = len(raw.encode("utf-8"))
    except UnicodeError as exc:
        raise AgentSnapshotUnavailable("source path contains invalid Unicode") from exc
    if encoded_length > 4096:
        raise AgentSnapshotUnavailable("source path is malformed or oversized")
    if "\\" in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise AgentSnapshotUnavailable("source path contains control or platform escape characters")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or path.as_posix() != raw
        or raw in {".", ".."}
        or len(path.parts) > 40
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise AgentSnapshotUnavailable("source path must be a normalized relative filename")
    if any(part.lower() in _DENIED_SEGMENTS for part in path.parts):
        raise AgentSnapshotUnavailable("source path contains a local agent/credential directory")
    name = path.name.lower()
    if name in _DENIED_NAMES or name.startswith(".env.") or name.endswith(_DENIED_EXTENSIONS) or ":" in raw:
        raise AgentSnapshotUnavailable("source path contains a credential-like filename")
    return path


def validate_agent_source_context(
    lease: WorkerLease,
    context: Mapping[str, Any],
    *,
    limits: AgentSnapshotLimits,
) -> AgentSnapshotManifest:
    """No content fetch until all paths, types, sizes and identity are admitted."""
    if lease.execution_class != "review_intelligence" or lease.capability != "review_intelligence:agent_task":
        raise AgentSnapshotUnavailable("signed lease does not authorize source for an author agent")
    if any(
        context.get(key) != value
        for key, value in (
            ("job_id", lease.job_id),
            ("review_id", lease.review_id),
            ("revision_id", lease.revision_id),
            ("snapshot_id", lease.snapshot_id),
            ("semantic_snapshot_digest", lease.snapshot_digest),
            ("attempt", lease.attempt),
        )
    ):
        raise AgentSnapshotUnavailable("worker source context differs from the signed Review revision")
    # Review Intelligence may request either old or new at the server; never
    # accidentally analyze base files when the user approved the current head.
    if context.get("side") != "new":
        raise AgentSnapshotUnavailable("author-agent source must be the current new side")
    if context.get("manifest_truncated") is not False:
        raise AgentSnapshotUnavailable("Revision manifest is truncated or unavailable")
    records = context.get("manifest")
    if not isinstance(records, Mapping) or not (1 <= len(records) <= limits.max_files):
        raise AgentSnapshotUnavailable("Revision manifest missing, empty or exceeds the file limit")
    accepted: list[tuple[str, str, int, int]] = []
    total = 0
    seen: set[str] = set()
    for name, record in records.items():
        path = _safe_source_path(name)
        if not isinstance(record, Mapping):
            raise AgentSnapshotUnavailable("Revision manifest has malformed file metadata")
        digest = record.get("content_digest")
        size = record.get("size")
        mode = record.get("mode")
        if (
            not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or type(size) is not int
            or not 0 <= size <= limits.max_file_bytes
            or type(mode) is not int
            or mode not in _MODES
        ):
            raise AgentSnapshotUnavailable("Revision manifest contains unsupported or oversized source")
        parts = path.parts
        for depth in range(1, len(parts)):
            if PurePosixPath(*parts[:depth]).as_posix() in seen:
                raise AgentSnapshotUnavailable("Revision manifest contains file/directory collisions")
        seen.add(name)
        total += size
        if total > limits.max_total_bytes:
            raise AgentSnapshotUnavailable("Revision exceeds local total source byte limit")
        accepted.append((name, digest, size, mode))
    for name in seen:
        parent = PurePosixPath(name)
        # A file cannot be a parent directory of another file, regardless of
        # the iteration order used by the untrusted returned JSON object.
        if any(PurePosixPath(*parent.parts[:i]).as_posix() in seen for i in range(1, len(parent.parts))):
            raise AgentSnapshotUnavailable("Revision manifest contains file/directory collisions")
    return AgentSnapshotManifest(
        side="new",
        files=tuple(sorted(accepted)),
        total_bytes=total,
        snapshot_id=lease.snapshot_id,
    )


@contextmanager
def materialize_leased_agent_snapshot(
    client: ReviewWorkerClient,
    lease: WorkerLease,
    *,
    limits: AgentSnapshotLimits | None = None,
    expected_model_route_id: str | None = None,
) -> Iterator[tuple[Path, AgentSnapshotManifest]]:
    """Yield one fully verified immutable tree, then clean up even on exceptions.

    The context and all bytes are acquired using the pinned signed worker
    authorization. No caller-controlled destination is accepted. A partial
    materialization, revoked lease or digest mismatch never reaches the agent.
    """
    client._require_lease(lease)
    if client.heartbeat(lease) != "running":
        raise AgentSnapshotUnavailable("lease was cancelled before source acquisition")
    context = client.context(lease)
    if expected_model_route_id is not None and (
        not expected_model_route_id or context.get("model_route_id") != expected_model_route_id
    ):
        raise AgentSnapshotUnavailable("source context model route differs from signed assignment")
    manifest = validate_agent_source_context(lease, context, limits=limits or AgentSnapshotLimits())
    with tempfile.TemporaryDirectory(prefix="lc-review-agent-source-") as raw:
        root = Path(raw)
        source = root / "snapshot"
        source.mkdir(mode=0o700)
        try:
            for name, digest, expected_size, mode in manifest.files:
                if client.heartbeat(lease) != "running":
                    raise AgentSnapshotUnavailable("lease revoked during source materialization")
                body = client.content(lease, digest, side="new")
                if len(body) != expected_size or hashlib.sha256(body).hexdigest() != digest:
                    raise AgentSnapshotUnavailable("source content differs from exact leased manifest")
                relative = _safe_source_path(name)
                destination = source.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # No symlink/hardlink extraction or reused file: a new owner-only
                # temp tree + exclusive creation + no-follow + checked metadata.
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    with open(fd, "wb") as stream:
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
                os.chmod(destination, 0o500 if mode == 0o100755 else 0o400)
            if client.heartbeat(lease) != "running":
                raise AgentSnapshotUnavailable("lease revoked after source materialization")
            for directory, _, _ in os.walk(source, topdown=False):
                os.chmod(directory, stat.S_IRUSR | stat.S_IXUSR)
            yield source, manifest
        finally:
            # Restore directory owner write bits so TemporaryDirectory can
            # remove the source after the sandbox exits or is cancelled.
            for directory, _, _ in os.walk(source, topdown=False):
                os.chmod(directory, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


__all__ = [
    "AgentSnapshotLimits",
    "AgentSnapshotManifest",
    "AgentSnapshotUnavailable",
    "materialize_leased_agent_snapshot",
    "validate_agent_source_context",
]
