"""Deterministic (static) findings on a Snapshot: projection, chains, judgments, usefulness.

Phase 11 P11-FED items 7, 8 and 13, over the ingestion artifacts written by
:mod:`.check_ingest`. Nothing here is stored except a human's disposition of
one finding, which is an append-only Review activity event
(``finding.static_disposition``) closed by Review deletion like every other
activity row. Usefulness rates are derived on read from imported findings and
those dispositions; no counter exists to drift or to retain.

* **Current vs stale.** Only the newest observation per check identity on the
  requested Snapshot contributes current findings, so a check-run rerun or
  completion replaces its predecessor and nothing is double counted. A check
  that has not reported on this Snapshot contributes its latest earlier
  findings labelled ``stale``: shown, never counted, never citable as current.
* **Investigators** (:func:`investigator_static_context`) receive current
  findings as typed refs (``static:<evidence>:<finding>``) they may cite, and
  stale ones only as a labelled data block with no ref.
* **Chains** (:func:`security_chains`) compose several lower-severity security
  facts in one file into one attack path by named, versioned rules; every
  contributing finding and evidence edge is kept with a step-by-step
  explanation. The chain severity is display only; it gates nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.check_ingest import (
    ImportedFinding,
    IngestDocument,
    is_ingest_row,
    load_ingest,
)
from lemoncrow.pro.capabilities.review.criticality import SEVERITY_LABELS, SEVERITY_RANK, FindingSeverity
from lemoncrow.pro.capabilities.review.review_triage import STATIC_PRODUCER, SeveritySignal
from lemoncrow.pro.capabilities.review.session_models import (
    FINDING_DISMISSAL_REASONS,
    ReviewActivityEvent,
    ReviewEvidence,
    ReviewRevision,
)

DISPOSITION_EVENT: Final[str] = "finding.static_disposition"
StaticDisposition = Literal["dismissed", "confirmed", "open"]
STATIC_DISPOSITIONS: Final[tuple[str, ...]] = ("dismissed", "confirmed", "open")
REF_PREFIX: Final[str] = "static:"
CHAIN_RULES_VERSION: Final[int] = 1
#: Bound on findings handed to one investigator (current refs and stale context each).
MAX_INVESTIGATOR_FINDINGS: Final[int] = 24
_MAX_USEFULNESS_REVIEWS: Final[int] = 200


def finding_ref(evidence_id: str, finding_id: str) -> str:
    return f"{REF_PREFIX}{evidence_id}:{finding_id}"


@dataclass(frozen=True, slots=True)
class StaticFinding:
    """One imported finding as seen from one Snapshot."""

    finding: ImportedFinding
    evidence: ReviewEvidence
    state: Literal["current", "stale"]
    snapshot_number: int | None
    check_identity: str
    category: str

    @property
    def ref(self) -> str:
        return finding_ref(self.evidence.id, self.finding.id)

    @property
    def fingerprint(self) -> str:
        """Line-independent identity: what dispositions and usefulness are keyed by."""

        return self.finding.fingerprint

    def to_wire(self, disposition: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from lemoncrow.pro.capabilities.review.verification import evidence_authority

        return {
            **self.finding.to_wire(),
            "ref": self.ref,
            "state": self.state,
            "evidence_id": self.evidence.id,
            "snapshot_id": self.evidence.revision_id,
            "snapshot_number": self.snapshot_number,
            "check": self.evidence.title,
            "check_identity": self.check_identity,
            "check_status": self.evidence.verification_status,
            "category": self.category,
            "attestation_class": self.evidence.attestation_class,
            "authority": evidence_authority(self.evidence),
            "head_sha": self.evidence.provider_revision,
            "disposition": dict(disposition) if disposition else None,
        }


@dataclass(frozen=True, slots=True)
class _Observed:
    document: IngestDocument
    state: Literal["current", "stale"]
    identity: str


def _identity(item: ReviewEvidence) -> str:
    from lemoncrow.pro.capabilities.review.verification import check_key, evidence_authority

    typed = check_key(item)
    if typed:
        return typed
    return f"{evidence_authority(item)}|{item.stable_key.strip() or item.title.strip()}"


def _observations(
    store: Any, evidence: Sequence[ReviewEvidence], revision: ReviewRevision, revisions: Sequence[ReviewRevision]
) -> list[_Observed]:
    numbers = {item.id: item.revision_number for item in revisions}
    rows = [
        item
        for item in evidence
        if is_ingest_row(item)
        and item.execution_intent != "explicit_historical"
        and numbers.get(item.revision_id, 1 << 30) <= revision.revision_number
    ]
    rows.sort(key=lambda item: (numbers.get(item.revision_id, 0), item.created_at, item.id), reverse=True)
    seen: set[str] = set()
    out: list[_Observed] = []
    for item in rows:
        identity = _identity(item)
        if identity in seen:
            continue
        document = load_ingest(store, item)
        if document is None:
            continue
        seen.add(identity)
        out.append(_Observed(document, "current" if item.revision_id == revision.id else "stale", identity))
    return out


def dispositions(store: Any, review_id: str) -> dict[str, dict[str, Any]]:
    """Latest human disposition per finding *fingerprint* (``open`` clears an earlier one).

    Keyed by the line-independent fingerprint, so a judgment follows its
    finding when the code around it moves on a later Snapshot, and never
    transfers to another result of the same rule.
    """

    latest: dict[str, dict[str, Any]] = {}
    for event in store.list_activity_events(review_id, kinds=(DISPOSITION_EVENT,), limit=5000):
        try:
            detail = json.loads(event.detail_json or "{}")
        except ValueError:
            continue
        if not isinstance(detail, Mapping):
            continue
        key = str(detail.get("fingerprint") or "")
        if not key:
            continue
        if detail.get("disposition") == "open":
            latest.pop(key, None)
            continue
        latest[key] = {
            "disposition": str(detail.get("disposition") or ""),
            "reason": str(detail.get("reason") or ""),
            "actor": event.actor_id,
            "snapshot_id": event.revision_id,
            "recorded_at": event.created_at,
            "tool": str(detail.get("tool") or ""),
            "rule_id": str(detail.get("rule_id") or ""),
            "finding_id": str(detail.get("finding_id") or ""),
        }
    return latest


def snapshot_findings(
    store: Any,
    revision: ReviewRevision,
    *,
    evidence: Sequence[ReviewEvidence] | None = None,
    revisions: Sequence[ReviewRevision] | None = None,
) -> tuple[tuple[StaticFinding, ...], tuple[StaticFinding, ...], list[dict[str, Any]]]:
    """``(current, stale, checks)`` for one Snapshot; ``checks`` carries each source's truncation."""

    rows = tuple(evidence) if evidence is not None else tuple(store.list_evidence(revision.review_id))
    history = tuple(revisions) if revisions is not None else tuple(store.list_revisions(revision.review_id))
    numbers = {item.id: max(0, item.revision_number - 1) for item in history}
    current: list[StaticFinding] = []
    stale: list[StaticFinding] = []
    checks: list[dict[str, Any]] = []
    for observed in _observations(store, rows, revision, history):
        document = observed.document
        item = document.evidence
        checks.append(
            {
                "evidence_id": item.id,
                "check": item.title,
                "check_identity": observed.identity,
                "tool": document.tool,
                "tool_version": document.tool_version,
                "category": document.category,
                "format": document.format,
                "status": item.verification_status,
                "status_basis": document.status_basis,
                "state": observed.state,
                "snapshot_number": numbers.get(item.revision_id),
                "findings": len(document.findings),
                "truncation": document.truncation.to_wire(),
            }
        )
        target = current if observed.state == "current" else stale
        target.extend(
            StaticFinding(
                finding=finding,
                evidence=item,
                state=observed.state,
                snapshot_number=numbers.get(item.revision_id),
                check_identity=observed.identity,
                category=document.category,
            )
            for finding in document.findings
        )

    def order(row: StaticFinding) -> tuple[Any, ...]:
        return (-SEVERITY_RANK[row.finding.severity], row.finding.path, row.finding.start_line, row.finding.id)

    return tuple(sorted(current, key=order)), tuple(sorted(stale, key=order)), checks


# --------------------------------------------------------------------------- #
# security chains
# --------------------------------------------------------------------------- #

SecurityFact = Literal[
    "missing_authz",
    "injection_sink",
    "unsafe_deserialization",
    "path_traversal",
    "ssrf",
    "secret_exposure",
    "csrf",
]
#: CWE ids that classify one finding as one security fact (from the producer's
#: tags/rule id only; never from message text).
FACT_CWES: Final[Mapping[str, frozenset[int]]] = {
    "missing_authz": frozenset({285, 287, 306, 639, 862, 863}),
    "injection_sink": frozenset({77, 78, 79, 89, 94, 95, 917, 943}),
    "unsafe_deserialization": frozenset({502}),
    "path_traversal": frozenset({22, 23, 36, 73}),
    "ssrf": frozenset({918}),
    "secret_exposure": frozenset({200, 209, 259, 312, 321, 532, 798}),
    "csrf": frozenset({352}),
}
FACT_LABELS: Final[Mapping[str, str]] = {
    "missing_authz": "missing authentication/authorization check",
    "injection_sink": "injection sink",
    "unsafe_deserialization": "unsafe deserialization",
    "path_traversal": "path traversal",
    "ssrf": "server-side request forgery",
    "secret_exposure": "exposed credential or sensitive data",
    "csrf": "cross-site request forgery",
}


@dataclass(frozen=True, slots=True)
class ChainRule:
    """One named composition: every ``needs`` fact present in one file forms the path."""

    id: str
    title: str
    needs: tuple[str, ...]
    severity: FindingSeverity
    consequence: str


CHAIN_RULES: Final[tuple[ChainRule, ...]] = (
    ChainRule(
        "unauthenticated-injection",
        "Unauthenticated injection path",
        ("missing_authz", "injection_sink"),
        "p0",
        "a caller who never authenticates may reach the injection sink",
    ),
    ChainRule(
        "unauthenticated-deserialization",
        "Unauthenticated unsafe deserialization",
        ("missing_authz", "unsafe_deserialization"),
        "p0",
        "an unauthenticated caller may supply the object graph that is deserialized",
    ),
    ChainRule(
        "unauthenticated-file-access",
        "Unauthenticated file access",
        ("missing_authz", "path_traversal"),
        "p1",
        "an unauthenticated caller may read or write paths outside the intended directory",
    ),
    ChainRule(
        "ssrf-credential-exposure",
        "SSRF with exposed credentials",
        ("ssrf", "secret_exposure"),
        "p1",
        "a forged server-side request may carry or reveal the exposed credential",
    ),
    ChainRule(
        "csrf-to-injection",
        "Cross-site request into an injection sink",
        ("csrf", "injection_sink"),
        "p1",
        "a forged cross-site request may drive the injection sink with the victim's session",
    ),
)


def security_facts(finding: ImportedFinding) -> tuple[str, ...]:
    cwes = set(finding.cwe)
    return tuple(fact for fact, ids in FACT_CWES.items() if cwes & ids)


@dataclass(frozen=True, slots=True)
class FindingChain:
    """A composed attack path. Display severity only; every edge and member is kept."""

    id: str
    rule: ChainRule
    path: str
    members: tuple[StaticFinding, ...]
    member_facts: tuple[tuple[str, ...], ...]
    edges: tuple[tuple[str, str, str], ...]
    explanation: tuple[str, ...]

    @property
    def contributing_max(self) -> FindingSeverity:
        return max((item.finding.severity for item in self.members), key=lambda value: SEVERITY_RANK[value])

    @property
    def severity(self) -> FindingSeverity:
        rule = self.rule.severity
        top = self.contributing_max
        return rule if SEVERITY_RANK[rule] >= SEVERITY_RANK[top] else top

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule": {
                "id": self.rule.id,
                "title": self.rule.title,
                "version": CHAIN_RULES_VERSION,
                "needs": list(self.rule.needs),
                "severity": self.rule.severity,
            },
            "title": self.rule.title,
            "path": self.path,
            "severity": self.severity,
            "severity_label": SEVERITY_LABELS[self.severity],
            "contributing_max": self.contributing_max,
            "raised": SEVERITY_RANK[self.severity] > SEVERITY_RANK[self.contributing_max],
            "members": [
                {
                    "ref": item.ref,
                    "finding_id": item.finding.id,
                    "evidence_id": item.evidence.id,
                    "check_identity": item.check_identity,
                    "tool": item.finding.tool,
                    "rule_id": item.finding.rule_id,
                    "severity": item.finding.severity,
                    "path": item.finding.path,
                    "start_line": item.finding.start_line,
                    "facts": list(facts),
                }
                for item, facts in zip(self.members, self.member_facts, strict=True)
            ],
            "edges": [{"from": source, "to": target, "relation": relation} for source, target, relation in self.edges],
            "explanation": list(self.explanation),
            "gates": False,
        }


def security_chains(findings: Iterable[StaticFinding], *, excluded: Iterable[str] = ()) -> tuple[FindingChain, ...]:
    """Compose current findings into attack paths by :data:`CHAIN_RULES` (same-file co-occurrence).

    Stale findings never contribute; ``excluded`` finding fingerprints
    (dismissed by a human) never contribute. The relation is co-location in one file, not a
    proven data flow, and the explanation says so.
    """

    skip = set(excluded)
    by_path: dict[str, list[tuple[StaticFinding, tuple[str, ...]]]] = {}
    for item in findings:
        if item.state != "current" or item.fingerprint in skip or not item.finding.path:
            continue
        facts = security_facts(item.finding)
        if facts:
            by_path.setdefault(item.finding.path, []).append((item, facts))
    chains: list[FindingChain] = []
    for path in sorted(by_path):
        rows = sorted(by_path[path], key=lambda pair: (pair[0].finding.start_line, pair[0].finding.id))
        for rule in CHAIN_RULES:
            groups = [[pair for pair in rows if need in pair[1]] for need in rule.needs]
            if not all(groups):
                continue
            members: dict[str, tuple[StaticFinding, tuple[str, ...]]] = {}
            for group in groups:
                for pair in group:
                    members.setdefault(pair[0].ref, pair)
            edges: list[tuple[str, str, str]] = []
            for left, right in pairwise(groups):
                for source, _facts in left:
                    for target, _target_facts in right:
                        if source.ref != target.ref:
                            edges.append((source.ref, target.ref, "same_file"))
            ordered = list(members.values())
            top = max((pair[0].finding.severity for pair in ordered), key=lambda value: SEVERITY_RANK[value])
            composed: FindingSeverity = rule.severity if SEVERITY_RANK[rule.severity] >= SEVERITY_RANK[top] else top
            explanation = [
                *(
                    f"Fact: {', '.join(FACT_LABELS[fact] for fact in facts if fact in rule.needs)} — "
                    f"{item.finding.tool} {item.finding.rule_id or '(no rule id)'} at "
                    f"{item.finding.path}:{item.finding.start_line} ({SEVERITY_LABELS[item.finding.severity]}, "
                    f"CWE {', '.join(str(value) for value in item.finding.cwe)})."
                    for item, facts in ordered
                ),
                (
                    f"Composition: rule {rule.id} v{CHAIN_RULES_VERSION} needs "
                    + " + ".join(FACT_LABELS[need] for need in rule.needs)
                    + f" in one file; together {rule.consequence}."
                ),
                (
                    f"Severity: {SEVERITY_LABELS[composed]} "
                    + (
                        f"(raised from the highest contributing {SEVERITY_LABELS[top]} by this rule)"
                        if SEVERITY_RANK[composed] > SEVERITY_RANK[top]
                        else f"(the highest contributing finding is already {SEVERITY_LABELS[top]})"
                    )
                    + "; display only, it does not gate readiness."
                ),
                "Relation: the facts co-occur in one file; no data flow between them was proven.",
            ]
            chain_id = (
                "chain_"
                + hashlib.sha256(
                    json.dumps([rule.id, CHAIN_RULES_VERSION, path, sorted(members)], separators=(",", ":")).encode()
                ).hexdigest()[:20]
            )
            chains.append(
                FindingChain(
                    id=chain_id,
                    rule=rule,
                    path=path,
                    members=tuple(pair[0] for pair in ordered),
                    member_facts=tuple(pair[1] for pair in ordered),
                    edges=tuple(edges),
                    explanation=tuple(explanation),
                )
            )
    return tuple(chains)


# --------------------------------------------------------------------------- #
# projection
# --------------------------------------------------------------------------- #


def static_severity_signals(
    store: Any,
    revision: ReviewRevision,
    *,
    evidence: Sequence[ReviewEvidence] | None = None,
    revisions: Sequence[ReviewRevision] | None = None,
) -> tuple[SeveritySignal, ...]:
    """Imported findings as P9-PRIO severity signals for the Review triage of *revision*.

    Current, undismissed findings count by their (P9-PRIO) severity and fold
    below the finding threshold by their ``ref`` (``hidden_finding_ids``).
    Stale ones are counted apart and never raise priority. A dismissed
    finding (by fingerprint) is resolved and not a signal.
    """

    current, stale, _checks = snapshot_findings(store, revision, evidence=evidence, revisions=revisions)
    dismissed = {
        key for key, value in dispositions(store, revision.review_id).items() if value["disposition"] == "dismissed"
    }
    out: dict[str, SeveritySignal] = {}
    for row in (*current, *stale):
        key = f"{REF_PREFIX}{row.fingerprint}"
        if row.fingerprint in dismissed or key in out:
            continue
        out[key] = SeveritySignal(
            source="static_analysis",
            key=key,
            producer=STATIC_PRODUCER,
            severity=row.finding.severity,
            title=f"{row.finding.tool} {row.finding.rule_id} at {row.finding.path}:{row.finding.start_line}"[:200],
            revision_id=row.evidence.revision_id,
            stale=row.state == "stale",
            ref=row.ref,
        )
    return tuple(out.values())


def static_findings_projection(store: Any, revision: ReviewRevision, *, path: str = "") -> dict[str, Any]:
    """Current and stale findings, sources (with truncation) and chains for one Snapshot."""

    current, stale, checks = snapshot_findings(store, revision)
    judged = dispositions(store, revision.review_id)
    dismissed = {key for key, value in judged.items() if value.get("disposition") == "dismissed"}
    chains = security_chains(current, excluded=dismissed)

    def scoped(rows: Sequence[StaticFinding]) -> list[StaticFinding]:
        return [row for row in rows if not path or row.finding.path == path]

    in_scope = {row.ref for row in scoped(current)}
    return {
        "review_id": revision.review_id,
        "snapshot_id": revision.id,
        "path": path,
        "findings": [row.to_wire(judged.get(row.fingerprint)) for row in scoped(current)],
        "stale": [row.to_wire(judged.get(row.fingerprint)) for row in scoped(stale)],
        "checks": checks,
        "chains": [
            chain.to_wire() for chain in chains if not path or any(member.ref in in_scope for member in chain.members)
        ],
        "truncation_notes": [
            f"{row['check']}: {row['truncation']['note']}" for row in checks if row["truncation"].get("note")
        ],
        "counts": {
            "current": len(current),
            "stale": len(stale),
            "dismissed": sum(1 for row in current if row.fingerprint in dismissed),
            "chains": len(chains),
        },
    }


def record_disposition(
    store: Any,
    revision: ReviewRevision,
    finding_id: str,
    *,
    disposition: str,
    reason: str,
    actor_id: str,
) -> dict[str, Any]:
    """A human's judgment of one current finding, as an append-only activity event.

    ``dismissed`` needs a reason from the shared dismissal taxonomy
    (``incorrect`` is a false positive). Only a finding current on this
    Snapshot can be judged; a stale one must report again first.
    """

    if disposition not in STATIC_DISPOSITIONS:
        raise ValueError("disposition must be dismissed, confirmed or open")
    if disposition == "dismissed" and (not reason or reason not in FINDING_DISMISSAL_REASONS):
        raise ValueError("a dismissal needs a reason: incorrect, accepted_risk, duplicate or not_applicable")
    if disposition != "dismissed" and reason:
        raise ValueError("a reason applies only to a dismissal")
    current, _stale, _checks = snapshot_findings(store, revision)
    row = next((item for item in current if item.finding.id == finding_id), None)
    if row is None:
        raise LookupError("no current static finding with that id on this Snapshot")
    detail = {
        "finding_id": finding_id,
        "fingerprint": row.fingerprint,
        "evidence_id": row.evidence.id,
        "check_identity": row.check_identity,
        "tool": row.finding.tool,
        "rule_id": row.finding.rule_id,
        "severity": row.finding.severity,
        "disposition": disposition,
        "reason": reason,
    }
    store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=revision.review_id,
            revision_id=revision.id,
            kind="finding.static_disposition",
            actor_id=actor_id,
            actor_type="human",
            subject_type="static_finding",
            subject_id=finding_id,
            summary=f"{disposition} {row.finding.tool} {row.finding.rule_id}".strip()[:200],
            detail_json=json.dumps(detail, sort_keys=True, separators=(",", ":")),
        )
    )
    return detail


# --------------------------------------------------------------------------- #
# investigator context (item 7)
# --------------------------------------------------------------------------- #


def investigator_static_context(
    store: Any, revision: ReviewRevision, paths: Iterable[str]
) -> tuple[list[tuple[str, str, str]], list[dict[str, Any]]]:
    """``(current refs, stale context)`` for one investigation over ``paths``.

    Current rows are ``(ref, label, path)`` an investigator may cite; a
    dismissed finding is not offered. Stale rows carry no ref: they are
    context labelled with the Snapshot they came from and can never be cited
    as current. Messages are untrusted producer text, bounded and passed only
    inside data-tagged blocks.
    """

    wanted = set(paths)
    current, stale, _checks = snapshot_findings(store, revision)
    dismissed = {
        key for key, value in dispositions(store, revision.review_id).items() if value["disposition"] == "dismissed"
    }
    refs = [
        (
            row.ref,
            (
                f"Static finding (deterministic, {row.evidence.attestation_class or 'reported'}) "
                f"{row.finding.tool} {row.finding.rule_id} {SEVERITY_LABELS[row.finding.severity]} at "
                f"{row.finding.path}:{row.finding.start_line} · {row.finding.message[:200]}"
            )[:320],
            row.finding.path,
        )
        for row in current
        if row.finding.path in wanted and row.fingerprint not in dismissed
    ][:MAX_INVESTIGATOR_FINDINGS]
    stale_rows = [
        {
            "state": "stale",
            "snapshot_number": row.snapshot_number,
            "tool": row.finding.tool,
            "rule_id": row.finding.rule_id,
            "severity": row.finding.severity,
            "path": row.finding.path,
            "start_line": row.finding.start_line,
            "message": row.finding.message[:200],
        }
        for row in stale
        if row.finding.path in wanted
    ][:MAX_INVESTIGATOR_FINDINGS]
    return refs, stale_rows


# --------------------------------------------------------------------------- #
# usefulness (item 13)
# --------------------------------------------------------------------------- #


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def rule_usefulness(store: Any, review_ids: Sequence[str]) -> dict[str, Any]:
    """Per rule and per tool: findings, dismissals, false positives and LemonCrow cost.

    Derived on read from ingestion artifacts and disposition events. A finding
    (by fingerprint) counts once per Review however many Snapshots or reruns
    reported it, and wherever its lines moved.
    Cost is LemonCrow compute: ingested results ran elsewhere (provider CI or
    a caller) and cost LemonCrow nothing, which is reported as such; per-rule
    cost is not apportioned (no truthful source exists).
    """

    rules: dict[tuple[str, str], dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    for review_id in list(dict.fromkeys(review_ids))[:_MAX_USEFULNESS_REVIEWS]:
        revisions = tuple(store.list_revisions(review_id))
        evidence = tuple(store.list_evidence(review_id))
        judged = dispositions(store, review_id)
        seen: dict[str, ImportedFinding] = {}
        runs: set[tuple[str, str]] = set()
        for revision in revisions:
            current, _stale, checks = snapshot_findings(store, revision, evidence=evidence, revisions=revisions)
            for check in checks:
                if check["state"] == "current":
                    runs.add((str(check["tool"]), str(check["evidence_id"])))
            for row in current:
                seen.setdefault(row.fingerprint, row.finding)
        for tool, _evidence_id in runs:
            entry = tools.setdefault(tool, {"tool": tool, "runs": 0, "findings": 0, "dismissed": 0})
            entry["runs"] += 1
        for finding in seen.values():
            rule = rules.setdefault(
                (finding.tool, finding.rule_id),
                {
                    "tool": finding.tool,
                    "rule_id": finding.rule_id,
                    "findings": 0,
                    "dismissed": 0,
                    "false_positive": 0,
                    "accepted_risk": 0,
                    "confirmed": 0,
                },
            )
            tool_row = tools.setdefault(finding.tool, {"tool": finding.tool, "runs": 0, "findings": 0, "dismissed": 0})
            rule["findings"] += 1
            tool_row["findings"] += 1
            judgment = judged.get(finding.fingerprint)
            if judgment is None:
                continue
            if judgment["disposition"] == "confirmed":
                rule["confirmed"] += 1
            elif judgment["disposition"] == "dismissed":
                rule["dismissed"] += 1
                tool_row["dismissed"] += 1
                if judgment["reason"] == "incorrect":
                    rule["false_positive"] += 1
                elif judgment["reason"] == "accepted_risk":
                    rule["accepted_risk"] += 1
    rule_rows = [
        {
            **row,
            "dismissal_rate": _rate(row["dismissed"], row["findings"]),
            "false_positive_rate": _rate(row["false_positive"], row["findings"]),
        }
        for row in rules.values()
    ]
    rule_rows.sort(key=lambda row: (-(row["dismissal_rate"] or 0.0), -row["findings"], row["tool"], row["rule_id"]))
    tool_rows = [
        {
            **row,
            "dismissal_rate": _rate(row["dismissed"], row["findings"]),
            "lemoncrow_compute_minutes": 0.0,
            "cost_basis": "ingested result: executed outside LemonCrow, no LemonCrow compute",
        }
        for row in tools.values()
    ]
    tool_rows.sort(key=lambda row: row["tool"])
    return {
        "reviews": min(len(set(review_ids)), _MAX_USEFULNESS_REVIEWS),
        "review_bound": _MAX_USEFULNESS_REVIEWS,
        "rules": rule_rows,
        "tools": tool_rows,
        "basis": "derived on read from imported findings and human dispositions; nothing is counted twice per Review",
    }


__all__ = [
    "CHAIN_RULES",
    "CHAIN_RULES_VERSION",
    "DISPOSITION_EVENT",
    "FACT_CWES",
    "STATIC_DISPOSITIONS",
    "ChainRule",
    "FindingChain",
    "StaticFinding",
    "dispositions",
    "finding_ref",
    "investigator_static_context",
    "record_disposition",
    "rule_usefulness",
    "security_chains",
    "security_facts",
    "snapshot_findings",
    "static_findings_projection",
    "static_severity_signals",
]
