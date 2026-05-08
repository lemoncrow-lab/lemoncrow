"""Tests for OpenMemory bridge local persistence + optional remote sync."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from atelier.gateway.integrations.openmemory import (
    is_enabled,
    list_available_memory_tools,
    maybe_fetch_memory_context_for_task,
    maybe_link_trace_to_memory_context,
    maybe_store_memory_pointer,
)

# --------------------------------------------------------------------------- #
# is_enabled                                                                  #
# --------------------------------------------------------------------------- #


def _data(result: dict[str, object]) -> dict[str, Any]:
    return cast("dict[str, Any]", result["data"])


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_OPENMEMORY_ENABLED", raising=False)
    assert is_enabled() is False


def test_enabled_via_env_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OPENMEMORY_ENABLED", "true")
    assert is_enabled() is True


def test_enabled_via_env_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OPENMEMORY_ENABLED", "1")
    assert is_enabled() is True


def test_disabled_via_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OPENMEMORY_ENABLED", "false")
    assert is_enabled() is False


# --------------------------------------------------------------------------- #
# list_available_memory_tools                                                 #
# --------------------------------------------------------------------------- #


def test_list_tools_disabled_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_OPENMEMORY_ENABLED", raising=False)
    tools = list_available_memory_tools()
    assert "fetch_memory_context_for_task" in tools


def test_list_tools_enabled_server_unavailable_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATELIER_OPENMEMORY_ENABLED", "true")
    tools = list_available_memory_tools()
    assert isinstance(tools, list)


# --------------------------------------------------------------------------- #
# maybe_link_trace_to_memory_context                                         #
# --------------------------------------------------------------------------- #


def test_link_trace_disabled_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_OPENMEMORY_ENABLED", raising=False)
    result = maybe_link_trace_to_memory_context("trace-123")
    assert result["ok"] is True
    assert result["action"] == "link_trace_to_memory_context"
    assert _data(result)["context_id"]


def test_link_trace_disabled_with_context_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_OPENMEMORY_ENABLED", raising=False)
    result = maybe_link_trace_to_memory_context("trace-123", context_id="ctx-456")
    assert result["ok"] is True
    assert result["action"] == "link_trace_to_memory_context"
    assert _data(result)["context_id"] == "ctx-456"


def test_link_trace_enabled_server_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OPENMEMORY_ENABLED", "true")
    monkeypatch.setenv("ATELIER_OPENMEMORY_MCP_SERVER_NAME", "openmemory-test")
    result = maybe_link_trace_to_memory_context("trace-123", context_id="ctx-456")
    assert result["ok"] is True
    assert _data(result)["context_id"] == "ctx-456"


# --------------------------------------------------------------------------- #
# maybe_fetch_memory_context_for_task                                        #
# --------------------------------------------------------------------------- #


def test_fetch_context_disabled_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_OPENMEMORY_ENABLED", raising=False)
    result = maybe_fetch_memory_context_for_task("fix the failing test")
    assert result["ok"] is True
    assert result["action"] == "fetch_memory_context_for_task"
    assert "matches" in _data(result)


def test_fetch_context_enabled_server_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OPENMEMORY_ENABLED", "true")
    result = maybe_fetch_memory_context_for_task("update Shopify product metafields", project_id="proj-1")
    assert result["ok"] is True
    assert "matches" in _data(result)


# --------------------------------------------------------------------------- #
# maybe_store_memory_pointer                                                  #
# --------------------------------------------------------------------------- #


def test_store_pointer_disabled_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_OPENMEMORY_ENABLED", raising=False)
    result = maybe_store_memory_pointer("trace-123", "mem-456")
    assert result["ok"] is True
    assert _data(result)["memory_id"] == "mem-456"


def test_store_pointer_enabled_server_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_OPENMEMORY_ENABLED", "true")
    result = maybe_store_memory_pointer("trace-abc", "mem-xyz")
    assert result["ok"] is True
    assert _data(result)["memory_id"] == "mem-xyz"


# --------------------------------------------------------------------------- #
# Response shape — bridge responses share a stable contract                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fn",
    [
        lambda: maybe_link_trace_to_memory_context("t1"),
        lambda: maybe_fetch_memory_context_for_task("task"),
        lambda: maybe_store_memory_pointer("t1", "m1"),
    ],
)
def test_disabled_response_has_required_keys(
    fn: Callable[[], dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATELIER_OPENMEMORY_ENABLED", raising=False)
    result = fn()
    assert "ok" in result
    assert result["ok"] is True
    assert "data" in result
    assert "action" in result


# CLI openmemory tests removed — openmemory top-level group cut in CLI
# consolidation. Backend selection belongs in init --backend=... and
# ATELIER_MEMORY_BACKEND env. Bridge logic is tested via Python API below.


def test_bridge_persists_local_state(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.delenv("ATELIER_OPENMEMORY_ENABLED", raising=False)

    link = maybe_link_trace_to_memory_context("trace-x", context_id="ctx-x")
    store = maybe_store_memory_pointer("trace-x", "mem-x")
    fetch = maybe_fetch_memory_context_for_task("trace-x")

    assert link["ok"] is True
    assert store["ok"] is True
    assert fetch["ok"] is True
    assert cast("int", _data(fetch)["count"]) >= 1
