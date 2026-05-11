"""Tests for atelier.runtime.cost_tracker (Phase 7)."""

from __future__ import annotations

import json
from pathlib import Path

from atelier.infra.runtime.cost_tracker import (
    CostTracker,
    estimate_cost,
    load_cost_history,
    operation_key,
)


def test_estimate_cost_unknown_model_returns_zero() -> None:
    # Unknown models have no pricing data → cost is $0.00
    cost = estimate_cost("totally-unknown-model", 1000, 500)
    assert cost == 0.0


def test_estimate_cost_known_model_pricing() -> None:
    cost = estimate_cost("claude-sonnet-4.6", 10_000, 2_000, cache_read_tokens=5_000)
    expected = (10_000 * 3.00 + 2_000 * 15.00 + 5_000 * 0.30) / 1_000_000
    assert cost == round(expected, 6)


def test_operation_key_is_stable_across_numerals_and_whitespace() -> None:
    a = operation_key("pdp", "audit  product 12 schema")
    b = operation_key("pdp", "audit product 9999 schema")
    c = operation_key("pdp", "AUDIT product 1 schema\n")
    assert a == b == c
    # Different domain → different key
    assert operation_key("billing", "audit product 12 schema") != a


def test_record_call_persists_history(tmp_path: Path) -> None:
    tracker = CostTracker(tmp_path)
    rec = tracker.record_call(
        operation="plan",
        model="claude-sonnet-4.6",
        input_tokens=4000,
        output_tokens=1500,
        domain="pdp",
        task="audit product detail page",
        lessons_used=["block-001", "block-002"],
    )
    assert rec.cost_usd > 0
    assert rec.op_key == operation_key("pdp", "audit product detail page")
    history = load_cost_history(tmp_path)
    assert rec.op_key in history["operations"]
    entry = history["operations"][rec.op_key]
    assert len(entry["calls"]) == 1
    assert entry["calls"][0]["lessons_used"] == ["block-001", "block-002"]


def test_savings_for_computes_delta_vs_last_and_baseline(tmp_path: Path) -> None:
    tracker = CostTracker(tmp_path)
    # Baseline call (no lessons → expensive)
    tracker.record_call(
        operation="plan",
        model="claude-sonnet-4.6",
        input_tokens=4000,
        output_tokens=1500,
        domain="pdp",
        task="audit pdp",
    )
    # Round 2 (some lessons)
    tracker.record_call(
        operation="plan",
        model="claude-sonnet-4.6",
        input_tokens=3300,
        output_tokens=1300,
        domain="pdp",
        task="audit pdp",
        lessons_used=["b1", "b2"],
    )
    # Round 3 (more lessons → cheapest)
    tracker.record_call(
        operation="plan",
        model="claude-sonnet-4.6",
        input_tokens=2600,
        output_tokens=1100,
        domain="pdp",
        task="audit pdp",
        lessons_used=["b1", "b2", "b3"],
    )
    op_key = operation_key("pdp", "audit pdp")
    s = tracker.savings_for(op_key)
    assert s["calls_count"] == 3
    assert s["baseline_cost_usd"] > s["current_cost_usd"]
    assert s["delta_vs_last_usd"] > 0  # current is cheaper than previous round
    assert s["delta_vs_base_usd"] > 0  # current is cheaper than baseline
    assert s["pct_vs_base"] > 0


def test_total_savings_aggregates_across_operations(tmp_path: Path) -> None:
    tracker = CostTracker(tmp_path)
    for i, in_tok in enumerate([4000, 3000]):
        tracker.record_call(
            operation="plan",
            model="claude-sonnet-4.6",
            input_tokens=in_tok,
            output_tokens=1500,
            domain="pdp",
            task="audit pdp",
            lessons_used=[] if i == 0 else ["b1"],
        )
    for i, in_tok in enumerate([4000, 2000]):
        tracker.record_call(
            operation="plan",
            model="claude-sonnet-4.6",
            input_tokens=in_tok,
            output_tokens=1500,
            domain="billing",
            task="issue credit",
            lessons_used=[] if i == 0 else ["b2"],
        )
    summary = tracker.total_savings()
    assert summary["operations_tracked"] == 2
    assert summary["total_calls"] == 4
    assert summary["saved_usd"] > 0
    assert summary["saved_pct"] > 0
    assert len(summary["per_operation"]) == 2


def test_run_ledger_record_call_attaches_cost_to_snapshot(tmp_path: Path) -> None:
    from atelier.infra.runtime.run_ledger import RunLedger

    led = RunLedger(
        agent="test",
        root=tmp_path,
        task="audit pdp",
        domain="pdp",
    )
    led.record_call(
        operation="plan",
        model="claude-sonnet-4.6",
        input_tokens=4000,
        output_tokens=1500,
        lessons_used=["block-x"],
    )
    led.close("complete")
    path = led.persist(tmp_path)
    snap = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "cost" in snap
    assert snap["cost"]["calls"][0]["model"] == "claude-sonnet-4.6"
    assert snap["cost"]["calls"][0]["lessons_used"] == ["block-x"]
    assert snap["cost"]["total_cost_usd"] > 0
    assert snap["token_count"] == 5500
    # cost_history.json populated
    assert (tmp_path / "cost_history.json").exists()


def test_failure_cluster_includes_suggested_prompt() -> None:
    from atelier.core.improvement.failure_analyzer import analyze_failures

    snapshots = [
        {
            "session_id": f"run-{i}",
            "status": "failed",
            "environment_id": "shopify",
            "events": [
                {
                    "kind": "command_result",
                    "summary": "publish",
                    "payload": {"ok": False, "error_signature": "HTTP 429 rate limit exceeded"},
                }
            ],
        }
        for i in range(3)
    ]
    clusters = analyze_failures(snapshots)
    assert clusters
    c = clusters[0]
    assert c.suggested_prompt
    assert "backoff" in c.suggested_prompt.lower() or "rate" in c.suggested_prompt.lower()
    assert "shopify" in c.suggested_prompt
