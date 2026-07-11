"""Tests for the week-2 FastAPI routes (spec 06 — web wiring).

Tests cover: happy path + 404 for all 9 new endpoints.

Architecture: create_app() with a custom store_root pointing to a
tmp_path fixture so every test is fully isolated with no real filesystem reads.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lemoncrow.core.foundation.models import RawArtifact, Trace
from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.core.service import api as service_api

# ---------------------------------------------------------------------------
# Helpers to build fake on-disk data
# ---------------------------------------------------------------------------


def test_reasoning_output_tokens_are_non_additive_for_totals_and_cost() -> None:
    usage = {
        "model": "gpt-5-mini",
        "input_tokens": 100,
        "output_tokens": 40,
        "reasoning_output_tokens": 12,
        "thinking_tokens": 0,
        "cached_input_tokens": 20,
        "cache_creation_input_tokens": 0,
    }
    without_reasoning = {**usage, "reasoning_output_tokens": 0}

    assert service_api._usage_total_tokens(usage) == 160
    assert service_api._model_usage_cost(usage) == service_api._model_usage_cost(without_reasoning)


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
    runs_dir = root / "sessions" / session_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    snap = _run_snapshot(session_id, cost=cost)
    p = runs_dir / "run.json"
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
    workspace_path: str | None = None,
) -> None:
    store = ContextStore(root)
    store.init()
    trace = Trace(
        id=f"{host}-{session_id}",
        session_id=session_id,
        agent="lemon:code",
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
        workspace_path=workspace_path,
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
    created_at: datetime | None = None,
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
        **({"created_at": created_at} if created_at is not None else {}),
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
    runs_dir = root / "sessions" / session_id
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
    p = runs_dir / "outcomes.json"
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
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
    monkeypatch.chdir(tmp_path)  # chdir so relative Path("reports") resolves correctly

    from lemoncrow.core.service.api import create_app

    app = create_app(store_root=str(tmp_path))
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Tests: GET /v1/sessions
# ---------------------------------------------------------------------------


def test_reasoning_context_accepts_runtime_bootstrap_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")

    from lemoncrow.core.service.api import create_app

    class FakeRuntime:
        def __init__(self, root: Path) -> None:
            self.root = root

        def get_context(self, **_: Any) -> dict[str, Any]:
            return {
                "context": "Use the bootstrap map first.",
                "tokens_used": 7,
                "bootstrap": {"status": "cold", "repo_hash": "abc123", "blocks": []},
            }

    monkeypatch.setattr("lemoncrow.gateway.adapters.runtime.ContextRuntime", FakeRuntime)
    client = TestClient(create_app(store_root=str(tmp_path)), raise_server_exceptions=True)

    resp = client.post("/v1/reasoning/context", json={"task": "review edit"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["context"] == "Use the bootstrap map first."
    assert payload["bootstrap"] == {"status": "cold", "repo_hash": "abc123", "blocks": []}


class TestListHosts:
    """GET /hosts — per-host last_import_at + imported_session_count (task 1)."""

    def test_host_with_no_data_has_null_import_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        resp = client.get("/hosts")
        assert resp.status_code == 200
        hosts = {h["host_id"]: h for h in resp.json()}
        assert "claude" in hosts
        assert hosts["claude"]["status"] == "not_detected"
        assert hosts["claude"]["last_import_at"] is None
        assert hosts["claude"]["imported_session_count"] == 0

    def test_last_import_at_is_max_raw_artifact_created_at_not_session_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """last_import_at must reflect RawArtifact.created_at (the real
        wall-clock import time) rather than Trace.created_at (seeded from the
        session's own first-message timestamp, i.e. session start) — use two
        artifacts with an older and a newer import time and a session-start
        timestamp that sits *between* them, so the two fields can't agree by
        coincidence.
        """
        older_import = datetime(2020, 1, 1, tzinfo=UTC)
        newer_import = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        session_started_at = datetime(2023, 1, 1, tzinfo=UTC)

        _write_raw_artifact(
            tmp_path,
            artifact_id="claude-sess-a-art",
            session_id="sess-a",
            source="claude",
            content="content-a",
            created_at=older_import,
        )
        _write_raw_artifact(
            tmp_path,
            artifact_id="claude-sess-b-art",
            session_id="sess-b",
            source="claude",
            content="content-b",
            created_at=newer_import,
        )
        precheck_store = ContextStore(tmp_path)
        precheck_store.init()
        for session_id in ("sess-a", "sess-b"):
            assert precheck_store.get_trace(f"claude-{session_id}") is None  # not yet written below
        _write_imported_trace(
            tmp_path,
            "sess-a",
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
            raw_artifact_ids=["claude-sess-a-art"],
        )
        _write_imported_trace(
            tmp_path,
            "sess-b",
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
            raw_artifact_ids=["claude-sess-b-art"],
        )
        # Force both traces' created_at (session start) to a value strictly
        # between the two artifact import times, so if the endpoint were
        # using Trace.created_at instead of RawArtifact.created_at the
        # returned last_import_at would land at session_started_at (wrong)
        # rather than newer_import (right).
        store = ContextStore(tmp_path)
        store.init()
        for session_id in ("sess-a", "sess-b"):
            trace = store.get_trace(f"claude-{session_id}")
            assert trace is not None
            trace.created_at = session_started_at
            store.record_trace(trace, write_json=False)

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        resp = client.get("/hosts")
        assert resp.status_code == 200
        hosts = {h["host_id"]: h for h in resp.json()}
        claude = hosts["claude"]
        assert claude["status"] == "active"
        assert claude["imported_session_count"] == 2
        assert claude["last_import_at"] is not None
        returned = datetime.fromisoformat(str(claude["last_import_at"]).replace("Z", "+00:00"))
        assert returned == newer_import
        assert returned != session_started_at


class TestListTracesWorkspaceFilter:
    """GET /traces?workspace=... — server-side workspace filter (task 2)."""

    def test_workspace_filter_matches_only_that_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_imported_trace(
            tmp_path,
            "sess-ws-a",
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
            workspace_path="/home/user/project-a",
        )
        _write_imported_trace(
            tmp_path,
            "sess-ws-b",
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
            workspace_path="/home/user/project-b",
        )

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))

        resp = client.get("/traces", params={"workspace": "/home/user/project-a"})
        assert resp.status_code == 200
        body = resp.json()
        session_ids = {item["session_id"] for item in body["items"]}
        assert session_ids == {"sess-ws-a"}

    def test_workspace_facet_lists_full_history_not_just_loaded_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_imported_trace(
            tmp_path,
            "sess-ws-a",
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
            workspace_path="/home/user/project-a",
        )
        _write_imported_trace(
            tmp_path,
            "sess-ws-b",
            host="codex",
            model="gpt-5-mini",
            input_tokens=10,
            output_tokens=5,
            workspace_path="/home/user/project-b",
        )

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))

        # limit=1 caps the *items* page but the workspaces facet must still
        # reflect every distinct workspace_path in the store, not just the
        # one session that made it into this page.
        resp = client.get("/traces", params={"limit": 1})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert set(body["metrics"]["workspaces"]) == {
            "/home/user/project-a",
            "/home/user/project-b",
        }

    def test_workspace_filter_composes_with_host_filter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # host=claude alone would return both claude sessions below; and
        # workspace=/home/user/shared alone would return the claude AND
        # codex sessions sharing that workspace -- only the combination of
        # both filters narrows to exactly sess-ws-claude-shared.
        _write_imported_trace(
            tmp_path,
            "sess-ws-claude-shared",
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
            workspace_path="/home/user/shared",
        )
        _write_imported_trace(
            tmp_path,
            "sess-ws-claude-other",
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
            workspace_path="/home/user/other",
        )
        _write_imported_trace(
            tmp_path,
            "sess-ws-codex-shared",
            host="codex",
            model="gpt-5-mini",
            input_tokens=10,
            output_tokens=5,
            workspace_path="/home/user/shared",
        )

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))

        resp = client.get("/traces", params={"workspace": "/home/user/shared", "host": "claude"})
        assert resp.status_code == 200
        session_ids = {item["session_id"] for item in resp.json()["items"]}
        assert session_ids == {"sess-ws-claude-shared"}


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
        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)
        from lemoncrow.core.service.api import create_app

        app = create_app(store_root=str(tmp_path))
        c = TestClient(app)
        resp = c.get("/v1/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.parametrize("host", ["claude", "codex", "copilot", "opencode"])
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

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

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
            runs_dir = tmp_path / "sessions" / session_id
            runs_dir.mkdir(parents=True, exist_ok=True)
            snap = _run_snapshot(session_id)
            snap["status"] = "running"
            snap["created_at"] = started
            snap["updated_at"] = started
            path = runs_dir / "run.json"
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

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        listing = client.get("/v1/sessions")
        assert listing.status_code == 200
        data = listing.json()
        assert [item["session_id"] for item in data[:2]] == ["sess-new", "sess-old"]
        assert data[0]["updated_at"] > data[1]["updated_at"]

    def test_in_progress_session_sorts_above_older_completed_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for finding 5: sort must use effective last-activity
        (ended_at or updated_at or started_at), not a 3-tuple that maps a
        running session's missing ended_at to 0.0 and buries it below any
        completed session regardless of how recently it was active.
        """
        now = datetime.now(UTC)

        def write_run(session_id: str, *, status: str, business_at: str, mtime: float) -> None:
            runs_dir = tmp_path / "sessions" / session_id
            runs_dir.mkdir(parents=True, exist_ok=True)
            snap = _run_snapshot(session_id)
            snap["status"] = status
            snap["created_at"] = business_at
            snap["updated_at"] = business_at
            path = runs_dir / "run.json"
            path.write_text(json.dumps(snap))
            os.utime(path, (mtime, mtime))

        # Completed a full day ago (ended_at set, far in the past).
        write_run(
            "sess-completed",
            status="done",
            business_at=(now - timedelta(days=1)).isoformat(),
            mtime=(now - timedelta(days=1)).timestamp(),
        )
        # Still running; last activity was seconds ago (ended_at is None).
        write_run(
            "sess-running",
            status="running",
            business_at=(now - timedelta(minutes=5)).isoformat(),
            mtime=(now - timedelta(seconds=5)).timestamp(),
        )

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        listing = client.get("/v1/sessions")
        assert listing.status_code == 200
        data = listing.json()
        session_ids = [item["session_id"] for item in data]
        assert "sess-running" in session_ids
        assert "sess-completed" in session_ids
        assert session_ids.index("sess-running") < session_ids.index("sess-completed")

        running_item = next(item for item in data if item["session_id"] == "sess-running")
        assert running_item["ended_at"] is None

    def test_running_session_started_at_uses_real_created_at_not_evicted_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Root-cause regression: build_report()'s started_at prefers the
        oldest *surviving* ledger event, but RunLedger.events is a bounded,
        evicted list for long sessions — the true first event can be evicted,
        drifting started_at forward until it looks like processing time
        instead of the session's real start. The ledger's own created_at is
        recorded once at session start and is never evicted, so /v1/sessions
        must prefer it whenever it is earlier than the reconstructed
        started_at. A running session must also still surface ended_at: null
        and sort above an older, fully completed session.
        """
        now = datetime.now(UTC)
        real_start = now - timedelta(hours=2)

        def write_run(session_id: str, *, status: str, created_at: datetime, event_at: datetime, mtime: float) -> None:
            runs_dir = tmp_path / "sessions" / session_id
            runs_dir.mkdir(parents=True, exist_ok=True)
            snap = _run_snapshot(session_id)
            snap["status"] = status
            snap["created_at"] = created_at.isoformat()
            snap["updated_at"] = event_at.isoformat()
            # Simulate a bounded/evicted events list: only a recent event
            # survived, well after the ledger's real created_at.
            snap["events"] = [{"kind": "tool_call", "at": event_at.isoformat(), "tool": "Bash"}]
            path = runs_dir / "run.json"
            path.write_text(json.dumps(snap))
            os.utime(path, (mtime, mtime))

        # Older, fully completed session.
        write_run(
            "sess-completed-old",
            status="done",
            created_at=now - timedelta(days=1),
            event_at=now - timedelta(days=1) + timedelta(minutes=5),
            mtime=(now - timedelta(days=1)).timestamp(),
        )
        # Running session: real start 2h ago, but only a recent surviving
        # event remains in the bounded ledger events list.
        write_run(
            "sess-running",
            status="running",
            created_at=real_start,
            event_at=now - timedelta(minutes=1),
            mtime=now.timestamp(),
        )

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        listing = client.get("/v1/sessions")
        assert listing.status_code == 200
        data = listing.json()

        session_ids = [item["session_id"] for item in data]
        assert session_ids.index("sess-running") < session_ids.index("sess-completed-old")

        running_item = next(item for item in data if item["session_id"] == "sess-running")
        assert running_item["ended_at"] is None
        assert datetime.fromisoformat(running_item["started_at"]) == real_start

    def test_completed_session_keeps_real_ended_at_after_started_at(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A completed session must never invert started_at/ended_at: ended_at
        must reflect the real last activity and stay after started_at."""
        now = datetime.now(UTC)
        started = now - timedelta(hours=1)
        ended = now - timedelta(minutes=10)

        runs_dir = tmp_path / "sessions" / "sess-done"
        runs_dir.mkdir(parents=True, exist_ok=True)
        snap = _run_snapshot("sess-done")
        snap["status"] = "done"
        snap["created_at"] = started.isoformat()
        snap["updated_at"] = ended.isoformat()
        snap["events"] = [
            {"kind": "tool_call", "at": started.isoformat(), "tool": "Bash"},
            {"kind": "tool_call", "at": ended.isoformat(), "tool": "Bash"},
        ]
        (runs_dir / "run.json").write_text(json.dumps(snap))

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        listing = client.get("/v1/sessions")
        assert listing.status_code == 200
        item = next(row for row in listing.json() if row["session_id"] == "sess-done")

        assert item["ended_at"] is not None
        started_dt = datetime.fromisoformat(item["started_at"])
        ended_dt = datetime.fromisoformat(item["ended_at"])
        assert ended_dt > started_dt

    def test_imported_session_ended_at_reflects_real_last_turn_not_import_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Root-cause regression: _build_imported_session_payload used to seed
        both started_at and ended_at from trace.created_at (import/ingest
        time) and only ever *raise* ended_at from there — for a session
        imported after it finished (trace.created_at is always >= every real
        turn), ended_at stayed pinned at import time forever instead of the
        real last turn. The fixture's turn is dated 2026-05-16; trace.created_at
        defaults to "now" (test run time), which is always later —
        reproducing the real-world import-after-the-fact case.
        """
        session_id = "claude-import-ts"
        artifact_id = "artifact-import-ts"
        content, _ = _build_imported_host_fixture("claude")
        _write_raw_artifact(
            tmp_path,
            artifact_id=artifact_id,
            session_id=session_id,
            source="claude",
            content=content,
        )
        _write_imported_trace(
            tmp_path,
            session_id,
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            raw_artifact_ids=[artifact_id],
        )

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        listing = client.get("/v1/sessions")
        assert listing.status_code == 200
        item = next(row for row in listing.json() if row["session_id"] == session_id)

        real_turn_at = datetime.fromisoformat("2026-05-16T00:00:05+00:00")
        assert datetime.fromisoformat(item["started_at"]) == real_turn_at
        assert datetime.fromisoformat(item["ended_at"]) == real_turn_at
        # updated_at must track the real last activity, not import time.
        assert datetime.fromisoformat(item["updated_at"]) == real_turn_at

    def test_naive_turn_timestamp_does_not_500(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression for finding 4: an offset-less ISO turn timestamp used to
        parse as a naive datetime and raise (TypeError: can't compare
        offset-naive and offset-aware datetimes) when compared against the
        aware trace.created_at. _parse_session_datetime now normalizes naive
        results to UTC, so both the list and detail endpoints stay 200.
        """
        session_id = "sess-naive-ts"
        artifact_id = "artifact-naive-ts"
        content, _ = _build_imported_host_fixture("claude")
        # Strip the trailing "Z" so the turn's "at" timestamp parses naive.
        assert "2026-05-16T00:00:05Z" in content
        content = content.replace("2026-05-16T00:00:05Z", "2026-05-16T00:00:05")
        _write_raw_artifact(
            tmp_path,
            artifact_id=artifact_id,
            session_id=session_id,
            source="claude",
            content=content,
        )
        _write_imported_trace(
            tmp_path,
            session_id,
            host="claude",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            raw_artifact_ids=[artifact_id],
        )

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))

        listing = client.get("/v1/sessions")
        assert listing.status_code == 200
        assert any(item["session_id"] == session_id for item in listing.json())

        detail = client.get(f"/v1/sessions/{session_id}")
        assert detail.status_code == 200
        assert detail.json()["session_id"] == session_id

    def test_bad_imported_session_is_skipped_not_fatal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression for finding 4: one imported session raising inside
        _build_imported_session_payload must not 500 the whole /v1/sessions
        listing — it should be logged and skipped, leaving other sessions intact.
        """
        for session_id in ("sess-bad", "sess-good"):
            artifact_id = f"artifact-{session_id}"
            content, _ = _build_imported_host_fixture("claude")
            _write_raw_artifact(
                tmp_path,
                artifact_id=artifact_id,
                session_id=session_id,
                source="claude",
                content=content,
            )
            _write_imported_trace(
                tmp_path,
                session_id,
                host="claude",
                model="claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=50,
                raw_artifact_ids=[artifact_id],
            )

        def fake_savings(session_id: str, root: Path) -> float:
            if session_id == "sess-bad":
                raise RuntimeError("boom")
            return 0.0

        monkeypatch.setattr("lemoncrow.infra.runtime.session_report.read_total_savings_from_events", fake_savings)
        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

        client = TestClient(create_app(store_root=str(tmp_path)))
        resp = client.get("/v1/sessions")
        assert resp.status_code == 200
        session_ids = [item["session_id"] for item in resp.json()]
        assert "sess-good" in session_ids
        assert "sess-bad" not in session_ids


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

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

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

        monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
        monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "0")
        monkeypatch.chdir(tmp_path)

        from lemoncrow.core.service.api import create_app

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

    def test_empty_list_when_no_outcomes_file(self, client: TestClient) -> None:
        resp = client.get("/v1/outcomes/no-such-session")
        assert resp.status_code == 200
        assert resp.json() == []


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
