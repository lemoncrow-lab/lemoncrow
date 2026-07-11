from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI API tests require the api extra")

from typing import cast

from fastapi.testclient import TestClient

from lemoncrow.core.foundation.models import Trace
from lemoncrow.core.foundation.savings_models import ContextBudget
from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.core.service.api import create_app
from lemoncrow.infra.storage.factory import create_store


def _write_cost_history(path: Path) -> None:
    now = datetime.now(UTC)
    history = {
        "operations": {
            "op-search": {
                "domain": "lemoncrow.platform",
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
                "domain": "lemoncrow.platform",
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
                "total_tokens_lemoncrow": 600,
                "tokens_saved": 400,
                "reduction_pct": 40.0,
                "total_cost_baseline_usd": 0.02,
                "total_cost_lemoncrow_usd": 0.012,
                "cost_saved_usd": 0.008,
                "total_time_baseline_ms": 2000,
                "total_time_lemoncrow_ms": 1500,
                "time_saved_ms": 500,
                "baseline_success_rate": 1.0,
                "lemoncrow_success_rate": 1.0,
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
) -> ContextStore:
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
    return cast(ContextStore, store)


def _write_run_ledger_snapshot(root: Path, *, session_id: str, tool_name: str) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    now = datetime.now(UTC).isoformat()
    runs_dir = session_dir(root, "codex", session_id)
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
    (runs_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_run_ledger_snapshot_with_events(
    root: Path,
    *,
    session_id: str,
    events: list[dict[str, object]],
    tools_called: list[str] | None = None,
) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    now = datetime.now(UTC).isoformat()
    runs_dir = session_dir(root, "codex", session_id)
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
    (runs_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")


def test_savings_summary_returns_per_lever_and_by_day(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_cost_history(root / "cost_history.json")

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

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
    root = tmp_path / ".lemoncrow"
    _write_cost_history(root / "cost_history.json")
    _write_live_savings_events(root / "live_savings_events.jsonl")
    _write_latest_benchmark(root / "benchmarks" / "savings" / "latest.json")

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

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
    root = tmp_path / ".lemoncrow"
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

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

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
    assert data["would_have_cost_usd"] == pytest.approx(0.0102, abs=1e-6)
    assert data["saved_usd"] == pytest.approx(0.0012, abs=1e-6)
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
            "baseline_cost_usd": 0.0102,
            "saved_cost_usd": 0.0012,
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
            "saved_cost_usd": 0.0012,
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
    root = tmp_path / ".lemoncrow"
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

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

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
            "baseline_cost_usd": 0.0102,
            "saved_cost_usd": 0.0012,
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
            "saved_cost_usd": 0.0012,
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
    root = tmp_path / ".lemoncrow"
    store = _write_context_budget(
        root,
        session_id="run-zero-1",
        naive_input_tokens=515,
        output_tokens=515,
        lever_savings={},
    )

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

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
    root = tmp_path / ".lemoncrow"
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

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

    client = TestClient(create_app(store=store))
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tool_aggregates"][0]["tool_name"] == "trace"
    assert data["session_proof"][0]["items"][0]["tool_name"] == "trace"


def test_savings_summary_backfills_agent_from_live_event_for_untraced_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".lemoncrow"
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

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

    client = TestClient(create_app(store=store))
    resp = client.get("/v1/savings/summary?window_days=14")

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_proof"][0]["agent"] == "claude"


def test_savings_summary_surfaces_untracked_copilot_coverage_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".lemoncrow"
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

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

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


def test_savings_summary_headline_uses_session_ledger_rule(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without context-budget proof rows the headline must equal the per-session
    ledger figure (the statusline / stop-hook / CLI rule). Routing is now
    FOLDED into the headline saved_usd/total_saved_usd (Total saved = Read +
    Carry + Output + Routing, per the locked-in consistency decision) while
    still riding its own routing_saved_usd field; the ops composite (a
    different spend domain) stays separate."""
    import json as _json
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    root = tmp_path / ".lemoncrow"
    sdir = root / "sessions" / "22222222-2222-2222-2222-222222222222"
    sdir.mkdir(parents=True, exist_ok=True)
    now = _datetime.now(_UTC).isoformat()
    rows = [
        {"tool": "read", "tokens": 2000, "calls": 1, "cost_saved_usd": 0.02, "ts": now},
        {"kind": "routing", "usd": 0.4, "tool": "edit", "model": "claude-sonnet-4-5", "ts": now},
    ]
    (sdir / "savings.jsonl").write_text("\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

    client = TestClient(create_app())
    resp = client.get("/v1/savings/summary?window_days=7")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cost_basis"] == "session_ledger"
    # Headline now includes routing: 0.02 (context savings) + 0.4 (routing).
    assert data["saved_usd"] == pytest.approx(0.42)
    # Routing still rides its own field (breakdown detail) too.
    assert data["routing_saved_usd"] == pytest.approx(0.4)
    # The "read" row is a read-lever row: shows up in the Read breakdown
    # (raw cost_saved_usd, mirroring the per-session read-savings rule).
    assert data["read_saved_usd"] == pytest.approx(0.02)
    # No session_end row in this ledger -> no carry; total == saved_usd.
    assert data["carry_usd"] == pytest.approx(0.0)
    assert data["carry_tokens"] == 0
    assert data["total_saved_usd"] == pytest.approx(data["saved_usd"])
    # The old duplicate ledger_saved_usd/ledger_saved_pct keys are gone.
    assert "ledger_saved_usd" not in data
    assert "ledger_saved_pct" not in data
    assert "ops_saved_usd" in data


def test_savings_summary_empty_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))

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
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.infra.runtime.run_ledger import RunLedger

    recorded: list[dict[str, object]] = []

    class Recorder:
        def record(self, **kwargs: object) -> None:
            recorded.append(kwargs)

        def record_compact_tool_output(self, **kwargs: object) -> None:
            raise AssertionError("regular tool savings should use record()")

    monkeypatch.setattr(mcp_server, "_get_context_budget_recorder", lambda: Recorder())
    monkeypatch.setattr(mcp_server, "_record_smart_state_savings", lambda **_kwargs: None)

    ledger = RunLedger(session_id="run-no-double-count", agent="codex")
    ledger.record("tool_result", "search result", {"tool": "search"})

    mcp_server._record_context_budget_for_tool(
        "search",
        {"query": "needle"},
        ledger,
        {"tokens_saved": 100, "total_tokens": 25},
    )

    assert len(recorded) == 1
    lever_savings = recorded[0]["lever_savings"]
    assert lever_savings == {"search_read": 100, "tool:search": 0}
    assert sum(value for value in lever_savings.values() if value > 0) == 100


# NOTE: tests that asserted _record_context_budget_for_tool wrote events to
# live_savings_events.jsonl were removed when the savings mechanism moved
# into the MCP tool response (content[].saved). The transcript walker in
# savings_summary.py is now the single source of truth.
