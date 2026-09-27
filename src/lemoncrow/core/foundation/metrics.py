"""Lightweight metrics for the reasoning runtime.

Intentionally minimal: we count things, not collect telemetry. Useful
for the `lc list-playbooks` summary and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lemoncrow.infra.storage.bundle import StoreBundle


@dataclass
class StoreSummary:
    blocks_total: int
    blocks_active: int
    blocks_deprecated: int
    blocks_quarantined: int
    traces_total: int
    rubrics_total: int


def summarize(store: StoreBundle, since: datetime | None = None) -> StoreSummary:
    all_blocks = store.knowledge.list_blocks(include_deprecated=True)
    active = [b for b in all_blocks if b.status == "active"]
    deprecated = [b for b in all_blocks if b.status == "deprecated"]
    quarantined = [b for b in all_blocks if b.status == "quarantined"]
    return StoreSummary(
        blocks_total=len(all_blocks),
        blocks_active=len(active),
        blocks_deprecated=len(deprecated),
        blocks_quarantined=len(quarantined),
        traces_total=len(store.history.list_traces(limit=10_000, since=since)),
        rubrics_total=len(store.knowledge.list_rubrics()),
    )
