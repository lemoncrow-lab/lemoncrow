from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI API tests require the api extra")

from fastapi.testclient import TestClient

from atelier.core.foundation.models import Trace
from atelier.core.foundation.store import ReasoningStore
from atelier.core.service.api import create_app


def _write_cost_history(path: Path) -> None:
    now = datetime.now(UTC)
    payload = {
        "operations": {
            "op-search": {
                "domain": "atelier.platform",
                "task_sample": "search",
                "first_seen": now.isoformat(),
                "calls": [
                    {
                        "operation": "search_read",
                        "model": "test-model",
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "cache_read_tokens": 60,
                        "cost_usd": 0.01,
                        "lessons_used": [],
                        "op_key": "op-search",
                        "at": now.isoformat(),
                    }
                ],
            },
            "op-batch": {
                "domain": "atelier.platform",
                "task_sample": "edit",
                "first_seen": now.isoformat(),
                "calls": [
                    {
                        "operation": "batch_edit",
                        "model": "test-model",
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "cache_read_tokens": 50,
                        "cost_usd": 0.009,
                        "lessons_used": [],
                        "op_key": "op-batch",
                        "at": now.isoformat(),
                    }
                ],
            },
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _record_traces(root: Path) -> None:
    store = ReasoningStore(root)
    store.init()
    created_at = datetime.now(UTC)
    traces = [
        Trace(
            id="peer-low",
            agent="codex",
            host="codex",
            domain="project-a",
            task="small run",
            status="success",
            input_tokens=80_000,
            output_tokens=4_000,
            model="gpt-5.5-pro",
            files_touched=["a.py"],
            created_at=created_at,
        ),
        Trace(
            id="outlier",
            agent="codex",
            host="codex",
            domain="project-a",
            task="large run",
            status="success",
            input_tokens=1_000_000,
            output_tokens=10_000,
            model="gpt-5.5-pro",
            created_at=created_at - timedelta(days=2),
        ),
        Trace(
            id="context-heavy",
            agent="claude",
            host="claude",
            domain="project-b",
            task="context heavy",
            status="success",
            input_tokens=120_000,
            cached_input_tokens=260_000,
            output_tokens=4_000,
            files_touched=["b.py"],
            model="gpt-5.5-pro",
            created_at=created_at - timedelta(days=1),
        ),
    ]
    for trace in traces:
        store.record_trace(trace)


def _write_live_savings_events(path: Path) -> None:
    now = datetime.now(UTC)
    rows = [
        {
            "at": now.isoformat(),
            "session_id": "peer-low",
            "agent": "codex",
            "tool_name": "read",
            "lever": "structure_map",
            "read_mode": "outline",
            "path": "/workspace/app.py",
            "tokens_saved": 180,
            "cost_saved_usd": 0.0012,
            "calls_saved": 0,
            "time_saved_ms": 0,
        },
        {
            "at": now.isoformat(),
            "session_id": "peer-low",
            "agent": "codex",
            "tool_name": "read",
            "lever": "delta_read",
            "read_mode": "range",
            "path": "/workspace/app.py",
            "range": "10-40",
            "tokens_saved": 120,
            "cost_saved_usd": 0.0008,
            "calls_saved": 0,
            "time_saved_ms": 0,
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def test_optimizations_summary_returns_runtime_catalog_and_recommendations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".atelier"
    (tmp_path / "AGENTS.md").write_text(
        "# Project rules\n" + "- Keep context narrow and delivery-focused.\n" * 40,
        encoding="utf-8",
    )
    _write_cost_history(root / "cost_history.json")
    _write_live_savings_events(root / "live_savings_events.jsonl")
    _record_traces(root)
    (root / "blocks").mkdir(parents=True, exist_ok=True)
    (root / "blocks" / "delivery.md").write_text(
        "# Delivery procedure\n\nAlways verify the slice you changed.\n",
        encoding="utf-8",
    )
    (root / "rubrics").mkdir(parents=True, exist_ok=True)
    (root / "rubrics" / "delivery.yaml").write_text(
        "id: delivery-check\ndomain: swe.general\nrequired_checks:\n  - verify\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app(store_root=root))
    resp = client.get("/v1/optimizations/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["automatic_hosts"] == 5
    assert data["advisory_only_hosts"] == 0
    assert any(item["host"] == "claude" and item["automatic_mid_session"] for item in data["runtime_coverage"])
    assert any(item["host"] == "codex" and item["automatic_mid_session"] for item in data["runtime_coverage"])
    assert any(item["host"] == "copilot" and item["automatic_at_start"] for item in data["runtime_coverage"])
    assert any(
        item["id"] == "search_read" and item["observed_tokens_saved"] == 60 for item in data["implemented_levers"]
    )
    assert any(
        item["id"] == "batch_edit" and item["observed_tokens_saved"] == 50 for item in data["implemented_levers"]
    )

    recommendation_ids = {item["id"] for item in data["recommendations"]["recommendations"]}
    assert "high-cost-session-outliers" in recommendation_ids
    assert "context-heavy-sessions" in recommendation_ids
    assert "low-worth-expensive-sessions" in recommendation_ids

    auto_ids = {item["id"] for item in data["auto_optimizations"]}
    assert {"search_read", "batch_edit", "structure_map", "delta_read"} <= auto_ids

    assert data["impact_validation"]["strategy"] == "chronological_halves"
    assert data["impact_validation"]["verdict"] == "improved"
    assert data["impact_validation"]["before"]["trace_count"] == 1
    assert data["impact_validation"]["after"]["trace_count"] == 2
    assert data["impact_validation"]["deltas"]["tokens_pct"] < 0
    assert data["impact_validation"]["deltas"]["cost_pct"] < 0

    assert data["reread_telemetry"]["event_count"] == 2
    assert data["reread_telemetry"]["total_tokens_saved"] == 300
    assert {item["id"] for item in data["reread_telemetry"]["kinds"]} == {"structure_map", "delta_read"}
    assert data["reread_telemetry"]["top_paths"][0]["path"] == "/workspace/app.py"

    assert data["model_routing_simulation"]["candidate_count"] == 1
    assert data["model_routing_simulation"]["estimated_cost_saved_usd"] > 0
    assert data["model_routing_simulation"]["candidates"][0]["trace_id"] == "peer-low"

    assert data["context_audit"]["always_on_tokens"] > 0
    assert data["context_audit"]["component_count"] >= 3
    assert any(item["id"] == "repo_guidance" for item in data["context_audit"]["components"])

    assert data["quality_score"]["trace_count"] == 3
    assert 0 <= data["quality_score"]["score"] <= 100
    assert any(item["id"] == "context_fill" for item in data["quality_score"]["signals"])
    assert data["quality_score"]["recommendations"]
