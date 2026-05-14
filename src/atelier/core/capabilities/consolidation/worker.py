"""Manual sleep-time consolidation worker."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from atelier.core.foundation.models import ConsolidationCandidate, ReasonBlock
from atelier.core.foundation.store import ContextStore
from atelier.infra.internal_llm.ollama_client import OllamaUnavailable, chat


@dataclass(frozen=True)
class ConsolidationReport:
    duplicates: int
    stale: int
    ollama_suggestions: int
    written: int

    def to_dict(self) -> dict[str, int]:
        return {
            "duplicates": self.duplicates,
            "stale": self.stale,
            "ollama_suggestions": self.ollama_suggestions,
            "written": self.written,
        }


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[a-zA-Z0-9_]+", text)}


def _similarity(a: ReasonBlock, b: ReasonBlock) -> float:
    left = _tokens(" ".join([a.title, a.situation, *a.procedure, *a.failure_signals]))
    right = _tokens(" ".join([b.title, b.situation, *b.procedure, *b.failure_signals]))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _draft_merge(blocks: list[ReasonBlock]) -> tuple[str | None, bool]:
    payload = [block.model_dump(mode="json") for block in blocks]
    try:
        response = chat(
            [
                {
                    "role": "system",
                    "content": "Return JSON with duplicate:boolean and proposed_body:string.",
                },
                {"role": "user", "content": json.dumps(payload, sort_keys=True)},
            ],
            json_schema={"type": "object"},
        )
    except OllamaUnavailable:
        return None, False
    if isinstance(response, dict) and response.get("duplicate") is not False:
        return str(response.get("proposed_body", "") or "").strip() or None, True
    return None, True


def consolidate(
    store: ContextStore,
    *,
    since: timedelta = timedelta(days=7),
    dry_run: bool = False,
) -> ConsolidationReport:
    """Find duplicate/stale knowledge rows and write human-reviewed candidates."""
    _ = since
    blocks = store.list_blocks(include_deprecated=False)
    candidates: list[ConsolidationCandidate] = []
    used: set[str] = set()
    ollama_suggestions = 0

    for idx, block in enumerate(blocks):
        if block.id in used:
            continue
        cluster = [block]
        for other in blocks[idx + 1 :]:
            if other.id in used:
                continue
            if _similarity(block, other) >= 0.75:
                cluster.append(other)
        if len(cluster) < 2:
            continue
        for item in cluster:
            used.add(item.id)
        proposed_body, used_ollama = _draft_merge(cluster)
        if used_ollama:
            ollama_suggestions += 1
        candidates.append(
            ConsolidationCandidate(
                kind="duplicate_cluster",
                affected_block_ids=[item.id for item in cluster],
                proposed_action="merge",
                proposed_body=proposed_body,
                evidence={"method": "ollama" if used_ollama else "deterministic_only"},
            )
        )

    cutoff = datetime.now(UTC) - timedelta(days=180)
    stale_lessons = [
        item for item in store.list_lesson_candidates(status="inbox", limit=500) if item.created_at < cutoff
    ]
    for lesson in stale_lessons:
        candidates.append(
            ConsolidationCandidate(
                kind="stale_candidate",
                affected_block_ids=lesson.evidence_trace_ids,
                proposed_action="deprecate",
                evidence={"lesson_id": lesson.id, "method": "deterministic_only"},
            )
        )

    if not dry_run:
        for candidate in candidates:
            store.upsert_consolidation_candidate(candidate)

    duplicate_count = sum(1 for candidate in candidates if candidate.kind == "duplicate_cluster")
    stale_count = sum(1 for candidate in candidates if candidate.kind == "stale_candidate")
    return ConsolidationReport(
        duplicates=duplicate_count,
        stale=stale_count,
        ollama_suggestions=ollama_suggestions,
        written=0 if dry_run else len(candidates),
    )


__all__ = ["ConsolidationReport", "consolidate"]
