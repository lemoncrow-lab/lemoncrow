"""In-process implementation of the index contracts.

This is the reference behaviour, and it is complete: org-scoped content
addressing with digest verification, write-once derived analysis, verified
manifest roots shared across views, monotonic revisions with idempotent
``client_seq`` replay, view leases, a two-phase reference-counted blob sweep,
and a real incremental Layer 3 driven by :mod:`.layers`.

It differs from :mod:`.sqlite` in exactly one respect -- nothing survives the
process -- which is why both are exercised by the same conformance suite and
the same differential oracle. Where the two could drift, they share code: the
analysis derivation, the query scoring rule and the link algorithm are all
single implementations that both stores call.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from ..context import new_opaque_id
from ..errors import AgentAction, ErrorCode, ServerError
from .analysis import LexicalQuery, analyze, cosine, parse_query, score_weighted_match
from .contracts import (
    AnalysisArtifact,
    ContentRef,
    IndexBackend,
    ManifestEntry,
    RepoIdentityClaim,
    SweepResult,
    SymbolDef,
    ViewOpenResult,
    ViewState,
    sha256_hex,
)
from .layers import (
    DEFAULT_INLINE_REBUILD_BUDGET,
    InMemoryLinkGraphStore,
    LayerSources,
    LinkLayer,
    SemanticLayer,
)
from .manifest import manifest_root as canonical_manifest_root

__all__ = [
    "DEFAULT_CONTENT_SIZE_CAP",
    "InMemoryAnalysisStore",
    "InMemoryContentProvenance",
    "InMemoryContentStore",
    "InMemoryRepoIdentityService",
    "InMemoryViewStore",
    "build_memory_backend",
]

_REPLAY_HISTORY: Final[int] = 64

#: Files larger than this are manifested but not uploaded. Mirrors the default
#: in :class:`~lemoncrow_server.limits.Limits`; the server passes
#: its configured value in.
DEFAULT_CONTENT_SIZE_CAP: Final[int] = 1024 * 1024


class InMemoryContentStore:
    """Layer 1 physical store. Nothing crosses an ``org_id``."""

    __slots__ = ("_blobs", "_lock", "_unreferenced")

    def __init__(self) -> None:
        self._blobs: dict[str, dict[str, bytes]] = {}
        self._unreferenced: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def missing(self, org_id: str, digests: Sequence[str]) -> tuple[str, ...]:
        with self._lock:
            held = self._blobs.get(org_id, {})
            # Order-preserving and de-duplicated: the client uploads in the
            # order it is told to, and a repeated digest is one upload.
            seen: set[str] = set()
            out: list[str] = []
            for digest in digests:
                if digest in held or digest in seen:
                    continue
                seen.add(digest)
                out.append(digest)
            return tuple(out)

    def put(self, org_id: str, digest: str, data: bytes) -> ContentRef:
        actual = sha256_hex(data)
        if actual != digest:
            raise ServerError(
                ErrorCode.BLOB_DIGEST_MISMATCH,
                "uploaded content does not match the declared digest",
                details={"declared": digest, "computed": actual, "size": len(data)},
                action=AgentAction.FIX_REQUEST,
            )
        with self._lock:
            self._blobs.setdefault(org_id, {})[digest] = data
            self._unreferenced.pop((org_id, digest), None)
        return ContentRef(digest=digest, size=len(data))

    def storage_size(self, org_id: str, data: bytes) -> int:
        return len(data)

    def get(self, org_id: str, digest: str) -> bytes | None:
        with self._lock:
            return self._blobs.get(org_id, {}).get(digest)

    def stored_bytes(self, org_id: str) -> int:
        with self._lock:
            return sum(len(blob) for blob in self._blobs.get(org_id, {}).values())

    def blob_count(self, org_id: str) -> int:
        with self._lock:
            return len(self._blobs.get(org_id, {}))

    def sweep(self, referenced: Mapping[str, frozenset[str]]) -> int:
        """Mark-and-sweep with no retention. An org with no live view loses all."""
        dropped = 0
        with self._lock:
            for org_id in list(self._blobs):
                live = referenced.get(org_id, frozenset())
                held = self._blobs[org_id]
                for digest in list(held):
                    if digest not in live:
                        del held[digest]
                        self._unreferenced.pop((org_id, digest), None)
                        dropped += 1
                if not held:
                    del self._blobs[org_id]
        return dropped

    def mark_unreferenced(self, referenced: Mapping[str, frozenset[str]], now: float) -> int:
        with self._lock:
            for org_id, held in self._blobs.items():
                live = referenced.get(org_id, frozenset())
                for digest in held:
                    key = (org_id, digest)
                    if digest in live:
                        self._unreferenced.pop(key, None)
                    elif key not in self._unreferenced:
                        self._unreferenced[key] = now
            return len(self._unreferenced)

    def sweep_marked(self, before: float) -> SweepResult:
        dropped: dict[str, list[str]] = {}
        reclaimed: dict[str, int] = {}
        with self._lock:
            for (org_id, digest), marked_at in list(self._unreferenced.items()):
                if marked_at > before:
                    continue
                held = self._blobs.get(org_id)
                if held is not None and digest in held:
                    reclaimed[org_id] = reclaimed.get(org_id, 0) + len(held[digest])
                    del held[digest]
                    dropped.setdefault(org_id, []).append(digest)
                    if not held:
                        del self._blobs[org_id]
                del self._unreferenced[(org_id, digest)]
        return SweepResult(
            dropped={org_id: tuple(sorted(digests)) for org_id, digests in dropped.items()},
            bytes_reclaimed=reclaimed,
        )

    @property
    def marked_unreferenced(self) -> int:
        with self._lock:
            return len(self._unreferenced)


class InMemoryContentProvenance:
    """Who demonstrated possession of what, keyed ``(org, repo, subject)``.

    A set per scope rather than a row per digest: the two questions asked of it
    are both "which of these digests", and a scope holds at most one
    organization's working set.
    """

    __slots__ = ("_held", "_lock")

    def __init__(self) -> None:
        self._held: dict[tuple[str, str, str], set[str]] = {}
        self._lock = threading.Lock()

    def record(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> int:
        if not repo_id or not subject or not digests:
            # An unattributed row would be indistinguishable from the
            # repository-wide scope on read, which is the one thing that must
            # not be forgeable. Nothing is better than something unattributable.
            return 0
        with self._lock:
            scope = self._held.setdefault((org_id, repo_id, subject), set())
            before = len(scope)
            scope.update(digests)
            return len(scope) - before

    def held(self, org_id: str, repo_id: str, subject: str, digests: Sequence[str]) -> frozenset[str]:
        wanted = set(digests)
        if not wanted or not repo_id:
            return frozenset()
        with self._lock:
            if subject:
                return frozenset(wanted & self._held.get((org_id, repo_id, subject), set()))
            found: set[str] = set()
            for (owner, repo, _subject), scope in self._held.items():
                if owner == org_id and repo == repo_id:
                    found |= wanted & scope
            return frozenset(found)

    def forget(self, org_id: str, digests: Sequence[str]) -> int:
        doomed = set(digests)
        if not doomed:
            return 0
        dropped = 0
        with self._lock:
            for key in list(self._held):
                if key[0] != org_id:
                    continue
                scope = self._held[key]
                overlap = scope & doomed
                if not overlap:
                    continue
                scope -= overlap
                dropped += len(overlap)
                if not scope:
                    del self._held[key]
        return dropped

    @property
    def rows(self) -> int:
        """Total possession rows, for the storage report and for tests."""
        with self._lock:
            return sum(len(scope) for scope in self._held.values())


class InMemoryAnalysisStore:
    """Layer 1 derived store, written exactly once per scoped key."""

    __slots__ = ("_artifacts", "_derivations", "_lock")

    def __init__(self) -> None:
        self._artifacts: dict[tuple[str, str, str], AnalysisArtifact] = {}
        self._derivations = 0
        self._lock = threading.Lock()

    @property
    def derivations(self) -> int:
        """How many times analysis was actually computed.

        The three-worktree storage claim is the difference between this and the
        number of manifest rows, so it is observable rather than asserted.
        """
        return self._derivations

    def get(self, org_id: str, digest: str, parser_profile: str) -> AnalysisArtifact | None:
        with self._lock:
            return self._artifacts.get((org_id, digest, parser_profile))

    def metadata(self, org_id: str, digest: str, parser_profile: str) -> tuple[str, int] | None:
        artifact = self.get(org_id, digest, parser_profile)
        return None if artifact is None else (artifact.language, artifact.line_count)

    def ensure(
        self,
        org_id: str,
        digest: str,
        parser_profile: str,
        data: bytes,
        *,
        artifact: AnalysisArtifact | None = None,
    ) -> AnalysisArtifact:
        key = (org_id, digest, parser_profile)
        with self._lock:
            existing = self._artifacts.get(key)
            if existing is not None:
                return existing
        artifact = artifact if artifact is not None else analyze(digest, parser_profile, data)
        with self._lock:
            existing = self._artifacts.get(key)
            if existing is not None:
                return existing
            self._artifacts[key] = artifact
            self._derivations += 1
        return artifact

    def count(self, org_id: str) -> int:
        with self._lock:
            return sum(1 for key in self._artifacts if key[0] == org_id)

    def stored_bytes(self, org_id: str) -> int:
        """Approximate size of the derived rows, for the storage report."""
        with self._lock:
            total = 0
            for (owner, _digest, _profile), artifact in self._artifacts.items():
                if owner != org_id:
                    continue
                total += self.storage_size(org_id, artifact)
            return total

    def storage_size(self, org_id: str, artifact: AnalysisArtifact) -> int:
        return (
            sum(len(term) for term in artifact.lexical_terms)
            + sum(len(gram) for gram in artifact.trigrams)
            + 24 * len(artifact.definitions)
            + 16 * len(artifact.references)
            + 8 * len(artifact.embedding)
        )

    def _for_org(self, org_id: str, digests: Sequence[str]) -> list[tuple[str, AnalysisArtifact]]:
        wanted = set(digests)
        with self._lock:
            return [
                (key[1], artifact) for key, artifact in self._artifacts.items() if key[0] == org_id and key[1] in wanted
            ]

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
        parsed: LexicalQuery = parse_query(query)
        ignored = set(ignored_terms)
        if parsed.empty:
            return {}
        rows = self._for_org(org_id, digests)
        if not rows:
            return {}
        terms_by_digest: dict[str, set[str]] = {}
        grams_by_digest: dict[str, set[str]] = {}
        for digest, artifact in rows:
            terms_by_digest.setdefault(digest, set()).update(artifact.lexical_terms)
            grams_by_digest.setdefault(digest, set()).update(artifact.trigrams)
        gram_df = {gram: sum(1 for grams in grams_by_digest.values() if gram in grams) for gram in parsed.grams}
        anchors = set(sorted(parsed.grams, key=lambda gram: (gram_df.get(gram, 0), gram))[:3])
        term_df = {term: sum(1 for terms in terms_by_digest.values() if term in terms) for term in parsed.terms}
        document_count = len(terms_by_digest)
        scores: dict[str, float] = {}
        for digest, terms in terms_by_digest.items():
            grams = grams_by_digest.get(digest, set())
            matched_terms = tuple(
                term
                for term in parsed.terms
                if term in terms and (term not in ignored or (bool(anchors) and anchors.issubset(grams)))
            )
            inferred_gram_hits = len(parsed.grams) if matched_terms and not include_grams else 0
            gram_hits = (
                sum(1 for gram in parsed.grams if gram in grams)
                if include_grams and (matched_terms or include_substring_only)
                else inferred_gram_hits
            )
            score = score_weighted_match(
                parsed,
                matched_terms=matched_terms,
                term_document_frequency=term_df,
                document_count=document_count,
                gram_hits=gram_hits,
                match_any_term=match_any_term,
            )
            if score > 0:
                scores[digest] = score
        return scores

    def definitions_named(self, org_id: str, digests: Sequence[str], name: str) -> Mapping[str, tuple[SymbolDef, ...]]:
        out: dict[str, set[SymbolDef]] = {}
        for digest, artifact in self._for_org(org_id, digests):
            hits = {item for item in artifact.definitions if item.name == name}
            if hits:
                out.setdefault(digest, set()).update(hits)
        return {digest: tuple(sorted(items)) for digest, items in out.items()}

    def nearest(
        self, org_id: str, digests: Sequence[str], vector: Sequence[float], limit: int
    ) -> tuple[tuple[str, float], ...]:
        scored: dict[str, float] = {}
        for digest, artifact in self._for_org(org_id, digests):
            similarity = cosine(vector, artifact.embedding)
            if similarity > 0.0:
                scored[digest] = max(scored.get(digest, 0.0), similarity)
        ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
        return tuple(ranked[:limit])

    def sweep(self, org_id: str, digests: Sequence[str]) -> int:
        doomed = set(digests)
        with self._lock:
            keys = [key for key in self._artifacts if key[0] == org_id and key[1] in doomed]
            for key in keys:
                del self._artifacts[key]
            return len(keys)


@dataclass
class _ManifestRoot:
    """Rows for one canonical manifest root, shared by every view using it.

    :attr:`entries` holds **proved** rows and nothing else: they are written in
    one step, by the view whose own assembled manifest hashed to this root.
    Rows a fill is still delivering stay on the view that delivered them (see
    :attr:`_View.staged`), because a root's rows are shared by everyone who
    names it and unproved rows shared that way are an injection point: a
    colleague who reached a victim's in-flight root could add a row to it and
    the victim's assembly would then never hash to the root she announced --
    permanently, since the root outlives her view and a reopen lands on it
    again.
    """

    root: str
    org_id: str
    entries: dict[str, ManifestEntry] = field(default_factory=dict)
    complete: bool = False
    #: When a view last named this root. A root outlives the views that named
    #: it -- see :meth:`_MemoryViewStore.sweep_manifest_roots` -- and this is
    #: what the retention window is measured from.
    last_referenced_at: float = 0.0


@dataclass
class _View:
    view_id: str
    org_id: str
    repo_id: str
    base_revision: str
    root: str
    revision: int
    lease_expires_at: float
    #: See :attr:`~.contracts.ViewState.content_subject`. Fixed at open, because
    #: a view's content reach must not depend on who is asking: the derived
    #: layers and the materialized tree are cached per view, and a reach that
    #: widened for one caller would still be cached for the next.
    content_subject: str = ""
    #: The subject that opened the view. Unlike content_subject this never
    #: widens when repository sharing is allowed.
    owner_subject: str = ""
    #: The chunk ids *this* view announced at open, and the ones it has since
    #: delivered. Per view rather than per root so that what one caller owes is
    #: never something another caller can add to.
    announced: tuple[str, ...] = ()
    delivered: set[str] = field(default_factory=set)
    #: Rows delivered into this view and not yet proved against the root.
    staged: dict[str, ManifestEntry] = field(default_factory=dict)
    overlay: dict[str, ManifestEntry] = field(default_factory=dict)
    tombstones: set[str] = field(default_factory=set)
    last_client_seq: int = 0
    replays: OrderedDict[int, ViewState] = field(default_factory=OrderedDict)


class InMemoryViewStore:
    """Layer 2 with revisioned overlay, leases and reference accounting."""

    __slots__ = ("_clock", "_lock", "_roots", "_views")

    def __init__(self, *, clock: object = time.time) -> None:
        self._views: dict[str, _View] = {}
        self._roots: dict[tuple[str, str], _ManifestRoot] = {}
        self._lock = threading.RLock()
        self._clock = clock

    def _now(self) -> float:
        clock = self._clock
        assert callable(clock)
        return float(clock())

    # -- lookup ---------------------------------------------------------- #

    def _require(self, org_id: str, view_id: str) -> _View:
        view = self._views.get(view_id)
        if view is None or view.org_id != org_id:
            # Identical refusal either way: "not yours" and "does not exist"
            # must be indistinguishable or the 404 becomes an existence oracle.
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "view is not available",
                details={"view_id": view_id},
                action=AgentAction.REOPEN_VIEW,
            )
        if view.lease_expires_at <= self._now():
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "view lease has expired",
                details={"view_id": view_id},
                action=AgentAction.REOPEN_VIEW,
            )
        return view

    def _base(self, view: _View) -> dict[str, ManifestEntry]:
        """The manifest rows this view reads: proved ones, or its own staged.

        A proved root is shared, which is what makes a warm open cheap. An
        unproved fill is not, which is what stops one caller's rows from
        deciding whether another caller's manifest hashes to its root.
        """
        root = self._roots.get((view.org_id, view.root))
        if root is not None and root.complete:
            return dict(root.entries)
        return dict(view.staged)

    def _effective(self, view: _View) -> dict[str, ManifestEntry]:
        merged = self._base(view)
        for path in view.tombstones:
            merged.pop(path, None)
        merged.update(view.overlay)
        return merged

    def _state(self, view: _View) -> ViewState:
        root = self._roots.get((view.org_id, view.root))
        complete = bool(root is not None and root.complete)
        pending = () if complete else tuple(sorted(set(view.announced) - view.delivered))
        return ViewState(
            view_id=view.view_id,
            org_id=view.org_id,
            repo_id=view.repo_id,
            base_revision=view.base_revision,
            view_revision=view.revision,
            manifest_root=view.root,
            entry_count=len(self._effective(view)),
            lease_expires_at=view.lease_expires_at,
            pending_chunks=pending,
            manifest_complete=complete,
            content_subject=view.content_subject,
            owner_subject=view.owner_subject,
        )

    # -- ViewStore ------------------------------------------------------- #

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
        with self._lock:
            key = (org_id, manifest_root)
            root = self._roots.get(key)
            warm = root is not None and root.complete
            if root is None:
                root = _ManifestRoot(root=manifest_root, org_id=org_id)
                if not chunk_ids:
                    # Nothing announced and nothing known. An empty manifest is
                    # a legitimate view, but only if the root says so.
                    root.complete = manifest_root == canonical_manifest_root(())
                self._roots[key] = root
            root.last_referenced_at = self._now()
            view = _View(
                view_id=new_opaque_id("view"),
                org_id=org_id,
                repo_id=repo_id,
                base_revision=base_revision,
                root=manifest_root,
                revision=0,
                lease_expires_at=self._now() + lease_s,
                content_subject=content_subject,
                owner_subject=owner_subject,
                # This view owes exactly the chunks it just named, and nobody
                # else's announcement changes that. A warm open owes none of
                # them -- the root is already proved -- but they stay announced
                # so that re-delivering one is the no-op it has always been
                # rather than a refusal.
                announced=tuple(dict.fromkeys(chunk_ids)),
            )
            if root.complete:
                view.delivered = set(view.announced)
            self._views[view.view_id] = view
            declared = tuple(dict.fromkeys(entry.content_digest for entry in root.entries.values()))
            return ViewOpenResult(
                state=self._state(view),
                declared_digests=declared,
                have_link_ancestor=False,
                warm=warm,
                needs_manifest=not root.complete and not view.announced,
            )

    def get(self, org_id: str, view_id: str) -> ViewState:
        with self._lock:
            return self._state(self._require(org_id, view_id))

    def put_manifest_chunk(
        self,
        *,
        org_id: str,
        view_id: str,
        chunk_id: str,
        entries: Sequence[ManifestEntry],
    ) -> ViewState:
        with self._lock:
            view = self._require(org_id, view_id)
            root = self._roots.get((org_id, view.root))
            if root is None:
                raise ServerError(
                    ErrorCode.VIEW_UNKNOWN,
                    "view is not available",
                    details={"view_id": view_id},
                    action=AgentAction.REOPEN_VIEW,
                )
            if chunk_id not in view.announced:
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "chunk_id was not announced at views/open",
                    details={"chunk_id": chunk_id},
                    action=AgentAction.REOPEN_VIEW,
                )
            if root.complete:
                # Somebody proved this root while this fill was in flight. The
                # rows are the root's now and they are the rows the root names,
                # so there is nothing left for this chunk to contribute.
                view.delivered.add(chunk_id)
                view.staged = {}
                return self._state(view)
            candidate = dict(view.staged)
            for entry in entries:
                candidate[entry.path] = entry
            delivered = view.delivered | {chunk_id}
            if set(view.announced) <= delivered:
                # The last announced chunk is where the root is proved, and it
                # is proved against what *this* view delivered. Rows shared
                # across views must be the rows the root names, or a warm open
                # silently inherits somebody else's file set.
                assembled = canonical_manifest_root(candidate.values())
                if assembled != view.root:
                    raise ServerError(
                        ErrorCode.MANIFEST_INCOMPLETE,
                        "assembled manifest does not hash to the announced manifest_root",
                        details={
                            "manifest_root": view.root,
                            "assembled_root": assembled,
                            "entry_count": len(candidate),
                        },
                    )
                root.entries = candidate
                root.complete = True
                candidate = {}
            # Committed only once the assembly above has had its say, so a
            # refused completion leaves the view exactly where it was.
            view.staged = candidate
            view.delivered = delivered
            return self._state(view)

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
        with self._lock:
            view = self._require(org_id, view_id)
            replayed = view.replays.get(client_seq)
            if replayed is not None:
                # A retried write after a lost ACK re-reads *its own* committed
                # acknowledgement -- not the view's current state. Between the
                # lost ACK and the retry another writer may have committed, and
                # answering with the current revision would hand this client a
                # revision it never observed; it adopts whatever comes back as
                # its bound revision, so it would then pass the
                # acknowledged-revision check while reading somebody else's
                # overlay. Idempotent, not an error, never a second bump.
                return replayed
            if client_seq <= view.last_client_seq:
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "client_seq must increase monotonically within a view",
                    details={"client_seq": client_seq, "last_client_seq": view.last_client_seq},
                    action=AgentAction.FIX_REQUEST,
                )
            if expected_view_revision != view.revision:
                raise ServerError(
                    ErrorCode.VIEW_REVISION_CONFLICT,
                    "overlay write is based on a revision that is no longer current",
                    details={
                        "expected_view_revision": expected_view_revision,
                        "committed_view_revision": view.revision,
                    },
                    action=AgentAction.REFRESH_VIEW_REVISION,
                )
            for entry in entries:
                view.overlay[entry.path] = entry
                view.tombstones.discard(entry.path)
            for path in removed_paths:
                view.overlay.pop(path, None)
                view.tombstones.add(path)
            view.revision += 1
            view.last_client_seq = client_seq
            state = self._state(view)
            view.replays[client_seq] = state
            while len(view.replays) > _REPLAY_HISTORY:
                view.replays.popitem(last=False)
            return state

    def entry(self, org_id: str, view_id: str, path: str) -> ManifestEntry | None:
        with self._lock:
            view = self._require(org_id, view_id)
            if path in view.overlay:
                return view.overlay[path]
            if path in view.tombstones:
                return None
            return self._base(view).get(path)

    def membership(self, org_id: str, view_id: str) -> Mapping[str, ManifestEntry]:
        with self._lock:
            return self._effective(self._require(org_id, view_id))

    def declared_digests(self, org_id: str, view_id: str) -> tuple[str, ...]:
        """Digests this view's own manifest names, in canonical path order.

        The resume and missing routes answer only about these. A digest the
        caller never declared is not something this view can be asked about,
        which is a strictly stronger property than org scoping alone.
        """
        with self._lock:
            effective = self._effective(self._require(org_id, view_id))
        return tuple(dict.fromkeys(effective[path].content_digest for path in sorted(effective)))

    def renew(self, org_id: str, view_id: str, lease_s: float) -> ViewState:
        with self._lock:
            view = self._require(org_id, view_id)
            view.lease_expires_at = self._now() + lease_s
            return self._state(view)

    def close(self, org_id: str, view_id: str) -> None:
        with self._lock:
            view = self._views.get(view_id)
            if view is None or view.org_id != org_id:
                return
            del self._views[view_id]
            self._touch_orphan_roots(self._now())

    def evict_expired(self, now: float) -> tuple[str, ...]:
        with self._lock:
            expired = tuple(sorted(view_id for view_id, view in self._views.items() if view.lease_expires_at <= now))
            for view_id in expired:
                del self._views[view_id]
            if expired:
                self._touch_orphan_roots(now)
            return expired

    def _touch_orphan_roots(self, now: float) -> None:
        """Start the retention window when the *last* view lets a root go.

        Measuring from the open would reclaim a root the moment a long-lived
        session ended, which is precisely the session whose next start should
        be warm.
        """
        live = {(view.org_id, view.root) for view in self._views.values()}
        for key, root in self._roots.items():
            if key not in live:
                root.last_referenced_at = now

    def sweep_manifest_roots(self, before: float) -> int:
        """Drop roots no live view names and nobody has opened since ``before``.

        Closing a view must *not* take its manifest root with it. The thin
        client's ``SessionStart`` hook opens a view, syncs, and closes it again
        -- so a root that died with its last view would make every session
        cold, re-sending thousands of manifest rows for an unchanged tree and
        putting the plan's warm target out of reach. A root is org-scoped,
        content-addressed and immutable, so it is safe to keep; what bounds it
        is the retention window, exactly like an unreferenced blob.
        """
        with self._lock:
            live = {(view.org_id, view.root) for view in self._views.values()}
            dropped = 0
            for key, root in list(self._roots.items()):
                if key in live or root.last_referenced_at > before:
                    continue
                del self._roots[key]
                dropped += 1
            return dropped

    def referenced_digests(self) -> Mapping[str, frozenset[str]]:
        with self._lock:
            out: dict[str, set[str]] = {}
            for view in self._views.values():
                bucket = out.setdefault(view.org_id, set())
                for entry in self._effective(view).values():
                    bucket.add(entry.content_digest)
            # A retained manifest root references its content just as a live
            # view does. Roots outlive their views (see
            # ``sweep_manifest_roots``), and a warm root whose blobs had been
            # swept underneath it would be warm in name only.
            for root in self._roots.values():
                bucket = out.setdefault(root.org_id, set())
                for entry in root.entries.values():
                    bucket.add(entry.content_digest)
            return {org_id: frozenset(digests) for org_id, digests in out.items()}

    # -- introspection used by tests and the health route ---------------- #

    @property
    def live_views(self) -> int:
        with self._lock:
            return len(self._views)

    @property
    def live_roots(self) -> int:
        with self._lock:
            return len(self._roots)

    def view_count(self, org_id: str) -> int:
        with self._lock:
            return sum(1 for view in self._views.values() if view.org_id == org_id)

    def view_owner(self, org_id: str, view_id: str) -> str | None:
        with self._lock:
            view = self._views.get(view_id)
            if view is None or view.org_id != org_id:
                return None
            return view.owner_subject or None

    def view_ids_for_org(self, org_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(view.view_id for view in self._views.values() if view.org_id == org_id))

    def view_ids_for_user(self, org_id: str, subject: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    view.view_id
                    for view in self._views.values()
                    if view.org_id == org_id and view.owner_subject == subject
                )
            )

    def view_count_for_user(self, org_id: str, subject: str) -> int:
        with self._lock:
            return sum(1 for view in self._views.values() if view.org_id == org_id and view.owner_subject == subject)

    def view_bytes(self, org_id: str) -> int:
        """Approximate per-view Layer-2 cost: overlay and tombstone rows only.

        Base manifest rows are attributed to the shared root, not to the view,
        because that sharing is the storage claim being made.
        """
        with self._lock:
            total = 0
            for view in self._views.values():
                if view.org_id != org_id:
                    continue
                for path, entry in view.overlay.items():
                    total += len(path) + len(entry.content_digest) + len(entry.parser_profile) + 16
                total += sum(len(path) for path in view.tombstones)
            return total

    def root_bytes(self, org_id: str) -> int:
        with self._lock:
            total = 0
            for (owner, _root), root in self._roots.items():
                if owner != org_id:
                    continue
                for path, entry in root.entries.items():
                    total += len(path) + len(entry.content_digest) + len(entry.parser_profile) + 16
            return total


class InMemoryRepoIdentityService:
    """Issues opaque, org-scoped ``repo_id`` values.

    The client's claim selects an existing identity; it never becomes one. Two
    organizations presenting the identical claim receive different ``repo_id``
    values, so a repository identifier cannot be correlated across tenants.
    """

    __slots__ = ("_ids", "_lock")

    def __init__(self) -> None:
        self._ids: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def canonical_repo_id(self, org_id: str, claim: RepoIdentityClaim) -> str:
        key = (org_id, claim.key())
        with self._lock:
            existing = self._ids.get(key)
            if existing is not None:
                return existing
            repo_id = new_opaque_id("repo")
            self._ids[key] = repo_id
            return repo_id


def build_memory_backend(
    *,
    clock: object = time.time,
    content_size_cap: int = DEFAULT_CONTENT_SIZE_CAP,
    inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
) -> IndexBackend:
    """Wire the in-process implementations into an :class:`IndexBackend`."""
    content = InMemoryContentStore()
    analysis_store = InMemoryAnalysisStore()
    views = InMemoryViewStore(clock=clock)
    provenance = InMemoryContentProvenance()
    sources = LayerSources(
        views=views,
        content=content,
        analysis=analysis_store,
        provenance=provenance,
        content_size_cap=content_size_cap,
    )
    return IndexBackend(
        content=content,
        analysis=analysis_store,
        views=views,
        links=LinkLayer(sources, InMemoryLinkGraphStore(), inline_rebuild_budget=inline_rebuild_budget),
        semantic=SemanticLayer(sources, inline_rebuild_budget=inline_rebuild_budget),
        repos=InMemoryRepoIdentityService(),
        provenance=provenance,
    )
