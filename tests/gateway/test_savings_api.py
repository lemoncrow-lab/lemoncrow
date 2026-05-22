from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="FastAPI API tests require the api extra")

from fastapi.testclient import TestClient

from atelier.core.foundation.models import Trace
from atelier.core.foundation.savings_models import ContextBudget
from atelier.core.service.api import create_app
from atelier.infra.storage.factory import create_store


def _write_cost_history(path: Path) -> None:
    now = datetime.now(UTC)
    history = {
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
                    },
                    {
                        "operation": "search_read",
                        "model": "test-model",
                        "input_tokens": 80,
                        "output_tokens": 20,
                        "cache_read_tokens": 40,
                        "cost_usd": 0.008,
                        "lessons_used": [],
                        "op_key": "op-search",
                        "at": (now - timedelta(days=1)).isoformat(),
                    },
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
    path.write_text(json.dumps(history), encoding="utf-8")


def _write_live_savings_events(path: Path) -> None:
    now = datetime.now(UTC)
    rows = [
        {
            "at": now.isoformat(),
            "session_id": "run-live-1",
            "agent": "codex",
            "tool_name": "search",
            "lever": "search_read",
            "equivalent_baseline_calls": 3.0,
            "calls_saved": 2,
            "time_saved_ms": 50_000,
            "input_tokens_saved": 52_000,
            "output_tokens_saved": 100_000,
            "cache_read_tokens_saved": 2_600,
            "cache_write_tokens_saved": 0,
            "live_tokens_saved": 154_600,
            "tool_tokens_saved": 400,
            "tokens_saved": 155_000,
            "cost_saved_usd": 1.66278,
            "model": "claude-sonnet-4",
        },
        {
            "at": (now - timedelta(days=40)).isoformat(),
            "session_id": "old-run",
            "agent": "codex",
            "tool_name": "sql",
            "lever": "sql_batch",
            "calls_saved": 4,
            "tokens_saved": 300_000,
            "cost_saved_usd": 3.0,
            "time_saved_ms": 100_000,
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _write_latest_benchmark(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "bench-live",
                "model": "test-model",
                "n_prompts": 2,
                "total_tokens_baseline": 1000,
                "total_tokens_atelier": 600,
                "tokens_saved": 400,
                "reduction_pct": 40.0,
                "total_cost_baseline_usd": 0.02,
                "total_cost_atelier_usd": 0.012,
                "cost_saved_usd": 0.008,
                "total_time_baseline_ms": 2000,
                "total_time_atelier_ms": 1500,
                "time_saved_ms": 500,
                "baseline_success_rate": 1.0,
                "atelier_success_rate": 1.0,
                "prompts": [],
            }
        ),
        encoding="utf-8",
    )


def _write_context_budget(
    root: Path,
    *,
    session_id: str,
    naive_input_tokens: int,
    output_tokens: int,
    lever_savings: dict[str, int] | None = None,
    turn_index: int = 0,
) -> object:
    store = create_store(root)
    store.init()
    saved_tokens = naive_input_tokens - output_tokens
    store.persist_context_budget(
        ContextBudget(
            session_id=session_id,
            turn_index=turn_index,
            model="test-model",
            input_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=output_tokens,
            naive_input_tokens=naive_input_tokens,
            lever_savings=lever_savings or {"search_read": saved_tokens},
            tool_calls=1,
        )
    )
    return store


def _write_run_ledger_snapshot(root: Path, *, session_id: str, tool_name: str) -> None:
    now = datetime.now(UTC).isoformat()
    runs_dir = root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "agent": "codex",
        "task": "tracked savings proof",
        "status": "success",
        "created_at": now,
        "events": [
            {
                "kind": "tool_result",
                "summary": f"{tool_name} result",
                "payload": {
                    "tool": tool_name,
                    "output": "ok",
                    "output_chars": 24,
                },
            }
        ],
    }
    (runs_dir / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_run_ledger_snapshot_with_events(
    root: Path,
    *,
    session_id: str,
    events: list[dict[str, object]],
    tools_called: list[str] | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    runs_dir = root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "agent": "codex",
        "task": "tracked savings proof",
        "status": "success",
        "created_at": now,
        "tools_called": tools_called or [],
        "events": events,
    }
    (runs_dir / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_savings_summary_returns_per_lever_and_by_day(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _write_cost_history(root / "cost_history.json")

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app())
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["window_days"] == 14
    assert data["total_naive_tokens"] == 525
    assert data["total_actual_tokens"] == 375
    assert data["reduction_pct"] == 28.6
    assert data["per_lever"]["search_read"] == 100
    assert data["per_lever"]["batch_edit"] == 50
    assert len(data["by_day"]) == 14
    assert all("day" in row and "naive" in row and "actual" in row for row in data["by_day"])


def test_savings_summary_includes_live_plugin_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _write_cost_history(root / "cost_history.json")
    _write_live_savings_events(root / "live_savings_events.jsonl")
    _write_latest_benchmark(root / "benchmarks" / "savings" / "latest.json")

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app())
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_naive_tokens"] == 155_125
    assert data["total_actual_tokens"] == 375
    assert data["per_lever"]["search_read"] == 154_700
    assert data["live_calls_saved"] == 2
    assert data["live_time_saved_ms"] == 50_000
    assert data["live_saved_usd"] == 1.66278
    assert data["top_sources"] == [
        {
            "lever": "search_read",
            "tool_name": "search",
            "calls_saved": 2,
            "tokens_saved": 154_600,
            "cost_saved_usd": 1.66278,
            "time_saved_ms": 50_000,
        }
    ]
    assert data["latest_benchmark"]["session_id"] == "bench-live"
    assert data["latest_benchmark"]["reduction_pct"] == 40.0
    assert "prompts" not in data["latest_benchmark"]


def test_savings_summary_uses_context_budget_for_live_run_totals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".atelier"
    store = _write_context_budget(
        root,
        session_id="run-live-1",
        naive_input_tokens=1000,
        output_tokens=600,
        lever_savings={"batch_edit": 400},
    )
    _write_run_ledger_snapshot(root, session_id="run-live-1", tool_name="edit")
    (root / "live_savings_events.jsonl").write_text(
        json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "session_id": "run-live-1",
                "agent": "codex",
                "tool_name": "edit",
                "lever": "batch_edit",
                "equivalent_baseline_calls": 3.0,
                "calls_saved": 2,
                "time_saved_ms": 50_000,
                "input_tokens_saved": 52_000,
                "output_tokens_saved": 100_000,
                "cache_read_tokens_saved": 2_600,
                "cache_write_tokens_saved": 0,
                "live_tokens_saved": 154_600,
                "tool_tokens_saved": 400,
                "tokens_saved": 155_000,
                "cost_saved_usd": 1.66278,
                "model": "claude-sonnet-4",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app(store=store))
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_naive_tokens"] == 1000
    assert data["total_actual_tokens"] == 600
    assert data["reduction_pct"] == 40.0
    assert data["per_lever"]["batch_edit"] == 400
    assert data["tracked_tool_calls"] == 1
    assert data["live_calls_saved"] == 2
    assert data["top_sources"][0]["tokens_saved"] == 400
    assert data["cost_basis"] == "context_budget"
    assert data["actually_cost_usd"] == pytest.approx(0.009, abs=1e-6)
    assert data["would_have_cost_usd"] == pytest.approx(0.012, abs=1e-6)
    assert data["saved_usd"] == pytest.approx(0.003, abs=1e-6)
    assert data["tool_aggregates"] == [
        {
            "tool_name": "edit",
            "lever": "batch_edit",
            "turns": 1,
            "session_count": 1,
            "actual_tokens": 600,
            "naive_tokens": 1000,
            "saved_tokens": 400,
            "actual_cost_usd": 0.009,
            "baseline_cost_usd": 0.012,
            "saved_cost_usd": 0.003,
            "live_calls_saved": 2,
            "live_time_saved_ms": 50_000,
            "live_saved_usd": 1.66278,
        }
    ]
    assert data["session_proof"][0]["session_id"] == "run-live-1"
    assert data["session_proof"][0]["agent"] == "codex"
    assert data["session_proof"][0]["items"][0]["tool_name"] == "edit"
    assert data["session_proof"][0]["items"][0]["saved_tokens"] == 400
    assert data["verification"] == {
        "data_root": str(root),
        "headline_kind": "tracked_proof_reduction",
        "headline_explanation": (
            "These top-line totals come from headline-eligible context-budget proof rows and exclude live-estimate-only overlays. "
            "They are proof-oriented estimates, not audited provider billing."
        ),
        "tracked_row_count": 1,
        "tracked_run_count": 1,
        "trace_linked_run_count": 0,
        "ledger_backed_run_count": 1,
        "live_event_count": 1,
        "coverage_gap_count": 0,
        "compact_output_row_count": 0,
        "compact_output_saved_tokens": 0,
        "dominant_run": {
            "session_id": "run-live-1",
            "agent": "codex",
            "task": "tracked savings proof",
            "saved_tokens": 400,
            "saved_cost_usd": 0.003,
        },
        "dominant_item": {
            "session_id": "run-live-1",
            "turn_index": 0,
            "tool_name": "edit",
            "lever": "batch_edit",
            "actual_tokens": 600,
            "naive_tokens": 1000,
            "saved_tokens": 400,
            "created_at": data["verification"]["dominant_item"]["created_at"],
        },
        "dominant_run_share_pct": 100.0,
        "dominant_item_share_pct": 100.0,
        "warning": (
            "One proof row dominates the estimated saved-token total. Inspect the leading session/item rows below before trusting the aggregate."
        ),
    }


def test_savings_summary_excludes_compact_tool_output_rows_from_headline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".atelier"
    store = _write_context_budget(
        root,
        session_id="run-compact-1",
        naive_input_tokens=1000,
        output_tokens=600,
        lever_savings={"compact_tool_output:search_read": 400},
    )
    _write_run_ledger_snapshot(root, session_id="run-compact-1", tool_name="search")
    (root / "live_savings_events.jsonl").write_text(
        json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "session_id": "run-compact-1",
                "agent": "codex",
                "tool_name": "search",
                "lever": "search_read",
                "equivalent_baseline_calls": 3.0,
                "calls_saved": 2,
                "time_saved_ms": 50_000,
                "input_tokens_saved": 52_000,
                "output_tokens_saved": 100_000,
                "cache_read_tokens_saved": 2_600,
                "cache_write_tokens_saved": 0,
                "live_tokens_saved": 154_600,
                "tool_tokens_saved": 400,
                "tokens_saved": 155_000,
                "cost_saved_usd": 1.66278,
                "model": "claude-sonnet-4",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app(store=store))
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_naive_tokens"] == 0
    assert data["total_actual_tokens"] == 0
    assert data["reduction_pct"] == 0.0
    assert data["per_lever"] == {}
    assert data["tracked_tool_calls"] == 0
    assert data["live_calls_saved"] == 2
    assert data["top_sources"][0]["tokens_saved"] == 400
    assert data["cost_basis"] == "context_budget"
    assert data["actually_cost_usd"] == pytest.approx(0.0, abs=1e-6)
    assert data["would_have_cost_usd"] == pytest.approx(0.0, abs=1e-6)
    assert data["saved_usd"] == pytest.approx(0.0, abs=1e-6)
    assert data["tool_aggregates"] == [
        {
            "tool_name": "search",
            "lever": "search_read",
            "turns": 1,
            "session_count": 1,
            "actual_tokens": 600,
            "naive_tokens": 1000,
            "saved_tokens": 400,
            "actual_cost_usd": 0.009,
            "baseline_cost_usd": 0.012,
            "saved_cost_usd": 0.003,
            "live_calls_saved": 2,
            "live_time_saved_ms": 50_000,
            "live_saved_usd": 1.66278,
        }
    ]
    assert data["session_proof"][0]["session_id"] == "run-compact-1"
    assert data["session_proof"][0]["items"][0]["tool_name"] == "search"
    assert data["session_proof"][0]["items"][0]["saved_tokens"] == 400
    assert data["verification"] == {
        "data_root": str(root),
        "headline_kind": "tracked_proof_reduction",
        "headline_explanation": (
            "This headline excludes compact-tool-output rows such as search_read naive-vs-compacted comparisons. "
            "Those rows remain in the proof tables below as tool-output compression evidence, "
            "but they do not count toward top-line token or cost savings. "
            "The headline is still a proof-oriented estimate, not audited provider billing."
        ),
        "tracked_row_count": 1,
        "tracked_run_count": 1,
        "trace_linked_run_count": 0,
        "ledger_backed_run_count": 1,
        "live_event_count": 1,
        "coverage_gap_count": 0,
        "compact_output_row_count": 1,
        "compact_output_saved_tokens": 400,
        "dominant_run": {
            "session_id": "run-compact-1",
            "agent": "codex",
            "task": "tracked savings proof",
            "saved_tokens": 400,
            "saved_cost_usd": 0.003,
        },
        "dominant_item": {
            "session_id": "run-compact-1",
            "turn_index": 0,
            "tool_name": "search",
            "lever": "search_read",
            "actual_tokens": 600,
            "naive_tokens": 1000,
            "saved_tokens": 400,
            "created_at": data["verification"]["dominant_item"]["created_at"],
        },
        "dominant_run_share_pct": 100.0,
        "dominant_item_share_pct": 100.0,
        "warning": (
            "1 compact-tool-output proof row(s) were excluded from the headline totals. "
            "One proof row dominates the estimated saved-token total. Inspect the leading session/item rows below before trusting the aggregate."
        ),
    }


def test_savings_summary_clamps_zero_saved_rows_to_zero_cost_delta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".atelier"
    store = _write_context_budget(
        root,
        session_id="run-zero-1",
        naive_input_tokens=515,
        output_tokens=515,
        lever_savings={},
    )

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app(store=store))
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tool_aggregates"] == [
        {
            "tool_name": "unattributed",
            "lever": "unattributed",
            "turns": 1,
            "session_count": 1,
            "actual_tokens": 515,
            "naive_tokens": 515,
            "saved_tokens": 0,
            "actual_cost_usd": 0.007725,
            "baseline_cost_usd": 0.007725,
            "saved_cost_usd": 0.0,
            "live_calls_saved": 0,
            "live_time_saved_ms": 0,
            "live_saved_usd": 0.0,
        }
    ]
    assert data["session_proof"][0]["items"][0]["tool_name"] == "unattributed"
    assert data["session_proof"][0]["items"][0]["saved_cost_usd"] == pytest.approx(0.0, abs=1e-6)


def test_savings_summary_uses_nearest_ledger_tool_event_when_turn_index_drifts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".atelier"
    store = _write_context_budget(
        root,
        session_id="run-ledger-nearest",
        naive_input_tokens=418,
        output_tokens=418,
        lever_savings={},
        turn_index=5,
    )
    _write_run_ledger_snapshot_with_events(
        root,
        session_id="run-ledger-nearest",
        tools_called=["get_context", "check_plan"],
        events=[
            {
                "kind": "note",
                "summary": "startup",
                "payload": {},
            },
            {
                "kind": "tool_result",
                "summary": "lint result",
                "payload": {"tool": "lint", "output": "ok", "output_chars": 32},
            },
            {
                "kind": "tool_result",
                "summary": "trace result",
                "payload": {"tool": "trace", "output": "ok", "output_chars": 32},
            },
        ],
    )

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app(store=store))
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tool_aggregates"][0]["tool_name"] == "trace"
    assert data["session_proof"][0]["items"][0]["tool_name"] == "trace"


def test_savings_summary_backfills_agent_from_live_event_for_untraced_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".atelier"
    store = create_store(root)
    store.init()
    store.persist_context_budget(
        ContextBudget(
            session_id="run-live-agent",
            turn_index=0,
            model="test-model",
            input_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=600,
            naive_input_tokens=1000,
            lever_savings={"search_read": 400},
            tool_calls=1,
        )
    )
    store.persist_context_budget(
        ContextBudget(
            session_id="run-live-agent",
            turn_index=1,
            model="test-model",
            input_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=515,
            naive_input_tokens=515,
            lever_savings={},
            tool_calls=1,
        )
    )
    (root / "live_savings_events.jsonl").write_text(
        json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "session_id": "run-live-agent",
                "agent": "claude",
                "tool_name": "search",
                "lever": "search_read",
                "calls_saved": 1,
                "time_saved_ms": 25_000,
                "tokens_saved": 400,
                "cost_saved_usd": 0.25,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app(store=store))
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_proof"][0]["agent"] == "claude"


def test_savings_summary_surfaces_untracked_copilot_coverage_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".atelier"
    store = create_store(root)
    store.init()
    store.record_trace(
        Trace(
            id="copilot-gap",
            session_id="copilot-gap",
            agent="copilot",
            domain="coding",
            task="investigate copilot savings",
            status="success",
            trace_confidence="manual",
        ),
        write_json=False,
    )

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app(store=store))
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["coverage_gaps"] == [
        {
            "session_id": "copilot-gap",
            "trace_id": "copilot-gap",
            "agent": "copilot",
            "task": "investigate copilot savings",
            "status": "success",
            "trace_confidence": "manual",
            "created_at": data["coverage_gaps"][0]["created_at"],
            "reason": "Copilot trace/import exists, but no live MCP savings telemetry was captured for this run; proof is limited to the imported trace/ledger surface.",
            "missing_surfaces": [],
        }
    ]


def test_savings_summary_empty_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    monkeypatch.setenv("ATELIER_ROOT", str(root))

    client = TestClient(create_app())
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["window_days"] == 14
    assert data["total_naive_tokens"] == 0
    assert data["total_actual_tokens"] == 0
    assert data["reduction_pct"] == 0.0
    assert data["per_lever"] == {}
    assert len(data["by_day"]) == 14


def test_record_context_budget_avoids_double_counting_tool_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atelier.core.capabilities import plugin_runtime
    from atelier.gateway.adapters import mcp_server

    events: list[dict[str, object]] = []
    recorder_rows: list[dict[str, object]] = []
    compact_rows: list[dict[str, object]] = []

    class Recorder:
        def record(self, **kwargs: object) -> None:
            recorder_rows.append(kwargs)

        def record_compact_tool_output(self, **kwargs: object) -> None:
            compact_rows.append(kwargs)

    monkeypatch.setattr(mcp_server, "_get_context_budget_recorder", lambda: Recorder())
    monkeypatch.setattr(mcp_server, "_record_smart_state_savings", lambda **_: None)
    monkeypatch.setattr(mcp_server, "_append_live_savings_event", lambda event: events.append(event))
    monkeypatch.setattr(plugin_runtime, "equivalent_calls", lambda *_args, **_kwargs: 3.0)
    monkeypatch.setattr(
        plugin_runtime,
        "compute_live_savings",
        lambda *_args, **_kwargs: {
            "calls_saved": 2,
            "time_saved_ms": 50_000,
            "input_tokens_saved": 52_000,
            "output_tokens_saved": 100_000,
            "cache_read_tokens_saved": 2_600,
            "cache_write_tokens_saved": 0,
        },
    )

    led = SimpleNamespace(session_id="run-proof", agent="codex", model="claude-sonnet-4", events=[{}, {}, {}])
    mcp_server._record_context_budget_for_tool(
        "search_read",
        {},
        led,
        {"tokens_saved_vs_naive": 400, "total_tokens": 600},
    )

    assert events[0]["live_tokens_saved"] == 154_600
    assert events[0]["tool_tokens_saved"] == 400
    assert events[0]["tokens_saved"] == 400
    assert recorder_rows == []
    assert compact_rows == [
        {
            "session_id": "run-proof",
            "turn_index": 2,
            "model": "claude-sonnet-4",
            "method": "search_read",
            "tokens_in": 1000,
            "tokens_out": 600,
        }
    ]


def test_record_context_budget_persists_tool_marker_for_zero_saved_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atelier.core.capabilities import plugin_runtime
    from atelier.gateway.adapters import mcp_server

    recorder_rows: list[dict[str, object]] = []

    class Recorder:
        def record(self, **kwargs: object) -> None:
            recorder_rows.append(kwargs)

        def record_compact_tool_output(self, **kwargs: object) -> None:
            raise AssertionError("compact recording should not be used for zero-saved turns")

    monkeypatch.setattr(mcp_server, "_get_context_budget_recorder", lambda: Recorder())
    monkeypatch.setattr(mcp_server, "_record_smart_state_savings", lambda **_: None)
    monkeypatch.setattr(mcp_server, "_append_live_savings_event", lambda _event: None)
    monkeypatch.setattr(plugin_runtime, "equivalent_calls", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(
        plugin_runtime,
        "compute_live_savings",
        lambda *_args, **_kwargs: {
            "calls_saved": 0,
            "time_saved_ms": 0,
            "input_tokens_saved": 0,
            "output_tokens_saved": 0,
            "cache_read_tokens_saved": 0,
            "cache_write_tokens_saved": 0,
        },
    )

    led = SimpleNamespace(session_id="run-proof", agent="codex", model="claude-sonnet-4", events=[{}, {}])
    mcp_server._record_context_budget_for_tool(
        "trace",
        {},
        led,
        {"total_tokens": 418},
    )

    assert recorder_rows == [
        {
            "session_id": "run-proof",
            "turn_index": 1,
            "model": "claude-sonnet-4",
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 418,
            "naive_input_tokens": 418,
            "lever_savings": {"tool:trace": 0},
            "tool_calls": 1,
        }
    ]


@pytest.mark.parametrize(
    ("mode", "expected_lever", "extra_args", "expected_range"),
    [
        ("outline", "structure_map", {}, None),
        ("range", "delta_read", {"range": "12-40"}, "12-40"),
    ],
)
def test_record_context_budget_classifies_smart_read_savings(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_lever: str,
    extra_args: dict[str, object],
    expected_range: str | None,
) -> None:
    from atelier.core.capabilities import plugin_runtime
    from atelier.gateway.adapters import mcp_server

    events: list[dict[str, object]] = []
    recorder_rows: list[dict[str, object]] = []

    class Recorder:
        def record(self, **kwargs: object) -> None:
            recorder_rows.append(kwargs)

        def record_compact_tool_output(self, **kwargs: object) -> None:
            raise AssertionError("compact recording should not be used for smart read telemetry")

    monkeypatch.setattr(mcp_server, "_get_context_budget_recorder", lambda: Recorder())
    monkeypatch.setattr(mcp_server, "_record_smart_state_savings", lambda **_: None)
    monkeypatch.setattr(mcp_server, "_append_live_savings_event", lambda event: events.append(event))
    monkeypatch.setattr(plugin_runtime, "equivalent_calls", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(
        plugin_runtime,
        "compute_live_savings",
        lambda *_args, **_kwargs: {
            "calls_saved": 0,
            "time_saved_ms": 0,
            "input_tokens_saved": 0,
            "output_tokens_saved": 0,
            "cache_read_tokens_saved": 0,
            "cache_write_tokens_saved": 0,
        },
    )

    led = SimpleNamespace(session_id="run-proof", agent="codex", model="claude-sonnet-4", events=[{}, {}, {}])
    args = {"path": "/tmp/sample.py", **extra_args}
    result = {
        "mode": mode,
        "path": "/tmp/sample.py",
        "cache_hit": False,
        "tokens_saved": 240,
        "total_tokens": 180,
    }
    if expected_range is not None:
        result["range"] = expected_range

    mcp_server._record_context_budget_for_tool("read", args, led, result)

    # Tool-reported savings (240 tokens) are now credited to input_tokens_saved
    # and priced via LiteLLM (claude-sonnet-4 input = $3/1M).
    assert events == [
        {
            "agent": "codex",
            "at": events[0]["at"],
            "cache_hit": False,
            "cache_read_tokens_saved": 0,
            "cache_write_tokens_saved": 0,
            "calls_saved": 0,
            "cost_saved_usd": 0.00072,
            "equivalent_baseline_calls": 0.0,
            "input_tokens_saved": 240,
            "lever": expected_lever,
            "live_tokens_saved": 240,
            "model": "claude-sonnet-4",
            "output_tokens_saved": 0,
            "path": "/tmp/sample.py",
            "read_mode": mode,
            "session_id": "run-proof",
            "time_saved_ms": 0,
            "tokens_saved": 240,
            "tool_name": "read",
            "tool_tokens_saved": 240,
            **({"range": expected_range} if expected_range is not None else {}),
        }
    ]
    assert recorder_rows == [
        {
            "session_id": "run-proof",
            "turn_index": 2,
            "model": "claude-sonnet-4",
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 180,
            "naive_input_tokens": 420,
            "lever_savings": {expected_lever: 240, "tool:read": 0},
            "tool_calls": 1,
        }
    ]


def test_record_context_budget_attaches_cache_metadata_for_code_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atelier.core.capabilities import plugin_runtime
    from atelier.gateway.adapters import mcp_server

    events: list[dict[str, object]] = []
    recorder_rows: list[dict[str, object]] = []

    class Recorder:
        def record(self, **kwargs: object) -> None:
            recorder_rows.append(kwargs)

        def record_compact_tool_output(self, **kwargs: object) -> None:
            raise AssertionError("compact recording should not be used for direct code-tool savings")

    monkeypatch.setattr(mcp_server, "_get_context_budget_recorder", lambda: Recorder())
    monkeypatch.setattr(mcp_server, "_record_smart_state_savings", lambda **_: None)
    monkeypatch.setattr(mcp_server, "_append_live_savings_event", lambda event: events.append(event))
    monkeypatch.setattr(plugin_runtime, "equivalent_calls", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(
        plugin_runtime,
        "compute_live_savings",
        lambda *_args, **_kwargs: {
            "calls_saved": 0,
            "time_saved_ms": 0,
            "input_tokens_saved": 0,
            "output_tokens_saved": 0,
            "cache_read_tokens_saved": 0,
            "cache_write_tokens_saved": 0,
        },
    )

    led = SimpleNamespace(session_id="run-proof", agent="codex", model="claude-sonnet-4", events=[{}, {}, {}])
    args = {"op": "search", "query": "OrderService"}
    result = {
        "cache_hit": True,
        "provenance": "cached",
        "tokens_saved": 120,
        "total_tokens": 80,
    }

    mcp_server._record_context_budget_for_tool("code", args, led, result)

    # 120 saved tokens credited as input via LiteLLM pricing ($3/1M).
    assert events == [
        {
            "agent": "codex",
            "at": events[0]["at"],
            "cache_hit": True,
            "cache_read_tokens_saved": 0,
            "cache_write_tokens_saved": 0,
            "calls_saved": 0,
            "cost_saved_usd": 0.00036,
            "equivalent_baseline_calls": 0.0,
            "input_tokens_saved": 120,
            "lever": "code",
            "live_tokens_saved": 120,
            "model": "claude-sonnet-4",
            "op": "search",
            "output_tokens_saved": 0,
            "provenance": "cached",
            "session_id": "run-proof",
            "time_saved_ms": 0,
            "tokens_saved": 120,
            "tool_name": "code",
            "tool_tokens_saved": 120,
        }
    ]
    assert recorder_rows == [
        {
            "session_id": "run-proof",
            "turn_index": 2,
            "model": "claude-sonnet-4",
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 80,
            "naive_input_tokens": 200,
            "lever_savings": {"code": 120, "tool:code": 0},
            "tool_calls": 1,
        }
    ]


def test_record_context_budget_attaches_local_metadata_for_uncached_code_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atelier.core.capabilities import plugin_runtime
    from atelier.gateway.adapters import mcp_server

    events: list[dict[str, object]] = []
    recorder_rows: list[dict[str, object]] = []

    class Recorder:
        def record(self, **kwargs: object) -> None:
            recorder_rows.append(kwargs)

        def record_compact_tool_output(self, **kwargs: object) -> None:
            raise AssertionError("compact recording should not be used for direct code-tool savings")

    monkeypatch.setattr(mcp_server, "_get_context_budget_recorder", lambda: Recorder())
    monkeypatch.setattr(mcp_server, "_record_smart_state_savings", lambda **_: None)
    monkeypatch.setattr(mcp_server, "_append_live_savings_event", lambda event: events.append(event))
    monkeypatch.setattr(plugin_runtime, "equivalent_calls", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(
        plugin_runtime,
        "compute_live_savings",
        lambda *_args, **_kwargs: {
            "calls_saved": 0,
            "time_saved_ms": 0,
            "input_tokens_saved": 0,
            "output_tokens_saved": 0,
            "cache_read_tokens_saved": 0,
            "cache_write_tokens_saved": 0,
        },
    )

    led = SimpleNamespace(session_id="run-proof", agent="codex", model="claude-sonnet-4", events=[{}, {}, {}])
    args = {"op": "search", "query": "OrderService"}
    result = {
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 40,
        "total_tokens": 120,
    }

    mcp_server._record_context_budget_for_tool("code", args, led, result)

    # 40 saved tokens credited as input via LiteLLM pricing ($3/1M).
    assert events == [
        {
            "agent": "codex",
            "at": events[0]["at"],
            "cache_hit": False,
            "cache_read_tokens_saved": 0,
            "cache_write_tokens_saved": 0,
            "calls_saved": 0,
            "cost_saved_usd": 0.00012,
            "equivalent_baseline_calls": 0.0,
            "input_tokens_saved": 40,
            "lever": "code",
            "live_tokens_saved": 40,
            "model": "claude-sonnet-4",
            "op": "search",
            "output_tokens_saved": 0,
            "provenance": "local",
            "session_id": "run-proof",
            "time_saved_ms": 0,
            "tokens_saved": 40,
            "tool_name": "code",
            "tool_tokens_saved": 40,
        }
    ]
    assert recorder_rows == [
        {
            "session_id": "run-proof",
            "turn_index": 2,
            "model": "claude-sonnet-4",
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 120,
            "naive_input_tokens": 160,
            "lever_savings": {"code": 40, "tool:code": 0},
            "tool_calls": 1,
        }
    ]
