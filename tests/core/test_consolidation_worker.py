from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.consolidation import consolidate
from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.store import ContextStore
from atelier.infra.internal_llm.ollama_client import OllamaUnavailable


def _block(block_id: str, title: str) -> ReasonBlock:
    return ReasonBlock(
        id=block_id,
        title=title,
        domain="testing",
        situation="When checkout retries fail with timeout during webhook delivery",
        triggers=["checkout", "retry", "timeout"],
        procedure=["Inspect retry budget", "Verify idempotency key", "Run webhook tests"],
        failure_signals=["timeout", "duplicate delivery"],
    )


def test_consolidate_writes_duplicate_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    store.upsert_block(_block("rb-one", "Checkout retry timeout"), write_markdown=False)
    store.upsert_block(_block("rb-two", "Checkout retry webhook timeout"), write_markdown=False)

    def unavailable(messages: object, json_schema: object | None = None) -> None:
        _ = (messages, json_schema)
        raise OllamaUnavailable("offline")

    monkeypatch.setattr("atelier.core.capabilities.consolidation.worker.chat", unavailable)

    report = consolidate(store)

    candidates = store.list_consolidation_candidates()
    assert report.duplicates == 1
    assert report.written == 1
    assert len(candidates) == 1
    assert candidates[0].kind == "duplicate_cluster"
    assert set(candidates[0].affected_block_ids) == {"rb-one", "rb-two"}


def test_consolidate_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    store.upsert_block(_block("rb-one", "Checkout retry timeout"), write_markdown=False)
    store.upsert_block(_block("rb-two", "Checkout retry webhook timeout"), write_markdown=False)
    monkeypatch.setattr(
        "atelier.core.capabilities.consolidation.worker.chat",
        lambda messages, json_schema=None: (_ for _ in ()).throw(OllamaUnavailable("offline")),
    )

    report = consolidate(store, dry_run=True)

    assert report.duplicates == 1
    assert report.written == 0
    assert store.list_consolidation_candidates() == []
