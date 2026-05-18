"""Checks for generated agent entrypoints and repo-legibility tooling."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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


def test_worktree_env_is_stable_for_the_same_path(tmp_path: Path) -> None:
    module = load_script(ROOT / "scripts/worktree_env.py", "worktree_env")
    env1 = module.build_env(tmp_path / "feature-a")
    env2 = module.build_env(tmp_path / "feature-a")
    env3 = module.build_env(tmp_path / "feature-b")

    assert env1 == env2
    assert env1["ATELIER_SERVICE_PORT"] != env3["ATELIER_SERVICE_PORT"]
    assert env1["ATELIER_FRONTEND_PORT"] != env3["ATELIER_FRONTEND_PORT"]


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
