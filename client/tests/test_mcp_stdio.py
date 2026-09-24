"""The stdio entry point: one process, one enterprise-safe namespace.

The host launches exactly this process. MCP initialize owns remote bootstrap;
there is no SessionStart helper, local listener, MCP child server or second
LemonCrow executable. Advertised tools keep their existing names and schemas,
while security-excluded process-spawning tools are absent by construction.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest
from _stub import StubServer
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.mcpserver import MCP_PROTOCOL_VERSION, McpServer, serve
from lemoncrow_client.routing import ROUTES, SECURITY_EXCLUDED_TOOLS
from lemoncrow_client.surface import load_surface


def rpc(method: str, params: Mapping[str, object] | None = None, request_id: int = 1) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = dict(params)
    return payload


def test_initialize_bootstraps_the_one_remote_session(stub: StubServer, config: ClientConfig) -> None:
    server = McpServer(config)
    answer = server.handle(rpc("initialize"))
    assert answer is not None
    result = answer["result"]
    assert result["serverInfo"]["name"] == "lc"
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["capabilities"] == {"tools": {}}
    assert "LemonCrow:" in result["instructions"]
    assert server.dispatcher.session.bootstrapped is True
    assert stub.state.session_id
    assert stub.state.view_id
    server.close()


def test_tools_list_advertises_only_canonically_visible_routed_tools(config: ClientConfig) -> None:
    answer = McpServer(config).handle(rpc("tools/list"))
    assert answer is not None
    tools = answer["result"]["tools"]
    names = [entry["name"] for entry in tools]
    surface = load_surface()
    expected = sorted(name for name in ROUTES if bool(surface[name].get("visibleToLlm")))
    assert names == expected
    assert SECURITY_EXCLUDED_TOOLS.isdisjoint(names)
    for entry in tools:
        assert entry["description"], f"{entry['name']} has no description"
        assert isinstance(entry["inputSchema"], dict)


def test_tools_list_is_identical_whether_or_not_the_server_is_reachable(
    stub: StubServer, config: ClientConfig, unreachable: ClientConfig
) -> None:
    """An offline session must still have a tool surface."""
    online = McpServer(config).handle(rpc("tools/list"))
    offline = McpServer(unreachable).handle(rpc("tools/list"))
    assert online == offline


def test_the_tool_broker_is_absent_from_tools_list_in_the_default_full_profile(config: ClientConfig) -> None:
    answer = McpServer(config).handle(rpc("tools/list"))
    assert answer is not None
    assert "tool" not in {entry["name"] for entry in answer["result"]["tools"]}


def test_the_tool_broker_is_advertised_alongside_the_lean_surface_in_core_profile(
    config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "core")
    answer = McpServer(config).handle(rpc("tools/list"))
    assert answer is not None
    names = [entry["name"] for entry in answer["result"]["tools"]]
    assert names == sorted(names), "tools/list must stay in name order with the broker mixed in"
    assert "tool" in names
    surface = load_surface()
    expected = {name for name in ROUTES if bool(surface[name].get("visibleToLlm")) and bool(surface[name].get("core"))}
    assert set(names) - {"tool"} == expected


def test_the_tool_broker_never_discovers_baseline_hidden_tools(
    config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "core")
    answer = McpServer(config).handle(
        rpc("tools/call", {"name": "tool", "arguments": {"action": "search", "query": "grep"}})
    )
    assert answer is not None
    assert answer["result"]["isError"] is False
    text = answer["result"]["content"][0]["text"]
    assert "grep" not in text
    assert "no hidden tool matches" in text


def test_the_tool_broker_refuses_baseline_hidden_tools(config: ClientConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "core")
    server = McpServer(config)
    direct = server.handle(rpc("tools/call", {"name": "grep", "arguments": {"regex": "McpServer", "path": "."}}))
    via_broker = server.handle(
        rpc(
            "tools/call",
            {
                "name": "tool",
                "arguments": {"action": "call", "name": "grep", "arguments": {"regex": "McpServer", "path": "."}},
            },
        )
    )
    assert direct is not None and via_broker is not None
    assert direct["result"]["isError"] is False
    assert via_broker["result"]["isError"] is True
    assert "unknown or unavailable tool: grep" in via_broker["result"]["content"][0]["text"]


def test_the_tool_broker_refuses_to_call_a_tool_already_visible_in_tools_list(
    config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "core")
    answer = McpServer(config).handle(
        rpc(
            "tools/call",
            {"name": "tool", "arguments": {"action": "call", "name": "bash", "arguments": {"command": "true"}}},
        )
    )
    assert answer is not None
    assert answer["result"]["isError"] is True
    assert "already exposed" in answer["result"]["content"][0]["text"]


def test_the_tool_broker_refuses_an_unknown_action(config: ClientConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "core")
    answer = McpServer(config).handle(rpc("tools/call", {"name": "tool", "arguments": {"action": "delete"}}))
    assert answer is not None
    assert answer["result"]["isError"] is True
    assert "unknown broker action" in answer["result"]["content"][0]["text"]


def test_calling_the_broker_name_outside_core_profile_is_an_ordinary_unknown_tool(config: ClientConfig) -> None:
    """``tool`` is not routed, so in ``full`` profile it fails exactly like any other unknown name."""
    answer = McpServer(config).handle(rpc("tools/call", {"name": "tool", "arguments": {"action": "search"}}))
    assert answer is not None
    assert answer["result"]["isError"] is True
    assert "tool_unknown" in answer["result"]["content"][0]["text"]


def test_a_notification_produces_no_response(config: ClientConfig) -> None:
    assert McpServer(config).handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_unknown_method_is_a_jsonrpc_error(config: ClientConfig) -> None:
    answer = McpServer(config).handle(rpc("resources/list"))
    assert answer is not None
    assert answer["error"]["code"] == -32601


def test_a_client_side_and_a_server_side_call_have_the_same_result_shape(
    stub: StubServer, config: ClientConfig
) -> None:
    """The model cannot tell where a tool ran, which is the requirement."""
    server = McpServer(config)
    local = server.handle(rpc("tools/call", {"name": "bash", "arguments": {"command": "printf x"}}))
    remote = server.handle(rpc("tools/call", {"name": "code_search", "arguments": {"query": "alpha"}}))
    assert local is not None and remote is not None
    assert {"content", "isError"} <= set(local["result"])
    assert {"content", "isError"} <= set(remote["result"])
    assert "structuredContent" not in remote["result"]
    assert local["result"]["content"][0]["type"] == remote["result"]["content"][0]["type"] == "text"


def test_arguments_delivered_as_a_json_string_are_accepted(stub: StubServer, config: ClientConfig) -> None:
    answer = McpServer(config).handle(
        rpc("tools/call", {"name": "bash", "arguments": json.dumps({"command": "printf parsed"})})
    )
    assert answer is not None
    assert "parsed" in answer["result"]["content"][0]["text"]


def test_an_unknown_tool_is_an_actionable_result_not_a_transport_error(
    config: ClientConfig,
) -> None:
    answer = McpServer(config).handle(rpc("tools/call", {"name": "nope", "arguments": {}}))
    assert answer is not None
    assert "error" not in answer
    assert answer["result"]["isError"] is True
    assert "tool_unknown" in answer["result"]["content"][0]["text"]


def test_a_failure_does_not_repeat_for_client_only_tools(stub: StubServer, unreachable: ClientConfig) -> None:
    server = McpServer(unreachable)
    for _ in range(3):
        server.handle(rpc("tools/call", {"name": "bash", "arguments": {"command": "printf x"}}))
    assert server.dispatcher.session.state.reason


def test_a_server_tool_recovers_when_the_server_finishes_starting(stub: StubServer, config: ClientConfig) -> None:
    stub.state.dead = True
    server = McpServer(config)
    initialized = server.handle(rpc("initialize"))
    assert initialized is not None
    assert server.dispatcher.session.bootstrapped is False

    stub.state.dead = False
    server._last_bootstrap_attempt = 0.0
    answer = server.handle(rpc("tools/call", {"name": "code_search", "arguments": {"query": "alpha"}}))

    assert answer is not None
    assert answer["result"]["isError"] is False
    assert server.dispatcher.session.bootstrapped is True
    server.close()


def test_serve_exits_when_stdin_closes_and_leaves_no_thread(stub: StubServer, config: ClientConfig) -> None:
    """The whole lifecycle: read until stdin closes, then exit.

    The absolute "no thread survives" assertion lives in
    ``test_packaging_audit.py``, which runs the client alone in a clean
    subprocess. Here the stub server's own daemon handler threads share the
    process, so what is asserted is the part that is still exactly attributable:
    the client started no non-daemon thread.
    """
    before = {thread for thread in threading.enumerate() if not thread.daemon}
    source = io.StringIO(
        "\n".join(
            [
                json.dumps(rpc("initialize")),
                json.dumps(rpc("tools/list", request_id=2)),
                json.dumps(rpc("tools/call", {"name": "bash", "arguments": {"command": "printf y"}}, 3)),
            ]
        )
        + "\n"
    )
    sink = io.StringIO()
    assert serve(stdin=source, stdout=sink, config=config) == 0
    answers = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [entry["id"] for entry in answers] == [1, 2, 3]
    assert {thread for thread in threading.enumerate() if not thread.daemon} == before


def test_malformed_input_is_answered_rather_than_crashing(config: ClientConfig) -> None:
    sink = io.StringIO()
    assert serve(stdin=io.StringIO("not json\n[]\n"), stdout=sink, config=config) == 0
    answers = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [entry["error"]["code"] for entry in answers] == [-32700, -32600]


def test_the_installed_entry_point_runs_as_a_real_process(stub: StubServer, worktree: Path, state_dir: Path) -> None:
    """End to end through the console script the host actually launches."""
    from _stub import TOKEN

    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "LEMONCROW_URL": stub.url,
        "LEMONCROW_TOKEN": TOKEN,
        "LEMONCROW_HOME": str(state_dir),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "lemoncrow_client", "mcp"],
        input="\n".join([json.dumps(rpc("initialize")), json.dumps(rpc("tools/list", request_id=2))]) + "\n",
        capture_output=True,
        text=True,
        cwd=str(worktree),
        env=environment,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    answers = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert answers[0]["result"]["serverInfo"]["name"] == "lc"
    surface = load_surface()
    expected = {name for name in ROUTES if bool(surface[name].get("visibleToLlm"))}
    assert {entry["name"] for entry in answers[1]["result"]["tools"]} == expected


@pytest.fixture
def unreachable(worktree: Path, state_dir: Path) -> ClientConfig:
    import socket

    from conftest import make_config

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return make_config(
        url=f"http://127.0.0.1:{port}",
        worktree=worktree,
        state_dir=state_dir,
        LEMONCROW_REQUEST_TIMEOUT_S="1",
    )


def test_stdio_model_surface_never_emits_structured_content(
    config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client.dispatcher import Dispatcher, ToolOutcome

    monkeypatch.setattr(McpServer, "_ensure_session", lambda self, retry=False: None)
    monkeypatch.setattr(
        Dispatcher,
        "call",
        lambda self, tool, arguments: ToolOutcome(
            content=({"type": "text", "text": f"rendered {tool}"},),
            structured={"tool": tool, "raw": "machine-only"},
        ),
    )
    server = McpServer(config)
    listed = server.handle(rpc("tools/list"))
    assert listed is not None
    for request_id, entry in enumerate(listed["result"]["tools"], start=100):
        name = entry["name"]
        answer = server.handle(rpc("tools/call", {"name": name, "arguments": {}}, request_id))
        assert answer is not None
        assert "structuredContent" not in answer["result"], name
        assert answer["result"]["content"] == [{"type": "text", "text": f"rendered {name}"}]


def test_stdio_tool_broker_never_emits_structured_content(
    config: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client import toolbroker
    from lemoncrow_client.dispatcher import ToolOutcome

    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "core")
    monkeypatch.setattr(McpServer, "_ensure_session", lambda self, retry=False: None)
    monkeypatch.setattr(
        toolbroker,
        "handle",
        lambda arguments, dispatcher: ToolOutcome(
            content=({"type": "text", "text": "rendered broker target"},),
            structured={"raw": "machine-only"},
        ),
    )
    answer = McpServer(config).handle(
        rpc("tools/call", {"name": "tool", "arguments": {"action": "search", "query": "anything"}})
    )
    assert answer is not None
    assert "structuredContent" not in answer["result"]
    assert answer["result"]["content"] == [{"type": "text", "text": "rendered broker target"}]


def test_tool_outcome_hides_structured_content_by_default() -> None:
    from lemoncrow_client.dispatcher import ToolOutcome

    payload = ToolOutcome(
        content=({"type": "text", "text": "ok"},),
        structured={"kind": "example", "count": 2},
    ).to_mcp()
    assert payload == {"content": [{"type": "text", "text": "ok"}], "isError": False}


def test_tool_outcome_structured_content_requires_explicit_opt_in() -> None:
    from lemoncrow_client.dispatcher import ToolOutcome

    payload = ToolOutcome(
        content=({"type": "text", "text": "ok"},),
        structured={"kind": "example", "count": 2},
    ).to_mcp(include_structured=True)
    assert payload["structuredContent"] == {"kind": "example", "count": 2}


def test_local_grep_structured_content_survives_dispatcher(config: ClientConfig) -> None:
    from lemoncrow_client.session import RemoteSession

    target = config.repo_root / "structured.txt"
    target.write_text("structured-content-marker\n", encoding="utf-8")
    dispatcher = McpServer(config, session=RemoteSession(config)).dispatcher
    outcome = dispatcher.call("grep", {"regex": "structured-content-marker", "path": "structured.txt"})
    assert outcome.structured is not None
    payload = outcome.to_mcp(include_structured=True)
    assert payload["structuredContent"]["matches"] == 1
