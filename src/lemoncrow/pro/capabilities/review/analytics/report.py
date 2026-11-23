"""Review analytics: aggregates over per-Review facts, each drillable to source records (Phase 13 items 1-8).

Every number on the wire is computed from a drill index: an aggregate is the
count (or the sum of ``value``) of the :class:`~.facts.SourceRef` rows filed
under its ``drill`` key, so the drill-down for any aggregate re-adds to it by
construction (acceptance: "every aggregate can be traced back to source Review
records"). Ratios name the drill keys of their numerator and denominator.

What this never does:

* gate anything -- the report says ``"gating": "never"``; nothing in readiness,
  landing, auto-approval, priority or the Inbox imports this package;
* score or rank people -- volume is by author *kind* (human/agent), waiting is
  by owner *side* (reviewer, author, agent, checks, decision) and team, and no
  per-person figure exists;
* count stale or retracted findings as current -- they appear only under
  ``invalidated``;
* blur who pays -- local/customer compute and spend are separate from
  LemonCrow-managed spend, and different currencies are never added.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from lemoncrow.pro.capabilities.review.analytics.facts import OPTIONAL_SOURCES, FindingFact, ReviewFacts, SourceRef
from lemoncrow.pro.capabilities.review.analytics.scope import MAX_REVIEWS, AnalyticsScope
from lemoncrow.pro.capabilities.review.freshness import format_timestamp, parse_timestamp

REPORT_VERSION: Final[int] = 1
#: Judged findings (acted on + dismissed) a rule or check needs before it is called signal or noise.
MIN_JUDGED: Final[int] = 5
#: Acted-on share of judged findings at or above which a rule or check reads as signal.
SIGNAL_RATE: Final[float] = 0.5
DRILL_LIMIT: Final[int] = 500
DRIFT_ITEMS: Final[int] = 50
#: Production escapes counted; an incident link is correlation, never an escape.
ESCAPE_KINDS: Final[frozenset[str]] = frozenset({"contradicted", "deployment_failed", "deployment_rolled_back"})
_OUTCOME_LABELS: Final[Mapping[str, str]] = {
    "acted_on": "Acted on",
    "dismissed": "Dismissed",
    "open": "Open",
}
_INVALIDATED_LABELS: Final[Mapping[str, str]] = {"stale": "Stale", "retracted": "Retracted"}
_AI_REVIEW_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {"finding_investigation", "hypothesis_generation", "finding_dedupe", "rule_check"}
)
_COST_CLASS_LABELS: Final[Mapping[str, str]] = {
    "customer": "Your compute and provider accounts",
    "managed": "LemonCrow-managed",
    "unattributed": "Not attributed",
}
_JUDGMENT_LABELS: Final[Mapping[str, str]] = {
    "accepted": "Accepted by a reviewer",
    "change_requested": "Change requested",
    "not_judged": "Not judged",
}


# --------------------------------------------------------------------------- #
# drill index
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DrillIndex:
    """Aggregate key -> the source records it counted."""

    rows: dict[str, list[SourceRef]] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    def add(self, key: str, ref: SourceRef, *, label: str = "") -> None:
        self.rows.setdefault(key, []).append(ref)
        if label and key not in self.labels:
            self.labels[key] = label

    def touch(self, key: str, *, label: str = "") -> None:
        self.rows.setdefault(key, [])
        if label and key not in self.labels:
            self.labels[key] = label

    def refs(self, key: str) -> list[SourceRef]:
        return self.rows.get(key, [])

    def count(self, key: str) -> int:
        return len(self.refs(key))

    def total(self, key: str) -> float:
        return math.fsum(ref.value for ref in self.refs(key))

    def reviews(self, key: str) -> int:
        return len({(ref.repo_id, ref.review_id) for ref in self.refs(key)})

    def row(self, key: str, label: str = "", *, name: str = "", summed: bool = False, **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": name or key,
            "label": label or self.labels.get(key, key),
            "count": self.count(key),
            "reviews": self.reviews(key),
            "drill": key,
        }
        if summed:
            out["value"] = round(self.total(key), 6)
        out.update(extra)
        return out

    def prefixed(self, prefix: str) -> list[str]:
        return sorted(key for key in self.rows if key.startswith(prefix))

    def under(self, prefix: str, *, summed: bool = False, by_count: bool = False) -> list[dict[str, Any]]:
        """One row per key under *prefix*, named by the rest of the key (which may hold dots)."""

        rows = [self.row(key, name=key[len(prefix) :], summed=summed) for key in self.prefixed(prefix)]
        if by_count:
            rows.sort(key=lambda row: (-row["count"], row["key"]))
        return rows


def _hours(start: str, end: str) -> float | None:
    first, last = parse_timestamp(start), parse_timestamp(end)
    if first is None or last is None:
        return None
    return max(0.0, (last - first).total_seconds() / 3600.0)


def percentile(values: Sequence[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return round(ordered[rank - 1], 3)


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator <= 0 else round(numerator / denominator, 6)


def _signal(acted: int, dismissed: int) -> str:
    judged = acted + dismissed
    if judged < MIN_JUDGED:
        return "insufficient_data"
    return "signal" if acted / judged >= SIGNAL_RATE else "noise"


def _period_start(instant: datetime, granularity: str) -> str:
    day = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "month":
        return day.replace(day=1).strftime("%Y-%m-%d")
    return (day - timedelta(days=day.weekday())).strftime("%Y-%m-%d")


def _period_label(start: str, granularity: str) -> str:
    """A period row's name for people: "Week of 16 Nov 2026" or "November 2026", never the drill key."""

    day = datetime.strptime(start, "%Y-%m-%d")
    if granularity == "month":
        return f"{day:%B %Y}"
    return f"Week of {day.day} {day:%b %Y}"


# --------------------------------------------------------------------------- #
# building
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AnalyticsReport:
    wire: dict[str, Any]
    index: DrillIndex
    reviews: Mapping[tuple[str, str], ReviewFacts]

    def drill(self, key: str, *, limit: int = DRILL_LIMIT) -> dict[str, Any]:
        """The source records one aggregate counted (exact ``total``; at most *limit* rows listed)."""

        refs = sorted(
            self.index.refs(key), key=lambda ref: (ref.at, ref.repo_id, ref.review_id, ref.record_id), reverse=True
        )
        records = []
        for ref in refs[:limit]:
            facts = self.reviews.get((ref.repo_id, ref.review_id))
            records.append(
                {
                    **ref.to_wire(),
                    "review_title": "" if facts is None else facts.title,
                    "repo_name": ref.repo_id if facts is None else facts.repo_name,
                    "review_path": f"/r/{ref.review_id}",
                }
            )
        return {
            "key": key,
            "label": self.index.labels.get(key, key),
            "known": key in self.index.rows,
            "count": len(refs),
            "value": round(self.index.total(key), 6),
            "reviews": self.index.reviews(key),
            "records": records,
            "limit": limit,
            "truncated": len(refs) > limit,
        }


def _finding_selected(finding: FindingFact, scope: AnalyticsScope) -> bool:
    return not scope.paths or (bool(finding.path) and scope.path_selected(finding.path))


def build_report(
    facts: Sequence[ReviewFacts],
    *,
    scope: AnalyticsScope,
    now: datetime,
    candidates: int,
    analyzed: int,
    waiting_by_owner: Sequence[Mapping[str, Any]] = (),
    reviewer_load: Mapping[str, Any] | None = None,
    load_index: DrillIndex | None = None,
    escapes_truncated: bool = False,
    scan_truncated: bool = False,
    notes: Sequence[str] = (),
    unavailable: Iterable[str] = (),
) -> AnalyticsReport:
    """Aggregate already-scoped Review facts into the analytics wire.

    ``unavailable`` names optional sources this engine does not ship; the wire lists
    each with the sections it would have fed, so none reads as a measured zero.
    """

    missing = set(unavailable)

    index = load_index if load_index is not None else DrillIndex()
    by_review = {(item.repo_id, item.review_id): item for item in facts}

    # -- volume ------------------------------------------------------------- #
    index.touch("volume.reviews", label="Reviews")
    for item in facts:
        ref = item.ref("review", item.review_id, at=item.created_at, label=item.title or "Review")
        index.add("volume.reviews", ref)
        index.add(f"volume.status.{item.status}", ref, label=item.status.capitalize())
        index.add(f"volume.author.{item.actor_type}", ref, label=f"{item.actor_type.capitalize()} authored")
        index.add(f"volume.repo.{item.repo_id}", ref, label=item.repo_name)

    # -- time to first human review, merge time ----------------------------- #
    index.touch("first_review.measured", label="First human review")
    index.touch("first_review.awaiting", label="Awaiting a first human review")
    index.touch("merge.merged", label="Merged")
    for item in facts:
        if item.first_judgment is not None:
            hours = _hours(item.created_at, item.first_judgment.at)
            if hours is not None:
                judged = item.first_judgment
                index.add(
                    "first_review.measured",
                    SourceRef(
                        item.review_id,
                        item.repo_id,
                        judged.record_kind,
                        judged.record_id,
                        judged.revision_id,
                        judged.at,
                        judged.label,
                        hours,
                    ),
                )
        elif item.status == "open":
            index.add("first_review.awaiting", item.ref("review", item.review_id, at=item.created_at, label=item.title))
        if item.merged is not None:
            hours = _hours(item.created_at, item.merged.at)
            if hours is not None:
                merged = item.merged
                index.add(
                    "merge.merged",
                    SourceRef(
                        item.review_id,
                        item.repo_id,
                        merged.record_kind,
                        merged.record_id,
                        merged.revision_id,
                        merged.at,
                        merged.label,
                        hours,
                    ),
                )

    # -- findings ----------------------------------------------------------- #
    index.touch("findings.current", label="Current findings")
    index.touch("catch.useful", label="Useful findings (acted on)")
    for source in ("deterministic", "ai"):
        index.touch(f"catch.{source}", label="Deterministic checks" if source == "deterministic" else "AI review")
    for outcome, label in _OUTCOME_LABELS.items():
        index.touch(f"findings.outcome.{outcome}", label=label)
    for basis, label in _INVALIDATED_LABELS.items():
        index.touch(f"findings.invalidated.{basis}", label=label)
    rule_rows: dict[str, dict[str, str]] = {}
    check_rows: dict[str, dict[str, str]] = {}
    for item in facts:
        for finding in item.findings:
            if not _finding_selected(finding, scope):
                continue
            ref = item.ref(
                "ai_finding" if finding.source == "ai" else "deterministic_finding",
                finding.record_id,
                revision_id=finding.revision_id,
                at=finding.created_at,
                label=finding.title or finding.category,
            )
            if finding.outcome == "invalidated":
                # Stale or retracted: counted only here, never as a current finding.
                index.add(
                    f"findings.invalidated.{finding.basis}",
                    ref,
                    label=_INVALIDATED_LABELS.get(finding.basis, finding.basis),
                )
                continue
            index.add("findings.current", ref)
            index.add(
                f"findings.source.{finding.source}",
                ref,
                label="AI review" if finding.source == "ai" else "Deterministic checks",
            )
            index.add(f"findings.category.{finding.category}", ref, label=finding.category.replace("_", " "))
            index.add(
                f"findings.severity.{finding.severity}",
                ref,
                label=finding.severity.upper() if finding.severity.startswith("p") else finding.severity.capitalize(),
            )
            index.add(f"findings.outcome.{finding.outcome}", ref)
            if finding.outcome == "acted_on":
                index.add("catch.useful", ref)
                index.add(f"catch.{finding.source}", ref)
            for rule in finding.rules:
                rule_rows.setdefault(
                    rule,
                    {"kind": "ai_rule" if finding.source == "ai" else "deterministic_rule", "check": finding.check},
                )
                index.add(f"rule.{rule}.hits", ref, label=rule)
                if finding.outcome in ("acted_on", "dismissed"):
                    index.add(f"rule.{rule}.{finding.outcome}", ref, label=rule)
                if finding.outcome == "dismissed" and finding.dismissal_reason == "incorrect":
                    index.add(f"rule.{rule}.false_positive", ref, label=rule)
            check = finding.check
            if check:
                check_rows.setdefault(check, {"kind": "ai_review" if finding.source == "ai" else "deterministic_check"})
                index.add(f"check.{check}.hits", ref, label=check)
                if finding.outcome in ("acted_on", "dismissed"):
                    index.add(f"check.{check}.{finding.outcome}", ref, label=check)
        for rule in item.rule_asks:
            rule_rows.setdefault(rule, {"kind": "ai_rule", "check": "ai_review"})
            index.add(
                f"rule.{rule}.asked",
                item.ref(
                    "run_plan",
                    item.latest_revision_id,
                    revision_id=item.latest_revision_id,
                    at=item.latest_revision_at,
                    label="Rule asked",
                ),
                label=rule,
            )

    def rule_wire(rule: str, meta: Mapping[str, str]) -> dict[str, Any]:
        acted, dismissed = index.count(f"rule.{rule}.acted_on"), index.count(f"rule.{rule}.dismissed")
        hits = index.count(f"rule.{rule}.hits")
        for suffix in ("hits", "acted_on", "dismissed", "false_positive", "asked"):
            index.touch(f"rule.{rule}.{suffix}", label=rule)
        tool = rule.split(":", 1)[0] if meta["kind"] == "deterministic_rule" else ""
        return {
            "rule": rule,
            "kind": meta["kind"],
            "tool": tool,
            "check": meta["check"],
            "asked": index.count(f"rule.{rule}.asked"),
            "hits": hits,
            "acted_on": acted,
            "dismissed": dismissed,
            "false_positive": index.count(f"rule.{rule}.false_positive"),
            "hit_rate": _ratio(index.reviews(f"rule.{rule}.hits"), index.count(f"rule.{rule}.asked")),
            "dismissal_rate": _ratio(dismissed, hits),
            "acted_on_rate": _ratio(acted, acted + dismissed),
            "signal": _signal(acted, dismissed),
            "drill": {
                suffix: f"rule.{rule}.{suffix}"
                for suffix in ("asked", "hits", "acted_on", "dismissed", "false_positive")
            },
        }

    def check_wire(check: str, meta: Mapping[str, str]) -> dict[str, Any]:
        acted, dismissed = index.count(f"check.{check}.acted_on"), index.count(f"check.{check}.dismissed")
        hits = index.count(f"check.{check}.hits")
        for suffix in ("hits", "acted_on", "dismissed"):
            index.touch(f"check.{check}.{suffix}", label=check)
        return {
            "check": check,
            "kind": meta["kind"],
            "hits": hits,
            "acted_on": acted,
            "dismissed": dismissed,
            "dismissal_rate": _ratio(dismissed, hits),
            "acted_on_rate": _ratio(acted, acted + dismissed),
            "signal": _signal(acted, dismissed),
            "drill": {suffix: f"check.{check}.{suffix}" for suffix in ("hits", "acted_on", "dismissed")},
        }

    rules = [rule_wire(rule, meta) for rule, meta in rule_rows.items()]
    rules.sort(key=lambda row: (-(row["dismissal_rate"] or 0.0), -row["hits"], row["rule"]))
    checks = [check_wire(check, meta) for check, meta in check_rows.items()]
    checks.sort(key=lambda row: (-(row["dismissal_rate"] or 0.0), -row["hits"], row["check"]))

    # -- auto-approval and overrides ----------------------------------------- #
    index.touch("auto.evaluated", label="Auto-approval evaluated")
    index.touch("auto.eligible", label="Eligible for auto-approval")
    index.touch("auto.not_eligible", label="Not eligible")
    index.touch("override.break_glass", label="Break-glass landings")
    gate_meta: dict[str, dict[str, Any]] = {}
    for item in facts:
        decision = item.auto_approval
        if isinstance(decision, Mapping):
            snapshot = str(decision.get("snapshot_id") or item.latest_revision_id)
            generation = str(decision.get("policy_generation") or "")
            ref = item.ref(
                "auto_approval_decision",
                str(decision.get("decision_digest") or snapshot),
                revision_id=snapshot,
                at=str(decision.get("evaluated_at") or ""),
                label="Auto-approval decision",
            )
            index.add("auto.evaluated", ref)
            index.add("auto.eligible" if decision.get("eligible") is True else "auto.not_eligible", ref)
            for gate in decision.get("gates") or ():
                if not isinstance(gate, Mapping) or gate.get("passed") is True:
                    continue
                gate_id, reason = str(gate.get("id") or ""), str(gate.get("reason") or "failed")
                key = f"auto.gate.{gate_id}.{reason}"
                index.add(key, ref, label=str(gate.get("label") or gate_id))
                meta = gate_meta.setdefault(
                    key,
                    {
                        "gate": gate_id,
                        "reason": reason,
                        "label": str(gate.get("label") or gate_id),
                        "generations": set(),
                    },
                )
                if generation:
                    meta["generations"].add(generation)
        for record in item.break_glass:
            ref = item.ref(
                "break_glass",
                str(record.get("id") or ""),
                revision_id=str(record.get("revision_id") or ""),
                at=str(record.get("created_at") or ""),
                label="Break-glass",
            )
            index.add("override.break_glass", ref)
            for bypassed in record.get("bypassed") or ():
                if isinstance(bypassed, Mapping):
                    condition = str(bypassed.get("id") or "")
                    index.add(f"override.bypassed.{condition}", ref, label=str(bypassed.get("label") or condition))
    gates = sorted(
        (
            {
                **index.row(key, meta["label"]),
                "gate": meta["gate"],
                "reason": meta["reason"],
                "policy_generations": sorted(meta["generations"])[:5],
                "policy_generation_count": len(meta["generations"]),
            }
            for key, meta in gate_meta.items()
        ),
        key=lambda row: (-row["count"], row["gate"], row["reason"]),
    )

    # -- depth and escalation ------------------------------------------------ #
    index.touch("escalation.escalated", label="Escalated to deep review")
    index.touch("escalation.standard", label="Not escalated")
    for item in facts:
        ref = item.ref(
            "run_plan",
            item.latest_revision_id,
            revision_id=item.latest_revision_id,
            at=item.latest_revision_at,
            label=item.depth or "unknown",
        )
        depth = item.depth or "unknown"
        index.add(f"depth.{depth}", ref, label=depth.capitalize())
        lane = "escalated" if item.escalation_state == "escalated" else "standard"
        index.add(f"escalation.{lane}", ref)
        for trigger in item.escalation_triggers if lane == "escalated" else ():
            index.add(f"escalation.trigger.{trigger}", ref, label=trigger.replace("_", " "))
        for finding in item.findings:
            if finding.source == "ai" and finding.outcome == "acted_on" and _finding_selected(finding, scope):
                index.add(
                    f"escalation.{lane}.useful_ai",
                    item.ref(
                        "ai_finding",
                        finding.record_id,
                        revision_id=finding.revision_id,
                        at=finding.created_at,
                        label=finding.title,
                    ),
                    label="Useful AI findings",
                )
        for usage in item.usage:
            ai_review_call = usage.record_kind == "intelligence_run" and usage.capability in _AI_REVIEW_CAPABILITIES
            if ai_review_call:
                index.add(
                    f"escalation.{lane}.ai_calls",
                    item.ref(
                        usage.record_kind,
                        usage.record_id,
                        revision_id=usage.revision_id,
                        at=usage.at,
                        label=usage.capability,
                        value=float(usage.calls),
                    ),
                    label="AI review model calls",
                )

    def lane_wire(lane: str) -> dict[str, Any]:
        for suffix in ("useful_ai", "ai_calls"):
            index.touch(f"escalation.{lane}.{suffix}")
        reviews = index.count(f"escalation.{lane}")
        useful = index.count(f"escalation.{lane}.useful_ai")
        calls = index.total(f"escalation.{lane}.ai_calls")
        return {
            "reviews": index.row(f"escalation.{lane}"),
            "useful_ai_findings": index.row(f"escalation.{lane}.useful_ai", "Useful AI findings"),
            "ai_model_calls": index.row(f"escalation.{lane}.ai_calls", "AI review model calls", summed=True),
            "useful_per_review": _ratio(useful, reviews),
            "model_calls_per_useful": _ratio(calls, useful),
        }

    # -- cost ---------------------------------------------------------------- #
    spend_currencies: dict[str, set[str]] = {}
    for item in facts:
        for usage in item.usage:
            ref = item.ref(
                usage.record_kind,
                usage.record_id,
                revision_id=usage.revision_id,
                at=usage.at,
                label=usage.capability,
                value=float(usage.calls),
            )
            index.add(
                f"cost.calls.{usage.cost_class}.{usage.locus}",
                ref,
                label=f"{_COST_CLASS_LABELS[usage.cost_class]} ({usage.locus})",
            )
            if usage.provider_cost is None:
                index.add(f"cost.unreported.{usage.cost_class}", ref, label="Model calls without a reported cost")
            else:
                currency = usage.currency or "unspecified"
                spend_currencies.setdefault(usage.cost_class, set()).add(currency)
                index.add(
                    f"cost.spend.{usage.cost_class}.{currency}",
                    item.ref(
                        usage.record_kind,
                        usage.record_id,
                        revision_id=usage.revision_id,
                        at=usage.at,
                        label=usage.capability,
                        value=usage.provider_cost,
                    ),
                    label=f"{_COST_CLASS_LABELS[usage.cost_class]} spend ({currency})",
                )
        for compute in item.compute:
            # Measured executions (the compute ledger) and durable-job time (lease spans) are
            # different measures of possibly the same work: reported side by side, never added.
            measure = "jobs" if compute.record_kind == "hosted_jobs" else "compute"
            index.add(
                f"cost.{measure}.{compute.cost_class}.{compute.executor}",
                item.ref(
                    compute.record_kind,
                    compute.record_id,
                    revision_id=compute.revision_id,
                    label=compute.stage,
                    value=compute.minutes,
                ),
                label=f"{_COST_CLASS_LABELS[compute.cost_class]} ({compute.executor})",
            )
        for saving in item.savings:
            index.add(
                f"reuse.{saving.reason}",
                item.ref(
                    "compute_saved", f"{saving.entry_id}|{saving.reason}", label=saving.entry_id, value=saving.minutes
                ),
                label=saving.reason.replace("_", " "),
            )

    def class_of(key: str) -> str:
        return key.split(".")[2]

    calls_rows = [
        index.row(key, name=key[11:], summed=True, cost_class=class_of(key), locus=key.split(".", 3)[3])
        for key in index.prefixed("cost.calls.")
    ]
    spend_rows = [
        index.row(key, name=key[11:], summed=True, cost_class=class_of(key), currency=key.split(".", 3)[3])
        for key in index.prefixed("cost.spend.")
    ]
    unreported_rows = [
        index.row(key, name=key[16:], summed=True, cost_class=class_of(key))
        for key in index.prefixed("cost.unreported.")
    ]
    compute_rows = [
        index.row(key, name=key[13:], summed=True, cost_class=class_of(key), executor=key.split(".", 3)[3])
        for key in index.prefixed("cost.compute.")
    ]
    job_rows = [
        index.row(key, name=key[10:], summed=True, cost_class=class_of(key), executor=key.split(".", 3)[3])
        for key in index.prefixed("cost.jobs.")
    ]
    useful_total = index.count("catch.useful")
    useful_ai = index.count("catch.ai")
    per_useful: list[dict[str, Any]] = []
    for row in spend_rows:
        per_useful.append(
            {
                "measure": "provider_spend",
                "cost_class": row["cost_class"],
                "currency": row["currency"],
                "per_useful_finding": _ratio(row["value"], useful_total),
                "numerator_drill": row["drill"],
                "denominator_drill": "catch.useful",
            }
        )
    for cost_class in sorted({row["cost_class"] for row in calls_rows}):
        keys = [row["drill"] for row in calls_rows if row["cost_class"] == cost_class]
        per_useful.append(
            {
                "measure": "model_calls",
                "cost_class": cost_class,
                "per_useful_finding": _ratio(sum(index.total(key) for key in keys), useful_ai),
                "numerator_drill": keys,
                "denominator_drill": "catch.ai",
            }
        )
    for measure, rows in (("compute_minutes", compute_rows), ("job_minutes", job_rows)):
        for cost_class in sorted({row["cost_class"] for row in rows}):
            keys = [row["drill"] for row in rows if row["cost_class"] == cost_class]
            per_useful.append(
                {
                    "measure": measure,
                    "cost_class": cost_class,
                    "per_useful_finding": _ratio(sum(index.total(key) for key in keys), useful_total),
                    "numerator_drill": keys,
                    "denominator_drill": "catch.useful",
                }
            )

    # -- correction loops ---------------------------------------------------- #
    for name, label in (
        ("started", "Fix requests started"),
        ("snapshot_recorded", "New revision recorded"),
        ("re_reviewed", "Re-reviewed after the fix"),
        ("in_flight", "Waiting for a new revision"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ):
        index.touch(f"loops.{name}", label=label)
    for item in facts:
        for action in item.actions:
            ref = item.ref(
                "review_action",
                action.action_id,
                revision_id=action.revision_id,
                at=action.created_at,
                label=action.kind,
            )
            index.add("loops.started", ref)
            if action.status == "snapshot_recorded":
                index.add("loops.snapshot_recorded", ref)
                if action.re_reviewed:
                    index.add("loops.re_reviewed", ref)
            elif action.status in ("failed", "cancelled"):
                index.add(f"loops.{action.status}", ref)
            else:
                index.add("loops.in_flight", ref)
    settled = index.count("loops.started") - index.count("loops.in_flight")

    # -- escaped defects (Phase 15 feed) -------------------------------------- #
    index.touch("escapes.total", label="Escaped defects")
    for item in facts:
        for escape in item.escapes:
            kind = str(getattr(escape, "kind", "") or "")
            if kind not in ESCAPE_KINDS:
                continue
            ref = item.ref(
                "escaped_defect",
                f"{kind}:{getattr(escape, 'environment', '')}:{getattr(escape, 'subject', '')}",
                revision_id=str(getattr(escape, "revision_id", "") or ""),
                at=str(getattr(escape, "observed_at", "") or ""),
                label=str(getattr(escape, "subject", "") or kind),
            )
            index.add("escapes.total", ref)
            index.add(f"escapes.kind.{kind}", ref, label=kind.replace("_", " "))
            depth = str(getattr(escape, "depth", "") or "unknown")
            index.add(f"escapes.depth.{depth}", ref, label=depth.capitalize())
            for check in getattr(escape, "checks", ()) or ():
                index.add(f"escapes.check.{check}", ref, label=str(check))

    # -- side-effect drift ---------------------------------------------------- #
    span = scope.until - scope.since
    granularity = "week" if span <= timedelta(days=92) else "month"
    drift_items: list[dict[str, Any]] = []
    index.touch("drift.changes", label="Side-effect register changes")
    for judgment, label in _JUDGMENT_LABELS.items():
        index.touch(f"drift.judgment.{judgment}", label=label)
    for item in facts:
        when = item.merged.at if item.merged is not None else item.latest_revision_at
        instant = parse_timestamp(when)
        for change in item.register:
            if scope.paths and not any(scope.path_selected(path) for path in change.paths):
                continue
            ref = item.ref(
                "side_effect", change.behavior_key, revision_id=change.revision_id, at=when, label=change.title
            )
            index.add("drift.changes", ref)
            index.add(f"drift.judgment.{change.judgment}", ref)
            index.add(f"drift.category.{change.category}", ref, label=change.category.replace("_", " "))
            index.add(f"drift.change.{change.change}", ref, label=change.change.capitalize())
            if instant is not None:
                start = _period_start(instant, granularity)
                index.add(f"drift.period.{start}", ref, label=_period_label(start, granularity))
            drift_items.append(
                {
                    "review_id": item.review_id,
                    "repo_id": item.repo_id,
                    "repo_name": item.repo_name,
                    "review_title": item.title,
                    "review_path": f"/r/{item.review_id}",
                    "revision_id": change.revision_id,
                    "merged": item.merged is not None,
                    "at": when,
                    "category": change.category,
                    "change": change.change,
                    "title": change.title,
                    "critical": change.critical,
                    "judgment": {
                        "state": change.judgment,
                        "label": _JUDGMENT_LABELS[change.judgment],
                        "by": change.judged_by,
                        "at": change.judged_at,
                        "event_id": change.judgment_event_id,
                    },
                }
            )
    drift_items.sort(key=lambda row: (str(row["at"]), row["review_id"], row["title"]), reverse=True)

    # -- wire ----------------------------------------------------------------- #
    first_values = [ref.value for ref in index.refs("first_review.measured")]
    merge_values = [ref.value for ref in index.refs("merge.merged")]
    current = index.count("findings.current")
    wire: dict[str, Any] = {
        "version": REPORT_VERSION,
        "gating": "never",
        "individuals": "not_reported",
        "generated_at": format_timestamp(now),
        "scope": scope.to_wire(),
        "bounds": {
            "max_reviews": MAX_REVIEWS,
            "candidates": candidates,
            "analyzed": analyzed,
            "in_scope": len(facts),
            "truncated": candidates > analyzed,
            "scan_truncated": scan_truncated,
            "escapes_truncated": escapes_truncated,
        },
        "notes": list(notes),
        "unavailable": [
            {"source": name, "label": source.label, "sections": list(source.sections)}
            for name, source in OPTIONAL_SOURCES.items()
            if name in missing
        ],
        "volume": {
            "reviews": index.row("volume.reviews"),
            "by_status": index.under("volume.status."),
            "by_author_kind": index.under("volume.author."),
            "by_repository": index.under("volume.repo."),
        },
        "time_to_first_review": {
            "measured": index.row("first_review.measured"),
            "awaiting": index.row("first_review.awaiting"),
            "median_hours": percentile(first_values, 0.5),
            "p90_hours": percentile(first_values, 0.9),
        },
        "merge_time": {
            "merged": index.row("merge.merged"),
            "median_hours": percentile(merge_values, 0.5),
            "p90_hours": percentile(merge_values, 0.9),
        },
        "findings": {
            "current": index.row("findings.current"),
            "by_source": index.under("findings.source."),
            "by_category": index.under("findings.category.", by_count=True),
            "by_severity": index.under("findings.severity."),
            "by_outcome": [index.row(f"findings.outcome.{name}", name=name) for name in _OUTCOME_LABELS],
            "invalidated": [index.row(f"findings.invalidated.{name}", name=name) for name in _INVALIDATED_LABELS],
            "basis": "Stale and retracted findings are counted only as invalidated, never as current findings.",
        },
        "catch_contribution": {
            "useful": index.row("catch.useful"),
            "deterministic": index.row(
                "catch.deterministic", share=_ratio(index.count("catch.deterministic"), useful_total)
            ),
            "ai": index.row("catch.ai", share=_ratio(useful_ai, useful_total)),
            "acted_on_rate": _ratio(useful_total, current),
        },
        "rules": rules,
        "checks": checks,
        "policy_effectiveness": {
            "min_judged": MIN_JUDGED,
            "signal_rate": SIGNAL_RATE,
            "signal": [row["rule"] for row in rules if row["signal"] == "signal"],
            "noise": [row["rule"] for row in rules if row["signal"] == "noise"],
            "checks_signal": [row["check"] for row in checks if row["signal"] == "signal"],
            "checks_noise": [row["check"] for row in checks if row["signal"] == "noise"],
            "basis": (
                f"A rule or check reads as signal when at least {int(SIGNAL_RATE * 100)}% of its judged findings "
                f"(acted on or dismissed, at least {MIN_JUDGED}) were acted on; it is advisory and gates nothing."
            ),
        },
        "auto_approval": {
            "evaluated": index.row("auto.evaluated"),
            "eligible": index.row("auto.eligible"),
            "not_eligible": index.row("auto.not_eligible"),
            "failed_gates": gates,
        },
        "overrides": {
            "break_glass": index.row("override.break_glass"),
            "bypassed": index.under("override.bypassed.", by_count=True),
        },
        "depth": {
            "distribution": index.under("depth."),
        },
        "escalation": {
            "escalated": lane_wire("escalated"),
            "standard": lane_wire("standard"),
            "triggers": index.under("escalation.trigger.", by_count=True),
        },
        "cost": {
            "model_calls": calls_rows,
            "provider_spend": spend_rows,
            "calls_without_cost": unreported_rows,
            "compute_minutes": compute_rows,
            "job_minutes": job_rows,
            "basis": (
                "Your compute and provider accounts (local machine, your workers, your provider keys) are reported "
                "separately from LemonCrow-managed spend; amounts in different currencies are never added. Model "
                "calls and spend come from each recorded model run; durable-job time is shown beside measured "
                "compute, never added to it."
            ),
        },
        "cache_reuse": index.under("reuse.", summed=True),
        "cost_per_useful_finding": per_useful,
        "correction_loops": {
            **{
                name: index.row(f"loops.{name}")
                for name in ("started", "snapshot_recorded", "re_reviewed", "in_flight", "failed", "cancelled")
            },
            "completion_rate": _ratio(index.count("loops.re_reviewed"), settled),
        },
        "escaped_defects": {
            "total": index.row("escapes.total"),
            "by_depth": index.under("escapes.depth."),
            "by_check": index.under("escapes.check.", by_count=True),
            "by_kind": index.under("escapes.kind."),
            "basis": "Production contradictions and failed or rolled-back production deployments of merged Changes; incident links are never counted.",
        },
        "drift": {
            "granularity": granularity,
            "changes": index.row("drift.changes"),
            "by_judgment": [index.row(f"drift.judgment.{name}", name=name) for name in _JUDGMENT_LABELS],
            "by_category": index.under("drift.category.", by_count=True),
            "by_change": index.under("drift.change."),
            "series": index.under("drift.period."),
            "items": drift_items[:DRIFT_ITEMS],
            "items_total": len(drift_items),
        },
        "waiting_by_owner": [dict(row) for row in waiting_by_owner],
        "reviewer_load": dict(reviewer_load or {}),
    }
    return AnalyticsReport(wire=wire, index=index, reviews=by_review)


def select_facts(facts: Iterable[ReviewFacts], scope: AnalyticsScope) -> list[ReviewFacts]:
    """Review-level team and path selection (repository, organization and period are applied before)."""

    out: list[ReviewFacts] = []
    for item in facts:
        if scope.teams and not (item.teams & set(scope.teams)):
            continue
        if scope.paths and not any(scope.path_selected(path) for path in item.paths):
            continue
        out.append(item)
    return out


__all__ = [
    "DRIFT_ITEMS",
    "DRILL_LIMIT",
    "ESCAPE_KINDS",
    "MIN_JUDGED",
    "REPORT_VERSION",
    "SIGNAL_RATE",
    "AnalyticsReport",
    "DrillIndex",
    "build_report",
    "percentile",
    "select_facts",
]
