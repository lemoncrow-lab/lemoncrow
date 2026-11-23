"""Re-validate what actually lands: the merge result (Phase 10 item 18, P10-LAND).

The reviewed Snapshot is not what lands: the provider merges the head onto the
current base, plus whatever merged since review. When the provider builds the
merge result (GitHub ``merge_group`` for a merge queue, or checks reported on
the pull request's test-merge commit), the same checks are compared on three
sides, per stable check identity:

* **merge** -- the merge result;
* **head** -- the reviewed Snapshot's head;
* **base** -- the merge result's base: the provider's results on that base
  commit, which every Change queued on the same base shares (the R6 rule: the
  base side runs once per base commit, never per Change).

lc-debt: compares provider/CI checks on the three commits; LemonCrow-executed
Phase 9R recipes and behavior diffs do not run on the merge target yet (check
runs have only old/new sides), so ``behavior`` is empty and an R6 shared base
run is not reused here. Upgrade: a ``merge`` side in ``check_runs`` keyed by
the merge commit, reading its base side from ``base_results.find_shared_base``.

A check that passes on the head and does not fail on the base but fails on the
merge result is a **new failure**; a behavior key whose merge-result outcome
matches neither side is a **semantic conflict**. Either blocks landing (the
``merge-result`` readiness condition fails, so arming is refused and an armed
landing is withdrawn by the one revalidation path). A failure the base already
has is inherited, never blamed on this Change, and source judgments are never
touched: this is a landing gate, not a re-review.

Typed required checks (``landing.merge_result.required_checks``, RC11): each
named check identity must pass on the merge result *from its named producer
class* (``SideResult.source``). A same-identity row from another class (a
provider CI row standing in for a LemonCrow verification) never satisfies it:
missing or mistyped stays ``pending`` (landing waits; never a pass), and a
required check failing on the merge result fails the condition even when the
base fails it too. Every merge-result row is ``provider_ci`` today, so policy
refuses the other classes (``landing_policy.PRODUCED_MERGE_CHECK_CLASSES``)
until something produces them on the merge result.

No merge result yet is ``pending`` and SCM-owned (the merge queue builds it
only after landing is armed, so it can never block arming) when policy requires
merge-result checks; without that policy and without a result, no condition is
shown at all. Pure: classification and the readiness condition only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.readiness import ReadinessCondition, ReadinessInputs

FACT_KEY: Final[str] = "merge_result"
CONDITION_ORDER: Final[int] = 109
CONDITION_ID: Final[str] = "merge-result"
MAX_CHECKS: Final[int] = 200
"""Check identities compared per side; past it the comparison is ``unknown`` (never a pass)."""
MAX_NAMED: Final[int] = 4

_PASS: Final[frozenset[str]] = frozenset({"PASS", "SUCCESS", "PASSED"})
_FAIL: Final[frozenset[str]] = frozenset({"FAIL", "FAILED", "FAILURE", "ERROR", "TIMED_OUT", "CANCELLED"})


def _status(raw: str) -> str:
    value = str(raw or "").strip().upper()
    if value in _PASS:
        return "PASS"
    if value in _FAIL:
        return "FAIL"
    return "PENDING" if value in {"PENDING", "QUEUED", "IN_PROGRESS", "NOT_RUN", "REQUESTED", "WAITING"} else "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SideResult:
    """One check identity's result on one side."""

    identity: str
    name: str
    status: str
    source: str = "provider_ci"
    url: str = ""


@dataclass(frozen=True, slots=True)
class BehaviorOutcome:
    """A behavior key's observed outcome digest on each side (``""``: not observed)."""

    key: str
    head: str
    base: str
    merge: str


@dataclass(frozen=True, slots=True)
class MergeResultFacts:
    """The merge result as the composition observed it; ``target_sha`` empty = not built yet."""

    required: bool = False
    target_kind: str = ""  # merge_group | test_merge
    target_sha: str = ""
    base_sha: str = ""
    head_sha: str = ""
    queue_state: str = ""
    merge: tuple[SideResult, ...] = ()
    head: tuple[SideResult, ...] = ()
    base: tuple[SideResult, ...] = ()
    behavior: tuple[BehaviorOutcome, ...] = ()
    error: str = ""
    required_checks: tuple[tuple[str, str], ...] = ()
    """``(identity, producer class)`` pairs policy requires to pass on the merge side."""


@dataclass(frozen=True, slots=True)
class RequiredOutcome:
    """Typed required checks on the merge side: failed, and not (yet) satisfied (missing, mistyped, running)."""

    failed: tuple[str, ...] = ()
    unsatisfied: tuple[str, ...] = ()


def required_outcome(facts: MergeResultFacts) -> RequiredOutcome:
    """Each required ``(identity, class)`` against the merge side; only a row of that class counts."""

    merge = {row.identity: row for row in facts.merge[:MAX_CHECKS]}
    failed: list[str] = []
    unsatisfied: list[str] = []
    for identity, check_class in facts.required_checks:
        row = merge.get(identity)
        label = f"{identity} ({check_class})"
        if row is None or row.source != check_class:
            unsatisfied.append(label)  # missing, or reported by another producer: never a pass
        elif row.status == "FAIL":
            failed.append(label)
        elif row.status != "PASS":
            unsatisfied.append(label)
    return RequiredOutcome(failed=tuple(failed), unsatisfied=tuple(unsatisfied))


@dataclass(frozen=True, slots=True)
class Classification:
    new_failures: tuple[SideResult, ...]
    inherited: tuple[SideResult, ...]
    head_failures: tuple[SideResult, ...]
    pending: tuple[SideResult, ...]
    passed: tuple[SideResult, ...]
    conflicts: tuple[BehaviorOutcome, ...]
    over_bound: bool = False


def side_results(rows: Iterable[Mapping[str, Any]], *, source: str = "provider_ci") -> tuple[SideResult, ...]:
    """Normalize ``{identity|stable_key, name, status|verification, url}`` rows; the newest per identity wins."""

    out: dict[str, SideResult] = {}
    for row in rows:
        identity = str(row.get("identity") or row.get("stable_key") or row.get("name") or "").strip()
        if not identity:
            continue
        out[identity] = SideResult(
            identity=identity,
            name=str(row.get("name") or identity),
            status=_status(str(row.get("status") or row.get("verification") or "")),
            source=str(row.get("source") or source),
            url=str(row.get("url") or ""),
        )
    return tuple(out[key] for key in sorted(out))


def classify(facts: MergeResultFacts) -> Classification:
    """Per-identity three-way comparison; bounded by :data:`MAX_CHECKS` per side."""

    over = any(len(side) > MAX_CHECKS for side in (facts.merge, facts.head, facts.base))
    head = {row.identity: row for row in facts.head[:MAX_CHECKS]}
    base = {row.identity: row for row in facts.base[:MAX_CHECKS]}
    new_failures: list[SideResult] = []
    inherited: list[SideResult] = []
    head_failures: list[SideResult] = []
    pending: list[SideResult] = []
    passed: list[SideResult] = []
    for row in facts.merge[:MAX_CHECKS]:
        if row.status == "PASS":
            passed.append(row)
        elif row.status == "FAIL":
            on_base = base.get(row.identity)
            on_head = head.get(row.identity)
            if on_base is not None and on_base.status == "FAIL":
                inherited.append(row)
            elif on_head is not None and on_head.status == "FAIL":
                head_failures.append(row)  # already failing on the reviewed head: readiness shows it there
            else:
                # Passing (or never run) on both sides, failing once merged.
                new_failures.append(row)
        else:
            pending.append(row)
    conflicts = tuple(
        item
        for item in facts.behavior
        if item.merge and item.head and item.base and item.merge not in {item.head, item.base}
    )
    return Classification(
        new_failures=tuple(new_failures),
        inherited=tuple(inherited),
        head_failures=tuple(head_failures),
        pending=tuple(pending),
        passed=tuple(passed),
        conflicts=conflicts,
        over_bound=over,
    )


def _names(rows: Sequence[SideResult]) -> str:
    shown = ", ".join(row.name for row in rows[:MAX_NAMED])
    return shown + (f" and {len(rows) - MAX_NAMED} more" if len(rows) > MAX_NAMED else "")


def _where(facts: MergeResultFacts) -> str:
    kind = "merge queue group" if facts.target_kind == "merge_group" else "merge result"
    return f"{kind} {facts.target_sha[:12]}"


def merge_result_condition(facts: MergeResultFacts) -> ReadinessCondition | None:
    """The ``merge-result`` condition, or ``None`` when nothing is required or observed."""

    extra: dict[str, Any] = {
        "target_kind": facts.target_kind,
        "target_sha": facts.target_sha,
        "queue_state": facts.queue_state,
    }
    if facts.error:
        return ReadinessCondition(
            id=CONDITION_ID,
            label="Merge result",
            state="unknown",
            detail=f"The merge result could not be read: {facts.error[:200]}",
            category="landing",
            reason="evaluation_error",
            extra=extra,
        )
    if not facts.target_sha:
        if not (facts.required or facts.required_checks):
            return None
        return ReadinessCondition(
            id=CONDITION_ID,
            label="Merge result",
            state="pending",
            detail=(
                "Policy requires the checks to pass on the merge result; the provider builds it once landing is "
                "armed (merge queue), and landing waits for it."
            ),
            owner="scm",
            category="landing",
            reason="awaiting_merge_result",
            extra=extra,
        )
    result = classify(facts)
    required = required_outcome(facts)
    extra.update(
        {
            "new_failures": [row.identity for row in result.new_failures],
            "inherited_failures": [row.identity for row in result.inherited],
            "semantic_conflicts": [item.key for item in result.conflicts],
        }
    )
    if facts.required_checks:
        extra.update(
            {
                "required_checks": [{"check": check, "class": kind} for check, kind in facts.required_checks],
                "required_failed": list(required.failed),
                "required_unsatisfied": list(required.unsatisfied),
            }
        )
    if result.new_failures or result.conflicts or required.failed:
        parts = []
        if required.failed:
            parts.append(
                f"{len(required.failed)} required check(s) fail on the {_where(facts)}: "
                + ", ".join(required.failed[:MAX_NAMED])
            )
        if result.new_failures:
            parts.append(
                f"{len(result.new_failures)} check(s) pass on the reviewed head but fail on the "
                f"{_where(facts)}: {_names(result.new_failures)}"
            )
        if result.conflicts:
            keys = ", ".join(item.key for item in result.conflicts[:MAX_NAMED])
            parts.append(f"{len(result.conflicts)} behavior(s) differ from both sides once merged: {keys}")
        return ReadinessCondition(
            id=CONDITION_ID,
            label="Merge result",
            state="fail",
            detail="; ".join(parts) + ". Landing is blocked; the reviewed revision's judgments are unchanged.",
            category="landing",
            reason=(
                "merge_conflict"
                if result.conflicts and not (result.new_failures or required.failed)
                else "merge_failure"
            ),
            extra=extra,
        )
    if result.over_bound:
        return ReadinessCondition(
            id=CONDITION_ID,
            label="Merge result",
            state="unknown",
            detail=f"More than {MAX_CHECKS} checks on one side; the merge result was not compared in full.",
            category="landing",
            reason="over_bound",
            extra=extra,
        )
    if required.unsatisfied:
        return ReadinessCondition(
            id=CONDITION_ID,
            label="Merge result",
            state="pending",
            detail=(
                f"{len(required.unsatisfied)} required check(s) have not passed on the {_where(facts)} from "
                f"their required producer: {', '.join(required.unsatisfied[:MAX_NAMED])}."
            ),
            owner="scm",
            category="landing",
            reason="awaiting_required_checks",
            extra=extra,
        )
    if result.pending or not (result.passed or result.inherited or result.head_failures):
        return ReadinessCondition(
            id=CONDITION_ID,
            label="Merge result",
            state="pending",
            detail=f"Checks on the {_where(facts)} are still running ({len(result.pending)} pending).",
            owner="scm",
            category="landing",
            reason="awaiting_merge_result",
            extra=extra,
        )
    inherited = (
        f" {len(result.inherited)} failure(s) are inherited from the base ({_names(result.inherited)})."
        if result.inherited
        else ""
    )
    return ReadinessCondition(
        id=CONDITION_ID,
        label="Merge result",
        state="pass",
        detail=f"No new failure on the {_where(facts)}.{inherited}",
        category="landing",
        reason="satisfied",
        extra=extra,
    )


def evaluate(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
    facts = inputs.fact(FACT_KEY)
    if not isinstance(facts, MergeResultFacts):
        return ()
    condition = merge_result_condition(facts)
    return () if condition is None else (condition,)


def status_for(readiness: Mapping[str, Any]) -> str:
    """The merge-result condition's state in a readiness wire (``""`` when absent)."""

    for row in readiness.get("conditions") or ():
        if isinstance(row, Mapping) and row.get("id") == CONDITION_ID:
            return str(row.get("state") or "")
    return ""


__all__ = [
    "CONDITION_ID",
    "CONDITION_ORDER",
    "FACT_KEY",
    "MAX_CHECKS",
    "BehaviorOutcome",
    "Classification",
    "MergeResultFacts",
    "RequiredOutcome",
    "SideResult",
    "classify",
    "evaluate",
    "merge_result_condition",
    "required_outcome",
    "side_results",
    "status_for",
]
