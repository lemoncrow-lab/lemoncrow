"""Checks for generated agent entrypoints and repo-legibility tooling."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from atelier.infra.storage.sqlite_store import SQLiteStore

ROOT = Path(__file__).resolve().parents[2]


def load_script(path: Path, module_name: str) -> object:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_agent_contexts_are_current() -> None:
    subprocess.run(
        [sys.executable, "scripts/sync_agent_context.py", "--check"],
        cwd=ROOT,
        check=True,
    )


def test_root_entrypoints_stay_thin_and_link_to_live_docs() -> None:
    for rel in ("AGENTS.md", "GEMINI.md", ".github/copilot-instructions.md"):
        path = ROOT / rel
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 80, f"{rel} should stay a thin entrypoint"
        assert "docs/agent-os/README.md" in path.read_text(encoding="utf-8")


def test_copilot_tasks_include_worktree_and_runtime_evidence() -> None:
    data = json.loads((ROOT / "integrations/copilot/tasks.json").read_text(encoding="utf-8"))
    labels = {item.get("label") for item in data.get("tasks", [])}
    assert "Atelier: Worktree Bootstrap" in labels
    assert "Atelier: Runtime Evidence" in labels


def test_makefile_prefers_worktree_env_for_stack_commands() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "COMPOSE_ENV_FILE := $(if $(wildcard .env.worktree),--env-file .env.worktree,)" in makefile
    assert "$(DOCKER_COMPOSE) up --build -d" in makefile
    assert "$(DOCKER_COMPOSE) down" in makefile


def test_worktree_env_is_stable_for_the_same_path(tmp_path: Path) -> None:
    module = load_script(ROOT / "scripts/worktree_env.py", "worktree_env")
    env1 = module.build_env(tmp_path / "feature-a")
    env2 = module.build_env(tmp_path / "feature-a")
    env3 = module.build_env(tmp_path / "feature-b")

    assert env1 == env2
    assert env1["ATELIER_SERVICE_PORT"] != env3["ATELIER_SERVICE_PORT"]
    assert env1["ATELIER_FRONTEND_PORT"] != env3["ATELIER_FRONTEND_PORT"]


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
        except Exception:
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

    root1 = tmp_path / "wt1" / ".atelier-worktree"
    root2 = tmp_path / "wt2" / ".atelier-worktree"
    port1 = _free_port()
    port2 = _free_port()
    while port2 == port1:
        port2 = _free_port()

    env1 = {
        **os.environ,
        "ATELIER_ROOT": str(root1),
        "ATELIER_REQUIRE_AUTH": "false",
        "ATELIER_EMBEDDER": "null",
    }
    env2 = {
        **os.environ,
        "ATELIER_ROOT": str(root2),
        "ATELIER_REQUIRE_AUTH": "false",
        "ATELIER_EMBEDDER": "null",
    }
    process1 = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "atelier.core.service.api:create_app",
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
            "atelier.core.service.api:create_app",
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

        store1 = SQLiteStore(root1)
        store2 = SQLiteStore(root2)
        stored1 = store1.get_trace(trace1["id"])
        stored2 = store2.get_trace(trace2["id"])

        assert stored1 is not None
        assert stored2 is not None
        assert stored1.task == "wt1-trace"
        assert stored2.task == "wt2-trace"
        assert store1.get_trace(trace2["id"]) is None
        assert store2.get_trace(trace1["id"]) is None
    finally:
        for process in (process1, process2):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class _EvidenceHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            body = {"status": "ok"}
        elif self.path.startswith("/analytics/summary"):
            body = {"sessions": 1}
        elif self.path == "/v1/traces":
            body = {"items": []}
        else:
            body = {"error": "not-found"}
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_runtime_evidence_writes_expected_payload(tmp_path: Path) -> None:
    server = HTTPServer(("127.0.0.1", 0), _EvidenceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = tmp_path / "evidence.json"
        subprocess.run(
            [
                sys.executable,
                "scripts/runtime_evidence.py",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            check=True,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert payload["health"]["ok"] is True
    assert payload["analytics_summary"]["body"] == {"sessions": 1}
    assert payload["traces"]["body"] == {"items": []}
