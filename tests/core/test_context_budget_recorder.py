"""Tests for ContextBudgetRecorder.

Tests:
  - Recording a single turn's metrics
  - Aggregating metrics across a run
  - Prometheus metric emission (if available)
"""

from __future__ import annotations

import pytest

from lemoncrow.core.capabilities.telemetry.context_budget import (
    ContextBudgetRecorder,
    RunSavings,
)
from lemoncrow.core.foundation.store import ContextStore


def test_context_budget_recorder_record(store: ContextStore) -> None:
    """Test recording a single context budget record."""
    recorder = ContextBudgetRecorder(store)

    recorder.record(
        session_id="test-run-1",
        turn_index=0,
        model="claude-3-opus",
        input_tokens=1000,
        cache_read_tokens=100,
        cache_write_tokens=50,
        output_tokens=500,
        naive_input_tokens=2000,
        lever_savings={"semantic_file_memory": 300, "archival_recall": 700},
        tool_calls=2,
    )

    # Verify the record was persisted
    records = store.list_context_budgets("test-run-1")
    assert len(records) == 1
    record = records[0]
    assert record.session_id == "test-run-1"
    assert record.turn_index == 0
    assert record.model == "claude-3-opus"
    assert record.input_tokens == 1000
    assert record.cache_read_tokens == 100
    assert record.cache_write_tokens == 50
    assert record.output_tokens == 500
    assert record.naive_input_tokens == 2000
    assert record.lever_savings == {"semantic_file_memory": 300, "archival_recall": 700}
    assert record.tool_calls == 2


def test_context_budget_recorder_multiple_turns(store: ContextStore) -> None:
    """Test recording multiple turns in a run."""
    recorder = ContextBudgetRecorder(store)

    # Record turn 0
    recorder.record(
        session_id="test-run-2",
        turn_index=0,
        model="claude-3-opus",
        input_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=100,
        output_tokens=500,
        naive_input_tokens=2000,
        lever_savings={"semantic_file_memory": 500},
        tool_calls=1,
    )

    # Record turn 1
    recorder.record(
        session_id="test-run-2",
        turn_index=1,
        model="claude-3-opus",
        input_tokens=800,
        cache_read_tokens=200,
        cache_write_tokens=0,
        output_tokens=400,
        naive_input_tokens=1500,
        lever_savings={"archival_recall": 700},
        tool_calls=2,
    )

    # Verify both records were persisted
    records = store.list_context_budgets("test-run-2")
    assert len(records) == 2
    assert records[0].turn_index == 0
    assert records[1].turn_index == 1


def test_context_budget_recorder_aggregate_run(store: ContextStore) -> None:
    """Test aggregating metrics across a run."""
    recorder = ContextBudgetRecorder(store)

    # Record multiple turns
    recorder.record(
        session_id="test-run-3",
        turn_index=0,
        model="claude-3-opus",
        input_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=500,
        naive_input_tokens=2000,
        lever_savings={"semantic_file_memory": 300, "archival_recall": 200},
        tool_calls=1,
    )

    recorder.record(
        session_id="test-run-3",
        turn_index=1,
        model="claude-3-opus",
        input_tokens=800,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=400,
        naive_input_tokens=1500,
        lever_savings={"semantic_file_memory": 400},
        tool_calls=2,
    )

    # Aggregate
    savings = recorder.aggregate_run("test-run-3")
    assert isinstance(savings, RunSavings)
    assert savings.session_id == "test-run-3"
    assert savings.turn_count == 2
    assert savings.total_tokens_saved == 900  # 300 + 200 + 400
    assert savings.lever_totals == {
        "semantic_file_memory": 700,
        "archival_recall": 200,
    }


def test_context_budget_recorder_compact_method_totals(store: ContextStore) -> None:
    recorder = ContextBudgetRecorder(store)

    recorder.record_compact_tool_output(
        session_id="test-run-compact",
        turn_index=0,
        model="claude-3-opus",
        method="deterministic_truncate",
        tokens_in=1000,
        tokens_out=350,
    )
    recorder.record_compact_tool_output(
        session_id="test-run-compact",
        turn_index=1,
        model="claude-3-opus",
        method="ollama_summary",
        tokens_in=700,
        tokens_out=900,
    )

    savings = recorder.aggregate_run("test-run-compact")

    assert savings.total_tokens_saved == 650
    assert savings.lever_totals == {"compact_tool_output": 650}
    assert savings.compact_method_totals == {"deterministic_truncate": 650}


def test_context_budget_recorder_aggregate_empty_run(store: ContextStore) -> None:
    """Test aggregating metrics for a run with no records."""
    recorder = ContextBudgetRecorder(store)

    # Aggregate a non-existent run
    savings = recorder.aggregate_run("non-existent-run")
    assert savings.session_id == "non-existent-run"
    assert savings.turn_count == 0
    assert savings.total_tokens_saved == 0
    assert savings.lever_totals == {}


def test_context_budget_record_with_zero_savings(store: ContextStore) -> None:
    """Test recording a turn with zero lever savings."""
    recorder = ContextBudgetRecorder(store)

    recorder.record(
        session_id="test-run-4",
        turn_index=0,
        model="claude-3-opus",
        input_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=500,
        naive_input_tokens=1000,
        lever_savings={},
        tool_calls=1,
    )

    records = store.list_context_budgets("test-run-4")
    assert len(records) == 1
    assert records[0].lever_savings == {}


def test_run_savings_to_dict() -> None:
    """Test RunSavings.to_dict() serialization."""
    savings = RunSavings("test-run")
    savings.total_tokens_saved = 1000
    savings.lever_totals = {"semantic_file_memory": 600, "archival_recall": 400}
    savings.turn_count = 2

    result = savings.to_dict()
    assert result["session_id"] == "test-run"
    assert result["total_tokens_saved"] == 1000
    assert result["lever_totals"] == {
        "semantic_file_memory": 600,
        "archival_recall": 400,
    }
    assert result["turn_count"] == 2


def test_context_budget_get_single_record(store: ContextStore) -> None:
    """Test retrieving a single ContextBudget record by ID."""
    recorder = ContextBudgetRecorder(store)

    recorder.record(
        session_id="test-run-5",
        turn_index=0,
        model="claude-3-opus",
        input_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=500,
        naive_input_tokens=2000,
        lever_savings={"test_lever": 500},
        tool_calls=1,
    )

    records = store.list_context_budgets("test-run-5")
    record_id = records[0].id

    # Retrieve by ID
    retrieved = store.get_context_budget(record_id)
    assert retrieved is not None
    assert retrieved.id == record_id
    assert retrieved.session_id == "test-run-5"

    # Non-existent record
    non_existent = store.get_context_budget("non-existent-id")
    assert non_existent is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
