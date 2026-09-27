from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.runtime_trace import TraceStore, is_trace_id, new_trace_id, open_trace
from lemoncrow.gateway.tools import runtime_call
from lemoncrow.gateway.tools.call_runtime import META_CALL_ID, run_tool_call, runtime_call_from_jsonrpc
from lemoncrow.gateway.tools.registry import tool_spec
from lemoncrow.gateway.tools.runtime_call import RuntimeCall, ToolCallRuntime, ToolCallRuntimeHooks
from tests.gateway.test_tool_call_runtime_boundary import _noop_lifecycle_hooks

_FORBIDDEN_PREFIXES = ("lemoncrow.gateway.adapters", "aiohttp", "mcp", "starlette", "fastapi", "http")


def test_runtime_module_imports_no_transport() -> None:
    tree = ast.parse(Path(runtime_call.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    offenders = [name for name in imported if name.startswith(_FORBIDDEN_PREFIXES)]
    assert offenders == []
    assert "rid" not in {f for f in RuntimeCall.__dataclass_fields__}


def _hooks() -> ToolCallRuntimeHooks:
    return ToolCallRuntimeHooks(
        broker_spec={"name": "tool", "handler": lambda _args: {}},
        remote_tools=frozenset(),
        dispatch_remote=lambda *_args: pytest.fail("remote dispatch"),
        get_ledger=lambda: object(),
        prepare_model_recommendation=lambda *_args: ({"configured": False}, {}, {}, ""),
        finalize_model_recommendation=lambda *_args, **_kwargs: None,
        lifecycle_hooks=_noop_lifecycle_hooks,
        record_session_cwd=lambda *_args: None,
        is_deferred_result=lambda _value: False,
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TraceStore:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    from lemoncrow.core.foundation.runtime_trace import trace_store

    return trace_store(tmp_path)


def test_invoke_returns_typed_result_and_writes_one_trace(monkeypatch: pytest.MonkeyPatch, store: TraceStore) -> None:
    spec = tool_spec("read")
    assert spec is not None
    monkeypatch.setitem(spec, "handler", lambda _args: {"content": [{"type": "text", "text": "hello"}]})
    call_id = new_trace_id()
    result = ToolCallRuntime(_hooks()).invoke(
        RuntimeCall(tool="read", arguments={"path": "x.py"}, call_id=call_id, trace_mode="raw")
    )
    assert result.trace_id == call_id and not result.is_error
    records = store.find(call_id)
    assert len(records) == 1
    record = records[0]
    assert record["writer"] == "runtime" and record["tool"] == "read"
    assert record["request"]["arguments"] == {"path": "x.py"}
    assert record["outcome"]["is_error"] is False


def test_handler_failure_is_traced_as_error(monkeypatch: pytest.MonkeyPatch, store: TraceStore) -> None:
    spec = tool_spec("read")
    assert spec is not None
    monkeypatch.setitem(spec, "handler", lambda _args: (_ for _ in ()).throw(RuntimeError("boom")))
    call_id = new_trace_id()
    result = ToolCallRuntime(_hooks()).invoke(RuntimeCall(tool="read", arguments={}, call_id=call_id, trace_mode="raw"))
    assert result.is_error
    outcome = store.find(call_id)[0]["outcome"]
    assert outcome["is_error"] is True and outcome["error_type"] == "RuntimeError"


def test_runtime_joins_an_outer_server_trace(monkeypatch: pytest.MonkeyPatch, store: TraceStore) -> None:
    spec = tool_spec("read")
    assert spec is not None
    monkeypatch.setitem(spec, "handler", lambda _args: {"content": []})
    call_id = new_trace_id()
    with open_trace(trace_id=call_id, writer="server", tool="read", mode="raw", store=store):
        ToolCallRuntime(_hooks()).invoke(RuntimeCall(tool="read", arguments={}, call_id=call_id))
    assert [r["writer"] for r in store.find(call_id)] == ["server"]


def test_jsonrpc_adapter_maps_meta_without_leaking_it_into_the_call() -> None:
    call_id = new_trace_id()
    call = runtime_call_from_jsonrpc(
        {
            "name": "read",
            "arguments": '{"path": "a.py"}',
            "_meta": {META_CALL_ID: call_id, "mcp-project-path": "/repo"},
        }
    )
    assert (call.tool, dict(call.arguments), call.call_id, call.workspace_id) == (
        "read",
        {"path": "a.py"},
        call_id,
        "/repo",
    )
    minted = runtime_call_from_jsonrpc({"name": "read", "_meta": {META_CALL_ID: "not-an-id"}})
    assert is_trace_id(minted.call_id) and minted.call_id != "not-an-id"


def test_project_args_are_popped_only_when_meta_names_no_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    spec = tool_spec("read")
    assert spec is not None
    monkeypatch.setitem(spec, "handler", lambda args: seen.append(dict(args)) or {"content": []})
    run_tool_call(1, {"name": "read", "arguments": {"path": "a", "project_path": "/p"}}, hooks=_hooks())
    run_tool_call(
        2,
        {"name": "read", "arguments": {"path": "a", "project_path": "/p"}, "_meta": {"projectId": "x"}},
        hooks=_hooks(),
    )
    assert "project_path" not in seen[0]
    assert seen[1]["project_path"] == "/p"
