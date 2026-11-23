"""Per-Review analytic facts, read from records the Review already keeps (Phase 13 items 1, 4, 7, 8).

Nothing here is stored and nothing here writes: every fact is derived on read
from durable Review records through :class:`ReadOnlyStore`, which refuses any
store method that is not a read. Analytics therefore cannot change readiness,
landing, priority or any other Review state, and need no retention of their own.

Every fact keeps the identity of the record it came from (a :class:`SourceRef`),
so each aggregate built from facts can be traced back to source Review records.

Finding outcomes (one per finding, human judgment first):

* ``dismissed`` -- a human dismissed it (AI disposition, or a static disposition);
* ``acted_on`` -- a human confirmed it, it was fixed with revalidation evidence,
  it was the subject of a code-changing Review action, a human resolved a
  LemonCrow finding, or (deterministic) a later Snapshot's run of the same
  check no longer reports it;
* ``invalidated`` -- ``stale`` (superseded on a later Snapshot, or its check has
  not reported since) or ``retracted`` (its anchor is orphaned or obsolete).
  Invalidated findings are counted only as invalidated: never in current
  totals, rates, catch contribution or cost per useful finding;
* ``open`` -- current and not judged.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

FindingSource = Literal["ai", "deterministic"]
FindingOutcome = Literal["open", "acted_on", "dismissed", "invalidated"]
FINDING_OUTCOMES: Final[tuple[str, ...]] = ("acted_on", "dismissed", "open", "invalidated")
CostClass = Literal["customer", "managed", "unattributed"]
#: Executor loci by who pays for the compute or model spend.
CUSTOMER_LOCI: Final[frozenset[str]] = frozenset({"local", "worker", "customer"})
MANAGED_LOCI: Final[frozenset[str]] = frozenset({"managed"})
#: The principal auto-approval records as (never a person, never a human review).
_POLICY_ACTOR: Final[str] = "lemoncrow-policy"
_AI_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {"finding_investigation", "hypothesis_generation", "finding_dedupe", "rule_check"}
)


# --------------------------------------------------------------------------- #
# read-only guard
# --------------------------------------------------------------------------- #


class AnalyticsWriteRefused(RuntimeError):
    """Analytics tried to use a store method that is not a read."""


#: Store methods analytics may call. Everything else -- every writer, CAS, eviction,
#: artifact write -- is refused, so no analytics value can ever feed Review state.
READ_METHODS: Final[frozenset[str]] = frozenset(
    {
        "get_session",
        "list_sessions",
        "get_revision",
        "latest_revision",
        "list_revisions",
        "list_units",
        "list_annotations",
        "list_review_mark_events",
        "list_outcomes",
        "list_decision_events",
        "list_intelligence_runs",
        "list_relationships",
        "list_activity_events",
        "list_evidence",
        "get_evidence_by_source",
        "read_evidence_artifact",
        "derived_results",
        "derived_results_by_kind",
    }
)


class ReadOnlyStore:
    """A Review store view that exposes only :data:`READ_METHODS`."""

    __slots__ = ("_store",)

    def __init__(self, store: Any) -> None:
        object.__setattr__(self, "_store", store.unwrap() if isinstance(store, ReadOnlyStore) else store)

    def unwrap(self) -> Any:
        return self._store

    def __getattr__(self, name: str) -> Any:
        if name not in READ_METHODS:
            raise AnalyticsWriteRefused(f"analytics may only read Review records; {name!r} is refused")
        return getattr(self._store, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AnalyticsWriteRefused("analytics never changes the Review store")


# --------------------------------------------------------------------------- #
# optional sources
# --------------------------------------------------------------------------- #

_REVIEW_PACKAGE: Final[str] = "lemoncrow.pro.capabilities.review"
_FIRST_PARTY: Final[frozenset[str]] = frozenset({"lemoncrow", "lemoncrow_client", "lemoncrow_server_core"})


@dataclass(frozen=True, slots=True)
class OptionalSource:
    """Records only some Review engines keep: the modules that read them may be absent."""

    label: str
    #: Modules under ``lemoncrow.pro.capabilities.review`` that read the records.
    modules: tuple[str, ...]
    #: Report sections the source feeds.
    sections: tuple[str, ...]


#: The Review-only local engine ships none of these modules (nor the writers of their
#: records), so there each source is reported unavailable for the sections it feeds:
#: never read, never counted as zero, never an error. Every import analytics makes of
#: a module the engine does not ship must belong to one of these sources.
OPTIONAL_SOURCES: Final[Mapping[str, OptionalSource]] = {
    "review_plan": OptionalSource("Review depth and escalation", ("run_plan",), ("depth", "escalation", "rules")),
    "provider_merges": OptionalSource("Provider merges", ("outcomes.deployment_outcomes",), ("merge_time",)),
    "check_findings": OptionalSource(
        "Check findings", ("check_ingest", "static_findings"), ("findings", "rules", "checks", "catch_contribution")
    ),
    "fix_requests": OptionalSource("Fix requests", ("review_actions",), ("correction_loops", "findings")),
    "auto_approval": OptionalSource("Auto-approval", ("auto_approval",), ("auto_approval",)),
    "break_glass": OptionalSource("Break-glass", ("break_glass",), ("overrides",)),
    "compute": OptionalSource("Measured compute", ("compute_ledger",), ("cost",)),
    "cache_reuse": OptionalSource("Reused results", ("economy",), ("cache_reuse",)),
    "side_effects": OptionalSource("Side-effect register", ("behavior", "side_effects"), ("drift",)),
    "escaped_defects": OptionalSource(
        "Escaped defects",
        ("outcomes.deployment_outcomes", "outcomes.observation_outcomes", "outcomes.outcome_feeds"),
        ("escaped_defects",),
    ),
    # Supplied by the host (its Inbox projection) rather than imported here: unavailable
    # when that projection needs a module the engine does not ship.
    "reviewer_load": OptionalSource("Reviewer load", (), ("waiting_by_owner", "reviewer_load")),
}


def first_party_missing(exc: ModuleNotFoundError) -> bool:
    """A LemonCrow module this engine does not ship (a missing third-party module is a real fault)."""

    return (exc.name or "").split(".", 1)[0] in _FIRST_PARTY


def source_available(name: str) -> bool:
    """Whether this engine ships every module *name* reads with."""

    for module in OPTIONAL_SOURCES[name].modules:
        try:
            importlib.import_module(f"{_REVIEW_PACKAGE}.{module}")
        except ModuleNotFoundError as exc:
            if first_party_missing(exc):
                return False
            raise
    return True


def unavailable_sources() -> frozenset[str]:
    return frozenset(name for name in OPTIONAL_SOURCES if not source_available(name))


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One source record an aggregate counted: the drill-down row.

    ``value`` is what the record contributed (1 for a count; minutes, hours,
    calls or money for a sum), so a drill-down always re-adds to its aggregate.
    """

    review_id: str
    repo_id: str
    record_kind: str
    record_id: str
    revision_id: str = ""
    at: str = ""
    label: str = ""
    value: float = 1.0

    def to_wire(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "repo_id": self.repo_id,
            "record_kind": self.record_kind,
            "record_id": self.record_id,
            "revision_id": self.revision_id,
            "at": self.at,
            "label": self.label,
            "value": round(self.value, 6),
        }


@dataclass(frozen=True, slots=True)
class FindingFact:
    source: FindingSource
    record_id: str
    """Annotation id (AI / LemonCrow) or the line-independent fingerprint (imported static)."""
    revision_id: str
    category: str
    severity: str
    outcome: FindingOutcome
    rules: tuple[str, ...] = ()
    """Exact configured rule identities (AI: canonical ``rule:...@sha256:...`` refs;
    deterministic: ``<tool>:<rule id>``)."""
    check: str = ""
    """The check that reported it (deterministic: the check identity; AI: ``ai_review``)."""
    basis: str = ""
    """Why the outcome is what it is (``confirmed``, ``stale``, ``retracted``, ``action``, ...)."""
    dismissal_reason: str = ""
    path: str = ""
    created_at: str = ""
    title: str = ""


@dataclass(frozen=True, slots=True)
class UsageFact:
    """Model calls and spend of one run or job group, with who paid for it."""

    record_kind: str
    record_id: str
    revision_id: str
    locus: str
    cost_class: CostClass
    calls: int
    provider_cost: float | None
    currency: str
    capability: str = ""
    at: str = ""


@dataclass(frozen=True, slots=True)
class ComputeFact:
    record_kind: str
    record_id: str
    revision_id: str
    executor: str
    cost_class: CostClass
    stage: str
    minutes: float


@dataclass(frozen=True, slots=True)
class SavingFact:
    revision_id: str
    entry_id: str
    reason: str
    minutes: float
    runs: int


@dataclass(frozen=True, slots=True)
class ActionFact:
    action_id: str
    revision_id: str
    kind: str
    status: str
    resulting_revision_id: str
    re_reviewed: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class RegisterChange:
    """One side-effect register change a Review introduces, with the human judgment on it."""

    behavior_key: str
    revision_id: str
    subject: str
    category: str
    change: str
    title: str
    paths: tuple[str, ...]
    critical: bool
    judgment: str
    """``accepted``, ``change_requested`` or ``not_judged``."""
    judged_by: str = ""
    judged_at: str = ""
    judgment_event_id: str = ""


@dataclass(frozen=True, slots=True)
class ReviewFacts:
    review_id: str
    repo_id: str
    repo_name: str
    title: str
    status: str
    actor_type: str
    created_at: str
    revision_ids: tuple[str, ...]
    latest_revision_id: str
    latest_revision_at: str
    paths: tuple[str, ...] = ()
    teams: frozenset[str] = frozenset()
    findings: tuple[FindingFact, ...] = ()
    first_judgment: SourceRef | None = None
    merged: SourceRef | None = None
    depth: str = ""
    escalation_state: str = ""
    escalation_triggers: tuple[str, ...] = ()
    rule_asks: tuple[str, ...] = ()
    auto_approval: Mapping[str, Any] | None = None
    break_glass: tuple[Mapping[str, Any], ...] = ()
    usage: tuple[UsageFact, ...] = ()
    compute: tuple[ComputeFact, ...] = ()
    savings: tuple[SavingFact, ...] = ()
    actions: tuple[ActionFact, ...] = ()
    register: tuple[RegisterChange, ...] = ()
    escapes: tuple[Any, ...] = ()
    notes: tuple[str, ...] = field(default=())

    def ref(
        self,
        record_kind: str,
        record_id: str,
        *,
        revision_id: str = "",
        at: str = "",
        label: str = "",
        value: float = 1.0,
    ) -> SourceRef:
        return SourceRef(
            review_id=self.review_id,
            repo_id=self.repo_id,
            record_kind=record_kind,
            record_id=record_id,
            revision_id=revision_id,
            at=at,
            label=label,
            value=value,
        )


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HostUsage:
    """Host-specific cost attribution.

    ``review_host_cost_class`` says who pays for work placed on the process that
    holds the Snapshot (``customer`` locally, ``managed`` on the hosted server).
    ``external`` adds per-Review durable-job rows (hosted), keyed by Review id,
    each ``{stage, locus, jobs, compute_seconds}``: job time only (see :func:`_usage`).
    """

    review_host_cost_class: CostClass = "customer"
    external: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)


def cost_class_of(locus: str, *, review_host: CostClass) -> CostClass:
    if locus in CUSTOMER_LOCI:
        return "customer"
    if locus in MANAGED_LOCI:
        return "managed"
    if locus == "review_host":
        return review_host
    return "unattributed"


def _loads(raw: object) -> Mapping[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _earliest(rows: Iterable[SourceRef]) -> SourceRef | None:
    best: tuple[Any, SourceRef] | None = None
    for row in rows:
        instant = parse_timestamp(row.at)
        if instant is None:
            continue
        if best is None or (instant, row.record_id) < best[0]:
            best = ((instant, row.record_id), row)
    return None if best is None else best[1]


def _plans(store: Any, revision_ids: Sequence[str]) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review.run_plan import load_run_plan

    out: dict[str, Any] = {}
    for revision_id in revision_ids:
        try:
            plan = load_run_plan(store, revision_id)
        except (ValueError, TypeError, KeyError):
            plan = None
        if plan is not None:
            out[revision_id] = plan
    return out


def _ai_locus(plan: Any | None) -> str:
    """The one executor the Snapshot's AI entries ran on, or ``""`` when none or mixed."""

    if plan is None:
        return ""
    loci = {str(entry.executor) for entry in plan.entries if str(entry.id).startswith("ai.") and entry.status == "run"}
    return next(iter(loci)) if len(loci) == 1 else ""


def _human_judgments(store: Any, review_id: str, repo_id: str) -> list[SourceRef]:
    """Every human judgment on the Review: target marks, outcomes and decisions (never the policy actor)."""

    refs: list[SourceRef] = []
    for event in store.list_review_mark_events(review_id):
        # "Decide later" is recorded as a judgment event but is not one: setting a part aside is neither a first
        # review nor a re-review.
        if event.event_kind == "judgment" and event.actor_type == "human" and event.to_state != "later":
            refs.append(
                SourceRef(
                    review_id, repo_id, "mark_event", event.id, event.revision_id, event.created_at, "Target judgment"
                )
            )
    for outcome in store.list_outcomes(review_id):
        if outcome.reviewer_id != _POLICY_ACTOR:
            refs.append(
                SourceRef(
                    review_id,
                    repo_id,
                    "outcome",
                    outcome.id,
                    outcome.revision_id,
                    outcome.created_at,
                    f"Outcome {outcome.outcome}",
                )
            )
    for decision in store.list_decision_events(review_id):
        if decision.actor_type == "human":
            refs.append(
                SourceRef(
                    review_id,
                    repo_id,
                    "decision_event",
                    decision.id,
                    decision.revision_id,
                    decision.created_at,
                    "Decision",
                )
            )
    return refs


def _merged(store: Any, review_id: str, repo_id: str, *, provider_merges: bool = True) -> SourceRef | None:
    refs = [
        SourceRef(
            review_id, repo_id, "landing", item.id, "", item.updated_at or item.created_at, "Merged through LemonCrow"
        )
        for item in store.list_relationships(review_id, kind="landing")
        if item.status == "merged"
    ]
    if provider_merges:
        from lemoncrow.pro.capabilities.review.outcomes.deployment_outcomes import DeploymentLedger

        for key, (_stamp, record) in DeploymentLedger(store).records(review_id).items():
            refs.append(
                SourceRef(
                    review_id,
                    repo_id,
                    "merge_commit",
                    f"{key}:{record.merged.sha}",
                    record.revision_id,
                    record.merged.merged_at,
                    "Provider merge",
                )
            )
    return _earliest(refs)


def _ai_findings(
    store: Any,
    revision_ids: Sequence[str],
    annotations: Sequence[Any],
    acted_subjects: set[str],
) -> list[FindingFact]:
    from lemoncrow.pro.capabilities.review.review_triage import FINDING_EVIDENCE_KIND, FindingEvidence

    order = {revision_id: index for index, revision_id in enumerate(revision_ids)}
    evidence: dict[str, tuple[int, FindingEvidence]] = {}
    for (revision_id, _scope), (_stamp, payload) in store.derived_results(
        FINDING_EVIDENCE_KIND, list(revision_ids)
    ).items():
        parsed = FindingEvidence.from_wire(payload)
        if parsed is None:
            continue
        rank = order.get(revision_id, -1)
        if parsed.annotation_id not in evidence or rank >= evidence[parsed.annotation_id][0]:
            evidence[parsed.annotation_id] = (rank, parsed)
    out: list[FindingFact] = []
    for annotation in annotations:
        if annotation.parent_id or annotation.source not in {"ai_review", "lemoncrow"}:
            continue
        found = evidence.get(annotation.id)
        severity = (found[1].severity if found else None) or "unrated"
        category = (found[1].category if found else "") or "uncategorized"
        disposition = str(annotation.finding_disposition or "")
        outcome: FindingOutcome
        reason = ""
        if annotation.source == "ai_review":
            if disposition == "dismissed":
                outcome, basis, reason = "dismissed", "dismissed", str(annotation.finding_disposition_reason or "")
            elif disposition in {"confirmed_by_reviewer", "fixed"}:
                outcome, basis = "acted_on", disposition
            elif annotation.id in acted_subjects:
                outcome, basis = "acted_on", "action"
            elif disposition == "superseded_stale":
                outcome, basis = "invalidated", "stale"
            elif annotation.state in {"orphaned", "obsolete"}:
                outcome, basis = "invalidated", "retracted"
            else:
                outcome, basis = "open", "open"
            source: FindingSource = "ai"
            rules = tuple(dict.fromkeys(str(ref) for ref in annotation.rule_refs if str(ref)))
            check = "ai_review"
        else:
            # LemonCrow's own deterministic evidence annotations: no AI disposition exists.
            if annotation.id in acted_subjects:
                outcome, basis = "acted_on", "action"
            elif annotation.state == "resolved":
                outcome, basis = "acted_on", "resolved"
            elif annotation.state in {"orphaned", "obsolete"}:
                outcome, basis = "invalidated", "retracted"
            else:
                outcome, basis = "open", "open"
            source = "deterministic"
            rules = (f"lemoncrow:{annotation.source_id}",) if annotation.source_id else ()
            check = "lemoncrow"
        out.append(
            FindingFact(
                source=source,
                record_id=annotation.id,
                revision_id=annotation.revision_id,
                category=category,
                severity=severity,
                outcome=outcome,
                rules=rules,
                check=check,
                basis=basis,
                dismissal_reason=reason,
                path=str(annotation.anchor.path or ""),
                created_at=annotation.created_at,
                title=str(annotation.title or "")[:160],
            )
        )
    return out


def _static_findings(store: Any, review_id: str, revisions: Sequence[Any]) -> list[FindingFact]:
    from lemoncrow.pro.capabilities.review.check_ingest import is_ingest_row
    from lemoncrow.pro.capabilities.review.static_findings import dispositions, snapshot_findings

    if not revisions:
        return []
    evidence = tuple(store.list_evidence(review_id))
    if not any(is_ingest_row(item) for item in evidence):
        return []
    judged = dispositions(store, review_id)
    seen: dict[str, Any] = {}
    latest_current: set[str] = set()
    latest_checks: set[str] = set()
    latest = revisions[-1]
    for revision in revisions:
        current, _stale, checks = snapshot_findings(store, revision, evidence=evidence, revisions=revisions)
        for row in current:
            seen[row.fingerprint] = row
        if revision.id == latest.id:
            latest_current = {row.fingerprint for row in current}
            latest_checks = {str(check["check_identity"]) for check in checks if check.get("state") == "current"}
    out: list[FindingFact] = []
    for fingerprint, row in seen.items():
        judgment = judged.get(fingerprint)
        outcome: FindingOutcome
        reason = ""
        if judgment is not None and judgment["disposition"] == "dismissed":
            outcome, basis, reason = "dismissed", "dismissed", str(judgment.get("reason") or "")
        elif judgment is not None and judgment["disposition"] == "confirmed":
            outcome, basis = "acted_on", "confirmed"
        elif fingerprint in latest_current:
            outcome, basis = "open", "open"
        elif row.check_identity in latest_checks:
            outcome, basis = "acted_on", "resolved_on_later_snapshot"
        else:
            outcome, basis = "invalidated", "stale"
        finding = row.finding
        out.append(
            FindingFact(
                source="deterministic",
                record_id=fingerprint,
                revision_id=row.evidence.revision_id,
                category=row.category or "uncategorized",
                severity=str(finding.severity or "unrated"),
                outcome=outcome,
                rules=(f"{finding.tool}:{finding.rule_id}",),
                check=row.check_identity,
                basis=basis,
                dismissal_reason=reason,
                path=finding.path,
                created_at=row.evidence.created_at,
                title=f"{finding.tool} {finding.rule_id}"[:160],
            )
        )
    return out


def _register(store: Any, latest: Any | None) -> tuple[RegisterChange, ...]:
    if latest is None:
        return ()
    from lemoncrow.pro.capabilities.review.behavior import behavior_fingerprint, behavior_key
    from lemoncrow.pro.capabilities.review.side_effects import DERIVED_KIND, delta_from_wire

    row = store.derived_results(DERIVED_KIND, [latest.id]).get((latest.id, "current"))
    if row is None or not isinstance(row[1], Mapping):
        return ()
    deltas = []
    for raw in row[1].get("deltas") or ():
        if not isinstance(raw, Mapping) or raw.get("kind") != "side_effect":
            continue
        try:
            deltas.append(delta_from_wire(raw))
        except (KeyError, TypeError, ValueError):
            continue
    if not deltas:
        return ()
    events = [
        event
        for event in store.list_review_mark_events(latest.review_id)
        if event.actor_type == "human" and event.event_kind == "judgment"
    ]
    out: list[RegisterChange] = []
    for delta in deltas:
        key = behavior_key(delta)
        fingerprint = behavior_fingerprint(delta)
        newest: dict[str, Any] = {}
        for event in events:
            if event.unit_key != key:
                continue
            current = newest.get(event.reviewer_id)
            if current is None or (event.created_at, event.id) >= (current.created_at, current.id):
                newest[event.reviewer_id] = event
        accepted = sorted(
            (
                item
                for item in newest.values()
                if item.to_state == "reviewed" and item.content_fingerprint == fingerprint
            ),
            key=lambda item: (item.created_at, item.id),
        )
        requested = sorted(
            (
                item
                for item in newest.values()
                if item.to_state == "needs_changes" and item.content_fingerprint == fingerprint
            ),
            key=lambda item: (item.created_at, item.id),
        )
        judgment, chosen = (
            ("accepted", accepted[0])
            if accepted
            else ("change_requested", requested[0]) if requested else ("not_judged", None)
        )
        out.append(
            RegisterChange(
                behavior_key=key,
                revision_id=latest.id,
                subject=delta.subject,
                category=delta.subject.split(":", 1)[0] if ":" in delta.subject else "other",
                change=delta.change,
                title=delta.title[:200],
                paths=delta.paths,
                critical=delta.critical,
                judgment=judgment,
                judged_by="" if chosen is None else str(chosen.reviewer_id),
                judged_at="" if chosen is None else str(chosen.created_at),
                judgment_event_id="" if chosen is None else str(chosen.id),
            )
        )
    return tuple(out)


def _usage(
    store: Any,
    review_id: str,
    plans: Mapping[str, Any],
    host: HostUsage,
) -> tuple[list[UsageFact], list[ComputeFact]]:
    usage: list[UsageFact] = []
    for run in store.list_intelligence_runs(review_id):
        if run.status not in {"ready", "failed"} or int(run.model_calls or 0) <= 0:
            continue
        document = _loads(run.usage_json)
        reported = document.get("usage") if isinstance(document.get("usage"), Mapping) else {}
        assert isinstance(reported, Mapping)
        raw_cost = reported.get("provider_cost")
        cost = float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) else None
        locus = _ai_locus(plans.get(run.revision_id)) if run.capability in _AI_CAPABILITIES else ""
        locus = locus or "review_host"
        usage.append(
            UsageFact(
                record_kind="intelligence_run",
                record_id=run.id,
                revision_id=run.revision_id,
                locus=locus,
                cost_class=cost_class_of(locus, review_host=host.review_host_cost_class),
                calls=int(run.model_calls or 0),
                provider_cost=cost,
                currency=str(reported.get("currency") or "") if cost is not None else "",
                capability=str(run.capability),
                at=str(run.completed_at or run.created_at or ""),
            )
        )
    # Durable-job rows add time only. Their model calls and provider cost are the same
    # model runs the worker records as intelligence runs above (``run_independent_review``
    # writes them to this store), so counting them again would double the spend.
    compute: list[ComputeFact] = []
    for index, row in enumerate(host.external.get(review_id, ())):
        locus = str(row.get("locus") or "")
        stage = str(row.get("stage") or "")
        seconds = max(0.0, float(row.get("compute_seconds") or 0.0))
        if seconds:
            compute.append(
                ComputeFact(
                    "hosted_jobs",
                    f"jobs:{stage}:{locus}:{index}",
                    "",
                    locus,
                    cost_class_of(locus, review_host=host.review_host_cost_class),
                    stage,
                    seconds / 60.0,
                )
            )
    return usage, compute


def _actions(
    store: Any, revision_ids: Sequence[str], numbers: Mapping[str, int], judgments: Sequence[SourceRef]
) -> tuple[set[str], list[ActionFact]]:
    """``(subjects of code-changing Review actions, correction-loop facts)``."""

    from lemoncrow.pro.capabilities.review.review_actions import (
        CODE_CHANGING_KINDS,
        CROSS_CHANGE_KINDS,
        ReviewActionLedger,
    )

    actions = ReviewActionLedger(store).list(revision_ids) if revision_ids else ()
    acted_subjects = {
        subject.id
        for action in actions
        if action.kind in CODE_CHANGING_KINDS and action.status not in {"failed", "cancelled"}
        for subject in action.subjects
    }
    action_facts: list[ActionFact] = []
    for action in actions:
        if action.kind not in CODE_CHANGING_KINDS or action.kind in CROSS_CHANGE_KINDS:
            continue
        resulting = action.resulting_revision_id if action.status == "snapshot_recorded" else ""
        floor = numbers.get(resulting)
        re_reviewed = floor is not None and any(
            numbers.get(ref.revision_id, -1) >= floor for ref in judgments if ref.record_kind != "decision_event"
        )
        action_facts.append(
            ActionFact(
                action.id,
                action.source_revision_id,
                action.kind,
                action.status,
                resulting,
                re_reviewed,
                action.created_at,
            )
        )
    return acted_subjects, action_facts


def _measured_compute(store: Any, revision_ids: Sequence[str], host: HostUsage) -> list[ComputeFact]:
    from lemoncrow.pro.capabilities.review.compute_ledger import ComputeLedger

    return [
        ComputeFact(
            "compute_usage",
            f"{row.entry_id}|{row.executor_class}|{row.stage}",
            "",
            row.executor_class,
            cost_class_of(row.executor_class, review_host=host.review_host_cost_class),
            row.stage,
            row.seconds / 60.0,
        )
        for row in ComputeLedger(store).rows(revision_ids)
        if row.seconds > 0
    ]


def _savings(store: Any, revision_ids: Sequence[str]) -> list[SavingFact]:
    from lemoncrow.pro.capabilities.review.economy import SavingsLedger

    return [
        SavingFact("", entry_id, reason, seconds / 60.0, runs)
        for (entry_id, reason), (seconds, runs) in sorted(SavingsLedger(store).rows(revision_ids).items())
        if seconds > 0
    ]


def _auto_approval(store: Any, revision_id: str) -> Mapping[str, Any] | None:
    from lemoncrow.pro.capabilities.review.auto_approval import stored_decision

    decision: Mapping[str, Any] | None = stored_decision(store, revision_id)
    return decision


def _break_glass(store: Any, revision_ids: Sequence[str]) -> tuple[Mapping[str, Any], ...]:
    from lemoncrow.pro.capabilities.review import break_glass

    return tuple(break_glass.records(store, revision_ids)) if revision_ids else ()


def review_facts(
    raw_store: Any,
    session: Any,
    *,
    repo_id: str,
    repo_name: str = "",
    teams: frozenset[str] = frozenset(),
    want_paths: bool = False,
    host: HostUsage | None = None,
    escapes_for: Callable[[Any, Any], Sequence[Any]] | None = None,
    unavailable: frozenset[str] | None = None,
) -> ReviewFacts:
    """Every analytic fact of one Review, read-only, each traceable to its record.

    A source in ``unavailable`` (default: what this engine does not ship) is not read.
    """

    missing = unavailable_sources() if unavailable is None else unavailable
    store = ReadOnlyStore(raw_store)
    host = host or HostUsage()
    review_id = str(session.id)
    revisions = tuple(sorted(store.list_revisions(review_id), key=lambda item: item.revision_number))
    revision_ids = tuple(item.id for item in revisions)
    latest = revisions[-1] if revisions else None
    numbers = {item.id: item.revision_number for item in revisions}
    plans = {} if "review_plan" in missing else _plans(store, revision_ids)
    latest_plan = plans.get(latest.id) if latest is not None else None

    judgments = _human_judgments(store, review_id, repo_id)
    acted_subjects, action_facts = (
        (set(), []) if "fix_requests" in missing else _actions(store, revision_ids, numbers, judgments)
    )

    annotations = tuple(store.list_annotations(review_id))
    findings = [
        *_ai_findings(store, revision_ids, annotations, acted_subjects),
        *(() if "check_findings" in missing else _static_findings(store, review_id, revisions)),
    ]

    usage, external_compute = _usage(store, review_id, plans, host)
    compute = [] if "compute" in missing else _measured_compute(store, revision_ids, host)
    savings = [] if "cache_reuse" in missing else _savings(store, revision_ids)

    depth = ""
    escalation_state = ""
    triggers: tuple[str, ...] = ()
    rule_asks: tuple[str, ...] = ()
    if latest_plan is not None:
        depth = str(latest_plan.selection.get("base") or latest_plan.selection.get("name") or "")
        escalation = latest_plan.escalation if isinstance(latest_plan.escalation, Mapping) else {}
        escalation_state = str(escalation.get("state") or "")
        triggers = tuple(
            sorted(
                {str(item.get("class") or "") for item in escalation.get("triggers") or () if isinstance(item, Mapping)}
                - {""}
            )
        )
        rule_asks = tuple(
            dict.fromkeys(ref for entry in latest_plan.entries if entry.status == "run" for ref in entry.rule_refs)
        )

    paths: tuple[str, ...] = ()
    if want_paths and latest is not None:
        paths = tuple(sorted({str(unit.path) for unit in store.list_units(latest.id) if str(unit.path)}))

    return ReviewFacts(
        review_id=review_id,
        repo_id=repo_id,
        repo_name=repo_name or repo_id,
        title=str(session.title or "")[:200],
        status=str(session.status),
        actor_type=str(getattr(session, "actor_type", "") or "unknown"),
        created_at=str(session.created_at or ""),
        revision_ids=revision_ids,
        latest_revision_id="" if latest is None else latest.id,
        latest_revision_at="" if latest is None else str(latest.created_at or ""),
        paths=paths,
        teams=teams,
        findings=tuple(findings),
        first_judgment=_earliest(judgments),
        merged=_merged(store, review_id, repo_id, provider_merges="provider_merges" not in missing),
        depth=depth,
        escalation_state=escalation_state,
        escalation_triggers=triggers,
        rule_asks=rule_asks,
        auto_approval=None if latest is None or "auto_approval" in missing else _auto_approval(store, latest.id),
        break_glass=() if "break_glass" in missing else _break_glass(store, revision_ids),
        usage=tuple(usage),
        compute=(*compute, *external_compute),
        savings=tuple(savings),
        actions=tuple(action_facts),
        register=() if "side_effects" in missing else _register(store, latest),
        escapes=tuple(escapes_for(store, session)) if escapes_for is not None else (),
    )


__all__ = [
    "FINDING_OUTCOMES",
    "OPTIONAL_SOURCES",
    "READ_METHODS",
    "ActionFact",
    "AnalyticsWriteRefused",
    "ComputeFact",
    "FindingFact",
    "HostUsage",
    "OptionalSource",
    "ReadOnlyStore",
    "RegisterChange",
    "ReviewFacts",
    "SavingFact",
    "SourceRef",
    "UsageFact",
    "cost_class_of",
    "first_party_missing",
    "review_facts",
    "source_available",
    "unavailable_sources",
]
