from __future__ import annotations

from collections.abc import Sequence

from lemoncrow.core.foundation.models import Playbook
from lemoncrow.core.foundation.retriever import TaskContext, retrieve
from lemoncrow.infra.storage.bundle import StoreBundle


def _block(
    bid: str,
    *,
    domain: str = "coding",
    triggers: Sequence[str] = (),
    files: Sequence[str] = (),
    tools: Sequence[str] = (),
    failures: Sequence[str] = (),
    title: str = "T",
) -> Playbook:
    return Playbook(
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


def test_retrieve_scores_by_domain_and_overlap(store: StoreBundle) -> None:
    store.knowledge.upsert_block(_block("a", domain="coding", title="domain match", triggers=["alpha"]))
    store.knowledge.upsert_block(_block("b", domain="other", title="other domain"))
    store.knowledge.upsert_block(
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


def test_retrieve_excludes_deprecated_and_quarantined(store: StoreBundle) -> None:
    store.knowledge.upsert_block(_block("keep", triggers=["foo"]))
    store.knowledge.upsert_block(_block("dep", triggers=["foo"]))
    store.knowledge.upsert_block(_block("qua", triggers=["foo"]))
    store.knowledge.update_block_status("dep", "deprecated")
    store.knowledge.update_block_status("qua", "quarantined")

    ctx = TaskContext(task="foo task", domain="coding")
    ids = {s.block.id for s in retrieve(store, ctx)}
    assert "keep" in ids
    assert "dep" not in ids and "qua" not in ids


# --------------------------------------------------------------------------- #
# Block tiering: E3 / E2 / E1                                                  #
# --------------------------------------------------------------------------- #


def _tiered_block(bid: str, tier: str, **kw: object) -> Playbook:
    return Playbook(
        id=bid,
        title=f"{tier}-block",
        domain="coding",
        situation="ctx",
        procedure=["do"],
        tier=tier,  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def test_e3_blocks_always_prepended(store: StoreBundle) -> None:
    """E3 blocks come first regardless of relevance score."""
    store.knowledge.upsert_block(_tiered_block("e3-rule", "e3", triggers=["universal"]))
    store.knowledge.upsert_block(_block("e2-a", domain="coding", triggers=["alpha"]))
    ctx = TaskContext(task="alpha task", domain="coding")
    scored = retrieve(store, ctx, limit=5)
    ids = [s.block.id for s in scored]
    assert "e3-rule" in ids
    # E3 block must appear before any E2 block
    assert ids.index("e3-rule") < ids.index("e2-a")


def test_e1_blocks_gated_by_errors(store: StoreBundle) -> None:
    """E1 blocks are injected only when ctx.errors is non-empty."""
    store.knowledge.upsert_block(_tiered_block("e1-proc", "e1", triggers=["bug"]))
    store.knowledge.upsert_block(_block("e2-b", domain="coding", triggers=["bug"]))

    # Without errors: E1 should be absent
    ctx_clean = TaskContext(task="bug fix", domain="coding")
    ids_clean = {s.block.id for s in retrieve(store, ctx_clean, limit=10)}
    assert "e1-proc" not in ids_clean
    assert "e2-b" in ids_clean

    # With errors: E1 should be present
    ctx_err = TaskContext(task="bug fix", domain="coding", errors=["TypeError: NoneType"])
    ids_err = {s.block.id for s in retrieve(store, ctx_err, limit=10)}
    assert "e1-proc" in ids_err


def test_e3_score_is_1(store: StoreBundle) -> None:
    """E3 blocks get score=1.0 so they sort first in downstream processing."""
    store.knowledge.upsert_block(_tiered_block("e3-x", "e3"))
    ctx = TaskContext(task="anything", domain="coding")
    scored = retrieve(store, ctx, limit=10)
    e3_entries = [s for s in scored if s.block.id == "e3-x"]
    assert e3_entries, "E3 block should appear in results"
    assert e3_entries[0].score == 1.0
