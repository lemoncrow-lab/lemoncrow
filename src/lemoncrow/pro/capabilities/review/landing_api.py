"""Landing decisions: auto-approval, break-glass and the SCM status (P10-LAND).

The Review API wires these through :class:`LandingDeps`; a composition that
connects an SCM supplies :class:`LandingAuthority` (hosted: the provider
adapter and the control plane). Without an authority nothing is delivered to a
provider: eligibility is still evaluated and shown, but no approval is made.

Routes (in the existing Review Status surface; no new view):

* ``GET  /reviews/{id}/landing/auto-approval`` -- the eligibility decision as
  this viewer would get it now, every gate explained, plus the standing
  approval (read-only; the default-branch policy check runs only when deciding);
* ``POST /reviews/{id}/landing/auto-approval`` -- decide now *as this person*
  (a change set is read with their access; a request-free sweep reads a set as
  incomplete and never approves);
* ``POST /reviews/{id}/landing/break-glass`` -- an authorized responder lands
  an emergency Change with named LemonCrow blockers bypassed.

Request-free entry (``app.state.review_landing_evaluator``): the hosted
control plane decides after each provider event. Decisions are signed in the
control plane (``LandingAuthority.sign_decision``), never by a worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request, status

from lemoncrow.pro.capabilities.review import auto_approval as aa
from lemoncrow.pro.capabilities.review import break_glass as bg
from lemoncrow.pro.capabilities.review.blocking_reasons import merge_result_status, scm_status
from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.landing_policy import (
    LandingPolicy,
    LandingPolicyLayer,
    merge_landing_policies,
    parse_landing_section,
    parse_trusted_landing,
)

_LOG = logging.getLogger(__name__)
SCM_STATUS_KIND: Final[str] = "scm_status"


@dataclass(frozen=True, slots=True)
class LandingAuthority:
    """What a composition connected to an SCM supplies (every seam optional, fail closed).

    * ``facts(session, revision, read_base)``: provider truth for the Gate F
      preconditions -- ``provider_sync`` (:class:`auto_approval.ProviderSyncFact`),
      ``requests`` (:class:`auto_approval.RequestProgressFact`), ``trusted_base``
      (:class:`auto_approval.TrustedBaseFact`; only when *read_base*: deciding,
      or revalidating outside a plain read), ``labels``, ``target_branch``,
      ``provider_blockers`` and ``errors``. A missing key is unknown (never a pass).
    * ``trusted_base(session, revision)``: L4 alone -- the default branch's review
      policy now against the Snapshot's (:class:`auto_approval.TrustedBaseFact`).
      The armed Merge-when-ready revalidation re-reads it (RC11).
    * ``approve(session, revision, decision_wire)`` / ``withdraw(session, metadata)``:
      deliver / withdraw the approval through the provider (``{state, detail, remote_ref}``).
    * ``publish_status(session, sha, status)``: the commit status on that exact commit.
    * ``merge_result(session, revision)``: the provider's merge result
      (a ``merge_result.MergeResultFacts``; anything else reads as not built).
    * ``sign_decision(review_id, decision)``: sign the review record in the
      control plane after a landing decision.
    """

    facts: Callable[[Any, Any, bool], Mapping[str, Any]] | None = None
    approve: Callable[[Any, Any, Mapping[str, Any]], Mapping[str, Any]] | None = None
    withdraw: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]] | None = None
    publish_status: Callable[[Any, str, Mapping[str, str]], Mapping[str, Any]] | None = None
    merge_result: Callable[[Any, Any], Any] | None = None
    sign_decision: Callable[[str, Any], Mapping[str, Any]] | None = None
    trusted_base: Callable[[Any, Any], Any] | None = None


#: Revalidation origins that re-read L4 through the provider. A plain readiness read
#: (``read``) does not: the hosted sweep re-checks every standing decision on a cadence.
L4_RECHECK_ORIGINS: Final[frozenset[str]] = frozenset({"action", "sweep", "watch", "provider"})


def trusted_base_now(authority: LandingAuthority, session: Any, revision: Any) -> aa.TrustedBaseFact | None:
    """L4 now through the authority (``None``: no provider can answer it; a failure is ``unknown``)."""

    reader = authority.trusted_base
    if reader is None:
        return None
    try:
        fact = reader(session, revision)
    except Exception as exc:  # unknown, never current
        return aa.TrustedBaseFact(state="unknown", detail=(str(exc) or exc.__class__.__name__)[:300])
    if not isinstance(fact, aa.TrustedBaseFact):
        return aa.TrustedBaseFact(state="unknown", detail="The default branch's policy could not be read.")
    return fact


@dataclass(frozen=True, slots=True)
class LandingDeps:
    """What landing decisions read from the Review API (sessions already repository-scoped)."""

    store: Any
    session: Callable[[str], Any]
    latest: Callable[[Any], Any]
    display_number: Callable[[Any], int]
    #: Readiness as *session*'s reviewer at one instant (never persisted as that reviewer's record).
    readiness: Callable[[Any, str], Mapping[str, Any]]
    extension: Callable[[Any, Any], Mapping[str, Any]]
    review_config: Callable[[Any], Any]
    changed_files: Callable[[Any], Sequence[tuple[str, str, str]]]
    criticality: Callable[[Any, Any, Sequence[str]], str | None]
    behavior_blockers: Callable[[Any, Any], Sequence[Mapping[str, Any]]]
    verification_rows: Callable[[Any, Any], Sequence[Mapping[str, Any]]]
    findings: Callable[[Any, Any], aa.FindingsFact]
    arm: Callable[[Any, Any, Mapping[str, Any], Mapping[str, Any]], Awaitable[dict[str, Any]]]
    activity: Callable[[Any, Any, str, str, Mapping[str, Any]], None]
    approval_principal: Callable[[], Any] | None
    may_manage: Callable[[Any], bool]
    scm_link: Callable[[Any], Mapping[str, Any]]
    revalidate: Callable[[Any, str], str]
    authority: LandingAuthority
    now: Callable[[], str]


# --------------------------------------------------------------------------- #
# policy and facts
# --------------------------------------------------------------------------- #


def landing_policy(extension: Mapping[str, Any], config: Any) -> LandingPolicy:
    """Operator layers (the composition's ``landing_policy``, highest first) plus the trusted base's section."""

    layers: list[LandingPolicyLayer] = []
    for raw in extension.get("landing_policy", ()) or ():
        if isinstance(raw, Mapping):
            layers.append(
                parse_landing_section(
                    raw.get("document"), layer="operator", source=str(raw.get("source") or "organization policy")
                )
            )
    content = str(getattr(config, "content", "") or "")
    if content:
        source = str(getattr(config, "source", "") or "") or "trusted base .lemoncrow/review.yaml"
        layers.append(parse_trusted_landing(content, source=source))
    return merge_landing_policies(layers)


def _unqualified(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """LemonCrow-executed results Gate H has not qualified (a missing stamp is not qualified)."""

    from lemoncrow.pro.capabilities.review.evidence_qualification import is_lemoncrow_row

    out: list[str] = []
    for row in rows:
        if not is_lemoncrow_row(row):
            continue
        mark = row.get("qualification")
        state = str(mark.get("state") or "") if isinstance(mark, Mapping) else ""
        if state != "qualified":
            out.append(str(row.get("name") or row.get("stable_key") or "LemonCrow check"))
    return tuple(out)


def _changed_paths(files: Sequence[tuple[str, str, str]]) -> tuple[str, ...]:
    # A rename counts on both sides: moving a file out of a sensitive directory is sensitive.
    return tuple(dict.fromkeys(path for row in files for path in (row[0], row[1]) if path))


def gather_facts(
    deps: LandingDeps,
    session: Any,
    revision: Any,
    readiness: Mapping[str, Any],
    *,
    decision_time: bool,
    recheck_base: bool = False,
) -> aa.AutoApprovalFacts:
    """Every input of one eligibility decision; a read that fails leaves its fact unknown.

    *recheck_base* (revalidation outside a plain read) re-reads L4 like a
    decision does, so a standing approval is withdrawn once the default
    branch's review policy moved away from the Snapshot's.
    """

    errors: dict[str, str] = {}

    def attempt(key: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception as exc:  # unknown, never a pass
            errors[key] = (str(exc) or exc.__class__.__name__)[:300]
            return None

    extension = deps.extension(session, revision)
    policy = landing_policy(extension, deps.review_config(revision))
    files = attempt("hard_deny", lambda: deps.changed_files(revision)) or ()
    paths = _changed_paths(files)
    provider: Mapping[str, Any] = {}
    facts_reader = deps.authority.facts
    # L4 reads the default branch through the provider: only when the policy could approve,
    # when deciding or revalidating a standing approval (never on a plain read).
    read_base = (decision_time or recheck_base) and policy.auto_approve_enabled
    if facts_reader is None:
        errors["provider_sync"] = "no connected SCM provider"
    else:
        provider = attempt("provider_sync", lambda: facts_reader(session, revision, read_base)) or {}
    provider_errors = provider.get("errors")
    if isinstance(provider_errors, Mapping):
        errors.update({str(key): str(value) for key, value in provider_errors.items()})
    sync = provider.get("provider_sync")
    requests = provider.get("requests")
    base = provider.get("trusted_base")
    labels = provider.get("labels")
    branch = provider.get("target_branch")
    blockers = provider.get("provider_blockers")
    carried = attempt("verification_weakening", lambda: tuple(deps.behavior_blockers(session, revision)))
    unqualified = attempt("evidence_qualification", lambda: _unqualified(deps.verification_rows(session, revision)))
    return aa.AutoApprovalFacts(
        review_id=session.id,
        snapshot_id=revision.id,
        tree_fingerprint=str(revision.tree_fingerprint or ""),
        head_revision=str(getattr(revision, "head_sha", "") or ""),
        policy=policy,
        readiness=readiness,
        changed_paths=paths,
        criticality=attempt("criticality", lambda: deps.criticality(session, revision, paths)),
        labels=tuple(str(item) for item in labels) if isinstance(labels, list | tuple) else None,
        target_branch=str(branch) if isinstance(branch, str) else None,
        findings=attempt("findings", lambda: deps.findings(session, revision)),
        human_needs_changes=attempt("human_blockers", lambda: _needs_changes(deps.store, session, revision)),
        provider_blockers=tuple(str(item) for item in blockers) if isinstance(blockers, list | tuple) else None,
        carried_blockers=carried,
        unqualified_evidence=unqualified,
        provider_sync=sync if isinstance(sync, aa.ProviderSyncFact) else None,
        requests=requests if isinstance(requests, aa.RequestProgressFact) else None,
        # L4 is re-read when deciding and revalidating; a plain read (and a decision the
        # policy cannot approve) does not read the default branch.
        trusted_base=(
            base
            if isinstance(base, aa.TrustedBaseFact)
            else (
                None
                if read_base
                else aa.TrustedBaseFact(
                    state="not_rechecked",
                    detail=(
                        "Not read: the policy does not enable auto-approval."
                        if decision_time
                        else "Checked against the default branch when deciding; not re-read here."
                    ),
                )
            )
        ),
        errors=errors,
    )


def _needs_changes(store: Any, session: Any, revision: Any) -> int:
    """Human ``needs_changes`` judgments, by any reviewer, still valid on this Snapshot."""

    current = {(unit.unit_key, unit.content_fingerprint) for unit in store.list_units(revision.id)}
    reviewers = sorted({str(event.reviewer_id) for event in store.list_review_mark_events(session.id)})
    count = 0
    for reviewer in reviewers:
        for mark in store.list_marks(session.id, reviewer_id=reviewer):
            if (
                mark.state == "needs_changes"
                and mark.actor_type == "human"
                and (mark.unit_key, mark.content_fingerprint) in current
            ):
                count += 1
    return count


def evaluate(
    deps: LandingDeps, session: Any, *, decision_time: bool, now: str | None = None, recheck_base: bool = False
) -> tuple[aa.AutoApprovalDecision, Mapping[str, Any]]:
    """``(decision, readiness)``: readiness as the policy principal (its own review progress is what
    auto-approval stands in for); a change set resolves with the bound principal's access."""

    instant = now or deps.now()
    subject = replace(session, reviewer_id=aa.AUTO_APPROVAL_ACTOR)
    revision = deps.latest(session)
    readiness = deps.readiness(subject, instant)
    facts = gather_facts(deps, session, revision, readiness, decision_time=decision_time, recheck_base=recheck_base)
    return aa.evaluate_auto_approval(facts, evaluated_at=instant), readiness


# --------------------------------------------------------------------------- #
# deciding, delivering, signing and the SCM status
# --------------------------------------------------------------------------- #


def _sign(deps: LandingDeps, session: Any, decision: Any) -> Mapping[str, Any]:
    """Sign the review record after a landing decision (control plane only); never fails the decision."""

    signer = deps.authority.sign_decision
    if signer is None:
        return {"status": "unavailable"}
    try:
        return dict(signer(session.id, decision))
    except Exception as exc:  # recorded, never undoes the decision
        _LOG.warning("landing decision record not signed for Review %s", session.id, exc_info=True)
        return {"status": "failed", "detail": (str(exc) or exc.__class__.__name__)[:200]}


def auto_approval_decision_record(item: Any) -> Any:
    """The :class:`review_record.ReviewDecision` a standing auto-approval is signed as."""

    from lemoncrow.pro.capabilities.review.review_record import ReviewDecision

    metadata = aa.approval_metadata(item)
    return ReviewDecision(
        kind="landing",
        outcome="auto_approved",
        actor_id=aa.AUTO_APPROVAL_ACTOR,
        actor_type="system",
        decided_at=str(metadata.get("decided_at") or ""),
        reference=str(getattr(item, "id", "") or ""),
        detail=aa.decision_for_record(item),
    )


def break_glass_decision_record(record: Mapping[str, Any]) -> Any:
    from lemoncrow.pro.capabilities.review.review_record import ReviewDecision

    return ReviewDecision(
        kind="landing",
        outcome="break_glass",
        actor_id=str(record.get("actor_id") or ""),
        actor_type="human",
        decided_at=str(record.get("created_at") or ""),
        reference=str(record.get("id") or ""),
        detail=bg.decision_detail(record),
    )


def _withdraw(deps: LandingDeps, session: Any) -> Callable[[Mapping[str, Any]], Mapping[str, Any]] | None:
    seam = deps.authority.withdraw
    if seam is None:
        return None
    return lambda metadata: seam(session, metadata)


def _deliver(deps: LandingDeps, session: Any, revision: Any) -> Callable[[Mapping[str, Any]], Mapping[str, Any]] | None:
    seam = deps.authority.approve
    if seam is None:
        return None
    return lambda metadata: seam(session, revision, metadata)


def revalidate(deps: LandingDeps, session: Any, *, origin: str) -> str:
    """The auto-approval half of the one revalidation path (``_revalidate_armed_landing`` calls it)."""

    revision = deps.latest(session)
    holder: dict[str, Mapping[str, Any]] = {}

    def _evaluate() -> aa.AutoApprovalDecision:
        decision, readiness = evaluate(deps, session, decision_time=False, recheck_base=origin in L4_RECHECK_ORIGINS)
        holder["readiness"] = readiness
        return decision

    def _audit(item: Any, kind: str, summary: str, detail: Mapping[str, Any]) -> None:
        deps.activity(session, item, kind, summary, detail)

    return aa.revalidate_auto_approval(
        deps.store,
        review_id=session.id,
        latest_tree_fingerprint=str(revision.tree_fingerprint or ""),
        origin=origin,
        evaluate=_evaluate,
        valid_until=lambda: str(holder.get("readiness", {}).get("valid_until") or ""),
        withdraw=_withdraw(deps, session),
        deliver=_deliver(deps, session, revision),
        audit=_audit,
    )


def _merge_result_failing(readiness: Mapping[str, Any]) -> bool:
    return any(
        isinstance(row, Mapping) and row.get("id") == "merge-result" and row.get("state") == "fail"
        for row in readiness.get("conditions") or ()
    )


def _intent_armed(deps: LandingDeps, session: Any) -> bool:
    from lemoncrow.pro.capabilities.review.landing import ARMED_LANDING_STATES, landing_relationship

    landing = landing_relationship(deps.store, session.id)
    return landing is not None and landing.status in ARMED_LANDING_STATES


def decide(deps: LandingDeps, session: Any, *, origin: str) -> dict[str, Any]:
    """Decide auto-approval now, record and deliver it when eligible, and refresh the SCM status.

    Never approves twice for one decision basis: a standing approval in force
    for this Snapshot is revalidated instead (the one revalidation path), and
    a provider delivery carries the decision digest as its idempotency key.
    """

    revision = deps.latest(session)
    decision, readiness = evaluate(deps, session, decision_time=True)
    try:
        aa.store_decision(deps.store, decision)
    except Exception:  # the decision is still answered; only its durable copy is missing
        _LOG.warning("auto-approval decision not stored for Review %s", session.id, exc_info=True)
    outcome = "not_eligible"
    standing = aa.auto_approval_relationship(deps.store, session.id)
    if standing is not None and (
        standing.status in aa.IN_FORCE_STATES or aa.approval_metadata(standing).get("provider_cancel") == "pending"
    ):
        # A standing approval (this Snapshot's or an older one) is settled only by the one
        # revalidation path: an older Snapshot's is withdrawn there before any new approval.
        outcome = deps.revalidate(session, origin)
        standing = aa.auto_approval_relationship(deps.store, session.id)
    elif _merge_result_failing(readiness) and _intent_armed(deps, session):
        # Item 18: a merge result that fails once merged withdraws an armed Merge when ready
        # now, through the one revalidation path, not at the next read or sweep.
        deps.revalidate(session, origin)
    metadata = aa.approval_metadata(standing)
    current = str(metadata.get("tree_fingerprint") or "") == str(revision.tree_fingerprint or "")
    settled = standing is None or (
        standing.status not in aa.IN_FORCE_STATES and metadata.get("provider_cancel") != "pending"
    )
    # Never re-approve the Snapshot a pass just withdrew (no flapping on one basis).
    if settled and not (outcome == "withdrawn" and current) and decision.eligible and session.status != "archived":
        link = deps.scm_link(session)
        if deps.authority.approve is None:
            outcome = "no_provider"
        else:
            # Two concurrent decisions upsert the same row; the provider delivery is
            # keyed by the decision digest, so at most one approval reaches the SCM.
            item = aa.record_auto_approval(
                deps.store,
                decision=decision,
                revision_number=deps.display_number(revision),
                policy_layers=[
                    dict(layer)
                    for layer in (readiness.get("policy_generation") or {}).get("layers") or ()
                    if isinstance(layer, Mapping)
                ],
                valid_until=str(readiness.get("valid_until") or ""),
                provider=str(link.get("provider") or ""),
                external_id=str(link.get("external_id") or ""),
                url=str(link.get("url") or ""),
            )
            deps.activity(
                session,
                item,
                "landing.auto_approved",
                "Auto-approved by organization policy",
                {"origin": origin, "decision_digest": decision.digest, "generation": decision.policy_generation},
            )
            deliver = _deliver(deps, session, revision)
            assert deliver is not None
            try:
                result = deliver(aa.approval_metadata(item))
            except Exception as exc:  # provider boundary: stays owed and is retried
                result = {"state": "retryable", "detail": str(exc) or exc.__class__.__name__}
            delivered = aa.settle_delivery(deps.store, item, result) or item
            outcome = "approved" if delivered.status == "approved" else "approving"
            _sign(deps, session, auto_approval_decision_record(delivered))
    status_result = publish_status(deps, session, revision, readiness)
    return {
        "outcome": outcome,
        "decision": decision.to_wire(),
        "approval": aa.approval_wire(
            aa.auto_approval_relationship(deps.store, session.id),
            latest_tree_fingerprint=str(revision.tree_fingerprint or ""),
        ),
        "scm_status": status_result,
    }


def _status_digest(sha: str, value: Mapping[str, str]) -> str:
    raw = json.dumps({"sha": sha, **value}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _publish_once(deps: LandingDeps, session: Any, revision: Any, sha: str, value: Mapping[str, str]) -> dict[str, Any]:
    """Publish one commit status unless the same one was already published for that commit."""

    from lemoncrow.pro.capabilities.review.store import DerivedResult

    seam = deps.authority.publish_status
    if seam is None or not sha:
        return {"status": "unavailable"}
    scope = f"commit:{sha}"
    digest = _status_digest(sha, value)
    existing = deps.store.derived_results(SCM_STATUS_KIND, [revision.id]).get((revision.id, scope))
    if existing is not None and existing[0] == digest:
        return {"status": "unchanged", **dict(value)}
    try:
        result = dict(seam(session, sha, value))
    except Exception as exc:  # the status is advisory to the SCM; retried on the next decision
        _LOG.warning("SCM status not published for Review %s", session.id, exc_info=True)
        return {"status": "failed", "detail": (str(exc) or exc.__class__.__name__)[:200]}
    if str(result.get("state") or "") == "sent":
        # One row per (Snapshot, commit); the Snapshot's deletion cascade is its closer.
        deps.store.put_derived_results(
            [
                DerivedResult(
                    review_id=session.id,
                    revision_id=revision.id,
                    kind=SCM_STATUS_KIND,
                    scope_key=scope,
                    stamp=digest,
                    payload={"sha": sha, **dict(value)},
                )
            ]
        )
    return {"status": str(result.get("state") or "failed"), **dict(value)}


def current_break_glass(deps: LandingDeps, session: Any, revision: Any) -> Mapping[str, Any] | None:
    """The break-glass the armed landing of this exact Snapshot carries, if any."""

    from lemoncrow.pro.capabilities.review.landing import (
        ARMED_LANDING_STATES,
        landing_metadata,
        landing_relationship,
    )

    landing = landing_relationship(deps.store, session.id)
    metadata = landing_metadata(landing)
    if (
        landing is None
        or landing.status not in {*ARMED_LANDING_STATES, "merged"}
        or metadata.get("basis") != "break_glass"
        or metadata.get("revision_id") != revision.id
    ):
        return None
    return {
        "actor_id": str(metadata.get("armed_by") or ""),
        "bypassed": list(metadata.get("bypassed") or ()),
        "follow_up_due_at": str(metadata.get("follow_up_due_at") or ""),
    }


def publish_status(deps: LandingDeps, session: Any, revision: Any, readiness: Mapping[str, Any]) -> dict[str, Any]:
    """Exact blocking reasons on the reviewed head (and the merge result, when one is built)."""

    standing = aa.approval_wire(
        aa.auto_approval_relationship(deps.store, session.id), latest_tree_fingerprint=str(revision.tree_fingerprint)
    )
    reviewed = any(
        isinstance(row, Mapping) and row.get("id") == "human-approval-state" and row.get("state") == "pass"
        for row in readiness.get("conditions") or ()
    )
    value = scm_status(
        readiness, auto_approval=standing, break_glass=current_break_glass(deps, session, revision), reviewed=reviewed
    )
    out = {"head": _publish_once(deps, session, revision, str(getattr(revision, "head_sha", "") or ""), value)}
    merged = merge_result_status(readiness)
    if merged is not None:
        target = next(
            (
                str(row.get("target_sha") or "")
                for row in readiness.get("conditions") or ()
                if isinstance(row, Mapping) and row.get("id") == "merge-result"
            ),
            "",
        )
        out["merge_result"] = _publish_once(deps, session, revision, target, merged)
    return out


# --------------------------------------------------------------------------- #
# readiness additions
# --------------------------------------------------------------------------- #


def landing_extras(store: Any, session: Any, revision: Any, *, now: str) -> dict[str, Any]:
    """The readiness ``landing`` block's auto-approval and break-glass state (store reads only)."""

    instant = parse_timestamp(now) or datetime.now(UTC)
    decision = aa.stored_decision(store, revision.id)
    outcomes = store.list_outcomes(session.id)
    revisions = [item.id for item in store.list_revisions(session.id)]
    records = bg.records(store, revisions)
    return {
        "auto_approval": {
            **aa.approval_wire(
                aa.auto_approval_relationship(store, session.id),
                latest_tree_fingerprint=str(revision.tree_fingerprint or ""),
            ),
            "eligible": None if decision is None else bool(decision.get("eligible")),
            "blocking": [] if decision is None else list(decision.get("blocking") or ())[:20],
            "evaluated_at": "" if decision is None else str(decision.get("evaluated_at") or ""),
        },
        "break_glass": [bg.record_wire(row, outcomes, now=instant) for row in records[-5:]],
    }


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


def register_landing_routes(router: APIRouter, deps: LandingDeps) -> None:
    from starlette.concurrency import run_in_threadpool

    from lemoncrow.pro.capabilities.review.api import _json_object

    def _eligibility(review_id: str) -> dict[str, Any]:
        session = deps.session(review_id)
        revision = deps.latest(session)
        decision, _readiness = evaluate(deps, session, decision_time=False)
        return {
            "review_id": session.id,
            "snapshot_id": revision.id,
            "decision": decision.to_wire(),
            "approval": aa.approval_wire(
                aa.auto_approval_relationship(deps.store, session.id),
                latest_tree_fingerprint=str(revision.tree_fingerprint or ""),
            ),
            "delivers": deps.authority.approve is not None,
        }

    @router.get("/reviews/{review_id}/landing/auto-approval")
    async def _get_auto_approval(review_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await run_in_threadpool(_eligibility, review_id)
        return result

    @router.post("/reviews/{review_id}/landing/auto-approval")
    async def _post_auto_approval(review_id: str) -> dict[str, Any]:
        """Decide now as this person: their access resolves a change set; the decision stays policy's."""

        session = deps.session(review_id)
        principal = deps.approval_principal() if deps.approval_principal is not None else None
        if str(getattr(principal, "kind", "") or "") != "human" or not deps.may_manage(session):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Deciding auto-approval on request needs a person with review management permission.",
            )
        if deps.authority.approve is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="No connected provider can receive an approval."
            )
        result: dict[str, Any] = await run_in_threadpool(lambda: decide(deps, session, origin="action"))
        return result

    @router.post("/reviews/{review_id}/landing/break-glass")
    async def _post_break_glass(review_id: str, request: Request) -> dict[str, Any]:
        """Land an emergency Change with named LemonCrow blockers bypassed (an authorized person only)."""

        session = deps.session(review_id)
        if session.status == "archived":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="restore this archived Change first")
        body = await _json_object(request)
        revision = deps.latest(session)
        try:
            principal = deps.approval_principal() if deps.approval_principal is not None else None
            current = await run_in_threadpool(deps.readiness, session, deps.now())
            policy = landing_policy(deps.extension(session, revision), deps.review_config(revision))
            actor = bg.authorize(
                principal,
                responders=policy.break_glass.responders,
                may_manage=deps.may_manage(session),
                enabled=policy.break_glass_enabled,
            )
            parsed = bg.parse_request(body)
            if parsed.expected_revision_id != revision.id:
                raise bg.BreakGlassRefused(
                    "the Snapshot changed since readiness was inspected; reload before break-glass", status=409
                )
            if parsed.expected_conditions_hash != current["conditions_hash"]:
                raise bg.BreakGlassRefused(
                    "landing conditions changed since they were inspected; reload readiness first", status=409
                )
            if not current["landing"]["supported"]:
                raise bg.BreakGlassRefused("this Change has no connected SCM landing delegate", status=409)
            bypassed = bg.bypassed_conditions(current["conditions"], parsed.bypass)
        except bg.BreakGlassRefused as exc:
            if exc.status == 403:
                deps.activity(
                    session,
                    None,
                    "landing.break_glass_refused",
                    "Break-glass refused",
                    {"actor": str(getattr(principal, "principal_id", "") or ""), "reason": str(exc)[:300]},
                )
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        record = await run_in_threadpool(
            lambda: bg.record_break_glass(
                deps.store,
                review_id=session.id,
                revision=revision,
                actor_id=actor,
                request=parsed,
                bypassed=bypassed,
                readiness=current,
                follow_up_hours=policy.break_glass.follow_up_hours,
                now=deps.now(),
            )
        )
        deps.activity(
            session,
            None,
            "landing.break_glass",
            f"Break-glass by {actor}: {parsed.reason}"[:500],
            {
                "break_glass_id": record["id"],
                "actor": actor,
                "incident_url": parsed.incident_url,
                "bypassed": [row["id"] for row in bypassed],
                "follow_up_due_at": record["follow_up"]["due_at"],
            },
        )
        # Armed as this person (the responder): revalidation reads readiness as them.
        result = await deps.arm(
            session,
            revision,
            current,
            {
                "basis": "break_glass",
                "break_glass_id": record["id"],
                "bypassed": [row["id"] for row in bypassed],
                "follow_up_due_at": record["follow_up"]["due_at"],
            },
        )
        signed = await run_in_threadpool(lambda: _sign(deps, session, break_glass_decision_record(record)))
        await run_in_threadpool(lambda: publish_status(deps, session, revision, current))
        return {**result, "break_glass": {"id": record["id"], "record": dict(signed)}}


__all__ = [
    "L4_RECHECK_ORIGINS",
    "SCM_STATUS_KIND",
    "LandingAuthority",
    "LandingDeps",
    "auto_approval_decision_record",
    "break_glass_decision_record",
    "current_break_glass",
    "decide",
    "evaluate",
    "gather_facts",
    "landing_extras",
    "landing_policy",
    "publish_status",
    "register_landing_routes",
    "revalidate",
    "trusted_base_now",
]
