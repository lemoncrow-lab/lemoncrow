from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml

from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.renderer import render_block_for_agent
from atelier.core.foundation.retriever import TaskContext, count_tokens, retrieve
from atelier.core.foundation.store import ContextStore

TASK = "live state change resolved from url slug verification drift"


@pytest.fixture()
def seeded_store(tmp_path: Path) -> ContextStore:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    blocks_dir = resources.files("atelier") / "infra" / "seed_blocks"
    loaded: dict[str, ReasonBlock] = {}
    for path in blocks_dir.iterdir():
        if not path.name.endswith(".yaml"):
            continue
        data = yaml.safe_load(Path(str(path)).read_text(encoding="utf-8"))
        block = ReasonBlock.model_validate(data)
        loaded[block.id] = block
        store.upsert_block(block)

    source = loaded["canonical-identifier-over-display-name"]
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
