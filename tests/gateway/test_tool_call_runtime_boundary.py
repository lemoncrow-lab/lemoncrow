from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.gateway.tools.call_runtime import ToolCallRuntimeHooks, finalize_tool_payload, run_tool_call
from lemoncrow.gateway.tools.lifecycle import ToolLifecycleHooks
from lemoncrow.gateway.tools.registry import tool_spec


def _noop_lifecycle_hooks() -> ToolLifecycleHooks:
    def noop(*_args, **_kwargs) -> None:
        return None

    return ToolLifecycleHooks(
        get_ledger=lambda: None,
        make_outcome_writer=noop,
        append_live_savings_event=noop,
        append_debug_event=noop,
        append_tool_profile=noop,
        scrub_args_for_debug=lambda args: args,
        process_tool_accounting=noop,
        record_context_budget=noop,
        loop_review_enabled=lambda: False,
        loop_nudge_for_call=lambda *args: None,
        write_statusline_sidecar=noop,
        debug_enabled=lambda: False,
        append_workspace_savings=noop,
        spill=lambda text, *_args, **_kwargs: text,
        root=lambda: None,
        coerce_saved_tokens=lambda value: int(value or 0),
        extract_tokens_saved=lambda _result: 0,
        renderer=lambda _name, _result: None,
        read_tools=frozenset(),
    )


def test_call_runtime_returns_structured_handler_failure(monkeypatch) -> None:
    spec = tool_spec("read")
    assert spec is not None
    monkeypatch.setitem(spec, "handler", lambda _args: (_ for _ in ()).throw(RuntimeError("boom")))
    hooks = ToolCallRuntimeHooks(
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
    run = run_tool_call(1, {"name": "read", "arguments": {"path": "x.py"}}, hooks=hooks)
    assert isinstance(run.error, RuntimeError)
    assert str(run.error) == "boom"
    assert run.deferred is False


def test_call_runtime_marks_deferred_without_importing_mcp_deferral(monkeypatch) -> None:
    marker = object()
    spec = tool_spec("read")
    assert spec is not None
    monkeypatch.setitem(spec, "handler", lambda _args: marker)
    hooks = ToolCallRuntimeHooks(
        broker_spec={"name": "tool", "handler": lambda _args: {}},
        remote_tools=frozenset(),
        dispatch_remote=lambda *_args: {},
        get_ledger=lambda: object(),
        prepare_model_recommendation=lambda *_args: ({"configured": False}, {}, {}, ""),
        finalize_model_recommendation=lambda *_args, **_kwargs: None,
        lifecycle_hooks=_noop_lifecycle_hooks,
        record_session_cwd=lambda *_args: None,
        is_deferred_result=lambda value: value is marker,
    )
    run = run_tool_call(2, {"name": "read", "arguments": {}}, hooks=hooks)
    assert run.result is marker
    assert run.deferred is True


def test_finalize_payload_turns_deferred_into_execution_error() -> None:
    class Lifecycle:
        def finalize_error_payload(self, exc: Exception) -> dict[str, Any]:
            return {"error": str(exc)}

    from lemoncrow.gateway.tools.call_runtime import ToolCallRun

    run = ToolCallRun(lifecycle=Lifecycle(), result=object(), deferred=True)  # type: ignore[arg-type]
    assert "deferred tool result" in finalize_tool_payload(run)["error"]


def test_server_registry_dispatch_does_not_import_mcp_server() -> None:
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    source = (repo / "server/src/lemoncrow_server_core/registry_dispatch.py").read_text(encoding="utf-8")
    assert "from lemoncrow.gateway.adapters import mcp_server" not in source
    assert "lemoncrow.gateway.adapters.mcp_server" not in source.split("from __future__", 1)[1]
