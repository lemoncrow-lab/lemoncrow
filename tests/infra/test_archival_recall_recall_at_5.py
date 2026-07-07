from __future__ import annotations

from pathlib import Path

import yaml

from atelier.core.capabilities.archival_recall import ArchivalRecallCapability
from atelier.core.foundation.memory_models import ArchivalPassage
from atelier.infra.embeddings.null_embedder import NullEmbedder
from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore


def _load_questions() -> list[dict[str, str]]:
    path = Path("tests/fixtures/archival_eval_questions.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return [item for item in data if isinstance(item, dict)]


def test_archival_recall_recall_at_5_with_fts_floor(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "atelier")
    questions = _load_questions()

    for idx, item in enumerate(questions, 1):
        query = item["query"]
        expected_id = item["expected_passage_id"]
        store.insert_passage(
            ArchivalPassage(
                id=expected_id,
                agent_id="atelier:code",
                text=f"{query}. Durable memory fact number {idx}.",
                tags=["eval"],
                source="user",
                dedup_hash=expected_id,
            )
        )
        store.insert_passage(
            ArchivalPassage(
                id=f"pas-distractor-{idx:03d}",
                agent_id="atelier:code",
                text=f"Unrelated operational note {idx} about invoices and dashboards.",
                tags=["eval"],
                source="user",
                dedup_hash=f"distractor-{idx}",
            )
        )

    capability = ArchivalRecallCapability(store, NullEmbedder(), redactor=lambda text: text)
    hits = 0
    for item in questions:
        passages, _ = capability.recall(
            agent_id="atelier:code",
            query=item["query"],
            top_k=5,
            tags=["eval"],
        )
        if item["expected_passage_id"] in {passage.id for passage in passages}:
            hits += 1

    recall_at_5 = hits / len(questions)
    assert recall_at_5 >= 0.6
    assert len(store.list_recalls("atelier:code", limit=100)) == len(questions)
