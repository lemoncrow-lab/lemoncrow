from __future__ import annotations

from collections.abc import Sequence

from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.retriever import TaskContext, retrieve
from atelier.core.foundation.store import ContextStore


def _block(
    bid: str,
    *,
    domain: str = "coding",
    triggers: Sequence[str] = (),
    files: Sequence[str] = (),
    tools: Sequence[str] = (),
    failures: Sequence[str] = (),
    title: str = "T",
) -> ReasonBlock:
    return ReasonBlock(
        id=bid,
        title=title,
        domain=domain,
        situation="ctx",
        procedure=["do"],
        triggers=list(triggers),
        file_patterns=list(files),
        tool_patterns=list(tools),
        failure_signals=list(failures),
    )


def test_retrieve_scores_by_domain_and_overlap(store: ContextStore) -> None:
    store.upsert_block(_block("a", domain="coding", title="domain match", triggers=["alpha"]))
    store.upsert_block(_block("b", domain="other", title="other domain"))
    store.upsert_block(
        _block(
            "c",
            domain="coding",
            title="file match",
            files=["src/foo/**"],
            tools=["bash"],
            triggers=["alpha"],
        )
    )
    ctx = TaskContext(task="alpha task", domain="coding", files=["src/foo/bar.py"], tools=["bash"])
    scored = retrieve(store, ctx, limit=5)
    ids = [s.block.id for s in scored]
    assert "c" in ids and "a" in ids
    assert ids.index("c") < ids.index("a")  # c scored higher


def test_retrieve_excludes_deprecated_and_quarantined(store: ContextStore) -> None:
    store.upsert_block(_block("keep", triggers=["foo"]))
    store.upsert_block(_block("dep", triggers=["foo"]))
    store.upsert_block(_block("qua", triggers=["foo"]))
    store.update_block_status("dep", "deprecated")
    store.update_block_status("qua", "quarantined")

    ctx = TaskContext(task="foo task", domain="coding")
    ids = {s.block.id for s in retrieve(store, ctx)}
    assert "keep" in ids
    assert "dep" not in ids and "qua" not in ids
