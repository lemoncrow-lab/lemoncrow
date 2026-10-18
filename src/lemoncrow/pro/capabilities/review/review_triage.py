"""Review-level triage: evidence-backed priority with an explanation (Phase 9 items 3-5, 10-14).

One small typed projection per Review Snapshot that the Inbox (P9-INBOX),
routing (P9-ROUTE) and escalation (P11-ESC) consume without loading a Review
body. Its fields have separate semantics and are never collapsed into one score:

* ``priority`` (``urgent | high | normal | low``) -- what to open first. Only
  deterministic current-Snapshot signals feed it: unresolved current finding
  severity, failed required verification, criticality policy, dependency
  changes, and an explicit audited human override. Linked issue/release urgency
  has no source today and is reported ``unknown``, never invented.
* ``criticality`` -- the strictest path policy over the changed paths. A policy
  floor (R1 depth floors via ``criticality.effort_floors``), not a severity.
  Priority (including an override) can never weaken it.
* ``severity_summary`` -- open findings whose severity evidence is *current* for
  this Snapshot. A finding from an earlier Snapshot that was not revalidated on
  this one is ``stale`` and stops driving priority until current evidence exists.
* ``evidence_state`` (``current | stale | missing``) -- missing evidence is never
  evidence of low risk: ``low`` priority requires current evidence, and the label
  says ``Not reviewed · current evidence missing`` separately.
* ``explanation`` -- one entry per decision with the layer and file/commit (or
  actor) that set it.

Writes: the per-Snapshot projection row and per-finding severity evidence rows
(``review_derived``; Snapshot deletion owns their retention), and override
activity events (closed by a clear event, by a new Snapshot for ``snapshot``
scope, or by Review deletion). Nothing here writes readiness, merge eligibility,
assignment or permissions, and only a human actor can override priority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, cast

from lemoncrow.pro.capabilities.review.criticality import (
    BUILT_IN_ORIGIN,
    CRITICALITY_RANK,
    CRITICALITY_VALUES,
    DEFAULT_FINDING_THRESHOLD,
    FINDING_SEVERITIES,
    FINDING_THRESHOLDS,
    SEVERITY_LABELS,
    SEVERITY_RANK,
    THRESHOLD_LABELS,
    Criticality,
    EffectiveTriagePolicy,
    FindingSeverity,
    FindingThreshold,
    PolicyOrigin,
    PolicyState,
    RequiredFocus,
    normalize_finding_severity,
    surfaced,
)
from lemoncrow.pro.capabilities.review.review_priority import QueueReason
from lemoncrow.pro.capabilities.review.session_models import ActorType, ReviewActivityEvent

PriorityLevel = Literal["urgent", "high", "normal", "low"]
EvidenceState = Literal["current", "stale", "missing"]
ReviewState = Literal["not_reviewed", "in_progress", "reviewed", "unknown"]
OverrideScope = Literal["review", "snapshot"]

PRIORITY_LEVELS: Final[tuple[str, ...]] = ("urgent", "high", "normal", "low")
PRIORITY_RANK: Final[Mapping[str, int]] = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
EVIDENCE_STATES: Final[tuple[str, ...]] = ("current", "stale", "missing")
REVIEW_STATES: Final[tuple[str, ...]] = ("not_reviewed", "in_progress", "reviewed", "unknown")
OVERRIDE_SCOPES: Final[tuple[str, ...]] = ("review", "snapshot")

TRIAGE_VERSION: Final[int] = 1
TRIAGE_KIND: Final[str] = "review_triage"
TRIAGE_SCOPE: Final[str] = "current"
FINDING_EVIDENCE_KIND: Final[str] = "finding_severity"
PRIORITY_OVERRIDE_SET: Final[str] = "priority.override_set"
PRIORITY_OVERRIDE_CLEARED: Final[str] = "priority.override_cleared"
PRIORITY_OVERRIDE_KINDS: Final[tuple[str, ...]] = (PRIORITY_OVERRIDE_SET, PRIORITY_OVERRIDE_CLEARED)
MAX_OVERRIDE_REASON: Final[int] = 500

#: Unresolved AI finding dispositions: open, or confirmed real by a reviewer.
_UNRESOLVED_DISPOSITIONS: Final[frozenset[str]] = frozenset({"", "open", "confirmed_by_reviewer"})
#: Inbox order within one priority: unknown risk (missing/stale) before current.
_EVIDENCE_ORDER: Final[Mapping[str, int]] = {"missing": 0, "stale": 1, "current": 2}
#: A priority floor that criticality policy implies; never a severity.
_CRITICALITY_PRIORITY: Final[Mapping[str, PriorityLevel]] = {"critical": "high", "high": "normal"}

EVIDENCE_ORIGIN: Final[PolicyOrigin] = PolicyOrigin("evidence", "current Snapshot evidence")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# finding severity evidence (current-Snapshot only)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FindingEvidence:
    """Severity evidence for one finding validated on one exact Snapshot.

    Written when a finding is confirmed or revalidated on a Snapshot
    (``ai_review._persist_findings``). Its existence for a Snapshot *is* the
    proof that the finding's severity is current there; an older Snapshot's
    row never speaks for a newer one.
    """

    annotation_id: str
    finding_id: str
    source: str
    category: str
    severity: FindingSeverity | None
    target_ids: tuple[str, ...] = ()
    title: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "finding_id": self.finding_id,
            "source": self.source,
            "category": self.category,
            "severity": self.severity,
            "target_ids": list(self.target_ids),
            "title": self.title,
        }

    @classmethod
    def from_wire(cls, raw: object) -> FindingEvidence | None:
        if not isinstance(raw, Mapping) or not str(raw.get("annotation_id") or ""):
            return None
        severity = raw.get("severity")
        return cls(
            annotation_id=str(raw["annotation_id"]),
            finding_id=str(raw.get("finding_id") or ""),
            source=str(raw.get("source") or ""),
            category=str(raw.get("category") or ""),
            severity=cast(FindingSeverity, severity) if severity in FINDING_SEVERITIES else None,
            target_ids=tuple(str(item) for item in raw.get("target_ids") or () if str(item)),
            title=str(raw.get("title") or ""),
        )


def ai_finding_evidence(
    annotation_id: str, *, finding_id: str, category: str, target_ids: Sequence[str], title: str
) -> FindingEvidence:
    """Severity evidence for one confirmed AI finding: policy normalizes its validated category."""

    return FindingEvidence(
        annotation_id=annotation_id,
        finding_id=finding_id,
        source="ai_review",
        category=category,
        severity=normalize_finding_severity("ai_review", category=category),
        target_ids=tuple(target_ids),
        title=title,
    )


def record_finding_evidence(store: Any, *, review_id: str, revision_id: str, rows: Sequence[FindingEvidence]) -> None:
    """Store current-Snapshot severity evidence (one ``review_derived`` row per finding).

    Closer: Snapshot deletion removes ``review_derived`` rows with the Snapshot.
    A store without derived rows records nothing; its findings then read as
    unrated (always surfaced, never low).
    """

    put = getattr(store, "put_derived_results", None)
    if not rows or not callable(put):
        return
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    put(
        [
            DerivedResult(
                review_id=review_id,
                revision_id=revision_id,
                kind=FINDING_EVIDENCE_KIND,
                scope_key=row.annotation_id,
                stamp=row.finding_id or row.annotation_id,
                payload=row.to_wire(),
            )
            for row in rows
        ]
    )


@dataclass(frozen=True, slots=True)
class CurrentFinding:
    """One finding whose evidence is current on the Snapshot being triaged."""

    annotation_id: str
    severity: FindingSeverity | None
    category: str = ""
    title: str = ""
    target_ids: tuple[str, ...] = ()
    unresolved: bool = True
    surfaced: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "severity": self.severity,
            "severity_label": SEVERITY_LABELS.get(self.severity or "", "Unrated"),
            "category": self.category,
            "title": self.title,
            "target_ids": list(self.target_ids),
            "unresolved": self.unresolved,
            "surfaced": self.surfaced,
        }


def current_findings(
    store: Any,
    *,
    revision_id: str,
    annotations: Sequence[Any],
    threshold: FindingThreshold = DEFAULT_FINDING_THRESHOLD,
) -> tuple[tuple[CurrentFinding, ...], int]:
    """``(current findings, stale unresolved count)`` for one Snapshot.

    A root AI finding is current when this Snapshot holds its severity evidence
    (confirmed or revalidated here) or it was created on this Snapshot; the
    latter without evidence is *unrated*. An unresolved finding from an earlier
    Snapshot without current evidence is stale: counted, never a priority input.
    """

    rows = store.derived_results(FINDING_EVIDENCE_KIND, [revision_id]) if hasattr(store, "derived_results") else {}
    evidence: dict[str, FindingEvidence] = {}
    for _key, (_stamp, payload) in rows.items():
        parsed = FindingEvidence.from_wire(payload)
        if parsed is not None:
            evidence[parsed.annotation_id] = parsed
    found: list[CurrentFinding] = []
    stale = 0
    for annotation in annotations:
        if getattr(annotation, "source", "") != "ai_review" or getattr(annotation, "parent_id", ""):
            continue
        unresolved = (
            getattr(annotation, "state", "") == "open"
            and str(getattr(annotation, "finding_disposition", "") or "") in _UNRESOLVED_DISPOSITIONS
        )
        row = evidence.get(annotation.id)
        if row is None and annotation.revision_id != revision_id:
            stale += 1 if unresolved else 0
            continue
        severity = None if row is None else row.severity
        targets = (
            row.target_ids
            if row is not None and row.target_ids
            else tuple(
                ref[len("target:") :] for ref in getattr(annotation, "evidence", ()) if ref.startswith("target:")
            )
        )
        found.append(
            CurrentFinding(
                annotation_id=annotation.id,
                severity=severity,
                category="" if row is None else row.category,
                title=str(getattr(annotation, "title", "") or ("" if row is None else row.title)),
                target_ids=tuple(
                    target_id if target_id.startswith("target:") else f"target:{target_id}" for target_id in targets
                ),
                unresolved=unresolved,
                surfaced=surfaced(severity, threshold),
            )
        )
    found.sort(key=lambda item: (-SEVERITY_RANK.get(item.severity or "", 4), item.annotation_id))
    return tuple(found), stale


def finding_queue_reasons(findings: Iterable[CurrentFinding]) -> dict[str, list[QueueReason]]:
    """Unresolved current P0/P1 findings as Review-first queue reasons per target id."""

    out: dict[str, list[QueueReason]] = {}
    for finding in findings:
        if not finding.unresolved or finding.severity not in ("p0", "p1"):
            continue
        title = finding.title if len(finding.title) <= 80 else finding.title[:79].rstrip() + "…"
        reason = QueueReason("finding_unresolved", f"AI finding · {SEVERITY_LABELS[finding.severity]}: {title}")
        for target_id in finding.target_ids:
            bucket = out.setdefault(target_id, [])
            if reason not in bucket:
                bucket.append(reason)
    return out


# --------------------------------------------------------------------------- #
# behavior and vulnerability severity signals (current-Snapshot only)
# --------------------------------------------------------------------------- #

SignalSource = Literal["behavior", "vulnerability", "static_analysis"]
#: Every source the severity summary counts, in display order.
SEVERITY_SOURCES: Final[tuple[str, ...]] = ("ai_review", "behavior", "vulnerability", "static_analysis")
#: The producer id of an exact-Snapshot ``vulnerability_scan`` evidence row.
VULNERABILITY_PRODUCER: Final[str] = "vulnerability_scan"
#: The producer id of imported SARIF/check-run findings (P11-FED ``static_findings``).
STATIC_PRODUCER: Final[str] = "static_analysis"
_SOURCE_NOUNS: Final[Mapping[str, tuple[str, str]]] = {
    "ai_review": ("AI finding", "AI findings"),
    "behavior": ("behavior change", "behavior changes"),
    "vulnerability": ("vulnerability advisory", "vulnerability advisories"),
    "static_analysis": ("static finding", "static findings"),
}


@dataclass(frozen=True, slots=True)
class SeveritySignal:
    """One deterministic severity signal that is not an AI finding.

    ``behavior``: one R2 behavior delta, rated from its delta severity and its
    ``critical`` flag. ``vulnerability``: one scanner advisory on a dependency
    the Snapshot changes. *revision_id* is the Snapshot whose exact evidence
    observed it; ``stale`` marks one carried from an earlier Snapshot whose
    producer has not run since (:func:`stale_severity_signals`).
    ``static_analysis``: one imported SARIF/check-run finding
    (``static_findings.static_severity_signals``). ``ref`` is the id the Reader
    shows it under; a current signal with a ref is subject to the finding
    threshold exactly like an AI finding (``hidden_finding_ids``).
    """

    source: SignalSource
    key: str
    producer: str
    severity: FindingSeverity | None
    title: str
    revision_id: str
    target_ids: tuple[str, ...] = ()
    stale: bool = False
    ref: str = ""


def _target_refs(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(value if value.startswith("target:") else f"target:{value}" for value in values if value)
    )


def behavior_signals(deltas: Iterable[Any], *, revision_id: str) -> tuple[SeveritySignal, ...]:
    """Severity signals from the exact-Snapshot R2 behavior deltas of *revision_id*.

    Every observed delta counts until a later run no longer observes it. A
    behavior judgment is one reviewer's mark, so it orders that reviewer's
    queue but does not remove the evidence from the Review-level summary.
    """

    from lemoncrow.pro.capabilities.review.behavior import behavior_key

    return tuple(
        SeveritySignal(
            source="behavior",
            key=behavior_key(delta),
            producer=str(delta.producer),
            severity=normalize_finding_severity("behavior", raw=str(delta.severity), critical=bool(delta.critical)),
            title=str(delta.title),
            revision_id=revision_id,
            target_ids=_target_refs(str(item) for item in delta.target_ids),
        )
        for delta in deltas
    )


def dependency_package(label: str) -> str:
    """Lower-case package name of a dependency unit/target label, or ``""``.

    Labels are ``ecosystem:name`` or ``ecosystem:name (directory)``; the
    ``lockfile metadata`` row names no package.
    """

    _ecosystem, _sep, name = label.partition(":")
    if name.endswith(")") and " (" in name:
        name = name[: name.rindex(" (")]
    name = name.strip()
    return "" if name in ("", "lockfile metadata") else name.lower()


def vulnerability_signals(
    findings: Iterable[Any], *, revision_id: str, changed_packages: Mapping[str, Sequence[str]]
) -> tuple[SeveritySignal, ...]:
    """Advisories (``supply_chain.vulnerability_findings``) on dependencies this Snapshot changes.

    *changed_packages* maps a lower-case changed package name to its target
    ids, the same name join the dependency review uses. An advisory on a
    package the change does not touch is not this Review's signal.
    """

    out: dict[str, SeveritySignal] = {}
    for finding in findings:
        package = str(finding.package).lower()
        if package not in changed_packages:
            continue
        advisory = str(finding.advisory or "unnamed advisory")
        key = f"vulnerability:{package}:{advisory}"
        severity = normalize_finding_severity("vulnerability", raw=str(finding.severity or ""))
        previous = out.get(key)
        if previous is not None and SEVERITY_RANK.get(previous.severity or "", -1) >= SEVERITY_RANK.get(
            severity or "", -1
        ):
            continue  # one advisory once, at its strictest rating
        out[key] = SeveritySignal(
            source="vulnerability",
            key=key,
            producer=VULNERABILITY_PRODUCER,
            severity=severity,
            title=f"{advisory} in {finding.package}",
            revision_id=revision_id,
            target_ids=_target_refs(changed_packages[package]),
        )
    return tuple(out[key] for key in sorted(out))


def stale_severity_signals(
    current: Sequence[SeveritySignal],
    ran: Iterable[str],
    earlier: Iterable[tuple[Sequence[SeveritySignal], Iterable[str]]],
    *,
    producers: Iterable[str] = (),
) -> tuple[SeveritySignal, ...]:
    """Earlier-Snapshot signals that this Snapshot neither re-observed nor re-ran (item 5's rule).

    *ran* is the producers that ran on this Snapshot; *earlier* yields
    ``(observed signals, producers that ran)`` per earlier Snapshot, newest
    first. A signal is current when re-observed here; resolved once its
    producer ran on a newer Snapshot without observing it; otherwise stale:
    counted, never a priority input, and the evidence is not current. Iteration
    stops as soon as every producer in *producers* has run since, so *earlier*
    can be lazy.
    """

    # lc-debt: resolution is producer-level (a run of the producer on both sides); per-subject once ExecutedScope names its subjects
    since = set(ran)
    universe = set(producers)
    seen = {signal.key for signal in current}
    out: list[SeveritySignal] = []
    if universe and universe <= since:
        return ()
    for observed, ran_there in earlier:
        for signal in observed:
            if signal.key in seen:
                continue
            seen.add(signal.key)
            if signal.producer not in since:
                out.append(replace(signal, stale=True))
        since.update(ran_there)
        if universe and universe <= since:
            break  # anything older is re-observed or resolved: never compute it
    return tuple(sorted(out, key=lambda signal: signal.key))


def finding_evidence_state(ai_status: str, *, stale_findings: int, stale_signals: int = 0) -> tuple[EvidenceState, str]:
    """Severity evidence state from the current Snapshot's AI review status and stale signals.

    ``ready`` (ran here) and ``skipped_unnecessary`` (deterministic triage found
    nothing to investigate here) are current AI evidence. Anything else is not:
    ``stale`` when earlier findings await revalidation, otherwise ``missing``.
    A behavior/vulnerability signal from an earlier Snapshot whose producer has
    not run here also makes the evidence ``stale``, whatever the AI status.
    """

    not_rerun = (
        f"{stale_signals} behavior/vulnerability/static signal(s) from an earlier Snapshot were not re-run here"
        if stale_signals
        else ""
    )
    current = {
        "ready": "AI review ran on this Snapshot",
        "skipped_unnecessary": "deterministic triage found nothing to investigate on this Snapshot",
    }.get(ai_status)
    if current is not None:
        return ("stale", f"{current}; {not_rerun}") if not_rerun else ("current", current)
    why = {
        "pending": "AI review is still running on this Snapshot",
        "failed": "AI review failed on this Snapshot",
        "cancelled": "AI review was cancelled on this Snapshot",
    }.get(ai_status, "AI review has not run on this Snapshot")
    stale = [
        part
        for part in (
            f"{stale_findings} finding(s) from an earlier Snapshot are not revalidated" if stale_findings else "",
            not_rerun,
        )
        if part
    ]
    if stale:
        return "stale", "; ".join((why, *stale))
    return "missing", why


# --------------------------------------------------------------------------- #
# audited human overrides
# --------------------------------------------------------------------------- #


class PriorityOverrideRefused(ValueError):
    """An override the action boundary will not record."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PriorityOverride:
    """An explicit, attributed priority override (actor, reason, time, scope)."""

    level: PriorityLevel
    reason: str
    scope: OverrideScope
    actor_id: str
    actor_type: str
    revision_id: str
    created_at: str
    event_id: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reason": self.reason,
            "scope": self.scope,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "revision_id": self.revision_id,
            "created_at": self.created_at,
            "event_id": self.event_id,
        }

    @classmethod
    def from_wire(cls, raw: object) -> PriorityOverride | None:
        if not isinstance(raw, Mapping) or raw.get("level") not in PRIORITY_LEVELS:
            return None
        scope = raw.get("scope") if raw.get("scope") in OVERRIDE_SCOPES else "review"
        return cls(
            level=cast(PriorityLevel, raw["level"]),
            reason=str(raw.get("reason") or ""),
            scope=cast(OverrideScope, scope),
            actor_id=str(raw.get("actor_id") or ""),
            actor_type=str(raw.get("actor_type") or ""),
            revision_id=str(raw.get("revision_id") or ""),
            created_at=str(raw.get("created_at") or ""),
            event_id=str(raw.get("event_id") or ""),
        )


def _checked_actor(actor_id: str, actor_type: str) -> str:
    if actor_type != "human":
        raise PriorityOverrideRefused(
            "not_human",
            "only a human reviewer or admin may override Review priority; AI ranking can only order work",
        )
    actor = actor_id.strip()
    if not actor:
        raise PriorityOverrideRefused("no_actor", "a priority override must be attributed to an actor")
    return actor


def _checked_reason(reason: str) -> str:
    text = " ".join(str(reason or "").split())
    if not text:
        raise PriorityOverrideRefused("no_reason", "a priority override needs a reason")
    if len(text) > MAX_OVERRIDE_REASON:
        raise PriorityOverrideRefused(
            "reason_too_long", f"a priority override reason is at most {MAX_OVERRIDE_REASON} characters"
        )
    return text


def priority_override_event(
    *,
    review_id: str,
    revision_id: str,
    level: str,
    reason: str,
    scope: str,
    actor_id: str,
    actor_type: ActorType,
) -> ReviewActivityEvent:
    """The append-only activity event that records one override (refuses non-human actors)."""

    actor = _checked_actor(actor_id, actor_type)
    if level not in PRIORITY_LEVELS:
        raise PriorityOverrideRefused("invalid_level", "priority must be urgent, high, normal or low")
    if scope not in OVERRIDE_SCOPES:
        raise PriorityOverrideRefused("invalid_scope", "override scope must be review or snapshot")
    text = _checked_reason(reason)
    return ReviewActivityEvent(
        id="",
        review_id=review_id,
        revision_id=revision_id,
        kind=cast(Any, PRIORITY_OVERRIDE_SET),
        actor_id=actor,
        actor_type=actor_type,
        subject_type="review_priority",
        subject_id=review_id,
        summary=f"Priority set to {level} ({scope}): {text}"[:300],
        detail_json=json.dumps({"level": level, "reason": text, "scope": scope}, sort_keys=True),
    )


def priority_override_clear_event(
    *,
    review_id: str,
    revision_id: str,
    reason: str,
    actor_id: str,
    actor_type: ActorType,
) -> ReviewActivityEvent:
    """The activity event that closes the active override (computed priority resumes)."""

    actor = _checked_actor(actor_id, actor_type)
    text = _checked_reason(reason)
    return ReviewActivityEvent(
        id="",
        review_id=review_id,
        revision_id=revision_id,
        kind=cast(Any, PRIORITY_OVERRIDE_CLEARED),
        actor_id=actor,
        actor_type=actor_type,
        subject_type="review_priority",
        subject_id=review_id,
        summary=f"Priority override cleared: {text}"[:300],
        detail_json=json.dumps({"reason": text}, sort_keys=True),
    )


def active_override(events: Iterable[ReviewActivityEvent], *, current_revision_id: str) -> PriorityOverride | None:
    """The override in force on *current_revision_id*, if any.

    The newest set/clear event decides. A ``snapshot``-scoped override closes
    when the Snapshot changes. An event not written by a human, or one that is
    malformed, is ignored (the computed priority stands).
    """

    relevant = sorted(
        (event for event in events if event.kind in PRIORITY_OVERRIDE_KINDS),
        key=lambda event: (event.created_at, event.id),
    )
    if not relevant:
        return None
    latest = relevant[-1]
    if latest.kind != PRIORITY_OVERRIDE_SET or latest.actor_type != "human":
        return None
    try:
        detail = json.loads(latest.detail_json or "{}")
    except ValueError:
        return None
    if not isinstance(detail, Mapping) or detail.get("level") not in PRIORITY_LEVELS:
        return None
    scope = detail.get("scope")
    if scope not in OVERRIDE_SCOPES:
        return None
    if scope == "snapshot" and latest.revision_id != current_revision_id:
        return None
    return PriorityOverride(
        level=cast(PriorityLevel, detail["level"]),
        reason=str(detail.get("reason") or ""),
        scope=cast(OverrideScope, scope),
        actor_id=latest.actor_id,
        actor_type=latest.actor_type,
        revision_id=latest.revision_id,
        created_at=latest.created_at,
        event_id=latest.id,
    )


# --------------------------------------------------------------------------- #
# the per-Review projection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Explanation:
    """Why one field has its value, and which layer/file/commit or actor set it."""

    field: str
    value: str
    reason: str
    origin: PolicyOrigin = BUILT_IN_ORIGIN

    def to_wire(self) -> dict[str, Any]:
        return {"field": self.field, "value": self.value, "reason": self.reason, "origin": self.origin.to_wire()}

    @classmethod
    def from_wire(cls, raw: object) -> Explanation | None:
        if not isinstance(raw, Mapping) or not str(raw.get("field") or ""):
            return None
        return cls(
            field=str(raw["field"]),
            value=str(raw.get("value") or ""),
            reason=str(raw.get("reason") or ""),
            origin=PolicyOrigin.from_wire(raw.get("origin")),
        )


_COUNT_FIELDS: Final[tuple[str, ...]] = ("p0", "p1", "p2", "suggestion", "unrated", "stale")


def _source_counts(source: str, counts: Mapping[str, int]) -> SourceCounts:
    return SourceCounts(
        source,
        p0=counts.get("p0", 0),
        p1=counts.get("p1", 0),
        p2=counts.get("p2", 0),
        suggestion=counts.get("suggestion", 0),
        unrated=counts.get("unrated", 0),
        stale=counts.get("stale", 0),
    )


def _wire_count(raw: Mapping[Any, Any], name: str) -> int:
    value = raw.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


@dataclass(frozen=True, slots=True)
class SourceCounts:
    """One source's share of the :class:`SeveritySummary` (``ai_review | behavior | vulnerability``)."""

    source: str
    p0: int = 0
    p1: int = 0
    p2: int = 0
    suggestion: int = 0
    unrated: int = 0
    stale: int = 0

    def count(self, level: str) -> int:
        return int(getattr(self, level))

    def to_wire(self) -> dict[str, int]:
        return {name: self.count(name) for name in _COUNT_FIELDS}


@dataclass(frozen=True, slots=True)
class SeveritySummary:
    """Unresolved findings and signals with current evidence by severity; stale ones counted apart.

    Sources are AI findings, R2 behavior deltas and vulnerability advisories on
    changed dependencies (``by_source``); the top-level counts are their sums.
    """

    p0: int = 0
    p1: int = 0
    p2: int = 0
    suggestion: int = 0
    unrated: int = 0
    stale: int = 0
    below_threshold: int = 0
    by_source: tuple[SourceCounts, ...] = ()

    @property
    def highest(self) -> FindingSeverity | None:
        levels: tuple[tuple[FindingSeverity, int], ...] = (
            ("p0", self.p0),
            ("p1", self.p1),
            ("p2", self.p2),
            ("suggestion", self.suggestion),
        )
        return next((name for name, count in levels if count), None)

    def to_wire(self) -> dict[str, Any]:
        return {
            "p0": self.p0,
            "p1": self.p1,
            "p2": self.p2,
            "suggestion": self.suggestion,
            "unrated": self.unrated,
            "stale": self.stale,
            "below_threshold": self.below_threshold,
            "highest": self.highest,
            "by_source": {row.source: row.to_wire() for row in self.by_source},
        }

    @classmethod
    def from_wire(cls, raw: object) -> SeveritySummary:
        if not isinstance(raw, Mapping):
            return cls()
        sources = raw.get("by_source")
        by_source = tuple(
            _source_counts(source, {name: _wire_count(sources[source], name) for name in _COUNT_FIELDS})
            for source in SEVERITY_SOURCES
            if isinstance(sources, Mapping) and isinstance(sources.get(source), Mapping)
        )
        return cls(
            p0=_wire_count(raw, "p0"),
            p1=_wire_count(raw, "p1"),
            p2=_wire_count(raw, "p2"),
            suggestion=_wire_count(raw, "suggestion"),
            unrated=_wire_count(raw, "unrated"),
            stale=_wire_count(raw, "stale"),
            below_threshold=_wire_count(raw, "below_threshold"),
            by_source=by_source,
        )


@dataclass(frozen=True, slots=True)
class ReviewTriage:
    """The lightweight per-Review projection (see module docstring for field semantics).

    ``computed`` is ``False`` for the placeholder served when no projection was
    derived for the Snapshot yet: evidence ``missing``, priority ``normal``.
    """

    review_id: str
    revision_id: str
    priority: PriorityLevel = "normal"
    computed_priority: PriorityLevel = "normal"
    criticality: Criticality = "normal"
    severity_summary: SeveritySummary = SeveritySummary()
    finding_threshold: FindingThreshold = DEFAULT_FINDING_THRESHOLD
    finding_threshold_origin: PolicyOrigin = BUILT_IN_ORIGIN
    evidence_state: EvidenceState = "missing"
    review_state: ReviewState = "unknown"
    release_urgency: Literal["unknown"] = "unknown"
    dependency_changes: int = 0
    required_failures: int = 0
    override: PriorityOverride | None = None
    explanation: tuple[Explanation, ...] = ()
    hidden_finding_ids: tuple[str, ...] = ()
    required_focus: tuple[RequiredFocus, ...] = ()
    policy_state: PolicyState = "final"
    policy_errors: tuple[str, ...] = ()
    computed: bool = True

    @property
    def evidence_label(self) -> str:
        evidence = {
            "current": "current evidence",
            "stale": "current evidence missing (stale findings)",
            "missing": "current evidence missing",
        }[self.evidence_state]
        human = {
            "not_reviewed": "Not reviewed",
            "in_progress": "Review in progress",
            "reviewed": "Reviewed",
            "unknown": "",
        }[self.review_state]
        return f"{human} · {evidence}" if human else evidence[:1].upper() + evidence[1:]

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": TRIAGE_VERSION,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "priority": self.priority,
            "computed_priority": self.computed_priority,
            "criticality": self.criticality,
            "severity_summary": self.severity_summary.to_wire(),
            "finding_threshold": {
                "value": self.finding_threshold,
                "label": THRESHOLD_LABELS[self.finding_threshold],
                "origin": self.finding_threshold_origin.to_wire(),
            },
            "evidence_state": self.evidence_state,
            "review_state": self.review_state,
            "evidence_label": self.evidence_label,
            "release_urgency": self.release_urgency,
            "dependency_changes": self.dependency_changes,
            "required_failures": self.required_failures,
            "override": None if self.override is None else self.override.to_wire(),
            "explanation": [entry.to_wire() for entry in self.explanation],
            "hidden_finding_ids": list(self.hidden_finding_ids),
            "required_focus": [item.to_wire() for item in self.required_focus],
            "policy_state": self.policy_state,
            "policy_errors": list(self.policy_errors),
            "computed": self.computed,
        }

    @classmethod
    def from_wire(cls, raw: object) -> ReviewTriage | None:
        """Strict decode of a persisted row; anything malformed is ``None`` (served as missing)."""

        if not isinstance(raw, Mapping) or raw.get("version") != TRIAGE_VERSION:
            return None
        values = {
            "priority": PRIORITY_LEVELS,
            "computed_priority": PRIORITY_LEVELS,
            "criticality": CRITICALITY_VALUES,
            "evidence_state": EVIDENCE_STATES,
            "review_state": REVIEW_STATES,
            "policy_state": ("final", "provisional", "invalid"),
        }
        if any(raw.get(name) not in allowed for name, allowed in values.items()):
            return None
        threshold = raw.get("finding_threshold")
        if not isinstance(threshold, Mapping) or threshold.get("value") not in FINDING_THRESHOLDS:
            return None
        explanation = tuple(
            entry for entry in (Explanation.from_wire(item) for item in raw.get("explanation") or ()) if entry
        )
        return cls(
            review_id=str(raw.get("review_id") or ""),
            revision_id=str(raw.get("revision_id") or ""),
            priority=cast(PriorityLevel, raw["priority"]),
            computed_priority=cast(PriorityLevel, raw["computed_priority"]),
            criticality=cast(Criticality, raw["criticality"]),
            severity_summary=SeveritySummary.from_wire(raw.get("severity_summary")),
            finding_threshold=cast(FindingThreshold, threshold["value"]),
            finding_threshold_origin=PolicyOrigin.from_wire(threshold.get("origin")),
            evidence_state=cast(EvidenceState, raw["evidence_state"]),
            review_state=cast(ReviewState, raw["review_state"]),
            dependency_changes=int(raw.get("dependency_changes") or 0),
            required_failures=int(raw.get("required_failures") or 0),
            override=PriorityOverride.from_wire(raw.get("override")),
            explanation=explanation,
            hidden_finding_ids=tuple(str(item) for item in raw.get("hidden_finding_ids") or () if str(item)),
            required_focus=tuple(
                item for item in (RequiredFocus.from_wire(entry) for entry in raw.get("required_focus") or ()) if item
            ),
            policy_state=cast(PolicyState, raw["policy_state"]),
            policy_errors=tuple(str(item) for item in raw.get("policy_errors") or () if str(item)),
            computed=raw.get("computed") is not False,
        )


def _max_level(levels: Iterable[PriorityLevel]) -> PriorityLevel:
    return max(levels, key=lambda level: PRIORITY_RANK[level], default="normal")


def _breakdown(by_source: Sequence[SourceCounts], level: str) -> str:
    """``1 AI finding, 2 behavior changes`` for one severity level (or ``stale``)."""

    parts = []
    for row in by_source:
        count = row.count(level)
        if count:
            singular, plural = _SOURCE_NOUNS.get(row.source, (row.source, row.source))
            parts.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(parts)


def _clip_paths(paths: Sequence[str]) -> str:
    return ", ".join(paths[:3]) + ("…" if len(paths) > 3 else "")


def derive_review_triage(
    *,
    review_id: str,
    revision_id: str,
    policy: EffectiveTriagePolicy,
    changed_paths: Sequence[str],
    findings: Sequence[CurrentFinding],
    stale_findings: int = 0,
    signals: Sequence[SeveritySignal] = (),
    evidence_state: EvidenceState = "missing",
    evidence_detail: str = "",
    required_failures: int = 0,
    dependency_changes: int = 0,
    review_state: ReviewState = "unknown",
) -> ReviewTriage:
    """Compute the Review-level projection from deterministic current-Snapshot signals.

    Pure. *findings* must already be current for *revision_id* (see
    :func:`current_findings`); stale ones arrive only as a count. *signals* are
    behavior/vulnerability signals: current ones count like findings, ``stale``
    ones (:func:`stale_severity_signals`) are counted apart and never raise
    priority. The result carries no override -- apply one with
    :func:`with_override`.
    """

    unresolved = [finding for finding in findings if finding.unresolved]
    # Current signals the Reader shows under an id (imported static findings)
    # fold below the threshold like AI findings; stale ones are never shown as current.
    thresholded = [
        signal
        for signal in signals
        if signal.ref and not signal.stale and not surfaced(signal.severity, policy.finding_threshold)
    ]
    rated: list[tuple[str, FindingSeverity | None]] = [("ai_review", finding.severity) for finding in unresolved]
    rated.extend((signal.source, signal.severity) for signal in signals if not signal.stale)
    by_source = tuple(
        _source_counts(
            source,
            {
                **{
                    name: sum(1 for owner, level in rated if owner == source and level == name)
                    for name in FINDING_SEVERITIES
                },
                "unrated": sum(1 for owner, level in rated if owner == source and level is None),
                "stale": (stale_findings if source == "ai_review" else 0)
                + sum(1 for signal in signals if signal.stale and signal.source == source),
            },
        )
        for source in SEVERITY_SOURCES
    )
    totals = {name: sum(row.count(name) for row in by_source) for name in _COUNT_FIELDS}
    summary = SeveritySummary(
        p0=totals["p0"],
        p1=totals["p1"],
        p2=totals["p2"],
        suggestion=totals["suggestion"],
        unrated=totals["unrated"],
        stale=totals["stale"],
        below_threshold=sum(not finding.surfaced for finding in unresolved) + len(thresholded),
        by_source=by_source,
    )
    criticality, setters = policy.review_criticality(changed_paths)
    required_focus = policy.required_focus(changed_paths)
    raises: list[tuple[PriorityLevel, Explanation]] = []
    explanation: list[Explanation] = []

    # Criticality is its own field: the policy floor, with the rule and file/commit that set it.
    if setters and setters[0].criticality_rule is not None:
        rule = setters[0].criticality_rule
        paths = _clip_paths([item.path for item in setters])
        explanation.append(
            Explanation(
                "criticality", criticality, f"{paths} matched {rule.pattern} (strictest rule wins)", rule.origin
            )
        )
    else:
        explanation.append(
            Explanation("criticality", criticality, "no criticality rule matches a changed path", BUILT_IN_ORIGIN)
        )

    def severity_raise(level: PriorityLevel, name: str, label: str) -> None:
        count = summary.p0 if name == "p0" else summary.p1 if name == "p1" else summary.p2
        if count:
            reason = f"{count} unresolved {label} finding(s) with current evidence ({_breakdown(by_source, name)})"
            raises.append((level, Explanation("severity", name, reason, EVIDENCE_ORIGIN)))

    severity_raise("urgent", "p0", "P0 Critical")
    severity_raise("high", "p1", "P1 High")
    if required_failures:
        raises.append(
            (
                "high",
                Explanation(
                    "required_verification",
                    str(required_failures),
                    f"{required_failures} required check(s) failing on this Snapshot",
                    EVIDENCE_ORIGIN,
                ),
            )
        )
    implied = _CRITICALITY_PRIORITY.get(criticality)
    if implied is not None:
        origin = explanation[0].origin
        raises.append(
            (
                implied,
                Explanation(
                    "criticality", criticality, f"{criticality} criticality keeps priority at least {implied}", origin
                ),
            )
        )
    severity_raise("normal", "p2", "P2 Medium")
    if dependency_changes:
        raises.append(
            (
                "normal",
                Explanation(
                    "dependency",
                    str(dependency_changes),
                    f"{dependency_changes} dependency change(s) on this Snapshot",
                    EVIDENCE_ORIGIN,
                ),
            )
        )
    if summary.stale:
        explanation.append(
            Explanation(
                "severity",
                "stale",
                f"{summary.stale} finding(s) from an earlier Snapshot ({_breakdown(by_source, 'stale')}) no longer"
                " drive priority until revalidated",
                EVIDENCE_ORIGIN,
            )
        )
    explanation.extend(entry for _level, entry in raises if entry.field != "criticality")
    explanation.extend(
        Explanation(
            "required_focus",
            item.category,
            f"{item.category.replace('_', ' ')} review required on {_clip_paths(item.paths)} by focus.require"
            " (a readiness condition; priority never removes it)",
            item.origins[0] if item.origins else BUILT_IN_ORIGIN,
        )
        for item in required_focus
    )
    explanation.append(
        Explanation(
            "release_urgency",
            "unknown",
            "no linked issue or release source exists for this Review; urgency is never inferred",
            BUILT_IN_ORIGIN,
        )
    )
    explanation.append(Explanation("evidence", evidence_state, evidence_detail or evidence_state, EVIDENCE_ORIGIN))

    computed = _max_level(level for level, _entry in raises)
    why = max(raises, key=lambda row: PRIORITY_RANK[row[0]])[1] if raises else None
    every_low = bool(changed_paths) and all(policy.classify(path).criticality == "low" for path in changed_paths)
    if computed == "normal" and every_low:
        blockers = [
            label
            for label, present in (
                ("current evidence missing", evidence_state != "current"),
                ("unresolved or unrated findings", bool(summary.p0 or summary.p1 or summary.p2 or summary.unrated)),
                ("stale findings not revalidated", bool(summary.stale)),
                ("required checks failing", bool(required_failures)),
                ("review focus required", bool(required_focus)),
                (f"policy {policy.policy_state}", policy.policy_state != "final"),
            )
            if present
        ]
        if blockers:
            priority_entry = Explanation(
                "priority",
                "normal",
                "every changed path is low criticality, but not lowered: " + "; ".join(blockers),
                BUILT_IN_ORIGIN,
            )
        else:
            computed = "low"
            priority_entry = Explanation(
                "priority",
                "low",
                "every changed path is low criticality and current evidence has no unresolved finding",
                explanation[0].origin,
            )
    elif why is not None:
        priority_entry = Explanation("priority", computed, f"highest signal: {why.reason}", why.origin)
    else:
        detail = "no current signal raises it"
        if evidence_state != "current":
            detail += "; current evidence missing, so it is never lowered"
        priority_entry = Explanation("priority", "normal", detail, BUILT_IN_ORIGIN)

    threshold_entry = Explanation(
        "finding_threshold",
        policy.finding_threshold,
        f"{THRESHOLD_LABELS[policy.finding_threshold]}: {summary.below_threshold} finding(s) below it are hidden;"
        " the threshold never changes what runs",
        policy.threshold_origin,
    )
    return ReviewTriage(
        review_id=review_id,
        revision_id=revision_id,
        priority=computed,
        computed_priority=computed,
        criticality=criticality,
        severity_summary=summary,
        finding_threshold=policy.finding_threshold,
        finding_threshold_origin=policy.threshold_origin,
        evidence_state=evidence_state,
        review_state=review_state,
        dependency_changes=dependency_changes,
        required_failures=required_failures,
        explanation=(priority_entry, *explanation, threshold_entry),
        hidden_finding_ids=(
            *(finding.annotation_id for finding in findings if not finding.surfaced),
            *(signal.ref for signal in thresholded),
        ),
        required_focus=required_focus,
        policy_state=policy.policy_state,
        policy_errors=policy.errors,
    )


def with_override(triage: ReviewTriage, override: PriorityOverride | None) -> ReviewTriage:
    """Apply an audited override to ``priority`` only.

    Criticality, severity, evidence state and threshold are untouched: an
    override reorders work, it never weakens criticality or required checks.
    """

    base = tuple(entry for entry in triage.explanation if entry.field != "override")
    if override is None:
        return replace(triage, priority=triage.computed_priority, override=None, explanation=base)
    entry = Explanation(
        "override",
        override.level,
        f"{override.scope}-scoped override (computed {triage.computed_priority}): {override.reason}",
        PolicyOrigin("override", f"{override.actor_type}:{override.actor_id} at {override.created_at}"),
    )
    return replace(triage, priority=override.level, override=override, explanation=(entry, *base))


def missing_triage(review_id: str, revision_id: str) -> ReviewTriage:
    """The placeholder when no projection exists for the Snapshot: evidence missing, never low."""

    return ReviewTriage(
        review_id=review_id,
        revision_id=revision_id,
        computed=False,
        explanation=(
            Explanation(
                "priority",
                "normal",
                "no triage was derived for this Snapshot yet; current evidence missing, so it is never lowered",
                BUILT_IN_ORIGIN,
            ),
            Explanation("evidence", "missing", "no projection for this Snapshot", EVIDENCE_ORIGIN),
        ),
    )


def persist_review_triage(store: Any, triage: ReviewTriage) -> None:
    """Store the reviewer-independent projection for its Snapshot (no override, no review state).

    The Inbox reads this row instead of a Review body. Closer: Snapshot
    deletion removes ``review_derived`` rows with the Snapshot.
    """

    from lemoncrow.pro.capabilities.review.store import DerivedResult

    base = replace(with_override(triage, None), review_state="unknown")
    payload = base.to_wire()
    stamp = hashlib.sha256(_canonical(payload)).hexdigest()
    existing = store.derived_results(TRIAGE_KIND, [triage.revision_id]).get((triage.revision_id, TRIAGE_SCOPE))
    if existing is not None and existing[0] == stamp:
        return  # unchanged: a read never rewrites an identical row
    store.put_derived_results(
        [
            DerivedResult(
                review_id=triage.review_id,
                revision_id=triage.revision_id,
                kind=TRIAGE_KIND,
                scope_key=TRIAGE_SCOPE,
                stamp=stamp,
                payload=payload,
            )
        ]
    )


def load_review_triage(
    store: Any,
    *,
    review_id: str,
    revision_id: str,
    events: Iterable[ReviewActivityEvent] = (),
) -> ReviewTriage:
    """The lightweight projection for one Snapshot without loading the Review body.

    Reads only the stored ``review_triage`` row and the override events. A
    missing or malformed row is served as :func:`missing_triage`.
    """

    rows = store.derived_results(TRIAGE_KIND, [revision_id])
    row = rows.get((revision_id, TRIAGE_SCOPE))
    triage = ReviewTriage.from_wire(row[1]) if row is not None else None
    if triage is None or triage.review_id != review_id:
        triage = missing_triage(review_id, revision_id)
    return with_override(triage, active_override(events, current_revision_id=revision_id))


def inbox_order_key(triage: ReviewTriage) -> tuple[Any, ...]:
    """Deterministic Inbox order: priority, criticality, severity, unknown evidence first, then id.

    Equal evidence always yields the same order (the Review id breaks ties).
    """

    summary = triage.severity_summary
    return (
        -PRIORITY_RANK[triage.priority],
        -CRITICALITY_RANK[triage.criticality],
        -summary.p0,
        -summary.p1,
        -summary.p2,
        _EVIDENCE_ORDER[triage.evidence_state],
        triage.review_id,
        triage.revision_id,
    )


def order_for_inbox(rows: Iterable[ReviewTriage]) -> list[ReviewTriage]:
    return sorted(rows, key=inbox_order_key)


__all__ = [
    "EVIDENCE_ORIGIN",
    "EVIDENCE_STATES",
    "FINDING_EVIDENCE_KIND",
    "MAX_OVERRIDE_REASON",
    "OVERRIDE_SCOPES",
    "PRIORITY_LEVELS",
    "PRIORITY_OVERRIDE_CLEARED",
    "PRIORITY_OVERRIDE_KINDS",
    "PRIORITY_OVERRIDE_SET",
    "PRIORITY_RANK",
    "REVIEW_STATES",
    "SEVERITY_SOURCES",
    "TRIAGE_KIND",
    "TRIAGE_SCOPE",
    "TRIAGE_VERSION",
    "VULNERABILITY_PRODUCER",
    "CurrentFinding",
    "EvidenceState",
    "Explanation",
    "FindingEvidence",
    "OverrideScope",
    "PriorityLevel",
    "PriorityOverride",
    "PriorityOverrideRefused",
    "ReviewState",
    "ReviewTriage",
    "SeveritySignal",
    "SeveritySummary",
    "SignalSource",
    "SourceCounts",
    "active_override",
    "ai_finding_evidence",
    "behavior_signals",
    "current_findings",
    "dependency_package",
    "derive_review_triage",
    "finding_evidence_state",
    "finding_queue_reasons",
    "inbox_order_key",
    "load_review_triage",
    "missing_triage",
    "order_for_inbox",
    "persist_review_triage",
    "priority_override_clear_event",
    "priority_override_event",
    "record_finding_evidence",
    "stale_severity_signals",
    "vulnerability_signals",
    "with_override",
]
