"""Provider-observed landed history: human disposition + actual typed execution only."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.landed_review_knowledge import (
    build_landed_review_knowledge,
    landed_review_facts,
)
from lemoncrow.pro.capabilities.review.post_merge_actions import MergeObservation
from lemoncrow.pro.capabilities.review.project_knowledge import (
    CanonicalAdvanceProof,
    CanonicalRefIdentity,
    KnowledgeFact,
    KnowledgeQuery,
    KnowledgeSubject,
    ProjectKnowledgeStore,
    RepositoryGeneration,
    RepositoryIdentity,
    build_baseline_projection,
)
from tests.gateway.test_review_check_runs_api import _review, _run
from tests.gateway.test_review_decisions import _capture, _event, _git_repo
from tests.gateway.test_review_hermetic import needs_bwrap

REPO = RepositoryIdentity("github", "project/history")
REF = CanonicalRefIdentity(REPO, "refs/heads/main")


def _generation(char: str) -> RepositoryGeneration:
    return RepositoryGeneration(REF, char * 40)


def _proof() -> CanonicalAdvanceProof:
    return CanonicalAdvanceProof(authority="provider_observed", observed_ref=REF.name, descendant=True)


def _landed(store, session_id, revision_id, *, counted_evidence=None, proof=None, merge=None, source_facts=()):
    revision = store.get_revision(revision_id)
    reviewed_sha = str(getattr(revision, "head_sha", "") or "")
    observed = merge or MergeObservation(
        source="provider_landing",
        provider="github",
        merged_head=reviewed_sha,
        reviewed_head=reviewed_sha,
        revision_id=revision_id,
        observed_at="2026-10-27T15:00:00Z",
        ref="landing:confirmed",
        provider_confirmed_reviewed_head=True,
    )
    return build_landed_review_knowledge(
        store,
        session_id,
        revision_id,
        _generation("1"),
        _generation("2"),
        proof=proof or _proof(),
        merge=observed,
        counted_evidence=counted_evidence or (lambda _revision: store.list_evidence(session_id)),
        source_facts=source_facts,
    )


def test_final_human_decision_becomes_historical_source_linked_landed_knowledge(tmp_path: Path) -> None:
    store, session, revision = _capture(tmp_path / "db", _git_repo(tmp_path))
    event = store.append_decision_event(
        replace(
            _event(session, revision, "accept", "accepted", key="refund-policy"),
            option_key="do not reveal the vault token",
            reason="vault-secret-key",
        ),
        expected_prior_event_id="",
    )
    projection = _landed(store, session.id, revision.id)
    assert projection.event.kind == "merged_change"
    assert len(projection.entries) == 1
    (entry,) = projection.entries
    assert entry.state == "historical"
    assert entry.support == "deterministic"
    assert entry.content_access_class == "metadata_only"
    assert entry.knowledge_kind == "landed_review_decision"
    assert "refund-policy" not in entry.body
    assert "vault-secret" not in entry.body
    assert "do not reveal" not in entry.body
    assert "decision hash" in entry.body
    assert "governing policy" in entry.body
    assert f"outcome:decision-event:{event.id}" in entry.source_refs
    assert entry.review_refs == (f"review:{session.id}",)
    assert entry.revision_refs == (f"revision:{revision.id}",)
    assert entry.merge_refs == ("merge:github:landing:confirmed",)

    knowledge = ProjectKnowledgeStore(store)
    assert knowledge.apply(
        build_baseline_projection(
            _generation("1"),
            observed_at="2026-10-26T10:00:00Z",
            facts=(
                KnowledgeFact(
                    subject=KnowledgeSubject("repository", "history"),
                    knowledge_kind="inventory",
                    body="Repository exists",
                    source_refs=(_generation("1").source_ref,),
                    content_access_class="metadata_only",
                ),
            ),
        )
    )
    assert knowledge.apply(projection) is True
    stored = knowledge.entry(REPO, entry.entry_id)
    assert stored == entry
    assert knowledge.apply(projection) is False  # idempotent, not a second merge
    matches = knowledge.query(REPO, query=KnowledgeQuery(knowledge_kinds=("landed_review_decision",)))
    assert len(matches.entries) == 1
    assert matches.entries[0].entry_id == entry.entry_id
    assert matches.entries[0].merge_refs == ("merge:github:landing:confirmed",)


def test_merge_projection_combines_authoritative_source_facts_with_historical_review_metadata(tmp_path: Path) -> None:
    store, session, revision = _capture(tmp_path / "db", _git_repo(tmp_path))
    store.append_decision_event(_event(session, revision, "accept", "accepted"), expected_prior_event_id="")
    path_fact = KnowledgeFact(
        subject=KnowledgeSubject("path", "src/store.py"),
        knowledge_kind="implementation",
        body="Committed source implements pruning.",
        source_refs=("source:new:src/store.py@sha256:" + "b" * 64,),
    )
    projection = _landed(store, session.id, revision.id, source_facts=(path_fact,))
    assert {entry.knowledge_kind for entry in projection.entries} == {
        "landed_review_decision",
        "implementation",
    }
    assert projection.event.kind == "merged_change"
    assert all(entry.merge_refs == ("merge:github:landing:confirmed",) for entry in projection.entries)
    assert next(row for row in projection.entries if row.knowledge_kind == "implementation").source_paths == (
        "src/store.py",
    )
    assert next(row for row in projection.entries if row.knowledge_kind == "landed_review_decision").source_paths == ()


def test_reopened_or_model_disposition_does_not_turn_into_canonical_history(tmp_path: Path) -> None:
    store, session, revision = _capture(tmp_path / "db", _git_repo(tmp_path))
    first = store.append_decision_event(_event(session, revision, "accept", "accepted"), expected_prior_event_id="")
    store.append_decision_event(
        _event(session, revision, "reopen", "open"),
        expected_prior_event_id=first.id,
    )
    assert (
        landed_review_facts(
            store,
            session.id,
            revision.id,
            counted_evidence=lambda _revision: (),
        )
        == ()
    )
    with pytest.raises(ValueError, match="no eligible"):
        _landed(store, session.id, revision.id)

    model = replace(_event(session, revision, "accept", "accepted", key="model-guidance"), actor_type="agent")
    # The store may reject non-human actions directly; either way, it cannot be canonical knowledge.
    try:
        store.append_decision_event(model, expected_prior_event_id="")
    except ValueError:
        pass
    assert landed_review_facts(store, session.id, revision.id, counted_evidence=lambda _revision: ()) == ()


def test_unproven_merge_and_wrong_snapshot_cannot_publish_decision_history(tmp_path: Path) -> None:
    store, session, revision = _capture(tmp_path / "db", _git_repo(tmp_path))
    store.append_decision_event(_event(session, revision, "accept", "accepted"), expected_prior_event_id="")
    with pytest.raises(ValueError, match="provider-observed"):
        _landed(
            store,
            session.id,
            revision.id,
            proof=CanonicalAdvanceProof(
                authority="local_canonical_ref",
                observed_ref=REF.name,
                descendant=True,
            ),
        )
    with pytest.raises(ValueError, match="exact current Review Snapshot"):
        _landed(store, session.id, "not-this-Snapshot")
    wrong_merge = MergeObservation(
        source="provider_subject",
        provider="github",
        merged_head="a" * 40,
        reviewed_head="b" * 40,
        revision_id=revision.id,
        observed_at="2026-10-27T15:00:00Z",
        ref="provider:unmatched",
    )
    with pytest.raises(ValueError, match="provider merge observation"):
        _landed(store, session.id, revision.id, merge=wrong_merge)
    unconfirmed = MergeObservation(
        source="provider_landing",
        provider="github",
        merged_head="",
        reviewed_head="",
        revision_id=revision.id,
        observed_at="2026-10-27T15:00:00Z",
        ref="landing:not-confirmed",
        provider_confirmed_reviewed_head=False,
    )
    with pytest.raises(ValueError, match="provider merge observation"):
        _landed(store, session.id, revision.id, merge=unconfirmed)
    with pytest.raises(ValueError, match="provider merge observation"):
        _landed(store, session.id, revision.id, merge=replace(unconfirmed, provider="gitlab"))


@needs_bwrap
def test_real_sandbox_observation_is_historical_without_promoting_claimed_pass(tmp_path: Path) -> None:
    review = _review(tmp_path)
    result = _run(review, "checks.test")
    assert result.status_code == 200, result.text
    facts = landed_review_facts(
        review.store,
        review.review_id,
        review.revision_id,
        counted_evidence=lambda _rev: review.store.list_evidence(review.review_id),
    )
    observations = [fact for fact in facts if fact.knowledge_kind == "landed_review_observation"]
    assert observations, "a real executed and validated check should contribute historical evidence"
    assert all(fact.state == "historical" and fact.content_access_class == "metadata_only" for fact in observations)
    assert all("not proof of production safety" in fact.body for fact in observations)
    assert all("check:review-evidence:" in " ".join(fact.source_refs) for fact in observations)
    assert all("argv" not in fact.body and "secret" not in fact.body.lower() for fact in observations)
    projection = _landed(review.store, review.review_id, review.revision_id)
    assert any(row.knowledge_kind == "landed_review_observation" for row in projection.entries)
    assert all(row.review_refs == (f"review:{review.review_id}",) for row in projection.entries)
    # The authority-filtered evidence can remove a revoked record before projection.
    assert (
        landed_review_facts(
            review.store,
            review.review_id,
            review.revision_id,
            counted_evidence=lambda _rev: (),
        )
        == ()
    )


def test_unverified_claimed_check_pass_is_not_landed_execution_evidence(tmp_path: Path) -> None:
    """A normal human/agent verification claim lacks the typed runner artifact."""
    from tests.gateway.test_review_check_runs_api import HEADERS

    review = _review(tmp_path)
    claimed = review.client.post(
        f"/api/reviews/{review.review_id}/verification",
        headers=HEADERS,
        json={
            "revision_id": review.revision_id,
            "name": "tests",
            "status": "PASS",
            "source": "test",
            "source_ref": "ci:claimed-pass",
        },
    )
    assert claimed.status_code == 201, claimed.text
    facts = landed_review_facts(
        review.store,
        review.review_id,
        review.revision_id,
        counted_evidence=lambda _rev: review.store.list_evidence(review.review_id),
    )
    assert facts == ()
    with pytest.raises(ValueError, match="no eligible"):
        _landed(review.store, review.review_id, review.revision_id)
