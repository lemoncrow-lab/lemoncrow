"""Plan checker — validate a proposed agent plan against ReasonBlocks.

The plan checker is the most important entry point in v1. It is what
turns the runtime from "nice docs" into "actively prevents known dead
ends before the agent edits files".

A plan is a list of human-readable steps the agent intends to take. We:

1. Retrieve relevant ReasonBlocks for the task context.
2. For each block, scan plan steps for substrings matching its dead-ends.
3. For each block, scan plan steps for the keywords that would indicate
   verification was included. If verification is mandated by the block
   but missing from the plan, raise a warning (or block, for high-risk
   domains).
4. Compose a suggested-plan patch from the procedure of the most relevant
   matched block(s).

Severity rules (kept intentionally simple):
- dead-end match     → high
- missing verification (high-risk domain) → high
- missing verification (other domains)    → medium
- low-confidence match (only triggers, no scope) → low

Output status:
- blocked → any high-severity warning
- warn    → any medium/low warnings
- pass    → no warnings
"""

from __future__ import annotations

import re

from atelier.core.foundation.models import (
    PlanCheckResult,
    PlanStatus,
    PlanWarning,
    ReasonBlock,
    Severity,
)
from atelier.core.foundation.retriever import TaskContext, retrieve
from atelier.core.foundation.store import ReasoningStore

HIGH_RISK_DOMAINS = {
    "state.change",
    "source.truth",
    "policy.change",
    "knowledge",
}

# Words that indicate a step contains some form of verification.
_VERIFY_HINTS = (
    "verify",
    "validation",
    "validate",
    "audit",
    "re-fetch",
    "refetch",
    "post-publish",
    "post publish",
    "check",
    "assert",
    "test",
    "rollback",
    "snapshot",
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _step_matches_phrase(step: str, phrase: str) -> bool:
    """Loose substring match after normalization. Phrase must be >= 4 chars."""
    n_step = _normalize(step)
    n_phrase = _normalize(phrase)
    if len(n_phrase) < 4:
        return False
    return n_phrase in n_step


def _plan_includes_verification(plan: list[str]) -> bool:
    blob = " ".join(_normalize(s) for s in plan)
    return any(h in blob for h in _VERIFY_HINTS)


# --------------------------------------------------------------------------- #
# Plan checker                                                                #
# --------------------------------------------------------------------------- #


def check_plan(
    store: ReasoningStore,
    *,
    task: str,
    plan: list[str],
    domain: str | None = None,
    files: list[str] | None = None,
    tools: list[str] | None = None,
    errors: list[str] | None = None,
    max_blocks: int = 5,
) -> PlanCheckResult:
    """Run the plan checker against the store."""
    ctx = TaskContext(
        task=task,
        domain=domain,
        files=files or [],
        tools=tools or [],
        errors=errors or [],
    )
    scored = retrieve(store, ctx, limit=max_blocks)
    matched_blocks_ids = [s.block.id for s in scored]

    warnings: list[PlanWarning] = []
    suggested: list[str] = []
    dead_end_blocks: list[ReasonBlock] = []

    for s in scored:
        block = s.block
        # Dead-end detection.
        for dead in block.dead_ends:
            for step in plan:
                if _step_matches_phrase(step, dead):
                    warnings.append(
                        PlanWarning(
                            severity="high",
                            reason_block=block.title,
                            message=(f"Known dead end: {dead!r}. " f"Replace with the procedure from '{block.title}'."),
                        )
                    )
                    if block not in dead_end_blocks:
                        dead_end_blocks.append(block)
                    break

        # Missing verification.
        if block.verification and not _plan_includes_verification(plan):
            severity: Severity = "high" if (domain in HIGH_RISK_DOMAINS) else "medium"
            warnings.append(
                PlanWarning(
                    severity=severity,
                    reason_block=block.title,
                    message=(
                        "Plan does not include verification. Required for this "
                        f"domain: {', '.join(block.verification)}"
                    ),
                )
            )

    # Suggested plan: prefer the block(s) whose dead-ends were hit, then top-scored.
    source_blocks: list[ReasonBlock] = dead_end_blocks if dead_end_blocks else ([scored[0].block] if scored else [])
    for block in source_blocks:
        for step in [*block.procedure, *block.verification]:
            if step not in suggested:
                suggested.append(step)

    # Rubric-level forbidden checks (Beseam operating law).
    rubrics = store.list_rubrics(domain=domain)
    for rubric in rubrics:
        # Match rubric by triggers if domain match is too broad?
        # Or just check all rubrics in the domain.
        # For now, if task triggers match, we check forbidden.
        task_lower = task.lower()
        matched = False
        if domain and rubric.domain and domain.startswith(rubric.domain):
            matched = True
        for trig in rubric.triggers:
            if trig.lower() in task_lower:
                matched = True
                break

        if matched:
            for step in plan:
                step_lower = step.lower()
                for phrase in rubric.forbidden_phrases:
                    if phrase.lower() in step_lower:
                        warnings.append(
                            PlanWarning(
                                severity="high",
                                reason_block=f"rubric:{rubric.id}",
                                message=(
                                    f"Forbidden by rubric {rubric.id!r}: step {step!r} "
                                    f"contains banned phrase {phrase!r}."
                                ),
                            )
                        )

    if any(w.severity == "high" for w in warnings):
        status: PlanStatus = "blocked"
    elif warnings:
        status = "warn"
    else:
        status = "pass"

    return PlanCheckResult(
        status=status,
        warnings=warnings,
        suggested_plan=suggested,
        matched_blocks=matched_blocks_ids,
    )


def detect_known_dead_ends(plan: list[str], blocks: list[ReasonBlock]) -> list[PlanWarning]:
    """Standalone dead-end detector (used by monitors)."""
    warnings: list[PlanWarning] = []
    for block in blocks:
        for dead in block.dead_ends:
            for step in plan:
                if _step_matches_phrase(step, dead):
                    warnings.append(
                        PlanWarning(
                            severity="high",
                            reason_block=block.title,
                            message=f"Known dead end: {dead!r}",
                        )
                    )
                    break
    return warnings
