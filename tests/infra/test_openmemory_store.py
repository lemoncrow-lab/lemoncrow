from __future__ import annotations

from datetime import UTC, datetime

from atelier.infra.memory_bridges.openmemory import OpenMemoryMemoryStore

_PASSAGE_ROW = {
    "id": "mem-1",
    "content": "remembered text",
    "metadata": {"atelier_kind": "atelier_passage", "atelier_passage_id": "p-1"},
}


def test_row_to_passage_parses_iso_created_at() -> None:
    row = {**_PASSAGE_ROW, "created_at": "2026-01-02T03:04:05+00:00"}
    passage = OpenMemoryMemoryStore._row_to_passage(row, agent_id="a")
    assert passage is not None
    assert passage.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_row_to_passage_falls_back_on_malformed_created_at() -> None:
    """A malformed created_at must not raise -- it would crash an entire recall."""
    before = datetime.now(UTC)
    row = {**_PASSAGE_ROW, "created_at": "2026-13-99"}
    passage = OpenMemoryMemoryStore._row_to_passage(row, agent_id="a")
    assert passage is not None
    assert passage.created_at >= before
