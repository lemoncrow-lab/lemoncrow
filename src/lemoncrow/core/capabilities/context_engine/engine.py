"""ContextEngine: rank, assess sufficiency, and expand within a budget.

The loop::

    candidates -> ranker -> sufficiency --sufficient--> answer
                               |
                           insufficient
                               v
             next action from missing signals (follow a definition,
             expand relations of an ambiguous head)
                               v
          expander (index-specific) -> merge (head preserved) -> reassess

Bounded by ``RuntimeBudget.max_rounds`` and ``latency_target_ms``; at most two
additions per round, always inside the caller's result limit, and the top two
ranked results never move.

Mode (``LEMONCROW_CONTEXT_ENGINE_MODE``): ``shadow`` (default) computes and
records what it would do but returns the round-0 ranking unchanged;
``enforce`` executes expansions; ``off`` skips assessment.  Enforcement is
qualified offline with ``lemoncrow eval replay`` before it is turned on.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from lemoncrow.core.capabilities.context_engine.candidates import Candidate, ContextQuery
from lemoncrow.core.capabilities.context_engine.ranking import HeuristicRanker, Ranker
from lemoncrow.core.capabilities.context_engine.sufficiency import Sufficiency, assess_sufficiency
from lemoncrow.core.foundation.runtime_budget import RuntimeBudget, infer_budget
from lemoncrow.core.foundation.runtime_decisions import RuntimeDecisionEvent
from lemoncrow.core.foundation.runtime_trace import record_decision

ContextEngineMode = Literal["off", "shadow", "enforce"]
ActionKind = Literal["follow_symbol", "expand_relations"]

_MAX_ADDITIONS_PER_ROUND = 2
_KEEP_HEAD = 2


def context_engine_mode(value: str | None = None) -> ContextEngineMode:
    raw = (value if value is not None else os.environ.get("LEMONCROW_CONTEXT_ENGINE_MODE", "shadow")).strip().lower()
    if raw in {"off", "disabled", "none", "0"}:
        return "off"
    if raw in {"enforce", "on", "enabled", "1"}:
        return "enforce"
    return "shadow"


@dataclass(frozen=True, slots=True)
class ExpansionAction:
    kind: ActionKind
    target: str

    def to_payload(self) -> dict[str, str]:
        return {"action": self.kind, "target": self.target}


Expander = Callable[[ExpansionAction, ContextQuery], Sequence[Candidate]]


@dataclass(slots=True)
class ContextResult:
    candidates: list[Candidate]
    sufficiency: Sufficiency
    initial_sufficiency: Sufficiency
    budget: RuntimeBudget
    ranker: str
    mode: ContextEngineMode
    rounds: int = 0
    proposed: list[ExpansionAction] = field(default_factory=list)
    taken: list[ExpansionAction] = field(default_factory=list)
    stop_reason: str = "sufficient"
    elapsed_ms: int = 0
    decisions: list[RuntimeDecisionEvent] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.taken)


def _next_action(sufficiency: Sufficiency, tried: set[ExpansionAction]) -> ExpansionAction | None:
    for signal in sufficiency.missing:
        if signal.kind == "definition" and signal.target:
            action = ExpansionAction("follow_symbol", signal.target)
        elif signal.kind == "relations" and signal.target:
            action = ExpansionAction("expand_relations", signal.target)
        else:
            continue
        if action not in tried:
            return action
    return None


def merge_preserving_head(ranked: Sequence[Candidate], added: Sequence[Candidate], *, limit: int) -> list[Candidate]:
    """Add new evidence inside ``limit`` without moving the top results."""
    cap = max(1, int(limit))
    original = list(ranked[:cap])
    seen = {c.path for c in original}
    additions: list[Candidate] = []
    for candidate in added:
        if candidate.path not in seen:
            seen.add(candidate.path)
            additions.append(candidate)
        if len(additions) >= _MAX_ADDITIONS_PER_ROUND:
            break
    if not additions:
        return original
    room = cap - len(original)
    if room >= len(additions):
        return [*original, *additions]
    keep = max(min(_KEEP_HEAD, cap), cap - len(additions))
    return [*original[:keep], *additions][:cap]


class ContextEngine:
    """Minimum-sufficient context selection over index-provided candidates."""

    def __init__(
        self,
        *,
        ranker: Ranker | None = None,
        expanders: Mapping[str, Expander] | None = None,
        mode: ContextEngineMode | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.ranker: Ranker = ranker or HeuristicRanker()
        self.expanders = dict(expanders or {})
        self.mode: ContextEngineMode = mode or context_engine_mode()
        self._clock = clock

    def resolve(
        self,
        query: ContextQuery,
        candidates: Sequence[Candidate],
        budget: RuntimeBudget | None = None,
        *,
        ranked: bool = False,
    ) -> ContextResult:
        """Rank ``candidates`` (unless ``ranked``), assess, and expand if allowed."""
        started = self._clock()
        resolved_budget = budget or infer_budget("code_search", {"query": query.text})
        ordered = list(candidates) if ranked else self.ranker.rank(query, candidates)
        head = ordered[: max(1, query.limit)]
        if self.mode == "off":
            empty = assess_sufficiency(query, head)
            return ContextResult(head, empty, empty, resolved_budget, self.ranker.name, "off", stop_reason="off")

        initial = sufficiency = assess_sufficiency(query, head)
        result = ContextResult(
            candidates=head,
            sufficiency=sufficiency,
            initial_sufficiency=initial,
            budget=resolved_budget,
            ranker=self.ranker.name,
            mode=self.mode,
        )
        tried: set[ExpansionAction] = set()
        while not sufficiency.sufficient:
            action = _next_action(sufficiency, tried)
            if action is None:
                result.stop_reason = "no_expansion_target"
                break
            tried.add(action)
            result.proposed.append(action)
            if self.mode != "enforce":
                result.stop_reason = "shadow"
                break
            if result.rounds >= resolved_budget.max_rounds:
                result.stop_reason = "round_budget"
                break
            if self._elapsed_ms(started) >= resolved_budget.latency_target_ms:
                result.stop_reason = "latency_budget"
                break
            expander = self.expanders.get(action.kind)
            if expander is None:
                result.stop_reason = "no_expander"
                break
            try:
                added = list(expander(action, query))
            except Exception:
                result.stop_reason = "expander_failed"
                break
            result.rounds += 1
            merged = merge_preserving_head(result.candidates, added, limit=query.limit)
            if [c.path for c in merged] == [c.path for c in result.candidates]:
                continue
            result.taken.append(action)
            result.candidates = merged
            sufficiency = assess_sufficiency(query, merged)
            result.sufficiency = sufficiency
        else:
            result.stop_reason = "sufficient"

        result.elapsed_ms = self._elapsed_ms(started)
        result.decisions = self._decisions(result)
        for event in result.decisions:
            record_decision(event)
        return result

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))

    def _decisions(self, result: ContextResult) -> list[RuntimeDecisionEvent]:
        mode: Literal["shadow", "enforce"] = "enforce" if result.mode == "enforce" else "shadow"
        refs = tuple(c.path for c in result.candidates[:8])
        events = [
            RuntimeDecisionEvent(
                kind="context.sufficiency",
                phase="retrieve",
                policy="context-sufficiency",
                policy_version="1",
                mode=mode,
                evidence_refs=refs,
                confidence=result.sufficiency.score,
                reason_codes=tuple(f"missing:{m.kind}" for m in result.initial_sufficiency.missing) or ("sufficient",),
                proposed={"initial": _suff_summary(result.initial_sufficiency)},
                actual={"final": _suff_summary(result.sufficiency)},
                budget=result.budget.to_payload(),
                metrics=dict(result.initial_sufficiency.components),
            )
        ]
        taken = set(result.taken)
        for action in result.proposed:
            events.append(
                RuntimeDecisionEvent(
                    kind="context.expand",
                    phase="retrieve",
                    policy="context-expansion",
                    policy_version="1",
                    mode=mode,
                    reason_codes=(f"missing:{'definition' if action.kind == 'follow_symbol' else 'relations'}",),
                    proposed=action.to_payload(),
                    actual={"executed": action in taken},
                    budget={"rounds": result.budget.max_rounds, "latency_ms": result.budget.latency_target_ms},
                )
            )
        events.append(
            RuntimeDecisionEvent(
                kind="context.stop",
                phase="retrieve",
                policy="context-expansion",
                policy_version="1",
                mode=mode,
                reason_codes=(result.stop_reason,),
                proposed={"rounds_allowed": result.budget.max_rounds},
                actual={"rounds": result.rounds, "expanded": len(result.taken)},
                metrics={"elapsed_ms": result.elapsed_ms, "ranker": result.ranker},
            )
        )
        return events


def _suff_summary(sufficiency: Sufficiency) -> dict[str, Any]:
    return {
        "score": sufficiency.score,
        "sufficient": sufficiency.sufficient,
        "missing": [m.kind for m in sufficiency.missing],
    }


__all__ = [
    "ContextEngine",
    "ContextEngineMode",
    "ContextResult",
    "Expander",
    "ExpansionAction",
    "context_engine_mode",
    "merge_preserving_head",
]
