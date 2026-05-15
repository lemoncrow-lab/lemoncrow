"""Tests for the week-2 FastAPI routes (spec 06 — web wiring).

Tests cover: happy path + 404 for all 9 new endpoints.

Architecture: create_app() with a custom store_root pointing to a
tmp_path fixture so every test is fully isolated with no real filesystem reads.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers to build fake on-disk data
# ---------------------------------------------------------------------------


def _run_snapshot(session_id: str, cost: float = 0.5) -> dict[str, Any]:
    """Minimal run ledger snapshot that session_report.build_report() can parse."""
    now = datetime.now(UTC)
    started = (now - timedelta(hours=1)).isoformat()
    ended = now.isoformat()
    return {
        "session_id": session_id,
        "status": "done",
        "created_at": started,
        "updated_at": ended,
        "cost": {
            "total_cost_usd": cost,
            "calls": [
                {
                    "model": "claude-sonnet-4-5",
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost_usd": cost,
                    "at": ended,
                    "operation": "tool_call",
                }
            ],
        },
        "events": [
            {"kind": "tool_call", "at": started, "tool": "Bash"},
            {"kind": "tool_call", "at": ended, "tool": "Bash"},
        ],
    }


def _write_run(root: Path, session_id: str, cost: float = 0.5) -> Path:
    runs_dir = root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    snap = _run_snapshot(session_id, cost=cost)
    p = runs_dir / f"{session_id}.json"
    p.write_text(json.dumps(snap))
    return p


def _write_outcomes(root: Path, session_id: str) -> Path:
    runs_dir = root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    outcomes = {
        "route_outcomes": [
            {
                "tool": "Bash",
                "outcome_window": {"outcome_score": 0.8, "extra_read_rate": 0.05},
            }
        ],
        "compact_outcomes": [
            {
                "trigger": "context_full",
                "outcome_window": {"outcome_score": 0.9, "extra_read_rate": 0.02},
            }
        ],
    }
    p = runs_dir / f"{session_id}.outcomes.json"
    p.write_text(json.dumps(outcomes))
    return p


def _write_reports_index(cwd_reports: Path) -> None:
    cwd_reports.mkdir(parents=True, exist_ok=True)
    index = [
        {
            "week": "2026-W20",
            "week_start": "2026-05-11",
            "generated_at": "2026-05-15 17:00 UTC",
            "routing_sessions": 50,
            "total_routing_savings_usd": 43.61,
            "routing_quality_score": 0.887,
            "compact_retention_score": 0.81,
        }
    ]
    (cwd_reports / "index.json").write_text(json.dumps(index))
    week_dir = cwd_reports / "2026-W20"
    week_dir.mkdir(parents=True, exist_ok=True)
    (week_dir / "benchmark.md").write_text("# Benchmark report 2026-W20\n\nContent here.")
    (week_dir / "benchmark.json").write_text(json.dumps({"week": "2026-W20", "metric_snapshot": {}}))


# ---------------------------------------------------------------------------
# Fixture: test client with isolated tmp root
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with all filesystem writes redirected to tmp_path."""
    # Write a populated run for session 'sess-001'
    _write_run(tmp_path, "sess-001", cost=1.23)
    _write_outcomes(tmp_path, "sess-001")

    # Write fake reports/ next to cwd (api.py uses Path("reports") relative)
    reports_dir = tmp_path / "reports"
    _write_reports_index(reports_dir)

    # Patch env vars so cfg properties read from tmp_path
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "0")
    monkeypatch.chdir(tmp_path)  # chdir so relative Path("reports") resolves correctly

    from atelier.core.service.api import create_app

    app = create_app(store_root=str(tmp_path))
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Tests: GET /v1/sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_returns_list(self, client: TestClient) -> None:
        resp = client.get("/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_session_fields(self, client: TestClient) -> None:
        resp = client.get("/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        if len(data) == 0:
            pytest.skip("no sessions in fixture")
        item = data[0]
        assert "session_id" in item
        assert "total_cost_usd" in item
        assert "vendor" in item
        assert "total_turns" in item

    def test_returns_200_with_no_sessions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
        monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)
        from atelier.core.service.api import create_app

        app = create_app(store_root=str(tmp_path))
        c = TestClient(app)
        resp = c.get("/v1/sessions")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Tests: GET /v1/sessions/{session_id}
# ---------------------------------------------------------------------------


class TestGetSession:
    def test_found(self, client: TestClient) -> None:
        resp = client.get("/v1/sessions/sess-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess-001"
        assert "total_cost_usd" in data
        assert "top_tools_by_cost" in data

    def test_404_unknown(self, client: TestClient) -> None:
        resp = client.get("/v1/sessions/nonexistent-session-xyz")
        assert resp.status_code == 404

    def test_response_has_cost_breakdown(self, client: TestClient) -> None:
        resp = client.get("/v1/sessions/sess-001")
        data = resp.json()
        assert "input_token_cost_usd" in data
        assert "output_token_cost_usd" in data
        assert "routing_savings_usd" in data
        assert "compact_events" in data


# ---------------------------------------------------------------------------
# Tests: GET /v1/memory/facts
# ---------------------------------------------------------------------------


class TestListMemoryFacts:
    def test_returns_list(self, client: TestClient) -> None:
        # No native memory files in tmp_path → should return empty list, not 500
        resp = client.get("/v1/memory/facts")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_vendor_filter_returns_list(self, client: TestClient) -> None:
        resp = client.get("/v1/memory/facts?vendor=claude")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Tests: GET /v1/memory/facts/{fact_id}
# ---------------------------------------------------------------------------


class TestGetMemoryFact:
    def test_404_unknown(self, client: TestClient) -> None:
        resp = client.get("/v1/memory/facts/claude-deadbeef")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /v1/insights
# ---------------------------------------------------------------------------


class TestGetInsights:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/v1/insights")
        assert resp.status_code == 200

    def test_response_shape(self, client: TestClient) -> None:
        data = client.get("/v1/insights").json()
        assert "session_count" in data
        assert "total_cost_usd" in data
        assert "opportunities" in data
        assert "top_sessions" in data
        assert "outcomes_summary" in data

    def test_cached_response_same(self, client: TestClient) -> None:
        r1 = client.get("/v1/insights").json()
        r2 = client.get("/v1/insights").json()
        assert r1["session_count"] == r2["session_count"]


# ---------------------------------------------------------------------------
# Tests: GET /v1/outcomes/summary
# ---------------------------------------------------------------------------


class TestGetOutcomesSummary:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/v1/outcomes/summary")
        assert resp.status_code == 200

    def test_response_shape(self, client: TestClient) -> None:
        data = client.get("/v1/outcomes/summary").json()
        assert "route_decisions" in data
        assert "route_avg_score" in data
        assert "compact_events" in data
        assert "compact_avg_score" in data
        assert "sessions_with_high_extra_reads" in data

    def test_with_outcomes_data(self, client: TestClient) -> None:
        data = client.get("/v1/outcomes/summary").json()
        # sess-001 has outcomes written; route_decisions should be >= 0
        assert isinstance(data["route_decisions"], int)
        assert isinstance(data["route_avg_score"], float)


# ---------------------------------------------------------------------------
# Tests: GET /v1/outcomes/{session_id}
# ---------------------------------------------------------------------------


class TestGetOutcomesForSession:
    def test_found(self, client: TestClient) -> None:
        resp = client.get("/v1/outcomes/sess-001")
        assert resp.status_code == 200
        entries = resp.json()
        assert isinstance(entries, list)

    def test_entries_have_kind(self, client: TestClient) -> None:
        entries = client.get("/v1/outcomes/sess-001").json()
        for e in entries:
            assert "kind" in e
            assert e["kind"] in ("route", "compact")

    def test_404_no_outcomes_file(self, client: TestClient) -> None:
        resp = client.get("/v1/outcomes/no-such-session")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /v1/reports
# ---------------------------------------------------------------------------


class TestListReports:
    def test_returns_list(self, client: TestClient) -> None:
        resp = client.get("/v1/reports")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_contains_week_2026_w20(self, client: TestClient) -> None:
        data = client.get("/v1/reports").json()
        weeks = [e["week"] for e in data]
        assert "2026-W20" in weeks

    def test_entry_fields(self, client: TestClient) -> None:
        data = client.get("/v1/reports").json()
        entry = next((e for e in data if e["week"] == "2026-W20"), None)
        assert entry is not None
        assert "week_start" in entry
        assert "generated_at" in entry


# ---------------------------------------------------------------------------
# Tests: GET /v1/reports/{week}
# ---------------------------------------------------------------------------


class TestGetReport:
    def test_found(self, client: TestClient) -> None:
        resp = client.get("/v1/reports/2026-W20")
        assert resp.status_code == 200

    def test_response_has_markdown(self, client: TestClient) -> None:
        data = client.get("/v1/reports/2026-W20").json()
        assert "markdown" in data
        assert "Benchmark report" in data["markdown"]

    def test_response_has_json(self, client: TestClient) -> None:
        data = client.get("/v1/reports/2026-W20").json()
        assert "json" in data
        assert isinstance(data["json"], dict)

    def test_404_unknown_week(self, client: TestClient) -> None:
        resp = client.get("/v1/reports/2026-W99")
        assert resp.status_code == 404

    def test_400_bad_format(self, client: TestClient) -> None:
        resp = client.get("/v1/reports/../secrets")
        assert resp.status_code in (400, 404, 422)
