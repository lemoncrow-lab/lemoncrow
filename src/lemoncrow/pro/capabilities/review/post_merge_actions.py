"""Optional post-merge follow-ups (Phase 12, P12-AUTO item 7).

A post-merge action (a notification, a drafted changelog entry, a ticket
update) runs only after the connected provider reported the Change merged. It
is never a readiness condition: nothing here is read by the readiness core,
and every run records the readiness-record digest before and after it so the
record proves the action did not move it.

Who runs it: a human, explicitly, from the Review (``initiated_by="human"``),
or an organization automation rule that fired for this merge
(``initiated_by="rule"``, see :mod:`automation_rules`). A rule run is checked
against the rule's acting identity exactly as the human route checks a person,
and is recorded with its rule id; a refused automated run is recorded too.

Durable state: ``review_derived`` rows of kind :data:`POST_MERGE_ROW_KIND`
under the merged Snapshot. Writer: :class:`PostMergeLedger` (insert-if-absent,
then compare-and-set transitions). Closer: the Snapshot/Review deletion
cascade. Activity events are the human-visible history of each run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from lemoncrow.pro.capabilities.review.automation_rules import (
    ACTION_LABELS,
    ActingIdentity,
    AutomationDecision,
    AutomationFacts,
    AutomationRule,
    BranchProtectionFacts,
    PermissionCheck,
    decide_automation,
)
from lemoncrow.pro.capabilities.review.landing import landing_metadata, landing_relationship

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession

POST_MERGE_ROW_KIND: Final[str] = "post_merge_action"
_ROW_STAMP: Final[str] = "pm1"

PostMergeKind = Literal["notification", "changelog_entry", "ticket_update"]
POST_MERGE_KINDS: Final[tuple[PostMergeKind, ...]] = ("notification", "changelog_entry", "ticket_update")
PostMergeStatus = Literal["running", "completed", "failed", "unavailable", "refused"]
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed", "unavailable", "refused"})
_RETRYABLE: Final[frozenset[str]] = frozenset({"failed", "unavailable", "refused"})
Initiator = Literal["human", "rule"]

POST_MERGE_ACTIVITY: Final[str] = "post_merge.action"
AUTOMATION_REFUSED_ACTIVITY: Final[str] = "automation.refused"


def action_for(kind: str) -> str:
    return f"post_merge.{kind}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Provider-observed merge
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderMergeFact:
    """A composition's provider truth for one Change (hosted: the provider subject row)."""

    provider: str
    state: str
    """``open``, ``closed``, ``merged`` or ``unknown`` as the provider last reported."""
    head_sha: str = ""
    observed_at: str = ""
    external_id: str = ""


@dataclass(frozen=True, slots=True)
class MergeObservation:
    """A merge the connected provider reported; never inferred from local Git."""

    source: Literal["provider_landing", "provider_subject"]
    provider: str
    merged_head: str
    reviewed_head: str
    revision_id: str
    observed_at: str
    ref: str
    detail: str = ""
    provider_confirmed_reviewed_head: bool = False
    """The provider itself confirmed it merged exactly the reviewed head (landing ``merged``)."""

    @property
    def reviewed_head_merged(self) -> bool:
        if self.provider_confirmed_reviewed_head:
            return True
        return bool(self.merged_head) and self.merged_head == self.reviewed_head

    def to_wire(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "provider": self.provider,
            "merged_head": self.merged_head,
            "reviewed_head": self.reviewed_head,
            "reviewed_head_merged": self.reviewed_head_merged,
            "revision_id": self.revision_id,
            "observed_at": self.observed_at,
            "ref": self.ref,
            "detail": self.detail,
        }


def observed_merge(
    store: Any,
    session: ReviewSession,
    *,
    provider_fact: ProviderMergeFact | None = None,
) -> MergeObservation | None:
    """The provider-observed merge of this Change, or ``None``.

    Two sources only, both provider truth: the landing intent settled
    ``merged`` from a provider answer (the provider merged exactly the reviewed
    head), or the composition's provider subject reporting ``merged``.
    """

    landing = landing_relationship(store, session.id)
    metadata = landing_metadata(landing)
    if landing is not None and landing.status == "merged" and metadata.get("provider_state") == "merged":
        revision_id = str(metadata.get("revision_id") or "")
        revision = store.get_revision(revision_id) if revision_id else None
        head = str(getattr(revision, "head_sha", "") or "")
        return MergeObservation(
            source="provider_landing",
            provider=str(landing.provider or ""),
            # ``merged`` is written only when the provider merged the exact
            # reviewed head; an unreviewed merge settles ``blocked`` instead.
            merged_head=head,
            reviewed_head=head,
            revision_id=revision_id,
            observed_at=str(landing.updated_at or ""),
            ref=f"landing:{landing.id}:{head or revision_id}",
            detail=str(metadata.get("detail") or ""),
            provider_confirmed_reviewed_head=True,
        )
    if provider_fact is not None and provider_fact.state == "merged":
        latest = store.latest_revision(session.id)
        reviewed = str(getattr(latest, "head_sha", "") or "")
        merged_head = provider_fact.head_sha
        return MergeObservation(
            source="provider_subject",
            provider=provider_fact.provider,
            merged_head=merged_head,
            reviewed_head=reviewed,
            revision_id=str(getattr(latest, "id", "") or ""),
            observed_at=provider_fact.observed_at,
            ref=f"{provider_fact.provider}:{provider_fact.external_id or session.id}:{merged_head or 'unknown'}",
            detail=f"{provider_fact.provider or 'The provider'} reported the Change merged.",
        )
    return None


def readiness_record_digest(store: Any, review_id: str, readiness: Mapping[str, Any] | None = None) -> str:
    """Digest of the merge-readiness record: the stored landing record plus the evaluated conditions.

    The time-varying evaluation instant is excluded; the condition digest
    (which binds the policy generation) and the durable landing row are not.
    """

    landing = landing_relationship(store, review_id)
    document: dict[str, Any] = {
        "landing": (
            None
            if landing is None
            else {"status": landing.status, "metadata": landing.metadata_json, "updated_at": landing.updated_at}
        ),
    }
    if readiness is not None:
        generation = readiness.get("policy_generation")
        document["conditions_hash"] = str(readiness.get("conditions_hash") or "")
        document["policy_generation"] = (
            str(generation.get("digest") or "") if isinstance(generation, Mapping) else str(generation or "")
        )
        document["ready"] = bool(readiness.get("ready"))
    return _digest(document)


# --------------------------------------------------------------------------- #
# Durable record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostMergeRecord:
    id: str
    review_id: str
    revision_id: str
    kind: PostMergeKind
    status: PostMergeStatus
    initiated_by: Initiator
    actor_id: str
    merge_ref: str
    merge_source: str = ""
    rule_id: str = ""
    rule_digest: str = ""
    acts_as: Mapping[str, str] = field(default_factory=dict)
    permission: str = ""
    detail: str = ""
    output: Mapping[str, Any] = field(default_factory=dict)
    readiness_digest_before: str = ""
    readiness_digest_after: str = ""
    attempts: int = 1
    created_at: str = ""
    updated_at: str = ""
    version: int = 1

    @property
    def readiness_unchanged(self) -> bool | None:
        if not self.readiness_digest_before or not self.readiness_digest_after:
            return None
        return self.readiness_digest_before == self.readiness_digest_after

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "kind": self.kind,
            "label": ACTION_LABELS[action_for(self.kind)],
            "status": self.status,
            "initiated_by": self.initiated_by,
            "actor_id": self.actor_id,
            "merge_ref": self.merge_ref,
            "merge_source": self.merge_source,
            "rule_id": self.rule_id,
            "rule_digest": self.rule_digest,
            "acts_as": dict(self.acts_as),
            "permission": self.permission,
            "detail": self.detail,
            "output": dict(self.output),
            "readiness_digest_before": self.readiness_digest_before,
            "readiness_digest_after": self.readiness_digest_after,
            "readiness_unchanged": self.readiness_unchanged,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> PostMergeRecord:
        kind = str(raw["kind"])
        status = str(raw["status"])
        initiated_by = str(raw["initiated_by"])
        if kind not in POST_MERGE_KINDS or status not in {"running", *TERMINAL_STATUSES}:
            raise ValueError("not a post-merge record")
        if initiated_by not in {"human", "rule"}:
            raise ValueError("unknown initiator")
        acts_as = raw.get("acts_as") or {}
        output = raw.get("output") or {}
        return cls(
            id=str(raw["id"]),
            review_id=str(raw["review_id"]),
            revision_id=str(raw.get("revision_id") or ""),
            kind=kind,
            status=cast(PostMergeStatus, status),
            initiated_by=cast(Initiator, initiated_by),
            actor_id=str(raw.get("actor_id") or ""),
            merge_ref=str(raw.get("merge_ref") or ""),
            merge_source=str(raw.get("merge_source") or ""),
            rule_id=str(raw.get("rule_id") or ""),
            rule_digest=str(raw.get("rule_digest") or ""),
            acts_as={str(k): str(v) for k, v in acts_as.items()} if isinstance(acts_as, Mapping) else {},
            permission=str(raw.get("permission") or ""),
            detail=str(raw.get("detail") or ""),
            output=dict(output) if isinstance(output, Mapping) else {},
            readiness_digest_before=str(raw.get("readiness_digest_before") or ""),
            readiness_digest_after=str(raw.get("readiness_digest_after") or ""),
            attempts=int(raw.get("attempts") or 1),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            version=int(raw.get("version") or 1),
        )


def record_id_for(review_id: str, kind: str, merge_ref: str, initiator: str, rule_id: str = "") -> str:
    """One record per (merge, action, initiator[, rule]): a replayed trigger never runs twice."""

    return "pma-" + _digest([review_id, kind, merge_ref, initiator, rule_id])[:32]


class PostMergeConflict(ValueError):
    """The record is not in a state that allows this transition."""


class PostMergeRefused(ValueError):
    """No provider-observed merge (or another precondition) forbids running the action."""


class PostMergeLedger:
    """Post-merge records as ``review_derived`` rows under the merged Snapshot."""

    _CAS_ATTEMPTS: Final[int] = 8

    def __init__(self, store: Any) -> None:
        self._store = store

    def _row(self, record: PostMergeRecord) -> Any:
        from lemoncrow.pro.capabilities.review.store import DerivedResult

        return DerivedResult(
            review_id=record.review_id,
            revision_id=record.revision_id,
            kind=POST_MERGE_ROW_KIND,
            scope_key=record.id,
            stamp=f"{_ROW_STAMP}:{record.version}",
            payload=record.to_wire(),
        )

    def _rows(self, revision_ids: Iterable[str]) -> list[tuple[str, PostMergeRecord]]:
        rows = self._store.derived_results(POST_MERGE_ROW_KIND, list(revision_ids))
        out: list[tuple[str, PostMergeRecord]] = []
        for (_revision_id, _scope), (stamp, payload) in rows.items():
            if str(stamp).split(":", 1)[0] != _ROW_STAMP:
                continue
            try:
                out.append((stamp, PostMergeRecord.from_wire(payload)))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def list(self, revision_ids: Iterable[str]) -> tuple[PostMergeRecord, ...]:
        records = [record for _stamp, record in self._rows(revision_ids)]
        return tuple(sorted(records, key=lambda item: (item.created_at, item.id)))

    def _read(self, revision_id: str, record_id: str) -> tuple[str, PostMergeRecord] | None:
        return next(((stamp, item) for stamp, item in self._rows((revision_id,)) if item.id == record_id), None)

    def create(self, record: PostMergeRecord) -> tuple[PostMergeRecord, bool]:
        created, _stamp, payload = self._store.insert_derived_result(self._row(record))
        return (record, True) if created else (PostMergeRecord.from_wire(payload), False)

    def transition(
        self,
        revision_id: str,
        record_id: str,
        *,
        allowed_from: frozenset[str],
        **fields: Any,
    ) -> PostMergeRecord:
        allowed = {
            "status",
            "detail",
            "output",
            "readiness_digest_before",
            "readiness_digest_after",
            "attempts",
            "actor_id",
            "rule_id",
            "rule_digest",
            "acts_as",
            "permission",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))} on a post-merge record")
        for _attempt in range(self._CAS_ATTEMPTS):
            found = self._read(revision_id, record_id)
            if found is None:
                raise LookupError(f"no such post-merge record {record_id!r}")
            stamp, current = found
            if current.status not in allowed_from:
                raise PostMergeConflict(f"post-merge action is {current.status}")
            updated = replace(current, updated_at=now_iso(), version=current.version + 1, **fields)
            if self._store.compare_and_set_derived_result(self._row(updated), expected_stamp=stamp):
                return updated
        raise PostMergeConflict("post-merge action is changing concurrently; reload and try again")


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostMergeContext:
    session: ReviewSession
    revision: ReviewRevision | None
    merge: MergeObservation
    kind: PostMergeKind
    actor_id: str
    params: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PostMergeOutcome:
    status: Literal["completed", "failed", "unavailable"]
    detail: str
    output: Mapping[str, Any] = field(default_factory=dict)


PostMergeExecutor = Callable[[PostMergeContext], PostMergeOutcome]


def _notification(context: PostMergeContext) -> PostMergeOutcome:
    message = context.params.get("message") or (
        f"{context.session.title or 'This Change'} merged ({context.merge.merged_head[:12] or 'head unknown'})."
    )
    # The notification channel is the Review's own activity history; a
    # composition may register a richer executor (chat, email).
    return PostMergeOutcome(
        "completed",
        "Recorded a merge notification in the Review activity.",
        {
            "channel": "review_activity",
            "message": message[:500],
        },
    )


def _changelog(context: PostMergeContext) -> PostMergeOutcome:
    section = context.params.get("section") or "Changed"
    title = (context.session.title or (context.revision.title if context.revision is not None else "") or "").strip()
    head = context.merge.merged_head[:12]
    entry = f"- {title or 'Merged change'}" + (f" ({head})" if head else "")
    # A draft only: LemonCrow never writes the repository after a merge.
    return PostMergeOutcome(
        "completed",
        f"Drafted a changelog entry under {section!r}; nothing was written.",
        {
            "section": section,
            "entry": entry,
            "written": False,
        },
    )


def _ticket_unavailable(context: PostMergeContext) -> PostMergeOutcome:
    del context
    return PostMergeOutcome(
        "unavailable", "No ticket integration is configured for this deployment; no ticket was changed."
    )


DEFAULT_EXECUTORS: Final[Mapping[str, PostMergeExecutor]] = {
    "notification": _notification,
    "changelog_entry": _changelog,
    "ticket_update": _ticket_unavailable,
}


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def _record_activity(
    store: Any,
    session: ReviewSession,
    record: PostMergeRecord,
    *,
    kind: str,
    summary: str,
    actor_type: str,
) -> None:
    from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent

    store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=session.id,
            revision_id=record.revision_id,
            kind=kind,  # type: ignore[arg-type]
            actor_id=record.actor_id,
            actor_type=actor_type,  # type: ignore[arg-type]
            subject_type="post_merge_action",
            subject_id=record.id,
            summary=summary[:500],
            detail_json=json.dumps(
                {
                    "kind": record.kind,
                    "status": record.status,
                    "initiated_by": record.initiated_by,
                    "rule_id": record.rule_id,
                    "rule_digest": record.rule_digest,
                    "acts_as": dict(record.acts_as),
                    "permission": record.permission,
                    "merge_ref": record.merge_ref,
                    "readiness_unchanged": record.readiness_unchanged,
                    "output": dict(record.output),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )


def run_post_merge_action(
    store: Any,
    session: ReviewSession,
    kind: str,
    *,
    merge: MergeObservation | None,
    initiated_by: Initiator,
    actor_id: str,
    readiness_digest: Callable[[], str],
    executors: Mapping[str, PostMergeExecutor] | None = None,
    params: Mapping[str, str] | None = None,
    decision: AutomationDecision | None = None,
    actor_type: str = "human",
) -> tuple[PostMergeRecord, bool]:
    """Run one post-merge action once per merge; ``(record, ran_now)``.

    Refused (:class:`PostMergeRefused`) unless the provider observed a merge.
    A rule run must carry the ``rule`` decision that allowed it.
    """

    if kind not in POST_MERGE_KINDS:
        raise PostMergeRefused(f"{kind!r} is not a post-merge action")
    if merge is None:
        raise PostMergeRefused(
            "Post-merge actions run only after the connected provider reports this Change merged; "
            "no provider-observed merge is recorded."
        )
    if initiated_by == "rule" and (decision is None or not decision.allowed):
        raise PostMergeRefused("an automated post-merge action needs a firing automation rule")
    rule_id = decision.rule_id if decision is not None and initiated_by == "rule" else ""
    revision = store.get_revision(merge.revision_id) if merge.revision_id else None
    revision_id = merge.revision_id or str(getattr(store.latest_revision(session.id), "id", "") or "")
    if not revision_id:
        raise PostMergeRefused("this Review has no revision to record the post-merge action under")
    ledger = PostMergeLedger(store)
    record_id = record_id_for(session.id, kind, merge.ref, initiated_by, rule_id)
    now = now_iso()
    acts_as = decision.acts_as.to_wire() if decision is not None and decision.acts_as is not None else {}
    record, created = ledger.create(
        PostMergeRecord(
            id=record_id,
            review_id=session.id,
            revision_id=revision_id,
            kind=kind,
            status="running",
            initiated_by=initiated_by,
            actor_id=actor_id,
            merge_ref=merge.ref,
            merge_source=merge.source,
            rule_id=rule_id,
            rule_digest=decision.rule_digest if decision is not None and initiated_by == "rule" else "",
            acts_as=acts_as,
            permission=decision.permission if decision is not None else "",
            created_at=now,
            updated_at=now,
        )
    )
    if not created:
        if record.status not in _RETRYABLE or initiated_by == "rule":
            # Already ran (or is running) for this merge: never twice. A rule
            # never retries on its own; a human may retry a failed run.
            return record, False
        record = ledger.transition(
            revision_id,
            record_id,
            allowed_from=_RETRYABLE,
            status="running",
            attempts=record.attempts + 1,
            actor_id=actor_id,
            detail="",
        )
    before = readiness_digest()
    executor = (executors or DEFAULT_EXECUTORS).get(kind) or DEFAULT_EXECUTORS[kind]
    context = PostMergeContext(
        session=session,
        revision=revision,
        merge=merge,
        kind=kind,
        actor_id=actor_id,
        params=dict(params or {}),
    )
    try:
        outcome = executor(context)
    except Exception as exc:  # executor boundary: record the failure, never raise into the trigger
        outcome = PostMergeOutcome("failed", f"{exc.__class__.__name__}: {exc}"[:500])
    after = readiness_digest()
    record = ledger.transition(
        revision_id,
        record_id,
        allowed_from=frozenset({"running"}),
        status=outcome.status,
        detail=outcome.detail[:500],
        output=dict(outcome.output),
        readiness_digest_before=before,
        readiness_digest_after=after,
    )
    who = (
        f"rule {record.rule_id} (as {record.acts_as.get('subject', '')})"
        if record.initiated_by == "rule"
        else record.actor_id
    )
    _record_activity(
        store,
        session,
        record,
        kind=POST_MERGE_ACTIVITY,
        summary=f"{ACTION_LABELS[action_for(kind)]} {record.status} after the provider-observed merge, run by {who}: "
        f"{record.detail}",
        actor_type="agent" if initiated_by == "rule" else actor_type,
    )
    return record, True


@dataclass(frozen=True)
class MergeAutomationResult:
    merge: MergeObservation | None
    decisions: tuple[AutomationDecision, ...] = ()
    records: tuple[PostMergeRecord, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "merge": None if self.merge is None else self.merge.to_wire(),
            "decisions": [item.to_wire() for item in self.decisions],
            "records": [item.to_wire() for item in self.records],
        }


def _record_refusal(
    store: Any, session: ReviewSession, kind: str, merge: MergeObservation, decision: AutomationDecision
) -> PostMergeRecord | None:
    """An automated run refused by permissions is audited once, with its rule id."""

    revision_id = merge.revision_id or str(getattr(store.latest_revision(session.id), "id", "") or "")
    if not revision_id:
        return None
    now = now_iso()
    record, created = PostMergeLedger(store).create(
        PostMergeRecord(
            id=record_id_for(session.id, kind, merge.ref, "rule", decision.rule_id),
            review_id=session.id,
            revision_id=revision_id,
            kind=cast(PostMergeKind, kind),
            status="refused",
            initiated_by="rule",
            actor_id=decision.acts_as.subject if decision.acts_as is not None else "",
            merge_ref=merge.ref,
            merge_source=merge.source,
            rule_id=decision.rule_id,
            rule_digest=decision.rule_digest,
            acts_as=decision.acts_as.to_wire() if decision.acts_as is not None else {},
            permission=decision.permission,
            detail=decision.reason[:500],
            created_at=now,
            updated_at=now,
        )
    )
    if created:
        _record_activity(
            store,
            session,
            record,
            kind=AUTOMATION_REFUSED_ACTIVITY,
            summary=f"Automation refused {ACTION_LABELS[action_for(kind)]}: {decision.reason}",
            actor_type="agent",
        )
    return record


def run_merge_automation(
    store: Any,
    session: ReviewSession,
    *,
    rules: Sequence[AutomationRule],
    facts: AutomationFacts,
    merge: MergeObservation | None,
    permission_check: PermissionCheck,
    readiness_digest: Callable[[], str],
    executors: Mapping[str, PostMergeExecutor] | None = None,
) -> MergeAutomationResult:
    """Run every post-merge action an enabled rule fires for, once per merge.

    Idempotent: safe to call from every trigger (local landing watch, arm or
    cancel settling ``merged``, hosted provider webhook). Without an enabling
    rule nothing runs (``needs_human``); a rule whose acting identity lacks
    the permission is recorded as refused, with its rule id.
    """

    if merge is None or not rules:
        return MergeAutomationResult(merge=merge)
    decisions: list[AutomationDecision] = []
    records: list[PostMergeRecord] = []
    for kind in POST_MERGE_KINDS:
        decision = decide_automation(action_for(kind), rules=rules, facts=facts, permission_check=permission_check)
        decisions.append(decision)
        if decision.mode == "refused":
            refused = _record_refusal(store, session, kind, merge, decision)
            if refused is not None:
                records.append(refused)
            continue
        if decision.mode != "rule" or decision.acts_as is None:
            continue
        record, _ran = run_post_merge_action(
            store,
            session,
            kind,
            merge=merge,
            initiated_by="rule",
            actor_id=decision.acts_as.subject,
            readiness_digest=readiness_digest,
            executors=executors,
            params=dict(decision.params),
            decision=decision,
        )
        records.append(record)
    return MergeAutomationResult(merge=merge, decisions=tuple(decisions), records=tuple(records))


def automation_facts(
    store: Any,
    session: ReviewSession,
    *,
    repository: str,
    merge: MergeObservation | None,
    provider: str = "",
    readiness_ready: bool | None = None,
    branch_protection: BranchProtectionFacts | None = None,
    changed_paths: Sequence[str] | None = None,
) -> AutomationFacts:
    """The closed fact set the evaluator may read for one Review."""

    revision = store.latest_revision(session.id)
    paths: tuple[str, ...] | None = tuple(changed_paths) if changed_paths is not None else None
    if paths is None and revision is not None:
        from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

        packet = read_packet_json(store, revision)
        raw_files = packet.get("files", ()) if isinstance(packet, Mapping) else ()
        paths = tuple(
            str(item.get("path") or "")
            for item in raw_files
            if isinstance(item, Mapping) and str(item.get("path") or "")
        )
    landing = landing_relationship(store, session.id)
    values: dict[str, object] = {
        "merge.observed": merge is not None,
        "merge.reviewed_head": None if merge is None else merge.reviewed_head_merged,
        "review.status": session.status,
        "change.provider": provider
        or (merge.provider if merge is not None else str(getattr(landing, "provider", "") or "")),
        "change.repository": repository,
        "change.title": session.title,
    }
    if readiness_ready is not None:
        values["readiness.ready"] = readiness_ready
    if paths is not None:
        values["change.paths"] = paths
    sources = {
        "merge.observed": merge.source if merge is not None else "no provider-observed merge",
        "merge.reviewed_head": merge.source if merge is not None else "unknown",
        "readiness.ready": "readiness core" if readiness_ready is not None else "not evaluated",
        "change.paths": "current revision packet" if paths is not None else "unreadable",
    }
    return AutomationFacts(
        review_id=session.id,
        repository=repository,
        values=values,
        sources=sources,
        branch_protection=branch_protection or BranchProtectionFacts(),
    )


def acting_identity_wire(identity: ActingIdentity | None) -> dict[str, str]:
    return {} if identity is None else identity.to_wire()


def run_watch_merge_automation(store: Any, session: ReviewSession) -> MergeAutomationResult:
    """Post-merge automation from the local landing watch, after it recorded a provider merge.

    The watch has no Reader readiness projection, so the recorded digest covers
    the stored landing record; post-merge rules cannot read readiness anyway.
    """

    from lemoncrow.pro.capabilities.review.automation_rules import (
        load_local_automation_rules,
        local_permission_check,
    )

    rules = load_local_automation_rules().for_repository(session.repo_root)
    merge = observed_merge(store, session)
    if merge is None or not rules:
        return MergeAutomationResult(merge=merge)
    return run_merge_automation(
        store,
        session,
        rules=rules,
        facts=automation_facts(store, session, repository=session.repo_root, merge=merge),
        merge=merge,
        permission_check=local_permission_check,
        readiness_digest=lambda: readiness_record_digest(store, session.id),
    )


__all__ = [
    "AUTOMATION_REFUSED_ACTIVITY",
    "DEFAULT_EXECUTORS",
    "POST_MERGE_ACTIVITY",
    "POST_MERGE_KINDS",
    "POST_MERGE_ROW_KIND",
    "MergeAutomationResult",
    "MergeObservation",
    "PostMergeConflict",
    "PostMergeContext",
    "PostMergeExecutor",
    "PostMergeLedger",
    "PostMergeOutcome",
    "PostMergeRecord",
    "PostMergeRefused",
    "ProviderMergeFact",
    "action_for",
    "automation_facts",
    "observed_merge",
    "readiness_record_digest",
    "record_id_for",
    "run_merge_automation",
    "run_post_merge_action",
    "run_watch_merge_automation",
]
