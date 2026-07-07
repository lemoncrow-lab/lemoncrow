from __future__ import annotations

import pytest

from atelier.core.capabilities.archival_recall import ArchivalRecallCapability
from atelier.core.capabilities.archival_recall.ranking import RankedPassage
from atelier.core.foundation.memory_models import ArchivalPassage, MemoryRecall
from atelier.infra.embeddings.null_embedder import NullEmbedder


class _MemoryStore:
    def __init__(self, passage: ArchivalPassage) -> None:
        self.passage = passage
        self.recalls: list[MemoryRecall] = []

    def list_passages(self, *args: object, **kwargs: object) -> list[ArchivalPassage]:
        return [self.passage]

    def record_recall(self, recall: MemoryRecall) -> None:
        self.recalls.append(recall)


def test_archival_recall_retries_with_widened_query(monkeypatch: pytest.MonkeyPatch) -> None:
    passage = ArchivalPassage(
        id="pas-checkout",
        agent_id="atelier:code",
        text="checkout retry handling",
        tags=["eval"],
        source="user",
        dedup_hash="pas-checkout",
    )
    ranked_queries: list[str] = []

    def fake_rank(**kwargs: object) -> list[RankedPassage]:
        query = str(kwargs["query"])
        ranked_queries.append(query)
        if len(ranked_queries) == 1:
            return []
        return [RankedPassage(passage=passage, score=1.0, bm25_norm=1.0, cosine=0.0)]

    monkeypatch.setattr(
        "atelier.core.capabilities.archival_recall.capability.rank_archival_passages",
        fake_rank,
    )
    store = _MemoryStore(passage)
    capability = ArchivalRecallCapability(store, NullEmbedder(), redactor=lambda text: text)

    passages, recall = capability.recall(
        agent_id="atelier:code",
        query="checkout AND retry",
        tags=["eval"],
    )

    assert [item.id for item in passages] == ["pas-checkout"]
    assert ranked_queries == ["checkout AND retry", "checkout OR retry"]
    assert recall.query == "checkout OR retry"
    assert store.recalls == [recall]
