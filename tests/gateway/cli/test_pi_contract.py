from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.gateway.cli import pi_host
from lemoncrow.gateway.cli.coding_engine import EngineLaunch, _build_engine_launch


@pytest.fixture
def pi_contract_binary() -> Path:
    raw = os.environ.get("LEMONCROW_PI_CONTRACT_BIN", "").strip()
    if not raw:
        pytest.skip("set LEMONCROW_PI_CONTRACT_BIN to run the real pinned-Pi contract")
    binary = Path(raw).expanduser().resolve()
    pi_host.validate_host_binary(binary)
    return binary


@contextmanager
def _capture_server(*, tool_attack_marker: Path | None = None) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            payload["__path"] = self.path
            payload["__authorization"] = self.headers.get("authorization")
            requests.append(payload)

            if tool_attack_marker is None:
                chunks = [
                    {
                        "id": "chatcmpl-contract",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "zen/big-pickle",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "PI_CONTRACT_OK"},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-contract",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "zen/big-pickle",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
            else:
                chunks = [
                    {
                        "id": "chatcmpl-tool-attack",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "zen/big-pickle",
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    },
                    {
                        "id": "chatcmpl-tool-attack",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "zen/big-pickle",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-attack",
                                            "type": "function",
                                            "function": {
                                                "name": "bash",
                                                "arguments": json.dumps({"command": f"touch {tool_attack_marker}"}),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-tool-attack",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "zen/big-pickle",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    },
                ]

            body = ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _launch(binary: Path, tmp_path: Path, base_url: str, *, prompt: str = "contract prompt") -> EngineLaunch:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "AGENTS.md").write_text("PROJECT_PROMPT_SENTINEL\n", encoding="utf-8")
    project_extension = project / ".pi" / "extensions" / "project-extension.mjs"
    project_extension.parent.mkdir(parents=True, exist_ok=True)
    project_extension.write_text('throw new Error("PROJECT_EXTENSION_SENTINEL");\n', encoding="utf-8")
    return _build_engine_launch(
        engine="pi",
        executable=str(binary),
        base_url=base_url,
        token="contract-secret",
        store_root=tmp_path / "store",
        project_root=project,
        empty_mcp_config=tmp_path / "empty.json",
        budget="balanced",
        prompt=prompt,
        resume=None,
        base_env=dict(os.environ),
    )


def _run(launch: EngineLaunch, project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        launch.command,
        cwd=project,
        env=launch.env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_real_pi_print_mode_sends_only_sanitized_gateway_payload(tmp_path: Path, pi_contract_binary: Path) -> None:
    with _capture_server() as (base_url, requests):
        launch = _launch(pi_contract_binary, tmp_path, base_url, prompt="Reply exactly PI_CONTRACT_OK")
        completed = _run(launch, tmp_path / "project")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "PI_CONTRACT_OK"
    assert len(requests) == 1
    payload = requests[0]
    assert payload["__path"] == "/v1/chat/completions"
    assert payload["__authorization"] == "Bearer contract-secret"
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "parallel_tool_calls" not in payload
    assert {message["role"] for message in payload["messages"]} <= {"user", "assistant"}
    rendered = json.dumps(payload)
    assert "PROJECT_PROMPT_SENTINEL" not in rendered
    assert "PROJECT_EXTENSION_SENTINEL" not in rendered
    assert "LemonCrow managed frontend" not in rendered


def test_real_pi_tool_call_is_aborted_before_execution(tmp_path: Path, pi_contract_binary: Path) -> None:
    marker = tmp_path / "tool-executed"
    with _capture_server(tool_attack_marker=marker) as (base_url, requests):
        launch = _launch(pi_contract_binary, tmp_path, base_url, prompt="try a tool")
        completed = _run(launch, tmp_path / "project")

    assert completed.returncode != 0
    assert "aborted" in completed.stderr.lower()
    assert len(requests) == 1, "managed Pi must not retry the outer model after a tool call"
    assert not marker.exists(), "Pi executed a provider-requested tool"


@pytest.mark.parametrize("failure", ["missing-token", "missing-extension", "invalid-extension"])
def test_real_pi_startup_failures_make_no_provider_request(
    tmp_path: Path, pi_contract_binary: Path, failure: str
) -> None:
    with _capture_server() as (base_url, requests):
        launch = _launch(pi_contract_binary, tmp_path, base_url)
        env = dict(launch.env)
        command = list(launch.command)
        if failure == "missing-token":
            env.pop("LEMONCROW_PI_GATEWAY_TOKEN", None)
        else:
            extension_index = command.index("-e") + 1
            extension = tmp_path / ("missing.mjs" if failure == "missing-extension" else "invalid.mjs")
            if failure == "invalid-extension":
                extension.write_text("export default function managedPi( {\n", encoding="utf-8")
            command[extension_index] = str(extension)
        completed = subprocess.run(
            command,
            cwd=tmp_path / "project",
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    assert completed.returncode != 0
    assert requests == []
