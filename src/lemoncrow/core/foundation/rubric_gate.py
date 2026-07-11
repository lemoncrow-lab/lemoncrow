"""Rubric gate — verify agent output against domain-specific required checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from lemoncrow.core.foundation.models import (
    Rubric,
    RubricCheckOutcome,
    RubricResult,
)


def run_rubric(
    rubric: Rubric,
    checks: Mapping[str, bool | None],
) -> RubricResult:
    """Evaluate a result against a rubric.

    Args:
        rubric: The rubric to enforce.
        checks: Mapping of check_name -> outcome.
            - True  → pass
            - False → fail
            - None or missing key → missing
    """
    outcomes: list[RubricCheckOutcome] = []
    blocked = False
    warned = False

    # Required checks first.
    for name in rubric.required_checks:
        result = checks.get(name)
        if result is True:
            outcomes.append(RubricCheckOutcome(name=name, status="pass"))
        elif result is False:
            outcomes.append(RubricCheckOutcome(name=name, status="fail", detail="Required check failed."))
            if name in rubric.block_if_missing:
                blocked = True
            else:
                warned = True
        else:
            outcomes.append(RubricCheckOutcome(name=name, status="missing", detail="Required check not reported."))
            if name in rubric.block_if_missing:
                blocked = True
            else:
                warned = True

    # Optional warning checks.
    for name in rubric.warning_checks:
        result = checks.get(name)
        if result is True:
            outcomes.append(RubricCheckOutcome(name=name, status="pass"))
        elif result is False:
            outcomes.append(RubricCheckOutcome(name=name, status="warn"))
            warned = True
        else:
            outcomes.append(RubricCheckOutcome(name=name, status="missing"))
            warned = True

    escalations: list[str] = []
    for cond in rubric.escalation_conditions:
        if checks.get(cond):
            escalations.append(cond)

    if escalations:
        status: str = "escalate"
    elif blocked:
        status = "blocked"
    elif warned:
        status = "warn"
    else:
        status = "pass"

    return RubricResult(
        rubric_id=rubric.id,
        status=status,  # type: ignore[arg-type]
        outcomes=outcomes,
        escalations=escalations,
    )


def load_packaged_rubrics() -> list[Rubric]:
    """Load all rubrics shipped with the package.

    Reads every ``*.yaml`` / ``*.yml`` file from the sibling ``rubrics/``
    package directory (``lemoncrow/core/rubrics/``).  Parse failures are logged
    and skipped so a single bad file never prevents the store from starting.
    """
    import logging

    import yaml

    rubrics_dir = Path(__file__).parent.parent / "rubrics"
    rubrics: list[Rubric] = []

    if not rubrics_dir.is_dir():
        return rubrics

    paths = sorted(rubrics_dir.glob("*.yaml")) + sorted(rubrics_dir.glob("*.yml"))
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            rubrics.append(Rubric.model_validate(data))
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            logging.getLogger(__name__).warning("failed to load packaged rubric %s: %s", path.name, exc)

    return rubrics
