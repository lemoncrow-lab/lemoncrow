"""The per-view derived layers, and the one place their build is driven.

:mod:`.links` is the algorithm; this module is what actually runs it against a
view's membership rows and a tenant's Layer-1 content. Both the in-process
store and the SQLite store use the classes here and differ only in where the
graph is persisted, which is why one differential oracle covers both.

Three properties are enforced here rather than in the algorithm:

**Bounded work.** A build that would have to resolve more than
``inline_rebuild_budget`` files does not run inline. It returns
:attr:`~...LinkBuildMode.DEFERRED` and leaves the layer not-ready, which the
degradation contract turns into a flagged answer. Blocking a developer's first
tool call on a cold rebuild of a large repository is the failure mode the
indexed/non-indexed fallback pattern exists to avoid.

**Availability is part of readiness.** A view member whose content the view
cannot address cannot be resolved, so it is not in the graph. The layer says so
-- ``link_content_incomplete`` -- instead of reporting a confidently empty
answer. Files over the content size cap are excluded from that requirement:
they are manifested, not uploaded, by design, so they must never hold a layer
permanently not-ready.

"Cannot address" is :mod:`.boundary`, not ``ContentStore.missing``, and the
difference is load-bearing in both directions. A layer built from whatever the
*organization* happened to hold would resolve import and call edges out of a
colleague's source into a workspace that had only ever named its digest -- and
readiness itself would be an existence oracle, since a caller could declare a
digest, run one query and read whether the answer came back degraded.

**Derivation is lazy and keyed by profile.** Analysis is derived on demand for
``(digest, parser_profile)`` from stored content. Upload-time derivation is a
warm-up, not the contract: the profile a blob was uploaded under and the
profile a manifest row declares for it are allowed to differ.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from ..errors import ServerError
from .analysis import SEMANTIC_EMBEDDING_VERSION, resolve_profile, semantic_units
from .boundary import addressable_in
from .contracts import (
    AnalysisStore,
    ContentProvenance,
    ContentStore,
    Edge,
    EdgeKind,
    IndexLayerState,
    IndexLayerStatus,
    LinkBuildMode,
    LinkBuildReport,
    ManifestEntry,
    SemanticMatch,
    SemanticSearchResult,
    SemanticUnit,
    ViewState,
    ViewStore,
    repository_generation_scope_id,
)
from .links import (
    DEFAULT_FULL_REBUILD_RATIO,
    FileFacts,
    LinkGraph,
    build_graph,
    facts_from,
    update_graph,
)
from .semantic_ann import SemanticAnnIndex, SemanticAnnItem, signatures

__all__ = [
    "DEFAULT_INLINE_REBUILD_BUDGET",
    "InMemoryLinkGraphStore",
    "IndexBuildCancelled",
    "LayerSources",
    "LinkGraphStore",
    "LinkLayer",
    "SemanticLayer",
    "StatusTable",
]

#: Files an inline build may resolve before it defers to scheduled work.
DEFAULT_INLINE_REBUILD_BUDGET: Final[int] = 5_000
_BUILD_LOCK_STRIPES: Final[int] = 64

_COLD_LINK: Final[str] = "link_index_not_built"
_COLD_SEMANTIC: Final[str] = "semantic_index_not_built"


def _acquire_build_lock(lock: threading.Lock, *, wait: bool, cancel: Callable[[], bool] | None) -> bool:
    if not wait:
        return lock.acquire(blocking=False)
    while True:
        if cancel is not None and cancel():
            raise IndexBuildCancelled("deferred index build cancelled")
        if lock.acquire(timeout=0.1):
            return True


class IndexBuildCancelled(RuntimeError):
    """Cooperative stop for background derived-index work."""


class LinkGraphStore(Protocol):
    """Where a view's resolved graph lives between builds."""

    def load(self, org_id: str, view_id: str, base: str = "", base_graph: LinkGraph | None = None) -> LinkGraph | None:
        """The stored graph; *base_graph* is the graph stored under *base*, when held."""

    def save(
        self, org_id: str, view_id: str, graph: LinkGraph, base: str = "", base_graph: LinkGraph | None = None
    ) -> None:
        """Store *graph*; *base* names the generation graph it extends, if any."""

    def edges_for_paths(self, org_id: str, view_id: str, paths: Sequence[str]) -> tuple[Edge, ...]: ...

    def drop(self, org_id: str, view_id: str) -> None: ...

    def drop_view(self, view_id: str) -> None:
        """Drop by view alone, for lease eviction that no longer has the owner."""


class InMemoryLinkGraphStore:
    """Process-local graph storage. Correct, not durable."""

    __slots__ = ("_graphs", "_lock")

    def __init__(self) -> None:
        self._graphs: dict[tuple[str, str], LinkGraph] = {}
        self._lock = threading.Lock()

    def load(self, org_id: str, view_id: str, base: str = "", base_graph: LinkGraph | None = None) -> LinkGraph | None:
        del base, base_graph  # graphs are held whole in memory
        with self._lock:
            return self._graphs.get((org_id, view_id))

    def save(
        self, org_id: str, view_id: str, graph: LinkGraph, base: str = "", base_graph: LinkGraph | None = None
    ) -> None:
        del base, base_graph  # the graph object is shared in memory; nothing to deduplicate
        with self._lock:
            self._graphs[(org_id, view_id)] = graph

    def edges_for_paths(self, org_id: str, view_id: str, paths: Sequence[str]) -> tuple[Edge, ...]:
        wanted = set(paths)
        if not wanted:
            return ()
        with self._lock:
            graph = self._graphs.get((org_id, view_id))
            if graph is None:
                return ()
            return tuple(sorted(edge for edge in graph.edges if edge.src_path in wanted or edge.dst_path in wanted))

    def drop(self, org_id: str, view_id: str) -> None:
        with self._lock:
            self._graphs.pop((org_id, view_id), None)

    def drop_view(self, view_id: str) -> None:
        with self._lock:
            for key in [key for key in self._graphs if key[1] == view_id]:
                del self._graphs[key]


class StatusTable:
    """Readiness for one layer, per view.

    Deliberately process-local even where the graph is durable: after a
    restart, a layer that reports cold and then finds its persisted graph
    already at the committed revision costs one ``UNCHANGED`` build. Persisting
    a *ready* flag that outlived the data it describes costs a wrong answer.
    """

    __slots__ = ("_cold_reason", "_lock", "_reports", "_status")

    def __init__(self, cold_reason: str) -> None:
        self._cold_reason = cold_reason
        self._status: dict[tuple[str, str], IndexLayerStatus] = {}
        self._reports: dict[tuple[str, str], LinkBuildReport] = {}
        self._lock = threading.Lock()

    def cold(self) -> IndexLayerStatus:
        return IndexLayerStatus(
            state=IndexLayerState.COLD,
            reason=self._cold_reason,
            covered_paths=0,
            at_view_revision=0,
        )

    def status(self, org_id: str, view_id: str) -> IndexLayerStatus:
        with self._lock:
            return self._status.get((org_id, view_id), self.cold())

    def set(self, org_id: str, view_id: str, status: IndexLayerStatus) -> IndexLayerStatus:
        with self._lock:
            self._status[(org_id, view_id)] = status
        return status

    def note_revision(self, org_id: str, view_id: str, view_revision: int) -> None:
        with self._lock:
            current = self._status.get((org_id, view_id))
            if current is None or current.at_view_revision >= view_revision:
                return
            self._status[(org_id, view_id)] = IndexLayerStatus(
                state=IndexLayerState.BUILDING,
                reason="view_revision_advanced",
                covered_paths=current.covered_paths,
                at_view_revision=current.at_view_revision,
            )

    def mark_ready(self, org_id: str, view_id: str, *, view_revision: int, covered_paths: int) -> None:
        """Record that a build completed. The write half of this table.

        Called by :meth:`LinkLayer.refresh` after an inline build, and by an
        out-of-process builder that finished a deferred one.
        """
        self.set(
            org_id,
            view_id,
            IndexLayerStatus(
                state=IndexLayerState.READY,
                reason="",
                covered_paths=covered_paths,
                at_view_revision=view_revision,
            ),
        )

    def record_build(self, org_id: str, view_id: str, report: LinkBuildReport) -> None:
        with self._lock:
            self._reports[(org_id, view_id)] = report

    def last_build(self, org_id: str, view_id: str) -> LinkBuildReport | None:
        with self._lock:
            return self._reports.get((org_id, view_id))

    def forget(self, org_id: str, view_id: str) -> None:
        with self._lock:
            self._status.pop((org_id, view_id), None)
            self._reports.pop((org_id, view_id), None)

    def forget_view(self, view_id: str) -> None:
        with self._lock:
            for key in [key for key in self._status if key[1] == view_id]:
                del self._status[key]
            for key in [key for key in self._reports if key[1] == view_id]:
                del self._reports[key]


@dataclass(frozen=True, slots=True)
class LayerSources:
    """What a derived layer reads: Layer 2 membership and Layer 1 content."""

    views: ViewStore
    content: ContentStore
    analysis: AnalysisStore
    provenance: ContentProvenance
    #: Files larger than this are manifested but never uploaded, so they are
    #: excluded from the "is every member available" question.
    content_size_cap: int


@dataclass(frozen=True, slots=True)
class _Members:
    expected: Mapping[str, ManifestEntry]
    available: Mapping[str, ManifestEntry]
    snapshot: Mapping[str, str]
    missing_digests: int
    oversize: int


def _members_from(
    sources: LayerSources,
    state: ViewState,
    membership: Mapping[str, ManifestEntry],
) -> _Members:
    expected: dict[str, ManifestEntry] = {}
    oversize = 0
    for path, entry in membership.items():
        if entry.size > sources.content_size_cap:
            oversize += 1
            continue
        expected[path] = entry
    wanted = {entry.content_digest for entry in expected.values()}
    reachable = addressable_in(sources.provenance, state, tuple(sorted(wanted)))
    available = {path: entry for path, entry in expected.items() if entry.content_digest in reachable}
    return _Members(
        expected=expected,
        available=available,
        snapshot={path: entry.content_digest for path, entry in available.items()},
        missing_digests=len(wanted - reachable),
        oversize=oversize,
    )


def _members(sources: LayerSources, org_id: str, view_id: str) -> _Members:
    state = sources.views.get(org_id, view_id)
    return _members_from(sources, state, sources.views.membership(org_id, view_id))


def _generation_members(sources: LayerSources, org_id: str, view_id: str) -> _Members:
    state = sources.views.get(org_id, view_id)
    return _members_from(sources, state, sources.views.generation_membership(org_id, view_id))


def _generation_graph_key(state: ViewState) -> str:
    if not state.generation_id:
        return state.view_id
    return repository_generation_scope_id(state.generation_id, state.content_subject)


class LinkLayer:
    """Layer 3 for every view, backed by a pluggable graph store."""

    __slots__ = ("_budget", "_build_locks", "_graphs", "_ratio", "_sources", "_status")

    def __init__(
        self,
        sources: LayerSources,
        graphs: LinkGraphStore,
        *,
        inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
        full_rebuild_ratio: float = DEFAULT_FULL_REBUILD_RATIO,
    ) -> None:
        self._sources = sources
        self._graphs = graphs
        self._budget = inline_rebuild_budget
        self._ratio = full_rebuild_ratio
        self._status = StatusTable(_COLD_LINK)
        self._build_locks = tuple(threading.Lock() for _ in range(_BUILD_LOCK_STRIPES))

    # -- IndexLayer ------------------------------------------------------ #

    def status(self, org_id: str, view_id: str) -> IndexLayerStatus:
        current = self._status.status(org_id, view_id)
        revision = self._sources.views.get(org_id, view_id).view_revision
        if current.ready and current.at_view_revision != revision:
            self._status.note_revision(org_id, view_id, revision)
            current = self._status.status(org_id, view_id)
        return current

    def note_revision(self, org_id: str, view_id: str, view_revision: int) -> None:
        self._status.note_revision(org_id, view_id, view_revision)

    def forget(self, org_id: str, view_id: str) -> None:
        self._status.forget(org_id, view_id)
        # View-specific overlay graphs die with the View. Immutable generation
        # graphs deliberately survive and are shared by other Views/worktrees.
        self._graphs.drop(org_id, view_id)

    def forget_view(self, view_id: str) -> None:
        self._status.forget_view(view_id)
        self._graphs.drop_view(view_id)

    def mark_ready(self, org_id: str, view_id: str, *, view_revision: int, covered_paths: int) -> None:
        self._status.mark_ready(org_id, view_id, view_revision=view_revision, covered_paths=covered_paths)

    def last_build(self, org_id: str, view_id: str) -> LinkBuildReport | None:
        return self._status.last_build(org_id, view_id)

    def _read_graph_key(self, org_id: str, view_id: str) -> str:
        overlay, tombstones = self._sources.views.overlay(org_id, view_id)
        if overlay or tombstones:
            return view_id
        return _generation_graph_key(self._sources.views.get(org_id, view_id))

    def edges(self, org_id: str, view_id: str) -> tuple[Edge, ...]:
        graph = self._graphs.load(org_id, self._read_graph_key(org_id, view_id))
        return () if graph is None else tuple(sorted(graph.edges))

    def edges_for_paths(self, org_id: str, view_id: str, paths: Sequence[str]) -> tuple[Edge, ...]:
        return self._graphs.edges_for_paths(org_id, self._read_graph_key(org_id, view_id), paths)

    def export_generation(self, org_id: str, view_id: str) -> Mapping[str, object] | None:
        state = self._sources.views.get(org_id, view_id)
        if not state.generation_id:
            return None
        graph = self._graphs.load(org_id, _generation_graph_key(state))
        if graph is None:
            return None
        return {
            "graph": graph.to_json(),
            "edges": [edge.to_wire() for edge in sorted(graph.edges)],
        }

    def restore_generation(self, org_id: str, view_id: str, payload: Mapping[str, object]) -> bool:
        state = self._sources.views.get(org_id, view_id)
        if not state.generation_id:
            return False
        raw_graph = payload.get("graph")
        raw_edges = payload.get("edges")
        if not isinstance(raw_graph, Mapping) or not isinstance(raw_edges, list):
            return False
        try:
            if not all(isinstance(item, Mapping) for item in raw_edges):
                return False
            edges = frozenset(
                Edge(
                    kind=EdgeKind(str(item["kind"])),
                    src_path=str(item["src_path"]),
                    dst_path=str(item["dst_path"]),
                    symbol=str(item.get("symbol", "")),
                    src_line=int(item.get("src_line", 0)),
                )
                for item in raw_edges
            )
            graph = LinkGraph.from_json(raw_graph, edges)
        except (KeyError, TypeError, ValueError):
            return False
        members = _generation_members(self._sources, org_id, view_id)
        snapshot = dict(members.snapshot)
        if dict(graph.snapshot) != snapshot:
            return False
        paths = set(snapshot)
        if any(edge.src_path not in paths or edge.dst_path not in paths for edge in graph.edges):
            return False
        for table in (graph.path_modules, graph.path_defines, graph.path_refs, graph.path_candidates):
            if set(table) != paths:
                return False
        restored = replace(graph, at_view_revision=0)
        self._graphs.save(org_id, _generation_graph_key(state), restored)
        if not members.missing_digests:
            self._status.record_build(
                org_id,
                view_id,
                LinkBuildReport(
                    mode=LinkBuildMode.UNCHANGED,
                    at_view_revision=state.view_revision,
                    view_paths=len(members.snapshot),
                    changed_paths=0,
                    dirty_paths=0,
                    resolved_paths=0,
                    edges_before=len(restored.edges),
                    edges_after=len(restored.edges),
                    reason="generation_bundle_restore",
                ),
            )
            self._status.mark_ready(
                org_id,
                view_id,
                view_revision=state.view_revision,
                covered_paths=len(members.snapshot),
            )
        return True

    # -- the build ------------------------------------------------------- #

    def _loader(
        self,
        org_id: str,
        members: Mapping[str, ManifestEntry],
        cancel: Callable[[], bool] | None = None,
    ) -> Callable[[str], FileFacts | None]:
        def _load(path: str) -> FileFacts | None:
            if cancel is not None and cancel():
                raise IndexBuildCancelled("deferred index build cancelled")
            entry = members.get(path)
            if entry is None:
                return None
            data = self._sources.content.get(org_id, entry.content_digest)
            if data is None:
                return None
            artifact = self._sources.analysis.ensure(org_id, entry.content_digest, entry.parser_profile, data)
            return facts_from(path, entry.content_digest, artifact)

        return _load

    def _all_facts(
        self,
        org_id: str,
        members: Mapping[str, ManifestEntry],
        cancel: Callable[[], bool] | None = None,
    ) -> dict[str, FileFacts]:
        load = self._loader(org_id, members, cancel)
        facts: dict[str, FileFacts] = {}
        for path in members:
            item = load(path)
            if item is not None:
                facts[path] = item
        return facts

    def _advance_graph(
        self,
        org_id: str,
        previous: LinkGraph | None,
        members: _Members,
        *,
        at_view_revision: int,
        ceiling: int,
        cancel: Callable[[], bool] | None,
        cold_reason: str,
    ) -> tuple[LinkGraph | None, LinkBuildReport]:
        if previous is None:
            if len(members.snapshot) > ceiling:
                return None, LinkBuildReport(
                    mode=LinkBuildMode.DEFERRED,
                    at_view_revision=at_view_revision,
                    view_paths=len(members.snapshot),
                    changed_paths=len(members.snapshot),
                    dirty_paths=0,
                    resolved_paths=0,
                    edges_before=0,
                    edges_after=0,
                    reason=f"inline_rebuild_budget:{ceiling}",
                )
            graph = build_graph(self._all_facts(org_id, members.available, cancel), at_view_revision=at_view_revision)
            return graph, LinkBuildReport(
                mode=LinkBuildMode.FULL,
                at_view_revision=at_view_revision,
                view_paths=len(members.snapshot),
                changed_paths=len(members.snapshot),
                dirty_paths=len(members.snapshot),
                resolved_paths=len(members.snapshot),
                edges_before=0,
                edges_after=len(graph.edges),
                reason=cold_reason,
            )

        graph, report = update_graph(
            previous,
            snapshot=members.snapshot,
            load_facts=self._loader(org_id, members.available, cancel),
            at_view_revision=at_view_revision,
            full_rebuild_ratio=self._ratio,
        )
        if graph is not None:
            return graph, report
        if len(members.snapshot) > ceiling:
            return None, LinkBuildReport(
                mode=LinkBuildMode.DEFERRED,
                at_view_revision=at_view_revision,
                view_paths=len(members.snapshot),
                changed_paths=report.changed_paths,
                dirty_paths=0,
                resolved_paths=0,
                edges_before=len(previous.edges),
                edges_after=len(previous.edges),
                reason=f"inline_rebuild_budget:{ceiling}",
            )
        graph = build_graph(self._all_facts(org_id, members.available, cancel), at_view_revision=at_view_revision)
        return graph, replace(report, edges_after=len(graph.edges))

    def refresh(
        self,
        org_id: str,
        view_id: str,
        *,
        budget: int | None = None,
        wait: bool = False,
        cancel: Callable[[], bool] | None = None,
    ) -> IndexLayerStatus:
        """Bring Layer 3 to the committed revision without blocking foreground callers.

        The immutable base graph is keyed by RepositoryGeneration *and* the
        View's possession scope. Clean Views therefore converge on one build;
        dirty Views incrementally derive their private overlay graph from that
        shared base. Deferred workers pass ``wait=True``. Foreground queries use
        the default and remain explicitly degraded while the shared build runs.
        """
        state = self._sources.views.get(org_id, view_id)
        revision = state.view_revision
        current = self._status.status(org_id, view_id)
        if current.ready and current.at_view_revision == revision:
            return current
        build_key = _generation_graph_key(state)
        lock = self._build_locks[hash((org_id, build_key)) % len(self._build_locks)]
        if not _acquire_build_lock(lock, wait=wait, cancel=cancel):
            return self._status.set(
                org_id,
                view_id,
                IndexLayerStatus(
                    state=IndexLayerState.BUILDING,
                    reason="link_rebuild_in_progress",
                    covered_paths=current.covered_paths,
                    at_view_revision=current.at_view_revision,
                ),
            )
        self._status.set(
            org_id,
            view_id,
            IndexLayerStatus(
                state=IndexLayerState.BUILDING,
                reason="link_rebuild_in_progress",
                covered_paths=current.covered_paths,
                at_view_revision=current.at_view_revision,
            ),
        )
        try:
            return self._refresh_locked(org_id, view_id, revision, budget=budget, cancel=cancel)
        finally:
            lock.release()

    def _refresh_locked(
        self,
        org_id: str,
        view_id: str,
        revision: int,
        *,
        budget: int | None,
        cancel: Callable[[], bool] | None,
    ) -> IndexLayerStatus:
        state = self._sources.views.get(org_id, view_id)
        ceiling = self._budget if budget is None else budget
        overlay, tombstones = self._sources.views.overlay(org_id, view_id)
        generation_key = _generation_graph_key(state)
        generation_members = _generation_members(self._sources, org_id, view_id)
        generation_previous = self._graphs.load(org_id, generation_key)
        generation_graph, generation_report = self._advance_graph(
            org_id,
            generation_previous,
            generation_members,
            at_view_revision=0,
            ceiling=ceiling,
            cancel=cancel,
            cold_reason="generation_cold_start",
        )
        if generation_graph is None:
            return self._defer(org_id, view_id, generation_members, revision, ceiling)

        # A generation is immutable; publishing its scoped base graph remains
        # valid even if this particular View advances while the build runs.
        # Unchanged, it is already stored: saving it again rewrote every edge.
        if generation_previous is None or generation_report.mode is not LinkBuildMode.UNCHANGED:
            self._graphs.save(org_id, generation_key, generation_graph)

        if overlay or tombstones:
            members = _members(self._sources, org_id, view_id)
            previous = (
                self._graphs.load(org_id, view_id, base=generation_key, base_graph=generation_graph) or generation_graph
            )
            graph, report = self._advance_graph(
                org_id,
                previous,
                members,
                at_view_revision=revision,
                ceiling=ceiling,
                cancel=cancel,
                cold_reason="overlay_cold_start",
            )
            if graph is None:
                return self._defer(org_id, view_id, members, revision, ceiling)
            target_key = view_id
        else:
            members = generation_members
            graph = generation_graph
            target_key = generation_key
            report = replace(
                generation_report,
                at_view_revision=revision,
                reason=(
                    "generation_reuse"
                    if generation_previous is not None and generation_report.mode is LinkBuildMode.UNCHANGED
                    else generation_report.reason
                ),
            )

        # Only the View-specific publication is revision-sensitive. The base
        # generation graph above is reusable regardless of overlay churn.
        latest = self._sources.views.get(org_id, view_id).view_revision
        if latest != revision:
            current = self._status.status(org_id, view_id)
            return self._status.set(
                org_id,
                view_id,
                IndexLayerStatus(
                    state=IndexLayerState.BUILDING,
                    reason="view_revision_advanced",
                    covered_paths=current.covered_paths,
                    at_view_revision=current.at_view_revision,
                ),
            )
        if target_key == view_id:
            self._graphs.save(org_id, view_id, graph, base=generation_key, base_graph=generation_graph)
            if self._sources.views.get(org_id, view_id).view_revision != revision:
                self._graphs.drop(org_id, view_id)
                current = self._status.status(org_id, view_id)
                return self._status.set(
                    org_id,
                    view_id,
                    IndexLayerStatus(
                        state=IndexLayerState.BUILDING,
                        reason="view_revision_advanced",
                        covered_paths=current.covered_paths,
                        at_view_revision=current.at_view_revision,
                    ),
                )
        self._status.record_build(org_id, view_id, report)
        if members.missing_digests:
            return self._status.set(
                org_id,
                view_id,
                IndexLayerStatus(
                    state=IndexLayerState.BUILDING,
                    reason="link_content_incomplete",
                    covered_paths=len(members.snapshot),
                    at_view_revision=revision,
                ),
            )
        self._status.mark_ready(org_id, view_id, view_revision=revision, covered_paths=len(members.snapshot))
        return self._status.status(org_id, view_id)

    def _defer(
        self,
        org_id: str,
        view_id: str,
        members: _Members,
        revision: int,
        ceiling: int,
    ) -> IndexLayerStatus:
        self._status.record_build(
            org_id,
            view_id,
            LinkBuildReport(
                mode=LinkBuildMode.DEFERRED,
                at_view_revision=revision,
                view_paths=len(members.snapshot),
                changed_paths=len(members.snapshot),
                dirty_paths=0,
                resolved_paths=0,
                edges_before=0,
                edges_after=0,
                reason=f"inline_rebuild_budget:{ceiling}",
            ),
        )
        return self._status.set(
            org_id,
            view_id,
            IndexLayerStatus(
                state=IndexLayerState.BUILDING,
                reason="link_rebuild_deferred",
                covered_paths=0,
                at_view_revision=0,
            ),
        )


class SemanticLayer:
    """Generation-scoped ANN plus a small revisioned View-overlay ANN."""

    __slots__ = (
        "_budget",
        "_build_locks",
        "_generation_indexes",
        "_index_lock",
        "_overlay_indexes",
        "_signature_cache",
        "_sources",
        "_status",
        "_unit_cache",
    )

    def __init__(
        self,
        sources: LayerSources,
        *,
        inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
    ) -> None:
        self._sources = sources
        self._budget = inline_rebuild_budget
        self._status = StatusTable(_COLD_SEMANTIC)
        self._build_locks = tuple(threading.Lock() for _ in range(_BUILD_LOCK_STRIPES))
        self._generation_indexes: dict[tuple[str, str], SemanticAnnIndex] = {}
        self._overlay_indexes: dict[tuple[str, str], tuple[int, SemanticAnnIndex, frozenset[str]]] = {}
        self._index_lock = threading.Lock()
        # Consecutive generations share almost every file. Per file version, its
        # units and each embedding's LSH signatures are built once and shared
        # by every index holding them, instead of re-read, re-hashed in pure
        # Python and held again by each generation. Pruned to what retained
        # indexes reference whenever a generation index is retired.
        self._unit_cache: dict[tuple[str, str, str], tuple[SemanticUnit, ...]] = {}
        self._signature_cache: dict[Sequence[float], tuple[int, ...]] = {}

    def status(self, org_id: str, view_id: str) -> IndexLayerStatus:
        current = self._status.status(org_id, view_id)
        revision = self._sources.views.get(org_id, view_id).view_revision
        if current.ready and current.at_view_revision != revision:
            self._status.note_revision(org_id, view_id, revision)
            current = self._status.status(org_id, view_id)
        return current

    def note_revision(self, org_id: str, view_id: str, view_revision: int) -> None:
        self._status.note_revision(org_id, view_id, view_revision)

    def forget(self, org_id: str, view_id: str) -> None:
        self._status.forget(org_id, view_id)
        with self._index_lock:
            self._overlay_indexes.pop((org_id, view_id), None)

    def forget_view(self, view_id: str) -> None:
        self._status.forget_view(view_id)
        with self._index_lock:
            for key in [key for key in self._overlay_indexes if key[1] == view_id]:
                del self._overlay_indexes[key]

    def mark_ready(self, org_id: str, view_id: str, *, view_revision: int, covered_paths: int) -> None:
        self._status.mark_ready(org_id, view_id, view_revision=view_revision, covered_paths=covered_paths)

    @staticmethod
    def _fallback_unit(artifact: object) -> tuple[SemanticUnit, ...]:
        embedding = getattr(artifact, "embedding", ())
        if not embedding or not any(embedding):
            return ()
        return (
            SemanticUnit(
                unit_id="file:1",
                start_line=1,
                end_line=max(1, int(getattr(artifact, "line_count", 1))),
                symbol="",
                embedding_version=SEMANTIC_EMBEDDING_VERSION,
                embedding=tuple(float(value) for value in embedding),
            ),
        )

    def _retire_idle_generation_indexes(self, org_id: str) -> None:
        """Release the ANN of every generation no live View of *org_id* is on.

        Each new generation of a repository -- every commit its Views move
        onto -- builds a fresh in-memory index of all its embeddings, and none
        was ever released: the server grew by hundreds of MB per generation.
        """
        live: set[str] = set()
        for view_id in self._sources.views.view_ids_for_org(org_id):
            try:
                live.add(_generation_graph_key(self._sources.views.get(org_id, view_id)))
            except ServerError:
                continue  # closed while we looked
        with self._index_lock:
            for key in [key for key in self._generation_indexes if key[0] == org_id and key[1] not in live]:
                del self._generation_indexes[key]
            retained = [*self._generation_indexes.values(), *(row[1] for row in self._overlay_indexes.values())]
            live_digests = {item.digest for index in retained for item in index.items}
            for unit_key in [unit_key for unit_key in self._unit_cache if unit_key[1] not in live_digests]:
                del self._unit_cache[unit_key]
            live_vectors = {unit.embedding for units in self._unit_cache.values() for unit in units}
            for vector in [vector for vector in self._signature_cache if vector not in live_vectors]:
                del self._signature_cache[vector]

    def _signatures(self, vector: Sequence[float]) -> tuple[int, ...]:
        with self._index_lock:
            cached = self._signature_cache.get(vector)
        if cached is None:
            cached = signatures(vector)
            with self._index_lock:
                self._signature_cache[vector] = cached
        return cached

    def _file_units(self, org_id: str, entry: ManifestEntry) -> tuple[SemanticUnit, ...] | None:
        """One file version's semantic units, or ``None`` when its content is absent."""
        artifact = self._sources.analysis.get(org_id, entry.content_digest, entry.parser_profile)
        data: bytes | None = None
        if artifact is None:
            data = self._sources.content.get(org_id, entry.content_digest)
            if data is None:
                return None
            artifact = self._sources.analysis.ensure(org_id, entry.content_digest, entry.parser_profile, data)
        units = tuple(unit for unit in artifact.semantic_units if unit.embedding_version == SEMANTIC_EMBEDDING_VERSION)
        if not units and resolve_profile(entry.parser_profile).semantic_units:
            if data is None:
                data = self._sources.content.get(org_id, entry.content_digest)
            if data is not None:
                units = semantic_units(data.decode("utf-8", errors="replace"), artifact.definitions)
        return units or self._fallback_unit(artifact)

    def _build_index(
        self,
        org_id: str,
        members: _Members,
        cancel: Callable[[], bool] | None,
    ) -> SemanticAnnIndex:
        items: list[SemanticAnnItem] = []
        for path, entry in sorted(members.available.items()):
            if cancel is not None and cancel():
                raise IndexBuildCancelled("deferred index build cancelled")
            unit_key = (org_id, entry.content_digest, entry.parser_profile)
            with self._index_lock:
                units = self._unit_cache.get(unit_key)
            if units is None:
                units = self._file_units(org_id, entry)
                if units is None:
                    continue
                with self._index_lock:
                    units = self._unit_cache.setdefault(unit_key, units)
            items.extend(SemanticAnnItem(path=path, digest=entry.content_digest, unit=unit) for unit in units)
        return SemanticAnnIndex.build(items, signatures_of=self._signatures)

    def refresh(
        self,
        org_id: str,
        view_id: str,
        *,
        budget: int | None = None,
        wait: bool = False,
        cancel: Callable[[], bool] | None = None,
    ) -> IndexLayerStatus:
        state = self._sources.views.get(org_id, view_id)
        current = self._status.status(org_id, view_id)
        if current.ready and current.at_view_revision == state.view_revision:
            return current
        build_key = _generation_graph_key(state)
        lock = self._build_locks[hash((org_id, build_key)) % len(self._build_locks)]
        if not _acquire_build_lock(lock, wait=wait, cancel=cancel):
            return self._status.set(
                org_id,
                view_id,
                IndexLayerStatus(
                    state=IndexLayerState.BUILDING,
                    reason="semantic_rebuild_in_progress",
                    covered_paths=current.covered_paths,
                    at_view_revision=current.at_view_revision,
                ),
            )
        self._status.set(
            org_id,
            view_id,
            IndexLayerStatus(
                state=IndexLayerState.BUILDING,
                reason="semantic_rebuild_in_progress",
                covered_paths=current.covered_paths,
                at_view_revision=current.at_view_revision,
            ),
        )
        try:
            return self._refresh_locked(org_id, view_id, state.view_revision, budget=budget, cancel=cancel)
        finally:
            lock.release()

    def _deferred(self, org_id: str, view_id: str) -> IndexLayerStatus:
        return self._status.set(
            org_id,
            view_id,
            IndexLayerStatus(
                state=IndexLayerState.BUILDING,
                reason="semantic_rebuild_deferred",
                covered_paths=0,
                at_view_revision=0,
            ),
        )

    def _incomplete(self, org_id: str, view_id: str, revision: int, covered: int) -> IndexLayerStatus:
        return self._status.set(
            org_id,
            view_id,
            IndexLayerStatus(
                state=IndexLayerState.BUILDING,
                reason="semantic_content_incomplete",
                covered_paths=covered,
                at_view_revision=revision,
            ),
        )

    def _refresh_locked(
        self,
        org_id: str,
        view_id: str,
        revision: int,
        *,
        budget: int | None,
        cancel: Callable[[], bool] | None,
    ) -> IndexLayerStatus:
        state = self._sources.views.get(org_id, view_id)
        ceiling = self._budget if budget is None else budget
        generation_key = _generation_graph_key(state)
        generation_members = _generation_members(self._sources, org_id, view_id)
        with self._index_lock:
            generation_index = self._generation_indexes.get((org_id, generation_key))
        if generation_index is None:
            if len(generation_members.snapshot) > ceiling:
                return self._deferred(org_id, view_id)
            candidate = self._build_index(org_id, generation_members, cancel)
            if generation_members.missing_digests:
                return self._incomplete(org_id, view_id, revision, len(generation_members.snapshot))
            with self._index_lock:
                generation_index = self._generation_indexes.setdefault((org_id, generation_key), candidate)
            self._retire_idle_generation_indexes(org_id)

        overlay, tombstones = self._sources.views.overlay(org_id, view_id)
        if overlay or tombstones:
            overlay_members = _members_from(self._sources, state, overlay)
            if len(overlay_members.snapshot) > ceiling:
                return self._deferred(org_id, view_id)
            overlay_index = self._build_index(org_id, overlay_members, cancel)
            if overlay_members.missing_digests:
                return self._incomplete(org_id, view_id, revision, len(generation_members.snapshot))
            latest = self._sources.views.get(org_id, view_id).view_revision
            if latest != revision:
                current = self._status.status(org_id, view_id)
                return self._status.set(
                    org_id,
                    view_id,
                    IndexLayerStatus(
                        state=IndexLayerState.BUILDING,
                        reason="view_revision_advanced",
                        covered_paths=current.covered_paths,
                        at_view_revision=current.at_view_revision,
                    ),
                )
            excluded = frozenset(set(overlay) | set(tombstones))
            with self._index_lock:
                self._overlay_indexes[(org_id, view_id)] = (revision, overlay_index, excluded)
        else:
            with self._index_lock:
                self._overlay_indexes.pop((org_id, view_id), None)

        latest_state = self._sources.views.get(org_id, view_id)
        if latest_state.view_revision != revision:
            current = self._status.status(org_id, view_id)
            return self._status.set(
                org_id,
                view_id,
                IndexLayerStatus(
                    state=IndexLayerState.BUILDING,
                    reason="view_revision_advanced",
                    covered_paths=current.covered_paths,
                    at_view_revision=current.at_view_revision,
                ),
            )
        self._status.mark_ready(
            org_id,
            view_id,
            view_revision=revision,
            covered_paths=latest_state.entry_count,
        )
        return self._status.status(org_id, view_id)

    def nearest(
        self,
        org_id: str,
        view_id: str,
        vector: Sequence[float],
        limit: int,
    ) -> SemanticSearchResult:
        state = self._sources.views.get(org_id, view_id)
        generation_key = _generation_graph_key(state)
        overlay, tombstones = self._sources.views.overlay(org_id, view_id)
        excluded = frozenset(set(overlay) | set(tombstones))
        with self._index_lock:
            base = self._generation_indexes.get((org_id, generation_key))
            overlay_row = self._overlay_indexes.get((org_id, view_id))
        if base is None:
            return SemanticSearchResult((), 0, 0)
        search_limit = max(limit, limit * 4)
        base_result = base.query(vector, search_limit, excluded_paths=excluded)
        results = list(base_result.matches)
        candidates = base_result.candidates_scored
        total_units = base_result.total_units
        if overlay_row is not None and overlay_row[0] == state.view_revision:
            overlay_result = overlay_row[1].query(vector, search_limit)
            results.extend(overlay_result.matches)
            candidates += overlay_result.candidates_scored
            total_units += overlay_result.total_units
        best: dict[str, SemanticMatch] = {}
        for match in results:
            current = best.get(match.path)
            if current is None or match.score > current.score:
                best[match.path] = match
        ordered = sorted(best.values(), key=lambda match: (-match.score, match.path))
        return SemanticSearchResult(tuple(ordered[:limit]), candidates, total_units)

    def export_generation(self, org_id: str, view_id: str) -> Mapping[str, object] | None:
        state = self._sources.views.get(org_id, view_id)
        key = (org_id, _generation_graph_key(state))
        with self._index_lock:
            index = self._generation_indexes.get(key)
        return None if index is None else {"ann": index.to_json()}

    def restore_generation(self, org_id: str, view_id: str, payload: Mapping[str, object]) -> bool:
        state = self._sources.views.get(org_id, view_id)
        raw = payload.get("ann")
        if not isinstance(raw, Mapping):
            return False
        try:
            index = SemanticAnnIndex.from_json(raw)
        except (KeyError, TypeError, ValueError):
            return False
        members = _generation_members(self._sources, org_id, view_id)
        snapshot = dict(members.snapshot)
        expected: set[tuple[str, str]] = set()
        for path, entry in members.available.items():
            artifact = self._sources.analysis.get(org_id, entry.content_digest, entry.parser_profile)
            if artifact is None:
                return False
            usable_units = tuple(
                unit
                for unit in artifact.semantic_units
                if unit.embedding_version == SEMANTIC_EMBEDDING_VERSION and any(unit.embedding)
            )
            if usable_units or (artifact.embedding and any(artifact.embedding)):
                expected.add((path, entry.content_digest))
        if index.path_digests != frozenset(expected):
            return False
        if any(snapshot.get(path) != digest for path, digest in index.path_digests):
            return False
        with self._index_lock:
            self._generation_indexes[(org_id, _generation_graph_key(state))] = index
        if not members.missing_digests:
            self._status.mark_ready(
                org_id,
                view_id,
                view_revision=state.view_revision,
                covered_paths=state.entry_count,
            )
        return True
