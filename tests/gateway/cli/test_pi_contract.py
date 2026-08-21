from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import threading
import time
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


def _launch(binary: Path, tmp_path: Path, base_url: str, *, prompt: str | None = "contract prompt") -> EngineLaunch:
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


class _RpcClient:
    def __init__(self, launch: EngineLaunch, project: Path) -> None:
        self.process = subprocess.Popen(
            [*launch.command, "--mode", "rpc"],
            cwd=project,
            env=launch.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._backlog: list[dict[str, Any]] = []
        self._counter = 0
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        for line in self._stdout:
            stripped = line.strip()
            if not stripped:
                continue
            self._queue.put(json.loads(stripped))

    def _take(self, predicate, *, timeout: float = 8.0) -> dict[str, Any]:
        for index, item in enumerate(self._backlog):
            if predicate(item):
                return self._backlog.pop(index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                item = self._queue.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if predicate(item):
                return item
            self._backlog.append(item)
        raise AssertionError(f"timed out waiting for Pi RPC event; backlog={self._backlog[-8:]!r}")

    def command(self, command_type: str, **payload: Any) -> dict[str, Any]:
        self._counter += 1
        request_id = f"rpc-{self._counter}"
        request = {"id": request_id, "type": command_type, **payload}
        self._stdin.write(json.dumps(request) + "\n")
        self._stdin.flush()
        return self._take(lambda item: item.get("type") == "response" and item.get("id") == request_id)

    def wait_event(self, event_type: str, *, timeout: float = 8.0) -> dict[str, Any]:
        return self._take(lambda item: item.get("type") == event_type, timeout=timeout)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._reader.join(timeout=2)

    def __enter__(self) -> _RpcClient:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


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


def test_real_pi_multimodal_prompt_reaches_gateway_when_model_allows_images(
    tmp_path: Path, pi_contract_binary: Path
) -> None:
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    )
    with _capture_server() as (base_url, requests):
        launch = _launch(pi_contract_binary, tmp_path, base_url, prompt=None)
        env = dict(launch.env)
        env["LEMONCROW_PI_MODELS"] = json.dumps(
            {
                "vision-test": {
                    "name": "vision test",
                    "input": ["text", "image"],
                    "limit": {"context": 32000, "output": 1000},
                }
            }
        )
        command = list(launch.command)
        model_index = command.index("--model") + 1
        command[model_index] = "vision-test"
        command.extend(["-p", f"@{image_path}", "inspect image"])
        completed = subprocess.run(
            command,
            cwd=tmp_path / "project",
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    assert completed.returncode == 0, completed.stderr
    assert len(requests) == 1
    content = requests[0]["messages"][-1]["content"]
    assert any(part.get("type") == "image_url" for part in content)
    image_part = next(part for part in content if part.get("type") == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_real_pi_rpc_stays_scoped_to_lc_models_and_managed_state(tmp_path: Path, pi_contract_binary: Path) -> None:
    with _capture_server() as (base_url, requests):
        launch = _launch(pi_contract_binary, tmp_path, base_url, prompt=None)
        with _RpcClient(launch, tmp_path / "project") as rpc:
            models_response = rpc.command("get_available_models")
            assert models_response["success"] is True
            models = models_response["data"]["models"]
            assert models
            assert {model["provider"] for model in models} == {"lc"}
            assert all(model["baseUrl"] == f"{base_url}/v1" for model in models)

            escaped = rpc.command("set_model", provider="openai", modelId="gpt-4o")
            assert escaped["success"] is False
            assert "Model not found" in escaped["error"]

            chosen = models[-1]
            switched = rpc.command("set_model", provider="lc", modelId=chosen["id"])
            assert switched["success"] is True
            assert switched["data"]["provider"] == "lc"
            assert switched["data"]["id"] == chosen["id"]

            assert rpc.command("prompt", message="model fidelity")["success"] is True
            rpc.wait_event("agent_end")
            assert requests[-1]["model"] == chosen["id"]

            state = rpc.command("get_state")
            assert state["success"] is True
            assert state["data"]["model"]["provider"] == "lc"
            assert state["data"]["model"]["id"] == chosen["id"]
            assert state["data"]["thinkingLevel"] == "off"
            assert state["data"]["autoCompactionEnabled"] is False
            assert state["data"]["isCompacting"] is False


def test_real_pi_rpc_session_tree_and_fork_are_deterministic(tmp_path: Path, pi_contract_binary: Path) -> None:
    with _capture_server() as (base_url, _requests):
        launch = _launch(pi_contract_binary, tmp_path, base_url, prompt=None)
        with _RpcClient(launch, tmp_path / "project") as rpc:
            assert rpc.command("prompt", message="first")["success"] is True
            rpc.wait_event("agent_end")
            first = rpc.command("get_entries")
            first_entries = first["data"]["entries"]
            first_user = next(
                entry
                for entry in first_entries
                if entry.get("type") == "message" and entry.get("message", {}).get("role") == "user"
            )
            first_state = rpc.command("get_state")["data"]
            first_session = first_state["sessionFile"]

            assert rpc.command("prompt", message="second")["success"] is True
            rpc.wait_event("agent_end")
            second = rpc.command("get_entries")
            assert second["data"]["leafId"] != first["data"]["leafId"]

            forked = rpc.command("fork", entryId=first_user["id"])
            assert forked == {
                **{key: forked[key] for key in ("id", "type", "command")},
                "success": True,
                "data": {"text": "first", "cancelled": False},
            }
            fork_state = rpc.command("get_state")["data"]
            assert fork_state["sessionFile"] != first_session
            fork_entries = rpc.command("get_entries")["data"]["entries"]
            assert not any(
                entry.get("type") == "message" and entry.get("message", {}).get("role") == "user"
                for entry in fork_entries
            )

            switched = rpc.command("switch_session", sessionPath=first_session)
            assert switched["success"] is True
            assert switched["data"]["cancelled"] is False
            restored = rpc.command("get_entries")["data"]
            assert restored["leafId"] == second["data"]["leafId"]


def test_real_pi_rpc_abort_stops_stream_without_retry(tmp_path: Path, pi_contract_binary: Path) -> None:
    requests: list[dict[str, Any]] = []

    class SlowHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            requests.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            try:
                start = {
                    "id": "slow",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "zen/big-pickle",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "start"},
                            "finish_reason": None,
                        }
                    ],
                }
                self.wfile.write(f"data: {json.dumps(start)}\n\n".encode())
                self.wfile.flush()
                for _ in range(100):
                    time.sleep(0.05)
                    chunk = {
                        "id": "slow",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "zen/big-pickle",
                        "choices": [{"index": 0, "delta": {"content": "."}, "finish_reason": None}],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        launch = _launch(
            pi_contract_binary,
            tmp_path,
            f"http://127.0.0.1:{server.server_port}",
            prompt=None,
        )
        with _RpcClient(launch, tmp_path / "project") as rpc:
            assert rpc.command("prompt", message="slow response")["success"] is True
            rpc.wait_event("message_update")
            assert rpc.command("abort")["success"] is True
            ended = rpc.wait_event("agent_end")
            assert ended["willRetry"] is False
            assistant = next(message for message in ended["messages"] if message.get("role") == "assistant")
            assert assistant["stopReason"] == "aborted"
            assert len(requests) == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_real_pi_rate_limit_is_not_retried_by_outer_host(tmp_path: Path, pi_contract_binary: Path) -> None:
    requests: list[dict[str, Any]] = []

    class RateLimitHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            requests.append(json.loads(self.rfile.read(length)))
            body = json.dumps({"error": {"message": "rate limited", "type": "rate_limit_error"}}).encode()
            self.send_response(429)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), RateLimitHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        launch = _launch(
            pi_contract_binary,
            tmp_path,
            f"http://127.0.0.1:{server.server_port}",
            prompt="rate limit me",
        )
        completed = _run(launch, tmp_path / "project")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert completed.returncode != 0
    assert "429" in completed.stderr
    assert len(requests) == 1, "managed Pi must not add its own provider retry loop"


def test_real_pi_offline_rpc_startup_makes_no_proxy_requests(tmp_path: Path, pi_contract_binary: Path) -> None:
    proxy_requests: list[str] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            return

        def _reject(self) -> None:
            proxy_requests.append(f"{self.command} {self.path}")
            self.send_response(502)
            self.end_headers()

        do_CONNECT = _reject
        do_GET = _reject
        do_POST = _reject

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        launch = _launch(pi_contract_binary, tmp_path, "http://127.0.0.1:9", prompt=None)
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        env = dict(launch.env)
        env.update({"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "ALL_PROXY": proxy_url})
        launch = EngineLaunch(launch.engine, launch.command, env)
        with _RpcClient(launch, tmp_path / "project") as rpc:
            state = rpc.command("get_state")
            assert state["success"] is True
            time.sleep(0.25)
        assert proxy_requests == []
    finally:
        proxy.shutdown()
        thread.join(timeout=5)
        proxy.server_close()


def test_real_pi_rpc_steering_queues_only_with_explicit_behavior(tmp_path: Path, pi_contract_binary: Path) -> None:
    requests: list[dict[str, Any]] = []
    request_lock = threading.Lock()

    class SteeringHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            with request_lock:
                requests.append(payload)
                index = len(requests)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            first = {
                "id": f"steer-{index}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "zen/big-pickle",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": f"TURN{index}"},
                        "finish_reason": None,
                    }
                ],
            }
            self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
            self.wfile.flush()
            if index == 1:
                time.sleep(0.4)
            stop = {
                "id": f"steer-{index}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "zen/big-pickle",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self.wfile.write(f"data: {json.dumps(stop)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), SteeringHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        launch = _launch(
            pi_contract_binary,
            tmp_path,
            f"http://127.0.0.1:{server.server_port}",
            prompt=None,
        )
        with _RpcClient(launch, tmp_path / "project") as rpc:
            assert rpc.command("prompt", message="first")["success"] is True
            rpc.wait_event("message_update")
            rejected = rpc.command("prompt", message="ambiguous queue")
            assert rejected["success"] is False
            assert "streamingBehavior" in rejected["error"]
            assert rpc.command("steer", message="change direction")["success"] is True
            ended = rpc.wait_event("agent_end")
            assert ended["willRetry"] is False

        assert len(requests) == 2
        second_messages = requests[1]["messages"]
        assert second_messages[-1]["role"] == "user"
        assert second_messages[-1]["content"][0]["text"] == "change direction"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_real_pi_rpc_user_bash_is_blocked_without_execution(tmp_path: Path, pi_contract_binary: Path) -> None:
    marker = tmp_path / "rpc-bash-executed"
    launch = _launch(pi_contract_binary, tmp_path, "http://127.0.0.1:9", prompt=None)
    with _RpcClient(launch, tmp_path / "project") as rpc:
        response = rpc.command("bash", command=f"touch {marker}")

    assert response["success"] is True
    assert response["data"]["exitCode"] == 126
    assert response["data"]["cancelled"] is False
    assert "disables local shell execution" in response["data"]["output"]
    assert not marker.exists()
