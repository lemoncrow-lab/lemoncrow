"""Tests for memory interoperability wrappers."""

from __future__ import annotations

from pathlib import Path

from atelier.core.foundation.memory_models import ArchivalPassage, MemoryBlock
from atelier.infra.memory_bridges.openmemory import OpenMemoryAdapter, OpenMemoryMemoryStore


def test_openmemory_adapter_disabled_by_default() -> None:
    adapter = OpenMemoryAdapter()
    result = adapter.fetch_context(task="Fix the checkout bug")

    assert result.ok is True
    assert result.skipped is False
    assert result.source == "openmemory"


def test_openmemory_memory_store_delegates_to_sqlite_and_mirrors_best_effort(
    tmp_path: Path,
) -> None:
    class FakeAdapter:
        def __init__(self) -> None:
            self.pushed: list[tuple[str, str]] = []
            self.fetched: list[tuple[str, str]] = []

        def push_procedural_lesson(self, trace_id: str, memory_id: str):
            self.pushed.append((trace_id, memory_id))
            return object()

        def fetch_context(self, task: str, project_id: str | None = None):
            self.fetched.append((task, project_id or ""))
            return object()

    adapter = FakeAdapter()
    store = OpenMemoryMemoryStore(tmp_path / "atelier", adapter=adapter)  # type: ignore[arg-type]

    block = store.upsert_block(MemoryBlock(agent_id="atelier:code", label="style", value="compact"), actor="tests")
    assert store.get_block("atelier:code", "style") == block

    passage = store.insert_passage(
        ArchivalPassage(
            agent_id="atelier:code",
            text="checkout retry guidance",
            tags=["checkout"],
            source="trace",
            source_ref="trace-1",
            dedup_hash="hash-1",
        )
    )
    assert adapter.pushed == [("trace-1", passage.id)]

    results = store.search_passages("atelier:code", "checkout", top_k=1)
    assert [item.id for item in results] == [passage.id]
    assert adapter.fetched == [("checkout", "atelier:code")]
