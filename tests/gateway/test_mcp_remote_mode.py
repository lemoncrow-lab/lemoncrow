"""Tests for service-backed MCP mode.

Validates that:
- Service-backed tools route through RemoteClient.
- Service unavailable returns a structured error dict.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lemoncrow.core.environment import HIDDEN_LLM_TOOLS
from lemoncrow.gateway.adapters.mcp_server import _REMOTE_TOOLS, _handle
from lemoncrow.infra.storage.sqlite_store import SQLiteStore
from tests.helpers import init_store_at

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def service_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reset the module-level cache between tests.
    import lemoncrow.gateway.adapters.mcp_server as m

    m._remote_client = None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    return _handle(req)  # type: ignore[return-value]


def _mock_client(return_values: dict[str, dict[str, Any]]) -> MagicMock:
    client = MagicMock()
    for method_name, retval in return_values.items():
        getattr(client, method_name).return_value = retval
    return client


def _seed_store(root: Path) -> None:
    init_store_at(str(root))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, process: subprocess.Popen[str], timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    last_error = "service never became healthy"
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"service exited early with code {process.returncode}: {stderr}")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1.0) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise AssertionError(last_error)


@contextmanager
def _live_service(root: Path) -> Any:
    pytest.importorskip("fastapi", reason="live remote-mode tests require the api extra")
    pytest.importorskip("uvicorn", reason="live remote-mode tests require uvicorn")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "LEMONCROW_ROOT": str(root),
        "LEMONCROW_REQUIRE_AUTH": "false",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "lemoncrow.core.service.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _wait_for_health(base_url, process)
        yield base_url
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_initialize_request_returns_server_info(service_mode: None) -> None:
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }
    resp = _handle(req)
    assert resp is not None
    assert "result" in resp
    assert resp["result"]["serverInfo"]["name"] == "lemon"


def test_tools_list_returns_all_tools(service_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = _handle(req)
    assert resp is not None
    tools = {t["name"] for t in resp["result"]["tools"]}
    for remote_tool in _REMOTE_TOOLS - HIDDEN_LLM_TOOLS:
        assert remote_tool in tools
    assert "read" in tools
    assert "reasoning" not in tools
    assert "lint" not in tools
    assert "compact" not in tools


# --------------------------------------------------------------------------- #
# Remote mode — happy path                                                    #
# --------------------------------------------------------------------------- #


def test_remote_context_same_shape(service_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"context": "Here are the relevant procedures."}
    client = _mock_client({"get_context": expected})

    import lemoncrow.gateway.adapters.mcp_server as m

    m._remote_client = client

    resp = _call_tool("context", {"task": "publish product"})
    assert "result" in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["context"] == "Here are the relevant procedures."


def test_remote_record_trace_same_shape(service_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"id": "trace-abc-123"}
    client = _mock_client({"record_trace": expected})

    import lemoncrow.gateway.adapters.mcp_server as m

    m._remote_client = client

    resp = _call_tool(
        "trace",
        {"agent": "test", "domain": "e2e", "task": "deploy", "status": "success"},
    )
    assert "result" in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["trace_id"] == "trace-abc-123"
    assert payload["event_recorded"] is False


def test_remote_routed_tools_do_not_create_local_runtime_state(
    service_mode: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _mock_client({"get_context": {"context": "remote"}})
    local_root = tmp_path / "no-local-lemoncrow"
    monkeypatch.setenv("LEMONCROW_ROOT", str(local_root))

    import lemoncrow.gateway.adapters.mcp_server as m

    m._current_ledger = None
    m._realtime_ctx = None
    m._context_budget_recorder = None
    m._remote_client = client

    resp = _call_tool("context", {"task": "publish product"})

    assert "result" in resp
    client.get_context.assert_called_once()
    assert m._realtime_ctx is None


def test_remote_memory_routes_to_service(service_mode: None) -> None:
    expected = {"id": "mem-1", "version": 1}
    client = _mock_client({"memory": expected})

    import lemoncrow.gateway.adapters.mcp_server as m

    m._remote_client = client

    resp = _call_tool(
        "memory",
        {"op": "recall", "agent_id": "codex", "query": "remember"},
    )

    assert "result" in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload == expected
    client.memory.assert_called_once()


@pytest.mark.slow  # Spawns a real HTTP service subprocess
def test_remote_mode_live_service_round_trip(
    service_mode: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".lemoncrow"
    _seed_store(root)

    import lemoncrow.gateway.adapters.mcp_server as m

    m._current_ledger = None
    m._realtime_ctx = None
    m._remote_client = None

    with _live_service(root) as base_url:
        monkeypatch.setenv("LEMONCROW_SERVICE_URL", base_url)

        context = _call_tool("context", {"task": "deploy the app"})
        context_payload = json.loads(context["result"]["content"][0]["text"])
        assert "context" in context_payload

        memory = _call_tool(
            "memory",
            {
                "op": "store_fact",
                "agent_id": "codex",
                "subject": "deploy-note",
                "fact": "Use remote service storage.",
                "citations": "integration test",
                "reason": "Verify remote mode memory routing.",
                "scope": "repository",
            },
        )
        memory_payload = json.loads(memory["result"]["content"][0]["text"])
        assert memory_payload["id"]

        rescue = _call_tool("rescue", {"task": "deploy", "error": "connection refused"})
        rescue_payload = json.loads(rescue["result"]["content"][0]["text"])
        assert "rescue" in rescue_payload

        verify = _call_tool(
            "verify",
            {
                "rubric_id": "rubric_state_change_safety",
                "checks": {
                    "canonical_identifier_used": True,
                    "pre_change_state_captured": True,
                    "read_after_write_completed": True,
                    "observed_state_matches_intent": True,
                    "rollback_plan_available": True,
                    "user_visible_surface_checked": True,
                },
            },
        )
        verify_payload = json.loads(verify["result"]["content"][0]["text"])
        assert verify_payload["status"] == "pass"

        trace = _call_tool(
            "trace",
            {"agent": "codex", "domain": "coding", "task": "remote e2e", "status": "success"},
        )
        trace_payload = json.loads(trace["result"]["content"][0]["text"])
        assert trace_payload["trace_id"]

    stored = SQLiteStore(root).get_trace(trace_payload["trace_id"])
    assert stored is not None
    assert stored.task == "remote e2e"


# --------------------------------------------------------------------------- #
# Remote mode — error handling                                                #
# --------------------------------------------------------------------------- #


def test_remote_service_unavailable_returns_structured_error(
    service_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the remote service is unreachable, the MCP handler returns a JSON-RPC error."""
    from urllib.error import URLError

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise URLError("Connection refused")

    import lemoncrow.gateway.adapters.mcp_server as m
    import lemoncrow.gateway.adapters.remote_client as rc

    # Create a real RemoteClient whose underlying urlopen will fail.
    real_client = rc.RemoteClient(base_url="http://127.0.0.1:1")  # port 1 is always closed
    m._remote_client = real_client

    # Monkeypatch urlopen to raise immediately.
    with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
        resp = _call_tool("context", {"task": "t"})

    # The MCP wrapper must return a structured error, not raise.
    assert resp is not None
    # Either the result contains an "ok": False dict OR it's a JSON-RPC error.
    if "error" in resp:
        assert "message" in resp["error"]
    elif resp["result"].get("isError"):
        assert resp["result"]["content"][0]["text"]
    else:
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload.get("ok") is False or "error" in payload


# --------------------------------------------------------------------------- #
# Remote client unit tests                                                    #
# --------------------------------------------------------------------------- #


def test_remote_client_routes_correctly() -> None:
    """RemoteClient methods call the right paths."""
    from unittest.mock import patch as _patch

    from lemoncrow.gateway.adapters.remote_client import RemoteClient

    client = RemoteClient(base_url="http://localhost:8787", api_key="key")

    captured: list[tuple[str, str]] = []

    def _fake_request(self: Any, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        captured.append((method, path))
        return {"ok": True}

    with _patch.object(RemoteClient, "_request", _fake_request):
        client.get_context({"task": "t"})
        client.rescue_failure({"task": "t", "error": "e"})
        client.run_rubric_gate({"rubric_id": "r", "checks": {}})
        client.record_trace({"agent": "a", "domain": "d", "task": "t", "status": "success"})
        client.memory({"op": "block_get", "agent_id": "a", "label": "l"})

    paths = [p for _, p in captured]
    assert "/v1/reasoning/context" in paths
    assert "/v1/reasoning/rescue" in paths
    assert "/v1/rubrics/run" in paths
    assert "/v1/traces" in paths
    assert any(path.startswith("/v1/memory/blocks?") for path in paths)
