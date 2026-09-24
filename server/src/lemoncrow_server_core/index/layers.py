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

from .boundary import addressable_in
from .contracts import (
    AnalysisStore,
    ContentProvenance,
    ContentStore,
    Edge,
    IndexLayerState,
    IndexLayerStatus,
    LinkBuildMode,
    LinkBuildReport,
    ManifestEntry,
    ViewStore,
)
from .links import (
    DEFAULT_FULL_REBUILD_RATIO,
    FileFacts,
    LinkGraph,
    build_graph,
    facts_from,
    update_graph,
)

__all__ = [
    "DEFAULT_INLINE_REBUILD_BUDGET",
    "InMemoryLinkGraphStore",
    "LayerSources",
    "LinkGraphStore",
    "LinkLayer",
    "SemanticLayer",
    "StatusTable",
]

#: Files an inline build may resolve before it defers to scheduled work.
DEFAULT_INLINE_REBUILD_BUDGET: Final[int] = 5_000

_COLD_LINK: Final[str] = "link_index_not_built"
_COLD_SEMANTIC: Final[str] = "semantic_index_not_built"


class LinkGraphStore(Protocol):
    """Where a view's resolved graph lives between builds."""

    def load(self, org_id: str, view_id: str) -> LinkGraph | None: ...

    def save(self, org_id: str, view_id: str, graph: LinkGraph) -> None: ...

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

    def load(self, org_id: str, view_id: str) -> LinkGraph | None:
        with self._lock:
            return self._graphs.get((org_id, view_id))

    def save(self, org_id: str, view_id: str, graph: LinkGraph) -> None:
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


def _members(sources: LayerSources, org_id: str, view_id: str) -> _Members:
    state = sources.views.get(org_id, view_id)
    membership = sources.views.membership(org_id, view_id)
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


class LinkLayer:
    """Layer 3 for every view, backed by a pluggable graph store."""

    __slots__ = ("_budget", "_graphs", "_ratio", "_sources", "_status")

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

    # -- IndexLayer ------------------------------------------------------ #

    def status(self, org_id: str, view_id: str) -> IndexLayerStatus:
        return self._status.status(org_id, view_id)

    def note_revision(self, org_id: str, view_id: str, view_revision: int) -> None:
        self._status.note_revision(org_id, view_id, view_revision)

    def forget(self, org_id: str, view_id: str) -> None:
        self._status.forget(org_id, view_id)
        self._graphs.drop(org_id, view_id)

    def forget_view(self, view_id: str) -> None:
        self._status.forget_view(view_id)
        self._graphs.drop_view(view_id)

    def mark_ready(self, org_id: str, view_id: str, *, view_revision: int, covered_paths: int) -> None:
        self._status.mark_ready(org_id, view_id, view_revision=view_revision, covered_paths=covered_paths)

    def last_build(self, org_id: str, view_id: str) -> LinkBuildReport | None:
        return self._status.last_build(org_id, view_id)

    def edges(self, org_id: str, view_id: str) -> tuple[Edge, ...]:
        graph = self._graphs.load(org_id, view_id)
        return () if graph is None else tuple(sorted(graph.edges))

    def edges_for_paths(self, org_id: str, view_id: str, paths: Sequence[str]) -> tuple[Edge, ...]:
        return self._graphs.edges_for_paths(org_id, view_id, paths)

    # -- the build ------------------------------------------------------- #

    def _loader(self, org_id: str, members: Mapping[str, ManifestEntry]) -> Callable[[str], FileFacts | None]:
        def _load(path: str) -> FileFacts | None:
            entry = members.get(path)
            if entry is None:
                return None
            data = self._sources.content.get(org_id, entry.content_digest)
            if data is None:
                return None
            artifact = self._sources.analysis.ensure(org_id, entry.content_digest, entry.parser_profile, data)
            return facts_from(path, entry.content_digest, artifact)

        return _load

    def _all_facts(self, org_id: str, members: Mapping[str, ManifestEntry]) -> dict[str, FileFacts]:
        load = self._loader(org_id, members)
        facts: dict[str, FileFacts] = {}
        for path in members:
            item = load(path)
            if item is not None:
                facts[path] = item
        return facts

    def refresh(self, org_id: str, view_id: str, *, budget: int | None = None) -> IndexLayerStatus:
        """Bring Layer 3 to the view's committed revision, within budget."""
        state = self._sources.views.get(org_id, view_id)
        revision = state.view_revision
        current = self._status.status(org_id, view_id)
        if current.ready and current.at_view_revision == revision:
            return current

        members = _members(self._sources, org_id, view_id)
        ceiling = self._budget if budget is None else budget
        previous = self._graphs.load(org_id, view_id)

        graph: LinkGraph | None = None
        report: LinkBuildReport
        if previous is None:
            if len(members.snapshot) > ceiling:
                return self._defer(org_id, view_id, members, revision, ceiling)
            graph = build_graph(self._all_facts(org_id, members.available), at_view_revision=revision)
            report = LinkBuildReport(
                mode=LinkBuildMode.FULL,
                at_view_revision=revision,
                view_paths=len(members.snapshot),
                changed_paths=len(members.snapshot),
                dirty_paths=len(members.snapshot),
                resolved_paths=len(members.snapshot),
                edges_before=0,
                edges_after=len(graph.edges),
                reason="cold_start",
            )
        else:
            graph, report = update_graph(
                previous,
                snapshot=members.snapshot,
                load_facts=self._loader(org_id, members.available),
                at_view_revision=revision,
                full_rebuild_ratio=self._ratio,
            )
            if graph is None:
                if len(members.snapshot) > ceiling:
                    return self._defer(org_id, view_id, members, revision, ceiling)
                graph = build_graph(self._all_facts(org_id, members.available), at_view_revision=revision)
                report = replace(report, edges_after=len(graph.edges))

        self._graphs.save(org_id, view_id, graph)
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
    """Embedding search readiness for one view.

    There is no per-view artifact to build: the vectors are Layer-1 rows and the
    view contributes only membership. "Building" therefore means deriving the
    analysis for members that have content but no derived rows yet, and
    readiness means every member that *should* have content does.
    """

    __slots__ = ("_budget", "_sources", "_status")

    def __init__(
        self,
        sources: LayerSources,
        *,
        inline_rebuild_budget: int = DEFAULT_INLINE_REBUILD_BUDGET,
    ) -> None:
        self._sources = sources
        self._budget = inline_rebuild_budget
        self._status = StatusTable(_COLD_SEMANTIC)

    def status(self, org_id: str, view_id: str) -> IndexLayerStatus:
        return self._status.status(org_id, view_id)

    def note_revision(self, org_id: str, view_id: str, view_revision: int) -> None:
        self._status.note_revision(org_id, view_id, view_revision)

    def forget(self, org_id: str, view_id: str) -> None:
        self._status.forget(org_id, view_id)

    def forget_view(self, view_id: str) -> None:
        self._status.forget_view(view_id)

    def mark_ready(self, org_id: str, view_id: str, *, view_revision: int, covered_paths: int) -> None:
        self._status.mark_ready(org_id, view_id, view_revision=view_revision, covered_paths=covered_paths)

    def refresh(self, org_id: str, view_id: str, *, budget: int | None = None) -> IndexLayerStatus:
        state = self._sources.views.get(org_id, view_id)
        members = _members(self._sources, org_id, view_id)
        ceiling = self._budget if budget is None else budget
        if len(members.snapshot) > ceiling:
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
        for entry in members.available.values():
            data = self._sources.content.get(org_id, entry.content_digest)
            if data is None:
                continue
            self._sources.analysis.ensure(org_id, entry.content_digest, entry.parser_profile, data)
        if members.missing_digests:
            return self._status.set(
                org_id,
                view_id,
                IndexLayerStatus(
                    state=IndexLayerState.BUILDING,
                    reason="semantic_content_incomplete",
                    covered_paths=len(members.snapshot),
                    at_view_revision=state.view_revision,
                ),
            )
        self._status.mark_ready(org_id, view_id, view_revision=state.view_revision, covered_paths=len(members.snapshot))
        return self._status.status(org_id, view_id)
