from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.memory_models import ArchivalPassage, MemoryBlock, RunMemoryFrame
from lemoncrow.infra.memory_bridges.letta_adapter import LettaMemoryStore
from lemoncrow.infra.memory_bridges.openmemory import OpenMemoryMemoryStore
from lemoncrow.infra.storage.memory_store import MemoryStore
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore


class _FakeLettaClient:
    def __init__(self) -> None:
        self.blocks: dict[str, dict[str, object]] = {}
        self.passages: dict[str, dict[str, object]] = {}

    def upsert_block(self, payload: dict[str, object]) -> dict[str, object]:
        self.blocks[str(payload["label"])] = payload
        return payload

    def get_block(self, *, agent_id: str, label: str) -> dict[str, object] | None:
        _ = agent_id
        return self.blocks.get(label)

    def list_blocks(self, *, agent_id: str) -> list[dict[str, object]]:
        _ = agent_id
        return list(self.blocks.values())

    def update_block(self, *, block_id: str, metadata: dict[str, object]) -> None:
        for payload in self.blocks.values():
            metadata_obj = payload.get("metadata")
            existing = dict(metadata_obj) if isinstance(metadata_obj, dict) else {}
            if existing.get("lemoncrow_block_id") == block_id:
                existing.update(metadata)
                payload["metadata"] = existing
                return

    def archival_search(
        self,
        *,
        agent_id: str,
        query: str,
        top_k: int,
        tags: list[str],
        since: str | None,
    ) -> list[dict[str, Any]]:
        _ = (agent_id, query, tags, since)
        return list(self.passages.values())[:top_k]

    def archival_insert(self, **payload: object) -> dict[str, object]:
        metadata_obj = payload.get("metadata")
        metadata = dict(metadata_obj) if isinstance(metadata_obj, dict) else {}
        passage_id = str(metadata.get("lemoncrow_passage_id", "pas-letta"))
        row = {
            "id": passage_id,
            "agent_id": payload.get("agent_id", "lemon:code"),
            "text": payload.get("text", payload.get("value", "")),
            "tags": payload.get("tags", []),
            "metadata": metadata,
        }
        self.passages[passage_id] = row
        return row

    def archival_list(self, *, agent_id: str, tags: list[str], limit: int) -> list[dict[str, object]]:
        _ = (agent_id, tags)
        return list(self.passages.values())[:limit]

    def archival_update(self, passage_id: str, metadata: dict[str, object]) -> None:
        if passage_id in self.passages:
            metadata_obj = self.passages[passage_id].get("metadata")
            existing = dict(metadata_obj) if isinstance(metadata_obj, dict) else {}
            existing.update(metadata)
            self.passages[passage_id]["metadata"] = existing


class _FakeOpenMemoryClient:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "add_memories":
            row = {
                "id": f"mem-{len(self.rows) + 1}",
                "memory": str(arguments["messages"][0]["content"]),
                "metadata": dict(arguments.get("metadata") or {}),
            }
            self.rows.append(row)
            return {"results": [row]}
        if name == "search_memory":
            query = str(arguments["query"]).lower()
            return [
                row
                for row in self.rows
                if query in row["memory"].lower() or query in str(row.get("metadata", {})).lower()
            ]
        if name == "list_memories":
            return list(self.rows)
        raise AssertionError(f"unexpected tool call: {name}")


@pytest.fixture(params=["sqlite", "letta", "openmemory"])
def memory_store(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryStore:
    if request.param == "sqlite":
        return SqliteMemoryStore(tmp_path / "lemoncrow")
    if request.param == "letta":
        return LettaMemoryStore(tmp_path / "lemoncrow", client=_FakeLettaClient())
    return OpenMemoryMemoryStore(tmp_path / "lemoncrow", client=_FakeOpenMemoryClient())


def test_memory_store_core_round_trip(memory_store: MemoryStore) -> None:
    block = MemoryBlock(
        agent_id="lemon:code",
        label="working-style",
        value="Keep implementation scoped.",
        pinned=True,
    )

    stored = memory_store.upsert_block(block, actor="agent:lemon:code", reason="test")
    fetched = memory_store.get_block("lemon:code", "working-style")

    assert fetched is not None
    assert fetched.label == stored.label
    assert fetched.value == stored.value
    assert memory_store.list_pinned_blocks("lemon:code")[0].label == "working-style"
    if isinstance(memory_store, SqliteMemoryStore):
        assert memory_store.list_block_history(stored.id)


def test_memory_store_passage_and_run_frame_round_trip(memory_store: MemoryStore) -> None:
    passage = ArchivalPassage(
        agent_id="lemon:code",
        text="Memory store persists archival passages.",
        tags=["memory"],
        source="user",
        dedup_hash="passage-hash",
    )
    inserted = memory_store.insert_passage(passage)
    duplicate = memory_store.insert_passage(passage.model_copy(update={"id": "pas-second"}))

    assert inserted.dedup_hit is False
    if isinstance(memory_store, SqliteMemoryStore):
        assert duplicate.dedup_hit is True

    frame = RunMemoryFrame(
        session_id="run-1",
        pinned_blocks=["working-style"],
        recalled_passages=[inserted.id],
        summarized_events=[],
        tokens_pre_summary=100,
        tokens_post_summary=40,
        compaction_strategy="none",
    )
    memory_store.write_run_frame(frame)

    assert memory_store.get_run_frame("run-1") == frame


def test_letta_memory_store_does_not_mirror_primary_memory_to_sqlite(tmp_path: Path) -> None:
    store = LettaMemoryStore(tmp_path / "lemoncrow", client=_FakeLettaClient())
    store.upsert_block(
        MemoryBlock(agent_id="lemon:code", label="primary", value="stored in letta"),
        actor="agent:lemon:code",
    )
    store.insert_passage(
        ArchivalPassage(
            agent_id="lemon:code",
            text="Letta owns this archival passage.",
            tags=["letta"],
            source="user",
            dedup_hash="letta-primary",
        )
    )

    sqlite = SqliteMemoryStore(tmp_path / "lemoncrow")
    assert sqlite.get_block("lemon:code", "primary") is None
    assert sqlite.list_passages("lemon:code") == []


def test_openmemory_memory_store_does_not_mirror_primary_memory_to_sqlite(tmp_path: Path) -> None:
    store = OpenMemoryMemoryStore(tmp_path / "lemoncrow", client=_FakeOpenMemoryClient())
    store.upsert_block(
        MemoryBlock(agent_id="lemon:code", label="primary", value="stored in openmemory"),
        actor="agent:lemon:code",
    )
    store.insert_passage(
        ArchivalPassage(
            agent_id="lemon:code",
            text="OpenMemory owns this archival passage.",
            tags=["openmemory"],
            source="user",
            dedup_hash="openmemory-primary",
        )
    )

    sqlite = SqliteMemoryStore(tmp_path / "lemoncrow")
    assert sqlite.get_block("lemon:code", "primary") is None
    assert sqlite.list_passages("lemon:code") == []


def test_letta_memory_store_tombstones_blocks_with_metadata(tmp_path: Path) -> None:
    store = LettaMemoryStore(tmp_path / "lemoncrow", client=_FakeLettaClient())
    block = MemoryBlock(agent_id="lemon:code", label="primary", value="stored in letta")
    store.upsert_block(block, actor="agent:lemon:code")

    store.tombstone_block(block.id, reason="superseded")

    assert store.get_block("lemon:code", "primary") is None
    tombstoned = store.get_block("lemon:code", "primary", include_tombstoned=True)
    assert tombstoned is not None
    assert tombstoned.deprecated_at is not None
    assert tombstoned.deprecation_reason == "superseded"
