from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities.context_compression.sleeptime import summarize_ledger
from atelier.core.capabilities.tool_supervision.compact_output import compact
from atelier.infra.internal_llm import ollama_client
from atelier.infra.storage.sqlite_store import SQLiteStore

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def _wait_for_health(base_url: str, process: subprocess.Popen[str], timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    last_error = "service never became healthy"
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"service exited early with code {process.returncode}: {stderr}")
        try:
            status, payload = _request_json("GET", f"{base_url}/health", timeout=1.0)
            if status == 200 and payload.get("status") == "ok":
                return
        except Exception as exc:  # pragma: no cover - best-effort polling
            last_error = str(exc)
        time.sleep(0.2)
    raise AssertionError(last_error)


@contextmanager
def _live_service(root: Path) -> Any:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "ATELIER_ROOT": str(root),
        "ATELIER_REQUIRE_AUTH": "false",
        "ATELIER_OLLAMA_MODEL": "llama3.2:latest",
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
        yield process, base_url
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _prepare_install_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "install-source"
    shutil.copytree(
        REPO_ROOT,
        source,
        symlinks=True,
        ignore_dangling_symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".atelier",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".coverage",
        ),
    )
    branch = "install-test"
    subprocess.run(["git", "-C", str(source), "init", "-b", branch], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "tests@atelier.local"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Atelier Tests"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "test snapshot"],
        check=True,
        capture_output=True,
        text=True,
    )
    return source, branch


def _start_container(name: str, tag: str, root: Path, port: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"{port}:8787",
            "-e",
            "ATELIER_ROOT=/app/.atelier",
            "-v",
            f"{root}:/app/.atelier",
            tag,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_real_ollama_model_backed_paths() -> None:
    os.environ["ATELIER_OLLAMA_MODEL"] = "llama3.2:latest"
    try:
        try:
            payload = ollama_client.chat(
                [{"role": "user", "content": 'Return JSON with key "procedural" set to true.'}],
                json_schema={"type": "object"},
            )
            assert isinstance(payload, dict)
            assert payload.get("procedural") is True

            summary = ollama_client.summarize(
                "Atelier recorded a trace, verified it with pytest, and persisted it to sqlite.",
                model="llama3.2:latest",
                max_tokens=128,
            )
            assert isinstance(summary, str)
            assert summary.strip()

            compacted = compact(
                ("tool output line with trace context\n" * 5000),
                content_type="tool_output",
                budget_tokens=128,
            )
            assert compacted.method == "ollama_summary"
            assert compacted.compacted_tokens < compacted.original_tokens

            chunks = summarize_ledger(
                [
                    {"kind": "tool_output", "summary": "pytest output", "payload": {"stdout": "ok"}},
                    {"kind": "tool_output", "summary": "trace saved", "payload": {"trace_id": "t-1"}},
                ],
                start_index=3,
            )
            assert chunks
            assert chunks[0].paraphrase.strip()
        except ollama_client.OllamaUnavailable as exc:
            pytest.skip(f"Ollama unavailable: {exc}")
    finally:
        os.environ.pop("ATELIER_OLLAMA_MODEL", None)


def test_live_service_concurrency_and_race_behavior(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    SQLiteStore(root).init()

    with _live_service(root) as (_, base_url):
        status, block = _request_json(
            "POST",
            f"{base_url}/v1/memory/blocks",
            {"agent_id": "atelier:code", "label": "race", "value": "v1", "actor": "test"},
        )
        assert status == 200
        version = int(block["version"])

        def _race_update(value: str) -> tuple[int, Any]:
            return _request_json(
                "POST",
                f"{base_url}/v1/memory/blocks",
                {
                    "agent_id": "atelier:code",
                    "label": "race",
                    "value": value,
                    "expected_version": version,
                    "actor": f"race:{value}",
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_race_update, ["winner-a", "winner-b"]))

        statuses = sorted(status for status, _ in results)
        assert statuses == [200, 409]

        status, final_block = _request_json(
            "GET",
            f"{base_url}/v1/memory/blocks?agent_id=atelier:code&label=race",
        )
        assert status == 200
        assert final_block["value"] in {"winner-a", "winner-b"}

        def _record_trace(index: int) -> tuple[int, Any]:
            return _request_json(
                "POST",
                f"{base_url}/v1/traces",
                {
                    "agent": "codex",
                    "domain": "coding",
                    "task": f"concurrent-trace-{index}",
                    "status": "success",
                },
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            trace_results = list(executor.map(_record_trace, range(24)))

        assert all(status == 200 for status, _ in trace_results)
        trace_ids = {payload["id"] for _, payload in trace_results}
        assert len(trace_ids) == 24

    traces = SQLiteStore(root).list_traces(limit=100)
    assert len([trace for trace in traces if trace.task.startswith("concurrent-trace-")]) == 24


def test_service_restart_preserves_traces_after_crash(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    SQLiteStore(root).init()

    with _live_service(root) as (process, base_url):
        status, payload = _request_json(
            "POST",
            f"{base_url}/v1/traces",
            {
                "agent": "codex",
                "domain": "coding",
                "task": "restart-durability",
                "status": "success",
            },
        )
        assert status == 200
        trace_id = str(payload["id"])
        process.kill()
        process.wait(timeout=5)

    with _live_service(root) as (_, base_url):
        status, restored = _request_json("GET", f"{base_url}/v1/traces/{trace_id}")
        assert status == 200
        assert restored["id"] == trace_id
        assert restored["task"] == "restart-durability"


def test_real_installer_runs_in_target_directory(tmp_path: Path) -> None:
    source, branch = _prepare_install_source(tmp_path)
    install_dir = tmp_path / "installed"
    bin_dir = tmp_path / "bin"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    env = {
        **os.environ,
        "ATELIER_REPO_URL": source.as_uri(),
        "ATELIER_REF": branch,
        "ATELIER_INSTALL_DIR": str(install_dir),
        "ATELIER_BIN_DIR": str(bin_dir),
        "ATELIER_TOOL_DIR": str(tmp_path / "uv-tools"),
        "ATELIER_NO_HOSTS": "1",
    }
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "install.sh")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (bin_dir / "atelier").exists()
    assert (bin_dir / "atelier-mcp").exists()

    help_result = subprocess.run(
        [str(bin_dir / "atelier"), "--help"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "Usage:" in help_result.stdout

    mcp_result = subprocess.run(
        [str(bin_dir / "atelier-mcp")],
        cwd=workspace,
        input="\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            ]
        )
        + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert mcp_result.returncode == 0, mcp_result.stderr
    responses = [json.loads(line) for line in mcp_result.stdout.splitlines() if line.strip()]
    tools_list = next(response for response in responses if response.get("id") == 2)
    tool_names = {tool["name"] for tool in tools_list["result"]["tools"]}
    assert {"context", "sql", "trace"}.issubset(tool_names)


def test_docker_deploy_load_latency_and_stability(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    SQLiteStore(root).init()

    tag = f"atelier-api-test:{uuid.uuid4().hex[:8]}"
    name = f"atelier-api-test-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    build = subprocess.run(
        ["docker", "build", "-f", str(REPO_ROOT / "Dockerfile.api"), "-t", tag, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    try:
        run = _start_container(name, tag, root, port)
        assert run.returncode == 0, run.stderr
        base_url = f"http://127.0.0.1:{port}"

        deadline = time.time() + 60
        healthy = False
        while time.time() < deadline:
            try:
                status, payload = _request_json("GET", f"{base_url}/health", timeout=1.0)
                if status == 200 and payload.get("status") == "ok":
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(1)
        assert healthy, "dockerized atelier service never became healthy"

        latencies: list[float] = []

        def _reasoning(index: int) -> tuple[int, Any, float]:
            started = time.perf_counter()
            status, payload = _request_json(
                "POST",
                f"{base_url}/v1/reasoning/context",
                {"task": f"docker-load-{index}", "domain": "coding"},
                timeout=15.0,
            )
            return status, payload, time.perf_counter() - started

        for wave in range(3):
            with ThreadPoolExecutor(max_workers=6) as executor:
                batch = list(executor.map(_reasoning, range(wave * 12, (wave + 1) * 12)))
            assert all(status == 200 for status, _, _ in batch)
            assert all("context" in payload for _, payload, _ in batch)
            latencies.extend(duration for _, _, duration in batch)
            status, payload = _request_json("GET", f"{base_url}/ready", timeout=2.0)
            assert status == 200
            assert payload["status"] in {"ok", "degraded"}
            time.sleep(1)

        status, payload = _request_json("GET", f"{base_url}/health", timeout=2.0)
        assert status == 200
        assert payload["status"] == "ok"
        assert _p95(latencies) < 8.0
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, check=False)
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, text=True, check=False)
