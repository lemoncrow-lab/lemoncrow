"""Lightweight metrics for the reasoning runtime.

Intentionally minimal: we count things, not collect telemetry. Useful
for the `atelier list-playbooks` summary and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from atelier.core.foundation.store import ContextStore


@dataclass
class StoreSummary:
    blocks_total: int
    blocks_active: int
    blocks_deprecated: int
    blocks_quarantined: int
    traces_total: int
    rubrics_total: int


def summarize(store: ContextStore, since: datetime | None = None) -> StoreSummary:
    all_blocks = store.list_blocks(include_deprecated=True)
    active = [b for b in all_blocks if b.status == "active"]
    deprecated = [b for b in all_blocks if b.status == "deprecated"]
    quarantined = [b for b in all_blocks if b.status == "quarantined"]
    return StoreSummary(
        blocks_total=len(all_blocks),
        blocks_active=len(active),
        blocks_deprecated=len(deprecated),
        blocks_quarantined=len(quarantined),
        traces_total=len(store.list_traces(limit=10_000, since=since)),
        rubrics_total=len(store.list_rubrics()),
    )
