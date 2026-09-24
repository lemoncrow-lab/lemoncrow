from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.foundation.memory_models import MemoryBlock
from lemoncrow.infra.embeddings.null_embedder import NullEmbedder
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore
from lemoncrow.pro.capabilities.memory import MemoryService
from lemoncrow.pro.capabilities.memory import service as service_module
from lemoncrow.pro.capabilities.memory_arbitration import ArbitrationDecision


def _service(tmp_path: Path) -> MemoryService:
    return MemoryService(
        store=SqliteMemoryStore(tmp_path / "lemoncrow"),
        embedder=NullEmbedder(),
        redactor=lambda value: value,
    )


def test_store_list_and_get_fact(tmp_path: Path) -> None:
    service = _service(tmp_path)

    stored = service.store_fact(
        agent_id="lemoncrow:code",
        subject="workflow preference",
        fact="Prefer canonical memory operations.",
        citations='User input: "canonical"',
        reason="Keeps host surfaces consistent.",
        scope="user",
    )

    assert stored.fact == "Prefer canonical memory operations."
    assert stored.scope == "user"
    assert service.list_facts(agent_id="lemoncrow:code") == [stored]
    assert service.get_fact(agent_id="lemoncrow:code", fact_id=stored.id) == stored


def test_vote_fact_preserves_response_shape(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store_fact(
        agent_id="lemoncrow:code",
        subject="workflow preference",
        fact="Prefer canonical memory operations.",
        scope="repository",
    )

    voted = service.vote_fact(
        agent_id="lemoncrow:code",
        fact="Prefer canonical memory operations.",
        direction="upvote",
        reason="Useful across hosts.",
        scope="repository",
    )

    assert voted.fact == "Prefer canonical memory operations."
    assert voted.scope == "repository"
    assert voted.direction == "upvote"


def test_share_fact_uses_visibility_scope_without_overwriting_fact_scope(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "lemoncrow")
    service = MemoryService(store=store, embedder=NullEmbedder(), redactor=lambda value: value)
    stored = service.store_fact(
        agent_id="lemoncrow:code",
        subject="testing",
        fact="Repository facts are not workspace visibility metadata.",
        scope="repository",
    )

    shared = service.share_fact(
        agent_id="lemoncrow:code",
        fact_id=stored.id,
        workspace_id="workspace-1",
        shared_by_user_id="admin@example.com",
    )

    block = next(block for block in store.list_blocks("lemoncrow:code") if block.id == shared.id)
    assert shared.scope == "repository"
    assert block.metadata["scope"] == "repository"
    assert block.metadata["fact_scope"] == "repository"
    assert block.metadata["visibility_scope"] == "shared"
    assert block.metadata["workspace_id"] == "workspace-1"


def test_upsert_ignores_target_block_id_outside_candidate_set(tmp_path: Path, monkeypatch) -> None:
    store = SqliteMemoryStore(tmp_path / "lemoncrow")
    service = MemoryService(store=store, embedder=NullEmbedder(), redactor=lambda value: value)
    victim = store.upsert_block(
        MemoryBlock(
            agent_id="lemoncrow:code",
            label="victim",
            value="unrelated protected fact",
        ),
        actor="agent:lemoncrow:code",
    )

    # The arbiter showed no candidates, but returns a DELETE pointing at the
    # victim block (hallucinated/malicious target_block_id). The guard must
    # refuse to act on a target outside the candidate set.
    monkeypatch.setattr(service_module, "_similar_blocks", lambda block, store, *, k: [])
    monkeypatch.setattr(
        service_module,
        "arbitrate",
        lambda block, store, embedder: ArbitrationDecision(op="DELETE", target_block_id=victim.id, reason="forced"),
    )

    service.store_fact(
        agent_id="lemoncrow:code",
        subject="new",
        fact="totally different topic with no token overlap whatsoever",
        scope="repository",
    )

    survivors = {block.id for block in store.list_blocks("lemoncrow:code")}
    assert victim.id in survivors


def test_upsert_editable_block_preserves_version_history_and_metadata(tmp_path: Path, monkeypatch) -> None:
    from lemoncrow.infra.storage.memory_store import MemoryConcurrencyError

    store = SqliteMemoryStore(tmp_path / "lemoncrow")
    service = MemoryService(store=store, embedder=NullEmbedder(), redactor=lambda value: value)
    monkeypatch.setattr(
        service_module,
        "arbitrate",
        lambda block, store, embedder: ArbitrationDecision(op="ADD", reason="new editable block"),
    )

    created = service.upsert_editable_block(
        agent_id="shared",
        label="edits/sym-1",
        value="trace-1",
        metadata={"symbol_id": "sym-1"},
        pinned=True,
        actor="test:create",
    )
    assert created["version"] == 1
    block = store.get_block("shared", "edits/sym-1")
    assert block is not None
    assert block.value == "trace-1"
    assert block.metadata == {"symbol_id": "sym-1"}
    assert block.pinned is True

    updated = service.upsert_editable_block(
        agent_id="shared",
        label="edits/sym-1",
        value="trace-2",
        metadata={"symbol_id": "sym-1", "trace_id": "trace-2"},
        actor="test:update",
    )
    assert updated["version"] == 2
    block = store.get_block("shared", "edits/sym-1")
    assert block is not None
    assert block.value == "trace-2"
    assert block.metadata["trace_id"] == "trace-2"
    history = store.list_block_history(block.id)
    assert history[0].actor == "test:update"
    assert history[0].prev_value == "trace-1"
    assert history[0].new_value == "trace-2"

    with pytest.raises(MemoryConcurrencyError):
        service.upsert_editable_block(
            agent_id="shared",
            label="edits/sym-1",
            value="stale",
            expected_version=1,
        )


def test_upsert_editable_block_update_keeps_target_metadata(tmp_path: Path, monkeypatch) -> None:
    store = SqliteMemoryStore(tmp_path / "lemoncrow")
    service = MemoryService(store=store, embedder=NullEmbedder(), redactor=lambda value: value)
    target = store.upsert_block(
        MemoryBlock(
            agent_id="shared",
            label="existing",
            value="old",
            metadata={"keep": "target"},
        ),
        actor="seed",
    )
    monkeypatch.setattr(
        service_module,
        "arbitrate",
        lambda block, store, embedder: ArbitrationDecision(
            op="UPDATE",
            target_block_id=target.id,
            merged_value="merged",
            reason="merge exact legacy target",
        ),
    )

    result = service.upsert_editable_block(
        agent_id="shared",
        label="new-label",
        value="new",
        metadata={"replace": "must-not-merge"},
    )

    stored = store.get_block("shared", "existing")
    assert stored is not None
    assert result["id"] == target.id
    assert stored.value == "merged"
    assert stored.metadata == {"keep": "target"}
    assert store.get_block("shared", "new-label") is None


def test_upsert_editable_block_delete_tombstones_target_then_adds_new_block(tmp_path: Path, monkeypatch) -> None:
    store = SqliteMemoryStore(tmp_path / "lemoncrow")
    service = MemoryService(store=store, embedder=NullEmbedder(), redactor=lambda value: value)
    target = store.upsert_block(
        MemoryBlock(agent_id="shared", label="old", value="obsolete"),
        actor="seed",
    )
    monkeypatch.setattr(
        service_module,
        "arbitrate",
        lambda block, store, embedder: ArbitrationDecision(
            op="DELETE",
            target_block_id=target.id,
            reason="replace obsolete editable block",
        ),
    )

    result = service.upsert_editable_block(
        agent_id="shared",
        label="replacement",
        value="fresh",
        metadata={"kind": "edit"},
    )

    old = store.get_block("shared", "old", include_tombstoned=True)
    replacement = store.get_block("shared", "replacement")
    assert old is not None and old.deprecated_at is not None
    assert replacement is not None
    assert old.deprecated_by_block_id == replacement.id
    assert result["id"] == replacement.id


def test_upsert_editable_block_uses_field_redactor_for_value_and_description(tmp_path: Path, monkeypatch) -> None:
    store = SqliteMemoryStore(tmp_path / "lemoncrow")
    service = MemoryService(store=store, embedder=NullEmbedder(), redactor=lambda value: f"base:{value}")
    monkeypatch.setattr(
        service_module,
        "arbitrate",
        lambda block, store, embedder: ArbitrationDecision(op="ADD", reason="new"),
    )
    seen: list[tuple[str, str]] = []

    def field_redactor(value: str, field: str) -> str:
        seen.append((field, value))
        return f"{field}:{value}"

    service.upsert_editable_block(
        agent_id="shared",
        label="edit/redacted",
        value="trace",
        description="desc",
        field_redactor=field_redactor,
    )
    block = store.get_block("shared", "edit/redacted")
    assert block is not None
    assert block.value == "value:trace"
    assert block.description == "description:desc"
    assert seen == [("value", "trace"), ("description", "desc")]
