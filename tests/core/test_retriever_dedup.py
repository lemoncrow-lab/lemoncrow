from __future__ import annotations

from collections.abc import Sequence

from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.renderer import render_block_for_agent
from atelier.core.foundation.retriever import TaskContext, count_tokens, retrieve
from atelier.core.foundation.store import ContextStore


def _block(
    bid: str,
    *,
    title: str,
    procedure: Sequence[str],
    dead_ends: Sequence[str] = (),
    files: Sequence[str] = (),
    success_count: int = 0,
    failure_count: int = 0,
) -> ReasonBlock:
    return ReasonBlock(
        id=bid,
        title=title,
        domain="coding",
        situation="Changing retrieval logic for ReasonBlocks.",
        triggers=["retriever", "reasonblock"],
        file_patterns=list(files),
        dead_ends=list(dead_ends),
        procedure=list(procedure),
        success_count=success_count,
        failure_count=failure_count,
    )


def test_dedup_collapses_near_duplicate_pair(store: ContextStore) -> None:
    keeper = _block(
        "keeper",
        title="Keeper",
        files=["src/**"],
        dead_ends=["Returning duplicate procedures to the agent"],
        procedure=[
            "Score candidate ReasonBlocks before filtering",
            "Remove near-duplicate dead-end and procedure text",
            "Keep the higher-ranked candidate",
        ],
        success_count=3,
    )
    duplicate = keeper.model_copy(
        update={
            "id": "duplicate",
            "title": "Duplicate",
            "file_patterns": [],
            "success_count": 0,
            "failure_count": 5,
        }
    )
    distinct = _block(
        "distinct",
        title="Distinct",
        procedure=["Measure compact rendered tokens", "Pack blocks under a fixed budget"],
    )
    store.upsert_block(keeper)
    store.upsert_block(duplicate)
    store.upsert_block(distinct)

    ctx = TaskContext(
        task="retriever reasonblock dedup",
        domain="coding",
        files=["src/atelier/core/foundation/retriever.py"],
    )

    naive_ids = [s.block.id for s in retrieve(store, ctx, limit=5, dedup=False, token_budget=None)]
    tuned_ids = [s.block.id for s in retrieve(store, ctx, limit=5, dedup=True, token_budget=None)]

    assert naive_ids[0] == "keeper"
    assert "duplicate" in naive_ids
    assert "keeper" in tuned_ids
    assert "duplicate" not in tuned_ids


def test_token_budget_greedy_packs_highest_scoring_blocks(store: ContextStore) -> None:
    top = _block(
        "top",
        title="Top",
        files=["src/**"],
        procedure=["Use the highest-scoring block first"],
    )
    large = _block(
        "large",
        title="Large",
        procedure=[" ".join(["large-token-payload"] * 500)],
    )
    small = _block(
        "small",
        title="Small",
        procedure=["Fit the remaining token budget"],
    )
    store.upsert_block(top)
    store.upsert_block(large)
    store.upsert_block(small)

    ctx = TaskContext(
        task="retriever reasonblock budget",
        domain="coding",
        files=["src/atelier/core/foundation/retriever.py"],
    )
    budget = count_tokens(render_block_for_agent(top)) + count_tokens(render_block_for_agent(small))
    ids = [s.block.id for s in retrieve(store, ctx, limit=3, token_budget=budget, dedup=False)]

    assert ids == ["top", "small"]
