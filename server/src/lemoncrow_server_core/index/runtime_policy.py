"""Evidence-aware policy adapter for the authoritative server index.

The public/hosted thin-server path answers ``code_search`` natively from the
revision-bound three-layer index.  It must therefore participate in runtime
policy directly instead of relying on the legacy MCP wrapper's observer hook.

Production is observation/shadow only.  The only behavior-changing mode is the
explicit benchmark experiment admitted by ``evidence_resolution``; even there
we preserve the native ranking head and add at most two Layer-3 relation
neighbors inside the caller's existing result limit.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.code_context.evidence_resolution import (
    EvidenceResolutionProposal,
    propose_evidence_resolution,
)
from lemoncrow.pro.capabilities.code_context.evidence_state import EvidenceState, evaluate_explore_evidence

from .contracts import IndexBackend
from .graph import ServerGraphAnalytics
from .query import QueryAnswer, QueryHit

_MAX_RELATION_ADDITIONS = 2


def _definition_matches_query(detail: Mapping[str, Any], query: str) -> bool:
    needle = query.strip().rsplit(".", 1)[-1]
    definitions = detail.get("definitions")
    if not needle or not isinstance(definitions, list):
        return False
    for definition in definitions:
        if isinstance(definition, Mapping) and str(definition.get("name") or "") == needle:
            return True
    return False


def native_evidence_state(
    answer: QueryAnswer,
    *,
    query: str,
    source_paths: Iterable[str] = (),
) -> EvidenceState:
    """Project one native server answer into the shared EvidenceState contract."""

    source = frozenset(source_paths)
    entries = [hit.to_wire() for hit in answer.hits]
    files = [{"path": hit.path, "source_sections": [{"available": True}]} for hit in answer.hits if hit.path in source]
    relationships = [
        {
            "path": hit.path,
            "via": str(hit.detail.get("related_via") or hit.detail.get("runtime_expansion") or ""),
        }
        for hit in answer.hits
        if hit.detail.get("related_via") or hit.detail.get("runtime_expansion")
    ]
    exact_match = False
    if answer.hits:
        first = answer.hits[0]
        exact_match = (
            first.path == query
            or first.path.rsplit("/", 1)[-1] == query
            or _definition_matches_query(first.detail, query)
        )
    payload: dict[str, Any] = {
        "entry_points": entries,
        "files": files,
        "relationships": {"native": relationships},
        "exact_match": exact_match,
        "truncated": answer.truncated,
        "cache_hit": False,
        "total_tokens": 0,
    }
    return evaluate_explore_evidence(
        payload,
        channels_requested=("server_index",),
        channels_live=("server_index",),
    )


def _relation_paths(payload: Mapping[str, Any]) -> tuple[str, ...]:
    ordered: list[str] = []
    for key in ("direct_importers", "transitive_importers", "affected_tests"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            path = str(value or "")
            if path and path not in ordered:
                ordered.append(path)
    return tuple(ordered)


def _relation_hit(
    backend: IndexBackend,
    *,
    org_id: str,
    view_id: str,
    path: str,
    related_to: str,
    score: float,
) -> QueryHit | None:
    entry = backend.views.entry(org_id, view_id, path)
    if entry is None:
        return None
    metadata = backend.analysis.metadata(org_id, entry.content_digest, entry.parser_profile)
    return QueryHit(
        path=path,
        score=max(0.0, float(score)),
        language=metadata[0] if metadata is not None else "",
        line_count=metadata[1] if metadata is not None else 0,
        size=entry.size,
        detail={
            "content_indexed": True,
            "runtime_expansion": "relation",
            "related_to": related_to,
        },
    )


def merge_relation_candidates(
    answer: QueryAnswer,
    relation_hits: Iterable[QueryHit],
    *,
    limit: int,
) -> QueryAnswer:
    """Add bounded relation evidence while preserving the native ranking head."""

    cap = max(1, int(limit))
    original = list(answer.hits[:cap])
    if not original:
        return answer
    seen = {hit.path for hit in original}
    additions = [hit for hit in relation_hits if hit.path not in seen][:_MAX_RELATION_ADDITIONS]
    if not additions:
        return answer

    keep_head = min(2, len(original), cap)
    room = max(0, cap - len(original))
    appended = additions[:room]
    remaining = additions[len(appended) :]
    merged = [*original, *appended]
    if remaining and cap > keep_head:
        replaceable = cap - keep_head
        replacement = remaining[:replaceable]
        if replacement:
            merged = [*merged[: cap - len(replacement)], *replacement]
    merged = merged[:cap]
    if tuple(merged) == answer.hits:
        return answer
    return QueryAnswer(
        kind=answer.kind,
        hits=tuple(merged),
        at_view_revision=answer.at_view_revision,
        searched_paths=answer.searched_paths,
        degraded=answer.degraded,
        degraded_reason=answer.degraded_reason,
        truncated=answer.truncated or len(additions) > max(0, cap - len(original)),
        remaining_hits=answer.remaining_hits,
    )


def _diagnostic(
    state: EvidenceState,
    proposal: EvidenceResolutionProposal,
    *,
    actual_action: str,
    rounds: int,
    before: int,
    after: int,
    elapsed_ms: int,
) -> dict[str, Any]:
    reasons = [*state.reason_codes, *proposal.reason_codes]
    if proposal.experimental:
        reasons.append("benchmark_experiment")
    return {
        "policy": "bounded-evidence-resolution",
        "policy_version": "1-experiment" if proposal.experimental else "1-shadow",
        "mode": proposal.mode,
        "evidence_status": state.status,
        "confidence": state.confidence,
        "proposed_action": proposal.action,
        "actual_action": actual_action,
        "rounds": max(0, int(rounds)),
        "candidate_count_before": max(0, int(before)),
        "candidate_count_after": max(0, int(after)),
        "reason_codes": list(dict.fromkeys(reasons))[:32],
        "elapsed_ms": max(0, int(elapsed_ms)),
    }


def apply_native_search_policy(
    backend: IndexBackend,
    *,
    org_id: str,
    view_id: str,
    query: str,
    answer: QueryAnswer,
    source_paths: Iterable[str],
    limit: int,
    repo_root: str | Path | None = None,
) -> tuple[QueryAnswer, dict[str, Any]]:
    """Observe native evidence and optionally run one benchmark-only relation round."""

    started = time.monotonic()
    state = native_evidence_state(answer, query=query, source_paths=source_paths)
    proposal = propose_evidence_resolution(state)
    resolved = answer
    actual_action = "STOP"
    rounds = 0

    if (
        proposal.experimental
        and proposal.eligible
        and proposal.action == "EXPAND_RELATIONS"
        and proposal.target_path
        and proposal.round_limit > 0
    ):
        graph = ServerGraphAnalytics(
            backend,
            org_id=org_id,
            view_id=view_id,
            content_size_cap=getattr(backend, "content_size_cap", 8 * 1024 * 1024),
            repo_root=Path(repo_root) if repo_root is not None else None,
        )
        relation_payload = graph.blast_radius(proposal.target_path, max_transitive_depth=2)
        paths = _relation_paths(relation_payload)
        tail_score = answer.hits[-1].score * 0.5 if answer.hits else 0.0
        relation_hits = [
            hit
            for path in paths[:8]
            if (
                hit := _relation_hit(
                    backend,
                    org_id=org_id,
                    view_id=view_id,
                    path=path,
                    related_to=proposal.target_path,
                    score=tail_score,
                )
            )
            is not None
        ]
        candidate = merge_relation_candidates(answer, relation_hits, limit=limit)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if candidate is not answer and elapsed_ms <= proposal.latency_budget_ms:
            resolved = candidate
            actual_action = "EXPAND_RELATIONS"
            rounds = 1

    elapsed_ms = round((time.monotonic() - started) * 1000)
    return resolved, _diagnostic(
        state,
        proposal,
        actual_action=actual_action,
        rounds=rounds,
        before=len(answer.hits),
        after=len(resolved.hits),
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "apply_native_search_policy",
    "merge_relation_candidates",
    "native_evidence_state",
]
