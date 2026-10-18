"""Deterministic PlanArtifact: the structure behind the read-only Brief.

Spec: docs/planning/2026-09-28-evidence-backed-review-design.md §5 (logical
structures), §7 (validator, questions), §8 (evidence language).

The plan embeds ChangeItems, Claims, EvidenceLinks, ReviewQuestions and
read-only proposed decisions. It is a pure function of the Snapshot's targets,
the Phase 2 guidance view, recorded check rows, chapters and Routine proofs,
so it is derived on read and never persisted in this phase. Nothing here
invents an explanation: a group without author guidance says so.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from lemoncrow.pro.capabilities.review.guidance import (
    GuidanceConcern,
    GuidanceDecision,
    GuidanceDefinition,
    GuidanceView,
    RefResolution,
    producer_key,
)
from lemoncrow.pro.capabilities.review.review_priority import (
    TIER_POLICY_BLOCKER,
    TIER_REVIEW_FIRST,
    QueueReason,
)

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.decisions import DecisionState
    from lemoncrow.pro.capabilities.review.routine import RoutineProof
    from lemoncrow.pro.capabilities.review.synthesis import ValidatedSynthesis
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

PLAN_SCHEMA_VERSION = 1

_OFF = frozenset({"0", "false", "no", "off"})


def review_brief_enabled() -> bool:
    """The one review capability gating the Brief (spec §11 rollout).

    ``LEMONCROW_REVIEW_BRIEF=0`` restores the existing Reader; nothing stored
    depends on it, so turning it off loses no marks, evidence or guidance.
    """

    import os

    return os.environ.get("LEMONCROW_REVIEW_BRIEF", "1").strip().lower() not in _OFF


EvidenceSummary = Literal[
    "observed", "author_cited", "inferred", "inferred_failing", "partial", "contradictory", "none"
]
EvidenceMapping = Literal["explicit", "inferred"]
ItemKind = Literal["behavior", "mechanical", "area", "other"]

# Spec §8: the language depends on who established the relationship.
SUMMARY_TEXT: dict[EvidenceSummary, str] = {
    "observed": "Supported by observed evidence",
    "author_cited": "Author cites these recorded checks",
    "inferred": "Related checks passed · relevance inferred",
    # Only an explicit link can contradict a claim; an inferred failure is
    # reported with the same inferred-relevance caveat as an inferred pass.
    "inferred_failing": "Related checks failed · relevance inferred",
    "partial": "Partial evidence",
    "contradictory": "Contradictory evidence",
    "none": "No linked evidence",
}

# Spec §3: the author's attributed claim when no external behavior should change.
INTERNAL_ONLY_TEXT = "Internal implementation only — external behavior intended to remain unchanged."

_OWNING_OUTCOMES = frozenset({"resolved", "renamed", "deleted_file"})
_GAP_OUTCOMES = frozenset({"ambiguous", "symbol_not_found", "not_in_change", "stale_content"})
_AUTHOR_LABEL = "Author's agent note"
_LEMONCROW_LABEL = "LemonCrow observation"


@dataclass(frozen=True)
class EvidenceLink:
    key: str
    title: str
    status: str
    state: str
    snapshot_number: int | None
    mapping: EvidenceMapping
    relevance: str
    # Spec §8: when it ran, who recorded it, and its environment when recorded.
    observed_at: str = ""
    source: str = ""
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Claim:
    id: str
    statement: str
    source: str
    target_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    evidence: tuple[EvidenceLink, ...]
    summary: EvidenceSummary
    result_line: str


@dataclass(frozen=True)
class ChangeItem:
    id: str
    key: str
    kind: ItemKind
    title: str
    before: str
    after: str
    rationale: str
    rationale_origin: Literal["author", "inferred", "none"]
    source_label: str
    producer: str
    stale: bool
    primary_target_ids: tuple[str, ...]
    related_target_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    concerns: tuple[GuidanceConcern, ...]
    unresolved_refs: tuple[RefResolution, ...]
    limitations: tuple[str, ...] = ()
    internal_only: bool = False


@dataclass(frozen=True)
class ReviewQuestion:
    id: str
    reason: str
    title: str
    detail: str
    item_ids: tuple[str, ...]
    target_ids: tuple[str, ...]
    actions: tuple[str, ...]
    tier: int = TIER_REVIEW_FIRST
    review_level: bool = False
    origin: Literal["lemoncrow", "ai"] = "lemoncrow"


@dataclass(frozen=True)
class ProposedDecision:
    """An author-proposed human choice. Read-only until Phase 3b."""

    key: str
    producer: str
    decision: GuidanceDecision
    target_ids: tuple[str, ...]
    stale: bool = False
    read_only: bool = True


@dataclass(frozen=True)
class PlanCoverage:
    target_count: int
    author_explained: int
    fallback_grouped: int
    other: int
    routine: int
    unresolved_refs: int
    synthesized: int = 0
    unsynthesized_target_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanArtifact:
    schema_version: int
    input_digest: str
    intents: tuple[tuple[str, str], ...]
    items: tuple[ChangeItem, ...]
    claims: tuple[Claim, ...]
    decisions: tuple[ProposedDecision, ...]
    questions: tuple[ReviewQuestion, ...]
    coverage: PlanCoverage
    limitations: tuple[str, ...]
    captured_intent_producers: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _resolved(definition: GuidanceDefinition, known: Mapping[str, Any]) -> tuple[str, ...]:
    return _unique(
        [
            target_id
            for resolution in definition.resolutions
            if resolution.outcome in _OWNING_OUTCOMES
            for target_id in resolution.target_ids
            if target_id in known
        ]
    )


def _mentioned(definition: GuidanceDefinition, known: Mapping[str, Any]) -> tuple[str, ...]:
    return _unique(
        [
            target_id
            for resolution in definition.resolutions
            for target_id in resolution.target_ids
            if target_id in known
        ]
    )


def _matches(ref: str, row: Mapping[str, Any]) -> bool:
    artifact = row.get("artifact")
    evidence_id = str(artifact.get("evidence_id") or "") if isinstance(artifact, Mapping) else ""
    return ref in {evidence_id, str(row.get("source_ref") or ""), str(row.get("name") or "")} - {""}


def _current(row: Mapping[str, Any]) -> bool:
    return str(row.get("state") or "") == "current"


def _status(row: Mapping[str, Any]) -> str:
    return str(row.get("status") or "UNKNOWN").upper()


def _snapshot(row: Mapping[str, Any]) -> int | None:
    value = row.get("snapshot_number")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _item_id(key: str) -> str:
    return f"item:{key}"


# --------------------------------------------------------------------------- #
# questions (also feed the canonical queue)
# --------------------------------------------------------------------------- #


def review_questions(
    targets: Sequence[ReviewTarget],
    guidance: GuidanceView,
    checks: Sequence[Mapping[str, Any]],
    *,
    handled_decisions: Collection[str] = (),
) -> tuple[ReviewQuestion, ...]:
    """Investigation questions from sourced records only (spec §7).

    A failed *required* browser scenario is a review-level tier-1 question
    whether or not anything links to it. Conflicts, author-stated concerns and
    failures of explicitly cited checks are tier-3 questions on their targets.
    A decision conflict in *handled_decisions* is carried by its decision
    card instead (one card, one action; accepting it settles the conflict).
    """

    known = {target.target_id: target for target in targets}
    questions: list[ReviewQuestion] = []

    for row in checks:
        if not (_current(row) and row.get("required") and _status(row) == "FAIL"):
            continue
        ref = str(row.get("source_ref") or row.get("name") or "required check")
        linked = [
            definition.key
            for definition in guidance.items
            if definition.item is not None
            and any(_matches(item_ref, row) for item_ref in definition.item.evidence_refs)
        ]
        linked.extend(
            definition.key
            for definition in guidance.decisions
            if definition.decision is not None and ref in definition.decision.relied_checks
        )
        number = _snapshot(row)
        where = f" on Snapshot {number}" if number is not None else ""
        association = (
            f" Linked to: {', '.join(dict.fromkeys(linked))}."
            if linked
            else " No specific change item association is known."
        )
        questions.append(
            ReviewQuestion(
                id=f"q:required:{ref}",
                reason="required_browser_scenario_failed",
                title="Required check failing",
                detail=f"{ref} failed{where}.{association}",
                item_ids=_unique([_item_id(key) for key in linked]),
                target_ids=(),
                actions=("inspect_evidence",),
                tier=TIER_POLICY_BLOCKER,
                review_level=True,
            )
        )

    for conflict in guidance.conflicts:
        if conflict.kind == "decision" and conflict.key in handled_decisions:
            continue
        involved = _unique([target for definition in conflict.definitions for target in _resolved(definition, known)])
        producers = ", ".join(dict.fromkeys(definition.producer for definition in conflict.definitions))
        stale = " All positions were written for earlier content." if conflict.stale else ""
        questions.append(
            ReviewQuestion(
                id=f"q:conflict:{conflict.kind}:{conflict.key}",
                reason="author_guidance_conflict",
                title=f"Authors disagree: {conflict.key}",
                detail=f"{producers} recorded incompatible {conflict.kind} definitions for '{conflict.key}'.{stale}",
                item_ids=(_item_id(conflict.key),) if conflict.kind == "item" else (),
                target_ids=involved,
                actions=("ask", "open_implementation"),
            )
        )

    current_failures = [row for row in checks if _current(row) and _status(row) == "FAIL"]
    for definition in guidance.items:
        item = definition.item
        if item is None or definition.stale:
            continue
        owned = _resolved(definition, known)
        if not owned:
            continue
        for index, concern in enumerate(item.concerns):
            questions.append(
                ReviewQuestion(
                    id=f"q:concern:{item.key}:{index}",
                    reason="claim_evidence_scope_mismatch",
                    title=f"{item.title}: {concern.scope}",
                    detail=concern.reason,
                    item_ids=(_item_id(item.key),),
                    target_ids=owned,
                    actions=("inspect_evidence", "ask", "open_implementation"),
                )
            )
        cited = [row for row in current_failures if any(_matches(ref, row) for ref in item.evidence_refs)]
        for row in cited:
            name = str(row.get("name") or row.get("source_ref") or "check")
            number = _snapshot(row)
            where = f" on Snapshot {number}" if number is not None else ""
            questions.append(
                ReviewQuestion(
                    id=f"q:check:{item.key}:{name}",
                    reason="check_failed_linked",
                    title=f"Linked check failing: {name}",
                    detail=f"{name} failed{where}; the author cites it for '{item.title}'.",
                    item_ids=(_item_id(item.key),),
                    target_ids=owned,
                    actions=("inspect_evidence", "ask", "open_implementation"),
                )
            )
    return tuple(questions)


def question_reasons(questions: Sequence[ReviewQuestion]) -> dict[str, list[QueueReason]]:
    """Tier-3 queue reasons per target; review-level questions have no target."""

    out: dict[str, list[QueueReason]] = {}
    for question in questions:
        if question.review_level:
            continue
        # An AI-raised question is never presented as LemonCrow's own reason.
        detail = f"AI question: {question.title}" if question.origin == "ai" else question.title
        reason = QueueReason(question.reason, detail)
        for target_id in question.target_ids:
            bucket = out.setdefault(target_id, [])
            if reason not in bucket:
                bucket.append(reason)
    return out


# --------------------------------------------------------------------------- #
# claims and evidence
# --------------------------------------------------------------------------- #


def _environment(row: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The environment the recorder captured (a browser run's), never a guess."""

    browser = row.get("browser")
    run = browser.get("run") if isinstance(browser, Mapping) else None
    recorded = run.get("environment") if isinstance(run, Mapping) else None
    if not isinstance(recorded, Mapping):
        return ()
    return tuple((str(name), str(value)) for name, value in recorded.items() if str(value))


def _link(row: Mapping[str, Any], mapping: EvidenceMapping, relevance: str) -> EvidenceLink:
    return EvidenceLink(
        # Typed identity first: two checks sharing a title are two links.
        key=str(row.get("check_key") or row.get("name") or row.get("source_ref") or ""),
        title=str(row.get("name") or row.get("source_ref") or "Check"),
        status=_status(row),
        state=str(row.get("state") or ""),
        snapshot_number=_snapshot(row),
        mapping=mapping,
        relevance=relevance,
        observed_at=str(row.get("observed_at") or ""),
        source=str(row.get("source") or ""),
        environment=_environment(row),
    )


def _result_line(links: Sequence[EvidenceLink], snapshot_number: int | None) -> str:
    current = [link for link in links if link.state == "current"]
    if not current:
        return ""
    total = len(current)
    failed = sum(link.status == "FAIL" for link in current)
    passed = sum(link.status == "PASS" for link in current)
    number = next((link.snapshot_number for link in current if link.snapshot_number is not None), snapshot_number)
    where = f" on Snapshot {number}" if number is not None else ""
    noun = "linked check" if total == 1 else "linked checks"
    if failed:
        return f"{failed} of {total} {noun} failed{where}"
    if passed == total:
        return f"{total} {noun} passed{where}"
    return f"{passed} of {total} {noun} passed{where}; {total - passed} not settled"


def _evidence_summary(links: Sequence[EvidenceLink], *, concerns: bool) -> EvidenceSummary:
    """Spec §8: current results only; only an explicit link can contradict."""

    current = [link for link in links if link.state == "current"]
    if not current:
        return "none"
    if any(link.status == "FAIL" and link.mapping == "explicit" for link in current):
        return "contradictory"
    if any(link.status == "FAIL" for link in current):
        return "inferred_failing"
    if any(link.status != "PASS" for link in current):
        return "partial"
    if any(link.mapping == "explicit" for link in current):
        return "partial" if concerns else "author_cited"
    return "inferred"


def _claim(
    definition: GuidanceDefinition,
    primary: Sequence[str],
    known: Mapping[str, ReviewTarget],
    checks: Sequence[Mapping[str, Any]],
    snapshot_number: int | None,
) -> Claim:
    item = definition.item
    if item is None:
        raise ValueError("a claim needs an item definition")
    links: list[EvidenceLink] = []
    seen: set[tuple[str, str, int | None]] = set()
    limitations: list[str] = []

    def add(link: EvidenceLink) -> None:
        identity = (link.key, link.state, link.snapshot_number)
        if identity not in seen:
            seen.add(identity)
            links.append(link)

    for ref in item.evidence_refs:
        matched = [row for row in checks if _matches(ref, row)]
        if not matched:
            limitations.append(f"Referenced evidence '{ref}' is not recorded on this Snapshot.")
            continue
        for row in matched:
            reported = str(row.get("state") or "") == "session"
            add(
                _link(
                    row,
                    "explicit",
                    (
                        "Cited by the author; agent-reported during authoring, not a captured run"
                        if reported
                        else "Cited by the author"
                    ),
                )
            )
    paths = {known[target_id].path for target_id in primary if target_id in known}
    for row in checks:
        path = str(row.get("path") or "")
        if _current(row) and path and path in paths:
            add(_link(row, "inferred", f"Recorded for {path}; relevance inferred"))
    for concern in item.concerns:
        limitations.append(f"Not covered: {concern.scope} — {concern.reason}")

    summary = _evidence_summary(links, concerns=bool(item.concerns))
    if summary == "none":
        if any(link.state == "session" for link in links):
            limitations.append("Agent-reported during authoring; not observed on this Snapshot.")
        if any(link.state not in ("current", "session") for link in links):
            limitations.append("Linked results are from an earlier Snapshot.")
    return Claim(
        id=f"claim:{item.key}",
        statement=INTERNAL_ONLY_TEXT if item.internal_only else (item.after or item.title),
        source=definition.producer,
        target_ids=tuple(primary),
        limitations=tuple(limitations),
        evidence=tuple(links),
        summary=summary,
        result_line=_result_line(links, snapshot_number),
    )


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


def _narrative(guidance: GuidanceView) -> list[GuidanceDefinition]:
    position: dict[str, int] = {}
    for record in guidance.records:
        for key in record.payload.suggested_order:
            position.setdefault(key, len(position))
    return sorted(
        (definition for definition in guidance.items if definition.item is not None),
        key=lambda definition: (position.get(definition.key, len(position)), definition.created_at, definition.key),
    )


def _author_ownership(
    guidance: GuidanceView, known: Mapping[str, Any]
) -> list[tuple[GuidanceDefinition, tuple[str, ...]]]:
    """Each author item in narrative order with the targets it primarily owns."""

    owned: set[str] = set()
    rows: list[tuple[GuidanceDefinition, tuple[str, ...]]] = []
    for definition in _narrative(guidance):
        candidates = () if definition.stale else _resolved(definition, known)
        primary = tuple(target_id for target_id in candidates if target_id not in owned)
        owned.update(primary)
        rows.append((definition, primary))
    return rows


def narrative_positions(targets: Sequence[ReviewTarget], guidance: GuidanceView) -> dict[str, int]:
    """Target id -> narrative position of the author item that owns it.

    Ownership is the plan's own rule, so the queue and the Brief agree on
    which item a shared target belongs to. Unowned targets are absent.
    """

    known = {target.target_id: target for target in targets}
    return {
        target_id: index
        for index, (_, primary) in enumerate(_author_ownership(guidance, known))
        for target_id in primary
    }


def _group(
    item_id: str, kind: ItemKind, title: str, rationale: str, primary: Sequence[str], source_label: str
) -> ChangeItem:
    return ChangeItem(
        id=item_id,
        key=item_id,
        kind=kind,
        title=title,
        before="",
        after="",
        rationale=rationale,
        rationale_origin="none",
        source_label=source_label,
        producer="lemoncrow",
        stale=False,
        primary_target_ids=tuple(primary),
        related_target_ids=(),
        claim_ids=(),
        concerns=(),
        unresolved_refs=(),
    )


def _digest(
    targets: Sequence[ReviewTarget],
    guidance: GuidanceView,
    checks: Sequence[Mapping[str, Any]],
    chapters: Sequence[Mapping[str, Any]],
    routine_proofs: Mapping[str, RoutineProof],
) -> str:
    payload = {
        "v": PLAN_SCHEMA_VERSION,
        "targets": sorted([target.target_id, target.fingerprint_method, target.state] for target in targets),
        "guidance": sorted(record.id for record in guidance.records),
        "checks": sorted(
            [str(row.get("name") or ""), _status(row), str(row.get("state") or ""), _snapshot(row) or 0]
            for row in checks
        ),
        "proofs": sorted([target_id, proof.rule] for target_id, proof in routine_proofs.items()),
        "chapters": [str(chapter.get("key") or "") for chapter in chapters],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_review_plan(
    *,
    targets: Sequence[ReviewTarget],
    guidance: GuidanceView,
    checks: Sequence[Mapping[str, Any]],
    chapters: Sequence[Mapping[str, Any]],
    routine_proofs: Mapping[str, RoutineProof],
    snapshot_number: int | None,
    handled_decisions: Collection[str] = (),
) -> PlanArtifact:
    """Build the deterministic plan; every target gets exactly one primary item."""

    known = {target.target_id: target for target in targets}
    owner: dict[str, str] = {}
    items: list[ChangeItem] = []
    claims: list[Claim] = []

    for definition, primary in _author_ownership(guidance, known):
        item = definition.item
        if item is None:
            continue
        item_id = _item_id(item.key)
        for target_id in primary:
            owner[target_id] = item_id
        related = tuple(target_id for target_id in _mentioned(definition, known) if target_id not in primary)
        claim = _claim(definition, primary, known, checks, snapshot_number)
        claims.append(claim)
        items.append(
            ChangeItem(
                id=item_id,
                key=item.key,
                kind="behavior",
                title=item.title,
                before=item.before,
                after=item.after,
                rationale=item.rationale,
                rationale_origin="author",
                source_label=_AUTHOR_LABEL,
                producer=definition.producer,
                stale=definition.stale,
                primary_target_ids=primary,
                related_target_ids=related,
                claim_ids=(claim.id,),
                concerns=item.concerns,
                unresolved_refs=tuple(
                    resolution for resolution in definition.resolutions if resolution.outcome in _GAP_OUTCOMES
                ),
                internal_only=item.internal_only,
            )
        )
    author_explained = len(owner)

    routine = [
        target.target_id for target in targets if target.target_id in routine_proofs and target.target_id not in owner
    ]
    if routine:
        for target_id in routine:
            owner[target_id] = "group:routine"
        items.append(
            _group(
                "group:routine",
                "mechanical",
                "Routine changes",
                "Proven comment-only or formatting-only by a parser; collapsed until you open them.",
                routine,
                _LEMONCROW_LABEL,
            )
        )

    fallback = 0
    for chapter in chapters:
        rows = chapter.get("targets") or ()
        ids = [
            str(row.get("target_id") or "")
            for row in rows
            if isinstance(row, Mapping) and str(row.get("target_id") or "") in known
        ]
        primary_ids = [target_id for target_id in _unique(ids) if target_id not in owner]
        if not primary_ids:
            continue
        item_id = f"area:{chapter.get('key') or chapter.get('label') or len(items)}"
        for target_id in primary_ids:
            owner[target_id] = item_id
        fallback += len(primary_ids)
        items.append(
            _group(item_id, "area", str(chapter.get("label") or "Implementation"), "", primary_ids, _LEMONCROW_LABEL)
        )

    other = [target.target_id for target in targets if target.target_id not in owner]
    if other:
        items.append(_group("group:other", "other", "Other implementation changes", "", other, _LEMONCROW_LABEL))

    decisions = tuple(
        ProposedDecision(
            key=definition.key,
            producer=definition.producer,
            decision=definition.decision,
            target_ids=_resolved(definition, known),
            stale=definition.stale,
        )
        for definition in guidance.decisions
        if definition.decision is not None
    )
    intents: dict[str, str] = {}
    for record in guidance.records:
        if record.payload.intent:
            intents[producer_key(record)] = record.payload.intent

    return PlanArtifact(
        schema_version=PLAN_SCHEMA_VERSION,
        input_digest=_digest(targets, guidance, checks, chapters, routine_proofs),
        intents=tuple(intents.items()),
        items=tuple(items),
        claims=tuple(claims),
        decisions=decisions,
        questions=review_questions(targets, guidance, checks, handled_decisions=handled_decisions),
        coverage=PlanCoverage(
            target_count=len(targets),
            author_explained=author_explained,
            fallback_grouped=fallback,
            other=len(other),
            routine=len(routine),
            unresolved_refs=sum(len(item.unresolved_refs) for item in items),
        ),
        limitations=guidance.limitations,
    )


def apply_agent_context(plan: PlanArtifact, rows: Sequence[Mapping[str, str]]) -> PlanArtifact:
    """Overlay captured author-visible prose without pretending it is guidance.

    Plan-role output contributes a short intent only when explicit guidance from
    that producer did not already provide one. Other author output becomes an
    explanation-only Brief item, which naturally lands in Understand because it
    owns no ReviewTarget. Reviewer-agent output remains in annotations/comments.
    """

    intents = dict(plan.intents)
    items = list(plan.items)
    accepted: list[tuple[str, str, str, str, str, str, str, str]] = []
    captured_intents = set(plan.captured_intent_producers)
    existing_ids = {item.id for item in items}

    for row in rows:
        if str(row.get("source") or "") != "author":
            continue
        row_id = str(row.get("id") or "")
        producer = str(row.get("producer") or "agent")
        role = str(row.get("role") or "unknown")
        title = str(row.get("title") or "").strip()
        body = str(row.get("body") or "").strip()
        kind = str(row.get("kind") or "context")
        if not row_id or not title:
            continue
        stale = str(row.get("stale") or "").casefold() == "true"
        accepted.append((row_id, str(row.get("message_id") or ""), producer, role, kind, title, body, str(stale)))
        if kind == "intent":
            if producer not in intents:
                intents[producer] = title
                captured_intents.add(producer)
            continue

        item_id = f"agent:{row_id}"
        if item_id in existing_ids:
            continue
        existing_ids.add(item_id)
        role_label = role.replace("_", " ").replace("-", " ").strip().title()
        source_label = (
            "Captured author" if not role_label or role.casefold() == "unknown" else f"Captured author · {role_label}"
        )
        items.append(
            ChangeItem(
                id=item_id,
                key=item_id,
                kind="behavior",
                title=title,
                before="",
                after="",
                rationale=body,
                rationale_origin="author",
                source_label=source_label,
                producer=producer,
                stale=stale,
                primary_target_ids=(),
                related_target_ids=(),
                claim_ids=(),
                concerns=(),
                unresolved_refs=(),
            )
        )

    if not accepted:
        return plan
    digest_payload = json.dumps(
        {"plan": plan.input_digest, "agent_context": accepted},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return replace(
        plan,
        input_digest=hashlib.sha256(digest_payload.encode("utf-8")).hexdigest(),
        intents=tuple(intents.items()),
        items=tuple(items),
        captured_intent_producers=tuple(sorted(captured_intents)),
    )


def apply_synthesis(
    plan: PlanArtifact,
    synthesis: ValidatedSynthesis,
    checks: Sequence[Mapping[str, Any]],
) -> PlanArtifact:
    """Overlay a validated synthesis; every fact still comes from records.

    Explanations fill only groups the author did not explain, labelled as
    inferred. An inferred relevance link carries the *recorded* status and
    state of its check row; the model never supplies a result. Questions are
    tier 3 at most and marked as AI-raised.
    """

    from dataclasses import replace

    from lemoncrow.pro.capabilities.review.synthesis import SYNTHESIS_LIMITATION, SYNTHESIS_SOURCE_LABEL

    items: list[ChangeItem] = []
    synthesized = 0
    for item in plan.items:
        explanation = synthesis.explanations.get(item.id)
        if explanation is None or item.kind not in ("area", "other") or item.rationale_origin != "none":
            items.append(item)
            continue
        synthesized += len(explanation.target_ids)
        items.append(
            replace(
                item,
                rationale=explanation.summary,
                rationale_origin="inferred",
                source_label=SYNTHESIS_SOURCE_LABEL,
                limitations=(*item.limitations, SYNTHESIS_LIMITATION),
            )
        )

    concerned = {claim_id for item in plan.items if item.concerns for claim_id in item.claim_ids}
    claims: list[Claim] = []
    for claim in plan.claims:
        links = list(claim.evidence)
        seen = {(link.key, link.state, link.snapshot_number) for link in links}
        for inferred in synthesis.relevance.get(claim.id, ()):
            for row in checks:
                if str(row.get("name") or row.get("source_ref") or "") != inferred.check_key:
                    continue
                why = f"AI-inferred relevance: {inferred.why}" if inferred.why else "AI-inferred relevance"
                link = _link(row, "inferred", why)
                if (link.key, link.state, link.snapshot_number) not in seen:
                    seen.add((link.key, link.state, link.snapshot_number))
                    links.append(link)
        if len(links) == len(claim.evidence):
            claims.append(claim)
            continue
        claims.append(
            replace(
                claim,
                evidence=tuple(links),
                summary=_evidence_summary(links, concerns=claim.id in concerned),
                result_line=_result_line(links, None),
            )
        )

    return replace(
        plan,
        items=tuple(items),
        claims=tuple(claims),
        questions=(*plan.questions, *synthesis.questions),
        coverage=replace(
            plan.coverage,
            synthesized=synthesized,
            unsynthesized_target_ids=tuple(synthesis.unsynthesized_target_ids),
        ),
    )


# --------------------------------------------------------------------------- #
# projection (spec §11: versioned, server-ordered, no client re-ranking)
# --------------------------------------------------------------------------- #

PROJECTION_VERSION = 1


def _resolution_dict(resolution: RefResolution) -> dict[str, Any]:
    ref = resolution.ref
    return {
        "ref": {"path": ref.path, "symbol": ref.symbol, "start_line": ref.start_line, "end_line": ref.end_line},
        "outcome": resolution.outcome,
        "target_ids": list(resolution.target_ids),
        "detail": resolution.detail,
    }


def _question_dict(question: ReviewQuestion) -> dict[str, Any]:
    return {
        "id": question.id,
        "reason": question.reason,
        "title": question.title,
        "detail": question.detail,
        "item_ids": list(question.item_ids),
        "target_ids": list(question.target_ids),
        "actions": list(question.actions),
        "tier": question.tier,
        "origin": question.origin,
    }


def projection_payload(
    plan: PlanArtifact,
    targets: Sequence[ReviewTarget],
    *,
    review_id: str,
    revision_id: str,
    snapshot_number: int | None,
    historical: bool,
    decision_states: Sequence[DecisionState] | None = None,
    revision_numbers: Mapping[str, int] | None = None,
    synthesis: Mapping[str, Any] | None = None,
    delta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The Brief's payload: plan structure ordered by the canonical queue.

    *synthesis* is the status block for AI explanations (Phase 4); the plan
    must already carry any overlay (``apply_synthesis``).

    *targets* must carry their final priorities. Item attention and order
    come from their targets' queue placement, never from item prose.
    With *decision_states* (Phase 3b) decisions carry their human
    disposition, derived validity and the actions the server allows.
    """

    from lemoncrow.pro.capabilities.review.review_priority import TIER_DONE, TIER_UNDERSTAND, attention_for_tier

    by_id = {target.target_id: target for target in targets}
    queue = [target.target_id for target in sorted(targets, key=lambda target: target.priority.position)]
    position = {target_id: index for index, target_id in enumerate(queue)}
    far = len(queue) + 1
    questions_by_item: dict[str, list[str]] = {}
    for question in plan.questions:
        for item_id in question.item_ids:
            questions_by_item.setdefault(item_id, []).append(question.id)

    def placement(item: ChangeItem) -> tuple[int, int, str]:
        scope = [by_id[target_id] for target_id in item.primary_target_ids if target_id in by_id] or [
            by_id[target_id] for target_id in item.related_target_ids if target_id in by_id
        ]
        outstanding = [target for target in scope if target.priority.outstanding]
        if outstanding:
            tier = min(target.priority.tier for target in outstanding)
            first = min(position.get(target.target_id, far) for target in outstanding)
            return tier, first, attention_for_tier(tier)
        if not scope:
            return TIER_UNDERSTAND, far, "understand"  # explanation-only item
        return TIER_DONE, min(position.get(target.target_id, far) for target in scope), "done"

    placed = sorted(((placement(item), index, item) for index, item in enumerate(plan.items)), key=lambda row: row[:2])
    claims = {claim.id: claim for claim in plan.claims}
    items_payload: list[dict[str, Any]] = []
    counts = {"review_first": 0, "understand": 0, "routine": 0, "done": 0}
    for (tier, first, attention), _, item in placed:
        counts[attention] = counts.get(attention, 0) + 1
        items_payload.append(
            {
                "id": item.id,
                "key": item.key,
                "kind": item.kind,
                "title": item.title,
                "before": item.before,
                "after": item.after,
                "rationale": item.rationale,
                "rationale_origin": item.rationale_origin,
                "source_label": item.source_label,
                "producer": item.producer,
                "stale": item.stale,
                "attention": attention,
                "tier": tier,
                "position": first if first < far else None,
                "primary_target_ids": sorted(
                    item.primary_target_ids, key=lambda target_id: position.get(target_id, far)
                ),
                "related_target_ids": list(item.related_target_ids),
                "claim_ids": list(item.claim_ids),
                "question_ids": questions_by_item.get(item.id, []),
                "concerns": [{"scope": concern.scope, "reason": concern.reason} for concern in item.concerns],
                "unresolved_refs": [_resolution_dict(resolution) for resolution in item.unresolved_refs],
                "limitations": list(item.limitations),
                "internal_only": item.internal_only,
            }
        )

    claims_payload = [
        {
            "id": claim.id,
            "statement": claim.statement,
            "source": claim.source,
            "target_ids": list(claim.target_ids),
            "limitations": list(claim.limitations),
            "evidence": [
                {
                    "key": link.key,
                    "title": link.title,
                    "status": link.status,
                    "state": link.state,
                    "snapshot_number": link.snapshot_number,
                    "mapping": link.mapping,
                    "relevance": link.relevance,
                    "observed_at": link.observed_at,
                    "source": link.source,
                    "environment": dict(link.environment),
                }
                for link in claim.evidence
            ],
            "summary": claim.summary,
            "summary_text": SUMMARY_TEXT[claim.summary],
            "result_line": claim.result_line,
        }
        for claim in claims.values()
    ]
    numbers = revision_numbers or {}
    if decision_states is not None:
        from lemoncrow.pro.capabilities.review.decisions import decision_actions

        def lead(state: DecisionState) -> int:
            return min((position.get(target_id, far) for target_id in state.target_ids), default=far)

        ranked = sorted(
            enumerate(decision_states),
            key=lambda row: (row[1].withdrawn, row[1].tier, lead(row[1]), row[0]),
        )
        decisions_payload = [decision_state_payload(state, position, numbers, far) for _, state in ranked]
        decision_entries = decision_actions([state for _, state in ranked])
    else:
        decision_entries = []
        decisions_payload = [
            {
                "key": proposed.key,
                "producer": proposed.producer,
                "question": proposed.decision.question,
                "options": [{"key": option.key, "label": option.label} for option in proposed.decision.options],
                "selected_option": proposed.decision.selected_option,
                "reason_human_required": proposed.decision.reason_human_required,
                "assumptions": [
                    {"key": assumption.key, "value": assumption.value, "statement": assumption.statement}
                    for assumption in proposed.decision.assumptions
                ],
                "contract_scope": list(proposed.decision.contract_scope),
                "relied_checks": list(proposed.decision.relied_checks),
                "target_ids": sorted(proposed.target_ids, key=lambda target_id: position.get(target_id, far)),
                "stale": proposed.stale,
                "read_only": proposed.read_only,
            }
            for proposed in plan.decisions
        ]
    review_level = [_question_dict(question) for question in plan.questions if question.review_level]
    actions = [
        row
        for _, row in sorted(enumerate([*review_level, *decision_entries]), key=lambda pair: (pair[1]["tier"], pair[0]))
    ]
    states = list(decision_states or ())
    progress = {
        "total": sum(1 for state in states if not state.withdrawn),
        "settled": sum(
            1 for state in states if not state.withdrawn and state.disposition == "accepted" and not state.outstanding
        ),
        "outstanding": sum(1 for state in states if state.outstanding),
        "deferred": sum(1 for state in states if state.disposition == "deferred" and not state.navigable),
        "withdrawn": sum(1 for state in states if state.withdrawn),
    }
    coverage = plan.coverage
    delta_payload: dict[str, Any] | None = None
    if delta is not None:
        # Spec §9 step 5: name the behavior and items the new Snapshot moved,
        # each moved target under the one item that owns it, in Brief order.
        changed = {str(target_id) for target_id in delta.get("changed") or ()}
        added = {str(target_id) for target_id in delta.get("added") or ()}
        touched = []
        for _, _, item in placed:
            primary = sorted(item.primary_target_ids, key=lambda target_id: position.get(target_id, far))
            moved = [target_id for target_id in primary if target_id in changed]
            new = [target_id for target_id in primary if target_id in added]
            if moved or new:
                touched.append({"id": item.id, "title": item.title, "kind": item.kind, "changed": moved, "added": new})
        delta_payload = {**delta, "items": touched}
    return {
        "projection_version": PROJECTION_VERSION,
        "review_id": review_id,
        "revision_id": revision_id,
        "snapshot_number": snapshot_number,
        "historical": historical,
        "plan": {
            "schema_version": plan.schema_version,
            "input_digest": plan.input_digest,
            "intents": [
                {
                    "producer": producer,
                    "intent": intent,
                    "origin": "captured" if producer in plan.captured_intent_producers else "guidance",
                }
                for producer, intent in plan.intents
            ],
            "limitations": list(plan.limitations),
        },
        "actions": actions,
        "questions": [_question_dict(question) for question in plan.questions if not question.review_level],
        "items": items_payload,
        "claims": claims_payload,
        "decisions": decisions_payload,
        "coverage": {
            "target_count": coverage.target_count,
            "author_explained": coverage.author_explained,
            "fallback_grouped": coverage.fallback_grouped,
            "other": coverage.other,
            "routine": coverage.routine,
            "unresolved_refs": coverage.unresolved_refs,
            "unexplained_targets": coverage.target_count - coverage.author_explained - coverage.routine,
            "file_count": len({target.path for target in targets}),
            "synthesized": coverage.synthesized,
            "unsynthesized_target_ids": list(coverage.unsynthesized_target_ids),
        },
        "synthesis": dict(synthesis) if synthesis is not None else {"status": "none"},
        # Phase 5: what changed since the previous Snapshot (null on the first).
        "delta": delta_payload,
        "counts": {
            **counts,
            "decisions": progress["total"] if decision_states is not None else len(plan.decisions),
        },
        "decision_progress": progress,
        "queue": queue,
    }


def decision_state_payload(
    state: DecisionState,
    position: Mapping[str, int] | None = None,
    numbers: Mapping[str, int] | None = None,
    far: int = 1 << 30,
) -> dict[str, Any]:
    """One decision as the projection and the decision API render it."""

    from lemoncrow.pro.capabilities.review.decisions import semantic_fingerprint

    position = position or {}
    numbers = numbers or {}
    latest = state.latest_event
    number = numbers.get(latest.revision_id) if latest is not None else None
    return {
        "key": state.key,
        "producer": state.producer,
        "question": state.question,
        "options": [{"key": key, "label": label} for key, label in state.options],
        "selected_option": state.author_selected,
        "reason_human_required": state.reason_human_required,
        "assumptions": [
            {"key": item.key, "value": item.value, "statement": item.statement} for item in state.assumptions
        ],
        "contract_scope": list(state.contract_scope),
        "relied_checks": list(state.relied_checks),
        "target_ids": sorted(state.target_ids, key=lambda target_id: position.get(target_id, far)),
        "stale": state.stale,
        "read_only": all(action == "ask" for action in state.available_actions),
        "conflict": state.conflict,
        "positions": [
            {
                "producer": item.producer,
                "selected_option": item.selected_option,
                "option_keys": list(item.option_keys),
                "assumptions": [{"key": key, "value": value} for key, value in item.assumptions],
            }
            for item in state.positions
        ],
        "state": {
            "disposition": state.disposition,
            "validity": state.validity,
            "changed_inputs": list(state.changed_inputs),
            "outstanding": state.outstanding,
            "navigable": state.navigable,
            "withdrawn": state.withdrawn,
            "trigger": (
                {"kind": state.trigger_kind, "check_key": state.trigger_check_key, "note": state.trigger_note}
                if state.disposition == "deferred"
                else None
            ),
            "trigger_fired": state.trigger_fired,
            "accepted_option": state.accepted_option,
            "latest_event_id": latest.id if latest is not None else "",
            # The scope this card shows; a write must name it (stale-scope CAS).
            "semantic_fingerprint": semantic_fingerprint(state.scope) if state.scope is not None else "",
            "latest_action": latest.action if latest is not None else "",
            "latest_reason": latest.reason if latest is not None else "",
            "event_snapshot_number": max(0, number - 1) if number is not None else None,
            "reason": state.reason_code,
            "detail": state.reason_detail,
            "tier": state.tier,
            "limitations": list(state.limitations),
        },
        "actions": list(state.available_actions),
    }


__all__ = [
    "INTERNAL_ONLY_TEXT",
    "PLAN_SCHEMA_VERSION",
    "PROJECTION_VERSION",
    "SUMMARY_TEXT",
    "ChangeItem",
    "Claim",
    "EvidenceLink",
    "EvidenceSummary",
    "PlanArtifact",
    "PlanCoverage",
    "ProposedDecision",
    "ReviewQuestion",
    "apply_synthesis",
    "build_review_plan",
    "decision_state_payload",
    "narrative_positions",
    "projection_payload",
    "question_reasons",
    "review_brief_enabled",
    "review_questions",
]
