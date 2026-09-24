"""The mutable facts one client process learns from the server.

Split out from :mod:`lemoncrow_client.session` so the blob service can read and
advance them without importing the session, and so the whole of the client's
*mutable* state is one small, readable object -- which matters when the
question being asked is "what does this process remember, and where does it
keep it?". The answer is: in memory, for the life of one stdio process, and
nowhere else. Nothing here is persisted.

Two fields carry the degradation contract and are deliberately separate:

``bootstrapped``
    The session opened at least once. It stays ``True`` afterwards.
``online``
    The last request reached the server.

"Never came up" (``not bootstrapped``) and "went away mid-session"
(``bootstrapped and not online``) are different conditions with different
required behaviours, and one boolean could not tell them apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .manifest import ManifestEntry
from .protocol import HEADER_REVISION, HEADER_SESSION, HEADER_VIEW
from .recovery import RecoveryEvent, RecoveryKind, RecoveryOutcome

__all__ = ["SessionState"]

_MIB: Final[int] = 1024 * 1024
_SERVER_DIGEST_CACHE_ENTRIES: Final[int] = 16_384
_MAX_RECOVERY_EVENTS: Final[int] = 128


@dataclass
class SessionState:
    """What the client knows right now. In memory only."""

    session_id: str = ""
    view_id: str = ""
    repo_id: str = ""
    view_revision: int = 0
    capabilities: frozenset[str] = frozenset()
    local_fs: bool = False
    #: The same-host proof nonce this session was issued. The server chooses
    #: it, so writing it into the worktree is a demonstration that this client
    #: can write there -- which is the whole of what ``local_fs`` requires.
    local_fs_proof: str = ""
    #: Set once the session opens; never cleared. Distinguishes "the server
    #: was never there" from "the server went away".
    bootstrapped: bool = False
    #: Whether the most recent request reached the server.
    online: bool = False
    #: One line, shown to the developer when the server is not usable.
    reason: str = ""

    # -- server-declared limits, defaulted to this build's expectations ---- #
    content_size_cap: int = _MIB
    max_request_bytes: int = 4 * _MIB
    max_blob_bytes: int = 8 * _MIB
    max_blobs_per_request: int = 256
    max_manifest_entries_per_chunk: int = 5_000
    max_overlay_entries: int = 256

    #: Monotonic within a view. The server refuses a replayed or decreasing
    #: sequence, and returns the previously committed revision for an exact
    #: replay, which is what makes a retried write after a lost ACK safe.
    client_seq: int = 0

    #: Last manifest row this client sent per path, so an unchanged file is
    #: not re-uploaded and not re-overlaid.
    known: dict[str, ManifestEntry] = field(default_factory=dict)
    #: Digests this server acknowledged during the current authenticated
    #: session. This is only a round-trip optimization: a later blob-miss can
    #: always repair a mistaken/stale belief. Bounded independently of the
    #: response cache because 16k SHA-256 strings are already plenty for edit /
    #: revert cycles.
    server_digests: dict[str, None] = field(default_factory=dict)
    #: Bounded, process-local recovery facts. No request/source payloads live
    #: here; this is only the automatic decision/outcome trail.
    recovery_events: list[RecoveryEvent] = field(default_factory=list)

    def record_recovery(
        self,
        kind: RecoveryKind,
        outcome: RecoveryOutcome,
        *,
        error_code: str = "",
        tool: str = "",
        client_seq: int = 0,
        from_revision: int | None = None,
        to_revision: int | None = None,
        count: int = 0,
    ) -> RecoveryEvent:
        event = RecoveryEvent(
            kind=kind,
            outcome=outcome,
            error_code=str(error_code)[:64],
            tool=str(tool)[:64],
            client_seq=max(0, int(client_seq)),
            from_revision=from_revision,
            to_revision=to_revision,
            count=max(0, int(count)),
        )
        self.recovery_events.append(event)
        if len(self.recovery_events) > _MAX_RECOVERY_EVENTS:
            del self.recovery_events[: len(self.recovery_events) - _MAX_RECOVERY_EVENTS]
        return event

    def next_seq(self) -> int:
        self.client_seq += 1
        return self.client_seq

    def remember_server_digests(self, digests: Any) -> None:
        for raw in digests:
            digest = str(raw)
            if not digest:
                continue
            self.server_digests.pop(digest, None)
            self.server_digests[digest] = None
            while len(self.server_digests) > _SERVER_DIGEST_CACHE_ENTRIES:
                self.server_digests.pop(next(iter(self.server_digests)))

    def server_has_digest(self, digest: str) -> bool:
        if digest not in self.server_digests:
            return False
        # Refresh insertion order so hot revert targets survive the bound.
        self.server_digests.pop(digest, None)
        self.server_digests[digest] = None
        return True

    def adopt_limits(self, limits: Mapping[str, Any]) -> None:
        """Take the server's declared ceilings. Only integers, only known keys."""
        for key in (
            "max_request_bytes",
            "max_blob_bytes",
            "max_blobs_per_request",
            "max_manifest_entries_per_chunk",
            "max_overlay_entries",
        ):
            value = limits.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                setattr(self, key, value)

    def headers(self, *, with_revision: bool = True) -> dict[str, str]:
        """Session, view and the revision this call claims to have observed.

        The revision header is what makes read-after-edit a protocol guarantee:
        a call that names a revision the server has not committed, or one it has
        already moved past, is refused rather than answered from a view the
        caller never saw.
        """
        out: dict[str, str] = {}
        if self.session_id:
            out[HEADER_SESSION] = self.session_id
        if self.view_id:
            out[HEADER_VIEW] = self.view_id
            if with_revision:
                out[HEADER_REVISION] = str(self.view_revision)
        return out

    @property
    def view_bound(self) -> bool:
        return bool(self.view_id)

    def go_offline(self, reason: str) -> None:
        self.online = False
        self.reason = reason
