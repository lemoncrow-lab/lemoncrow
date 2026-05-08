"""Tests for the Atelier production service API (P4).

Uses FastAPI TestClient with an in-memory SQLite store so no server starts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI API tests require the api extra")

from fastapi.testclient import TestClient

from atelier.core.service.api import create_app
from atelier.infra.storage.sqlite_store import SQLiteStore

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteStore:
    st = SQLiteStore(tmp_path / ".atelier")
    st.init()
    return st


@pytest.fixture()
def app_no_auth(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with auth disabled."""
    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    return TestClient(create_app(store=store))


@pytest.fixture()
def app_with_auth(store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App with auth enabled and known key."""
    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "true")
    monkeypatch.setenv("ATELIER_API_KEY", "test-secret-key-123")
    return TestClient(create_app(store=store))


AUTH_HEADERS = {"Authorization": "Bearer test-secret-key-123"}


# --------------------------------------------------------------------------- #
# Health / readiness                                                          #
# --------------------------------------------------------------------------- #


def test_health_returns_ok(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ready_returns_ok_with_sqlite(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert "storage" in data
    assert data["storage"]["backend"] == "sqlite"


def test_metrics_accessible_no_auth(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "block_count" in data
    assert "trace_count" in data


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


def test_check_plan_pass(app_no_auth: TestClient) -> None:
    resp = app_no_auth.post(
        "/v1/reasoning/check-plan",
        json={
            "task": "deploy the new feature",
            "plan": ["Write code", "Run tests", "Validate output"],
            "domain": "general",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert data["status"] in ("pass", "warn", "blocked")


def test_check_plan_blocks_bad_state_change_plan(app_no_auth: TestClient, store: SQLiteStore) -> None:
    """A plan that references a known dead end should be blocked if a block exists."""
    from atelier.core.foundation.models import ReasonBlock

    block = ReasonBlock(
        id="rb-state-change-test",
        title="Canonical Identifier Required",
        domain="state.change",
        situation="Applying a live state change",
        triggers=["state change", "deploy"],
        dead_ends=["resolve target from url slug alone"],
        procedure=["Resolve and record the canonical stable identifier first"],
        failure_signals=[],
    )
    store.upsert_block(block, write_markdown=False)

    resp = app_no_auth.post(
        "/v1/reasoning/check-plan",
        json={
            "task": "deploy a live config update",
            "plan": ["resolve target from url slug alone"],
            "domain": "state.change",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "blocked"


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


def test_trace_record_persists(app_no_auth: TestClient, store: SQLiteStore) -> None:
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
    trace = store.get_trace(trace_id)
    assert trace is not None
    assert trace.status == "success"


def test_trace_redacts_secrets(app_no_auth: TestClient, store: SQLiteStore) -> None:
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
    trace = store.get_trace(trace_id)
    assert trace is not None
    assert "sk-supersecret" not in trace.task


# --------------------------------------------------------------------------- #
# ReasonBlocks CRUD                                                           #
# --------------------------------------------------------------------------- #


def test_list_blocks_empty(app_no_auth: TestClient) -> None:
    resp = app_no_auth.get("/v1/reasonblocks")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_create_and_retrieve_block(app_no_auth: TestClient) -> None:
    resp = app_no_auth.post(
        "/v1/reasonblocks",
        json={
            "id": "rb-api-test",
            "title": "API Test Block",
            "domain": "test",
            "situation": "Testing API",
            "triggers": ["api"],
            "procedure": ["Step 1", "Step 2"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "rb-api-test"

    # list should now contain it
    resp2 = app_no_auth.get("/v1/reasonblocks?domain=test")
    assert resp2.status_code == 200
    ids = [b["id"] for b in resp2.json()]
    assert "rb-api-test" in ids


# --------------------------------------------------------------------------- #
# Rubric endpoints                                                            #
# --------------------------------------------------------------------------- #


def test_run_rubric_not_found(app_no_auth: TestClient) -> None:
    resp = app_no_auth.post(
        "/v1/rubrics/run",
        json={"rubric_id": "nonexistent-rubric", "checks": {}},
    )
    assert resp.status_code == 404


def test_run_rubric_pass(app_no_auth: TestClient, store: SQLiteStore) -> None:
    from atelier.core.foundation.models import Rubric

    rubric = Rubric(
        id="rubric-test-api",
        domain="test",
        required_checks=["check_a"],
        block_if_missing=["check_a"],
    )
    store.upsert_rubric(rubric, write_yaml=False)

    resp = app_no_auth.post(
        "/v1/rubrics/run",
        json={"rubric_id": "rubric-test-api", "checks": {"check_a": True}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pass"


# --------------------------------------------------------------------------- #
# Extract                                                                     #
# --------------------------------------------------------------------------- #


def test_extract_reasonblock_not_found(app_no_auth: TestClient) -> None:
    resp = app_no_auth.post(
        "/v1/extract/reasonblock",
        json={"trace_id": "nonexistent-trace-id", "save": False},
    )
    assert resp.status_code == 404


def test_extract_reasonblock_from_trace(app_no_auth: TestClient, store: SQLiteStore) -> None:
    from atelier.core.foundation.models import Trace

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
    store.record_trace(trace, write_json=False)

    resp = app_no_auth.post(
        "/v1/extract/reasonblock",
        json={"trace_id": "trace-extract-test", "save": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "block" in data
    assert "confidence" in data


# --------------------------------------------------------------------------- #
# CLI service commands                                                        #
# --------------------------------------------------------------------------- #


def test_cli_service_config_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """service config command prints JSON."""
    from click.testing import CliRunner

    from atelier.gateway.adapters.cli import cli

    monkeypatch.setenv("ATELIER_REQUIRE_AUTH", "false")
    runner = CliRunner()
    result = runner.invoke(cli, ["service", "config"], obj={"root": Path(".")})
    assert result.exit_code == 0
    import json

    data = json.loads(result.output)
    assert "require_auth" in data
    assert "api_key_configured" in data
    # api_key value must NOT appear in output
    assert "test-secret-key" not in result.output
