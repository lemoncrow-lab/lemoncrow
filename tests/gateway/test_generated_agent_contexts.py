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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def test_copilot_instructions_has_the_compact_managed_block() -> None:
    block_start = "<!-- LEMONCROW START -->"
    block_end = "<!-- LEMONCROW END -->"
    source = (ROOT / "integrations/AGENTS.lemoncrow.md").read_text(encoding="utf-8").strip()
    content = (ROOT / ".github/copilot-instructions.md").read_text(encoding="utf-8")
    _, found_start, remainder = content.partition(block_start)
    managed_body, found_end, _ = remainder.partition(block_end)

    assert found_start and found_end
    assert managed_body.strip() == source
    assert "docs/architecture" not in content
    assert len(managed_body.splitlines()) <= 20, "the managed Copilot block should stay compact"


def test_copilot_distribution_instructions_match_agent_guide() -> None:
    source = (ROOT / "integrations/AGENTS.lemoncrow.md").read_text(encoding="utf-8")
    copilot = (ROOT / "integrations/copilot/COPILOT_INSTRUCTIONS.lemoncrow.md").read_text(encoding="utf-8")

    assert copilot == source


def test_root_agents_md_has_a_compact_managed_block() -> None:
    block_start = "<!-- LEMONCROW START -->"
    block_end = "<!-- LEMONCROW END -->"
    source = (ROOT / "integrations/AGENTS.lemoncrow.md").read_text(encoding="utf-8").strip()
    content = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    _, found_start, remainder = content.partition(block_start)
    managed_body, found_end, _ = remainder.partition(block_end)

    assert found_start and found_end
    assert managed_body.strip() == source
    assert len(managed_body.splitlines()) <= 20, "the managed AGENTS.md block should stay compact"


def test_managed_context_preserves_existing_content() -> None:
    module = load_script(ROOT / "scripts/sync_agent_context.py", "sync_agent_context")
    existing = "# Project rules\n\nKeep this instruction.\n"

    rendered = module.render_managed_context(existing)

    assert rendered.startswith(existing.rstrip() + "\n\n---\n\n")
    assert "Keep this instruction." in rendered
    assert rendered.count("<!-- LEMONCROW START -->") == 1
    assert rendered.count("<!-- LEMONCROW END -->") == 1


def test_managed_context_updates_only_existing_block() -> None:
    module = load_script(ROOT / "scripts/sync_agent_context.py", "sync_agent_context_update")
    existing = "# Project rules\n\n<!-- LEMONCROW START -->\nstale\n<!-- LEMONCROW END -->\n\nKeep this too.\n"

    rendered = module.render_managed_context(existing)

    assert rendered.startswith("# Project rules\n\n<!-- LEMONCROW START -->")
    assert rendered.endswith("<!-- LEMONCROW END -->\n\nKeep this too.\n")
    assert "stale" not in rendered


def test_opencode_agent_has_host_specific_tool_policy() -> None:
    content = (ROOT / "integrations/opencode/agents/code.md").read_text(encoding="utf-8")
    # `context` is in HIDDEN_LLM_TOOLS -- never advertised to any host's model,
    # OpenCode included -- so it must never be named as something to call.
    assert "lemoncrow_context" not in content
    # OpenCode keeps the same core workflow bullets as Codex/Copilot/Cursor
    # (tool-discipline.md) -- only Claude drops them (its equivalent arrives via
    # the MCP server's `instructions` field instead).
    assert "One search → one bulk edit." in content
    assert "Known path → `lc_read`; `lc_bash` = execution only." in content
    assert "Batch independent calls." in content
    assert "Large output → a file, never prose." in content
    # The old "use lc_code_search/read/edit/bash instead" bullet was dropped
    # as pure duplication of the shared workflow bullets above.
    assert "OpenCode host" not in content
    assert "Native OpenCode `read`, `grep`, `bash`, `edit`, and `patch` are fallback-only" in content
    # Regression: native OpenCode tool names must stay bare (they name OpenCode's
    # own tools, not LemonCrow's) while the "use lc: ..." clause right after
    # them must be prefixed -- both directions have broken before.
    assert "`lc_read`, `grep`, `lc_bash`" not in content
    assert "— use lc: `lc_bash`, `lc_read`, `lc_edit`, `lc_code_search`." in content


def test_codex_skill_names_its_own_native_tools_as_disallowed() -> None:
    # Codex's real native tool-call names (apply_patch/exec_command) must be
    # named explicitly -- the generic "Host tools disabled" phrasing every
    # fully-disabled host keeps doesn't apply to Codex (no permission-deny
    # mechanism exists; see plugin_runtime._codex_native_tool_replacement).
    code_skill = (ROOT / "integrations/codex/plugin/skills/code/SKILL.md").read_text(encoding="utf-8")
    assert (
        "Native Codex `apply_patch` and `exec_command` are disallowed — use lc: "
        "`lc.bash`, `lc.read`, `lc.edit`, `lc.code_search`."
    ) in code_skill
    # Read-only roles have no edit tool to name apply_patch as a fallback from --
    # only exec_command applies, singular verb.
    explore_skill = (ROOT / "integrations/codex/plugin/skills/explore/SKILL.md").read_text(encoding="utf-8")
    assert "Native Codex `exec_command` is disallowed" in explore_skill
    assert "apply_patch" not in explore_skill


def test_copilot_tasks_include_worktree_and_runtime_evidence() -> None:
    data = json.loads((ROOT / "integrations/copilot/tasks.json").read_text(encoding="utf-8"))
    labels = {item.get("label") for item in data.get("tasks", [])}
    assert "LemonCrow: Worktree Bootstrap" in labels
    assert "LemonCrow: Runtime Evidence" in labels


def test_makefile_prefers_worktree_env_for_stack_commands() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "if [ -f .env.worktree ]; then set -a; . ./.env.worktree; set +a; fi" in makefile
    assert "stack start" in makefile
    assert "stack stop" in makefile


def test_worktree_env_is_stable_for_the_same_path(tmp_path: Path) -> None:
    module = load_script(ROOT / "scripts/worktree_env.py", "worktree_env")
    env1 = module.build_env(tmp_path / "feature-a")
    env2 = module.build_env(tmp_path / "feature-a")
    env3 = module.build_env(tmp_path / "feature-b")

    assert env1 == env2
    assert env1["LEMONCROW_SERVICE_PORT"] != env3["LEMONCROW_SERVICE_PORT"]
    assert env1["LEMONCROW_FRONTEND_PORT"] != env3["LEMONCROW_FRONTEND_PORT"]


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
