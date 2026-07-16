"""Tests for the LemonCrow production service API (P4).

Uses FastAPI TestClient with an in-memory SQLite store so no server starts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from lemoncrow.core.environment import HIDDEN_LLM_TOOLS
from lemoncrow.core.foundation.paths import resolve_session_state_path
from lemoncrow.core.service.api import create_app
from lemoncrow.infra.storage.bundle import StoreBundle, build_sqlite_store_bundle
from lemoncrow.pro.capabilities.swarm.models import (
    SwarmAcceptedCommit,
    SwarmArtifactRef,
    SwarmChildState,
    SwarmRunState,
    SwarmWaveState,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

FastAPITestClient = pytest.importorskip(
    "fastapi.testclient",
    reason="FastAPI API tests require the api extra",
).TestClient

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def store(tmp_path: Path) -> StoreBundle:
    st = build_sqlite_store_bundle(tmp_path / ".lemoncrow")
    st.init()
    return st


@pytest.fixture()
def app_no_auth(store: StoreBundle, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with auth disabled."""
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    return cast("TestClient", FastAPITestClient(create_app(store_root=store.knowledge.root)))


@pytest.fixture()
def app_with_auth(store: StoreBundle, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with auth enabled and known key."""
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "true")
    monkeypatch.setenv("LEMONCROW_API_KEY", "test-secret-key-123")
    return cast("TestClient", FastAPITestClient(create_app(store_root=store.knowledge.root)))


AUTH_HEADERS = {"Authorization": "Bearer test-secret-key-123"}


# --------------------------------------------------------------------------- #
# Health / basic info                                                         #
# --------------------------------------------------------------------------- #


def test_health_returns_ok(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_config_returns_runtime_settings(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["require_auth"] is False
    assert data["lemoncrow_root"]


def test_code_map_endpoints_are_bounded_and_use_the_selected_project(
    app_no_auth: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lemoncrow.core.service import code_map

    project = tmp_path / "repo"
    project.mkdir()
    engine = object()
    monkeypatch.setattr(code_map, "resolve_project_root", lambda value=None: project)
    monkeypatch.setattr(code_map, "get_engine", lambda value: engine)
    monkeypatch.setattr(
        code_map,
        "build_overview",
        lambda selected_engine, root: {
            "project": {"root": str(root), "label": root.name},
            "index": {"symbols_indexed": 3},
            "graph": {"focus": "root", "nodes": [], "edges": [], "truncated": False},
        },
    )
    monkeypatch.setattr(
        code_map,
        "build_full_graph",
        lambda selected_engine, root: {
            "project": {"root": str(root), "label": root.name},
            "total_symbols": 3,
            "total_files": 1,
            "communities": [],
            "file_types": [],
            "languages": [],
            "graph": {"focus": "root", "nodes": [], "edges": [], "truncated": False},
        },
    )
    monkeypatch.setattr(
        code_map,
        "search_symbols",
        lambda selected_engine, query, limit: [{"id": "root", "label": query, "score": 1.0}],
    )
    monkeypatch.setattr(
        code_map,
        "build_neighborhood",
        lambda selected_engine, symbol_id, depth, limit: {
            "focus": symbol_id,
            "nodes": [{"id": symbol_id}],
            "edges": [],
            "depth": depth,
            "truncated": False,
        },
    )

    overview = app_no_auth.get("/v1/code-map/overview", params={"project_root": str(project)})
    full = app_no_auth.get("/v1/code-map/full", params={"project_root": str(project)})
    search = app_no_auth.get(
        "/v1/code-map/search",
        params={"project_root": str(project), "q": "chargeCard", "limit": 999},
    )
    graph = app_no_auth.get(
        "/v1/code-map/neighborhood",
        params={"project_root": str(project), "symbol_id": "root", "depth": 9, "limit": 999},
    )

    assert overview.status_code == 200
    assert overview.json()["project"]["root"] == str(project)
    assert full.status_code == 200
    assert full.json()["total_symbols"] == 3
    assert search.status_code == 200
    assert search.json()["results"][0]["label"] == "chargeCard"
    assert graph.status_code == 200
    assert graph.json()["depth"] == 2


def test_code_map_requires_auth_when_service_auth_is_enabled(
    app_with_auth: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lemoncrow.core.service import code_map

    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setattr(code_map, "resolve_project_root", lambda value=None: project)

    denied = app_with_auth.get("/v1/code-map/overview", params={"project_root": str(project)})

    assert denied.status_code == 401


def test_claude_code_router_endpoint_evaluates_request(
    app_no_auth: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.service.api.evaluate_host_router_request",
        lambda **kwargs: {"bridge_mode": "shadow", "requested_path": kwargs["path"]},
    )

    resp = app_no_auth.post(
        "/v1/router/claude-code/evaluate",
        json={
            "path": "/router-preset/claudecode/auto",
            "model": "claude-sonnet-4.6",
            "messages": [{"role": "user", "content": "Plan the refactor."}],
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "bridge_mode": "shadow",
        "requested_path": "/router-preset/claudecode/auto",
    }


def test_overview_accessible_no_auth(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_traces" in data
    assert "total_blocks" in data


def test_mcp_status_matches_non_dev_tool_visibility(store: StoreBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    app = create_app(store_root=store.knowledge.root)
    route = next(route for route in app.routes if getattr(route, "path", "") == "/mcp/status")

    tools = route.endpoint()

    names = {tool["tool_name"] for tool in tools}
    assert not (names & HIDDEN_LLM_TOOLS)
    assert {tool["tool_name"] for tool in tools if tool["mode"] == "active"} == names
    assert not {tool["tool_name"] for tool in tools if tool["mode"] == "passive"}
    assert "read" in names
    assert "bash" in names
    assert "code_search" in names
    assert "edit" in names
    assert "web_fetch" in names


def test_workflow_current_and_snapshot_actions(store: StoreBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_ROOT", str(store.knowledge.root))
    workspace = store.knowledge.root / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(workspace))
    state_path = resolve_session_state_path(workspace)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "workflow": {
                    "current_step": "review",
                    "session_phase": "review",
                    "current_task": {
                        "workflow_id": "owned-execute-review-loop",
                        "run_id": "wf-123",
                        "step_id": "execute",
                    },
                    "task_outputs": {
                        "plan": {
                            "step_id": "plan",
                            "kind": "agent",
                            "status": "done",
                            "output": "plan ready",
                            "output_json": {},
                            "execution_receipt": {"mode": "native"},
                            "duration_seconds": 1.2,
                            "cost_usd": 0.0,
                            "error": "",
                        }
                    },
                    "plan_review": {
                        "decision": "pending",
                        "paused_step_id": "execute",
                        "workflow_id": "owned-execute-review-loop",
                    },
                },
                "workflow_runtime": {
                    "run_id": "wf-123",
                    "workflow_id": "owned-execute-review-loop",
                    "status": "awaiting_review",
                    "step_order": ["plan", "execute"],
                    "current_step": "execute",
                    "paused_step_id": "execute",
                    "failed_step_id": "",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:05:00Z",
                    "workflow": {
                        "workflow_id": "owned-execute-review-loop",
                        "steps": [
                            {"step_id": "plan", "kind": "agent", "prompt": "Draft"},
                            {"step_id": "execute", "kind": "agent", "prompt": "Apply"},
                        ],
                    },
                    "route": {"mode": "native"},
                    "plan_review": {
                        "decision": "pending",
                        "paused_step_id": "execute",
                        "workflow_id": "owned-execute-review-loop",
                    },
                    "runner": {
                        "run_id": "wf-123",
                        "status": "awaiting_review",
                        "definition_hash": "abc",
                        "step_results": {
                            "plan": {
                                "step_id": "plan",
                                "kind": "agent",
                                "status": "done",
                                "output": "plan ready",
                                "output_json": {},
                                "execution_receipt": {"mode": "native"},
                                "duration_seconds": 1.2,
                                "cost_usd": 0.0,
                                "error": "",
                            }
                        },
                        "step_order": ["plan"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    client = cast("TestClient", FastAPITestClient(create_app(store_root=store.knowledge.root)))

    current = client.get("/v1/workflow/current")
    assert current.status_code == 200
    current_payload = current.json()
    assert current_payload["summary"]["run_id"] == "wf-123"
    assert current_payload["summary"]["status"] == "awaiting_review"
    assert current_payload["available_actions"]["can_resume"] is True
    assert current_payload["control_payloads"]["resume_approve"]["plan_review"]["decision"] == "approve"

    paused = client.post("/v1/workflow/current/pause", json={"reason": "waiting on review"})
    assert paused.status_code == 200
    paused_payload = paused.json()
    assert paused_payload["summary"]["status"] == "paused"
    assert paused_payload["summary"]["pause_reason"] == "waiting on review"

    stopped = client.post("/v1/workflow/current/stop", json={"reason": "user cancelled"})
    assert stopped.status_code == 200
    stopped_payload = stopped.json()
    assert stopped_payload["summary"]["status"] == "stopped"
    assert stopped_payload["summary"]["stop_reason"] == "user cancelled"


def test_hosts_endpoint_lists_supported_integrations(store: StoreBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    app = create_app(store_root=store.knowledge.root)
    route = next(route for route in app.routes if getattr(route, "path", "") == "/hosts")

    hosts = route.endpoint()

    labels = {host["host_id"]: host["label"] for host in hosts}
    assert labels["claude"] == "Claude Code"
    assert labels["codex"] == "Codex CLI"
    assert labels["copilot"] == "Copilot / VS Code"
    assert labels["cursor"] == "Cursor IDE"
    assert labels["hermes"] == "Hermes Agent (global-only)"


# --------------------------------------------------------------------------- #
# Auth enforcement                                                            #
# --------------------------------------------------------------------------- #


def test_auth_required_when_enabled(app_with_auth: TestClient) -> None:
    """Unauthenticated request must be rejected."""
    resp = app_with_auth.post(
        "/v1/reasoning/context",
        json={"task": "deploy the app"},
    )
    assert resp.status_code in (401, 403)


def test_auth_disabled_when_require_auth_false(app_no_auth: TestClient) -> None:
    """With auth disabled any request passes the auth check."""
    resp = app_no_auth.post(
        "/v1/reasoning/context",
        json={"task": "deploy the app"},
    )
    assert resp.status_code == 200


def test_wrong_key_is_rejected(app_with_auth: TestClient) -> None:
    resp = app_with_auth.post(
        "/v1/reasoning/context",
        json={"task": "task"},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 403


def test_correct_key_is_accepted(app_with_auth: TestClient) -> None:
    resp = app_with_auth.post(
        "/v1/reasoning/context",
        json={"task": "task"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Reasoning endpoints                                                         #
# --------------------------------------------------------------------------- #


def test_reasoning_context_returns_string(app_no_auth: TestClient) -> None:
    resp = app_no_auth.post(
        "/v1/reasoning/context",
        json={"task": "add a product to the catalog", "domain": "ecommerce"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "context" in data
    assert isinstance(data["context"], str)


def test_rescue_returns_result(app_no_auth: TestClient) -> None:
    resp = app_no_auth.post(
        "/v1/reasoning/rescue",
        json={"task": "deploy", "error": "connection refused", "domain": "devops"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "rescue" in data
    assert isinstance(data["rescue"], str)


# --------------------------------------------------------------------------- #
# Trace recording                                                             #
# --------------------------------------------------------------------------- #


def test_trace_record_persists(app_no_auth: TestClient, store: StoreBundle) -> None:
    resp = app_no_auth.post(
        "/v1/traces",
        json={
            "agent": "test-agent",
            "domain": "ecommerce",
            "task": "update product prices",
            "status": "success",
            "files_touched": ["products.py"],
            "commands_run": ["pytest"],
            "errors_seen": [],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    trace_id = data["id"]

    # Verify it was stored.
    trace = store.history.get_trace(trace_id)
    assert trace is not None
    assert trace.status == "success"


def test_trace_redacts_secrets(app_no_auth: TestClient, store: StoreBundle) -> None:
    resp = app_no_auth.post(
        "/v1/traces",
        json={
            "agent": "test-agent",
            "domain": "ecommerce",
            "task": "task with api_key=sk-supersecret123456789012",
            "status": "success",
        },
    )
    assert resp.status_code == 200
    trace_id = resp.json()["id"]
    trace = store.history.get_trace(trace_id)
    assert trace is not None
    assert "sk-supersecret" not in trace.task


def test_trace_record_accepts_legacy_run_id(app_no_auth: TestClient, store: StoreBundle) -> None:
    resp = app_no_auth.post(
        "/v1/traces",
        json={
            "agent": "test-agent",
            "domain": "ecommerce",
            "task": "accept legacy run_id",
            "status": "success",
            "run_id": "legacy-run-001",
        },
    )
    assert resp.status_code == 200
    trace_id = resp.json()["id"]

    trace = store.history.get_trace(trace_id)
    assert trace is not None
    assert trace.session_id == "legacy-run-001"


def test_trace_record_normalizes_legacy_strength_confidence(app_no_auth: TestClient, store: StoreBundle) -> None:
    resp = app_no_auth.post(
        "/v1/traces",
        json={
            "agent": "test-agent",
            "domain": "ecommerce",
            "task": "accept legacy confidence strength",
            "status": "success",
            "trace_confidence": "high",
        },
    )
    assert resp.status_code == 200
    trace_id = resp.json()["id"]

    trace = store.history.get_trace(trace_id)
    assert trace is not None
    assert trace.trace_confidence == "manual"


def test_trace_record_accepts_mcp_context_fields_and_learnings(app_no_auth: TestClient, store: StoreBundle) -> None:
    resp = app_no_auth.post(
        "/v1/traces",
        json={
            "agent": "test-agent",
            "domain": "ecommerce",
            "task": "accept mcp-only extras",
            "status": "success",
            "trace_confidence": "high",
            "capture_files": ["src/example.py"],
            "learnings": [
                "Prefer focused regressions.",
                {
                    "kind": "rubric",
                    "text": "Statusline changes need cache and savings assertions.",
                    "promote_to": "rubric",
                },
            ],
            "capture_sources": ["mcp"],
        },
    )
    assert resp.status_code == 200
    trace_id = resp.json()["id"]

    trace = store.history.get_trace(trace_id)
    assert trace is not None
    assert trace.trace_confidence == "manual"
    assert trace.capture_sources == ["mcp"]
    assert [learning.text for learning in trace.learnings] == [
        "Prefer focused regressions.",
        "Statusline changes need cache and savings assertions.",
    ]
    assert trace.learnings[1].kind == "next_rule"
    assert trace.learnings[1].promote_to == "rubric"


def test_analytics_day_windows_use_calendar_days(
    app_no_auth: TestClient,
    store: StoreBundle,
) -> None:
    from lemoncrow.core.capabilities.pricing import usage_cost_usd
    from lemoncrow.core.foundation.models import Trace

    local_now = datetime.now().astimezone()
    today_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_end = today_start_local - timedelta(minutes=1)
    today_early = today_start_local + timedelta(minutes=1)

    store.history.record_trace(
        Trace(
            id="trace-yesterday-recent",
            agent="lemoncrow:code",
            host="copilot",
            domain="coding",
            task="yesterday but within 24h",
            status="success",
            model="gpt-5.4",
            input_tokens=200,
            output_tokens=40,
            created_at=yesterday_end,
        ),
        write_json=False,
    )
    store.history.record_trace(
        Trace(
            id="trace-today-current",
            agent="lemoncrow:code",
            host="copilot",
            domain="coding",
            task="today only",
            status="success",
            model="gpt-5.4",
            input_tokens=120,
            output_tokens=20,
            created_at=today_early,
        ),
        write_json=False,
    )

    today_cost = usage_cost_usd("gpt-5.4", input_tokens=120, output_tokens=20)
    two_day_cost = usage_cost_usd("gpt-5.4", input_tokens=200, output_tokens=40) + today_cost

    summary_today = app_no_auth.get("/analytics/summary?days=1")
    assert summary_today.status_code == 200
    assert summary_today.json()["total_cost"] == today_cost

    dashboard_today = app_no_auth.get("/analytics/dashboard?days=1")
    assert dashboard_today.status_code == 200
    dashboard_today_payload = dashboard_today.json()
    assert dashboard_today_payload["summary"]["total_cost"] == today_cost
    assert dashboard_today_payload["summary"]["total_sessions"] == 1
    assert dashboard_today_payload["daily"] == [
        {
            "date": today_start_local.date().isoformat(),
            "sessions": 1,
            "cost": today_cost,
            "input_tokens": 120,
            "output_tokens": 20,
        }
    ]

    summary_two_days = app_no_auth.get("/analytics/summary?days=2")
    assert summary_two_days.status_code == 200
    assert summary_two_days.json()["total_cost"] == round(two_day_cost, 6)


def test_dashboard_excludes_prompt_only_stub_sessions_from_usage_breakdowns(
    app_no_auth: TestClient,
    store: StoreBundle,
) -> None:
    from lemoncrow.core.foundation.models import ToolCall, Trace

    store.history.record_trace(
        Trace(
            id="trace-codex-stub",
            agent="lemoncrow:code",
            host="codex",
            domain="coding",
            task="prompt only",
            status="success",
            user_prompt_tokens=42,
        ),
        write_json=False,
    )
    store.history.record_trace(
        Trace(
            id="trace-codex-usage",
            agent="lemoncrow:code",
            host="codex",
            domain="coding",
            task="real codex usage",
            status="success",
            input_tokens=180,
            user_prompt_tokens=24,
            output_tokens=60,
            tools_called=[
                ToolCall(name="exec_command", args_hash="", count=2, output_tokens=120),
            ],
        ),
        write_json=False,
    )
    store.history.record_trace(
        Trace(
            id="trace-claude-usage",
            agent="lemoncrow:code",
            host="claude",
            domain="coding",
            task="real claude usage",
            status="success",
            input_tokens=100,
            output_tokens=40,
            model="claude-sonnet-4-5",
        ),
        write_json=False,
    )

    resp = app_no_auth.get("/analytics/dashboard")
    assert resp.status_code == 200
    dashboard = resp.json()

    by_host = {row["host"]: row["sessions"] for row in dashboard["by_host"]}
    assert by_host["codex"] == 1
    assert by_host["claude"] == 1

    by_model = {row["model"]: row["sessions"] for row in dashboard["by_model"]}
    assert by_model["unknown"] == 1
    assert by_model["claude-sonnet-4-5"] == 1

    overview = {(row["host"], row["model"]): row for row in dashboard["host_model_overview"]}
    assert ("codex", "unknown") in overview
    assert overview[("codex", "unknown")]["sessions"] == 1
    assert overview[("codex", "unknown")]["tool_calls"] == 2
    assert overview[("codex", "unknown")]["tool_output_tokens"] == 120
    assert overview[("codex", "unknown")]["billable_output_tokens"] == 60

    total_daily_sessions = sum(row["sessions"] for row in dashboard["daily"])
    assert total_daily_sessions == 2


def test_dashboard_collapses_duplicate_trace_rows_into_one_session(
    app_no_auth: TestClient,
    store: StoreBundle,
) -> None:
    from lemoncrow.core.capabilities.pricing import usage_cost_usd
    from lemoncrow.core.foundation.models import Trace

    created_at = datetime.now(UTC)
    expected_cost = usage_cost_usd("gpt-5.4", input_tokens=120, output_tokens=20)

    store.history.record_trace(
        Trace(
            id="trace-copilot-session-primary",
            session_id="copilot-session-1",
            agent="lemoncrow:code",
            host="copilot",
            domain="coding",
            task="priced copilot session",
            status="success",
            model="gpt-5.4",
            input_tokens=120,
            output_tokens=20,
            created_at=created_at,
        ),
        write_json=False,
    )
    store.history.record_trace(
        Trace(
            id="trace-copilot-session-transcript",
            session_id="copilot-session-1",
            agent="lemoncrow:code",
            host="copilot",
            domain="coding",
            task="prompt-only transcript stub",
            status="success",
            created_at=created_at + timedelta(minutes=1),
        ),
        write_json=False,
    )

    resp = app_no_auth.get("/analytics/dashboard?days=1")
    assert resp.status_code == 200
    dashboard = resp.json()

    assert dashboard["summary"]["total_sessions"] == 1

    by_host = {row["host"]: row for row in dashboard["by_host"]}
    assert by_host["copilot"]["sessions"] == 1
    assert by_host["copilot"]["cost"] == expected_cost

    assert dashboard["top_sessions"][0]["id"] == "copilot-session-1"
    assert sum(row["sessions"] for row in dashboard["daily"]) == 1


def test_dashboard_rolls_cursor_agent_into_cursor_host_family(
    app_no_auth: TestClient,
    store: StoreBundle,
) -> None:
    from lemoncrow.core.foundation.models import ModelUsage, Trace

    created_at = datetime.now(UTC)

    store.history.record_trace(
        Trace(
            id="trace-cursor-family",
            agent="lemoncrow:code",
            host="cursor",
            domain="coding",
            task="cursor family usage",
            status="success",
            model="claude-sonnet-4-5",
            model_usages=[
                ModelUsage(
                    model="claude-sonnet-4-5",
                    input_tokens=100,
                    output_tokens=40,
                )
            ],
            input_tokens=100,
            output_tokens=40,
            created_at=created_at,
        ),
        write_json=False,
    )
    store.history.record_trace(
        Trace(
            id="trace-cursor-agent-family",
            agent="lemoncrow:code",
            host="cursor-agent",
            domain="coding",
            task="cursor agent family usage",
            status="success",
            model="claude-sonnet-4-5",
            model_usages=[
                ModelUsage(
                    model="claude-sonnet-4-5",
                    input_tokens=80,
                    output_tokens=20,
                )
            ],
            input_tokens=80,
            output_tokens=20,
            created_at=created_at,
        ),
        write_json=False,
    )

    resp = app_no_auth.get("/analytics/dashboard")
    assert resp.status_code == 200
    dashboard = resp.json()

    by_host = {row["host"]: row for row in dashboard["by_host"]}
    assert by_host["cursor"]["sessions"] == 2
    assert "cursor-agent" not in by_host

    overview = {(row["host"], row["model"]): row for row in dashboard["host_model_overview"]}
    assert overview[("cursor", "claude-sonnet-4-5")]["sessions"] == 2


def test_dashboard_returns_hourly_usage_buckets(
    app_no_auth: TestClient,
    store: StoreBundle,
) -> None:
    from lemoncrow.core.foundation.models import Trace

    created_at = datetime.now(UTC).replace(minute=15, second=0, microsecond=0)
    store.history.record_trace(
        Trace(
            id="trace-hourly-usage",
            agent="lemoncrow:code",
            host="codex",
            domain="coding",
            task="hourly dashboard usage",
            status="success",
            output_tokens=60,
            created_at=created_at,
        ),
        write_json=False,
    )

    resp = app_no_auth.get("/analytics/dashboard?days=1")
    assert resp.status_code == 200
    dashboard = resp.json()

    expected_hour = created_at.isoformat()[:13].replace("T", " ") + ":00"
    hourly = {row["date"]: row for row in dashboard["hourly"]}
    assert hourly[expected_hour]["sessions"] == 1


def test_analytics_summary_uses_backend_pricing(app_no_auth: TestClient, store: StoreBundle) -> None:
    from lemoncrow.core.capabilities.pricing import usage_cost_usd
    from lemoncrow.core.foundation.models import ToolCall, Trace

    store.history.record_trace(
        Trace(
            id="trace-summary-priced",
            agent="lemoncrow:code",
            host="claude",
            domain="coding",
            task="summary pricing",
            status="success",
            model="claude-sonnet-4-5",
            input_tokens=120,
            output_tokens=40,
            user_prompt_tokens=12,
            tools_called=[ToolCall(name="exec_command", args_hash="", count=2, output_tokens=60)],
        ),
        write_json=False,
    )

    resp = app_no_auth.get("/analytics/summary?days=1")
    assert resp.status_code == 200
    summary = resp.json()

    assert summary["total_cost"] == usage_cost_usd("claude-sonnet-4-5", input_tokens=120, output_tokens=40)
    assert summary["tool_calls"] == 2
    assert summary["unique_tools"] == 1


def test_dashboard_splits_multi_model_usage_into_by_model_breakdown(
    app_no_auth: TestClient,
    store: StoreBundle,
) -> None:
    from lemoncrow.core.foundation.models import ModelUsage, Trace

    store.history.record_trace(
        Trace(
            id="trace-mixed-model-dashboard",
            agent="lemoncrow:code",
            host="gemini",
            domain="coding",
            task="mixed model session",
            status="success",
            model="",
            model_usages=[
                ModelUsage(
                    model="gemini-3-flash-preview",
                    input_tokens=80,
                    cached_input_tokens=20,
                    output_tokens=50,
                    thinking_tokens=5,
                ),
                ModelUsage(
                    model="gemini-3.1-pro-preview",
                    input_tokens=75,
                    cached_input_tokens=5,
                    output_tokens=30,
                    thinking_tokens=2,
                ),
            ],
            input_tokens=155,
            cached_input_tokens=25,
            output_tokens=80,
            thinking_tokens=7,
        ),
        write_json=False,
    )

    resp = app_no_auth.get("/analytics/dashboard?days=1")
    assert resp.status_code == 200
    dashboard = resp.json()

    by_model = {row["model"]: row for row in dashboard["by_model"]}
    assert by_model["gemini-3-flash-preview"]["input_tokens"] == 80
    assert by_model["gemini-3-flash-preview"]["cached_tokens"] == 20
    assert by_model["gemini-3.1-pro-preview"]["input_tokens"] == 75
    assert by_model["gemini-3.1-pro-preview"]["cached_tokens"] == 5


# --------------------------------------------------------------------------- #
# Blocks compatibility                                                        #
# --------------------------------------------------------------------------- #


def test_list_blocks_empty(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/blocks")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_block_from_compat_endpoints(app_no_auth: TestClient, store: StoreBundle) -> None:
    from lemoncrow.core.foundation.models import Playbook

    block = Playbook(
        id="rb-api-test",
        title="API Test Block",
        domain="test",
        situation="Testing API",
        triggers=["api"],
        procedure=["Step 1", "Step 2"],
    )
    store.knowledge.upsert_block(block, write_markdown=False)

    resp = app_no_auth.get("/blocks")
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()]
    assert "rb-api-test" in ids

    resp2 = app_no_auth.get("/blocks/rb-api-test")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == "rb-api-test"


# --------------------------------------------------------------------------- #
# Rubric endpoints                                                            #
# --------------------------------------------------------------------------- #


def test_run_rubric_not_found(app_no_auth: TestClient) -> None:
    resp = app_no_auth.post(
        "/v1/rubrics/run",
        json={"rubric_id": "nonexistent-rubric", "checks": {}},
    )
    assert resp.status_code == 404


def test_run_rubric_pass(app_no_auth: TestClient, store: StoreBundle) -> None:
    from lemoncrow.core.foundation.models import Rubric

    rubric = Rubric(
        id="rubric-test-api",
        domain="test",
        required_checks=["check_a"],
        block_if_missing=["check_a"],
    )
    store.knowledge.upsert_rubric(rubric, write_yaml=False)

    resp = app_no_auth.post(
        "/v1/rubrics/run",
        json={"rubric_id": "rubric-test-api", "checks": {"check_a": True}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pass"


# --------------------------------------------------------------------------- #
# Trace retrieval                                                             #
# --------------------------------------------------------------------------- #


def test_get_trace_not_found(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/v1/traces/nonexistent-trace-id")
    assert resp.status_code == 404


def test_get_trace_by_id(app_no_auth: TestClient, store: StoreBundle) -> None:
    from lemoncrow.core.foundation.models import Trace

    trace = Trace(
        id="trace-extract-test",
        agent="agent",
        domain="ecommerce",
        task="add product images",
        status="success",
        files_touched=["images.py"],
        commands_run=["pytest"],
        errors_seen=[],
        diff_summary="added image resize",
        output_summary="images resized successfully",
    )
    store.history.record_trace(trace, write_json=False)

    resp = app_no_auth.get("/v1/traces/trace-extract-test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "trace-extract-test"
    assert data["task"] == "add product images"


def test_compat_ledger_merges_main_and_subagent_artifacts(
    app_no_auth: TestClient,
    store: StoreBundle,
) -> None:
    from lemoncrow.core.foundation.models import RawArtifact, Trace

    created_at = datetime.now(UTC)
    main_artifact = RawArtifact(
        id="claude-main-artifact",
        source="claude",
        source_session_id="sess-123",
        kind="session.jsonl",
        relative_path="sess-123.jsonl",
        content_path="raw/claude/sess-123.jsonl",
        sha256_original="a" * 64,
        sha256_redacted="b" * 64,
        byte_count_original=10,
        byte_count_redacted=10,
        created_at=created_at,
        source_path="/tmp/main-session.jsonl",
    )
    subagent_artifact = RawArtifact(
        id="claude-subagent-artifact",
        source="claude",
        source_session_id="sess-123",
        kind="session.jsonl",
        relative_path="sess-123/subagents/agent-42.jsonl",
        content_path="raw/claude/sess-123/subagents/agent-42.jsonl",
        sha256_original="c" * 64,
        sha256_redacted="d" * 64,
        byte_count_original=10,
        byte_count_redacted=10,
        created_at=created_at,
        source_path="/tmp/subagent-session.jsonl",
    )
    store.history.record_raw_artifact(
        main_artifact,
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-05-16T00:00:00Z",
                        "message": {"id": "u1", "content": "main task"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-05-16T00:00:02Z",
                        "message": {
                            "id": "a1",
                            "content": [{"type": "text", "text": "main reply"}],
                            "usage": {"input_tokens": 10, "output_tokens": 4},
                        },
                    }
                ),
            ]
        ),
    )
    store.history.record_raw_artifact(
        subagent_artifact,
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-05-16T00:00:01Z",
                        "agentId": "agent-42",
                        "isSidechain": True,
                        "message": {"id": "u2", "content": "subagent task"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-05-16T00:00:03Z",
                        "agentId": "agent-42",
                        "isSidechain": True,
                        "message": {
                            "id": "a2",
                            "content": [{"type": "text", "text": "subagent reply"}],
                            "usage": {"input_tokens": 8, "output_tokens": 3},
                        },
                    }
                ),
            ]
        ),
    )
    store.history.record_trace(
        Trace(
            id="claude-sess-123",
            session_id="sess-123",
            agent="claude",
            host="claude",
            domain="coding",
            task="merge artifacts",
            status="success",
            raw_artifact_ids=[main_artifact.id, subagent_artifact.id],
            created_at=created_at,
        ),
        write_json=False,
    )

    resp = app_no_auth.get("/ledgers/sess-123")
    assert resp.status_code == 200
    data = resp.json()

    assert [turn["content"] for turn in data["conversations"]] == [
        "main task",
        "subagent task",
        "main reply",
        "subagent reply",
    ]
    assert {turn["source_scope"] for turn in data["conversations"]} == {
        "main",
        "subagent",
    }
    assert {artifact["scope"] for artifact in data["artifacts"]} == {
        "main",
        "subagent",
    }


def test_file_content_endpoint_serves_local_file(
    app_no_auth: TestClient,
    tmp_path: Path,
) -> None:
    sample = tmp_path / "note.txt"
    sample.write_text("hello rich sessions\n", encoding="utf-8")

    resp = app_no_auth.get("/v1/files/content", params={"path": str(sample)})
    assert resp.status_code == 200
    assert resp.text == "hello rich sessions\n"
    assert resp.headers["content-type"].startswith("text/plain")


def test_file_projection_endpoint_returns_compact_projection_metadata(
    app_no_auth: TestClient,
    tmp_path: Path,
) -> None:
    sample = tmp_path / "main.go"
    sample.write_text('package   main\n\nfunc   main()   {\n    println("x")\n}\n', encoding="utf-8")

    resp = app_no_auth.get("/v1/files/projection", params={"path": str(sample), "view": "compact"})
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["projection"]["view"] == "minified"
    assert payload["projection_mapping"]["projection_kind"] == "minified"
    assert payload["projection_delta"]["saved_tokens"] > 0


def test_file_projection_endpoint_requires_range_for_range_view(
    app_no_auth: TestClient,
    tmp_path: Path,
) -> None:
    sample = tmp_path / "note.txt"
    sample.write_text("hello\n", encoding="utf-8")

    resp = app_no_auth.get("/v1/files/projection", params={"path": str(sample), "view": "range"})
    assert resp.status_code == 400
    assert "range is required" in resp.json()["detail"]


def test_compat_ledger_keeps_copilot_tool_result_content(
    app_no_auth: TestClient,
    store: StoreBundle,
) -> None:
    from lemoncrow.core.foundation.models import RawArtifact, Trace

    created_at = datetime.now(UTC)
    artifact = RawArtifact(
        id="copilot-tool-artifact",
        source="copilot",
        source_session_id="copilot-sess-1",
        kind="events.jsonl",
        relative_path="events.jsonl",
        content_path="raw/copilot/copy/events.jsonl",
        sha256_original="e" * 64,
        sha256_redacted="f" * 64,
        byte_count_original=10,
        byte_count_redacted=10,
        created_at=created_at,
        source_path="/tmp/events.jsonl",
    )
    store.history.record_raw_artifact(
        artifact,
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool.execution_start",
                        "timestamp": "2026-05-16T00:00:00Z",
                        "data": {
                            "toolCallId": "call-1",
                            "toolName": "view",
                            "arguments": {
                                "path": "/tmp/demo.tsx",
                                "view_range": [10, 12],
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "tool.execution_complete",
                        "timestamp": "2026-05-16T00:00:01Z",
                        "data": {
                            "toolCallId": "call-1",
                            "result": {
                                "content": "10. first line\n11. second line",
                                "detailedContent": "10. first line\n11. second line",
                            },
                            "toolTelemetry": {"metrics": {"resultForLlmLength": 88}},
                        },
                    }
                ),
            ]
        ),
    )
    store.history.record_trace(
        Trace(
            id="copilot-copilot-sess-1",
            session_id="copilot-sess-1",
            agent="copilot",
            host="copilot",
            domain="coding",
            task="show tool results",
            status="success",
            raw_artifact_ids=[artifact.id],
            created_at=created_at,
        ),
        write_json=False,
    )

    resp = app_no_auth.get("/ledgers/copilot-sess-1")
    assert resp.status_code == 200
    data = resp.json()

    view_turns = [turn for turn in data["conversations"] if turn.get("tool_name") == "view"]
    assert len(view_turns) == 1
    assert view_turns[0]["content"] == "10. first line\n11. second line"


# --------------------------------------------------------------------------- #
# CLI service commands                                                        #
# --------------------------------------------------------------------------- #


def test_cli_service_config_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """service config command prints JSON."""
    from click.testing import CliRunner

    from lemoncrow.gateway.cli import cli

    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    runner = CliRunner()
    result = runner.invoke(cli, ["service", "config"], obj={"root": Path(".")})
    assert result.exit_code == 0
    import json

    data = json.loads(result.output)
    assert "require_auth" in data
    assert "api_key_configured" in data
    # api_key value must NOT appear in output
    assert "test-secret-key" not in result.output


def test_swarm_runs_endpoint_lists_live_activity(
    app_no_auth: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    program = tmp_path / "PROGRAM.md"
    program.write_text("Prompt title\n\nDo the thing.\n", encoding="utf-8")
    running_child = SwarmChildState(
        child_id="wave-01-run-01",
        label="candidate-1",
        wave_index=1,
        status="running",
        worktree_path=str(tmp_path / "worktree"),
        lemoncrow_root=str(tmp_path / "lemoncrow-root"),
        run_dir=str(tmp_path / "run"),
        spec_path=str(program),
        result_path=str(tmp_path / "result.json"),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        metadata_path=str(tmp_path / "meta.json"),
        current_activity="Running validation",
    )
    state = SwarmRunState(
        run_id="swarm-123",
        status="running",
        mode="continuous",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        base_snapshot_ref="base-snapshot",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="accepted-head",
        spec_source_path=str(program),
        copied_spec_path=str(program),
        runner_name="claude",
        runner_model="sonnet",
        evaluator_backend="ollama",
        evaluator_model="claude-opus-4.8",
        child_command=["echo", "hi"],
        runs=4,
        max_runs=4,
        current_wave=1,
        convergence_status="continue",
        convergence_summary="Keep exploring independent optimization directions.",
        next_wave_directives=["Trim redundant trace metadata."],
        primary_winner_child_id="wave-01-run-02",
        accepted_child_ids=["wave-01-run-02"],
        used_program_md=True,
        waves=[
            SwarmWaveState(
                wave_index=1,
                max_runs=4,
                planned_runs=2,
                planning_mode="bounded",
                child_ids=["wave-01-run-01", "wave-01-run-02"],
                accepted_child_ids=["wave-01-run-02"],
                primary_winner_child_id="wave-01-run-02",
            )
        ],
        children=[running_child],
    )
    monkeypatch.setattr("lemoncrow.core.service.api.list_swarm_runs", lambda _root: [state])

    response = app_no_auth.get("/v1/swarm/runs")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["run_id"] == "swarm-123"
    assert payload[0]["planned_runs"] == 2
    assert payload[0]["max_runs"] == 4
    assert payload[0]["running_children"][0]["activity"] == "Running validation"
    assert payload[0]["spec_title"] == "Prompt title"
    assert payload[0]["used_program_md"] is True
    assert payload[0]["evaluator_backend"] == "ollama"
    assert payload[0]["convergence_status"] == "continue"
    assert payload[0]["next_wave_directives"] == ["Trim redundant trace metadata."]


def test_swarm_launch_options_endpoint_returns_projects_and_editor_state(
    app_no_auth: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    program = tmp_path / "PROGRAM.md"
    program.write_text("Prompt title\n\nDo the thing.\n", encoding="utf-8")
    monkeypatch.setattr("lemoncrow.core.service.api.discover_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("lemoncrow.core.service.api.list_swarm_runs", lambda _root: [])

    response = app_no_auth.get("/v1/swarm/launch/options")

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_project_root"] == str(tmp_path)
    assert payload["selected_spec_path"] == "PROGRAM.md"
    assert payload["project_roots"][0]["full_path"] == str(tmp_path)
    assert payload["providers"][0]["id"] == "cli"
    assert payload["providers"][1]["supported"] is True
    assert payload["defaults"]["max_waves"] == 5
    assert payload["spec_document"]["content"].startswith("Prompt title")
    assert payload["notes"]["provider_credentials"]


def test_swarm_run_create_endpoint_uses_default_program_md(
    app_no_auth: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    program = tmp_path / "PROGRAM.md"
    program.write_text("Prompt title\n\nDo the thing.\n", encoding="utf-8")
    captured: dict[str, object] = {}
    state = SwarmRunState(
        run_id="swarm-123",
        status="pending",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path=str(program),
        copied_spec_path=str(program),
        runner_name="claude",
        child_command=["claude"],
        runs=3,
        max_runs=3,
    )
    monkeypatch.setattr("lemoncrow.core.service.api.discover_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("lemoncrow.core.service.api.list_swarm_runs", lambda _root: [])
    monkeypatch.setattr(
        "lemoncrow.core.service.api.initialize_swarm_run",
        lambda **kwargs: (captured.update(kwargs) or state, tmp_path / "state.json"),
    )
    monkeypatch.setattr(
        "lemoncrow.core.service.api.spawn_swarm_coordinator",
        lambda _root, _repo_root, _state_path, env_overrides=None: (
            4321,
            tmp_path / "coordinator.log",
        ),
    )
    monkeypatch.setattr("lemoncrow.core.service.api.save_swarm_state", lambda *_args, **_kwargs: None)

    response = app_no_auth.post(
        "/v1/swarm/runs",
        json={
            "project_root": str(tmp_path),
            "provider": "cli",
            "runner": "claude",
            "runs": 3,
            "continuous": True,
            "keep_worktrees": True,
            "effort": "high",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["coordinator_pid"] == 4321
    assert captured["spec_path"] == program
    assert captured["spec_resolution"] in {"default", "explicit"}
    assert captured["used_program_md"] is True
    assert captured["launch_provider"] == "cli"
    assert captured["evaluator_backend"] == "auto"
    assert captured["max_waves"] == 5
    assert captured["max_evaluator_failures"] == 3


def test_swarm_run_create_endpoint_supports_provider_worker_and_inline_spec(
    app_no_auth: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    captured_spawn: dict[str, object] = {}
    state = SwarmRunState(
        run_id="swarm-456",
        status="pending",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="HEAD",
        spec_source_path="PROGRAM.md",
        copied_spec_path=str(tmp_path / "PROGRAM.md"),
        runner_name="openai",
        runner_model="gpt-4o-mini",
        child_command=["python"],
        runs=2,
        max_runs=2,
    )
    monkeypatch.setattr("lemoncrow.core.service.api.discover_repo_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("lemoncrow.core.service.api.list_swarm_runs", lambda _root: [])
    monkeypatch.setattr(
        "lemoncrow.core.service.api.initialize_swarm_run",
        lambda **kwargs: (captured.update(kwargs) or state, tmp_path / "state.json"),
    )
    monkeypatch.setattr(
        "lemoncrow.core.service.api.spawn_swarm_coordinator",
        lambda _root, _repo_root, _state_path, env_overrides=None: (
            captured_spawn.update({"env_overrides": env_overrides}) or 9876,
            tmp_path / "coordinator.log",
        ),
    )
    monkeypatch.setattr("lemoncrow.core.service.api.save_swarm_state", lambda *_args, **_kwargs: None)

    response = app_no_auth.post(
        "/v1/swarm/runs",
        json={
            "project_root": str(tmp_path),
            "spec_mode": "inline",
            "spec_path": "PROGRAM.md",
            "spec_content": "Prompt title\n\nDo the thing.\n",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "evaluator_backend": "litellm",
            "evaluator_model": "claude-opus-4.8",
            "max_waves": 7,
            "max_evaluator_failures": 4,
            "provider_api_key": "sk-test-key",
            "provider_base_url": "https://openrouter.example/v1",
            "runs": 2,
            "continuous": False,
            "keep_worktrees": True,
            "effort": "medium",
        },
    )

    assert response.status_code == 200
    assert (tmp_path / "PROGRAM.md").read_text(encoding="utf-8").startswith("Prompt title")
    assert captured["runner_name"] == "openai"
    assert captured["runner_model"] == "gpt-4o-mini"
    assert captured["spec_source_path"] == "PROGRAM.md"
    assert captured["launch_provider"] == "openai"
    assert captured["evaluator_backend"] == "litellm"
    assert captured["evaluator_model"] == "claude-opus-4.8"
    assert captured["max_waves"] == 1
    assert captured["max_evaluator_failures"] == 4
    assert "_provider-worker" in captured["child_command"]
    assert "gpt-4o-mini" not in " ".join(captured["child_command"])
    assert captured_spawn["env_overrides"] == {
        "LEMONCROW_OPENAI_API_KEY": "sk-test-key",
        "LEMONCROW_OPENAI_BASE_URL": "https://openrouter.example/v1",
    }


def test_swarm_run_detail_returns_export_and_apply_payloads(
    app_no_auth: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    program = tmp_path / "program.md"
    program.write_text("Prompt title\n\nDo the thing.\n", encoding="utf-8")
    artifact = SwarmArtifactRef(
        kind="wave-manifest",
        label="Wave 1 manifest",
        path=str(tmp_path / "wave-01-manifest.json"),
        exists=True,
    )
    state = SwarmRunState(
        run_id="swarm-123",
        status="success",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        base_snapshot_ref="base-snapshot",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="accepted-head",
        artifact_root=str(tmp_path / "artifacts"),
        spec_source_path=str(program),
        copied_spec_path=str(program),
        runner_name="claude",
        child_command=["echo", "hi"],
        runs=2,
        max_runs=2,
        accepted_commits=[
            SwarmAcceptedCommit(
                order=1,
                child_id="wave-01-run-01",
                commit_ref="abc1234",
                patch_path=str(tmp_path / "candidate.patch"),
                artifacts=[artifact],
            )
        ],
        export_artifacts=[artifact],
        transplant_commands=["git cherry-pick abc1234"],
    )
    monkeypatch.setattr("lemoncrow.core.service.api.resolve_state_path", lambda _root, _run_id: state_path)
    monkeypatch.setattr("lemoncrow.core.service.api.load_swarm_state", lambda _path: state)

    response = app_no_auth.get("/v1/swarm/runs/swarm-123")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["run_id"] == "swarm-123"
    assert payload["spec"]["content"].startswith("Prompt title")
    assert payload["export"]["accepted_commits"][0]["commit_ref"] == "abc1234"
    assert payload["apply"]["commands"][0] == "git cherry-pick abc1234"


def test_swarm_logs_and_stop_endpoints(
    app_no_auth: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    state = SwarmRunState(
        run_id="swarm-123",
        status="stopped",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        integration_worktree=str(tmp_path / "pool" / "integration"),
        integration_base_ref="accepted-head",
        spec_source_path=str(tmp_path / "program.md"),
        copied_spec_path=str(tmp_path / "program.md"),
        runner_name="claude",
        child_command=["echo", "hi"],
        runs=1,
        max_runs=1,
        stop_reason="Stopped by user.",
    )
    monkeypatch.setattr("lemoncrow.core.service.api.resolve_state_path", lambda _root, _run_id: state_path)
    monkeypatch.setattr("lemoncrow.core.service.api.read_swarm_log", lambda *_args, **_kwargs: "child heartbeat")
    monkeypatch.setattr("lemoncrow.core.service.api.stop_swarm_run", lambda **_kwargs: state)

    logs_response = app_no_auth.get("/v1/swarm/runs/swarm-123/logs", params={"child_id": "wave-01-run-01"})
    stop_response = app_no_auth.post("/v1/swarm/runs/swarm-123/stop")

    assert logs_response.status_code == 200
    assert logs_response.json()["content"] == "child heartbeat"
    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "stopped"
