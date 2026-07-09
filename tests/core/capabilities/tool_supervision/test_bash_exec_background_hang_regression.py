"""Regression tests for the background-service-timeout bug (terminal-bench
kv-store-grpc / pypi-server / mailman / install-windows-3.11): a task backgrounds
a long-running service and checks on it in the same bash call. The wrapping
command exits quickly, but a detached descendant can still hold a duplicate of
the output pipe open, so poll_managed_command's reader-thread join used to
block forever even though the command we actually ran was long dead -- eating
the agent's entire task budget.

Fix: poll_managed_command / the watcher's own reap both bound that join
(bx._READER_JOIN_GRACE_S) and ship whatever's captured so far instead of
hanging. These tests replicate the exact command *shapes* from the failing
terminal-bench tasks (grpc/pypi-server/websockify swapped for `python3 -m
http.server` as a safe, always-available stand-in -- the bug is about pipe
file-descriptor inheritance, not about which server binary is running) plus a
few structural variations of our own.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import atelier.core.capabilities.tool_supervision.bash_exec as bx

_BOUND_S = bx._READER_JOIN_GRACE_S + 3.0  # generous margin over the join grace


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _poll_until_done(session_id: str, timeout_s: float = 10.0) -> dict[str, object]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = bx.poll_managed_command(session_id)
        if result["status"] != "running":
            return result
        time.sleep(0.02)
    raise AssertionError("managed command did not finish in time")


def _kill_stray(pattern: str) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(["pkill", "-f", pattern], check=False, timeout=5)


@pytest.mark.slow
def test_kv_store_grpc_pattern_start_server_and_check_port(tmp_path: Path) -> None:
    """terminal-bench kv-store-grpc: `nohup <server> > log 2>&1 & \\n sleep N;
    cat log; ss | grep <port>` -- backgrounds a grpc server, then inline-checks
    its log and listening port. This exact shape hung for the task's entire
    900s budget in production (reward still came back 1 -- the server was up --
    but the agent phase itself was recorded as AgentTimeoutError)."""
    port = _free_port()
    log = tmp_path / "server.log"
    probe_script = tmp_path / "probe_port.py"
    probe_script.write_text(
        "import socket, sys\n"
        "port = int(sys.argv[1])\n"
        "socket.create_connection(('127.0.0.1', port), 2).close()\n"
        "print(port)\n",
        encoding="utf-8",
    )
    command = (
        f"cd {tmp_path} && nohup {sys.executable} -m http.server {port} > {log} 2>&1 &\n"
        f'sleep 1; echo "log:"; cat {log}; echo "---port---"; {sys.executable} {probe_script} {port}'
    )
    started = bx.start_managed_command(command, timeout=5)
    sid = str(started["session_id"])
    try:
        start = time.monotonic()
        result = _poll_until_done(sid, timeout_s=_BOUND_S + 2.0)
        elapsed = time.monotonic() - start
        assert elapsed < _BOUND_S + 2.0
        assert str(port) in str(result["stdout"])
    finally:
        _kill_stray(f"http.server {port}")


@pytest.mark.slow
def test_pypi_server_pattern_background_then_tail_log(tmp_path: Path) -> None:
    """terminal-bench pypi-server: `nohup pypi-server run -p PORT dist/ >log
    2>&1 & \\n sleep 3; ...` -- same shape, different sleep/check timing."""
    port = _free_port()
    log = tmp_path / "pypi.log"
    command = (
        f"nohup {sys.executable} -m http.server {port} --directory {tmp_path} >{log} 2>&1 &\n"
        f"sleep 3; tail -5 {log}; echo tail-ok"
    )
    started = bx.start_managed_command(command, timeout=6)
    sid = str(started["session_id"])
    try:
        start = time.monotonic()
        result = _poll_until_done(sid, timeout_s=_BOUND_S + 4.0)
        elapsed = time.monotonic() - start
        assert elapsed < _BOUND_S + 4.0
        assert "tail-ok" in str(result["stdout"])
    finally:
        _kill_stray(f"http.server {port}")


@pytest.mark.slow
def test_install_windows_websockify_pattern_process_check(tmp_path: Path) -> None:
    """terminal-bench install-windows-3.11: `nohup websockify ... &` then a
    process-list check (rather than a port check) to confirm it's up."""
    port = _free_port()
    log = tmp_path / "websockify.log"
    marker = f"stand_in_websockify_{port}"
    command = (
        f"nohup {sys.executable} -m http.server {port} --directory {tmp_path} >{log} 2>&1 &\n"
        f'sleep 1; ps aux | grep -c "http.server {port}"; echo "{marker}"'
    )
    started = bx.start_managed_command(command, timeout=5)
    sid = str(started["session_id"])
    try:
        start = time.monotonic()
        result = _poll_until_done(sid, timeout_s=_BOUND_S + 2.0)
        elapsed = time.monotonic() - start
        assert elapsed < _BOUND_S + 2.0
        assert marker in str(result["stdout"])
    finally:
        _kill_stray(f"http.server {port}")


def test_explicitly_wedged_pipe_via_lingering_fork_is_bounded(tmp_path: Path) -> None:
    """Deterministic, environment-independent reproduction: a forked grandchild
    inherits the wrapping process's stdout pipe and holds it open for 3s after
    its parent (and the wrapping bash command) already exited. No networking,
    no OS-specific nohup quirks -- this isolates the exact fd-inheritance shape
    of the bug and must still resolve within the bounded join, not the full 3s."""
    wedge_script = tmp_path / "wedge.py"
    wedge_script.write_text(
        "import os, sys, time\nif os.fork() == 0:\n    time.sleep(3)\n    sys.exit(0)\nsys.exit(0)\n"
    )
    command = f"{sys.executable} {wedge_script} &\nsleep 0.2; echo main-done"
    started = bx.start_managed_command(command, timeout=10)
    sid = str(started["session_id"])
    start = time.monotonic()
    result = _poll_until_done(sid, timeout_s=_BOUND_S)
    elapsed = time.monotonic() - start
    assert elapsed < _BOUND_S  # bounded well under the grandchild's 3s hold
    assert "main-done" in str(result["stdout"])
    assert "still be running" in str(result["stderr"])


@pytest.mark.slow
def test_background_with_disown_returns_promptly(tmp_path: Path) -> None:
    """A task that explicitly disowns the backgrounded job (common advice for
    'don't let this get killed when the shell exits') must not change the
    hang behavior -- still bounded, not blocked on the disowned process."""
    port = _free_port()
    log = tmp_path / "disowned.log"
    command = (
        f"{sys.executable} -m http.server {port} --directory {tmp_path} >{log} 2>&1 &\n"
        "disown -a 2>/dev/null; sleep 0.5; echo disown-done"
    )
    started = bx.start_managed_command(command, timeout=5)
    sid = str(started["session_id"])
    try:
        start = time.monotonic()
        result = _poll_until_done(sid, timeout_s=_BOUND_S + 1.0)
        elapsed = time.monotonic() - start
        assert elapsed < _BOUND_S + 1.0
        assert "disown-done" in str(result["stdout"])
    finally:
        _kill_stray(f"http.server {port}")


@pytest.mark.slow
def test_two_backgrounded_processes_in_one_command(tmp_path: Path) -> None:
    """Multiple independent backgrounded descendants (not just one) must all be
    tolerated -- the bounded join must not need every wedging descendant to
    exit, only the grace window to elapse."""
    port_a, port_b = _free_port(), _free_port()
    log_a, log_b = tmp_path / "a.log", tmp_path / "b.log"
    command = (
        f"{sys.executable} -m http.server {port_a} --directory {tmp_path} >{log_a} 2>&1 &\n"
        f"{sys.executable} -m http.server {port_b} --directory {tmp_path} >{log_b} 2>&1 &\n"
        "sleep 1; echo both-started"
    )
    started = bx.start_managed_command(command, timeout=6)
    sid = str(started["session_id"])
    try:
        start = time.monotonic()
        result = _poll_until_done(sid, timeout_s=_BOUND_S + 2.0)
        elapsed = time.monotonic() - start
        assert elapsed < _BOUND_S + 2.0
        assert "both-started" in str(result["stdout"])
    finally:
        _kill_stray(f"http.server {port_a}")
        _kill_stray(f"http.server {port_b}")


@pytest.mark.slow
def test_nested_subshell_background_pattern(tmp_path: Path) -> None:
    """A backgrounded job launched from inside a subshell `( ... ) &` -- a
    slightly different fork/exec path than a plain `cmd &` -- must be equally
    bounded, not just the top-level-command case."""
    port = _free_port()
    log = tmp_path / "subshell.log"
    command = (
        f"({sys.executable} -m http.server {port} --directory {tmp_path} >{log} 2>&1 &)\nsleep 0.5; echo subshell-done"
    )
    started = bx.start_managed_command(command, timeout=5)
    sid = str(started["session_id"])
    try:
        start = time.monotonic()
        result = _poll_until_done(sid, timeout_s=_BOUND_S + 1.0)
        elapsed = time.monotonic() - start
        assert elapsed < _BOUND_S + 1.0
        assert "subshell-done" in str(result["stdout"])
    finally:
        _kill_stray(f"http.server {port}")


def test_grandchild_with_extra_fork_after_nohup_is_bounded(tmp_path: Path) -> None:
    """A server that itself forks an extra worker (nohup'd process spawns a
    grandchild of its own) -- the bug isn't limited to a single generation of
    descendant; a deeper fork tree must be tolerated the same way."""
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "import os, sys, time\nif os.fork() == 0:\n    time.sleep(2)\n    sys.exit(0)\ntime.sleep(2)\n"
    )
    command = f"nohup {sys.executable} {worker_script} > /dev/null 2>&1 &\nsleep 0.3; echo worker-launched"
    started = bx.start_managed_command(command, timeout=5)
    sid = str(started["session_id"])
    start = time.monotonic()
    result = _poll_until_done(sid, timeout_s=_BOUND_S + 1.0)
    elapsed = time.monotonic() - start
    assert elapsed < _BOUND_S + 1.0
    assert "worker-launched" in str(result["stdout"])


def test_normal_foreground_command_unaffected_by_the_fix(tmp_path: Path) -> None:
    """Regression guard: an ordinary command with no backgrounding at all must
    return immediately with no wedged-reader note -- the bound must never add
    latency or noise to the common case."""
    started = bx.start_managed_command("echo hello world", timeout=10)
    sid = str(started["session_id"])
    start = time.monotonic()
    result = _poll_until_done(sid, timeout_s=5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # nowhere near the join-grace bound -- genuinely instant
    assert result["stdout"] == "hello world"
    assert "still be running" not in str(result["stderr"])
