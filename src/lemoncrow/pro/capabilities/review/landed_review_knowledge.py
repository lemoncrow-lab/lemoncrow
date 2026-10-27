"""Conservative source facts for a provider-observed, canonical landed Review.

An approval or a sandbox PASS is *history*, not a source-code truth or Review
policy. This adapter does not observe merges and does not accept caller-authored
fact bodies. A trusted merge observer must provide the provider-observed
canonical proof used by the existing Project Knowledge projection builder.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from typing import Any

from lemoncrow.pro.capabilities.review.evaluation_timeline import evaluation_timeline
from lemoncrow.pro.capabilities.review.post_merge_actions import MergeObservation
from lemoncrow.pro.capabilities.review.project_knowledge import (
    CanonicalAdvanceProof,
    KnowledgeFact,
    KnowledgeKey,
    KnowledgeProjection,
    KnowledgeSubject,
    RepositoryGeneration,
    build_merged_change_projection,
)

_MAX_DECISIONS = 100
_MAX_OBSERVATIONS = 100


def landed_review_facts(
    review_store: Any,
    review_id: str,
    revision_id: str,
    *,
    counted_evidence: Callable[[Any], Sequence[Any]],
) -> tuple[KnowledgeFact, ...]:
    """Select metadata-only, source-linked human dispositions and observed runs.

    Require the trusted-base authority-filtering callback rather than querying
    unfiltered evidence: revoked provider reports never silently become history.
    The Review must still point at this exact Snapshot. A different or advanced
    Snapshot requires another landing observation, never inferred continuity.
    """
    session = review_store.get_session(review_id)
    revisions = review_store.list_revisions(review_id)
    if (
        session is None
        or session.current_revision_id != revision_id
        or not revisions
        or revisions[-1].id != revision_id
    ):
        raise ValueError("landed Review facts require the exact current Review Snapshot")

    refs = (f"review:{review_id}", f"revision:{revision_id}")
    facts: list[KnowledgeFact] = []
    latest: dict[str, Any] = {}
    for event in review_store.list_decision_events(review_id):
        latest[event.decision_key] = event
    for key, event in sorted(latest.items()):
        if len(facts) >= _MAX_DECISIONS:
            break
        if (
            event.revision_id != revision_id
            or event.reviewer_id != session.reviewer_id
            or event.actor_type != "human"
            or event.action not in {"accept", "reconfirm"}
            or event.disposition != "accepted"
        ):
            continue
        fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        facts.append(
            KnowledgeFact(
                subject=KnowledgeSubject("concept", f"review-decision:{review_id}:{fingerprint}"),
                knowledge_kind="landed_review_decision",
                body=(
                    f"Human reviewer accepted a recorded decision in Review Snapshot {revision_id} "
                    f"(decision hash {fingerprint}; event {event.id}). "
                    "Historical reviewer disposition, not code evidence or governing policy; "
                    "read decision details only through the authorized Review."
                ),
                source_refs=(*refs, f"outcome:decision-event:{event.id}"),
                state="historical",
                content_access_class="metadata_only",
            )
        )

    # This is the existing SHA-256 and typed-execution verifier, not an
    # independent trust implementation. It also handles corrupted newer retry
    # receipts and superseded outcomes without falling back to an old PASS.
    timeline = evaluation_timeline(
        review_store,
        review_id,
        selected_revision_id=revision_id,
        limit=1,
        counted_evidence=counted_evidence,
    )
    current = timeline.get("current") or {}
    count = 0
    for receipt in current.get("receipts", ()):
        if count >= _MAX_OBSERVATIONS:
            break
        if (
            receipt.get("revision_id") != revision_id
            or receipt.get("superseded_by_newer_result")
            or receipt.get("raw_attempt")
            or receipt.get("trust") != "typed_observation"
            or receipt.get("execution_provenance") != "validated"
            or receipt.get("status") not in {"PASS", "FAIL"}
            or not receipt.get("evaluator_config_digest")
        ):
            continue
        evidence_id = str(receipt["evidence_id"])
        executor = str(receipt["executor_class"])
        scope = str(receipt.get("scope") or "")
        scenario_digest = hashlib.sha256(str(receipt.get("scenario") or "").encode("utf-8")).hexdigest()[:24]
        facts.append(
            KnowledgeFact(
                subject=KnowledgeSubject("concept", f"review-check:{review_id}:{evidence_id}"),
                knowledge_kind="landed_review_observation",
                body=(
                    f"Review Snapshot {revision_id} check identity {scenario_digest} ({scope}) recorded "
                    f"{receipt['status']} on {executor}; execution artifact verified by digest. "
                    "This local observation is not proof of production safety or independent evaluation."
                ),
                source_refs=(*refs, f"check:review-evidence:{evidence_id}"),
                state="historical",
                content_access_class="metadata_only",
            )
        )
        count += 1
    return tuple(facts)


def build_landed_review_knowledge(
    review_store: Any,
    review_id: str,
    revision_id: str,
    previous: RepositoryGeneration,
    current: RepositoryGeneration,
    *,
    proof: CanonicalAdvanceProof,
    merge: MergeObservation,
    counted_evidence: Callable[[Any], Sequence[Any]],
    source_facts: Sequence[KnowledgeFact] = (),
    retire: Sequence[KnowledgeKey] = (),
) -> KnowledgeProjection:
    """Prepare, but do not publish, one provider-verified merged-change projection.

    Review IDs, Snapshot IDs, merge ref and time are derived from inspected
    records, not arbitrary strings. Combine existing source-backed change facts
    in this *one* merged projection so Project Knowledge has no second writer
    for the same canonical generation. Canonical ancestry and merge authority
    are enforced by build_merged_change_projection. No model,
    checkout, privileged code execution, or network work is performed here.
    """
    if proof.authority != "provider_observed":
        raise ValueError("landed Review knowledge requires provider-observed merge authority")
    facts = landed_review_facts(
        review_store,
        review_id,
        revision_id,
        counted_evidence=counted_evidence,
    )
    review_revision = review_store.get_revision(revision_id)
    expected_head = str(getattr(review_revision, "head_sha", "") or "")
    # A settled landing is recorded by the provider integration only after
    # confirming the exact reviewed revision. Other provider-subject reports
    # must additionally bind to the immutable reviewed Git head.
    confirmed_landing = (
        merge.source == "provider_landing"
        and merge.provider_confirmed_reviewed_head
        and merge.ref.startswith("landing:")
        and (not expected_head or merge.reviewed_head == expected_head)
    )
    matched_subject = (
        merge.source == "provider_subject"
        and bool(expected_head)
        and merge.merged_head == expected_head == merge.reviewed_head
    )
    if (
        merge.revision_id != revision_id
        or merge.provider.lower() != current.repository.provider
        or not (confirmed_landing or matched_subject)
        or not merge.ref.strip()
        or not merge.observed_at.strip()
    ):
        raise ValueError("provider merge observation does not match the exact reviewed head")
    if not facts:
        raise ValueError("no eligible verified Review history for this landed Snapshot")
    return build_merged_change_projection(
        previous,
        current,
        proof=proof,
        merge_ref=f"merge:{merge.provider}:{merge.ref}",
        review_ref=f"review:{review_id}",
        revision_ref=f"revision:{revision_id}",
        facts=(*source_facts, *facts),
        observed_at=merge.observed_at,
        retire=retire,
    )
