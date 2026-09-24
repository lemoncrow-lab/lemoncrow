from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.gateway.tools.execution import execute_prepared_tool_call
from lemoncrow.gateway.tools.invocation import PreparedToolCall
from lemoncrow.gateway.tools.state import tool_call_counterfactual, tool_call_rendered_text, tool_call_tokens_saved
from lemoncrow.infra.runtime.run_ledger import RunLedger


def _ledger(tmp_path) -> RunLedger:
    return RunLedger(root=tmp_path, agent="test", session_id="s1")


def test_remote_execution_bypasses_local_handler_and_cleans_result(tmp_path) -> None:
    called: list[str] = []
    prepared = PreparedToolCall(
        name="context",
        args={"query": "x"},
        spec={"handler": lambda _args: (_ for _ in ()).throw(AssertionError("local handler called"))},
        remote_routed=True,
    )
    outcome = execute_prepared_tool_call(
        prepared,
        dispatch_remote=lambda name, args: called.append(name) or {"value": 1, "drop": None},
        get_ledger=lambda: _ledger(tmp_path),
        prepare_model_recommendation=lambda *_args: ({}, {}, {}, ""),
        finalize_model_recommendation=lambda *_args, **_kwargs: None,
    )
    assert called == ["context"]
    assert outcome.result == {"value": 1}
    assert outcome.remote_routed is True
    assert outcome.ledger is None


def test_local_execution_resets_call_state_and_finalizes_route(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    finalized: dict[str, Any] = {}
    tool_call_tokens_saved.value = 99
    tool_call_counterfactual.value = {"stale": True}
    tool_call_rendered_text.value = "stale"

    prepared = PreparedToolCall(
        name="read",
        args={"path": "x.py"},
        spec={"handler": lambda args: {"seen": args["path"]}},
        remote_routed=False,
    )
    outcome = execute_prepared_tool_call(
        prepared,
        dispatch_remote=lambda *_args: pytest.fail("remote dispatch used"),
        get_ledger=lambda: ledger,
        prepare_model_recommendation=lambda *_args: (
            {"model": "", "configured": False},
            {"state": True},
            {"workflow": True},
            "step-1",
        ),
        finalize_model_recommendation=lambda payload, **kwargs: finalized.update(payload=payload, **kwargs),
    )
    assert outcome.result == {"seen": "x.py"}
    assert outcome.ledger is ledger
    assert outcome.remote_routed is False
    assert tool_call_tokens_saved.value == 0
    assert tool_call_counterfactual.value is None
    assert tool_call_rendered_text.value is None
    assert finalized["tool_name"] == "read"
    assert finalized["wrapper_applied"] is False


def test_handler_exception_is_not_masked_by_route_finalization(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    prepared = PreparedToolCall(
        name="read",
        args={},
        spec={"handler": lambda _args: (_ for _ in ()).throw(RuntimeError("handler boom"))},
        remote_routed=False,
    )

    def finalize(*_args, **_kwargs):
        raise RuntimeError("finalize boom")

    with pytest.raises(RuntimeError, match="handler boom"):
        execute_prepared_tool_call(
            prepared,
            dispatch_remote=lambda *_args: {},
            get_ledger=lambda: ledger,
            prepare_model_recommendation=lambda *_args: ({"configured": False}, {}, {}, ""),
            finalize_model_recommendation=finalize,
        )


def test_smart_state_reexports_canonical_token_savings_state() -> None:
    from lemoncrow.gateway.adapters.mcp import smart_state
    from lemoncrow.gateway.tools import state

    assert smart_state._tool_call_tokens_saved is state.tool_call_tokens_saved


def test_route_enforcement_flag_is_canonical(monkeypatch) -> None:
    from lemoncrow.gateway.adapters import mcp_server
    from lemoncrow.gateway.tools import execution

    assert mcp_server._route_enforcement_enabled is execution.route_enforcement_enabled
    monkeypatch.setenv("LEMONCROW_ENFORCE_ROUTE_MODEL", "1")
    assert execution.route_enforcement_enabled() is True
    monkeypatch.setenv("LEMONCROW_ENFORCE_ROUTE_MODEL", "0")
    assert execution.route_enforcement_enabled() is False
