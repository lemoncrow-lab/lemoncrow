"""Checks for generated agent entrypoints and repo-legibility tooling."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle

ROOT = Path(__file__).resolve().parents[2]


def load_script(path: Path, module_name: str) -> object:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses (PEP 563 string annotations) resolve
    # cls.__module__ via sys.modules during class creation and crash with
    # AttributeError on None if the module isn't registered yet.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_managed_context_preserves_existing_content() -> None:
    module = load_script(ROOT / "scripts/sync_agent_context.py", "sync_agent_context")
    existing = "# Project rules\n\nKeep this instruction.\n"

    rendered = module.render_managed_context(existing)

    assert rendered.startswith(existing.rstrip() + "\n\n---\n\n")
    assert "Keep this instruction." in rendered
    assert rendered.count("<!-- LEMONCROW START -->") == 1
    assert rendered.count("<!-- LEMONCROW END -->") == 1


def test_claude_personas_preserve_non_git_grep_exception_after_regeneration() -> None:
    module = load_script(ROOT / "scripts/sync_agent_context.py", "sync_agent_context_claude")
    outputs = module.build_outputs()
    for role in ("code", "explore"):
        path = ROOT / "integrations/claude/plugin/agents" / f"{role}.md"
        rendered = outputs[path]
        assert rendered == path.read_text(encoding="utf-8")
        assert "Outside Git worktrees, Bash" in rendered
        assert "grep/rg" in rendered or "`grep`/`rg`" in rendered
        assert "Inside Git worktrees, use `mcp__lc__code_search`" in rendered


def test_managed_context_updates_only_existing_block() -> None:
    module = load_script(ROOT / "scripts/sync_agent_context.py", "sync_agent_context_update")
    existing = "# Project rules\n\n<!-- LEMONCROW START -->\nstale\n<!-- LEMONCROW END -->\n\nKeep this too.\n"

    rendered = module.render_managed_context(existing)

    assert rendered.startswith("# Project rules\n\n<!-- LEMONCROW START -->")
    assert rendered.endswith("<!-- LEMONCROW END -->\n\nKeep this too.\n")
    assert "stale" not in rendered


# Phrases that reason about, or name, the host's own built-in tools.
#
# Two earlier phrasings both backfired. "Host tools disabled" was a factual claim
# on hosts that disable nothing (Codex and OpenCode have no permission-deny
# mechanism; Cursor's hooks are inert unless LEMONCROW_CURSOR_ENFORCE=1) -- an
# agent that checks it and finds it false discounts the whole block. "Native X
# are fallback-only" was accurate but advertised the fallback, which is an
# invitation to take it. The closing line is now an unconditional directive
# naming only the lc tools.
_BANNED_NATIVE_PHRASES = (
    "Host tools disabled",
    "fallback-only",
    "Native Codex",
    "Native OpenCode",
    "built-ins stay",
    "apply_patch",
    "exec_command",
)


def _assert_no_native_names(rel: str) -> None:
    content = (ROOT / rel).read_text(encoding="utf-8")
    for phrase in _BANNED_NATIVE_PHRASES:
        assert phrase not in content, f"{rel} names a host built-in: {phrase!r}"
    assert "Always use lc" in content or "Always use LemonCrow" in content, f"{rel}: missing always-use-lc directive"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_for_health(port: int, process: subprocess.Popen[str], timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"service exited early with code {process.returncode}: {stderr}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and payload.get("status") == "ok":
                    return
        except (TimeoutError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"service on port {port} never became healthy")


def _post_json(port: int, path: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5.0) as response:
        return json.loads(response.read().decode("utf-8"))


@pytest.mark.slow  # Spawns two real HTTP service subprocesses in parallel
def test_live_services_can_run_in_parallel_with_isolated_roots(tmp_path: Path) -> None:
    pytest.importorskip("fastapi", reason="live service tests require the api extra")
    pytest.importorskip("uvicorn", reason="live service tests require uvicorn")

    root1 = tmp_path / "wt1" / ".lemoncrow-worktree"
    root2 = tmp_path / "wt2" / ".lemoncrow-worktree"
    port1 = _free_port()
    port2 = _free_port()
    while port2 == port1:
        port2 = _free_port()

    env1 = {
        **os.environ,
        "LEMONCROW_ROOT": str(root1),
        "LEMONCROW_REQUIRE_AUTH": "false",
    }
    env2 = {
        **os.environ,
        "LEMONCROW_ROOT": str(root2),
        "LEMONCROW_REQUIRE_AUTH": "false",
    }
    process1 = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "lemoncrow.core.service.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port1),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env1,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process2 = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "lemoncrow.core.service.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port2),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env2,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_health(port1, process1)
        _wait_for_health(port2, process2)

        trace1 = _post_json(
            port1,
            "/v1/traces",
            {"agent": "codex", "domain": "coding", "task": "wt1-trace", "status": "success"},
        )
        trace2 = _post_json(
            port2,
            "/v1/traces",
            {"agent": "codex", "domain": "coding", "task": "wt2-trace", "status": "success"},
        )

        store1 = build_sqlite_store_bundle(root1)
        store2 = build_sqlite_store_bundle(root2)
        stored1 = store1.history.get_trace(trace1["id"])
        stored2 = store2.history.get_trace(trace2["id"])

        assert stored1 is not None
        assert stored2 is not None
        assert stored1.task == "wt1-trace"
        assert stored2.task == "wt2-trace"
        assert store1.history.get_trace(trace2["id"]) is None
        assert store2.history.get_trace(trace1["id"]) is None
    finally:
        for process in (process1, process2):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
