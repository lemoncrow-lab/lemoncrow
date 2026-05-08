"""Tests for MCP remote mode (P5).

Validates that:
- Local mode still works as before.
- Remote mode routes the 5 HTTP-backed tools through RemoteClient.
- Response shape is the same whether local or remote.
- Service unavailable returns a structured error dict.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from atelier.gateway.adapters.cli import cli
from atelier.gateway.adapters.mcp_server import _REMOTE_TOOLS, _handle
from atelier.infra.storage.sqlite_store import SQLiteStore

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_MCP_MODE", raising=False)


@pytest.fixture()
def remote_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_MCP_MODE", "remote")
    # Reset the module-level cache between tests.
    import atelier.gateway.adapters.mcp_server as m

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
    result = CliRunner().invoke(cli, ["--root", str(root), "init"])
    assert result.exit_code == 0, result.output


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
        except Exception as exc:
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
        "ATELIER_ROOT": str(root),
        "ATELIER_REQUIRE_AUTH": "false",
        "ATELIER_EMBEDDER": "null",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "atelier.core.service.api:app",
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


# --------------------------------------------------------------------------- #
# Local mode                                                                  #
# --------------------------------------------------------------------------- #


def test_mcp_local_mode_still_works(local_mode: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_reasoning_context works in local mode with an empty store."""
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path / ".atelier"))

    from atelier.infra.storage.sqlite_store import SQLiteStore

    st = SQLiteStore(tmp_path / ".atelier")
    st.init()

    resp = _call_tool("reasoning", {"task": "deploy the app"})
    assert resp["result"]["content"][0]["type"] == "text"
    text = resp["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert "context" in payload


def test_initialize_request_returns_server_info(local_mode: None) -> None:
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }
    resp = _handle(req)
    assert resp is not None
    assert "result" in resp
    assert resp["result"]["serverInfo"]["name"] == "atelier-reasoning"


def test_tools_list_returns_all_tools(local_mode: None) -> None:
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = _handle(req)
    assert resp is not None
    tools = {t["name"] for t in resp["result"]["tools"]}
    for remote_tool in _REMOTE_TOOLS:
        assert remote_tool in tools
    assert "reasoning" in tools
    assert "compact" in tools


# --------------------------------------------------------------------------- #
# Remote mode — happy path                                                    #
# --------------------------------------------------------------------------- #


def test_remote_check_plan_same_shape(remote_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """check_plan in remote mode returns the same top-level keys."""
    expected = {
        "status": "pass",
        "warnings": [],
        "suggested_plan": [],
        "matched_blocks": [],
    }
    client = _mock_client({"check_plan": expected})

    import atelier.gateway.adapters.mcp_server as m

    m._remote_client = client

    resp = _call_tool("lint", {"task": "deploy", "plan": ["step 1"]})
    assert resp is not None
    assert "result" in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "status" in payload
    assert payload["status"] == "pass"


def test_remote_get_reasoning_context_same_shape(remote_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"context": "Here are the relevant procedures."}
    client = _mock_client({"get_reasoning_context": expected})

    import atelier.gateway.adapters.mcp_server as m

    m._remote_client = client

    resp = _call_tool("reasoning", {"task": "publish product"})
    assert "result" in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["context"] == "Here are the relevant procedures."


def test_remote_record_trace_same_shape(remote_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"id": "trace-abc-123"}
    client = _mock_client({"record_trace": expected})

    import atelier.gateway.adapters.mcp_server as m

    m._remote_client = client

    resp = _call_tool(
        "trace",
        {"agent": "test", "domain": "e2e", "task": "deploy", "status": "success"},
    )
    assert "result" in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "id" in payload


def test_remote_mode_live_service_round_trip(
    remote_mode: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".atelier"
    _seed_store(root)

    import atelier.gateway.adapters.mcp_server as m

    m._current_ledger = None
    m._realtime_ctx = None
    m._remote_client = None

    with _live_service(root) as base_url:
        monkeypatch.setenv("ATELIER_SERVICE_URL", base_url)

        reasoning = _call_tool("reasoning", {"task": "deploy the app"})
        reasoning_payload = json.loads(reasoning["result"]["content"][0]["text"])
        assert "context" in reasoning_payload

        lint = _call_tool("lint", {"task": "deploy", "plan": ["write code", "run tests"]})
        lint_payload = json.loads(lint["result"]["content"][0]["text"])
        assert lint_payload["status"] in {"pass", "warn", "blocked"}

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
        assert trace_payload["id"]

    stored = SQLiteStore(root).get_trace(trace_payload["id"])
    assert stored is not None
    assert stored.task == "remote e2e"


# --------------------------------------------------------------------------- #
# Remote mode — error handling                                                #
# --------------------------------------------------------------------------- #


def test_remote_service_unavailable_returns_structured_error(
    remote_mode: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the remote service is unreachable, the MCP handler returns a JSON-RPC error."""
    from urllib.error import URLError

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise URLError("Connection refused")

    import atelier.gateway.adapters.mcp_server as m
    import atelier.gateway.adapters.remote_client as rc

    # Create a real RemoteClient whose underlying urlopen will fail.
    real_client = rc.RemoteClient(base_url="http://127.0.0.1:1")  # port 1 is always closed
    m._remote_client = real_client

    # Monkeypatch urlopen to raise immediately.
    with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
        resp = _call_tool("lint", {"task": "t", "plan": ["s"]})

    # The MCP wrapper must return a structured error, not raise.
    assert resp is not None
    # Either the result contains an "ok": False dict OR it's a JSON-RPC error.
    if "error" in resp:
        assert "message" in resp["error"]
    else:
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload.get("ok") is False or "error" in payload


# --------------------------------------------------------------------------- #
# Remote client unit tests                                                    #
# --------------------------------------------------------------------------- #


def test_remote_client_routes_correctly() -> None:
    """RemoteClient methods call the right paths."""
    from unittest.mock import patch as _patch

    from atelier.gateway.adapters.remote_client import RemoteClient

    client = RemoteClient(base_url="http://localhost:8787", api_key="key")

    captured: list[tuple[str, str]] = []

    def _fake_request(self: Any, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        captured.append((method, path))
        return {"ok": True}

    with _patch.object(RemoteClient, "_request", _fake_request):
        client.get_reasoning_context({"task": "t"})
        client.check_plan({"task": "t", "plan": []})
        client.rescue_failure({"task": "t", "error": "e"})
        client.run_rubric_gate({"rubric_id": "r", "checks": {}})
        client.record_trace({"agent": "a", "domain": "d", "task": "t", "status": "success"})

    paths = [p for _, p in captured]
    assert "/v1/reasoning/context" in paths
    assert "/v1/reasoning/check-plan" in paths
    assert "/v1/reasoning/rescue" in paths
    assert "/v1/rubrics/run" in paths
    assert "/v1/traces" in paths
