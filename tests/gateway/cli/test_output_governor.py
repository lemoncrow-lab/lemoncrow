from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("litellm", reason="lemoncrow[litellm] not installed")

from lemoncrow.gateway.cli.events import AssistantDelta, AssistantMessage, AssistantProgress
from lemoncrow.gateway.cli.runtime import InteractiveRuntime


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls or []),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def _tool(index: int, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        id=f"call-{index}",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def test_enforced_output_governor_strips_progress_and_finalizes_from_verification(tmp_path, monkeypatch) -> None:
    calls = 0

    def completion(**_kwargs):
        nonlocal calls
        calls += 1
        return [
            _chunk(
                content="I am about to edit and test.",
                tool_calls=[
                    _tool(
                        0,
                        "edit",
                        {
                            "edits": [{"file_path": "parser.py", "new_string": "fixed\n", "overwrite": True}],
                            "post_edit_hooks": False,
                        },
                    ),
                    _tool(1, "bash", {"command": "uv run pytest -q"}),
                ],
                finish_reason="tool_calls",
            )
        ]

    async def execute(_self, _name, _args, session_id=""):
        return "ok", True

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr(InteractiveRuntime, "_execute_tool_call", execute)
    runtime = InteractiveRuntime(
        root=tmp_path / ".lemoncrow",
        yolo=True,
        model="openai/gpt-5",
        optimization_mode="enforce",
    )

    async def run():
        session_id = await runtime.start_session(str(tmp_path))
        return session_id, [
            event async for event in runtime.handle_user_message(session_id, "Fix parser.py and run tests")
        ]

    session_id, events = asyncio.run(run())

    assert calls == 1
    assert [event.text for event in events if isinstance(event, AssistantProgress)] == ["I am about to edit and test."]
    deltas = [event.text for event in events if isinstance(event, AssistantDelta)]
    assert deltas == ["Done: updated parser.py. Verified: `uv run pytest -q` passed."]
    assert [event.text for event in events if isinstance(event, AssistantMessage)] == deltas
    tool_turn = next(message for message in runtime.session_messages(session_id) if message.get("tool_calls"))
    assert tool_turn["content"] is None


def test_output_budget_extends_only_after_real_truncation(tmp_path, monkeypatch) -> None:
    limits: list[int] = []

    def completion(**kwargs):
        limits.append(kwargs["max_tokens"])
        if len(limits) == 1:
            return [_chunk(content="Partial answer. ", finish_reason="length")]
        return [_chunk(content="Completed answer.", finish_reason="stop")]

    monkeypatch.setattr("litellm.completion", completion)
    runtime = InteractiveRuntime(
        root=tmp_path / ".lemoncrow",
        yolo=True,
        model="openai/gpt-5",
        optimization_mode="enforce",
    )

    async def run():
        session_id = await runtime.start_session(str(tmp_path))
        return [event async for event in runtime.handle_user_message(session_id, "Explain the parser")]

    events = asyncio.run(run())

    assert limits == [1200, 2400]
    assert [event.text for event in events if isinstance(event, AssistantDelta)] == [
        "Partial answer. ",
        "Completed answer.",
    ]


def test_mutation_claim_without_edit_or_verification_is_not_accepted(tmp_path, monkeypatch) -> None:
    def completion(**_kwargs):
        return [_chunk(content="Done.", finish_reason="stop")]

    monkeypatch.setattr("litellm.completion", completion)
    runtime = InteractiveRuntime(
        root=tmp_path / ".lemoncrow",
        yolo=True,
        model="openai/gpt-5",
        optimization_mode="enforce",
    )

    async def run():
        session_id = await runtime.start_session(str(tmp_path))
        return [event async for event in runtime.handle_user_message(session_id, "Fix parser.py")]

    asyncio.run(run())

    from lemoncrow.pro.capabilities.optimization.runtime_decisions import (
        load_runtime_decision_traces,
    )

    traces = load_runtime_decision_traces(tmp_path / ".lemoncrow")
    assert len(traces) == 1
    assert not traces[0]["accepted"]
    assert traces[0]["verification"]["passed"] == 0


def test_global_off_writes_no_v2_optimization_state(tmp_path, monkeypatch) -> None:
    def completion(**_kwargs):
        return [_chunk(content="Parser explained.", finish_reason="stop")]

    monkeypatch.setattr("litellm.completion", completion)
    route = SimpleNamespace(
        provider="openai",
        model="gpt-5",
        tier="high",
        reason="legacy route",
        alternatives=(),
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.owned_execution_routing.select_owned_route",
        lambda *_args, **_kwargs: route,
    )
    root = tmp_path / ".lemoncrow"
    runtime = InteractiveRuntime(
        root=root,
        yolo=True,
        optimization_mode="off",
    )

    async def run():
        session_id = await runtime.start_session(str(tmp_path))
        return [event async for event in runtime.handle_user_message(session_id, "Explain parser")]

    asyncio.run(run())

    assert not (root / "optimization" / "runtime-decisions.jsonl").exists()
    assert not (root / "optimization" / "routing-outcomes.sqlite3").exists()
    assert not (root / "cache" / "cache-economics.sqlite3").exists()
    assert not (root / "cache" / "evidence-reuse.sqlite3").exists()
