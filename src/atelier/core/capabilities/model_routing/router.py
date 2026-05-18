"""Prospective per-turn model routing recommendations.

The router is advisory: host CLIs keep ownership of actual model selection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

ModelTier = Literal["cheap", "medium", "expensive"]


_CHEAP_TOOLS = {
    "bash",
    "shell",
    "read",
    "smart_read",
    "search",
    "smart_search",
    "grep",
    "glob",
    "ls",
    "list",
    "context",
    "memory",
}
_MEDIUM_TOOLS = {"edit", "smart_edit", "write", "apply_patch", "compact", "verify", "route"}
_EXPENSIVE_TOOLS = {"agent", "task", "spawn", "delegate", "architect"}

# Session-phase classification — used by _score_session_phase.
# Exploration tools: reading, searching, inspecting the codebase.
_EXPLORATION_TOOLS = frozenset(
    {
        "grep",
        "glob",
        "search",
        "smart_search",
        "context",
        "memory",
        "webfetch",
        "websearch",
        "toolsearch",
        "ls",
        "list",
        "read",
        "smart_read",
    }
)
# Execution tools: mutating the workspace or delegating sub-work.
_EXECUTION_TOOLS = frozenset(
    {
        "edit",
        "smart_edit",
        "write",
        "multiedit",
        "notebookedit",
        "bash",
        "shell",
        "agent",
        "task",
        "todowrite",
        "apply_patch",
    }
)

_CHEAP_VERBS_RE = re.compile(r"\b(explain|show|list|summari[sz]e|read|find|search|inspect)\b", re.IGNORECASE)
_MEDIUM_VERBS_RE = re.compile(r"\b(implement|fix|add|update|change|refactor|test|verify)\b", re.IGNORECASE)
_EXPENSIVE_VERBS_RE = re.compile(
    r"\b(design|architect|plan|strategy|migrate|rewrite|end[- ]to[- ]end)\b", re.IGNORECASE
)
_SMALL_OUTPUT_RE = re.compile(r"\b(<\s*500|under\s+500|brief|short|concise|one[- ]line)\b", re.IGNORECASE)
_OPEN_OUTPUT_RE = re.compile(r"\b(open[- ]ended|comprehensive|full|deep|thorough|all files|entire)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ModelRecommendation:
    tier: ModelTier
    model: str
    reasons: list[str] = field(default_factory=list)
    score: int = 0
    cache_affinity_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "model": self.model,
            "reasons": list(self.reasons),
            "score": self.score,
            "cache_affinity_model": self.cache_affinity_model,
        }


class ModelRouter:
    """Score a prospective turn into cheap, medium, or expensive model tiers."""

    def __init__(
        self,
        *,
        cheap_model: str = "claude-haiku-4-5",
        medium_model: str = "claude-sonnet-4.6",
        expensive_model: str = "claude-opus-4-7",
    ) -> None:
        self._models: dict[ModelTier, str] = {
            "cheap": cheap_model,
            "medium": medium_model,
            "expensive": expensive_model,
        }

    def score(
        self,
        tool_name: str,
        task_text: str,
        session_state: Mapping[str, Any] | None = None,
    ) -> ModelRecommendation:
        state = session_state or {}
        reasons: list[str] = []
        score = 0

        tool_score, tool_reason = self._score_tool(tool_name)
        score += tool_score
        reasons.append(tool_reason)

        verb_score, verb_reason = self._score_task_text(task_text)
        score += verb_score
        reasons.append(verb_reason)

        output_score, output_reason = self._score_output_target(task_text, state)
        score += output_score
        reasons.append(output_reason)

        error_score, error_reason = self._score_prior_errors(state)
        score += error_score
        reasons.append(error_reason)

        phase_score, phase_reason = self._score_session_phase(state)
        score += phase_score
        reasons.append(phase_reason)

        tier = self._tier_for_score(score)
        model = self._models[tier]

        cache_affinity_model = _clean_string(state.get("cache_affinity_model"))
        if cache_affinity_model:
            affinity_tier = self._tier_for_model(cache_affinity_model, fallback=tier)
            _tier_rank: dict[ModelTier, int] = {"cheap": 0, "medium": 1, "expensive": 2}
            # Only follow cache affinity when it is at the same tier or at most one
            # step below the scored tier.  Never downgrade an expensive task to cheap
            # just because a haiku wrote the last cache block.
            if _tier_rank[affinity_tier] >= _tier_rank[tier] - 1:
                model = cache_affinity_model
                tier = affinity_tier
                reasons.append("cache_affinity: follow previous cache-writing model")
            else:
                reasons.append(f"cache_affinity ignored: scored {tier}, affinity suggests {affinity_tier}")

        return ModelRecommendation(
            tier=tier,
            model=model,
            reasons=reasons,
            score=score,
            cache_affinity_model=cache_affinity_model,
        )

    def _score_tool(self, tool_name: str) -> tuple[int, str]:
        normalized = tool_name.strip().lower().replace("-", "_")
        if normalized in _EXPENSIVE_TOOLS or "agent" in normalized:
            return 2, f"tool={tool_name}: agent/delegation work"
        if normalized in _MEDIUM_TOOLS or "edit" in normalized or "write" in normalized:
            return 1, f"tool={tool_name}: edit/write work"
        if normalized in _CHEAP_TOOLS or normalized.startswith(("read", "search")):
            return 0, f"tool={tool_name}: read/search work"
        return 1, f"tool={tool_name}: unknown tool defaults to medium"

    def _score_task_text(self, task_text: str) -> tuple[int, str]:
        if _EXPENSIVE_VERBS_RE.search(task_text):
            return 2, "task_verb: design/architecture/planning signal"
        if _MEDIUM_VERBS_RE.search(task_text):
            return 1, "task_verb: implementation/fix signal"
        if _CHEAP_VERBS_RE.search(task_text):
            return 0, "task_verb: explain/show/list signal"
        return 1, "task_verb: no clear cheap signal"

    def _score_output_target(self, task_text: str, state: Mapping[str, Any]) -> tuple[int, str]:
        target = " ".join([task_text, str(state.get("output_target", ""))])
        if _OPEN_OUTPUT_RE.search(target):
            return 2, "output_target: open-ended or comprehensive"
        if _SMALL_OUTPUT_RE.search(target):
            return 0, "output_target: bounded under 500 tokens"
        max_tokens = _safe_int(state.get("max_output_tokens"))
        if max_tokens and max_tokens < 500:
            return 0, "output_target: max_output_tokens < 500"
        if max_tokens and max_tokens < 3_000:
            return 1, "output_target: max_output_tokens < 3000"
        return 1, "output_target: medium default"

    def _score_session_phase(self, state: Mapping[str, Any]) -> tuple[int, str]:
        """Score based on where we are in the session lifecycle.

        Exploration (score 0): early turns or read-dominant recent history.
          Haiku can discover the repo just fine.
        Transition (score 1): mix of reads and edits, not clearly either phase.
        Execution (score 2): edit-dominant recent history or deep into the session.
          Sonnet/Opus needed — model must track accumulated context about specific
          files and paths it already read, not re-discover them.
        """
        turn_number = _safe_int(state.get("turn_number"))
        recent: list[str] = []
        raw = state.get("recent_tool_calls")
        if isinstance(raw, list):
            recent = [str(t).strip().lower().replace("-", "_") for t in raw[-10:]]

        if not recent and turn_number == 0:
            return 0, "session_phase: no history, treat as exploration"

        explore_count = sum(1 for t in recent if t in _EXPLORATION_TOOLS or t.startswith(("read", "search", "grep")))
        exec_count = sum(1 for t in recent if t in _EXECUTION_TOOLS or "edit" in t or "write" in t)
        total = len(recent) or 1
        explore_ratio = explore_count / total
        exec_ratio = exec_count / total

        # Deep into session with execution-heavy recent history → execution phase
        if turn_number > 20 and exec_ratio >= 0.30:
            return 2, f"session_phase: execution (turn={turn_number}, exec_ratio={exec_ratio:.0%})"
        if exec_ratio >= 0.40:
            return 2, f"session_phase: execution (exec_ratio={exec_ratio:.0%})"

        # Clearly exploration-dominant → exploration phase
        if explore_ratio >= 0.60 and turn_number <= 15:
            return 0, f"session_phase: exploration (explore_ratio={explore_ratio:.0%}, turn={turn_number})"

        return 1, f"session_phase: transition (turn={turn_number}, exec={exec_ratio:.0%}, explore={explore_ratio:.0%})"

    def _score_prior_errors(self, state: Mapping[str, Any]) -> tuple[int, str]:
        errors = _safe_int(state.get("prior_errors"))
        if errors >= 3:
            return 2, "prior_errors: 3+ errors need deeper reasoning"
        if errors >= 1:
            return 1, "prior_errors: 1-2 errors"
        return 0, "prior_errors: none"

    def _tier_for_score(self, score: int) -> ModelTier:
        if score <= 2:
            return "cheap"
        if score <= 4:
            return "medium"
        return "expensive"

    def _tier_for_model(self, model: str, *, fallback: ModelTier) -> ModelTier:
        normalized = model.lower()
        if "haiku" in normalized or "mini" in normalized or "flash" in normalized:
            return "cheap"
        if "opus" in normalized or "pro" in normalized or "gpt-5.5" in normalized:
            return "expensive"
        if "sonnet" in normalized or "gpt-5.4" in normalized:
            return "medium"
        return fallback


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
