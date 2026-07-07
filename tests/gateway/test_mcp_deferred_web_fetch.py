"""Phase 3 deferred async web_fetch tests.

On the stdio worker, web_fetch runs on the shared asyncio reactor and frees the
MCP worker immediately; the reactor future's completion fires the deferral
continuation which writes the JSON-RPC response. Hermetic: ``_write_jsonrpc`` is
monkeypatched to capture responses and a loopback HTTP server stands in for the
network.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities import web_fetch
from atelier.gateway.adapters import mcp_server
from tests.helpers import init_store_at


def _wait_for(predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _TextHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"deferred web body"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        pass


@pytest.fixture()
def loopback() -> Iterator[int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _TextHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def wf_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_MEMORY_BACKEND", "sqlite")
    monkeypatch.delenv("ATELIER_SERVICE_URL", raising=False)
    monkeypatch.delenv("ATELIER_MCP_DEFER_WEB_FETCH", raising=False)
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    web_fetch.clear_web_fetch_cache()
    return tmp_path


def _wf_request(rid: Any, url: str, **args: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": "web_fetch", "arguments": {"url": url, "type": "text", **args}},
    }


def test_deferred_web_fetch_written_by_reactor_continuation(
    wf_env: Path, loopback: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []
    lock = threading.Lock()
    monkeypatch.setattr(mcp_server, "_write_jsonrpc", lambda msg: captured.append(msg) or None)

    mcp_server._handle_and_write(_wf_request(11, f"http://127.0.0.1:{loopback}/"))

    assert _wait_for(lambda: len(captured) >= 1)
    time.sleep(0.1)  # guard against a spurious second write
    with lock:
        assert len(captured) == 1
        resp = captured[0]
    assert resp["id"] == 11
    assert "deferred web body" in resp["result"]["content"][0]["text"]


def test_kill_switch_keeps_web_fetch_synchronous(wf_env: Path, loopback: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_MCP_DEFER_WEB_FETCH", "0")
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_server, "_write_jsonrpc", lambda msg: captured.append(msg) or None)

    mcp_server._handle_and_write(_wf_request(22, f"http://127.0.0.1:{loopback}/"))
    # Written synchronously, before _handle_and_write returned.
    assert len(captured) == 1
    assert captured[0]["id"] == 22
    assert "deferred web body" in captured[0]["result"]["content"][0]["text"]


def test_deferred_web_fetch_ssrf_error_is_a_clean_tool_error(wf_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_server, "_write_jsonrpc", lambda msg: captured.append(msg) or None)

    # A private-IP target: the deferred collect() raises -> finalize_error routes
    # it through the same tool-error pipeline as the synchronous path.
    mcp_server._handle_and_write(_wf_request(33, "http://10.0.0.1/"))

    assert _wait_for(lambda: len(captured) >= 1)
    time.sleep(0.1)
    assert len(captured) == 1
    resp = captured[0]
    assert resp["id"] == 33
    blob = str(resp)
    assert "private/local" in blob


def test_deferred_marker_returned_in_capable_context(wf_env: Path, loopback: int) -> None:
    mcp_server._deferral_context.active = True
    try:
        result = mcp_server.tool_web_fetch({"url": f"http://127.0.0.1:{loopback}/", "type": "text"})
        assert isinstance(result, mcp_server._DeferredResult)
        done = threading.Event()
        box: dict[str, Any] = {}

        def _cb() -> None:
            box["res"] = result.collect()
            done.set()

        if result.register(_cb) is False:
            _cb()
        assert done.wait(8.0)
    finally:
        mcp_server._deferral_context.active = False
    assert box["res"]["content"] == "deferred web body"
