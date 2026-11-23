"""Fast lane on eligible pushes (Phase 11 P11-FED item 4).

On a push that is eligible, the effective run plan keeps every deterministic
check and cheap rule-specific entry and skips only the broad deep-agent pass
(:data:`FAST_LANE_SKIPPED`), visibly, with the reason on the entry and the
decision on the plan. Eligibility is conservative -- any doubt keeps the full
plan:

* the Snapshot came from a push, and the previous Snapshot's persisted plan ran
  the broad AI pass (so the change was already deep-reviewed once, and two
  consecutive pushes are never both fast-laned);
* the depth is a Quick/Auto preset (Custom, profiles, Thorough and Deep ask for
  more and are never fast-laned), and the AI family is not ``always``;
* no escalation signal (risk candidates, or a P11-ESC deep-review escalation)
  and no policy depth floor (``min_effort``, which criticality policy compiles
  to) applies to the change;
* no changed path is sensitive (:data:`SENSITIVE_PATHS`: auth/security/crypto,
  secrets, billing, migrations, CI/release, Review policy, infrastructure and
  dependency manifests).

Policy-required and per-Review requested entries always keep running. Pure:
no store, no I/O.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.check_ingest import IngestedSatisfaction

#: The broad deep-agent pass the fast lane does not run.
FAST_LANE_SKIPPED: Final[tuple[str, ...]] = ("ai.investigators", "ai.hypotheses")
FAST_LANE_PRESETS: Final[frozenset[str]] = frozenset({"quick", "auto"})
#: Changed paths that always keep the full plan (matched case-insensitively).
SENSITIVE_PATHS: Final[tuple[str, ...]] = (
    "*auth*",
    "*security*",
    "*secret*",
    "*crypt*",
    "*password*",
    "*permission*",
    "*token*",
    "*billing*",
    "*payment*",
    "*migration*",
    ".github/*",
    ".gitlab-ci.yml",
    ".circleci/*",
    "jenkinsfile",
    ".lemoncrow/*",
    "*.tf",
    "*.tfvars",
    "*dockerfile*",
    "*docker-compose*",
    "*helm/*",
    "*k8s/*",
    "*package.json",
    "*package-lock.json",
    "*pnpm-lock.yaml",
    "*yarn.lock",
    "*requirements*.txt",
    "*pyproject.toml",
    "*uv.lock",
    "*poetry.lock",
    "*cargo.toml",
    "*cargo.lock",
    "*go.mod",
    "*go.sum",
    "*gemfile*",
)


@dataclass(frozen=True, slots=True)
class FastLaneSignals:
    """What the plan service observed about the previous Snapshot (part of the plan inputs)."""

    prior_snapshot_id: str
    prior_broad_pass: bool

    def digest_document(self) -> dict[str, Any]:
        return {"prior_snapshot_id": self.prior_snapshot_id, "prior_broad_pass": self.prior_broad_pass}


@dataclass(frozen=True, slots=True)
class FastLaneDecision:
    eligible: bool
    reasons: tuple[str, ...]
    skipped: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": list(self.reasons), "skipped": list(self.skipped)}

    @classmethod
    def from_wire(cls, raw: Any) -> FastLaneDecision | None:
        if not isinstance(raw, dict):
            return None
        return cls(
            eligible=raw.get("eligible") is True,
            reasons=tuple(str(item) for item in raw.get("reasons") or ()),
            skipped=tuple(str(item) for item in raw.get("skipped") or ()),
        )


def sensitive_paths(changed_paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        path for path in changed_paths if any(fnmatch.fnmatchcase(path.lower(), pattern) for pattern in SENSITIVE_PATHS)
    )


def fast_lane_decision(
    *,
    trigger: str,
    selection_source: str,
    base: str,
    ai_mode: str,
    ai_candidates: int,
    risk_signal: bool,
    changed_paths: Sequence[str],
    depth_floor: bool,
    signals: FastLaneSignals | None,
    escalated: Sequence[str] = (),
) -> FastLaneDecision:
    """Whether this Snapshot's plan may skip the broad AI pass; every blocker is named."""

    if trigger != "push":
        return FastLaneDecision(False, ("the Review opened on this revision",))
    blockers: list[str] = []
    if signals is None or not signals.prior_snapshot_id:
        blockers.append("no earlier revision plan to build on")
    elif not signals.prior_broad_pass:
        blockers.append("the previous revision's plan did not run the broad AI pass")
    if selection_source != "preset" or base not in FAST_LANE_PRESETS:
        blockers.append(f"depth {base} ({selection_source}) asks for the full plan")
    if ai_mode == "always":
        blockers.append("the AI family is set to always")
    if risk_signal or ai_candidates:
        blockers.append(f"escalated: {ai_candidates} risk candidate(s)")
    if depth_floor:
        blockers.append("a policy depth floor applies to changed paths")
    if escalated:
        # P11-ESC: deterministic signals escalated deep review on this Change.
        blockers.append("deep review escalated: " + "; ".join(escalated))
    sensitive = sensitive_paths(changed_paths)
    if sensitive:
        shown = ", ".join(sensitive[:3]) + (f" and {len(sensitive) - 3} more" if len(sensitive) > 3 else "")
        blockers.append(f"sensitive paths changed: {shown}")
    if blockers:
        return FastLaneDecision(False, tuple(blockers))
    return FastLaneDecision(
        True,
        (
            "push with no risk signal, sensitive path or depth floor; the previous revision ran the broad AI "
            "pass, so deterministic checks run and the broad AI pass waits for escalation",
        ),
    )


_FAST_LANE_REASON: Final[str] = (
    "fast lane: deterministic checks run on this push; the broad AI pass runs when policy or signals escalate, "
    "or on request"
)


def _running_dependents(drafts: Mapping[str, Any], entry_id: str, excluding: frozenset[str] = frozenset()) -> list[str]:
    return sorted(
        other.catalog.id
        for other in drafts.values()
        if other.status == "run" and other.catalog.id not in excluding and entry_id in other.catalog.dependencies
    )


def apply_federation(
    drafts: Mapping[str, Any],
    *,
    trigger: str,
    selection_source: str,
    base: str,
    ai_mode: str,
    ai_candidates: int,
    risk_signal: bool,
    changed_paths: Sequence[str],
    depth_floor: bool,
    signals: FastLaneSignals | None,
    ingested: Sequence[IngestedSatisfaction],
    escalated: Sequence[str] = (),
    equivalence_disabled: bool = False,
    revision_id: str = "",
) -> dict[str, Any]:
    """Apply the push fast lane and ingestion-over-rerun to the planner's drafts.

    Runs after statuses and dependencies resolve and before budget, mutating
    each draft's ``status``/``reason``/``satisfied_by``. Returns the fast-lane
    decision for display (empty when the Snapshot did not come from a push).

    * Fast lane: an eligible push skips only :data:`FAST_LANE_SKIPPED`;
      required and requested entries, and anything running work needs, stay.
    * Ingestion: an entry the trusted base maps to an equivalent current-Snapshot
      result becomes ``satisfied`` (origin on ``satisfied_by``) instead of
      running; an entry a running dependent needs to execute keeps running.
      With ``equivalence_disabled`` (the Trust Gate T-J kill switch) nothing is
      satisfied: each entry keeps its status and its reason says why.
    * Each decision on an equivalent result is also recorded on the draft's
      ``equivalence`` as a :mod:`validity` result (``equivalent_result``,
      ``equivalence_disabled`` or ``fresh_run_requested``), which the run plan's
      re-verification summary, the check-run report and telemetry carry.
    """

    from lemoncrow.pro.capabilities.review.validity import equivalence_result

    def explain(draft: Any, code: str, evidence_id: str, detail: str) -> None:
        draft.equivalence = equivalence_result(
            (code,), subject_ref=f"entry:{draft.catalog.id}", evidence_id=evidence_id, current_revision=revision_id
        ).to_wire()
        draft.equivalence["detail"] = detail

    fast_lane: dict[str, Any] = {}
    if trigger == "push":
        decision = fast_lane_decision(
            trigger=trigger,
            selection_source=selection_source,
            base=base,
            ai_mode=ai_mode,
            ai_candidates=ai_candidates,
            risk_signal=risk_signal,
            changed_paths=changed_paths,
            depth_floor=depth_floor,
            signals=signals,
            escalated=escalated,
        )
        lane: set[str] = set()
        if decision.eligible:
            lane = {
                entry_id
                for entry_id in FAST_LANE_SKIPPED
                if (draft := drafts.get(entry_id)) is not None
                and draft.status == "run"
                and not draft.required
                and not draft.manual
            }
            pruned = True
            while pruned:
                pruned = False
                for entry_id in sorted(lane):
                    if _running_dependents(drafts, entry_id, frozenset(lane)):
                        lane.discard(entry_id)
                        pruned = True
            for entry_id in sorted(lane):
                drafts[entry_id].status = "skipped"
                drafts[entry_id].reason = _FAST_LANE_REASON
        fast_lane = {**decision.to_wire(), "skipped": sorted(lane)}

    satisfactions = {row.entry_id: row for row in ingested}
    for draft in drafts.values():
        satisfaction = satisfactions.get(draft.catalog.id)
        if satisfaction is None or draft.status not in {"run", "not_available", "on_request"}:
            continue
        origin = "provider CI" if satisfaction.attestation_class == "provider_ci" else "LemonCrow verification"
        result = (
            f"{origin} {satisfaction.title or satisfaction.check_key} {satisfaction.status} on head "
            f"{satisfaction.head_sha[:12]}"
        )
        if equivalence_disabled:
            clause = f"an equivalent result exists ({result}), but CI equivalence is turned off on this server"
            draft.reason = f"{draft.reason}; {clause}" if draft.reason else clause
            explain(draft, "equivalence_disabled", satisfaction.evidence_id, clause)
            continue
        if getattr(draft, "fresh", False):
            # An explicit fresh run: the reviewer asked to execute despite the equivalent
            # result. An entry that cannot run keeps its own reason.
            if draft.status == "run":
                draft.reason = f"fresh run requested; an equivalent result exists ({result}) and is not reused"
                explain(draft, "fresh_run_requested", satisfaction.evidence_id, draft.reason)
            continue
        needed_by = _running_dependents(drafts, draft.catalog.id) if draft.status == "run" else []
        if needed_by:
            draft.reason = f"an equivalent result exists ({result}), but {', '.join(needed_by)} needs this run"
            continue
        draft.status = "satisfied"
        draft.satisfied_by = satisfaction.to_wire()
        draft.reason = f"satisfied by {result}; not re-run"
        explain(draft, "equivalent_result", satisfaction.evidence_id, draft.reason)
    return fast_lane


__all__ = [
    "FAST_LANE_PRESETS",
    "FAST_LANE_SKIPPED",
    "SENSITIVE_PATHS",
    "FastLaneDecision",
    "FastLaneSignals",
    "apply_federation",
    "fast_lane_decision",
    "sensitive_paths",
]
