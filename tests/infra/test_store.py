from __future__ import annotations

from typing import Any

from atelier.core.foundation.models import ReasonBlock, Rubric, Trace
from atelier.core.foundation.store import ReasoningStore
from atelier.core.service.jobs import JOB_CONSOLIDATE_BLOCKS


def _block(bid: str = "b1", domain: str = "coding", title: str = "Title", **kw: object) -> ReasonBlock:
    base: dict[str, Any] = dict(
        id=bid,
        title=title,
        domain=domain,
        situation="When doing X.",
        procedure=["Step one"],
        triggers=["foo"],
        dead_ends=["never do bar"],
    )
    base.update(kw)
    return ReasonBlock(**base)


def test_upsert_and_get_block_roundtrip(store: ReasoningStore) -> None:
    block = _block()
    store.upsert_block(block)
    fetched = store.get_block(block.id)
    assert fetched is not None
    assert fetched.title == "Title"
    assert (store.blocks_dir / f"{block.id}.md").exists()


def test_search_blocks_uses_fts(store: ReasoningStore) -> None:
    store.upsert_block(_block(bid="b1", title="Shopify product handle"))
    store.upsert_block(_block(bid="b2", title="Tracker classification"))
    results = store.search_blocks("shopify")
    assert any(b.id == "b1" for b in results)


def test_list_filters_quarantined_and_deprecated(store: ReasoningStore) -> None:
    store.upsert_block(_block(bid="active", title="A"))
    store.upsert_block(_block(bid="dep", title="B"))
    store.upsert_block(_block(bid="qua", title="C"))
    store.update_block_status("dep", "deprecated")
    store.update_block_status("qua", "quarantined")

    active = store.list_blocks()
    assert {b.id for b in active} == {"active"}

    with_dep = store.list_blocks(include_deprecated=True)
    assert {"active", "dep"}.issubset({b.id for b in with_dep})


def test_record_trace_writes_json_mirror(store: ReasoningStore) -> None:
    trace = Trace(
        id="t1",
        agent="codex",
        domain="coding",
        task="do thing",
        status="success",
    )
    store.record_trace(trace)
    assert (store.traces_dir / "t1.json").exists()
    fetched = store.get_trace("t1")
    assert fetched is not None and fetched.agent == "codex"


def test_rubric_roundtrip(store: ReasoningStore) -> None:
    r = Rubric(id="r1", domain="coding", required_checks=["a"], block_if_missing=["a"])
    store.upsert_rubric(r)
    assert (store.rubrics_dir / "r1.yaml").exists()
    fetched = store.get_rubric("r1")
    assert fetched is not None
    assert fetched.required_checks == ["a"]


def test_job_queue_roundtrip(store: ReasoningStore) -> None:
    job_id = store.enqueue_job(JOB_CONSOLIDATE_BLOCKS, {"dry_run": True}, max_attempts=2)

    claimed = store.claim_job()

    assert claimed is not None
    assert claimed["id"] == job_id
    assert claimed["job_type"] == JOB_CONSOLIDATE_BLOCKS
    assert claimed["payload"] == {"dry_run": True}
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1

    assert store.complete_job(job_id, {"written": 0}) is True

    jobs = store.list_jobs(limit=10)
    assert jobs[0]["id"] == job_id
    assert jobs[0]["status"] == "succeeded"
