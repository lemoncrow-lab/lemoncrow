"""Data models for reasoning reuse capability.

Phase 13 (LINEAR-01/02) extends this module additively with the phase
state-machine schema: ``Phase``, ``PhasePlan``, ``PhaseResult``,
``PhaseCacheStats``, and ``RunMode``. Per D-01..D-06: a fixed shell prompt
plus per-phase user objectives is the cache-warm conversation backbone;
these dataclasses describe the schema only — orchestration lives in
``phase_runner.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


@dataclass
class ReuseSavings:
    """Tracks reasoning reuse savings over a session."""

    procedures_retrieved: int = 0
    context_tokens_saved: int = 0
    reuse_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "procedures_retrieved": self.procedures_retrieved,
            "context_tokens_saved": self.context_tokens_saved,
            "reuse_events": self.reuse_events,
        }


@dataclass
class RankedProcedure:
    """A procedure block ranked for relevance to the current task."""

    block_id: str
    title: str
    domain: str
    score: float
    base_score: float
    recency_score: float
    success_rate: float
    reuse_count: int
    snippet: str
    is_dead_end: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "title": self.title,
            "domain": self.domain,
            "score": round(self.score, 4),
            "base_score": round(self.base_score, 4),
            "recency_score": round(self.recency_score, 4),
            "success_rate": round(self.success_rate, 4),
            "reuse_count": self.reuse_count,
            "snippet": self.snippet,
            "is_dead_end": self.is_dead_end,
        }


@dataclass
class ProcedureCluster:
    """A group of related procedure blocks."""

    cluster_id: str
    centroid_title: str
    member_ids: list[str] = field(default_factory=list)
    avg_score: float = 0.0


# ---------------------------------------------------------------------------
# Phase 13 — Phase-Linear Cache-Reuse schema (LINEAR-01)
# ---------------------------------------------------------------------------


PhaseKind = Literal["agent", "gate", "side_effect"]
PhaseProfile = Literal["reader", "writer"]


@dataclass(frozen=True)
class Phase:
    """One node in the phase state-machine.

    A ``Phase`` describes a single segment of a phase-linear run. The
    runner replays phases in order, optionally continuing the prior
    phase's message list when ``continue_from`` is non-None (D-04).
    """

    name: str
    kind: PhaseKind
    profile: PhaseProfile
    objective_path: str | None
    continue_from: str | None
    next: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "profile": self.profile,
            "objective_path": self.objective_path,
            "continue_from": self.continue_from,
            "next": self.next,
        }


@dataclass
class PhasePlan:
    """Ordered DAG of phases keyed by name, with a single entry phase."""

    name: str
    entry: str
    phases: dict[str, Phase] = field(default_factory=dict)

    def iter_order(self) -> list[str]:
        """Yield phase names starting at ``entry`` and following ``next``.

        Terminates when a phase's ``next`` is None or already visited.
        """
        order: list[str] = []
        seen: set[str] = set()
        cur: str | None = self.entry
        while cur is not None and cur not in seen and cur in self.phases:
            order.append(cur)
            seen.add(cur)
            cur = self.phases[cur].next
        return order

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entry": self.entry,
            "phases": {n: p.to_dict() for n, p in self.phases.items()},
        }


@dataclass
class PhaseCacheStats:
    """Per-phase cache attribution recorded at the phase tail (D-07).

    Mirrors ``PrefixCachePlan.to_dict()`` plus provider-reported cache
    fields. ``minify_deltas`` is populated by later plans (LINEAR-03) and
    left empty here.
    """

    prefix_hash: str
    prefix_tokens: int
    dynamic_tokens: int
    total_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    invalidated_reason: str = ""
    minify_deltas: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix_hash": self.prefix_hash,
            "prefix_tokens": self.prefix_tokens,
            "dynamic_tokens": self.dynamic_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "invalidated_reason": self.invalidated_reason,
            "minify_deltas": list(self.minify_deltas),
        }


@dataclass
class PhaseResult:
    """The product of one phase: its terminal messages, stats, and output."""

    phase_name: str
    messages: list[dict[str, Any]]
    cache_stats: PhaseCacheStats
    output_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_name": self.phase_name,
            "messages": list(self.messages),
            "cache_stats": self.cache_stats.to_dict(),
            "output_text": self.output_text,
        }


class RunMode(StrEnum):
    """Top-level run mode for the cache-reuse agent (LINEAR-04 dispatch).

    Placed in this module so engine and benchmark can both import without
    introducing a cycle.
    """

    LINEAR = "linear"
    PER_AGENT = "per_agent"
    AUTO = "auto"
