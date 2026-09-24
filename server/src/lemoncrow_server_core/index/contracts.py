"""Interfaces the three-layer index must satisfy.

Phase 11B owns these contracts; Phase 11C implements them for real. They encode
the decisions the plan settles, in a form a later implementation cannot quietly
reverse:

* **Layer 1 is content-keyed and org-scoped.** Every :class:`ContentStore` and
  :class:`AnalysisStore` method takes ``org_id`` first. There is no method that
  answers a question about content without naming an organization, so there is
  no cross-tenant "do you have this digest?" oracle to expose.
* **Holding bytes is not the same as being allowed to address them.** Layer 1
  stays org-scoped and deduplicating; :class:`ContentProvenance` is the boundary
  above it, recording which repository scope actually *demonstrated* possession
  of a blob. A view addresses a digest through its own repository scope only --
  see :mod:`.boundary` -- so naming a digest in a manifest never, by itself,
  makes the bytes readable.
* **Path identity is not in Layer 1.** ``ContentStore`` never sees a path;
  ``AnalysisStore`` is keyed ``(org_id, digest, parser_profile_version)``.
  Paths appear first in :class:`ManifestEntry`, which is Layer 2.
* **``repo_id`` is server-issued.** :class:`RepoIdentityService` mints it. The
  claim a client may present (:class:`RepoIdentityClaim`) has no field for a
  raw git remote URL, and :meth:`RepoIdentityClaim.from_json` refuses a request
  that carries one.
* **Views are revisioned.** :meth:`ViewStore.apply_overlay` takes
  ``expected_view_revision`` and ``client_seq`` and returns the committed
  revision. Read-after-edit is a protocol guarantee, not a timing assumption.
* **A cold link layer degrades, it does not fail.** :class:`LinkIndexStatus`
  carries the reason that becomes ``degraded: true`` on the wire.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from ..context import validate_opaque_id
from ..errors import AgentAction, ErrorCode, ServerError

__all__ = [
    "AnalysisArtifact",
    "AnalysisStore",
    "ContentProvenance",
    "ContentRef",
    "ContentStore",
    "Edge",
    "EdgeKind",
    "ImportRef",
    "IndexBackend",
    "IndexLayer",
    "IndexLayerState",
    "IndexLayerStatus",
    "LinkBuildMode",
    "LinkBuildReport",
    "LinkIndex",
    "ManifestEntry",
    "RepoIdentityClaim",
    "RepoIdentityService",
    "SemanticIndex",
    "SweepResult",
    "SymbolDef",
    "SymbolRef",
    "ViewOpenResult",
    "ViewState",
    "ViewStore",
    "sha256_hex",
]

#: Bumped when the analysis produced for identical bytes changes. Part of the
#: Layer-1 derived-analysis key, never part of the physical content key.
DEFAULT_PARSER_PROFILE: Final[str] = "lexical-1"

_DIGEST_LEN: Final[int] = 64
_HEX: Final[frozenset[str]] = frozenset("0123456789abcdef")
_MAX_PATH_LEN: Final[int] = 1024


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_digest(value: object, field: str = "content_digest") -> str:
    if not isinstance(value, str) or len(value) != _DIGEST_LEN or not _HEX.issuperset(value):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be a lowercase hex sha256 digest",
            details={"field": field},
        )
    return value


def validate_path(value: object, field: str = "path") -> str:
    """Accept a repository-relative POSIX path.

    Absolute paths and ``..`` segments are refused: a view manifest addresses
    content inside one repository, and a path that escapes it is either a bug
    or an attempt to make the server describe the host filesystem.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_LEN:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be a non-empty repository-relative path under {_MAX_PATH_LEN} characters",
            details={"field": field, "limit": _MAX_PATH_LEN},
        )
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be a relative POSIX path",
            details={"field": field},
        )
    if any(segment in {"..", "."} for segment in value.split("/")):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must not contain '.' or '..' segments",
            details={"field": field},
        )
    return value


@dataclass(frozen=True, slots=True)
class ContentRef:
    """A stored Layer-1 blob. Identity is ``(org_id, digest)``; no path."""

    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one retention sweep deleted, partitioned by organization."""

    dropped: Mapping[str, tuple[str, ...]]
    bytes_reclaimed: Mapping[str, int]

    @property
    def blob_count(self) -> int:
        return sum(len(digests) for digests in self.dropped.values())

    @property
    def total_bytes(self) -> int:
        return sum(self.bytes_reclaimed.values())


@dataclass(frozen=True, slots=True, order=True)
class SymbolDef:
    """A definition found inside one blob.

    ``start``/``end`` are byte offsets into *those bytes*. There is deliberately
    no path and no module: the same definition in a renamed file is the same
    Layer-1 row.
    """

    name: str
    kind: str
    line: int
    start: int
    end: int
    exported: bool


@dataclass(frozen=True, slots=True, order=True)
class SymbolRef:
    """An identifier *use* inside one blob, and whether it is a call."""

    name: str
    line: int
    is_call: bool


@dataclass(frozen=True, slots=True, order=True)
class ImportRef:
    """An import *string* a blob contains, unresolved.

    ``module`` is what the source literally says. Turning it into a path needs
    to know which files the view contains, so that happens in Layer 3.
    ``bindings`` pairs the imported name with the local name it is bound to,
    which is what disambiguates a reference later.
    """

    module: str
    bindings: tuple[tuple[str, str], ...]
    line: int
    level: int = 0
    wildcard: bool = False


class EdgeKind(StrEnum):
    """Layer-3 edge kinds. Path-sensitive by construction."""

    IMPORT = "import"
    REFERENCE = "reference"
    CALL = "call"


@dataclass(frozen=True, slots=True, order=True)
class Edge:
    """One resolved cross-file edge inside one view.

    Ordered and hashable so an edge set has a canonical form: the differential
    oracle compares an incrementally-maintained set against a clean rebuild,
    and that comparison has to be exact rather than approximate.
    """

    kind: EdgeKind
    src_path: str
    dst_path: str
    symbol: str
    src_line: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "src_path": self.src_path,
            "dst_path": self.dst_path,
            "symbol": self.symbol,
            "src_line": self.src_line,
        }


class LinkBuildMode(StrEnum):
    """How a link-layer build was satisfied."""

    #: Nothing changed since the cached graph.
    UNCHANGED = "unchanged"
    #: Rebuilt from the cached ancestor by invalidating a dependent closure.
    INCREMENTAL = "incremental"
    #: Resolved every file in the view from scratch.
    FULL = "full"
    #: A full rebuild was required but exceeded the inline budget.
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class LinkBuildReport:
    """What one link-layer build actually did.

    Exposed because "the index is incremental" is a claim that has to be
    checkable: a test asserts the *mode*, the size of the dirty closure and the
    number of files re-resolved, not just that the answer came back.
    """

    mode: LinkBuildMode
    at_view_revision: int
    view_paths: int
    changed_paths: int
    dirty_paths: int
    resolved_paths: int
    edges_before: int
    edges_after: int
    reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "at_view_revision": self.at_view_revision,
            "view_paths": self.view_paths,
            "changed_paths": self.changed_paths,
            "dirty_paths": self.dirty_paths,
            "resolved_paths": self.resolved_paths,
            "edges_before": self.edges_before,
            "edges_after": self.edges_after,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AnalysisArtifact:
    """Derived analysis for ``(org_id, digest, parser_profile_version)``.

    Every field is a pure function of the bytes and the parser profile.
    ``lexical_terms``, ``trigrams``, ``symbol_spans``, ``definitions``,
    ``imports``, ``exports``, ``references`` and ``embedding`` are all
    content-local: offsets into the bytes and strings the bytes contain, never
    resolved against a path or a module. Path-sensitive resolution is Layer 3.
    """

    digest: str
    parser_profile: str
    line_count: int
    byte_count: int
    lexical_terms: tuple[str, ...]
    symbol_spans: tuple[tuple[str, int, int], ...]
    language: str = ""
    trigrams: tuple[str, ...] = ()
    definitions: tuple[SymbolDef, ...] = ()
    imports: tuple[ImportRef, ...] = ()
    exports: tuple[str, ...] = ()
    references: tuple[SymbolRef, ...] = ()
    embedding: tuple[float, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """JSON projection, for a store that persists the artifact as a row."""
        return {
            "digest": self.digest,
            "parser_profile": self.parser_profile,
            "line_count": self.line_count,
            "byte_count": self.byte_count,
            "language": self.language,
            "lexical_terms": list(self.lexical_terms),
            "symbol_spans": [list(span) for span in self.symbol_spans],
            "trigrams": list(self.trigrams),
            "definitions": [
                [item.name, item.kind, item.line, item.start, item.end, item.exported] for item in self.definitions
            ],
            "imports": [
                [item.module, [list(pair) for pair in item.bindings], item.line, item.level, item.wildcard]
                for item in self.imports
            ],
            "exports": list(self.exports),
            "references": [[item.name, item.line, item.is_call] for item in self.references],
            "embedding": list(self.embedding),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> AnalysisArtifact:
        return cls(
            digest=str(raw["digest"]),
            parser_profile=str(raw["parser_profile"]),
            line_count=int(raw["line_count"]),
            byte_count=int(raw["byte_count"]),
            lexical_terms=tuple(str(term) for term in raw.get("lexical_terms", ())),
            symbol_spans=tuple((str(span[0]), int(span[1]), int(span[2])) for span in raw.get("symbol_spans", ())),
            language=str(raw.get("language", "")),
            trigrams=tuple(str(gram) for gram in raw.get("trigrams", ())),
            definitions=tuple(
                SymbolDef(
                    name=str(item[0]),
                    kind=str(item[1]),
                    line=int(item[2]),
                    start=int(item[3]),
                    end=int(item[4]),
                    exported=bool(item[5]),
                )
                for item in raw.get("definitions", ())
            ),
            imports=tuple(
                ImportRef(
                    module=str(item[0]),
                    bindings=tuple((str(pair[0]), str(pair[1])) for pair in item[1]),
                    line=int(item[2]),
                    level=int(item[3]),
                    wildcard=bool(item[4]),
                )
                for item in raw.get("imports", ())
            ),
            exports=tuple(str(name) for name in raw.get("exports", ())),
            references=tuple(
                SymbolRef(name=str(item[0]), line=int(item[1]), is_call=bool(item[2]))
                for item in raw.get("references", ())
            ),
            embedding=tuple(float(value) for value in raw.get("embedding", ())),
        )


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One Layer-2 row: a path selecting a Layer-1 artifact."""

    path: str
    content_digest: str
    parser_profile: str
    mode: int
    size: int

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ManifestEntry:
        size = raw.get("size", 0)
        mode = raw.get("mode", 0o100644)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID, "size must be a non-negative integer", details={"field": "size"}
            )
        if not isinstance(mode, int) or isinstance(mode, bool) or mode < 0:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID, "mode must be a non-negative integer", details={"field": "mode"}
            )
        profile = raw.get("parser_profile", DEFAULT_PARSER_PROFILE)
        return cls(
            path=validate_path(raw.get("path")),
            content_digest=validate_digest(raw.get("content_digest")),
            parser_profile=validate_opaque_id(profile, "parser_profile"),
            mode=mode,
            size=size,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content_digest": self.content_digest,
            "parser_profile": self.parser_profile,
            "mode": self.mode,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class RepoIdentityClaim:
    """What a client may assert about which repository a view belongs to.

    Deliberately *not* a remote URL. A remote may carry credentials, may be one
    of several, and differs between forks and mirrors of the same repository.
    The server maps a claim to a canonical ``repo_id`` it issues itself.

    ``scm_provider``/``scm_repo_id``
        A canonical SCM identity (for example a provider's numeric repository
        id) when the deployment has one.
    ``fingerprint``
        Otherwise, an opaque client-computed fingerprint -- in practice the
        root-commit digest -- that is stable across clones without naming a
        host or a credential.
    """

    scm_provider: str
    scm_repo_id: str
    fingerprint: str

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> RepoIdentityClaim:
        for rejected in ("remote_url", "remote", "origin_url", "clone_url"):
            if rejected in raw:
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    (
                        f"repo_identity.{rejected} is not accepted: repository identity is server-issued "
                        "and a git remote may carry credentials or a fork-specific spelling"
                    ),
                    details={"field": f"repo_identity.{rejected}"},
                    action=AgentAction.FIX_REQUEST,
                )
        provider = raw.get("scm_provider", "")
        repo = raw.get("scm_repo_id", "")
        fingerprint = raw.get("fingerprint", "")
        has_scm = bool(provider) and bool(repo)
        if not has_scm and not fingerprint:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "repo_identity needs either scm_provider+scm_repo_id or an opaque fingerprint",
                details={"field": "repo_identity"},
            )
        return cls(
            scm_provider=validate_opaque_id(provider, "repo_identity.scm_provider") if provider else "",
            scm_repo_id=validate_opaque_id(repo, "repo_identity.scm_repo_id") if repo else "",
            fingerprint=validate_opaque_id(fingerprint, "repo_identity.fingerprint") if fingerprint else "",
        )

    def key(self) -> str:
        """Stable lookup key. Canonical SCM identity wins over a fingerprint."""
        if self.scm_provider and self.scm_repo_id:
            return f"scm:{self.scm_provider}:{self.scm_repo_id}"
        return f"fp:{self.fingerprint}"


@dataclass(frozen=True, slots=True)
class ViewState:
    """Layer-2 view metadata."""

    view_id: str
    org_id: str
    repo_id: str
    base_revision: str
    view_revision: int
    manifest_root: str
    entry_count: int
    lease_expires_at: float
    pending_chunks: tuple[str, ...]
    #: True once every announced chunk has arrived *and* the assembled rows
    #: hash to the announced root. Warm opens key on this: reusing a root whose
    #: rows were never verified would let one view silently inherit another
    #: manifest's file set.
    manifest_complete: bool = False
    #: Whose demonstrated possession this view may address content through,
    #: inside :attr:`repo_id`. Empty means the repository scope as a whole --
    #: any engineer's demonstration counts -- which is what a binding naming
    #: the repository as a sharing unit earns. A named subject means only that
    #: engineer's own, which is what every other principal gets, so a blob a
    #: colleague uploaded reads exactly like one nobody has ever held.
    #:
    #: Deliberately not on the wire. It is a server-side fact about the opener,
    #: and echoing it would tell a client which of its colleagues share a
    #: repository with it.
    content_subject: str = ""
    #: The subject that opened this view. Separate from ``content_subject``:
    #: repository sharing may widen content reach while ownership remains with
    #: one opener. Server-side only; never serialized to clients.
    owner_subject: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "repo_id": self.repo_id,
            "base_revision": self.base_revision,
            "view_revision": self.view_revision,
            "manifest_root": self.manifest_root,
            "entry_count": self.entry_count,
            "lease_expires_at": round(self.lease_expires_at, 3),
            "need_manifest_chunks": list(self.pending_chunks),
            "manifest_complete": self.manifest_complete,
        }


@dataclass(frozen=True, slots=True)
class ViewOpenResult:
    """Answer to ``POST /v1/views/open``.

    ``declared_digests`` is what the manifest root says this view contains --
    a Layer-2 fact. Which of those the organization is actually *missing* is a
    Layer-1 fact, so the join happens in the composition layer against the
    caller's own :class:`ContentStore` and never crosses an organization.
    Keeping the two apart is what stops a view store from accidentally becoming
    a cross-tenant content-existence oracle.
    """

    state: ViewState
    declared_digests: tuple[str, ...]
    have_link_ancestor: bool
    warm: bool
    #: The server does not know this root and the client announced no chunks to
    #: fill it with. Not an error -- the client simply has to announce chunks --
    #: but the view is unusable until it does, and saying so is cheaper than
    #: letting it look like an empty repository.
    needs_manifest: bool = False

    def to_wire(self) -> dict[str, Any]:
        payload = self.state.to_wire()
        payload.update(
            {
                "have_link_ancestor": self.have_link_ancestor,
                "warm": self.warm,
                "needs_manifest": self.needs_manifest,
            }
        )
        return payload


class IndexLayerState(StrEnum):
    COLD = "cold"
    BUILDING = "building"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class IndexLayerStatus:
    """Readiness of one derived layer for one view.

    Shared by the link layer and the semantic layer because the degradation
    contract is the same for both: not-ready is a flag on an answer that still
    returns, never a reason to fail the session.
    """

    state: IndexLayerState
    reason: str
    covered_paths: int
    at_view_revision: int

    @property
    def ready(self) -> bool:
        return self.state is IndexLayerState.READY

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "covered_paths": self.covered_paths,
            "at_view_revision": self.at_view_revision,
        }


class ContentStore(Protocol):
    """Layer 1, physical. Keyed ``(org_id, sha256(content))``."""

    def missing(self, org_id: str, digests: Sequence[str]) -> tuple[str, ...]:
        """Which of ``digests`` this organization has not uploaded."""

    def put(self, org_id: str, digest: str, data: bytes) -> ContentRef:
        """Store ``data``, verifying it hashes to ``digest``."""

    def get(self, org_id: str, digest: str) -> bytes | None:
        """Content for this organization, or ``None``."""

    def stored_bytes(self, org_id: str) -> int: ...

    def storage_size(self, org_id: str, data: bytes) -> int:
        """Bytes needed for a new blob, including any encryption envelope."""

    def blob_count(self, org_id: str) -> int: ...

    def sweep(self, referenced: Mapping[str, frozenset[str]]) -> int:
        """Drop unreferenced blobs now. ``referenced`` maps org_id -> live digests."""

    def mark_unreferenced(self, referenced: Mapping[str, frozenset[str]], now: float) -> int:
        """Start (or clear) the retention clock. Returns how many are marked.

        Two-phase on purpose. A blob can become unreferenced because the last
        view holding it was evicted a second before the developer reopens it;
        deleting on the first sweep would turn a normal reconnect into a full
        re-upload. The retention window is the difference between a cache and a
        shredder.
        """

    def sweep_marked(self, before: float) -> SweepResult:
        """Delete blobs marked unreferenced at or before ``before``.

        Reports the dropped digests *and* the bytes reclaimed, per
        organization. Both in one answer because the size of a blob is only
        knowable while it still exists: a caller that deleted first and
        measured afterwards would always report zero.
        """


class AnalysisStore(Protocol):
    """Layer 1, derived. Keyed ``(org_id, digest, parser_profile_version)``.

    The three query methods all take an explicit ``digests`` set, which the
    caller computes from a view's membership rows. That is how a Layer-1 query
    stays scoped to one view without Layer 1 knowing what a view is -- and it
    is why no method here can be used to ask whether an *arbitrary* digest
    exists: a digest the caller did not already select is simply not searched.
    """

    def get(self, org_id: str, digest: str, parser_profile: str) -> AnalysisArtifact | None: ...

    def metadata(self, org_id: str, digest: str, parser_profile: str) -> tuple[str, int] | None:
        """Language and line count without hydrating the full analysis payload."""

    def ensure(
        self,
        org_id: str,
        digest: str,
        parser_profile: str,
        data: bytes,
        *,
        artifact: AnalysisArtifact | None = None,
    ) -> AnalysisArtifact:
        """Return the artifact; an upload may supply its already prepared analysis."""

    def storage_size(self, org_id: str, artifact: AnalysisArtifact) -> int:
        """Upper bound on new derived bytes, in the same units as stored_bytes."""

    def stored_bytes(self, org_id: str) -> int: ...

    def count(self, org_id: str) -> int: ...

    def lexical_matches(
        self,
        org_id: str,
        digests: Sequence[str],
        query: str,
        *,
        match_any_term: bool = False,
        include_grams: bool = True,
        include_substring_only: bool = True,
        ignored_terms: Sequence[str] = (),
    ) -> Mapping[str, float]:
        """Digest -> IDF-aware score over term/trigram rows; broad callers may match any exact term."""

    def definitions_named(self, org_id: str, digests: Sequence[str], name: str) -> Mapping[str, tuple[SymbolDef, ...]]:
        """Digest -> the definitions of ``name`` it contains."""

    def nearest(
        self, org_id: str, digests: Sequence[str], vector: Sequence[float], limit: int
    ) -> tuple[tuple[str, float], ...]:
        """Digest/cosine pairs, best first, over the Layer-1 embedding rows."""

    def sweep(self, org_id: str, digests: Sequence[str]) -> int:
        """Drop derived rows for ``digests``. Called only by blob GC."""


class ContentProvenance(Protocol):
    """Which repository scope, and which engineer, actually supplied a blob.

    Layer 1 keeps its ``(org_id, sha256(content))`` key and keeps deduplicating
    inside an organization; this is the boundary *above* it. A row is written
    only where the server has had the bytes in its hand and hashed them -- an
    upload, or a read off a proven same-host worktree -- so naming a digest in a
    manifest writes nothing here. That is the whole mechanism by which
    declaration stops being possession.

    The key is ``(org_id, repo_id, subject)`` rather than ``(org_id, repo_id)``
    because ``repo_id`` is derived from a claim the *client* makes at view open,
    and a root-commit fingerprint is no harder to learn than a digest. Attribution
    to the engineer who demonstrated possession is what stops an attacker
    re-claiming a colleague's repository identity and inheriting their content.
    """

    def record(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> int:
        """Note that ``subject`` supplied these bytes for ``repo_id``.

        Returns how many rows were new. Called only where the bytes were
        verified against the digest.
        """

    def held(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> frozenset[str]:
        """Of ``digests``, those this scope has demonstrated possession of.

        An empty ``subject`` asks about the repository as a whole; a named one
        asks only about that engineer. Either way a digest with no row reads as
        absent, and reads identically whether the organization holds the bytes
        under some other scope or has never seen them at all.
        """

    def forget(self, org_id: str, digests: Sequence[str]) -> int:
        """Drop rows for content that no longer exists. Called only by blob GC.

        Provenance never outlives the bytes it describes: a row left behind
        would make a re-uploaded digest addressable to a scope that never
        demonstrated anything.
        """


class ViewStore(Protocol):
    """Layer 2. Views, manifests and the revisioned overlay."""

    def open(
        self,
        *,
        org_id: str,
        repo_id: str,
        base_revision: str,
        manifest_root: str,
        chunk_ids: Sequence[str],
        lease_s: float,
        content_subject: str = "",
        owner_subject: str = "",
    ) -> ViewOpenResult:
        """Open a view bound to one repository and one content scope.

        ``content_subject`` is :attr:`ViewState.content_subject`. The default is
        the repository scope as a whole, which is *not* "unrestricted": it still
        requires that somebody demonstrated possession in this repository. The
        composition layer passes the opener's subject whenever no binding names
        the repository as a sharing unit.
        """

    def get(self, org_id: str, view_id: str) -> ViewState:
        """Raise ``view_unknown`` when the view is absent *or* another org's."""

    def view_owner(self, org_id: str, view_id: str) -> str | None: ...

    def view_ids_for_org(self, org_id: str) -> tuple[str, ...]: ...

    def view_ids_for_user(self, org_id: str, subject: str) -> tuple[str, ...]: ...

    def view_count_for_user(self, org_id: str, subject: str) -> int: ...

    def put_manifest_chunk(
        self,
        *,
        org_id: str,
        view_id: str,
        chunk_id: str,
        entries: Sequence[ManifestEntry],
    ) -> ViewState: ...

    def apply_overlay(
        self,
        *,
        org_id: str,
        view_id: str,
        expected_view_revision: int,
        client_seq: int,
        entries: Sequence[ManifestEntry],
        removed_paths: Sequence[str],
    ) -> ViewState:
        """Commit an overlay write and return the state at the new revision.

        Three refusals and one replay, in this order:

        * ``client_seq`` already acknowledged -> return the acknowledgement
          that write produced, unchanged, and commit nothing. This is a retry
          after a lost ACK, and the answer must be the *recorded* one rather
          than the view's current state: another writer may have committed in
          between, and a client that adopted that later revision would pass the
          acknowledged-revision check while reading an overlay it never saw.
        * ``client_seq`` at or below the last one, with no replay row ->
          ``payload_invalid``. Fail-closed: a retry whose replay row has been
          reclaimed is refused, never applied twice.
        * ``expected_view_revision`` behind the committed one ->
          ``view_revision_conflict``.

        Otherwise the overlay is applied and the revision advances by exactly
        one. ``entries`` clear any tombstone at their path; ``removed_paths``
        tombstone it. Revision *N* therefore reflects exactly the overlays
        committed up to *N* -- not fewer, not more.
        """

    def entry(self, org_id: str, view_id: str, path: str) -> ManifestEntry | None: ...

    def membership(self, org_id: str, view_id: str) -> Mapping[str, ManifestEntry]:
        """The effective manifest: base rows, minus tombstones, plus overlay.

        These are the rows that filter every query. A Layer-1 search runs over
        the digests this mapping selects and over nothing else, which is what
        keeps one immutable artifact shared by N views instead of copied.
        """

    def declared_digests(self, org_id: str, view_id: str) -> tuple[str, ...]:
        """Digests this view's own manifest names.

        The resume route answers only about these, so a caller can never learn
        anything about a digest it did not itself declare -- a stronger
        property than org scoping alone, and the one that makes the missing
        route provably not an existence oracle.
        """

    def renew(self, org_id: str, view_id: str, lease_s: float) -> ViewState: ...

    def sweep_manifest_roots(self, before: float) -> int:
        """Drop manifest roots no live view names, last opened at or before ``before``.

        A manifest root outlives the views that named it. The thin client's
        ``SessionStart`` hook opens a view, synchronizes and closes it again, so
        a root reclaimed with its last view would make every session cold and
        re-send thousands of rows for an unchanged tree. Roots are org-scoped,
        content-addressed and immutable, so keeping one is safe; the retention
        window is what bounds it, exactly as for an unreferenced blob.
        """

    def close(self, org_id: str, view_id: str) -> None:
        """Best effort; a lease sweep must clean views this never reaches."""

    def evict_expired(self, now: float) -> tuple[str, ...]: ...

    def referenced_digests(self) -> Mapping[str, frozenset[str]]: ...


class IndexLayer(Protocol):
    """A derived, per-view layer with an explicit readiness state."""

    def status(self, org_id: str, view_id: str) -> IndexLayerStatus: ...

    def note_revision(self, org_id: str, view_id: str, view_revision: int) -> None:
        """Record that the view advanced; invalidates readiness."""

    def refresh(self, org_id: str, view_id: str) -> IndexLayerStatus:
        """Bring the layer up to the view's committed revision, within budget.

        Bounded on purpose. A build that would exceed the inline budget leaves
        the layer not-ready and returns saying so, which the degradation
        contract turns into a flagged answer rather than a blocked session.
        """

    def forget(self, org_id: str, view_id: str) -> None: ...

    def forget_view(self, view_id: str) -> None:
        """Drop everything held for ``view_id``, without naming an owner.

        Lease eviction discovers that a view is gone *after* the row that said
        which organization owned it, so the sweep needs a handle keyed by the
        view alone. View ids are server-issued and unique, so this is exact
        rather than a scan for something that looks right.
        """

    def mark_ready(self, org_id: str, view_id: str, *, view_revision: int, covered_paths: int) -> None:
        """Record that a build completed at ``view_revision``."""


class LinkIndex(IndexLayer, Protocol):
    """Layer 3. Path-sensitive edges (imports, references, calls), per view.

    Built incrementally from the nearest cached ancestor view. The differential
    oracle in the plan -- incremental result must equal a clean rebuild -- is a
    property of the implementation, and this contract deliberately exposes the
    ``at_view_revision`` the edges correspond to so that oracle has something
    to compare against.
    """

    def edges(self, org_id: str, view_id: str) -> tuple[Edge, ...]:
        """Every edge currently held for this view, in canonical order."""

    def edges_for_paths(self, org_id: str, view_id: str, paths: Sequence[str]) -> tuple[Edge, ...]:
        """Edges touching any requested path, in canonical order."""

    def last_build(self, org_id: str, view_id: str) -> LinkBuildReport | None:
        """What the most recent build did, or ``None`` if it never ran."""


class SemanticIndex(IndexLayer, Protocol):
    """Embedding-backed search. Optional Layer-1 vectors, per-view membership."""


class RepoIdentityService(Protocol):
    """Issues the canonical, org-scoped ``repo_id``."""

    def canonical_repo_id(self, org_id: str, claim: RepoIdentityClaim) -> str: ...


def _ready_noop() -> None:
    return None


@dataclass(frozen=True, slots=True)
class IndexBackend:
    """The ports the tool API depends on, bundled for injection.

    ``check_ready`` is deliberately tiny and side-effect free: readiness may
    call it every few seconds, so it must prove the authoritative store is
    reachable without rebuilding derived layers or taking a long-lived write
    lock. Backends that have no external durability requirement may keep the
    no-op default.
    """

    content: ContentStore
    analysis: AnalysisStore
    views: ViewStore
    links: LinkIndex
    semantic: SemanticIndex
    repos: RepoIdentityService
    provenance: ContentProvenance
    check_ready: Callable[[], None] = _ready_noop
    close_backend: Callable[[], None] = _ready_noop
    #: True only when the authoritative ports may perform network I/O. HTTP
    #: handlers use this to move direct lifecycle calls onto the bounded worker
    #: executor while memory/SQLite stay inline with zero extra thread hop.
    authority_io_bound: bool = False
    #: True only when Layer-1 content bytes/catalogue may perform network I/O.
    #: Local memory/SQLite content remains inline on the compatibility path.
    content_io_bound: bool = False
