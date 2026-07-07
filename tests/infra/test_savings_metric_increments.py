"""Integration tests for context budget telemetry.

Tests:
  - Recording metrics through the full dispatch loop
  - Database persistence
  - Prometheus metric increments
"""

from __future__ import annotations

import pytest

from atelier.core.capabilities.telemetry.context_budget import ContextBudgetRecorder
from atelier.core.foundation.store import ContextStore


def test_context_budget_dispatch_loop(store: ContextStore) -> None:
    """Test recording metrics through a simulated dispatch loop."""
    recorder = ContextBudgetRecorder(store)

    session_id = "dispatch-test-run"

    # Simulate a dispatch loop with 3 tool calls
    for turn_idx in range(3):
        lever_savings = {}

        # Simulate different tools with different savings
        if turn_idx == 0:
            lever_savings = {
                "semantic_file_memory": 200,
                "context_compression": 100,
            }
        elif turn_idx == 1:
            lever_savings = {"archival_recall": 300}
        else:
            lever_savings = {"context_compression": 150}

        recorder.record(
            session_id=session_id,
            turn_index=turn_idx,
            model="claude-3-opus",
            input_tokens=1000 - (turn_idx * 100),
            cache_read_tokens=turn_idx * 50,
            cache_write_tokens=50,
            output_tokens=500 - (turn_idx * 50),
            naive_input_tokens=2000 - (turn_idx * 100),
            lever_savings=lever_savings,
            tool_calls=1,
        )

    # Verify all records were persisted
    records = store.list_context_budgets(session_id)
    assert len(records) == 3

    # Verify ordering by turn_index
    for idx, record in enumerate(records):
        assert record.turn_index == idx

    # Verify aggregation
    savings = recorder.aggregate_run(session_id)
    assert savings.turn_count == 3
    assert savings.total_tokens_saved == 750  # 200 + 100 + 300 + 150
    assert savings.lever_totals == {
        "semantic_file_memory": 200,
        "context_compression": 250,
        "archival_recall": 300,
    }


def test_context_budget_database_schema_migration(store: ContextStore) -> None:
    """Test that the database schema includes the context_budget table."""
    # Verify the table exists by attempting to query it
    with store._connect() as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='context_budget';")
        result = cursor.fetchone()
        assert result is not None, "context_budget table should exist after init()"


def test_context_budget_unique_constraint(store: ContextStore) -> None:
    """Test that the unique constraint on (session_id, turn_index) is enforced."""
    recorder = ContextBudgetRecorder(store)

    session_id = "constraint-test"

    # Record a turn
    recorder.record(
        session_id=session_id,
        turn_index=0,
        model="claude-3-opus",
        input_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=500,
        naive_input_tokens=2000,
        lever_savings={"test": 100},
        tool_calls=1,
    )

    # Re-record the same turn (should replace due to REPLACE INSERT)
    recorder.record(
        session_id=session_id,
        turn_index=0,
        model="claude-3-opus",
        input_tokens=1100,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=550,
        naive_input_tokens=2100,
        lever_savings={"test": 200},
        tool_calls=2,
    )

    # Verify only one record exists
    records = store.list_context_budgets(session_id)
    assert len(records) == 1
    # Verify it has the latest values
    assert records[0].input_tokens == 1100
    assert records[0].lever_savings == {"test": 200}


def test_context_budget_large_run(store: ContextStore) -> None:
    """Test recording metrics for a large run with many turns."""
    recorder = ContextBudgetRecorder(store)

    session_id = "large-run"
    turn_count = 50

    # Record many turns
    for turn_idx in range(turn_count):
        recorder.record(
            session_id=session_id,
            turn_index=turn_idx,
            model="claude-3-opus",
            input_tokens=1000,
            cache_read_tokens=100 + turn_idx,
            cache_write_tokens=50,
            output_tokens=500,
            naive_input_tokens=2000,
            lever_savings={"semantic_file_memory": 100 + turn_idx},
            tool_calls=1,
        )

    # Verify all records were persisted
    records = store.list_context_budgets(session_id)
    assert len(records) == turn_count

    # Verify aggregation
    savings = recorder.aggregate_run(session_id)
    assert savings.turn_count == turn_count

    # Calculate expected total
    expected_total = sum(100 + idx for idx in range(turn_count))
    assert savings.total_tokens_saved == expected_total


def test_context_budget_lever_savings_json_serialization(store: ContextStore) -> None:
    """Test that lever_savings dict is properly serialized/deserialized."""
    recorder = ContextBudgetRecorder(store)

    complex_savings = {
        "semantic_file_memory": 300,
        "archival_recall": 200,
        "context_compression": 150,
        "tool_supervision": 50,
    }

    recorder.record(
        session_id="json-test",
        turn_index=0,
        model="claude-3-opus",
        input_tokens=1000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=500,
        naive_input_tokens=2000,
        lever_savings=complex_savings,
        tool_calls=1,
    )

    # Retrieve and verify
    records = store.list_context_budgets("json-test")
    assert records[0].lever_savings == complex_savings


def test_context_budget_index_performance(store: ContextStore) -> None:
    """Test that the database index on (session_id) is present for query performance."""
    # Verify the index exists
    with store._connect() as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_context_budget_run';")
        result = cursor.fetchone()
        assert result is not None, "Index on session_id should exist"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
