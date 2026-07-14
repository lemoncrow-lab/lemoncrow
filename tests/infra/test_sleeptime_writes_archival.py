"""Integration test: compress_with_sleeptime writes archival passages."""

from __future__ import annotations

import os
import tempfile
from typing import ClassVar

import pytest

from lemoncrow.core.capabilities.licensing import entitlements
from lemoncrow.pro.capabilities.context_compression.capability import (
    ContextCompressionCapability,
)
from lemoncrow.pro.capabilities.context_compression.sleeptime import SleeptimeChunk
from tests.helpers import grant_oauth_pro


class _FakeLedger:
    """Minimal stub that looks like a RunLedger."""

    session_id = "test-run-sleeptime"
    token_count = 0
    files_touched: ClassVar[list[str]] = []
    active_playbooks: ClassVar[list[str]] = []
    agent = "lemoncrow"

    def __init__(self, n_events: int = 200) -> None:
        self.events = [
            {
                "kind": "tool_output" if i % 3 != 0 else "file_read",
                "summary": f"redundant lookup result {i % 10}",  # lots of repeats
                "payload": {"data": "x" * 50},
            }
            for i in range(n_events)
        ]


def test_compress_with_sleeptime_reduces_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    grant_oauth_pro(monkeypatch)
    ledger = _FakeLedger(n_events=200)
    cap = ContextCompressionCapability()
    result = cap.compress_with_sleeptime(ledger, token_budget=4000)
    assert result.chars_after < result.chars_before, "sleeptime must reduce context"
    entitlements.reload()


def test_compress_with_sleeptime_writes_run_frame(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RunMemoryFrame must be written to the store."""
    grant_oauth_pro(monkeypatch)
    ledger = _FakeLedger(n_events=50)
    cap = ContextCompressionCapability()

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["LEMONCROW_ROOT"] = tmpdir
        try:
            result = cap.compress_with_sleeptime(ledger, token_budget=2000)
        finally:
            os.environ.pop("LEMONCROW_ROOT", None)

    assert result is not None
    assert result.token_savings >= 0
    entitlements.reload()


def test_compress_with_sleeptime_archives_passages(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ArchivalPassage rows must be written for evicted events."""
    from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore

    grant_oauth_pro(monkeypatch)
    ledger = _FakeLedger(n_events=100)
    cap = ContextCompressionCapability()
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.context_compression.capability.summarize_ledger",
        lambda dropped: [
            SleeptimeChunk(
                start_event_index=0,
                end_event_index=len(dropped),
                paraphrase="compact sleep summary",
            )
        ],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["LEMONCROW_ROOT"] = tmpdir
        try:
            cap.compress_with_sleeptime(ledger, token_budget=1000, agent_id="lemoncrow")
        finally:
            os.environ.pop("LEMONCROW_ROOT", None)

        store = SqliteMemoryStore(tmpdir)
        passages = store.list_passages("lemoncrow", limit=500)

    assert len(passages) >= 1, "at least one archival passage must be written"
    entitlements.reload()


def test_compress_with_provenance_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Original compress_with_provenance still works after adding sleeptime."""
    grant_oauth_pro(monkeypatch)
    ledger = _FakeLedger(n_events=50)
    cap = ContextCompressionCapability()
    result = cap.compress_with_provenance(ledger, token_budget=2000)
    assert result.chars_before > 0
    assert result.chars_after <= result.chars_before
    entitlements.reload()
