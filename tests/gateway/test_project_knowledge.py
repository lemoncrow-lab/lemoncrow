from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.project_knowledge import (
    CanonicalAdvanceProof,
    CanonicalRefIdentity,
    KnowledgeFact,
    KnowledgeKey,
    KnowledgeProjection,
    KnowledgeQuery,
    KnowledgeSubject,
    ProjectKnowledgeStore,
    RepositoryGeneration,
    RepositoryIdentity,
    build_baseline_projection,
    build_merged_change_projection,
    build_source_advance_projection,
    validate_source_ref,
)
from lemoncrow.pro.capabilities.review.rules import ContentAccessRule
from lemoncrow.pro.capabilities.review.store import ReviewStore


def _repo(key: str = "acme/widgets") -> RepositoryIdentity:
    return RepositoryIdentity("github", key)


def _ref(repo: RepositoryIdentity | None = None) -> CanonicalRefIdentity:
    return CanonicalRefIdentity(repo or _repo(), "refs/heads/main")


def _generation(char: str, *, repo: RepositoryIdentity | None = None) -> RepositoryGeneration:
    return RepositoryGeneration(_ref(repo), char * 40)


def _source(path: str, char: str = "a", *, side: str = "new") -> str:
    return f"source:{side}:{path}@sha256:{char * 64}"


def _fact(
    body: str,
    *,
    path: str = "src/pricing.py",
    char: str = "a",
    state: str = "current",
    kind: str = "responsibility",
) -> KnowledgeFact:
    return KnowledgeFact(
        subject=KnowledgeSubject("module", "pricing"),
        knowledge_kind=kind,
        body=body,
        source_refs=(_source(path, char),),
        state=state,  # type: ignore[arg-type]
    )


def _chain() -> tuple[
    RepositoryIdentity,
    RepositoryGeneration,
    RepositoryGeneration,
    RepositoryGeneration,
    tuple[KnowledgeProjection, ...],
]:
    repo = _repo()
    g1 = _generation("1", repo=repo)
    g2 = _generation("2", repo=repo)
    g3 = _generation("3", repo=repo)
    baseline = build_baseline_projection(
        g1,
        facts=(_fact("Pricing owns quote calculation."),),
        observed_at="2026-10-01T10:00:00Z",
    )
    merged = build_merged_change_projection(
        g1,
        g2,
        proof=CanonicalAdvanceProof(
            authority="provider_observed",
            observed_ref="refs/heads/main",
            descendant=True,
        ),
        merge_ref="merge:github:acme/widgets#42",
        review_ref="review:r-42",
        revision_ref="revision:rv-42",
        facts=(
            _fact("Pricing owns quote and discount calculation.", char="b"),
            _fact(
                "Change 42 added discount calculation.",
                char="b",
                state="historical",
                kind="change",
            ),
        ),
        observed_at="2026-10-02T10:00:00Z",
    )
    advanced = build_source_advance_projection(
        g2,
        g3,
        proof=CanonicalAdvanceProof(
            authority="local_canonical_ref",
            observed_ref="refs/heads/main",
            descendant=True,
        ),
        facts=(_fact("Pricing owns quote, discount and tax calculation.", char="c"),),
        observed_at="2026-10-03T10:00:00Z",
    )
    return repo, g1, g2, g3, (baseline, merged, advanced)


def test_repository_ref_generation_identity_is_canonical_and_content_addressed() -> None:
    repo = _repo()
    same_repo = RepositoryIdentity("GITHUB", "acme/widgets")
    assert repo.id == same_repo.id

    canonical = _ref(repo)
    generation = RepositoryGeneration(canonical, "A" * 40, tree_digest="b" * 64)
    assert canonical.name == "refs/heads/main"
    assert generation.revision == "a" * 40
    assert generation.id.startswith("generation:")
    assert validate_source_ref(generation.source_ref).kind == "generation"

    with pytest.raises(ValueError, match="refs/heads"):
        CanonicalRefIdentity(repo, "feature/pk0")
    with pytest.raises(ValueError, match="immutable git sha"):
        RepositoryGeneration(canonical, "workspace-dirty")


@pytest.mark.parametrize(
    "ref",
    [
        "garbage",
        "source:new:/etc/passwd@sha256:" + "a" * 64,
        "source:new:../secret.py@sha256:" + "a" * 64,
        "source:head:src/a.py@sha256:" + "a" * 64,
        "source:new:src/a.py@sha256:xyz",
        "claim:anything",
        "generation:not-a-digest",
    ],
)
def test_source_ref_validation_fails_closed(ref: str) -> None:
    with pytest.raises(ValueError):
        validate_source_ref(ref)


def test_source_ref_validation_reuses_review_exact_source_vocabulary() -> None:
    row = validate_source_ref(_source("src/auth.py"))
    assert row.kind == "source"
    assert row.path == "src/auth.py"
    assert row.ref == _source("src/auth.py")
    assert validate_source_ref("target:t-1").kind == "target"
    assert validate_source_ref("check:provider_ci:build-17").kind == "check"


def test_source_derived_fact_requires_filterable_path_provenance() -> None:
    with pytest.raises(ValueError, match="source path"):
        KnowledgeFact(
            subject=KnowledgeSubject("repository", "root"),
            knowledge_kind="owner",
            body="Repository metadata.",
            source_refs=("review:r-1",),
        )

    fact = KnowledgeFact(
        subject=KnowledgeSubject("repository", "root"),
        knowledge_kind="owner",
        body="Repository metadata.",
        source_refs=("review:r-1",),
        content_access_class="metadata_only",
    )
    assert fact.content_access_class == "metadata_only"


def test_direct_source_advance_refuses_review_and_workspace_authority() -> None:
    g1 = _generation("1")
    g2 = _generation("2")

    with pytest.raises(ValueError, match="only cite"):
        build_source_advance_projection(
            g1,
            g2,
            proof=CanonicalAdvanceProof(
                authority="local_canonical_ref",
                observed_ref="refs/heads/main",
                descendant=True,
            ),
            facts=(
                KnowledgeFact(
                    subject=KnowledgeSubject("module", "pricing"),
                    knowledge_kind="change",
                    body="A direct push changed pricing.",
                    source_refs=("review:invented", _source("src/pricing.py")),
                ),
            ),
            observed_at="2026-10-03T10:00:00Z",
        )

    with pytest.raises(ValueError, match="working-tree"):
        CanonicalAdvanceProof(
            authority="local_canonical_ref",
            observed_ref="refs/heads/main",
            descendant=True,
            uses_working_tree=True,
        ).validate(g1, g2)

    with pytest.raises(ValueError, match="configured canonical ref"):
        CanonicalAdvanceProof(
            authority="local_canonical_ref",
            observed_ref="refs/heads/feature",
            descendant=True,
        ).validate(g1, g2)

    with pytest.raises(ValueError, match="descendant"):
        CanonicalAdvanceProof(
            authority="local_canonical_ref",
            observed_ref="refs/heads/main",
            descendant=False,
        ).validate(g1, g2)


def test_merge_projection_preserves_review_revision_merge_and_source_provenance() -> None:
    g1 = _generation("1")
    g2 = _generation("2")
    projection = build_merged_change_projection(
        g1,
        g2,
        proof=CanonicalAdvanceProof(
            authority="provider_observed",
            observed_ref="refs/heads/main",
            descendant=True,
        ),
        merge_ref="merge:github:acme/widgets#42",
        review_ref="review:r-42",
        revision_ref="revision:rv-42",
        facts=(_fact("Pricing now calculates discounts."),),
        observed_at="2026-10-02T10:00:00Z",
    )
    entry = projection.entries[0]
    assert entry.review_refs == ("review:r-42",)
    assert entry.revision_refs == ("revision:rv-42",)
    assert entry.merge_refs == ("merge:github:acme/widgets#42",)
    assert g2.source_ref in entry.source_refs
    assert projection.event.kind == "merged_change"

    with pytest.raises(ValueError, match="merge, review, and revision"):
        build_merged_change_projection(
            g1,
            g2,
            proof=CanonicalAdvanceProof(
                authority="provider_observed",
                observed_ref="refs/heads/main",
                descendant=True,
            ),
            merge_ref="review:not-a-merge",
            review_ref="review:r-42",
            revision_ref="revision:rv-42",
            facts=(_fact("bad"),),
            observed_at="2026-10-02T10:00:00Z",
        )

    with pytest.raises(ValueError, match="provider-observed"):
        build_merged_change_projection(
            g1,
            g2,
            proof=CanonicalAdvanceProof(
                authority="local_canonical_ref",
                observed_ref="refs/heads/main",
                descendant=True,
            ),
            merge_ref="merge:github:acme/widgets#42",
            review_ref="review:r-42",
            revision_ref="revision:rv-42",
            facts=(_fact("not provider landed"),),
            observed_at="2026-10-02T10:00:00Z",
        )


def test_store_supersession_and_historical_events_are_explicit(tmp_path: Path) -> None:
    repo, _g1, _g2, g3, projections = _chain()
    store = ProjectKnowledgeStore(tmp_path / "project-knowledge.sqlite3")
    for projection in projections:
        assert store.apply(projection) is True

    result = store.query(repo)
    current = [entry for entry in result.entries if entry.knowledge_kind == "responsibility"]
    historical = [entry for entry in result.entries if entry.knowledge_kind == "change"]
    assert len(current) == 1
    assert current[0].body == "Pricing owns quote, discount and tax calculation."
    assert current[0].state == "current"
    assert len(historical) == 1
    assert historical[0].state == "historical"
    assert result.current_generation == g3.id

    all_states = store.query(
        repo,
        query=KnowledgeQuery(states=("current", "historical", "superseded"), limit=20),
    )
    superseded = [entry for entry in all_states.entries if entry.state == "superseded"]
    assert [entry.body for entry in superseded] == [
        "Pricing owns quote and discount calculation.",
        "Pricing owns quote calculation.",
    ]
    assert store.apply(projections[-1]) is False


def test_retire_key_supersedes_without_replacement(tmp_path: Path) -> None:
    repo = _repo()
    g1 = _generation("1", repo=repo)
    g2 = _generation("2", repo=repo)
    key = KnowledgeKey(KnowledgeSubject("module", "pricing"), "responsibility")
    store = ProjectKnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.apply(
        build_baseline_projection(
            g1,
            facts=(_fact("Pricing owns quotes."),),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    store.apply(
        build_source_advance_projection(
            g1,
            g2,
            proof=CanonicalAdvanceProof(
                authority="provider_observed",
                observed_ref="refs/heads/main",
                descendant=True,
            ),
            facts=(),
            retire=(key,),
            observed_at="2026-10-02T10:00:00Z",
        )
    )
    assert store.query(repo).entries == ()
    rows = store.query(repo, query=KnowledgeQuery(states=("superseded",), limit=10)).entries
    assert len(rows) == 1
    assert rows[0].last_observed_generation == g2.id


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    repo, _g1, _g2, _g3, projections = _chain()
    first = ProjectKnowledgeStore(tmp_path / "first.sqlite3")
    for projection in projections:
        first.apply(projection)
    before = first.deterministic_digest(repo)

    assert first.rebuild(repo, projections) == before

    second = ProjectKnowledgeStore(tmp_path / "second.sqlite3")
    assert second.rebuild(repo, projections) == before


def test_review_store_migration_backend_matches_standalone_digest(tmp_path: Path) -> None:
    repo, _g1, _g2, _g3, projections = _chain()
    standalone = ProjectKnowledgeStore(tmp_path / "standalone.sqlite3")
    review_backed = ProjectKnowledgeStore(ReviewStore(tmp_path / "review-store"))

    for projection in projections:
        standalone.apply(projection)
        review_backed.apply(projection)

    assert review_backed.deterministic_digest(repo) == standalone.deterministic_digest(repo)
    assert review_backed.current_generation(repo) == projections[-1].generation.id


def test_stale_or_out_of_order_projection_is_rejected(tmp_path: Path) -> None:
    repo, _g1, _g2, _g3, projections = _chain()
    store = ProjectKnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.apply(projections[0])
    with pytest.raises(ValueError, match="stale or out of order"):
        store.apply(projections[2])
    assert store.current_generation(repo) == projections[0].generation.id


def _rule(mode: str, path: str) -> ContentAccessRule:
    return ContentAccessRule(
        mode=mode,  # type: ignore[arg-type]
        path_scope=(path,),
        origin="base revision .lemoncrow/review.yaml",
        trust_class="trusted_base_repository",
        source_identity="base:review.yaml",
    )


def test_query_supports_bounded_path_subject_and_free_text_filters(tmp_path: Path) -> None:
    repo = _repo()
    generation = _generation("1", repo=repo)
    store = ProjectKnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.apply(
        build_baseline_projection(
            generation,
            facts=(
                _fact("Pricing calculates seasonal discounts.", path="src/pricing.py"),
                KnowledgeFact(
                    subject=KnowledgeSubject("path", "docs/contracts.md"),
                    knowledge_kind="contract",
                    body="Checkout contract documents tax semantics.",
                    source_refs=(_source("docs/contracts.md", "b"),),
                ),
            ),
            observed_at="2026-10-01T10:00:00Z",
        )
    )

    by_path = store.query(repo, query=KnowledgeQuery(paths=("docs/contracts.md",), limit=5))
    assert [entry.knowledge_kind for entry in by_path.entries] == ["contract"]

    by_term = store.query(repo, query=KnowledgeQuery(term="seasonal discounts", limit=5))
    assert [entry.knowledge_kind for entry in by_term.entries] == ["responsibility"]

    by_subject = store.query(
        repo,
        query=KnowledgeQuery(subjects=(KnowledgeSubject("module", "pricing"),), limit=5),
    )
    assert [entry.knowledge_kind for entry in by_subject.entries] == ["responsibility"]


def test_empty_baseline_degrades_without_inventing_facts(tmp_path: Path) -> None:
    repo = _repo()
    generation = _generation("1", repo=repo)
    store = ProjectKnowledgeStore(tmp_path / "knowledge.sqlite3")
    projection = build_baseline_projection(
        generation,
        facts=(),
        observed_at="2026-10-01T10:00:00Z",
    )
    assert store.apply(projection) is True
    result = store.query(repo)
    assert result.entries == ()
    assert result.current_generation == generation.id


def test_query_rechecks_current_content_access_for_old_entries(tmp_path: Path) -> None:
    repo = _repo()
    generation = _generation("1", repo=repo)
    store = ProjectKnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.apply(
        build_baseline_projection(
            generation,
            facts=(_fact("Secret-backed pricing context.", path="src/secret.py"),),
            observed_at="2026-10-01T10:00:00Z",
        )
    )

    assert len(store.query(repo).entries) == 1

    denied = store.query(
        repo,
        content_access=(_rule("ai_denied", "src/secret.py"),),
        route_trust="external",
    )
    assert denied.entries == ()
    assert denied.filtered_by_content_access == 1

    external = store.query(
        repo,
        content_access=(_rule("customer_local_only", "src/secret.py"),),
        route_trust="external",
    )
    local = store.query(
        repo,
        content_access=(_rule("customer_local_only", "src/secret.py"),),
        route_trust="customer_local",
    )
    assert external.entries == ()
    assert len(local.entries) == 1


def test_purge_is_repository_scoped_and_leaves_authoritative_files(tmp_path: Path) -> None:
    repo = _repo()
    other = _repo("acme/other")
    authoritative = tmp_path / "authoritative-review-evidence.json"
    authoritative.write_text('{"keep": true}', encoding="utf-8")

    store = ProjectKnowledgeStore(tmp_path / "derived.sqlite3")
    store.apply(
        build_baseline_projection(
            _generation("1", repo=repo),
            facts=(_fact("Pricing facts."),),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    store.apply(
        build_baseline_projection(
            _generation("4", repo=other),
            facts=(_fact("Other facts.", path="src/other.py"),),
            observed_at="2026-10-01T10:00:00Z",
        )
    )

    assert store.purge_repository(repo) > 0
    assert store.query(repo).entries == ()
    assert len(store.query(other).entries) == 1
    assert authoritative.read_text(encoding="utf-8") == '{"keep": true}'


def test_retention_prunes_rebuildable_history_not_current_projection(tmp_path: Path) -> None:
    repo, _g1, _g2, _g3, projections = _chain()
    store = ProjectKnowledgeStore(tmp_path / "knowledge.sqlite3")
    for projection in projections:
        store.apply(projection)

    pruned = store.prune_before(repo, cutoff="2026-10-03T00:00:00Z")
    assert pruned["entries"] >= 2
    current = store.query(repo)
    assert [entry.body for entry in current.entries if entry.state == "current"] == [
        "Pricing owns quote, discount and tax calculation."
    ]
    assert current.current_generation == projections[-1].generation.id


def test_query_is_hard_bounded() -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        KnowledgeQuery(limit=101)
    with pytest.raises(ValueError, match="between 1 and 100"):
        KnowledgeQuery(limit=0)
    with pytest.raises(ValueError, match="exceeds 256"):
        KnowledgeQuery(term="x" * 257)
