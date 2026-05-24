from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.renderer import render_block_for_agent
from atelier.core.foundation.retriever import TaskContext, count_tokens, retrieve
from atelier.core.foundation.store import ContextStore

TASK = "live state change resolved from url slug verification drift"


@pytest.fixture()
def seeded_store(tmp_path: Path) -> ContextStore:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    source = ReasonBlock(
        id="canonical-identifier-over-display-name",
        title="Canonical Identifier Over Display Name",
        domain="state.change",
        task_types=["integration_change", "data_write", "rollback"],
        triggers=["slug", "handle", "title", "url"],
        tool_patterns=["api.write", "db.write"],
        situation=(
            "Human-readable labels such as titles, URLs, paths, and display names can "
            "drift. Mutations and rollbacks should target a stable canonical identifier."
        ),
        dead_ends=[
            "resolve target from url slug alone",
            "use display name as stable identity",
        ],
        procedure=[
            "Resolve the target through its canonical stable identifier.",
            "Record that identifier before the write.",
            "Use the same identifier for the mutation and the readback.",
        ],
        verification=[
            "A canonical identifier was recorded before the write.",
            "The same identifier was used for readback.",
        ],
        failure_signals=["wrong target updated", "ambiguous match set"],
        required_rubrics=["rubric_state_change_safety"],
        when_not_to_apply="Pure read-only exploration where no state mutation or rollback will happen.",
    )
    store.upsert_block(source)
    for idx in range(6):
        clone = source.model_copy(
            update={
                "id": f"canonical-identifier-near-dup-{idx}",
                "title": f"Canonical Identifier Near Duplicate {idx}",
                "success_count": 0,
                "failure_count": idx + 3,
            }
        )
        store.upsert_block(clone)

    return store


def _tokens(blocks: list[ReasonBlock]) -> int:
    return sum(count_tokens(render_block_for_agent(block)) for block in blocks)


def test_dedup_and_budget_cut_tokens_at_least_30pct(seeded_store: ContextStore) -> None:
    ctx = TaskContext(
        task=TASK,
        domain="state.change",
        files=["services/integrations/publish.py"],
        tools=["api.write", "deploy.apply"],
        errors=["write succeeded but state did not change"],
    )

    naive = [item.block for item in retrieve(seeded_store, ctx, limit=10, dedup=False, token_budget=None)]
    tuned = [item.block for item in retrieve(seeded_store, ctx, limit=10, dedup=True, token_budget=2000)]

    naive_tok = _tokens(naive)
    tuned_tok = _tokens(tuned)
    assert tuned_tok <= naive_tok * 0.7, f"only {(1 - tuned_tok / naive_tok) * 100:.1f}% reduction"
    assert naive[0].id == tuned[0].id
