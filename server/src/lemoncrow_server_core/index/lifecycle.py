"""Lifecycle: view eviction, reference-counted blob GC, storage accounting.

The design calls this required, not optional -- "without this the store grows
monotonically with every commit anyone ever checks out" -- so it is code that
runs on a schedule, not a runbook entry.

**Two-phase GC.** A blob that no live view references is *marked*, not deleted.
Only a blob that has been unreferenced for the whole retention window is swept.
The window is what separates a cache from a shredder: a developer whose view
lease expired over a weekend reopens into a warm store instead of re-uploading
their entire worktree. Re-referencing a marked blob clears the mark.

**Reference counting never crosses a tenant.** ``referenced_digests`` is
org-scoped and so is every sweep, which is what makes "delete this customer"
a local operation. The plan is explicit that deleting one tenant can never
depend on another tenant's reference, and that falls out of refusing
cross-org dedup in the first place.

**Eviction is by lease, not by trust.** ``views/close`` is best effort. A view
whose lease expired is evicted here whether or not the client ever said
goodbye, and its derived layers go with it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from .contracts import IndexBackend

__all__ = [
    "DEFAULT_RETENTION_S",
    "Maintenance",
    "MaintenanceReport",
    "StorageReport",
    "ViewTreeStore",
]


class ViewTreeStore(Protocol):
    """Whatever holds per-view trees on disk, seen from the lifecycle.

    Declared as a protocol rather than imported so the index keeps knowing
    nothing about the tool surface: materializing a view is a composition-layer
    concern, but reclaiming one belongs on the same schedule as every other
    reclaim, and a view whose lease expired must not leave a copy of a tenant's
    source behind.
    """

    def forget_view(self, view_id: str) -> bool: ...

    def sweep(self) -> int: ...


#: How long a blob stays after the last view stopped referencing it.
DEFAULT_RETENTION_S: Final[float] = 7 * 24 * 60 * 60.0


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """What one scheduled pass did. Counts only -- no path, no digest."""

    ran_at: float
    views_evicted: int
    blobs_marked: int
    blobs_swept: int
    analysis_rows_dropped: int
    bytes_reclaimed: int
    manifest_roots_swept: int = 0
    view_trees_dropped: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "ran_at": round(self.ran_at, 3),
            "views_evicted": self.views_evicted,
            "blobs_marked": self.blobs_marked,
            "blobs_swept": self.blobs_swept,
            "analysis_rows_dropped": self.analysis_rows_dropped,
            "bytes_reclaimed": self.bytes_reclaimed,
            "manifest_roots_swept": self.manifest_roots_swept,
            "view_trees_dropped": self.view_trees_dropped,
        }


@dataclass(frozen=True, slots=True)
class StorageReport:
    """Where one organization's bytes actually are.

    Split exactly along the claim the design makes: Layer 1 is paid once per
    unique content, the manifest root is paid once per unique file set, and a
    view costs its overlay plus its link rows. Three worktrees of one
    repository should show one Layer 1 and three small views.
    """

    org_id: str
    views: int
    content_bytes: int
    content_blobs: int
    analysis_bytes: int
    analysis_artifacts: int
    manifest_bytes: int
    view_bytes: int

    @property
    def layer1_bytes(self) -> int:
        return self.content_bytes + self.analysis_bytes

    @property
    def per_view_bytes(self) -> int:
        """What each additional worktree costs on top of Layer 1."""
        if self.views <= 0:
            return 0
        return (self.manifest_bytes + self.view_bytes) // self.views

    def to_wire(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "views": self.views,
            "content_bytes": self.content_bytes,
            "content_blobs": self.content_blobs,
            "analysis_bytes": self.analysis_bytes,
            "analysis_artifacts": self.analysis_artifacts,
            "manifest_bytes": self.manifest_bytes,
            "view_bytes": self.view_bytes,
            "layer1_bytes": self.layer1_bytes,
            "per_view_bytes": self.per_view_bytes,
        }


class Maintenance:
    """Server-side scheduled work. Never runs on the client."""

    __slots__ = ("_backend", "_clock", "_last", "_on_view_evicted", "_reference_providers", "_retention_s", "_trees")

    def __init__(
        self,
        backend: IndexBackend,
        *,
        retention_s: float = DEFAULT_RETENTION_S,
        clock: Callable[[], float] = time.time,
        view_trees: ViewTreeStore | None = None,
        reference_providers: Sequence[Callable[[], Mapping[str, frozenset[str]]]] = (),
        on_view_evicted: Callable[[str], None] | None = None,
    ) -> None:
        if retention_s < 0:
            raise ValueError("retention_s must not be negative")
        self._backend = backend
        self._retention_s = retention_s
        self._clock = clock
        self._trees = view_trees
        self._reference_providers = tuple(reference_providers)
        self._on_view_evicted = on_view_evicted
        self._last: MaintenanceReport | None = None

    @property
    def retention_s(self) -> float:
        return self._retention_s

    @property
    def last_report(self) -> MaintenanceReport | None:
        return self._last

    def run_once(self) -> MaintenanceReport:
        now = float(self._clock())

        evicted = self._backend.views.evict_expired(now)
        trees_dropped = 0
        for view_id in evicted:
            self._backend.links.forget_view(view_id)
            self._backend.semantic.forget_view(view_id)
            if self._trees is not None and self._trees.forget_view(view_id):
                trees_dropped += 1
            # The view store owns lease expiry, but composition layers may hold
            # auxiliary per-view state (quota ownership, authorization reach,
            # local-fs grants). Notify them in the same maintenance pass so an
            # expired physical view cannot remain live in accounting forever.
            if self._on_view_evicted is not None:
                self._on_view_evicted(view_id)
        if self._trees is not None:
            # Then the quota, so a process that has been serving many views for
            # a long time reclaims on the same schedule as everything else
            # rather than only when the next materialization happens to notice.
            trees_dropped += self._trees.sweep()

        # Manifest roots outlive their views on purpose -- that is what makes a
        # reopened worktree warm -- so they are reclaimed here, on the same
        # retention window as an unreferenced blob, and only once no live view
        # names them. This runs *before* reference counting so the digests a
        # swept root was keeping alive stop counting in the same pass.
        roots_swept = self._backend.views.sweep_manifest_roots(now - self._retention_s)

        combined: dict[str, set[str]] = {
            org_id: set(view_digests) for org_id, view_digests in self._backend.views.referenced_digests().items()
        }
        for provider in self._reference_providers:
            for org_id, provider_digests in provider().items():
                combined.setdefault(org_id, set()).update(provider_digests)
        referenced: Mapping[str, frozenset[str]] = {
            org_id: frozenset(live_digests) for org_id, live_digests in combined.items()
        }
        marked = self._backend.content.mark_unreferenced(referenced, now)

        swept = self._backend.content.sweep_marked(now - self._retention_s)
        analysis_dropped = 0
        for org_id, swept_digests in swept.dropped.items():
            # Derived rows follow their content out. Nothing keeps an analysis
            # artifact for bytes the tenant no longer has.
            analysis_dropped += self._backend.analysis.sweep(org_id, swept_digests)
            # And so does the record of who demonstrated possession of them. A
            # possession row that outlived its bytes would make the same digest
            # addressable again the moment anybody re-uploaded it, to a scope
            # that had not demonstrated anything this time round.
            self._backend.provenance.forget(org_id, swept_digests)

        report = MaintenanceReport(
            ran_at=now,
            views_evicted=len(evicted),
            blobs_marked=marked,
            blobs_swept=swept.blob_count,
            analysis_rows_dropped=analysis_dropped,
            bytes_reclaimed=swept.total_bytes,
            manifest_roots_swept=roots_swept,
            view_trees_dropped=trees_dropped,
        )
        self._last = report
        return report

    def storage_report(self, org_id: str) -> StorageReport:
        """Per-layer byte accounting for one organization.

        Uses the stores' own accounting rather than a filesystem measurement so
        the answer attributes bytes to the layer that owns them -- which is the
        claim being checked -- instead of to whichever page they landed on.
        """
        views = self._backend.views
        analysis = self._backend.analysis
        return StorageReport(
            org_id=org_id,
            views=_int(views, "view_count", org_id),
            content_bytes=self._backend.content.stored_bytes(org_id),
            content_blobs=self._backend.content.blob_count(org_id),
            analysis_bytes=_int(analysis, "stored_bytes", org_id),
            analysis_artifacts=analysis.count(org_id),
            manifest_bytes=_int(views, "root_bytes", org_id),
            view_bytes=_int(views, "view_bytes", org_id),
        )


def _int(store: object, method: str, org_id: str) -> int:
    """Read one accounting number from a store that offers it.

    Byte accounting is store-specific -- rows in a table, objects on a heap --
    so it is not part of the index contract. A store that does not report a
    number reports zero rather than making the whole report unavailable.
    """
    reader = getattr(store, method, None)
    if reader is None:
        return 0
    return int(reader(org_id))
