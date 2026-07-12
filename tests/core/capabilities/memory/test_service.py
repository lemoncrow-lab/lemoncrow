from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.memory import MemoryService
from lemoncrow.core.capabilities.memory import service as service_module
from lemoncrow.core.capabilities.memory_arbitration import ArbitrationDecision
from lemoncrow.core.foundation.memory_models import MemoryBlock
from lemoncrow.infra.embeddings.null_embedder import NullEmbedder
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore


def _service(tmp_path: Path) -> MemoryService:
    return MemoryService(
        store=SqliteMemoryStore(tmp_path / "lemoncrow"),
        embedder=NullEmbedder(),
        redactor=lambda value: value,
    )


def test_store_list_and_get_fact(tmp_path: Path) -> None:
    service = _service(tmp_path)

    stored = service.store_fact(
        agent_id="lc:code",
        subject="workflow preference",
        fact="Prefer canonical memory operations.",
        citations='User input: "canonical"',
        reason="Keeps host surfaces consistent.",
        scope="user",
    )

    assert stored.fact == "Prefer canonical memory operations."
    assert stored.scope == "user"
    assert service.list_facts(agent_id="lc:code") == [stored]
    assert service.get_fact(agent_id="lc:code", fact_id=stored.id) == stored


def test_vote_fact_preserves_response_shape(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store_fact(
        agent_id="lc:code",
        subject="workflow preference",
        fact="Prefer canonical memory operations.",
        scope="repository",
    )

    voted = service.vote_fact(
        agent_id="lc:code",
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
        agent_id="lc:code",
        subject="testing",
        fact="Repository facts are not workspace visibility metadata.",
        scope="repository",
    )

    shared = service.share_fact(
        agent_id="lc:code",
        fact_id=stored.id,
        workspace_id="workspace-1",
        shared_by_user_id="admin@example.com",
    )

    block = next(block for block in store.list_blocks("lc:code") if block.id == shared.id)
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
            agent_id="lc:code",
            label="victim",
            value="unrelated protected fact",
        ),
        actor="agent:lc:code",
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
        agent_id="lc:code",
        subject="new",
        fact="totally different topic with no token overlap whatsoever",
        scope="repository",
    )

    survivors = {block.id for block in store.list_blocks("lc:code")}
    assert victim.id in survivors
