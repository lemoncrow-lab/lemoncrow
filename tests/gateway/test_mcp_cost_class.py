"""Unit tests for per-request cost-class routing in the MCP server.

Hermetic: exercises the pure ``_classify_cost`` classifier only; no server is
started and no I/O is performed.
"""

from __future__ import annotations

from typing import Any

import pytest

from atelier.gateway.adapters.mcp_server import (
    _COST_CPU,
    _COST_DETACHED,
    _COST_IO,
    _classify_cost,
    _is_heavy_request,
)


def _call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a ``tools/call`` JSON-RPC request for *name*."""
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"method": "tools/call", "params": params}


# --- protocol methods -------------------------------------------------------- #


@pytest.mark.parametrize(
    "method",
    ["tools/list", "initialize", "notifications/initialized", "ping"],
)
def test_protocol_methods_are_cpu(method: str) -> None:
    assert _classify_cost({"method": method}) == _COST_CPU


def test_missing_method_is_cpu() -> None:
    assert _classify_cost({}) == _COST_CPU


# --- cheap CPU-lane tools ---------------------------------------------------- #


@pytest.mark.parametrize("name", ["read", "grep", "code_search", "smart_read", "trace"])
def test_cheap_tools_are_cpu(name: str) -> None:
    assert _classify_cost(_call(name, {"foo": "bar"})) == _COST_CPU


def test_get_context_is_cpu() -> None:
    assert _classify_cost(_call("get_context")) == _COST_CPU


# --- bash routing ------------------------------------------------------------ #


def test_bash_foreground_is_io() -> None:
    assert _classify_cost(_call("bash", {"command": "ls -la"})) == _COST_IO


def test_bash_no_args_is_io() -> None:
    assert _classify_cost(_call("bash")) == _COST_IO


def test_bash_background_flag_is_detached() -> None:
    req = _call("bash", {"command": "sleep 100", "background": True})
    assert _classify_cost(req) == _COST_DETACHED


def test_bash_trailing_ampersand_is_detached() -> None:
    assert _classify_cost(_call("bash", {"command": "sleep 100 &"})) == _COST_DETACHED


def test_bash_trailing_ampersand_with_whitespace_is_detached() -> None:
    assert _classify_cost(_call("bash", {"command": "sleep 100 &   "})) == _COST_DETACHED


def test_bash_double_ampersand_is_io() -> None:
    req = _call("bash", {"command": "make lint && make test"})
    assert _classify_cost(req) == _COST_IO


def test_run_alias_is_treated_as_bash() -> None:
    assert _classify_cost(_call("run", {"command": "echo hi"})) == _COST_IO
    assert _classify_cost(_call("run", {"command": "echo hi &"})) == _COST_DETACHED


# --- other IO-lane tools ----------------------------------------------------- #


@pytest.mark.parametrize("name", ["edit", "web_fetch"])
def test_edit_and_web_fetch_are_io(name: str) -> None:
    assert _classify_cost(_call(name, {"x": 1})) == _COST_IO


# --- memory routing ---------------------------------------------------------- #


def test_memory_store_fact_is_io() -> None:
    req = _call("memory", {"op": "store_fact", "text": "x"})
    assert _classify_cost(req) == _COST_IO


def test_memory_recall_is_cpu() -> None:
    assert _classify_cost(_call("memory", {"op": "recall", "query": "x"})) == _COST_CPU


def test_memory_no_op_is_cpu() -> None:
    assert _classify_cost(_call("memory")) == _COST_CPU


# --- detached-class tools ---------------------------------------------------- #


@pytest.mark.parametrize("name", ["workflow", "agent"])
def test_workflow_and_agent_are_detached(name: str) -> None:
    assert _classify_cost(_call(name, {"task": "do it"})) == _COST_DETACHED


# --- defensiveness against malformed params ---------------------------------- #


def test_non_dict_params_is_cpu() -> None:
    assert _classify_cost({"method": "tools/call", "params": "nope"}) == _COST_CPU


def test_non_dict_arguments_is_handled() -> None:
    # bash with a non-dict arguments payload still classifies (foreground IO).
    req = {"method": "tools/call", "params": {"name": "bash", "arguments": "nope"}}
    assert _classify_cost(req) == _COST_IO


def test_unknown_tool_is_cpu() -> None:
    assert _classify_cost(_call("totally_made_up_tool")) == _COST_CPU


# --- back-compat shim -------------------------------------------------------- #


def test_is_heavy_request_matches_non_cpu_classes() -> None:
    assert _is_heavy_request(_call("bash", {"command": "ls"})) is True
    assert _is_heavy_request(_call("edit")) is True
    assert _is_heavy_request(_call("workflow", {"task": "x"})) is True
    assert _is_heavy_request(_call("read", {"path": "a"})) is False
    assert _is_heavy_request({"method": "tools/list"}) is False
