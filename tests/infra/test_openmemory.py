from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import pytest

import atelier.gateway.integrations.openmemory as openmemory
from atelier.gateway.integrations.openmemory import (
    OpenMemoryMCPError,
    list_available_memory_tools,
    maybe_fetch_memory_context_for_task,
    maybe_link_trace_to_memory_context,
    maybe_store_memory_pointer,
)


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[str]:
        return ["add_memories", "search_memory", "list_memories", "delete_all_memories"]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "add_memories":
            return {"results": [{"id": "mem-added"}]}
        if name == "search_memory":
            return [
                {
                    "id": "mem-1",
                    "memory": '{"atelier_kind":"trace_context_link","trace_id":"trace-123","context_id":"ctx-456"}',
                    "metadata": {"atelier_kind": "trace_context_link"},
                    "score": 0.9,
                }
            ]
        if name == "list_memories":
            return []
        raise AssertionError(f"unexpected tool call: {name}")


class _FailingClient:
    def list_tools(self) -> list[str]:
        raise OpenMemoryMCPError("boom")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        _ = (name, arguments)
        raise OpenMemoryMCPError("boom")


def _data(result: dict[str, object]) -> dict[str, Any]:
    return cast("dict[str, Any]", result["data"])


def _has_required_keys(result: dict[str, object]) -> bool:
    return all(k in result for k in ("ok", "data", "action"))


def test_list_tools_returns_openmemory_tool_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openmemory, "_CLIENT", _FakeClient())
    tools = list_available_memory_tools()
    assert tools[:3] == ["add_memories", "search_memory", "list_memories"]


def test_list_tools_falls_back_to_canonical_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openmemory, "_CLIENT", _FailingClient())
    assert list_available_memory_tools() == [
        "add_memories",
        "search_memory",
        "list_memories",
        "delete_memories",
        "delete_all_memories",
    ]


def test_link_trace_persists_via_add_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(openmemory, "_CLIENT", client)

    result = maybe_link_trace_to_memory_context("trace-123", context_id="ctx-456")

    assert result["ok"] is True
    assert result["action"] == "link_trace_to_memory_context"
    assert _data(result)["context_id"] == "ctx-456"
    name, arguments = client.calls[0]
    assert name == "add_memories"
    assert arguments["infer"] is False
    assert arguments["metadata"]["atelier_kind"] == "trace_context_link"
    content = arguments["messages"][0]["content"]
    assert content == json.dumps(json.loads(content), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_fetch_context_uses_search_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(openmemory, "_CLIENT", client)

    result = maybe_fetch_memory_context_for_task("trace-123")

    assert result["ok"] is True
    assert result["action"] == "fetch_memory_context_for_task"
    assert _data(result)["count"] == 1
    assert _data(result)["matches"][0]["parsed"]["trace_id"] == "trace-123"
    name, arguments = client.calls[0]
    assert name == "search_memory"
    assert arguments["query"] == "trace-123"


def test_store_pointer_persists_via_add_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(openmemory, "_CLIENT", client)

    result = maybe_store_memory_pointer("trace-123", "mem-456")

    assert result["ok"] is True
    assert _data(result)["memory_id"] == "mem-456"
    name, arguments = client.calls[0]
    assert name == "add_memories"
    assert arguments["metadata"]["memory_id"] == "mem-456"
    content = arguments["messages"][0]["content"]
    assert content == json.dumps(json.loads(content), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    "fn",
    [
        lambda: maybe_link_trace_to_memory_context("t1"),
        lambda: maybe_fetch_memory_context_for_task("task"),
        lambda: maybe_store_memory_pointer("t1", "m1"),
    ],
)
def test_response_has_required_keys(
    fn: Callable[[], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openmemory, "_CLIENT", _FakeClient())
    result = fn()
    assert _has_required_keys(result)


def test_bridge_returns_unavailable_when_client_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openmemory, "_CLIENT", _FailingClient())

    result = maybe_fetch_memory_context_for_task("task")

    assert result["ok"] is False
    assert result["skipped"] is True
    assert "unavailable" in str(result["reason"]).lower()
