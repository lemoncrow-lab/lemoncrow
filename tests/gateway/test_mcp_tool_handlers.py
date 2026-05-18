"""Tests for the consolidated MCP contract."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.environment import (
    DEV_LLM_TOOLS,
    NON_DEV_LLM_TOOLS,
    STABLE_LLM_TOOLS,
)
from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.cli import cli
from atelier.gateway.adapters.mcp_server import TOOLS, _handle

EXPECTED_TOOLS = {
    "context",
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
    "code",
    "shell",
}


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


def _mock_client(return_values: dict[str, dict[str, Any]]) -> MagicMock:
    client = MagicMock()
    for method_name, retval in return_values.items():
        getattr(client, method_name).return_value = retval
    return client


def _write_gateway_scip_fixture(repo_root: Path, *, symbol_id: str) -> Path:
    engine = CodeContextEngine(repo_root)
    source = (repo_root / "a.py").read_text(encoding="utf-8")
    artifact_dir = repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "python.scip"
    artifact_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repo_id": engine.repo_id,
                "language": "python",
                "symbols": [
                    {
                        "symbol_id": symbol_id,
                        "repo_id": engine.repo_id,
                        "file_path": "a.py",
                        "language": "python",
                        "symbol_name": "alpha",
                        "qualified_name": "alpha",
                        "kind": "function",
                        "signature": "def alpha():",
                        "start_byte": 0,
                        "end_byte": len(source.encode("utf-8")),
                        "start_line": 1,
                        "end_line": 2,
                        "content_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                        "source": source,
                        "provenance": "scip",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return artifact_path


@pytest.fixture()
def store_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".atelier"
    _seed_store(root)
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_DEV_MODE", "1")
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    mcp_server._remote_client = _mock_client(
        {
            "get_context": {"context": "Here are the relevant procedures.", "run_ledger": []},
            "rescue_failure": {
                "rescue": "Try a narrower reproduction.",
                "analysis": "repeat failure",
            },
            "record_trace": {"id": "trace-123", "event_recorded": True},
            "run_rubric_gate": {"status": "pass"},
        }
    )
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
    assert resp["result"]["serverInfo"]["name"] == "atelier-context"
    assert resp["result"]["protocolVersion"] == "2024-11-05"


def test_notifications_initialized_returns_none() -> None:
    resp = _handle({"jsonrpc": "2.0", "id": None, "method": "notifications/initialized", "params": {}})
    assert resp is None


def test_tools_list_returns_exact_consolidated_surface_in_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_DEV_MODE", "1")
    resp = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert resp is not None
    names = {tool["name"] for tool in resp["result"]["tools"]}
    assert names == EXPECTED_TOOLS
    assert set(TOOLS) == EXPECTED_TOOLS


def test_tools_list_only_product_tools_without_dev_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATELIER_DEV_MODE", raising=False)
    resp = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert resp is not None
    tools = resp["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == NON_DEV_LLM_TOOLS
    assert names == STABLE_LLM_TOOLS
    assert not (names & DEV_LLM_TOOLS)
    assert all("passive" not in tool["description"] for tool in tools if tool["name"] in STABLE_LLM_TOOLS)


def test_cli_tools_list_respects_stable_and_dev_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_DEV_MODE", raising=False)
    runner = CliRunner()

    stable = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "tools", "list"])
    assert stable.exit_code == 0, stable.output
    assert set(stable.output.splitlines()) == STABLE_LLM_TOOLS

    dev = runner.invoke(cli, ["--root", str(tmp_path / ".atelier"), "tools", "list", "--dev"])
    assert dev.exit_code == 0, dev.output
    assert set(dev.output.splitlines()) == EXPECTED_TOOLS
    assert "ATELIER_DEV_MODE" not in os.environ


def test_cli_tools_call_invokes_stable_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_DEV_MODE", raising=False)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path / ".atelier"),
            "tools",
            "call",
            "compact",
            "--args",
            '{"op":"output","content":"hello world","budget_tokens":10}',
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["method"] == "passthrough"
    assert payload["compacted"] == "hello world"


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


def test_get_context_can_include_folded_state(store_root: Path) -> None:
    resp = _call(
        "context",
        {"task": "Fix publish regression", "include_run_ledger": True},
    )
    payload = _result(resp)
    assert isinstance(payload.get("context"), str)
    assert "run_ledger" in payload


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
    assert "should_advise" in payload
    assert "should_handover" in payload
    assert "suggested_prompt" in payload


def test_compact_auto_gate_requires_boundary_and_turns(store_root: Path) -> None:
    _ = store_root
    led = mcp_server._get_ledger()
    led.token_count = 160_000
    for idx in range(16):
        led.record("agent_message", f"working turn {idx}", {"idx": idx})

    waiting = mcp_server._compact_advise()
    assert waiting["should_advise"] is True
    assert waiting["should_compact"] is False
    assert waiting["task_boundary_detected"] is False

    led.record_test("pytest", passed=True, detail="tests passed")
    ready = mcp_server._compact_advise()
    assert ready["should_auto_compact"] is True
    assert ready["should_compact"] is True
    assert ready["task_boundary_detected"] is True


def test_compact_high_utilisation_bypasses_turns_gate(store_root: Path) -> None:
    # Five huge turns push utilisation to >=90% before the 15-turn gate is met.
    # The high-utilisation override should fire auto-compact at a task boundary
    # even though turn_count < AUTO_COMPACT_MIN_TURNS.
    _ = store_root
    led = mcp_server._get_ledger()
    led.token_count = 181_000  # 90.5% of 200k
    for idx in range(5):
        led.record("agent_message", f"dense turn {idx}", {"idx": idx})
    led.record_test("pytest", passed=True, detail="all green")

    result = mcp_server._compact_advise()
    assert result["turn_count"] < mcp_server.AUTO_COMPACT_MIN_TURNS
    assert result["should_auto_compact"] is True
    assert "override" in result["reason"] or "auto-compact threshold" in result["reason"]


def test_compact_handover_writes_markdown(store_root: Path) -> None:
    root = store_root
    led = mcp_server._get_ledger()
    led.session_id = "handover-session"
    led.task = "Finish a large refactor"
    led.token_count = 190_000
    led.record_file_event("src/app.py", "edit", diff="--- a/src/app.py\n+++ b/src/app.py\n")

    payload = mcp_server._compact_advise()

    assert payload["should_handover"] is True
    assert payload["handover_file"]
    handover_path = Path(payload["handover_file"])
    assert handover_path == root / "runs" / "handover-session" / "HANDOVER.md"
    assert "Session Handover" in handover_path.read_text(encoding="utf-8")


def test_model_recommendation_emitted_before_tool_dispatch(store_root: Path) -> None:
    _ = store_root
    _result(_call("compact", {"op": "output", "content": "short output", "content_type": "bash"}))

    led = mcp_server._get_ledger()
    recommendations = [event for event in led.events if event.kind == "model_recommendation"]
    assert recommendations
    assert recommendations[-1].payload["tool_name"] == "compact"
    assert recommendations[-1].payload["tier"] in {"cheap", "medium", "expensive"}
    assert recommendations[-1].payload["lever"] == "model_routing"
    assert recommendations[-1].payload["tokens_saved"] == 0
    assert recommendations[-1].payload["cost_saved_usd"] >= 0


def test_compact_session_op_emits_session_compaction_savings(monkeypatch: pytest.MonkeyPatch, store_root: Path) -> None:
    _ = store_root
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_server, "_append_live_savings_event", lambda event: events.append(event))
    led = mcp_server._get_ledger()
    led.token_count = 48_000
    for idx in range(4):
        led.record("agent_message", f"working turn {idx}", {"idx": idx})

    payload = mcp_server._compress_context()

    session_events = [event for event in events if event.get("kind") == "session_compaction"]
    assert session_events
    assert session_events[-1]["lever"] == "session_compaction"
    assert session_events[-1]["tokens_saved"] > 0
    assert session_events[-1]["cost_saved_usd"] > 0
    assert payload["tokens_freed"] == session_events[-1]["tokens_saved"]
    assert payload["cost_saved_usd"] == session_events[-1]["cost_saved_usd"]


def test_compact_advise_emits_session_compaction_savings_when_auto_compacting(
    monkeypatch: pytest.MonkeyPatch, store_root: Path
) -> None:
    _ = store_root
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp_server, "_append_live_savings_event", lambda event: events.append(event))
    led = mcp_server._get_ledger()
    led.token_count = 160_000
    for idx in range(16):
        led.record("agent_message", f"working turn {idx}", {"idx": idx})
    led.record_test("pytest", passed=True, detail="tests passed")

    payload = mcp_server._compact_advise()

    session_events = [event for event in events if event.get("kind") == "session_compaction"]
    assert payload["should_compact"] is True
    assert session_events
    assert session_events[-1]["trigger"] == "compact_advise"
    assert session_events[-1]["tokens_saved"] == payload["tokens_freed"]
    assert session_events[-1]["cost_saved_usd"] == payload["cost_saved_usd"]


def test_detect_agent_supports_all_five_cli_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    for host in ("claude", "codex", "copilot", "opencode", "gemini"):
        monkeypatch.setenv("ATELIER_AGENT", host)
        assert mcp_server._detect_agent() == host
        monkeypatch.delenv("ATELIER_AGENT", raising=False)


def test_smart_read_and_search_surfaces(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    target = tmp_path / "sample.py"
    target.write_text("def alpha():\n    return 'needle'\n", encoding="utf-8")

    read_payload = _result(_call("read", {"path": str(target), "max_lines": 20}))
    assert read_payload["language"] == "python"

    search_payload = _result(_call("search", {"path": str(target), "content_regex": "needle"}))
    assert search_payload["isError"] is False
    assert search_payload["_meta"]["fileMatchCount"] == 1


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


def test_code_context_mcp_surfaces(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from a import alpha\n\ndef beta():\n    return alpha()\n", encoding="utf-8")

    indexed = _result(_call("code", {"op": "index", "repo_root": str(tmp_path)}))
    assert indexed["symbols_indexed"] >= 2
    assert indexed["cache_hit"] is False
    assert indexed["provenance"] == "local"

    searched = _result(_call("code", {"op": "search", "repo_root": str(tmp_path), "query": "alpha"}))
    assert searched["items"]
    assert searched["cache_hit"] is False
    cached_search = _result(_call("code", {"op": "search", "repo_root": str(tmp_path), "query": "alpha"}))
    assert cached_search["cache_hit"] is True
    assert cached_search["provenance"] == "cached"

    symbol = _result(
        _call(
            "code",
            {
                "op": "symbol",
                "repo_root": str(tmp_path),
                "qualified_name": "alpha",
                "file_path": "a.py",
            },
        )
    )
    assert "def alpha" in symbol["source"]
    assert symbol["provenance"] == "local"

    outline = _result(_call("code", {"op": "outline", "repo_root": str(tmp_path), "file_path": "a.py"}))
    assert "a.py" in outline["files"]
    assert outline["provenance"] == "local"

    context = _result(
        _call(
            "code",
            {
                "op": "context",
                "repo_root": str(tmp_path),
                "task": "change alpha",
                "seed_files": ["a.py"],
                "budget_tokens": 300,
            },
        )
    )
    assert context["token_count"] <= context["budget_tokens"]
    assert context["provenance"] == "local"

    impact = _result(_call("code", {"op": "impact", "repo_root": str(tmp_path), "file_path": "a.py"}))
    assert "b.py" in impact["direct_importers"]
    assert impact["provenance"] == "local"


def test_code_context_mcp_routes_scip_and_invalidates_cache(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    artifact_path = _write_gateway_scip_fixture(tmp_path, symbol_id="scip-alpha-v1")

    first = _result(_call("code", {"op": "search", "repo_root": str(tmp_path), "query": "alpha"}))
    cached = _result(_call("code", {"op": "search", "repo_root": str(tmp_path), "query": "alpha"}))
    artifact_path.write_text(
        artifact_path.read_text(encoding="utf-8").replace("scip-alpha-v1", "scip-alpha-v2"),
        encoding="utf-8",
    )
    fresh = _result(_call("code", {"op": "search", "repo_root": str(tmp_path), "query": "alpha"}))

    assert first["cache_hit"] is False
    assert first["provenance"] == "scip"
    assert first["items"][0]["symbol_id"] == "scip-alpha-v1"
    assert cached["cache_hit"] is True
    assert fresh["cache_hit"] is False
    assert fresh["provenance"] == "scip"
    assert fresh["items"][0]["symbol_id"] == "scip-alpha-v2"


def test_code_context_search_surface_supports_snippet_scope_and_glob(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_orders.py").write_text(
        "from src.orders import OrderService\n",
        encoding="utf-8",
    )

    payload = _result(
        _call(
            "code",
            {
                "op": "search",
                "repo_root": str(tmp_path),
                "query": "OrderService",
                "snippet": "head",
                "snippet_lines": 2,
                "file_glob": "src/*.py",
                "scope": "repo",
                "budget_tokens": 4000,
            },
        )
    )

    assert payload["cache_hit"] is False
    assert payload["provenance"] == "local"
    assert payload["provenance_breakdown"] == {"local": len(payload["items"])}
    assert payload["items"][0]["file_path"] == "src/orders.py"
    assert payload["items"][0]["snippet"] == "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:"


def test_code_context_mcp_falls_back_when_scip_artifact_is_invalid(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path)
    artifact_dir = tmp_path / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "python.scip").write_text("{invalid json", encoding="utf-8")

    searched = _result(_call("code", {"op": "search", "repo_root": str(tmp_path), "query": "alpha"}))

    assert searched["cache_hit"] is False
    assert searched["provenance"] == "local"
    assert searched["items"][0]["symbol_name"] == "alpha"
