from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI API tests require the api extra")

from fastapi.testclient import TestClient

from lemoncrow.core.service.api import create_app
from lemoncrow.core.service.telemetry.local_store import LocalTelemetryStore


@pytest.fixture()
def app_no_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("LEMONCROW_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LEMONCROW_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ACK", str(tmp_path / "telemetry_ack"))
    # Remote telemetry is mandatory (no opt-out); bypass the pytest-only
    # suppression guard so these assertions exercise the real default
    # instead of the blanket test-suite safety override. None of the
    # requests this fixture drives ever reach the network.
    monkeypatch.setenv("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS", "1")
    return TestClient(create_app(store_root=tmp_path / ".lemoncrow"))


def test_telemetry_api_local_schema_summary_and_config(app_no_auth: TestClient) -> None:
    cfg = app_no_auth.get("/telemetry/config")
    assert cfg.status_code == 200
    # Remote telemetry is mandatory since d41e3d88 ("make product telemetry
    # mandatory (remove opt-out)") -- there is no user-facing off switch.
    assert cfg.json()["remote_enabled"] is True

    write = app_no_auth.post(
        "/telemetry/local",
        json={
            "event": "session_start",
            "props": {
                "agent_host": "frontend",
                "lemoncrow_version": "0.1.0",
                "os": "browser",
                "py_version": "n/a",
                "anon_id": "a",
                "session_id": "s",
            },
        },
    )
    assert write.status_code == 200

    events = app_no_auth.get("/telemetry/local?limit=10")
    assert events.status_code == 200
    names = [event["event"] for event in events.json()["events"]]
    assert "session_start" in names

    summary = app_no_auth.get("/telemetry/summary")
    assert summary.status_code == 200
    assert summary.json()["events_total"] >= 1

    schema = app_no_auth.get("/telemetry/schema")
    assert schema.status_code == 200
    assert "cli_command_invoked" in schema.json()["events"]

    updated = app_no_auth.post("/telemetry/config", json={"lexical_frustration_enabled": False})
    assert updated.status_code == 200
    assert updated.json()["lexical_frustration_enabled"] is False

    ack = app_no_auth.post("/telemetry/ack")
    assert ack.status_code == 200
    assert ack.json()["remote_enabled"] is True


def test_telemetry_api_filters_by_window_and_host(app_no_auth: TestClient, tmp_path: Path) -> None:
    store = LocalTelemetryStore(tmp_path / "telemetry.db")
    now = time.time()
    store.write_event(
        event="session_start",
        props={
            "agent_host": "frontend",
            "lemoncrow_version": "0.1.0",
            "os": "browser",
            "py_version": "n/a",
            "anon_id": "a",
            "session_id": "frontend-session",
        },
        exported=False,
        ts=now - 600,
    )
    store.write_event(
        event="cli_command_invoked",
        props={
            "command_name": "reasoning",
            "session_id": "frontend-session",
            "anon_id": "a",
        },
        exported=False,
        ts=now - 45,
    )
    store.write_event(
        event="session_start",
        props={
            "agent_host": "codex",
            "lemoncrow_version": "0.1.0",
            "os": "browser",
            "py_version": "n/a",
            "anon_id": "b",
            "session_id": "codex-session",
        },
        exported=False,
        ts=now - 30,
    )

    events = app_no_auth.get(f"/telemetry/local?limit=10&since={now - 300}&host=frontend")
    assert events.status_code == 200
    payload = events.json()["events"]
    assert [event["event"] for event in payload] == ["cli_command_invoked"]

    summary = app_no_auth.get(f"/telemetry/summary?since={now - 300}&host=frontend")
    assert summary.status_code == 200
    body = summary.json()
    assert body["events_total"] == 1
    assert body["active_sessions"] == 1
    assert body["unique_event_types"] == 1
    assert body["agent_hosts"] == [{"name": "frontend", "count": 1}]
    assert body["top_commands"] == [{"name": "reasoning", "count": 1}]
