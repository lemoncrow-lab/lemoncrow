from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

litellm = pytest.importorskip("litellm", reason="lemoncrow[litellm] not installed")

from lemoncrow.gateway.cli.events import AssistantMessage, ToolFinished  # noqa: E402
from lemoncrow.gateway.cli.runtime import InteractiveRuntime, _dispatch_tool, _get_litellm_tools  # noqa: E402


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    finish_reason: str | None = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_agent_loop_executes_edit_before_final_message(tmp_path, monkeypatch) -> None:
    calls = 0

    def completion(**kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["tools"]
        assert kwargs["tool_choice"] == "auto"
        if calls == 1:
            tool_call = SimpleNamespace(
                index=0,
                id="call-edit",
                function=SimpleNamespace(
                    name="edit",
                    arguments=json.dumps(
                        {
                            "edits": [
                                {
                                    "file_path": "result.txt",
                                    "new_string": "edited by LemonCrow\n",
                                    "overwrite": True,
                                }
                            ],
                            "post_edit_hooks": False,
                        }
                    ),
                ),
            )
            return [
                _chunk(content="Applying the edit.", tool_calls=[tool_call], finish_reason="tool_calls"),
                _chunk(
                    finish_reason="tool_calls",
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=20,
                        prompt_tokens_details=None,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=0,
                    ),
                ),
            ]
        return [
            _chunk(content="Implemented and verified.", finish_reason="stop"),
            _chunk(
                finish_reason="stop",
                usage=SimpleNamespace(
                    prompt_tokens=140,
                    completion_tokens=12,
                    prompt_tokens_details=None,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            ),
        ]

    monkeypatch.setattr("litellm.completion", completion)
    runtime = InteractiveRuntime(root=tmp_path / ".lemoncrow", yolo=True, model="test/model", provider="test")

    async def run_session():
        session_id = await runtime.start_session(str(tmp_path), session_id="lemoncrow-run-test")
        return [event async for event in runtime.handle_user_message(session_id, "Create result.txt")]

    events = asyncio.run(run_session())

    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "edited by LemonCrow\n"
    assert [event.text for event in events if isinstance(event, AssistantMessage)] == ["Implemented and verified."]
    assert any(isinstance(event, ToolFinished) and event.ok for event in events)
    assert calls == 2


def test_bedrock_cache_breakpoint_moves_to_latest_tool_result(tmp_path) -> None:
    runtime = InteractiveRuntime(
        root=tmp_path / ".lemoncrow",
        model="bedrock/us.anthropic.claude-sonnet-4-6",
        provider="bedrock",
    )
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
    ]

    prepared = runtime._messages_with_cache_breakpoint(messages, runtime._override_model or "")

    assert messages[-1]["content"] == "tool output"
    assert prepared[-1]["content"][0]["text"] == "tool output"
    assert prepared[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_agent_loop_parallelizes_independent_read_tools(tmp_path, monkeypatch) -> None:
    import threading
    import time

    calls = 0
    active = 0
    max_active = 0
    lock = threading.Lock()

    def completion(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            tool_calls = [
                SimpleNamespace(
                    index=index,
                    id=f"call-{index}",
                    function=SimpleNamespace(
                        name="read",
                        arguments=json.dumps({"path": f"file-{index}.txt"}),
                    ),
                )
                for index in range(2)
            ]
            return [_chunk(tool_calls=tool_calls, finish_reason="tool_calls")]
        return [_chunk(content="Done.", finish_reason="stop")]

    def dispatch(_name, _args):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return "ok"

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("lemoncrow.gateway.cli.runtime._dispatch_tool", dispatch)
    runtime = InteractiveRuntime(root=tmp_path / ".lemoncrow", yolo=True, model="test/model")

    async def run_session():
        session_id = await runtime.start_session(str(tmp_path))
        return [event async for event in runtime.handle_user_message(session_id, "Read both files")]

    events = asyncio.run(run_session())

    assert max_active == 2
    assert len([event for event in events if isinstance(event, ToolFinished)]) == 2


def test_owned_edit_blocks_test_weakening(tmp_path, monkeypatch) -> None:
    # The owned runtime no longer publishes a self-authorize bypass; an edit that
    # WEAKENS a test (removes an assertion) is rolled back by the detector.
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "tests" / "test_contract.py"
    target.parent.mkdir()
    target.write_text("def test_x():\n    assert value == 'old'\n    assert other == 1\n", encoding="utf-8")

    edit_tool = next(tool for tool in _get_litellm_tools() if tool["function"]["name"] == "edit")
    properties = edit_tool["function"]["parameters"]["properties"]
    assert "allow_test_contract_change" not in properties
    assert "contract_change_evidence" not in properties

    result = _dispatch_tool(
        "edit",
        {
            "edits": [
                {
                    "file_path": "tests/test_contract.py",
                    "old_string": "    assert value == 'old'\n    assert other == 1\n",
                    "new_string": "    assert other == 1\n",
                }
            ],
            "post_edit_hooks": False,
        },
    )

    assert result["rolled_back"] is True
    assert "assert value == 'old'" in target.read_text(encoding="utf-8")


def test_dispatch_tool_bash_passes_args_through(monkeypatch) -> None:
    from lemoncrow.gateway.adapters.mcp_server import TOOLS

    captured: list[dict] = []

    def fake_handler(args: dict) -> str:
        captured.append(dict(args))
        return "ok"

    monkeypatch.setitem(TOOLS["bash"], "handler", fake_handler)

    # Foreground bash blocks to completion by default -- no injected params.
    foreground_args = {"command": "echo hi", "timeout": 1800}
    assert _dispatch_tool("bash", foreground_args) == "ok"
    assert captured[-1] == {"command": "echo hi", "timeout": 1800}

    # sync_wait is gone entirely: not a bash parameter anywhere.
    bash_tool = next(tool for tool in _get_litellm_tools() if tool["function"]["name"] == "bash")
    assert "sync_wait" not in bash_tool["function"]["parameters"]["properties"]


def test_cache_breakpoints_pin_system_and_latest_message(tmp_path) -> None:
    runtime = InteractiveRuntime(
        root=tmp_path / ".lemoncrow",
        model="bedrock/us.anthropic.claude-sonnet-4-6",
        provider="bedrock",
    )
    messages = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "done"},
    ]

    request = runtime._messages_with_cache_breakpoint(messages, "bedrock/us.anthropic.claude-sonnet-4-6")

    # Static breakpoint on the system message (pins tools+system prefix).
    assert request[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert request[0]["content"][0]["text"] == "sys prompt"
    # Moving breakpoint on the latest completed message.
    assert request[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Intermediate message untouched; original messages not mutated.
    assert request[1]["content"] == "do the task"
    assert messages[0]["content"] == "sys prompt"


def test_completion_retries_provider_rate_limits(tmp_path, monkeypatch) -> None:
    calls = 0

    def completion(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("429 Too many requests")
        return ["ok"]

    async def no_wait(_delay):
        return None

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("asyncio.sleep", no_wait)
    runtime = InteractiveRuntime(root=tmp_path / ".lemoncrow", model="test/model")

    result = asyncio.run(runtime._completion_with_backoff({"model": "test/model", "messages": []}))

    assert result == ["ok"]
    assert calls == 2


def test_compact_keeps_system_and_does_not_orphan_tool_result(tmp_path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / ".lemoncrow", model="test/model")
    # Tail where a naive messages[-4:] would start on a `tool` message, leaving
    # a tool_result with no preceding tool_use (providers 400 on this).
    runtime._sessions["s"] = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c2", "content": "out2"},
        {"role": "assistant", "content": "done"},
    ]

    async def run():
        return [event async for event in runtime.handle_slash_command("s", "compact", [])]

    asyncio.run(run())
    kept = runtime._sessions["s"]

    assert kept[0]["role"] == "system"
    # No retained message after the system prompt may be an orphaned tool_result
    # or a dangling assistant tool_call at the head of the tail.
    assert kept[1]["role"] not in ("tool",)
    assert not kept[1].get("tool_calls")
    assert kept[-1]["content"] == "done"
