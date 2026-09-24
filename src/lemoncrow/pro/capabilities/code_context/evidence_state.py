"""Structured, side-effect-free evidence state for code retrieval."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lemoncrow.core.foundation.runtime_decisions import RuntimeDecisionEvent

EvidenceStatus = Literal["decisive", "useful", "ambiguous", "dark", "absent"]


class EvidenceState(BaseModel):
    """Observable retrieval facts used by later bounded runtime policy.

    V1 is intentionally conservative: it never causes another retrieval round
    and it never modifies the model-facing code_search payload.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: EvidenceStatus
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = ()

    top_paths: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    unresolved_refs: tuple[str, ...] = ()

    channels_requested: tuple[str, ...] = ()
    channels_live: tuple[str, ...] = ()

    candidate_count: int = Field(default=0, ge=0)
    source_file_count: int = Field(default=0, ge=0)
    source_section_count: int = Field(default=0, ge=0)
    relationship_count: int = Field(default=0, ge=0)

    tokens_used: int = Field(default=0, ge=0)
    budget_tokens: int = Field(default=0, ge=0)
    rank_margin: float | None = Field(default=None, ge=0.0, le=1.0)

    exact_match: bool = False
    truncated: bool = False
    cache_hit: bool = False

    def to_routing_summary(self) -> dict[str, object]:
        """Provider-neutral evidence summary for the existing quality router."""

        fallback_confidence = {
            "decisive": 1.0,
            "useful": 0.8,
            "ambiguous": 0.5,
            "dark": 0.0,
            "absent": 0.4,
        }[self.status]
        confidence = self.confidence if self.confidence is not None else fallback_confidence
        return {
            "confidence": confidence,
            "refs": list(self.evidence_refs),
            "retrieval_status": self.status,
            "retrieval_reason_codes": list(self.reason_codes),
            "retrieval_tokens": self.tokens_used,
            "retrieval_candidate_count": self.candidate_count,
            "retrieval_truncated": self.truncated,
        }

    def to_runtime_event(
        self,
        *,
        session_id: str | None = None,
        policy_version: str = "1",
    ) -> RuntimeDecisionEvent:
        """Project this observation into the shared runtime decision contract."""

        summary = {
            "status": self.status,
            "candidate_count": self.candidate_count,
            "source_file_count": self.source_file_count,
            "source_section_count": self.source_section_count,
        }
        return RuntimeDecisionEvent(
            kind="retrieval.evidence_state",
            phase="retrieve",
            policy="code-context-evidence",
            policy_version=policy_version,
            mode="shadow",
            session_id=session_id,
            evidence_refs=self.evidence_refs,
            confidence=self.confidence,
            reason_codes=self.reason_codes,
            proposed=summary,
            actual=summary,
            budget={"tokens": self.budget_tokens},
            metrics={
                "tokens_used": self.tokens_used,
                "rank_margin": self.rank_margin,
                "relationship_count": self.relationship_count,
                "exact_match": self.exact_match,
                "truncated": self.truncated,
                "cache_hit": self.cache_hit,
                "channels_requested": self.channels_requested,
                "channels_live": self.channels_live,
            },
        )


def _path(entry: Mapping[str, Any]) -> str:
    return str(entry.get("path") or entry.get("file_path") or "")


def _entry_score(entry: Mapping[str, Any]) -> float | None:
    raw = entry.get("score")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _relative_rank_margin(entry_points: list[Mapping[str, Any]]) -> float | None:
    if len(entry_points) < 2:
        return None
    first = _entry_score(entry_points[0])
    second = _entry_score(entry_points[1])
    if first is None or second is None:
        return None
    denominator = max(abs(first), 1e-9)
    return round(max(0.0, min(1.0, (first - second) / denominator)), 6)


def _relationship_count(payload: Mapping[str, Any]) -> int:
    relationships = payload.get("relationships")
    if not isinstance(relationships, Mapping):
        return 0
    total = 0
    for rows in relationships.values():
        if isinstance(rows, list):
            total += len(rows)
    return total


def _evidence_refs(
    entry_points: list[Mapping[str, Any]],
    files: list[Mapping[str, Any]],
    *,
    limit: int = 16,
) -> tuple[str, ...]:
    refs: list[str] = []
    seen_paths: set[str] = set()

    for entry in entry_points:
        path = _path(entry)
        if not path:
            continue
        start = entry.get("start_line")
        if not isinstance(start, int):
            start = entry.get("line")
        end = entry.get("end_line")
        ref = path
        if isinstance(start, int) and start > 0:
            ref = f"{path}:L{start}"
            if isinstance(end, int) and end >= start:
                ref += f"-L{end}"
        if path not in seen_paths:
            seen_paths.add(path)
            refs.append(ref)
        if len(refs) >= limit:
            return tuple(refs)

    for file_entry in files:
        path = _path(file_entry)
        if path and path not in seen_paths:
            seen_paths.add(path)
            refs.append(path)
        if len(refs) >= limit:
            break
    return tuple(refs)


def evaluate_explore_evidence(
    payload: Mapping[str, Any],
    *,
    budget_tokens: int = 0,
    channels_requested: tuple[str, ...] = (),
    channels_live: tuple[str, ...] = (),
    dark_channels: tuple[str, ...] = (),
    search_verdict: str | None = None,
) -> EvidenceState:
    """Classify an explore payload without mutating it or running retrieval."""

    raw_entries = payload.get("entry_points")
    entry_points = (
        [entry for entry in raw_entries if isinstance(entry, Mapping)] if isinstance(raw_entries, list) else []
    )
    raw_files = payload.get("files")
    files = [entry for entry in raw_files if isinstance(entry, Mapping)] if isinstance(raw_files, list) else []

    top_paths: list[str] = []
    for entry in [*entry_points, *files]:
        path = _path(entry)
        if path and path not in top_paths:
            top_paths.append(path)
        if len(top_paths) >= 8:
            break

    source_file_count = 0
    source_section_count = 0
    for entry in files:
        sections = entry.get("source_sections")
        if isinstance(sections, list) and sections:
            source_file_count += 1
            source_section_count += sum(isinstance(section, Mapping) for section in sections)

    candidate_paths = {_path(entry) for entry in [*entry_points, *files] if _path(entry)}
    for key in ("additional_relevant_files", "fused_recall", "deep_recall"):
        extra = payload.get(key)
        if isinstance(extra, list):
            candidate_paths.update(str(path) for path in extra if isinstance(path, str) and path)
    candidate_count = len(candidate_paths)
    exact_match = bool(payload.get("exact_match"))
    rank_margin = _relative_rank_margin(entry_points)
    truncated = bool(payload.get("truncated"))
    cache_hit = bool(payload.get("cache_hit"))
    try:
        tokens_used = max(0, int(payload.get("total_tokens") or 0))
    except (TypeError, ValueError):
        tokens_used = 0

    reasons: list[str] = []
    confidence: float | None = None

    if candidate_count == 0 and dark_channels:
        status: EvidenceStatus = "dark"
        reasons.extend(f"channel_dark:{name}" for name in dark_channels)
        confidence = 0.0
    elif candidate_count == 0 and search_verdict == "absent":
        status = "absent"
        reasons.append("reformulations_exhausted")
        confidence = 0.8
    elif candidate_count == 0:
        # One empty retrieval is not proof of absence. The session-aware
        # SearchFeedbackPolicy owns missed->absent promotion after distinct
        # reformulations; until then this is unresolved evidence.
        status = "ambiguous"
        reasons.append("no_candidates_single_pass")
        confidence = 0.25
    elif exact_match and source_section_count > 0:
        status = "decisive"
        reasons.extend(("exact_match", "source_hydrated"))
        confidence = 1.0
    elif exact_match:
        status = "useful"
        reasons.append("exact_match")
        confidence = 0.9
    elif rank_margin is not None and rank_margin < 0.15 and candidate_count > 1:
        status = "ambiguous"
        reasons.append("low_rank_margin")
        confidence = round(max(0.0, min(1.0, 0.5 + rank_margin)), 6)
    else:
        status = "useful"
        reasons.append("ranked_candidates")
        if rank_margin is not None:
            reasons.append("rank_separated")
            confidence = round(max(0.0, min(1.0, 0.5 + rank_margin / 2.0)), 6)

    if dark_channels and candidate_count > 0:
        reasons.extend(f"channel_dark:{name}" for name in dark_channels)
    if source_section_count:
        reasons.append("source_available")
    if truncated:
        reasons.append("budget_truncated")

    return EvidenceState(
        status=status,
        confidence=confidence,
        reason_codes=tuple(dict.fromkeys(reasons)),
        top_paths=tuple(top_paths),
        evidence_refs=_evidence_refs(entry_points, files),
        channels_requested=channels_requested,
        channels_live=channels_live,
        candidate_count=candidate_count,
        source_file_count=source_file_count,
        source_section_count=source_section_count,
        relationship_count=_relationship_count(payload),
        tokens_used=tokens_used,
        budget_tokens=max(0, int(budget_tokens)),
        rank_margin=rank_margin,
        exact_match=exact_match,
        truncated=truncated,
        cache_hit=cache_hit,
    )


__all__ = [
    "EvidenceState",
    "EvidenceStatus",
    "evaluate_explore_evidence",
]
