from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atelier.core.capabilities.archival_recall.ranking import rank_archival_passages
from atelier.core.foundation.memory_models import ArchivalPassage


def _passage(
    pid: str,
    text: str,
    *,
    embedding: list[float] | None = None,
    tags: list[str] | None = None,
    created_at: datetime | None = None,
) -> ArchivalPassage:
    return ArchivalPassage(
        id=pid,
        agent_id="atelier:code",
        text=text,
        embedding=embedding,
        embedding_provenance="unit_test" if embedding is not None else "none",
        tags=tags or [],
        source="user",
        dedup_hash=pid,
        created_at=created_at or datetime.now(UTC),
    )


def test_pure_fts_path_ranks_lexical_match_first() -> None:
    ranked = rank_archival_passages(
        query="shopify product identity",
        passages=[
            _passage("p1", "Tracker evidence and classifier version"),
            _passage("p2", "Shopify product identity uses stable GIDs not handles"),
        ],
        top_k=2,
    )

    assert ranked[0].passage.id == "p2"
    assert ranked[0].bm25_norm == 1.0


def test_pure_vector_path_ranks_cosine_match_first() -> None:
    ranked = rank_archival_passages(
        query="semantic only",
        query_embedding=[1.0, 0.0],
        passages=[
            _passage("far", "unrelated lexical text", embedding=[0.0, 1.0]),
            _passage("near", "unrelated lexical text", embedding=[1.0, 0.0]),
        ],
        top_k=2,
    )

    assert ranked[0].passage.id == "near"
    assert ranked[0].cosine == 1.0


def test_hybrid_path_combines_vector_and_fts() -> None:
    ranked = rank_archival_passages(
        query="catalog truth",
        query_embedding=[1.0, 0.0],
        passages=[
            _passage("fts-only", "catalog truth before PDP fix", embedding=[0.0, 1.0]),
            _passage("hybrid", "catalog truth from source data", embedding=[1.0, 0.0]),
        ],
        top_k=2,
    )

    assert ranked[0].passage.id == "hybrid"
    assert ranked[0].score > ranked[1].score


def test_tag_filter_excludes_mismatched_passages() -> None:
    ranked = rank_archival_passages(
        query="identity",
        passages=[
            _passage("wrong-tag", "identity", tags=["tracker"]),
            _passage("right-tag", "identity", tags=["shopify"]),
        ],
        tags=["shopify"],
    )

    assert [item.passage.id for item in ranked] == ["right-tag"]


def test_time_filter_excludes_old_passages() -> None:
    now = datetime.now(UTC)
    ranked = rank_archival_passages(
        query="identity",
        passages=[
            _passage("old", "identity", created_at=now - timedelta(days=3)),
            _passage("new", "identity", created_at=now),
        ],
        since=now - timedelta(days=1),
    )

    assert [item.passage.id for item in ranked] == ["new"]
