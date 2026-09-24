"""The blob service: the client's half of the sync protocol.

It does three things, and the design names all three.

**Fill the manifest.** After ``views/open`` the server says which manifest
chunks it is missing and which declared digests it does not hold. Those are
answered here, in bounded batches, idempotently -- a digest already stored is a
no-op, so an interrupted upload resumes by asking
``/v1/views/{id}/missing`` again rather than by tracking byte offsets.

**Push after every write.** ``edit`` and ``codemod`` report the paths they
changed; this service uploads the new content and commits an overlay revision
*before* the tool result reaches the agent. That is why a subsequent ``read``
sees the edit: not because enough time passed, but because the revision the
read names is the revision the write committed.

**Answer a pull within the same turn.** When a server-side tool needs content
the server does not hold it answers ``{"need": [paths]}`` with HTTP 200. This
service fills exactly those paths and the call is retried once. A client that
could not do this is refused at handshake, which is why ``blob_miss.v1`` is a
required capability rather than an optional one.

Everything is bounded: a batch respects the server's declared
``max_blobs_per_request``, ``max_blob_bytes`` and ``max_request_bytes``, and a
file over ``content_size_cap`` is manifested but never uploaded -- the server
will not list it as missing and will never demand it, so trying would loop.
"""

from __future__ import annotations

import base64
import gzip
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .errors import AgentAction, ClientError, ErrorCode
from .manifest import ManifestChunk, ManifestEntry, profile_for_path, sha256_hex
from .state import SessionState
from .transport import HttpTransport

__all__ = ["BlobService", "PushReport"]

#: base64 inflates by 4/3; leave room for the JSON envelope around it too.
_ENCODING_OVERHEAD: Final[float] = 1.40
#: Below this, gzip costs more than it saves on a source file.
_MIN_COMPRESS_BYTES: Final[int] = 512


@dataclass(frozen=True, slots=True)
class PushReport:
    """What one push or fill actually moved."""

    uploaded: int
    bytes_sent: int
    overlaid: int
    removed: int
    view_revision: int
    skipped_oversize: int = 0


class BlobService:
    """Uploads content and commits overlay revisions for one bound view."""

    __slots__ = ("_repo_root", "_state", "_token", "_transport")

    def __init__(self, transport: HttpTransport, state: SessionState, repo_root: Path, *, token: str = "") -> None:
        self._transport = transport
        self._state = state
        self._repo_root = repo_root
        self._token = token

    # -- manifest ------------------------------------------------------- #

    def send_chunks(
        self,
        chunks: Sequence[ManifestChunk],
        wanted: Iterable[str],
        *,
        timeout_s: float | None = None,
    ) -> int:
        """Send only the chunks the server said it is missing.

        A warm open asks for none of them, which is the whole point of the
        canonical root: a large unchanged repository must not resend thousands
        of rows to learn that the server already has them.
        """
        pending = set(wanted)
        if not pending:
            return 0
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        sent = 0
        for chunk_id in sorted(pending):
            chunk = by_id.get(chunk_id)
            if chunk is None:
                # The server is waiting on a chunk this walk did not produce,
                # which means the worktree moved since the root was computed.
                # Reopening with a fresh root is the fix; guessing is not.
                raise ClientError(
                    ErrorCode.MANIFEST_INCOMPLETE,
                    "the server is waiting for a manifest chunk this worktree no longer produces",
                    details={"chunk_id": chunk_id},
                    retryable=True,
                    action=AgentAction.REOPEN_VIEW,
                )
            self._transport.post(
                f"/v1/views/{self._state.view_id}/manifest-chunks",
                body={
                    "chunk_id": chunk.chunk_id,
                    "entries": [entry.to_wire() for entry in chunk.entries],
                },
                headers=self._auth(with_revision=False),
                timeout_s=timeout_s,
            ).require()
            sent += 1
        return sent

    # -- content -------------------------------------------------------- #

    def upload_digests(
        self,
        digests: Sequence[str],
        sources: Mapping[str, Path],
        *,
        timeout_s: float | None = None,
    ) -> tuple[int, int]:
        """Upload the named digests from the files that carry them.

        Returns ``(blobs stored, bytes of plaintext sent)``. A digest whose file
        no longer hashes to it is skipped rather than uploaded under a digest
        that says otherwise -- the server verifies, so sending it would only
        produce ``blob_digest_mismatch``.
        """
        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        stored = 0
        plaintext = 0
        for digest in digests:
            source = sources.get(digest)
            if source is None:
                continue
            data = self._read_verified(source, digest)
            if data is None:
                continue
            if len(data) > self._state.max_blob_bytes:
                continue
            blob = _encode(digest, data, profile_for_path(_relative(self._repo_root, source)))
            encoded = int(len(blob["data"]) * _ENCODING_OVERHEAD) + 256
            over_count = len(batch) >= self._state.max_blobs_per_request
            over_bytes = batch and batch_bytes + encoded > self._state.max_request_bytes
            if over_count or over_bytes:
                stored += self._flush(batch, timeout_s=timeout_s)
                batch, batch_bytes = [], 0
            batch.append(blob)
            batch_bytes += encoded
            plaintext += len(data)
        if batch:
            stored += self._flush(batch, timeout_s=timeout_s)
        return stored, plaintext

    def upload_payloads(
        self,
        digests: Sequence[str],
        payloads: Mapping[str, tuple[bytes, str]],
    ) -> tuple[int, int]:
        """Upload in-memory content for a virtual or snapshot view.

        Payloads are keyed by content digest and carry bytes plus parser profile.
        This uses the same bounded /v1/blobs transport as worktree sync without
        requiring a historical or staged Review snapshot to exist on disk.
        """

        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        stored = 0
        plaintext = 0
        for digest in digests:
            item = payloads.get(digest)
            if item is None:
                continue
            data, parser_profile = item
            if sha256_hex(data) != digest or len(data) > self._state.max_blob_bytes:
                continue
            blob = _encode(digest, data, parser_profile)
            encoded = int(len(blob["data"]) * _ENCODING_OVERHEAD) + 256
            over_count = len(batch) >= self._state.max_blobs_per_request
            over_bytes = bool(batch) and batch_bytes + encoded > self._state.max_request_bytes
            if over_count or over_bytes:
                stored += self._flush(batch)
                batch, batch_bytes = [], 0
            batch.append(blob)
            batch_bytes += encoded
            plaintext += len(data)
        if batch:
            stored += self._flush(batch)
        return stored, plaintext

    def _flush(self, batch: list[dict[str, Any]], *, timeout_s: float | None = None) -> int:
        payload = self._transport.post(
            "/v1/blobs", body={"blobs": batch}, headers=self._auth(with_revision=False), timeout_s=timeout_s
        ).require()
        # A successful idempotent upload means every digest in the batch is now
        # addressable by this session whether it was newly stored or already
        # present. Remember that fact to avoid a future /missing round trip.
        self._state.remember_server_digests(
            blob.get("content_digest", "") for blob in batch if isinstance(blob.get("content_digest"), str)
        )
        raw = payload.get("stored")
        return len(raw) if isinstance(raw, list) else 0

    # -- overlay -------------------------------------------------------- #

    def push_snapshot(
        self,
        contents: Mapping[str, bytes],
        *,
        removed_paths: Sequence[str] = (),
    ) -> PushReport:
        """Pin an already-read source snapshot into the bound View.

        Review builds its packet before opening the remote View. If a developer
        saves a file in that window, walking the worktree sees newer bytes than
        the packet the human is about to review. This method commits only the
        differing paths through the ordinary overlay protocol, using the exact
        bytes the caller already captured rather than re-reading disk.
        """
        if not self._state.view_bound:
            raise ClientError(
                ErrorCode.VIEW_UNKNOWN,
                "no view is bound to this session",
                action=AgentAction.REOPEN_VIEW,
            )

        entries: list[ManifestEntry] = []
        removed: list[str] = []
        payloads: dict[str, tuple[str, bytes]] = {}
        skipped_oversize = 0
        for relative, data in contents.items():
            digest = sha256_hex(data)
            current = self._state.known.get(relative)
            if current is not None and current.content_digest == digest:
                continue
            mode = current.mode if current is not None else 0o100644
            entry = ManifestEntry(
                path=relative,
                content_digest=digest,
                parser_profile=profile_for_path(relative),
                mode=mode,
                size=len(data),
            )
            entries.append(entry)
            if len(data) <= self._state.content_size_cap and len(data) <= self._state.max_blob_bytes:
                payloads[digest] = (relative, data)
            else:
                skipped_oversize += 1

        for relative in dict.fromkeys(removed_paths):
            if relative in self._state.known:
                removed.append(relative)

        uploaded = 0
        sent = 0
        if payloads:
            missing = self._missing(tuple(payloads))
            batch: list[dict[str, Any]] = []
            batch_bytes = 0
            for digest in missing:
                item = payloads.get(digest)
                if item is None:
                    continue
                relative, data = item
                blob = _encode(digest, data, profile_for_path(relative))
                encoded = int(len(blob["data"]) * _ENCODING_OVERHEAD) + 256
                if batch and (
                    len(batch) >= self._state.max_blobs_per_request
                    or batch_bytes + encoded > self._state.max_request_bytes
                ):
                    uploaded += self._flush(batch)
                    batch, batch_bytes = [], 0
                batch.append(blob)
                batch_bytes += encoded
                sent += len(data)
            if batch:
                uploaded += self._flush(batch)

        revision = self._state.view_revision
        overlaid = 0
        for window in _windows(entries, removed, self._state.max_overlay_entries):
            revision = self._commit(window[0], window[1])
            overlaid += len(window[0])
        for entry in entries:
            self._state.known[entry.path] = entry
        for relative in removed:
            self._state.known.pop(relative, None)
        return PushReport(
            uploaded=uploaded,
            bytes_sent=sent,
            overlaid=overlaid,
            removed=len(removed),
            view_revision=revision,
            skipped_oversize=skipped_oversize,
        )

    def push_paths(self, paths: Sequence[str]) -> PushReport:
        """Make the server's view current for ``paths``. The one write path.

        ``edit`` calls it with what it wrote; the blob-miss handler calls it
        with what the server asked for. Both need exactly the same thing --
        content uploaded, then an overlay revision committed -- so there is one
        implementation and one place where the revision advances.
        """
        if not self._state.view_bound:
            raise ClientError(
                ErrorCode.VIEW_UNKNOWN,
                "no view is bound to this session",
                action=AgentAction.REOPEN_VIEW,
            )
        entries: list[ManifestEntry] = []
        removed: list[str] = []
        sources: dict[str, Path] = {}
        oversize = 0
        for relative in dict.fromkeys(paths):
            target = self._repo_root / relative
            if not target.is_file() or target.is_symlink():
                if self._state.known.pop(relative, None) is not None or not target.exists():
                    removed.append(relative)
                continue
            try:
                data = target.read_bytes()
            except OSError:
                continue
            digest = sha256_hex(data)
            entry = ManifestEntry(
                path=relative,
                content_digest=digest,
                parser_profile=profile_for_path(relative),
                mode=0o100755 if target.stat().st_mode & 0o100 else 0o100644,
                size=len(data),
            )
            entries.append(entry)
            if len(data) > self._state.content_size_cap:
                # Manifested but not uploaded, by server policy. Its row still
                # travels, so the path stays searchable by metadata.
                oversize += 1
                continue
            sources[digest] = target

        uploaded, sent = (0, 0)
        if sources:
            missing = self._missing(tuple(sources))
            if missing:
                uploaded, sent = self.upload_digests(missing, sources)

        revision = self._state.view_revision
        overlaid = 0
        for window in _windows(entries, removed, self._state.max_overlay_entries):
            revision = self._commit(window[0], window[1])
            overlaid += len(window[0])
        for entry in entries:
            self._state.known[entry.path] = entry
        return PushReport(
            uploaded=uploaded,
            bytes_sent=sent,
            overlaid=overlaid,
            removed=len(removed),
            view_revision=revision,
            skipped_oversize=oversize,
        )

    def _commit(self, entries: Sequence[ManifestEntry], removed: Sequence[str]) -> int:
        """One overlay write. Optimistic on revision, monotonic on ``client_seq``."""
        client_seq = self._state.next_seq()
        body = {
            "expected_view_revision": self._state.view_revision,
            "client_seq": client_seq,
            "entries": [entry.to_wire() for entry in entries],
            "removed_paths": list(removed),
        }
        path = f"/v1/views/{self._state.view_id}/overlay"
        headers = self._auth(with_revision=False)
        replayed = False
        try:
            payload = self._transport.post(path, body=body, headers=headers).require()
        except ClientError as exc:
            if exc.code is not ErrorCode.SERVER_UNREACHABLE:
                raise
            # The request may have committed and only its acknowledgement may
            # have been lost. Retry exactly once with the same client_seq and
            # body: the server's per-view replay cache returns the original
            # revision for an exact replay and never applies the mutation twice.
            replayed = True
            self._state.record_recovery(
                "write_replay",
                "retrying",
                error_code=exc.code.value,
                client_seq=client_seq,
                from_revision=self._state.view_revision,
            )
            try:
                payload = self._transport.post(path, body=body, headers=headers).require()
            except ClientError as retry_exc:
                self._state.record_recovery(
                    "write_replay",
                    "failed",
                    error_code=retry_exc.code.value,
                    client_seq=client_seq,
                    from_revision=self._state.view_revision,
                )
                raise
        revision = payload.get("view_revision")
        if not isinstance(revision, int):
            if replayed:
                self._state.record_recovery(
                    "write_replay",
                    "failed",
                    error_code=ErrorCode.INTERNAL.value,
                    client_seq=client_seq,
                    from_revision=self._state.view_revision,
                )
            raise ClientError(
                ErrorCode.INTERNAL,
                "overlay acknowledgement carried no view_revision",
                action=AgentAction.REOPEN_VIEW,
            )
        if replayed:
            self._state.record_recovery(
                "write_replay",
                "recovered",
                error_code=ErrorCode.SERVER_UNREACHABLE.value,
                client_seq=client_seq,
                from_revision=self._state.view_revision,
                to_revision=revision,
            )
        self._state.view_revision = revision
        return revision

    # -- the {need: [...]} answer --------------------------------------- #

    def fill(self, need: Sequence[str]) -> PushReport:
        """Answer a server pull, in the same turn it was asked.

        The paths come from the server, so they are validated the same way a
        manifest row is: repository-relative, no ``..``, no absolute path. A
        malformed one is dropped rather than joined onto the worktree root.
        """
        safe = tuple(path for path in need if _safe_relative(path))
        if not safe:
            raise ClientError(
                ErrorCode.BLOB_MISSING,
                "the server asked for content by paths this client cannot address",
                details={"requested": len(need)},
                action=AgentAction.ABANDON,
            )
        return self.push_paths(safe)

    # -- helpers -------------------------------------------------------- #

    def outstanding(self, digests: Sequence[str]) -> tuple[str, ...]:
        return self._missing(digests)

    def _missing(self, digests: Sequence[str]) -> tuple[str, ...]:
        unknown = tuple(dict.fromkeys(digest for digest in digests if not self._state.server_has_digest(digest)))
        if not unknown:
            return ()
        answer = self._transport.post(
            f"/v1/views/{self._state.view_id}/missing",
            body={"digests": list(unknown)},
            headers=self._auth(with_revision=False),
        ).require()
        raw = answer.get("missing_digests")
        missing = tuple(str(entry) for entry in raw) if isinstance(raw, list) else ()
        missing_set = set(missing)
        self._state.remember_server_digests(digest for digest in unknown if digest not in missing_set)
        return missing

    def _read_verified(self, source: Path, digest: str) -> bytes | None:
        try:
            data = source.read_bytes()
        except OSError:
            return None
        return data if sha256_hex(data) == digest else None

    def _auth(self, *, with_revision: bool) -> dict[str, str]:
        """Credential plus session/view/revision. Every sync route needs both.

        ``org_id`` is never on the wire -- the server derives it from the
        presented credential -- so the bearer token is what scopes a blob to a
        tenant, and a sync request without it is not a weaker request, it is an
        unauthenticated one.
        """
        headers = self._state.headers(with_revision=with_revision)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


def _encode(digest: str, data: bytes, parser_profile: str) -> dict[str, Any]:
    """base64, or gzip-base64 when compression actually helps."""
    if len(data) >= _MIN_COMPRESS_BYTES:
        compressed = gzip.compress(data, compresslevel=6, mtime=0)
        if len(compressed) < len(data):
            return {
                "content_digest": digest,
                "encoding": "gzip-base64",
                "data": base64.b64encode(compressed).decode("ascii"),
                "parser_profile": parser_profile,
            }
    return {
        "content_digest": digest,
        "encoding": "base64",
        "data": base64.b64encode(data).decode("ascii"),
        "parser_profile": parser_profile,
    }


def _windows(
    entries: Sequence[ManifestEntry], removed: Sequence[str], limit: int
) -> tuple[tuple[Sequence[ManifestEntry], Sequence[str]], ...]:
    """Split one logical push into overlay writes the server will accept.

    At least one window always exists, so a push that only deletes still
    commits a revision -- the caller's next read has to be able to name it.
    """
    windows: list[tuple[Sequence[ManifestEntry], Sequence[str]]] = []
    index = 0
    while index < len(entries):
        windows.append((entries[index : index + limit], ()))
        index += limit
    position = 0
    while position < len(removed):
        windows.append(((), removed[position : position + limit]))
        position += limit
    return tuple(windows) if windows else (((), ()),)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _safe_relative(path: object) -> bool:
    if not isinstance(path, str) or not path or len(path) > 1024:
        return False
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    return not any(segment in {"..", "."} for segment in path.split("/"))
