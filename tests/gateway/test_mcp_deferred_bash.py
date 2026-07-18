"""Phase 2 deferred-foreground-bash tests.

A foreground bash command frees the MCP pool worker immediately (the handler
returns a deferred marker) and lets bash_exec's watcher run the finalization
pipeline and write the JSON-RPC response when the command completes. These tests
are hermetic: they monkeypatch ``_write_jsonrpc`` to capture responses and never
touch real stdout.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import lemoncrow.pro.capabilities.tool_supervision.bash_exec as bx
from lemoncrow.gateway.adapters import mcp_server
from tests.helpers import init_store_at


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _bash_request(rid: Any, command: str, **args: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": "bash", "arguments": {"command": command, **args}},
    }


@pytest.fixture()
def bash_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_MEMORY_BACKEND", "sqlite")
    monkeypatch.delenv("LEMONCROW_SERVICE_URL", raising=False)
    # Default: deferral enabled (do not let an ambient value leak in).
    monkeypatch.delenv("LEMONCROW_MCP_DEFER_BASH", raising=False)
    mcp_server._ledger._current_ledger = None
    mcp_server._ledger._realtime_ctx = None
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. register_completion (bash_exec)                                          #
# --------------------------------------------------------------------------- #


def test_peek_and_poll_running_payload_over_budget_flag() -> None:
    """over_budget distinguishes a plain mid-flight peek (still well inside
    its window) from a command that has already burned through the timeout
    it was started with -- both peek_managed_command and poll_managed_command
    must agree on this, since the deferred path relies on either one."""
    started = bx.start_managed_command("sleep 2; echo done", timeout=10)
    sid = str(started["session_id"])
    try:
        # Freshly started with a generous timeout -- nowhere near its budget.
        assert bx.peek_managed_command(sid)["over_budget"] is False
        assert bx.poll_managed_command(sid)["over_budget"] is False
    finally:
        bx.poll_managed_command(sid, cancel=True)


def test_register_completion_running_fires_with_terminal_result() -> None:
    started = bx.start_managed_command("sleep 0.2; echo done", timeout=10)
    sid = str(started["session_id"])

    captured: dict[str, Any] = {}
    fired = threading.Event()

    def cb() -> None:
        captured["result"] = bx.poll_managed_command(sid)
        fired.set()

    assert bx.register_completion(sid, cb) is True
    assert fired.wait(5.0)
    assert captured["result"]["exit_code"] == 0
    assert captured["result"]["stdout"] == "done"


def test_register_completion_false_for_unknown_session() -> None:
    assert bx.register_completion("does-not-exist", lambda: None) is False


def test_register_completion_false_for_finished_session() -> None:
    started = bx.start_managed_command("echo hi", timeout=10)
    sid = str(started["session_id"])

    # The watcher keeps the finished session for a 300s grace window, so it is
    # finished-but-not-reaped here. register_completion must still refuse to arm.
    def _finished() -> bool:
        with bx._MANAGED_COMMANDS_LOCK:
            m = bx._MANAGED_COMMANDS.get(sid)
        return m is None or m.proc.poll() is not None

    assert _wait_for(_finished, 5.0)
    assert bx.register_completion(sid, lambda: None) is False


# --------------------------------------------------------------------------- #
# 2. Deferred end-to-end via _handle_and_write                                #
# --------------------------------------------------------------------------- #


def test_deferred_response_written_by_watcher_continuation(bash_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _capture(msg: dict[str, Any]) -> None:
        with lock:
            captured.append(msg)

    monkeypatch.setattr(mcp_server, "_write_jsonrpc", _capture)

    # A still-running command at register time -> the watcher continuation (not the
    # worker) writes the response after the command finishes.
    mcp_server._handle_and_write(_bash_request(42, "sleep 0.3; echo hi", timeout=30))

    # Nothing written synchronously: the worker handed control back deferred.
    with lock:
        assert captured == []

    assert _wait_for(lambda: len(captured) >= 1, 5.0)
    time.sleep(0.1)  # guard against a spurious second write
    with lock:
        assert len(captured) == 1
        resp = captured[0]
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 42
    text = resp["result"]["content"][0]["text"]
    assert "hi" in text
    assert "exit_code" not in text  # exit 0 renders without an exit_code line


def test_deferred_already_complete_race_writes_exactly_once(bash_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the already-complete race: refuse to arm, but only after the command
    # has finished so collect() yields the terminal result.
    def _fake_register(session_id: str, callback: Callable[[], None]) -> bool:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with bx._MANAGED_COMMANDS_LOCK:
                m = bx._MANAGED_COMMANDS.get(session_id)
            if m is None or m.proc.poll() is not None:
                break
            time.sleep(0.01)
        return False

    monkeypatch.setattr(bx, "register_completion", _fake_register)

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_server, "_write_jsonrpc", lambda msg: captured.append(msg))

    mcp_server._handle_and_write(_bash_request(99, "echo hi", timeout=30))

    # armed is False -> the continuation ran synchronously on the worker thread.
    assert len(captured) == 1
    assert captured[0]["id"] == 99
    assert "hi" in captured[0]["result"]["content"][0]["text"]


# --------------------------------------------------------------------------- #
# 3. Kill switch                                                              #
# --------------------------------------------------------------------------- #


def test_kill_switch_keeps_handler_synchronous(bash_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_DEFER_BASH", "0")

    # Even inside a deferral-capable context, the kill switch returns a plain dict.
    mcp_server._deferral_context.active = True
    try:
        result = mcp_server._run_bash_tool("echo hi", timeout=30)
    finally:
        mcp_server._deferral_context.active = False
    assert not isinstance(result, mcp_server._DeferredResult)
    assert isinstance(result, dict)
    assert result["exit_code"] == 0
    assert result["stdout"] == "hi"

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_server, "_write_jsonrpc", lambda msg: captured.append(msg))
    mcp_server._handle_and_write(_bash_request(7, "echo hi", timeout=30))
    # Written synchronously, before _handle_and_write returned.
    assert len(captured) == 1
    assert captured[0]["id"] == 7
    assert "hi" in captured[0]["result"]["content"][0]["text"]


# --------------------------------------------------------------------------- #
# 4. Parity: deferred result dict == synchronous result dict                  #
# --------------------------------------------------------------------------- #


def test_deferred_soft_deadline_returns_running_without_killing(bash_env: Path) -> None:
    """Regression for the background-service-timeout bug: a command genuinely
    still running past its requested `timeout` (e.g. a task starting a server
    and explicitly leaving it running) must not be killed, and must not block
    the MCP response indefinitely. The deferred continuation should resolve
    with status=running + session_id at the soft deadline while the process
    keeps going underneath."""
    mcp_server._deferral_context.active = True
    try:
        deferred = mcp_server._run_bash_tool("sleep 1.5; echo done", timeout=0)
        assert isinstance(deferred, mcp_server._DeferredResult)
        done = threading.Event()
        armed = deferred.register(done.set)
        assert armed is True
        assert done.wait(3.0)
        result = deferred.collect()
    finally:
        mcp_server._deferral_context.active = False

    assert result["status"] == "running"
    assert result["over_budget"] is True  # timeout=0 -- immediately past its soft budget
    # peek-sourced result also carries a tail marker line -- just check the handle line.
    rendered = mcp_server._render_bash_text(result)
    assert rendered.startswith(f"still running id={result['session_id']};")
    assert "[logs:" in rendered
    sid = result["session_id"]
    with bx._MANAGED_COMMANDS_LOCK:
        managed = bx._MANAGED_COMMANDS.get(sid)
    assert managed is not None
    assert managed.proc.poll() is None  # still alive -- the soft deadline did not kill it

    # Cleanup: let it finish and reap so no thread/temp file leaks past the test.
    managed.proc.wait(timeout=5)
    bx.poll_managed_command(sid)


def test_deferred_result_dict_matches_synchronous(
    bash_env: Path,
) -> None:
    command = "printf 'line1\\nline2\\n'"

    # Synchronous: no deferral context -> _run_bash_tool busy-polls to a dict.
    sync_result = mcp_server._run_bash_tool(command, timeout=30)
    assert isinstance(sync_result, dict)

    # Deferred: a deferral-capable context yields a _DeferredResult whose collect()
    # returns the terminal dict once the command finishes.
    mcp_server._deferral_context.active = True
    try:
        deferred = mcp_server._run_bash_tool(command, timeout=30)
        assert isinstance(deferred, mcp_server._DeferredResult)
        done = threading.Event()
        if deferred.register(done.set):
            assert done.wait(5.0)
        deferred_result = deferred.collect()
    finally:
        mcp_server._deferral_context.active = False
    assert isinstance(deferred_result, dict)

    # Ignore volatile timing and per-session log paths (each run gets its own
    # session_id, so the on-disk log file names legitimately differ).
    for volatile_key in ("duration_ms", "log_file", "log_file_stderr"):
        sync_result.pop(volatile_key, None)
        deferred_result.pop(volatile_key, None)
    assert sync_result == deferred_result
    assert deferred_result["exit_code"] == 0
    assert deferred_result["stdout"] == "line1\nline2"
    assert deferred_result["truncated"] is False


# --------------------------------------------------------------------------- #
# 5. More background-hang-bug patterns, exercised through the full MCP path   #
# --------------------------------------------------------------------------- #


def test_mailman_style_self_daemonizing_process_via_mcp_tool(bash_env: Path, tmp_path: Path) -> None:
    """terminal-bench mailman: `su ... -c \"mailman start\" | head -20 \\n sleep N;
    ps aux | grep mailman; ss ...` -- no explicit trailing `&` at all; the
    daemon self-detaches (double-fork) internally, then more foreground
    commands run after it. Exercised through the real MCP tool (not just
    bash_exec directly) since that's the actual path the agent hits."""
    daemon_script = tmp_path / "daemonize.py"
    daemon_script.write_text(
        "import os, sys, time\n"
        "if os.fork() > 0:\n"
        "    sys.exit(0)\n"
        "os.setsid()\n"
        "if os.fork() > 0:\n"
        "    sys.exit(0)\n"
        # A real daemon (mailman's included) redirects its fds away from
        # whatever piped it here before settling in -- otherwise the pipe this
        # ran through (here: `| head -5`) never sees EOF. Skipping this step
        # would hang the test for an unrelated reason (a bug in the stand-in
        # script, not in the fix under test).
        "devnull = os.open(os.devnull, os.O_RDWR)\n"
        "os.dup2(devnull, 0)\n"
        "os.dup2(devnull, 1)\n"
        "os.dup2(devnull, 2)\n"
        "time.sleep(30)\n"
    )
    command = (
        f"{sys.executable} {daemon_script} 2>&1 | head -5\n"
        'sleep 0.5; ps aux | grep -c daemonize; echo "mailman-style-ok"'
    )
    mcp_server._deferral_context.active = True
    try:
        start = time.monotonic()
        result = mcp_server._run_bash_tool(command, timeout=5)
        if isinstance(result, mcp_server._DeferredResult):
            done = threading.Event()
            armed = result.register(done.set)
            if armed:
                assert done.wait(10.0)
            result = result.collect()
        elapsed = time.monotonic() - start
    finally:
        mcp_server._deferral_context.active = False

    assert elapsed < 10.0  # did not hang past the bound
    assert "mailman-style-ok" in str(result.get("stdout"))


@pytest.mark.parametrize(
    ("command", "background", "expected_explicit"),
    [
        ("sleep 30", True, True),
        ("sleep 30 &", False, False),
    ],
)
def test_mcp_registration_tracks_only_bg_true_as_shutdown_persistent(
    bash_env: Path,
    command: str,
    background: bool,
    expected_explicit: bool,
) -> None:
    mcp_server._register_mcp_session()
    result: dict[str, Any] | None = None
    try:
        started = mcp_server._run_bash_tool(command, timeout=30, background=background)
        assert isinstance(started, dict)
        result = started
        registration = json.loads(mcp_server._mcp_session_file().read_text(encoding="utf-8"))
        records = registration["managed_bash"]
        assert len(records) == 1
        assert records[0]["session_id"] == started["session_id"]
        assert records[0]["explicit_background"] is expected_explicit
    finally:
        if result is not None:
            mcp_server._run_bash_tool(session_id=result["session_id"], action="kill")
        mcp_server._unregister_mcp_session()


def test_bare_trailing_ampersand_uses_fast_background_path(bash_env: Path, tmp_path: Path) -> None:
    """A command that ends in a bare `&` is auto-detected as background=True by
    _run_bash_tool and returns near-instantly via the explicit background path
    -- it should never even reach the join-bound fallback, background or not."""
    port_log = tmp_path / "bg.log"
    command = f"{sys.executable} -m http.server 0 --directory {tmp_path} > {port_log} 2>&1 &"
    start = time.monotonic()
    result = mcp_server._run_bash_tool(command, timeout=30)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # explicit background path -- essentially instant
    assert isinstance(result, dict)
    assert result["status"] == "running"
    assert result["session_id"]

    # Cleanup: cancel the managed wrapper (kills its process group).
    mcp_server._run_bash_tool(session_id=result["session_id"], action="kill")


def test_overrunning_foreground_command_does_not_block_another_run(bash_env: Path) -> None:
    mcp_server._deferral_context.active = True
    first_id = ""
    try:
        first_deferred = mcp_server._run_bash_tool("sleep 30", timeout=0)
        assert isinstance(first_deferred, mcp_server._DeferredResult)
        first_ready = threading.Event()
        assert first_deferred.register(first_ready.set) is True
        assert first_ready.wait(3.0)
        first = first_deferred.collect()
        first_id = str(first["session_id"])

        second_deferred = mcp_server._run_bash_tool("printf second", timeout=30)
        assert isinstance(second_deferred, mcp_server._DeferredResult)
        second_ready = threading.Event()
        if second_deferred.register(second_ready.set):
            assert second_ready.wait(3.0)
        second = second_deferred.collect()
        assert second["stdout"] == "second"
        with bx._MANAGED_COMMANDS_LOCK:
            assert bx._MANAGED_COMMANDS[first_id].proc.poll() is None
    finally:
        mcp_server._deferral_context.active = False
        if first_id:
            try:
                mcp_server._run_bash_tool(session_id=first_id, action="kill")
            except KeyError:
                pass


def test_soft_deadline_running_session_can_be_explicitly_cancelled(bash_env: Path) -> None:
    """The model's escape hatch for a soft-deadline 'running' handle: it can
    action="kill" by session_id, and the process actually dies -- 'don't
    kill automatically' does not mean 'can never be killed'."""
    mcp_server._deferral_context.active = True
    try:
        deferred = mcp_server._run_bash_tool("sleep 5; echo done", timeout=0)
        assert isinstance(deferred, mcp_server._DeferredResult)
        done = threading.Event()
        assert deferred.register(done.set) is True
        assert done.wait(3.0)
        result = deferred.collect()
    finally:
        mcp_server._deferral_context.active = False

    assert result["status"] == "running"
    sid = result["session_id"]
    with bx._MANAGED_COMMANDS_LOCK:
        managed = bx._MANAGED_COMMANDS.get(sid)
    assert managed is not None
    assert managed.proc.poll() is None  # confirmed still running before cancel

    cancelled = mcp_server._run_bash_tool(session_id=sid, action="kill")
    assert cancelled["status"] == "cancelled"
    time.sleep(0.2)
    assert managed.proc.poll() is not None  # actually dead now
