from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.foundation.memory_models import ArchivalPassage, MemoryBlock
from atelier.infra.storage.memory_store import MemoryConcurrencyError
from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore


def test_sqlite_memory_store_round_trips_block_and_history(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "atelier")
    block = MemoryBlock(
        agent_id="atelier:code",
        label="persona",
        value="Prefer small scoped patches.",
        metadata={"kind": "preference"},
        pinned=True,
    )

    created = store.upsert_block(block, actor="agent:atelier:code", reason="seed")

    fetched = store.get_block("atelier:code", "persona")
    assert fetched == created
    assert fetched is not None
    assert fetched.version == 1
    assert fetched.current_history_id is not None
    assert store.list_pinned_blocks("atelier:code") == [fetched]

    history = store.list_block_history(fetched.id)
    assert len(history) == 1
    assert history[0].prev_value == ""
    assert history[0].new_value == "Prefer small scoped patches."


def test_sqlite_memory_store_optimistic_locking_rejects_stale_update(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "atelier")
    first = store.upsert_block(
        MemoryBlock(agent_id="atelier:code", label="persona", value="v1"),
        actor="agent:atelier:code",
    )
    updated = store.upsert_block(
        first.model_copy(update={"value": "v2"}),
        actor="agent:atelier:code",
    )

    assert updated.version == 2
    with pytest.raises(MemoryConcurrencyError):
        store.upsert_block(first.model_copy(update={"value": "stale"}), actor="agent:atelier:code")


def test_sqlite_memory_store_deduplicates_passages(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "atelier")
    passage = ArchivalPassage(
        agent_id="atelier:code",
        text="Use catalog truth before editing PDP output.",
        tags=["catalog", "pdp"],
        source="user",
        dedup_hash="hash-one",
    )

    first = store.insert_passage(passage)
    second = store.insert_passage(passage.model_copy(update={"id": "pas-other"}))

    assert first.dedup_hit is False
    assert second.dedup_hit is True
    assert second.id == first.id


def test_sqlite_memory_store_searches_passages_with_fts_and_tags(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "atelier")
    wanted = store.insert_passage(
        ArchivalPassage(
            agent_id="atelier:code",
            text="Catalog truth should be checked before PDP fixes.",
            tags=["catalog"],
            source="user",
            dedup_hash="wanted",
        )
    )
    store.insert_passage(
        ArchivalPassage(
            agent_id="atelier:code",
            text="Tracker evidence should be persisted.",
            tags=["tracker"],
            source="user",
            dedup_hash="other",
        )
    )

    results = store.search_passages("atelier:code", "catalog PDP", tags=["catalog"])

    assert [item.id for item in results] == [wanted.id]


def test_sqlite_memory_store_rejects_legacy_stub_vector_shape(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "atelier")
    passage = ArchivalPassage(
        agent_id="atelier:code",
        text="Legacy vector should not be accepted on new writes.",
        embedding=[0.0] * 32,
        embedding_provenance="legacy_stub",
        tags=["legacy"],
        source="user",
        dedup_hash="legacy-vector",
    )

    with pytest.raises(ValueError, match="legacy stub"):
        store.insert_passage(passage)


def test_sqlite_memory_store_persists_embedding_provenance(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "atelier")
    passage = store.insert_passage(
        ArchivalPassage(
            agent_id="atelier:code",
            text="Feature-hashed vector provenance is retained.",
            embedding=[0.1] * 64,
            embedding_provenance="local:hashing",
            tags=["embedding"],
            source="user",
            dedup_hash="feature-hash",
        )
    )

    fetched = store.search_passages("atelier:code", "feature hashed")[0]
    assert fetched.id == passage.id
    assert fetched.embedding_provenance == "local:hashing"


def test_sqlite_memory_store_tombstones_blocks(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "atelier")
    block = store.upsert_block(
        MemoryBlock(agent_id="atelier:code", label="preference", value="keep it focused"),
        actor="agent:atelier:code",
    )

    store.tombstone_block(block.id, reason="superseded")

    assert store.get_block("atelier:code", "preference") is None
    assert store.list_blocks("atelier:code") == []
    tombstoned = store.get_block("atelier:code", "preference", include_tombstoned=True)
    assert tombstoned is not None
    assert tombstoned.deprecated_at is not None
    assert tombstoned.deprecation_reason == "superseded"
