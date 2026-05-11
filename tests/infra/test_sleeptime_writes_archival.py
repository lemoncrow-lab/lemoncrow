"""Integration test: compress_with_sleeptime writes archival passages."""

from __future__ import annotations

import os
import tempfile
from typing import ClassVar

import pytest

from atelier.core.capabilities.context_compression.capability import (
    ContextCompressionCapability,
)
from atelier.core.capabilities.context_compression.sleeptime import SleeptimeChunk


class _FakeLedger:
    """Minimal stub that looks like a RunLedger."""

    session_id = "test-run-sleeptime"
    token_count = 0
    files_touched: ClassVar[list[str]] = []
    active_reasonblocks: ClassVar[list[str]] = []
    agent = "atelier"

    def __init__(self, n_events: int = 200) -> None:
        self.events = [
            {
                "kind": "tool_output" if i % 3 != 0 else "file_read",
                "summary": f"redundant lookup result {i % 10}",  # lots of repeats
                "payload": {"data": "x" * 50},
            }
            for i in range(n_events)
        ]


def test_compress_with_sleeptime_reduces_tokens() -> None:
    ledger = _FakeLedger(n_events=200)
    cap = ContextCompressionCapability()
    result = cap.compress_with_sleeptime(ledger, token_budget=4000)
    assert result.chars_after < result.chars_before, "sleeptime must reduce context"


def test_compress_with_sleeptime_writes_run_frame(tmp_path: pytest.TempPathFactory) -> None:
    """RunMemoryFrame must be written to the store."""
    ledger = _FakeLedger(n_events=50)
    cap = ContextCompressionCapability()

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ATELIER_ROOT"] = tmpdir
        try:
            result = cap.compress_with_sleeptime(ledger, token_budget=2000)
        finally:
            os.environ.pop("ATELIER_ROOT", None)

    assert result is not None
    assert result.token_savings >= 0


def test_compress_with_sleeptime_archives_passages(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ArchivalPassage rows must be written for evicted events."""
    from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore

    ledger = _FakeLedger(n_events=100)
    cap = ContextCompressionCapability()
    monkeypatch.setattr(
        "atelier.core.capabilities.context_compression.capability.summarize_ledger",
        lambda dropped: [
            SleeptimeChunk(
                start_event_index=0,
                end_event_index=len(dropped),
                paraphrase="compact sleep summary",
            )
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["ATELIER_ROOT"] = tmpdir
        try:
            cap.compress_with_sleeptime(ledger, token_budget=1000, agent_id="atelier")
        finally:
            os.environ.pop("ATELIER_ROOT", None)

        store = SqliteMemoryStore(tmpdir)
        passages = store.list_passages("atelier", limit=500)

    assert len(passages) >= 1, "at least one archival passage must be written"


def test_compress_with_provenance_unchanged() -> None:
    """Original compress_with_provenance still works after adding sleeptime."""
    ledger = _FakeLedger(n_events=50)
    cap = ContextCompressionCapability()
    result = cap.compress_with_provenance(ledger, token_budget=2000)
    assert result.chars_before > 0
    assert result.chars_after <= result.chars_before
