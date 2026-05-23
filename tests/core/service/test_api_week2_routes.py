"""Tests for the week-2 FastAPI routes (spec 06 — web wiring).

Tests cover: happy path + 404 for all 9 new endpoints.

Architecture: create_app() with a custom store_root pointing to a
tmp_path fixture so every test is fully isolated with no real filesystem reads.
"""

from __future__ import annotations

import os
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from atelier.core.foundation.models import RawArtifact, Trace
from atelier.core.foundation.store import ContextStore

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


def _write_trace(
    root: Path,
    session_id: str,
    *,
    model: str = "claude-sonnet-4-5",
    input_tokens: int = 1500,
    output_tokens: int = 250,
) -> None:
    store = ContextStore(root)
    store.init()
    trace = Trace(
        id=f"trace-{session_id}",
        session_id=session_id,
        agent="copilot",
        domain="ui",
        task="Session UI audit",
        status="success",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    store.record_trace(trace, write_json=False)


def _write_imported_trace(
    root: Path,
    session_id: str,
    *,
    host: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    raw_artifact_ids: list[str] | None = None,
) -> None:
    store = ContextStore(root)
    store.init()
    trace = Trace(
        id=f"{host}-{session_id}",
        session_id=session_id,
        agent="atelier:code",
        host=host,
        domain="coding",
        task=f"{host} imported session",
        status="success",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        raw_artifact_ids=raw_artifact_ids or [],
    )
    store.record_trace(trace, write_json=False)


def _write_raw_artifact(
    root: Path,
    *,
    artifact_id: str,
    session_id: str,
    content: str,
    source: str = "cursor",
    relative_path: str | None = None,
) -> None:
    store = ContextStore(root)
    store.init()
    artifact = RawArtifact(
        id=artifact_id,
        source=source,
        source_session_id=session_id,
        kind="session_log",
        relative_path=relative_path or f"{session_id}.jsonl",
        content_path=f"raw/{artifact_id}.jsonl",
        sha256_original="orig",
        sha256_redacted="redacted",
        byte_count_original=len(content.encode("utf-8")),
        byte_count_redacted=len(content.encode("utf-8")),
    )
    store.record_raw_artifact(artifact, content)


def _build_imported_host_fixture(host: str) -> tuple[str, dict[str, int | str]]:
    if host == "claude":
        return (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": "2026-05-16T00:00:05Z",
                            "message": {
                                "id": "msg-claude-1",
                                "model": "claude-sonnet-4-6",
                                "usage": {
                                    "input_tokens": 100,
                                    "output_tokens": 50,
                                    "cache_read_input_tokens": 20,
                                    "cache_creation_input_tokens": 10,
                                },
                                "content": [
                                    {"type": "text", "text": "Inspecting the session."},
                                    {
                                        "type": "tool_use",
                                        "name": "Bash",
                                        "input": {"command": "ls"},
                                    },
                                ],
                            },
                        }
                    )
                ]
            ),
            {
                "model": "claude-sonnet-4-6",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_input_tokens": 20,
                "cache_creation_input_tokens": 10,
            },
        )
    if host == "codex":
        return (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "timestamp": "2026-05-16T00:00:00Z",
                            "payload": {"id": "codex-session"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn_context",
                            "timestamp": "2026-05-16T00:00:01Z",
                            "payload": {"model": "gpt-5-mini"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "timestamp": "2026-05-16T00:00:02Z",
                            "payload": {
                                "type": "function_call",
                                "name": "exec_command",
                                "arguments": '{"cmd": "pytest -q"}',
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "timestamp": "2026-05-16T00:00:03Z",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 200,
                                        "output_tokens": 60,
                                    },
                                    "total_token_usage": {
                                        "input_tokens": 300,
                                        "output_tokens": 90,
                                        "cached_input_tokens": 40,
                                    },
                                },
                            },
                        }
                    ),
                ]
            ),
            {
                "model": "gpt-5-mini",
                "input_tokens": 260,
                "output_tokens": 90,
                "cached_input_tokens": 40,
                "cache_creation_input_tokens": 0,
            },
        )
    if host == "copilot":
        return (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "timestamp": "2026-05-16T00:00:01Z",
                            "data": {
                                "model": "copilot-gpt-4",
                                "outputTokens": 80,
                                "toolRequests": [{"toolCallId": "tc1", "name": "edit"}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "session.shutdown",
                            "timestamp": "2026-05-16T00:00:04Z",
                            "data": {
                                "modelMetrics": {
                                    "copilot-gpt-4": {
                                        "usage": {
                                            "inputTokens": 300,
                                            "outputTokens": 110,
                                            "cacheReadTokens": 40,
                                            "cacheWriteTokens": 15,
                                            "reasoningTokens": 10,
                                        }
                                    }
                                }
                            },
                        }
                    ),
                ]
            ),
            {
                "model": "copilot-gpt-4",
                "input_tokens": 300,
                "output_tokens": 110,
                "cached_input_tokens": 40,
                "cache_creation_input_tokens": 15,
            },
        )
    if host == "gemini":
        return (
            "\n".join(
                [
                    json.dumps(
                        {
                            "id": "gemini-msg-1",
                            "type": "gemini",
                            "timestamp": "2026-05-16T00:00:02Z",
                            "model": "gemini-2.5-pro",
                            "tokens": {"input": 120, "output": 35, "thoughts": 8, "cached": 25},
                            "content": "Applied the requested edit.",
                            "toolCalls": [
                                {
                                    "name": "write_file",
                                    "args": {
                                        "path": "frontend/src/pages/Sessions.tsx",
                                        "content": "updated",
                                    },
                                }
                            ],
                        }
                    )
                ]
            ),
            {
                "model": "gemini-2.5-pro",
                "input_tokens": 120,
                "output_tokens": 35,
                "cached_input_tokens": 25,
                "cache_creation_input_tokens": 0,
            },
        )
    if host == "opencode":
        return (
            "\n".join(
                [
                    json.dumps(
                        {
                            "_type": "part",
                            "role": "assistant",
                            "timestamp": 1778891735304,
                            "data": {
                                "type": "tool",
                                "tool": "bash",
                                "state": {"input": {"command": "pytest -q"}},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "_type": "part",
                            "role": "assistant",
                            "timestamp": 1778891735404,
                            "data": {"type": "text", "text": "Validated the change."},
                        }
                    ),
                    json.dumps(
                        {
                            "_type": "message",
                            "role": "assistant",
                            "timestamp": 1778891735454,
                            "data": {
                                "role": "assistant",
                                "providerID": "opencode",
                                "modelID": "big-pickle",
                                "tokens": {
                                    "input": 140,
                                    "output": 30,
                                    "reasoning": 5,
                                    "cache": {"read": 20, "write": 10},
                                },
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "_type": "part",
                            "role": "assistant",
                            "timestamp": 1778891735504,
                            "data": {
                                "type": "step-finish",
                                "tokens": {
                                    "input": 140,
                                    "output": 30,
                                    "reasoning": 5,
                                    "cache": {"read": 20, "write": 10},
                                },
                            },
                        }
                    ),
                ]
            ),
            {
                "model": "opencode/big-pickle",
                "input_tokens": 140,
                "output_tokens": 30,
                "cached_input_tokens": 20,
                "cache_creation_input_tokens": 10,
            },
        )
    raise AssertionError(f"Unsupported host fixture: {host}")


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




def test_reasoning_context_accepts_runtime_bootstrap_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "0")

    from atelier.core.service.api import create_app

    class FakeRuntime:
        def __init__(self, root: Path) -> None:
            self.root = root

        def get_context(self, **_: Any) -> dict[str, Any]:
            return {
                "context": "Use the bootstrap map first.",
                "tokens_used": 7,
                "bootstrap": {"status": "cold", "repo_hash": "abc123", "blocks": []},
            }

    monkeypatch.setattr("atelier.gateway.adapters.runtime.ContextRuntime", FakeRuntime)
    client = TestClient(create_app(store_root=str(tmp_path)), raise_server_exceptions=True)

    resp = client.post("/v1/reasoning/context", json={"task": "review edit"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["context"] == "Use the bootstrap map first."
    assert payload["bootstrap"] == {"status": "cold", "repo_hash": "abc123", "blocks": []}

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
        assert "started_model" in item
        assert "cost_status" in item

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

    @pytest.mark.parametrize("host", ["claude", "codex", "copilot", "gemini", "opencode"])
    def test_imported_sessions_prefer_raw_artifact_resumming(
        self,
        host: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session_id = f"{host}-session"
        artifact_id = f"{host}-artifact"
        content, expected = _build_imported_host_fixture(host)
        _write_raw_artifact(
            tmp_path,
            artifact_id=artifact_id,
            session_id=session_id,
            source=host,
            content=content,
        )
        _write_imported_trace(
            tmp_path,
            session_id,
            host=host,
            model=str(expected["model"]),
            input_tokens=int(expected["input_tokens"]) + 999,
            output_tokens=int(expected["output_tokens"]) + 111,
            cached_input_tokens=int(expected["cached_input_tokens"]) + 77,
            cache_creation_input_tokens=int(expected["cache_creation_input_tokens"]) + 55,
            raw_artifact_ids=[artifact_id],
        )

        monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
        monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from atelier.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        listing = client.get("/v1/sessions")
        assert listing.status_code == 200
        item = next(row for row in listing.json() if row["session_id"] == session_id)

        assert item["started_model"] == expected["model"]
        assert item["input_tokens"] == expected["input_tokens"]
        assert item["output_tokens"] == expected["output_tokens"]
        assert item["cached_input_tokens"] == expected["cached_input_tokens"]
        assert item["cache_write_tokens"] == expected["cache_creation_input_tokens"]

        detail = client.get(f"/v1/sessions/{session_id}")
        assert detail.status_code == 200
        data = detail.json()
        assert data["started_model"] == expected["model"]
        assert data["input_tokens"] == expected["input_tokens"]
        assert data["output_tokens"] == expected["output_tokens"]
        assert data["cached_input_tokens"] == expected["cached_input_tokens"]
        assert data["cache_write_tokens"] == expected["cache_creation_input_tokens"]

    def test_running_sessions_sort_by_file_mtime_before_started_at(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)

        def write_run(session_id: str, started: str, mtime: float) -> None:
            runs_dir = tmp_path / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            snap = _run_snapshot(session_id)
            snap["status"] = "running"
            snap["created_at"] = started
            snap["updated_at"] = started
            path = runs_dir / f"{session_id}.json"
            path.write_text(json.dumps(snap))
            os.utime(path, (mtime, mtime))

        write_run(
            "sess-old",
            (now - timedelta(hours=2)).isoformat(),
            (now - timedelta(hours=1, minutes=30)).timestamp(),
        )
        write_run(
            "sess-new",
            (now - timedelta(hours=3)).isoformat(),
            (now - timedelta(minutes=30)).timestamp(),
        )

        monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
        monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from atelier.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        listing = client.get("/v1/sessions")
        assert listing.status_code == 200
        data = listing.json()
        assert [item["session_id"] for item in data[:2]] == ["sess-new", "sess-old"]
        assert data[0]["updated_at"] > data[1]["updated_at"]


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

    def test_estimates_cost_from_trace_when_ledger_cost_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_run(tmp_path, "sess-est", cost=0.0)
        _write_trace(tmp_path, "sess-est", model="claude-sonnet-4-5")

        monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
        monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from atelier.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        data = client.get("/v1/sessions/sess-est").json()

        assert data["started_model"] == "claude-sonnet-4-5"
        assert data["cost_status"] == "estimated"
        assert data["total_cost_usd"] > 0

    def test_imported_session_uses_raw_artifact_usage_when_trace_model_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_id = "sess-imported"
        artifact_id = "artifact-imported"
        content = "\n".join(
            [
                json.dumps(
                    {
                        "type": "message",
                        "timestamp": "2026-05-16T00:00:00Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "Check import pricing"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "timestamp": "2026-05-16T00:00:05Z",
                        "message": {
                            "role": "assistant",
                            "model": "claude-sonnet-4-6",
                            "usage": {"input": 1200, "output": 300, "cacheRead": 200},
                            "content": [{"type": "text", "text": "Done"}],
                        },
                    }
                ),
            ]
        )
        _write_raw_artifact(
            tmp_path,
            artifact_id=artifact_id,
            session_id=session_id,
            content=content,
        )
        _write_trace(
            tmp_path,
            session_id,
            model="",
            input_tokens=0,
            output_tokens=0,
        )
        store = ContextStore(tmp_path)
        trace = store.get_trace(f"trace-{session_id}")
        assert trace is not None
        trace.raw_artifact_ids = [artifact_id]
        store.record_trace(trace, write_json=False)

        monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
        monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from atelier.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        data = client.get(f"/v1/sessions/{session_id}").json()

        assert data["started_model"] == "claude-sonnet-4-6"
        assert data["cost_status"] == "estimated"
        assert data["total_cost_usd"] > 0
        assert data["models_used"] == {"claude-sonnet-4-6": 1}


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

    def test_400_encoded_path_traversal(self, client: TestClient) -> None:
        resp = client.get("/v1/reports/2026-W20%2F..%2Fsecrets")
        assert resp.status_code in (400, 404, 422)
