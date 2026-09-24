"""The negotiated ``local_fs`` capability.

When the client and the server are on the same host -- the loopback deployment
that a solo developer runs, and the remote-workspace mode where the agent and
the index share a machine -- uploading a blob means copying bytes the server
could have read directly. ``local_fs`` lets it read them instead.

It is a **capability inside the one protocol**, not a second code path. The
manifest, the revisions, the digests, the blob-miss answer and every query stay
exactly the same; the only difference is where the bytes came from. That is why
the conformance suite runs the identical scenario with the capability on and
off and compares the transcripts: if turning it on changed an answer, it would
not be an optimization.

Granting it requires three independent things to hold:

1. the deployment allows it, and it may only do so on a loopback listener
   (``LEMONCROW_SERVER_ALLOW_LOCAL_FS``, refused off loopback by
   :class:`~..config.ServerConfig`);
2. the client offered the ``local_fs`` capability at handshake;
3. the client proves it can **write into** the worktree, by putting the nonce
   the server minted for this session at the top of that tree.

The third is the part that matters, and it is the part that used to be wrong.
Without it, "read from this path" is an instruction from a remote client to
read the *server's* filesystem, which is a file-disclosure primitive. The first
shape of the check let the **client** choose the nonce and merely compared it
against the file's contents -- so any file whose contents the caller could
predict was a valid proof, and every repository ships one: ``.git/HEAD`` is
``ref: refs/heads/main`` and needs no write access at all. That turned
knowledge of a colleague's ``(path, digest, size)`` into their bytes, through
the second of the two writers :mod:`..index.boundary` names. The nonce is now
the server's, minted per session and handed only to that session's owner, so
the only way it reaches the file the server reads is for the caller to have
written it there.

The proof file must also sit *directly* in ``root``. A proof nested somewhere
under it would demonstrate write access to that subdirectory and win a read of
the whole tree above it -- one world-writable scratch directory on a shared
host would be enough.

And the mirror of that: a proof at the root of a directory is not a proof about
the directory's *children*. Writing a file into a directory demonstrates write
permission on that one directory and nothing else -- on the shared build host
this capability is for, the natural layout is a checkout parent several
engineers can write holding one checkout each, and "I can write the parent"
would otherwise have converted knowledge of a colleague's ``(path, digest)``
into their bytes and a forged possession row, which is the whole of what the
boundary exists to stop. So the grant is bounded by something stronger than
"I can write here": the **owner** of the proof file names the principal that
proved it, that principal must own ``root`` itself -- a root it does not own is
a directory it merely has write access to -- and every directory stepped
through on the way to a file, and the file, must belong to it too. What the
capability can honestly say is therefore "these are the prover's own files",
which is exactly the claim a demonstrated possession makes.

Everything read is then verified against the digest the manifest declared. A
file that changed on disk since the manifest was built does not match, so it is
not accepted -- the client uploads it like any other miss.
"""

from __future__ import annotations

import hmac
import os
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..errors import AgentAction, ErrorCode, ServerError
from .contracts import AnalysisStore, ContentStore, ManifestEntry, sha256_hex, validate_path

__all__ = ["LocalFsFill", "LocalFsGrant", "LocalFsSource", "OwnerOf", "write_proof"]

_MAX_PROOF_BYTES: Final[int] = 512
_MIN_PROOF_TOKEN: Final[int] = 16

#: The proof file has to be a name in ``root`` itself, not a path under it.
#: See the module docstring: a nested proof demonstrates write access to a
#: subdirectory and would win a read of everything above it.
_PATH_SEPARATOR: Final[str] = "/"

#: How the server learns which principal a path belongs to. Injected like the
#: clock, because the property the containment rule turns on -- that the prover
#: and the owner of the tree are the same principal -- does not exist on a host
#: where every file belongs to one user, and a test that cannot stand up a
#: second principal cannot demonstrate a containment it claims to have.
OwnerOf = Callable[[Path], int]


def _owner_of(path: Path) -> int:
    """The uid owning ``path``. Raises :class:`OSError` like any other stat."""
    return os.stat(path).st_uid


@dataclass(frozen=True, slots=True)
class LocalFsGrant:
    """A proven same-host worktree the server may read for one view."""

    root: Path
    proof_path: str
    granted_at: float
    #: The principal that wrote the proof, and therefore the only one whose
    #: files this grant covers. See the module docstring: a directory below the
    #: root that belongs to somebody else was never proven by anything.
    owner_uid: int

    def to_wire(self) -> dict[str, Any]:
        # The root is the client's own path and it already knows it; echoing it
        # back adds nothing and puts a filesystem path in a response body.
        return {"granted": True, "proof_path": self.proof_path}


@dataclass(frozen=True, slots=True)
class LocalFsFill:
    """What one fill-from-disk pass managed to satisfy."""

    filled: tuple[str, ...]
    bytes_read: int
    rejected: int
    skipped: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "filled": len(self.filled),
            "bytes_read": self.bytes_read,
            "rejected": self.rejected,
            "skipped": self.skipped,
        }


class LocalFsSource:
    """Negotiates the capability and reads content the manifest already named."""

    __slots__ = ("_allow", "_clock", "_owner_of", "_size_cap")

    def __init__(
        self,
        *,
        allow: bool,
        size_cap: int,
        clock: Callable[[], float] = time.time,
        owner_of: OwnerOf = _owner_of,
    ) -> None:
        self._allow = allow
        self._size_cap = size_cap
        self._clock = clock
        self._owner_of = owner_of

    @property
    def allowed(self) -> bool:
        return self._allow

    def negotiate(
        self,
        raw: Mapping[str, Any] | None,
        *,
        session_granted: bool,
        issued_token: str,
    ) -> LocalFsGrant | None:
        """Verify a same-host offer, or return ``None``.

        ``issued_token`` is the nonce this session was minted, and it is the
        whole proof: the client has to have put it at the top of the worktree
        it is naming. A caller who can only *read* that tree -- or who has only
        learned a path and a digest out of it -- cannot, which is the property
        the capability claims and did not have.

        Declining is never an error: an optimization the server will not take
        leaves the client on the upload path, which is the path CI always runs.
        A *malformed* offer is an error, because silently ignoring one would
        leave a client believing it had an optimization it does not have.
        """
        if not raw:
            return None
        if not self._allow or not session_granted:
            return None
        if len(issued_token) < _MIN_PROOF_TOKEN:
            # No nonce, nothing to prove against. A session that negotiated the
            # capability always has one, so this is a composition error rather
            # than anything a client can provoke -- and it fails closed.
            return None
        root_raw = raw.get("root")
        proof_path = raw.get("proof_path")
        if not isinstance(root_raw, str) or not root_raw:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "local_fs.root must be an absolute path on the server's host",
                details={"field": "local_fs.root"},
                action=AgentAction.FIX_REQUEST,
            )
        validate_path(proof_path, "local_fs.proof_path")
        assert isinstance(proof_path, str)
        if _PATH_SEPARATOR in proof_path:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "local_fs.proof_path must name a file directly in local_fs.root",
                details={"field": "local_fs.proof_path"},
                action=AgentAction.FIX_REQUEST,
            )

        root = Path(root_raw)
        if not root.is_absolute():
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "local_fs.root must be absolute",
                details={"field": "local_fs.root"},
                action=AgentAction.FIX_REQUEST,
            )
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            return None
        if not resolved_root.is_dir():
            return None

        proof_file = self._safe_join(resolved_root, proof_path)
        if proof_file is None:
            return None
        try:
            if proof_file.stat().st_size > _MAX_PROOF_BYTES:
                return None
            # Compared as bytes, never decoded. A proof file carrying any
            # non-UTF-8 byte is simply not the nonce; decoding it first made
            # "this offer is not proven" -- the one outcome this route promises
            # is silent -- into an exception that failed the whole view_open.
            presented = proof_file.read_bytes().strip()
            prover = self._owner_of(proof_file)
            owns_root = self._owner_of(resolved_root) == prover
        except OSError:
            return None
        if not hmac.compare_digest(presented, issued_token.encode("utf-8")):
            # Not an error: an unproven offer is simply declined, and saying
            # *why* would report on the server's filesystem to a caller that
            # has not shown it can already write into it.
            return None
        if not owns_root:
            # Writing the proof proved write access to this directory; it did
            # not prove the directory is the prover's. A root somebody else
            # owns is a shared parent, and the checkouts under it are theirs.
            return None
        return LocalFsGrant(
            root=resolved_root,
            proof_path=proof_path,
            granted_at=float(self._clock()),
            owner_uid=prover,
        )

    def _safe_join(self, root: Path, relative: str) -> Path | None:
        """Resolve ``relative`` under ``root``, refusing every way out of it.

        ``validate_path`` already rejected ``..`` and absolute paths, so this
        is the second line: a symlink anywhere along the way could still point
        outside the worktree, and ``resolve`` plus an explicit containment
        check is what catches that.
        """
        candidate = root / relative
        if candidate.is_symlink():
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if not resolved.is_relative_to(root):
            return None
        if not resolved.is_file():
            return None
        return resolved

    def _proven_for(self, grant: LocalFsGrant, resolved: Path) -> bool:
        """Whether every step from the grant root to ``resolved`` is the prover's.

        Asked again at each step down rather than once at the root, because the
        proof at the root demonstrated write access to *that* directory only. A
        subdirectory another principal owns -- one engineer's checkout under a
        parent several of them can write -- was never proven by anything, and
        reading it would make write access to a directory into read access to
        everything beneath it.
        """
        current = grant.root
        for part in resolved.relative_to(grant.root).parts:
            current = current / part
            try:
                if self._owner_of(current) != grant.owner_uid:
                    return False
            except OSError:
                return False
        return True

    def read(self, grant: LocalFsGrant, path: str, digest: str) -> bytes | None:
        """Read one file and accept it only if it hashes to ``digest``."""
        validate_path(path)
        target = self._safe_join(grant.root, path)
        if target is None:
            return None
        if not self._proven_for(grant, target):
            return None
        try:
            if target.stat().st_size > self._size_cap:
                return None
            data = target.read_bytes()
        except OSError:
            return None
        if sha256_hex(data) != digest:
            # The worktree moved on since the manifest was built. The upload
            # path handles it; guessing would put the wrong bytes in the store
            # under a digest that says otherwise.
            return None
        return data

    def fill(
        self,
        grant: LocalFsGrant,
        *,
        org_id: str,
        entries: Sequence[ManifestEntry],
        content: ContentStore,
        analysis: AnalysisStore,
        needed: Collection[str] | None = None,
    ) -> LocalFsFill:
        """Satisfy as many declared digests as the worktree still matches.

        Only entries the view actually still needs are read, so a warm view
        costs no disk I/O at all. ``needed`` is that set, and the composition
        layer passes the view's own outstanding digests: a blob the
        organization holds but *this* view has never demonstrated possession of
        is outstanding, and reading it off the client's proven worktree is
        exactly the demonstration that settles it. Left unset the question falls
        back to what the organization holds, which is the right answer for a
        caller that has no view in hand.
        """
        wanted = {entry.content_digest for entry in entries}
        missing = set(needed) & wanted if needed is not None else set(content.missing(org_id, tuple(sorted(wanted))))
        filled: list[str] = []
        total = 0
        rejected = 0
        skipped = 0
        seen: set[str] = set()
        # Durable analysis exposes a re-entrant bulk transaction. Keep the
        # capability optional so the in-memory/reference implementation and
        # lightweight test doubles retain their existing behavior. Because the
        # durable content and analysis stores share one _Db, content.put() and
        # analysis.ensure() both join this outer transaction automatically.
        batch = getattr(analysis, "batch", None)
        with batch() if callable(batch) else nullcontext():
            for entry in entries:
                if entry.content_digest not in missing or entry.content_digest in seen:
                    skipped += 1
                    continue
                if entry.size > self._size_cap:
                    # Manifested but not uploaded, by policy. Reading it from disk
                    # would quietly undo the cap the upload path enforces.
                    skipped += 1
                    continue
                data = self.read(grant, entry.path, entry.content_digest)
                if data is None:
                    rejected += 1
                    continue
                seen.add(entry.content_digest)
                content.put(org_id, entry.content_digest, data)
                analysis.ensure(org_id, entry.content_digest, entry.parser_profile, data)
                filled.append(entry.content_digest)
                total += len(data)
        return LocalFsFill(filled=tuple(filled), bytes_read=total, rejected=rejected, skipped=skipped)


def write_proof(root: Path, relative: str, token: str) -> Path:
    """Write the same-host proof file a client presents. Mode 0600.

    ``token`` is the nonce the *server* issued for the session; writing it is
    the demonstration. Lives here rather than only in the client so that the
    server's own tests and a same-host operator tool agree on the format.
    """
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    return target
