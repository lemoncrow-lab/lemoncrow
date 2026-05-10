"""Tests for the consolidated 12-surface MCP contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.mcp_server import TOOLS, _handle

EXPECTED_TOOLS = {
    "reasoning",
    "lint",
    "route",
    "rescue",
    "trace",
    "verify",
    "memory",
    "read",
    "edit",
    "sql",
    "search",
    "compact",
    "run",
}

SLIM_TOOLS = {"trace", "read", "edit", "sql", "search", "run"}


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    req: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    resp = _handle(req)
    assert isinstance(resp, dict)
    return resp


def _result(resp: dict[str, Any]) -> Any:
    assert "result" in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


def _seed_store(root: Path) -> None:
    from click.testing import CliRunner

    from atelier.gateway.adapters.cli import cli

    result = CliRunner().invoke(cli, ["--root", str(root), "init"])
    assert result.exit_code == 0, result.output


@pytest.fixture()
def store_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".atelier"
    _seed_store(root)
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_DEV_MODE", "1")
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    return root


def test_initialize_returns_server_info() -> None:
    resp = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }
    )
    assert resp is not None
    assert resp["result"]["serverInfo"]["name"] == "atelier-reasoning"
    assert resp["result"]["protocolVersion"] == "2024-11-05"


def test_notifications_initialized_returns_none() -> None:
    resp = _handle({"jsonrpc": "2.0", "id": None, "method": "notifications/initialized", "params": {}})
    assert resp is None


def test_tools_list_returns_exact_consolidated_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_DEV_MODE", "1")
    resp = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert resp is not None
    names = {tool["name"] for tool in resp["result"]["tools"]}
    assert names == EXPECTED_TOOLS
    assert set(TOOLS) == EXPECTED_TOOLS


def test_tools_list_slim_surface_without_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_DEV_MODE", raising=False)
    resp = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert resp is not None
    names = {tool["name"] for tool in resp["result"]["tools"]}
    assert names == SLIM_TOOLS


def test_tools_list_each_entry_has_schema() -> None:
    resp = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert resp is not None
    for tool in resp["result"]["tools"]:
        assert tool["name"]
        assert isinstance(tool.get("inputSchema"), dict)


def test_unknown_method_returns_error() -> None:
    resp = _handle({"jsonrpc": "2.0", "id": 3, "method": "unknown/method", "params": {}})
    assert resp is not None
    assert resp["error"]["code"] == -32601


def test_unknown_tool_returns_error() -> None:
    resp = _call("does_not_exist", {})
    assert "error" in resp
    assert "unknown tool" in resp["error"]["message"]


def test_get_reasoning_context_can_include_folded_state(store_root: Path) -> None:
    resp = _call(
        "reasoning",
        {"task": "Fix publish regression", "include_run_ledger": True},
    )
    payload = _result(resp)
    assert isinstance(payload.get("context"), str)
    assert "run_ledger" in payload


def test_check_plan_pass_status(store_root: Path) -> None:
    _ = store_root
    payload = _result(_call("lint", {"task": "Add tests", "plan": ["Write tests", "Run pytest"]}))
    assert payload["status"] in {"ok", "pass", "warn", "blocked"}


def test_rescue_failure_returns_procedure(store_root: Path) -> None:
    _ = store_root
    payload = _result(
        _call(
            "rescue",
            {
                "task": "Run tests",
                "error": "pytest AssertionError",
                "recent_actions": ["run pytest", "run pytest"],
            },
        )
    )
    assert "rescue" in payload
    assert "analysis" in payload


def test_record_trace_accepts_monitor_event_payload(store_root: Path) -> None:
    _ = store_root
    payload = _result(
        _call(
            "trace",
            {
                "agent": "codex",
                "domain": "coding",
                "task": "Fix failing tests",
                "status": "partial",
                "event_type": "monitor.warning",
                "event_payload": {"message": "saw repeated command"},
            },
        )
    )
    assert "id" in payload
    assert payload["event_recorded"] is True


def test_run_rubric_gate_pass(store_root: Path) -> None:
    _ = store_root
    payload = _result(
        _call(
            "verify",
            {
                "rubric_id": "rubric_state_change_safety",
                "checks": {
                    "canonical_identifier_used": True,
                    "pre_change_state_captured": True,
                    "read_after_write_completed": True,
                    "observed_state_matches_intent": True,
                    "rollback_plan_available": True,
                    "user_visible_surface_checked": True,
                },
            },
        )
    )
    assert payload["status"] == "pass"


def test_compact_output_op_passthrough(store_root: Path) -> None:
    _ = store_root
    payload = _result(_call("compact", {"op": "output", "content": "short output", "content_type": "bash"}))
    assert payload["compacted"] == "short output"
    assert payload["method"] == "passthrough"


def test_compact_advise_op(store_root: Path) -> None:
    _ = store_root
    payload = _result(_call("compact", {"op": "advise"}))
    assert "should_compact" in payload
    assert "suggested_prompt" in payload


def test_smart_read_and_search_surfaces(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    target = tmp_path / "sample.py"
    target.write_text("def alpha():\n    return 'needle'\n", encoding="utf-8")

    read_payload = _result(_call("read", {"path": str(target), "max_lines": 20}))
    assert read_payload["language"] == "python"

    search_payload = _result(_call("search", {"query": "needle", "path": str(tmp_path)}))
    assert search_payload["mode"] == "chunks"
    assert search_payload["matches"]


def test_smart_edit_surface_applies_patch(store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = store_root
    monkeypatch.chdir(tmp_path)
    target = Path("edit.txt")
    target.write_text("hello world", encoding="utf-8")

    payload = _result(
        _call(
            "edit",
            {
                "edits": [
                    {
                        "path": str(target),
                        "op": "replace",
                        "old_string": "world",
                        "new_string": "atelier",
                    }
                ]
            },
        )
    )
    assert len(payload["applied"]) == 1
    assert target.read_text(encoding="utf-8") == "hello atelier"


def test_repo_map_surface(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    target = tmp_path / "sample.py"
    target.write_text("def alpha():\n    return 1\n", encoding="utf-8")

    payload = _result(
        _call(
            "search",
            {"query": "", "seed_files": [str(target)], "mode": "map", "budget_tokens": 200},
        )
    )
    assert "ranked_files" in payload
