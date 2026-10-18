"""Interfaces the three-layer index must satisfy.

Phase 11B owns these contracts; Phase 11C implements them for real. They encode
the decisions the plan settles, in a form a later implementation cannot quietly
reverse:

* **Layer 1 is content-keyed inside an opaque storage partition.** Every
  :class:`ContentStore` and :class:`AnalysisStore` method receives that
  partition identifier before a digest.
* **Holding bytes is not the same as being allowed to address them.**
  :class:`ContentProvenance` records which repository/content scope actually
  demonstrated possession of a blob. A manifest declaration alone never
  grants access to bytes.
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
    "REPOSITORY_GENERATION_ANALYZER_VERSION",
    "REPOSITORY_GENERATION_INDEX_VERSION",
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
    "RepositoryGeneration",
    "SemanticIndex",
    "SemanticMatch",
    "SemanticSearchResult",
    "SemanticUnit",
    "SweepResult",
    "SymbolDef",
    "SymbolRef",
    "ViewOpenResult",
    "ViewState",
    "ViewStore",
    "repository_generation_id",
    "repository_generation_scope_id",
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


@dataclass(frozen=True, slots=True, order=True)
class SemanticUnit:
    """One content-local semantic retrieval unit inside an immutable blob."""

    unit_id: str
    start_line: int
    end_line: int
    symbol: str
    embedding_version: str
    embedding: tuple[float, ...]

    def to_json(self) -> list[Any]:
        return [
            self.unit_id,
            self.start_line,
            self.end_line,
            self.symbol,
            self.embedding_version,
            list(self.embedding),
        ]

    @classmethod
    def from_json(cls, raw: Sequence[Any]) -> SemanticUnit:
        return cls(
            unit_id=str(raw[0]),
            start_line=int(raw[1]),
            end_line=int(raw[2]),
            symbol=str(raw[3]),
            embedding_version=str(raw[4]),
            embedding=tuple(float(value) for value in raw[5]),
        )


@dataclass(frozen=True, slots=True, order=True)
class SemanticMatch:
    path: str
    digest: str
    unit_id: str
    start_line: int
    end_line: int
    symbol: str
    score: float


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    matches: tuple[SemanticMatch, ...]
    candidates_scored: int
    total_units: int


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
    semantic_units: tuple[SemanticUnit, ...] = ()

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
            "semantic_units": [unit.to_json() for unit in self.semantic_units],
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
            semantic_units=tuple(SemanticUnit.from_json(item) for item in raw.get("semantic_units", ())),
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


REPOSITORY_GENERATION_ANALYZER_VERSION: Final[str] = "layer1-analysis/1"
REPOSITORY_GENERATION_INDEX_VERSION: Final[str] = "repository-index/1"


def repository_generation_id(
    org_id: str,
    repo_id: str,
    base_revision: str,
    manifest_root: str,
    *,
    analyzer_version: str = REPOSITORY_GENERATION_ANALYZER_VERSION,
    index_version: str = REPOSITORY_GENERATION_INDEX_VERSION,
) -> str:
    """Opaque tenant/repository-scoped identity for one immutable base."""
    payload = "\x00".join((org_id, repo_id, base_revision, manifest_root, analyzer_version, index_version)).encode()
    return "gen_" + hashlib.sha256(payload).hexdigest()[:32]


def repository_generation_scope_id(generation_id: str, content_subject: str) -> str:
    """Opaque derived-index scope for one immutable generation.

    Generation metadata is safe to share across Views, but path-sensitive
    derived facts may only be reused by Views with the same possession reach.
    Empty ``content_subject`` is the repository-shared scope; a named subject is
    hashed so user identity never becomes a storage key or metric label. The
    generation component remains visible only as its already-opaque id so
    retention can reclaim every scoped graph for a generation without knowing
    or enumerating subjects.
    """
    scope = "repository" if not content_subject else "subject:" + hashlib.sha256(content_subject.encode()).hexdigest()
    suffix = generation_id.removeprefix("gen_")
    return f"gscope_{suffix}_{hashlib.sha256(scope.encode()).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class RepositoryGeneration:
    """Immutable indexing identity shared by Views over the same base tree."""

    generation_id: str
    org_id: str
    repo_id: str
    base_revision: str
    manifest_root: str
    entry_count: int
    analyzer_version: str = REPOSITORY_GENERATION_ANALYZER_VERSION
    index_version: str = REPOSITORY_GENERATION_INDEX_VERSION

    def to_wire(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "base_revision": self.base_revision,
            "manifest_root": self.manifest_root,
            "entry_count": self.entry_count,
            "analyzer_version": self.analyzer_version,
            "index_version": self.index_version,
        }


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
    #: Immutable base indexing identity. Product stores always populate it.
    generation_id: str = ""
    #: True once every announced chunk has arrived *and* the assembled rows
    #: hash to the announced root. Warm opens key on this: reusing a root whose
    #: rows were never verified would let one view silently inherit another
    #: manifest's file set.
    manifest_complete: bool = False
    #: Opaque content-provenance scope fixed by the server composition when the
    #: View opens. The storage engine treats every value, including empty, as
    #: an exact key; it assigns no sharing or authorization meaning to it.
    #: Server-side only; never serialized to clients.
    content_subject: str = ""
    #: Opaque subject that opened this View. Ownership remains separate from
    #: content reach. Server-side only; never serialized to clients.
    owner_subject: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "repo_id": self.repo_id,
            "base_revision": self.base_revision,
            "view_revision": self.view_revision,
            "manifest_root": self.manifest_root,
            "generation_id": self.generation_id,
            "entry_count": self.entry_count,
            "lease_expires_at": round(self.lease_expires_at, 3),
            "need_manifest_chunks": list(self.pending_chunks),
            "manifest_complete": self.manifest_complete,
        }


@dataclass(frozen=True, slots=True)
class ViewContentReachability:
    """Required content reach for one revision-bound View.

    ``missing_digests`` preserves canonical first-path order and contains only
    digests the View declared but cannot address through its own provenance
    scope. ``required_paths`` counts manifest paths at or below the configured
    content cap; ``missing_paths`` counts how many of those paths reference a
    missing digest. This lets callers report degradation honestly without
    materializing the complete manifest into Python objects.
    """

    missing_digests: tuple[str, ...]
    required_paths: int
    missing_paths: int


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
    #: No implementation is installed for this layer (the semantic layer of a
    #: public-only server). Never becomes ready; answers are flagged, not failed.
    UNAVAILABLE = "unavailable"


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

    def get_many(self, org_id: str, digests: Sequence[str]) -> Mapping[str, bytes]:
        """Held content for ``digests``, keyed by digest; missing entries are omitted.

        Callers should keep requests bounded. Stores may split larger inputs
        internally to respect their own query/object-store limits.
        """

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

    def sweep_marked(self, before: float, *, limit: int | None = None, dry_run: bool = False) -> SweepResult:
        # Delete blobs marked unreferenced at or before before. limit bounds one
        # maintenance batch; dry_run reports the exact eligible batch without
        # deleting or clearing marks. Implementations preserve reference and
        # generation fencing.
        ...


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

    def restore(self, org_id: str, artifact: AnalysisArtifact) -> AnalysisArtifact:
        """Install a verified immutable artifact without re-reading source bytes.

        Hosted generation bundles use this only after bundle integrity and
        artifact identity have been verified. Implementations keep the same
        write-once semantics as :meth:`ensure` and must not count restoration
        as a fresh derivation.
        """

    def storage_size(self, org_id: str, artifact: AnalysisArtifact) -> int:
        """Upper bound on new derived bytes, in the same units as stored_bytes."""

    def stored_bytes(self, org_id: str) -> int: ...

    def count(self, org_id: str) -> int: ...

    def stored_semantic_units(
        self, org_id: str, keys: Sequence[tuple[str, str]], embedding_version: str
    ) -> Mapping[tuple[str, str], tuple[SemanticUnit, ...]]:
        """Chunk vectors derived after the fact, by ``(digest, parser_profile)``; absent keys are omitted."""

    def store_semantic_units(
        self,
        org_id: str,
        rows: Sequence[tuple[str, str, tuple[SemanticUnit, ...]]],
        embedding_version: str,
    ) -> None:
        """Keep derived chunk vectors beside an artifact stored without them.

        Write-once like the artifact: a key that is already stored keeps its
        units, and a key with no analysis artifact is ignored, so nothing
        outlives the digest's sweep.
        """

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
        document_count: int | None = None,
    ) -> Mapping[str, float]:
        """Digest -> IDF-aware score over term/trigram rows; broad callers may match any exact term."""

    def lexical_candidates(
        self,
        org_id: str,
        query: str,
        *,
        limit: int,
        include_grams: bool = True,
        include_substring_only: bool = True,
        ignored_terms: Sequence[str] = (),
    ) -> tuple[tuple[str, ...], bool]:
        """Bounded superset of digests that could score for ``query``.

        ``truncated`` means the caller must use the exact full-scope fallback;
        invisible content must never consume a cap and change an authorized
        answer.
        """

    def definition_candidates(self, org_id: str, name: str, *, limit: int) -> tuple[tuple[str, ...], bool]:
        """Bounded digest candidates for an exact definition name."""

    def definitions_named(self, org_id: str, digests: Sequence[str], name: str) -> Mapping[str, tuple[SymbolDef, ...]]:
        """Digest -> the definitions of ``name`` it contains."""

    def nearest(
        self, org_id: str, digests: Sequence[str], vector: Sequence[float], limit: int
    ) -> tuple[tuple[str, float], ...]:
        """Digest/cosine pairs, best first, over the Layer-1 embedding rows.

        A neutral storage primitive: exact scoring of caller-chosen digests
        against the stored per-blob vector. Public query code never calls it;
        the private semantic layer uses it for its not-ready fallback.
        """

    def sweep(self, org_id: str, digests: Sequence[str]) -> int:
        """Drop derived rows for ``digests``. Called only by blob GC."""


class ContentProvenance(Protocol):
    """Record which exact repository/content scope supplied a blob.

    A row is written only after the server receives bytes and verifies their
    digest. Naming a digest in a manifest writes nothing here. ``subject`` is
    intentionally an opaque scope key despite the historical parameter name;
    the storage implementation does not interpret empty or named values.
    """

    def record(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> int:
        """Note that ``subject`` supplied these bytes for ``repo_id``.

        Returns how many rows were new. Called only where the bytes were
        verified against the digest.
        """

    def held(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> frozenset[str]:
        """Of ``digests``, those this exact opaque scope demonstrated.

        Empty and named scope values are ordinary exact keys. A digest held in
        one scope is not visible through another.
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

        ``content_subject`` is :attr:`ViewState.content_subject`. Its meaning is
        chosen by the composition layer; this store persists it as an exact
        opaque provenance key.
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
        tombstone it. An entry identical to its base row clears its overlay;
        deleting a path absent from the base clears its addition. Revision *N*
        therefore reflects exactly the overlays
        committed up to *N* -- not fewer, not more.
        """

    def generation(self, org_id: str, view_id: str) -> RepositoryGeneration:
        """Immutable base generation bound to ``view_id``."""

    def generation_membership(self, org_id: str, view_id: str) -> Mapping[str, ManifestEntry]:
        """Base-generation rows only, before this View's overlay."""

    def overlay(self, org_id: str, view_id: str) -> tuple[Mapping[str, ManifestEntry], frozenset[str]]:
        """Current additions/replacements plus tombstones for this View."""

    def entry(self, org_id: str, view_id: str, path: str) -> ManifestEntry | None: ...

    def entries_for_digests(self, org_id: str, view_id: str, digests: Sequence[str]) -> Mapping[str, ManifestEntry]:
        """Effective View rows whose digest is in the bounded candidate set."""

    def path_candidates(
        self, org_id: str, view_id: str, needle: str, *, limit: int
    ) -> tuple[Mapping[str, ManifestEntry], bool]:
        """Bounded effective View rows whose path contains ``needle`` case-insensitively."""

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

    def refresh(
        self,
        org_id: str,
        view_id: str,
        *,
        budget: int | None = None,
        wait: bool = False,
        cancel: Callable[[], bool] | None = None,
        defer_cold: bool = False,
    ) -> IndexLayerStatus:
        """Bring the layer up to the view's committed revision, within budget.

        Foreground calls are non-blocking. Deferred workers may wait for build
        ownership and cooperatively cancel on shutdown.
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

    def export_generation(self, org_id: str, view_id: str) -> Mapping[str, Any] | None:
        """Portable immutable generation graph for this View's possession scope."""

    def restore_generation(self, org_id: str, view_id: str, payload: Mapping[str, Any]) -> bool:
        """Install a portable generation graph after validating its exact snapshot."""


class SemanticIndex(IndexLayer, Protocol):
    """Semantic retrieval for one View: the port only.

    The implementation -- unit derivation, embeddings, ANN and scoring -- is
    private; :mod:`.semantic` resolves it or supplies an explicitly
    ``UNAVAILABLE`` layer. Queries are text: how text becomes vectors is the
    implementation's business, not the caller's.
    """

    def search(self, org_id: str, view_id: str, query: str, limit: int) -> SemanticSearchResult:
        """Best units for *query* in the View; call only while the layer is ready."""

    def scan(
        self, org_id: str, view_id: str, digests: Sequence[str], query: str, limit: int
    ) -> tuple[tuple[str, float], ...]:
        """Not-ready fallback: digest/score pairs over caller-authorized *digests*."""

    def export_generation(self, org_id: str, view_id: str) -> Mapping[str, Any] | None: ...

    def restore_generation(self, org_id: str, view_id: str, payload: Mapping[str, Any]) -> bool: ...

    def release_idle(self) -> None:
        """Drop in-memory state no live View reads; maintenance calls it off the request path."""


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
    #: Shared low-cardinality scale telemetry. Repository leaves share one
    #: instance so measurements describe the server rather than one shard.
    ops: Any | None = None
    #: Optional deferred-index executor queue. Repository leaves share the local
    #: catalogue-backed store and preserve exact generation/View semantics.
    deferred_jobs: Any | None = None
    #: Optional hosted durable control-plane queue. This is deliberately separate
    #: from deferred_jobs: hosted capacity classes schedule work, while the index
    #: executor continues to use the existing IndexBackend algorithms.
    durable_jobs: Any | None = None
    #: True only when the authoritative ports may perform network I/O. HTTP
    #: handlers use this to move direct lifecycle calls onto the bounded worker
    #: executor while memory/SQLite stay inline with zero extra thread hop.
    authority_io_bound: bool = False
    #: True only when Layer-1 content bytes/catalogue may perform network I/O.
    #: Local memory/SQLite content remains inline on the compatibility path.
    content_io_bound: bool = False
    #: A partition root resolves an explicit repository. Leaf backends return
    #: themselves; no thread-local or request-global routing state is involved.
    repository_backend: Callable[[str, str], IndexBackend] | None = None
    #: Unbound protocol uploads use a dedicated staging partition when the
    #: backend is repository-partitioned. Leaf/shared backends return themselves.
    unbound_backend: Callable[[str], IndexBackend] | None = None
    partitions: Callable[[], tuple[IndexBackend, ...]] | None = None
    #: Identity of one repository leaf. The partition root and shared backends
    #: have no key; maintenance uses this to scope external retention references.
    partition_key: tuple[str, str] | None = None
    reclaim_storage: Callable[[], None] = _ready_noop
    disk_usage: Callable[[str], int] | None = None
    #: Hosted immutable-generation acceleration. Local backends leave these
    #: unset; hosted composition may hydrate/publish rebuildable bundles.
    warm_generation: Callable[[str, str], bool] | None = None
    publish_generation: Callable[[str, str], bool] | None = None
    #: Optional hosted durable indexing scheduler. It receives org/view/layer
    #: only; the hosted composition resolves exact repository-generation state
    #: from its authoritative ViewStore before admitting a job. Core/local
    #: backends leave this unset and keep their existing deferred worker.
    schedule_index: Callable[[str, str, str], bool] | None = None
    #: Hosted object stores may persist rebuildable generation artifacts outside
    #: the relational/content catalogues. Tenant deletion invokes this hook so
    #: derived bytes cannot outlive the tenant merely because they are rebuildable.
    purge_derived_objects: Callable[[str], int] | None = None
    #: Optional backend-native content-reachability query. SQLite implements
    #: this as one scoped anti-join over manifest/overlay + provenance, avoiding
    #: an O(View) Python membership reconstruction on warm open. Other backends
    #: may leave it unset and use the portable composition-layer fallback.
    view_content_reachability: Callable[[str, str, int], ViewContentReachability] | None = None

    def for_repository(self, org_id: str, repo_id: str) -> IndexBackend:
        return self if self.repository_backend is None else self.repository_backend(org_id, repo_id)

    def for_unbound(self, org_id: str) -> IndexBackend:
        return self if self.unbound_backend is None else self.unbound_backend(org_id)

    def for_view(self, org_id: str, view_id: str) -> IndexBackend:
        if self.repository_backend is None:
            return self
        state = self.views.get(org_id, view_id)
        return self.repository_backend(org_id, state.repo_id)
