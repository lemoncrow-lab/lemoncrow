"""Shadow-first bounded evidence-resolution policy.

This module chooses deterministic next actions from EvidenceState.  It does not
execute those actions yet: enforcement is intentionally gated on the replay/A-B
acceptance criteria in the intelligent-runtime plan.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lemoncrow.core.foundation.runtime_decisions import RuntimeDecisionEvent
from lemoncrow.pro.capabilities.code_context.evidence_state import EvidenceState

EvidenceResolutionMode = Literal["off", "shadow", "experiment"]
EvidenceResolutionAction = Literal[
    "STOP",
    "EXPAND_RELATIONS",
    "FOLLOW_UNRESOLVED_SYMBOL",
    "REFORMULATE_LITERAL",
    "EXPAND_NEIGHBOR_FILE",
    "HYDRATE_SOURCE",
]

_DEFAULT_ROUND_LIMIT = 1
_DEFAULT_TOKEN_BUDGET = 800
_DEFAULT_LATENCY_BUDGET_MS = 150


def _bounded_env_int(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            return max(low, min(high, int(raw)))
        except ValueError:
            pass
    return default


def normalize_evidence_resolution_mode(value: str | None = None) -> EvidenceResolutionMode:
    """Return the qualified runtime mode.

    ``enforce`` deliberately remains shadow until a benchmark gate qualifies it.
    ``experiment`` is the explicit benchmark-only behavior arm used to generate
    that evidence; it is never selected by the production default.
    """

    normalized = (value or os.environ.get("LEMONCROW_EVIDENCE_RESOLUTION_MODE", "shadow")).strip().lower()
    if normalized in {"off", "disabled", "none"}:
        return "off"
    if (
        normalized == "experiment"
        and os.environ.get("LEMONCROW_EVIDENCE_RESOLUTION_EXPERIMENT", "").strip().lower() == "benchmark"
    ):
        return "experiment"
    return "shadow"


class EvidenceResolutionProposal(BaseModel):
    """One bounded deterministic resolution proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: EvidenceResolutionMode
    requested_enforce: bool = False
    experimental: bool = False
    eligible: bool = False
    action: EvidenceResolutionAction = "STOP"
    reason_codes: tuple[str, ...] = ()
    target_path: str | None = None

    round_limit: int = Field(default=_DEFAULT_ROUND_LIMIT, ge=0, le=5)
    token_budget: int = Field(default=_DEFAULT_TOKEN_BUDGET, ge=0, le=8000)
    latency_budget_ms: int = Field(default=_DEFAULT_LATENCY_BUDGET_MS, ge=0, le=5000)

    def to_runtime_event(
        self,
        state: EvidenceState,
        *,
        session_id: str | None = None,
        actual_action: EvidenceResolutionAction = "STOP",
        rounds: int = 0,
        elapsed_ms: int = 0,
    ) -> RuntimeDecisionEvent:
        """Record proposed and actual behavior without implying qualification."""

        kind = "retrieval.expand" if self.eligible and self.action != "STOP" else "retrieval.stop"
        reasons = [*state.reason_codes, *self.reason_codes]
        if self.requested_enforce:
            reasons.append("enforcement_not_qualified")
        if self.experimental:
            reasons.append("benchmark_experiment")
        return RuntimeDecisionEvent(
            kind=kind,
            phase="retrieve",
            policy="bounded-evidence-resolution",
            policy_version="1-experiment" if self.experimental else "1-shadow",
            mode="enforce" if self.experimental and rounds > 0 else "shadow",
            session_id=session_id,
            evidence_refs=state.evidence_refs,
            confidence=state.confidence,
            reason_codes=tuple(dict.fromkeys(reasons)),
            proposed={
                "action": self.action,
                "target_path": self.target_path,
                "round_limit": self.round_limit,
            },
            actual={"action": actual_action, "rounds": max(0, int(rounds))},
            budget={
                "tokens": self.token_budget,
                "latency_ms": self.latency_budget_ms,
                "rounds": self.round_limit,
            },
            metrics={
                "eligible": self.eligible,
                "evidence_status": state.status,
                "candidate_count": state.candidate_count,
                "source_section_count": state.source_section_count,
                "relationship_count": state.relationship_count,
                "requested_enforce": self.requested_enforce,
                "experimental": self.experimental,
                "elapsed_ms": max(0, int(elapsed_ms)),
            },
        )


def propose_evidence_resolution(
    state: EvidenceState,
    *,
    mode: str | None = None,
) -> EvidenceResolutionProposal:
    """Choose a bounded deterministic next action without executing it."""

    raw_mode = (mode or os.environ.get("LEMONCROW_EVIDENCE_RESOLUTION_MODE", "shadow")).strip().lower()
    normalized_mode = normalize_evidence_resolution_mode(raw_mode)
    requested_enforce = raw_mode in {"enforce", "on", "enabled"}
    experimental = normalized_mode == "experiment"
    round_limit = _bounded_env_int(
        "LEMONCROW_EVIDENCE_RESOLUTION_ROUNDS",
        _DEFAULT_ROUND_LIMIT,
        low=0,
        high=3,
    )
    token_budget = _bounded_env_int(
        "LEMONCROW_EVIDENCE_RESOLUTION_TOKENS",
        _DEFAULT_TOKEN_BUDGET,
        low=0,
        high=4000,
    )
    latency_budget_ms = _bounded_env_int(
        "LEMONCROW_EVIDENCE_RESOLUTION_LATENCY_MS",
        _DEFAULT_LATENCY_BUDGET_MS,
        low=0,
        high=2000,
    )

    common: dict[str, Any] = {
        "mode": normalized_mode,
        "requested_enforce": requested_enforce,
        "experimental": experimental,
        "round_limit": round_limit,
        "token_budget": token_budget,
        "latency_budget_ms": latency_budget_ms,
    }

    if normalized_mode == "off":
        return EvidenceResolutionProposal(**common, reason_codes=("resolution_disabled",))
    if state.status in {"decisive", "useful"}:
        return EvidenceResolutionProposal(**common, reason_codes=("evidence_sufficient",))
    if state.status == "dark":
        return EvidenceResolutionProposal(**common, reason_codes=("repair_dark_channel_first",))
    if state.status == "absent":
        return EvidenceResolutionProposal(**common, reason_codes=("no_safe_expansion_target",))
    if state.truncated:
        return EvidenceResolutionProposal(**common, reason_codes=("context_pressure",))
    if not state.top_paths:
        return EvidenceResolutionProposal(**common, reason_codes=("no_safe_expansion_target",))
    if round_limit <= 0 or token_budget <= 0 or latency_budget_ms <= 0:
        return EvidenceResolutionProposal(**common, reason_codes=("resolution_budget_exhausted",))

    target = state.top_paths[0]
    if state.source_section_count == 0:
        return EvidenceResolutionProposal(
            **common,
            eligible=True,
            action="HYDRATE_SOURCE",
            target_path=target,
            reason_codes=("low_rank_margin", "missing_source"),
        )
    if state.relationship_count == 0:
        return EvidenceResolutionProposal(
            **common,
            eligible=True,
            action="EXPAND_RELATIONS",
            target_path=target,
            reason_codes=("low_rank_margin", "missing_relations"),
        )
    return EvidenceResolutionProposal(**common, reason_codes=("ambiguous_but_enriched",))


__all__ = [
    "EvidenceResolutionAction",
    "EvidenceResolutionMode",
    "EvidenceResolutionProposal",
    "normalize_evidence_resolution_mode",
    "propose_evidence_resolution",
]
