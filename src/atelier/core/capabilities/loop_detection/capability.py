"""LoopDetectionCapability — orchestrates all loop detection sub-modules."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from .models import LoopReport, PatternMatch
from .patterns import _ALL_DETECTORS
from .rescue import match_rescue, scored_rescue

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from atelier.infra.runtime.run_ledger import RunLedger

_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Tokens wasted per iteration of a detected loop (conservative estimate)
_TOKENS_PER_LOOP_ITERATION = 150

# Risk contribution per severity level
_SEVERITY_RISK: dict[str, float] = {"low": 0.25, "medium": 0.55, "high": 0.85}


def _estimate_risk(patterns: list[PatternMatch], n_events: int) -> float:
    """
    Compute a composite loop risk score in [0, 1].

    Risk = 1 - product(1 - risk_i) over all detected patterns.
    This treats each detector as an independent evidence source and
    combines them probabilistically (no double-counting).
    """
    if not patterns:
        return 0.0
    risk_not = 1.0
    for p in patterns:
        r = _SEVERITY_RISK.get(p.severity, 0.3)
        # Scale down risk for very small ledgers (not enough evidence)
        evidence_factor = min(1.0, n_events / 10.0)
        risk_not *= 1.0 - r * evidence_factor
    return round(min(1.0, 1.0 - risk_not), 3)


def _estimate_wasted_tokens(patterns: list[PatternMatch]) -> int:
    """Rough estimate of tokens wasted by the detected loops."""
    total = 0
    for p in patterns:
        # count = number of loop iterations; each costs ~_TOKENS_PER_LOOP_ITERATION
        total += max(0, p.count - 1) * _TOKENS_PER_LOOP_ITERATION
    return total


def _estimate_risk_velocity(events: list[dict[str, Any]], full_risk: float) -> float:
    """
    Estimate how quickly risk is growing by comparing risk in the first half
    of events to the overall risk score.

    Returns a value in [-1, 1]: positive = risk accelerating, negative = decelerating.
    """
    if len(events) < 6:
        return 0.0
    mid = len(events) // 2
    first_half_events = events[:mid]
    # Run detectors on first-half only
    first_patterns: list[PatternMatch] = []
    for detector in _ALL_DETECTORS:
        try:
            m = detector(first_half_events)
            if m is not None:
                first_patterns.append(m)
        except Exception:
            logger.warning(
                "Suppressed exception at capability.py:70",
                exc_info=True,
            )
    first_risk = _estimate_risk(first_patterns, len(first_half_events))
    velocity = round(full_risk - first_risk, 3)
    return max(-1.0, min(1.0, velocity))


class LoopDetectionCapability:
    """
    Comprehensive loop detection with multiple pattern recognisers.

    Detectors:
    - patch_revert_cycle    - alternating edits and reverts on the same file
    - search_read_loop      - excessive searching/reading without writing
    - hypothesis_loop       - same tool call repeated many times
    - cascade_failure       - chained error -> error -> error sequences
    - budget_burn           - accelerating tool usage (nearing turn budget)

    Enhanced features:
    - Composite risk score (0-1) via probabilistic combination of pattern risks
    - Wasted-token estimation for cost attribution
    - Severity escalation: multiple concurrent patterns -> higher aggregate severity
    """

    def check(self, ledger: RunLedger) -> LoopReport:
        """
        Analyse the ledger and return a :class:`LoopReport`.

        ``ledger.events`` is expected to be a list of dicts with at least
        ``{"kind": str, "summary": str, "payload": dict}`` keys.
        """
        raw_events: list[Any] = []
        with contextlib.suppress(Exception):
            raw_events = list(getattr(ledger, "events", []) or [])
        # Normalise to plain dicts so pattern detectors can use .get()
        events: list[dict[str, Any]] = [
            (
                {
                    "kind": getattr(e, "kind", ""),
                    "summary": getattr(e, "summary", ""),
                    "payload": getattr(e, "payload", {}),
                }
                if not isinstance(e, dict)
                else e
            )
            for e in raw_events
        ]

        patterns: list[PatternMatch] = []
        for detector in _ALL_DETECTORS:
            try:
                match = detector(events)
                if match is not None:
                    patterns.append(match)
            except Exception:
                logger.warning(
                    "Suppressed exception at capability.py:124",
                    exc_info=True,
                )

        loop_types = [p.pattern_name for p in patterns]
        loop_detected = bool(patterns)

        # Aggregate severity — multiple concurrent patterns escalate severity
        severity = "none"
        for p in patterns:
            if _SEVERITY_ORDER.get(p.severity, 0) > _SEVERITY_ORDER.get(severity, 0):
                severity = p.severity
        # Escalate if multiple high-risk patterns co-occur
        high_count = sum(1 for p in patterns if p.severity == "high")
        if high_count >= 2 and severity == "high":
            severity = "high"  # already max; could add "critical" in future
        elif len(patterns) >= 3 and severity == "medium":
            severity = "high"

        rescue_strategies = match_rescue(loop_types)

        # Cascade chain (error messages from cascade_failure pattern)
        cascade_chain: list[str] = []
        for p in patterns:
            if p.pattern_name == "cascade_failure":
                cascade_chain = p.evidence

        budget_risk = any(p.pattern_name == "budget_burn" for p in patterns)

        # Enhanced: composite risk score and wasted token estimate
        risk_score = _estimate_risk(patterns, len(events))
        wasted_tokens = _estimate_wasted_tokens(patterns)

        # Phase 3: risk velocity and scored rescue strategies
        risk_velocity = _estimate_risk_velocity(events, risk_score)
        rescue_scores = scored_rescue(loop_types)

        return LoopReport(
            loop_detected=loop_detected,
            severity=severity,
            loop_types=loop_types,
            prior_attempts=len(events),
            rescue_strategies=rescue_strategies,
            patterns=patterns,
            cascade_chain=cascade_chain,
            budget_risk=budget_risk,
            risk_score=risk_score,
            wasted_tokens=wasted_tokens,
            risk_velocity=risk_velocity,
            rescue_scores=rescue_scores,
        )

    def from_ledger(self, ledger: RunLedger) -> LoopReport:
        """Primary engine API — analyse a run ledger for loop patterns."""
        return self.check(ledger)
