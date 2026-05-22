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
from atelier.core.service.bootstrap_context import build_bootstrap_plan, persist_bootstrap_plan
from atelier.core.service.jobs import JOB_BOOTSTRAP_CONTEXT
from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.cli import cli
from atelier.gateway.adapters.mcp_server import TOOLS, _handle, tool_code, tool_smart_edit
from atelier.infra.code_intel.astgrep import (
    AstGrepToolUnavailable,
    PatternMatch,
    PatternRewriteResult,
    PatternSearchResult,
)
from atelier.infra.storage.factory import create_store, make_memory_store

EXPECTED_TOOLS = {
    "context",
    "route",
    "rescue",
    "trace",
    "verify",
    "memory",
    "read",
    "edit",
    "grep",
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


def _write_gateway_scip_fixture(
    repo_root: Path,
    *,
    symbol_id: str,
    include_call_graph: bool = False,
    artifact_name: str = "python.scip",
    file_path: str = "a.py",
    symbol_name: str = "alpha",
    qualified_name: str = "alpha",
    source: str | None = None,
) -> Path:
    engine = CodeContextEngine(repo_root)
    symbol_source = source or (repo_root / file_path).read_text(encoding="utf-8")
    caller_source = (repo_root / "b.py").read_text(encoding="utf-8") if (repo_root / "b.py").exists() else ""
    artifact_dir = repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / artifact_name
    payload: dict[str, Any] = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "index_sha": "a" * 40,
        "symbols": [
            {
                "symbol_id": symbol_id,
                "repo_id": engine.repo_id,
                "file_path": file_path,
                "language": "python",
                "symbol_name": symbol_name,
                "qualified_name": qualified_name,
                "kind": "function",
                "signature": f"def {symbol_name}():",
                "start_byte": 0,
                "end_byte": len(symbol_source.encode("utf-8")),
                "start_line": 1,
                "end_line": len(symbol_source.splitlines()),
                "content_hash": hashlib.sha256(symbol_source.encode("utf-8")).hexdigest(),
                "source": symbol_source,
                "provenance": "scip",
            }
        ],
    }
    if include_call_graph:
        payload["symbols"].append(
            {
                "symbol_id": "scip-beta",
                "repo_id": engine.repo_id,
                "file_path": "b.py",
                "language": "python",
                "symbol_name": "beta",
                "qualified_name": "beta",
                "kind": "function",
                "signature": "def beta():",
                "start_byte": 0,
                "end_byte": len(caller_source.encode("utf-8")),
                "start_line": 3,
                "end_line": 4,
                "content_hash": hashlib.sha256(caller_source.encode("utf-8")).hexdigest(),
                "source": caller_source,
                "provenance": "scip",
            }
        )
        payload["call_graph"] = {
            "callers": {
                symbol_id: [
                    {
                        "symbol_id": "scip-beta",
                        "symbol_name": "beta",
                        "qualified_name": "beta",
                        "file_path": "b.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ]
            },
            "callees": {
                "scip-beta": [
                    {
                        "symbol_id": symbol_id,
                        "symbol_name": "alpha",
                        "qualified_name": "alpha",
                        "file_path": "a.py",
                        "kind": "function",
                        "start_line": 1,
                        "end_line": 2,
                        "provenance": "scip",
                    }
                ]
            },
        }
    artifact_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return artifact_path


def _write_bootstrap_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "from src.worker import run_worker\n\ndef main() -> str:\n    return run_worker()\n",
        encoding="utf-8",
    )
    (root / "src" / "worker.py").write_text(
        "def run_worker() -> str:\n    return 'ready'\n",
        encoding="utf-8",
    )
    (root / "scripts" / "cli.py").write_text(
        "from src.app import main\n\ndef cli() -> str:\n    return main()\n",
        encoding="utf-8",
    )


def _write_workspace_fixture_repo(root: Path, *, module_name: str, class_name: str = "SharedConfig") -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "config.py").write_text(
        f"class {class_name}:\n    SOURCE = '{module_name}'\n",
        encoding="utf-8",
    )


def _write_workspace_fixture_config(workspace_root: Path, sibling_root: Path) -> None:
    (workspace_root / ".atelier").mkdir(parents=True, exist_ok=True)
    (workspace_root / ".atelier" / "workspace.toml").write_text(
        "\n".join(
            [
                "[workspace]",
                'id = "fixture-workspace"',
                "",
                "[[workspace.repos]]",
                'name = "atelier"',
                'path = "."',
                "",
                "[[workspace.repos]]",
                'name = "billing"',
                f'path = "{os.path.relpath(sibling_root, workspace_root)}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


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


def test_tools_list_returns_exact_consolidated_surface_in_dev_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert "route" not in names
    assert all("passive" not in tool["description"] for tool in tools if tool["name"] in STABLE_LLM_TOOLS)


def test_memory_tool_call_works_without_dev_mode(store_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = store_root
    monkeypatch.delenv("ATELIER_DEV_MODE", raising=False)
    monkeypatch.delenv("ATELIER_SERVICE_URL", raising=False)
    mcp_server._remote_client = None
    resp = _call(
        "memory",
        {
            "op": "block_upsert",
            "agent_id": "atelier:non-dev",
            "label": "visible-memory",
            "value": "Memory should be active in non-dev mode.",
            "metadata": {"source": "pytest"},
        },
    )
    payload = _result(resp)
    assert payload["version"] == 1

    fetched = _result(
        _call(
            "memory",
            {
                "op": "block_get",
                "agent_id": "atelier:non-dev",
                "label": "visible-memory",
            },
        )
    )
    assert fetched["value"] == "Memory should be active in non-dev mode."


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


def test_tools_list_search_schema_prefers_file_path_and_documents_modes() -> None:
    search_tool = TOOLS["search"]
    properties = search_tool["inputSchema"]["properties"]

    assert "query" in search_tool["description"]
    assert "grep" in search_tool["description"]
    assert "file_path" in properties
    assert "path" not in properties
    assert "content_regex" not in properties
    assert "legacy callers may still send `path`" in properties["file_path"]["description"]
    assert "repo map" in properties["mode"]["description"].lower()


def test_tools_list_grep_schema_covers_native_mode() -> None:
    grep_tool = TOOLS["grep"]
    properties = grep_tool["inputSchema"]["properties"]

    assert "regex" in grep_tool["description"].lower()
    assert "file_path" in properties
    assert "path" not in properties
    assert "content_regex" in properties
    assert "summary" in properties




def test_tools_list_edit_schema_documents_descriptor_variants() -> None:
    edit_tool = TOOLS["edit"]
    schema = edit_tool["inputSchema"]
    edits_schema = schema["properties"]["edits"]
    variants = edits_schema["items"]["oneOf"]

    assert schema["required"] == ["edits"]
    assert len(variants) >= 6
    assert {variant["title"] for variant in variants} >= {
        "Legacy replace",
        "Legacy insert_after",
        "Legacy replace_range",
        "Rich file edit",
        "Notebook cell edit",
        "Symbol edit",
    }
    assert "Do not mix" in edits_schema["description"]

def test_tools_list_memory_schema_describes_ops_and_required_fields() -> None:
    memory_tool = TOOLS["memory"]
    properties = memory_tool["inputSchema"]["properties"]

    assert "block_upsert" in memory_tool["description"]
    assert "archive" in memory_tool["description"]
    assert "summarize" in memory_tool["description"]
    assert "block_upsert requires label+value" in properties["op"]["description"]
    assert "block_upsert and block_get" in properties["label"]["description"]
    assert "recall, recall_symbol, and transcript_recall" in properties["query"]["description"]
    assert "session id used by summarize" in properties["session_id"]["description"].lower()
    assert "metadata" not in properties
    assert "expected_version" not in properties
    assert "include" not in properties
    assert "budget_tokens" not in properties


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


def test_context_enqueues_single_bootstrap_job_for_cold_repo(
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = Path(os.environ["CLAUDE_WORKSPACE_ROOT"])
    _write_bootstrap_fixture_repo(workspace_root)
    mcp_server._reset_runtime_cache_for_testing()
    monkeypatch.setattr(mcp_server, "_run_worker_tick_safe", lambda root: None)

    first = mcp_server.tool_get_context({"task": "Map the repo entry points"})
    second = mcp_server.tool_get_context({"task": "Map the repo entry points"})

    store = create_store(store_root)
    store.init()
    jobs = [
        job
        for job in store.list_jobs(job_type=JOB_BOOTSTRAP_CONTEXT, limit=20)
        if job["status"] in {"pending", "running"}
    ]

    assert len(jobs) == 1
    assert first["bootstrap"]["queued"] is True
    assert second["bootstrap"]["queued"] is False


def test_context_worker_tick_persists_bootstrap_blocks_without_blocking_initial_response(
    store_root: Path,
) -> None:
    workspace_root = Path(os.environ["CLAUDE_WORKSPACE_ROOT"])
    _write_bootstrap_fixture_repo(workspace_root)
    mcp_server._reset_runtime_cache_for_testing()

    payload = mcp_server.tool_get_context({"task": "Warm the repository context"})

    assert "Repository bootstrap" not in payload["context"]
    mcp_server._run_worker_tick_safe(store_root)

    plan = build_bootstrap_plan(workspace_root)
    blocks = make_memory_store(store_root).list_pinned_blocks(plan.agent_id)

    assert len([block for block in blocks if block.label.startswith(f"bootstrap/{plan.repo_id}/")]) == 4


def test_context_reuses_bootstrap_blocks_instead_of_enqueuing_duplicate_work(
    store_root: Path,
) -> None:
    workspace_root = Path(os.environ["CLAUDE_WORKSPACE_ROOT"])
    _write_bootstrap_fixture_repo(workspace_root)
    mcp_server._reset_runtime_cache_for_testing()

    mcp_server.tool_get_context({"task": "Warm the repository context"})
    mcp_server._run_worker_tick_safe(store_root)
    mcp_server._reset_runtime_cache_for_testing()
    payload = mcp_server.tool_get_context({"task": "Warm the repository context"})

    store = create_store(store_root)
    store.init()
    jobs = store.list_jobs(job_type=JOB_BOOTSTRAP_CONTEXT, limit=20)

    assert len(jobs) == 1
    assert payload["bootstrap"]["status"] == "warm"
    assert "Repository bootstrap" in payload["context"]


def test_context_injects_preseeded_bootstrap_blocks_without_recomputing(
    store_root: Path,
) -> None:
    workspace_root = Path(os.environ["CLAUDE_WORKSPACE_ROOT"])
    _write_bootstrap_fixture_repo(workspace_root)
    memory_store = make_memory_store(store_root)
    persist_bootstrap_plan(workspace_root, memory_store)
    mcp_server._reset_runtime_cache_for_testing()

    payload = mcp_server.tool_get_context({"task": "Use the warmed bootstrap state"})

    store = create_store(store_root)
    store.init()
    assert store.list_jobs(job_type=JOB_BOOTSTRAP_CONTEXT, limit=20) == []
    assert payload["bootstrap"]["status"] == "warm"
    assert "architecture-sketch" in payload["context"]


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
    for host in ("claude", "codex", "copilot", "opencode", "antigravity"):
        monkeypatch.setenv("ATELIER_AGENT", host)
        assert mcp_server._detect_agent() == host
        monkeypatch.delenv("ATELIER_AGENT", raising=False)


def test_smart_read_and_search_surfaces(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    target = tmp_path / "sample.py"
    target.write_text("def alpha():\n    return 'needle'\n", encoding="utf-8")

    read_payload = _result(_call("read", {"file_path": str(target)}))
    assert read_payload["language"] == "python"

    search_payload = _result(_call("search", {"query": "needle", "file_path": str(tmp_path)}))
    assert search_payload["matches"]

    grep_payload = _result(_call("grep", {"file_path": str(target), "content_regex": "needle"}))
    assert grep_payload["isError"] is False
    assert grep_payload["_meta"]["fileMatchCount"] == 1

    legacy_payload = _result(_call("grep", {"path": str(target), "content_regex": "needle"}))
    assert legacy_payload["_meta"]["fileMatchCount"] == 1


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



def test_smart_edit_rejects_mixed_descriptor_families(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    target = tmp_path / "mixed.txt"
    target.write_text("hello world", encoding="utf-8")

    resp = _call(
        "edit",
        {
            "edits": [
                {"path": str(target), "op": "replace", "old_string": "world", "new_string": "legacy"},
                {"file_path": str(target), "old_string": "hello", "new_string": "rich"},
            ]
        },
    )

    assert "error" in resp
    assert "cannot mix legacy" in resp["error"]["message"]
    assert target.read_text(encoding="utf-8") == "hello world"


def test_smart_edit_legacy_rejects_protected_paths(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    protected = tmp_path / ".atelier" / "state.txt"
    protected.write_text("hello world", encoding="utf-8")

    payload = _result(
        _call(
            "edit",
            {
                "edits": [
                    {
                        "path": str(protected),
                        "op": "replace",
                        "old_string": "world",
                        "new_string": "atelier",
                    }
                ]
            },
        )
    )

    assert payload["rolled_back"] is True
    assert "Protected path denied" in payload["failed"][0]["error"]
    assert protected.read_text(encoding="utf-8") == "hello world"


def test_smart_edit_records_workspace_relative_diff_after_hooks(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = store_root
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other_cwd = tmp_path / "cwd"
    other_cwd.mkdir()
    target = workspace / "edit.txt"
    target.write_text("hello world", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.chdir(other_cwd)

    def fake_hooks(files: list[str], *, repo_root: Path, config: object) -> object:
        target.write_text("hello hooks", encoding="utf-8")

        class HookResult:
            diagnostics: list[object] = []
            steps_ran = ["fake-format"]
            steps_skipped: list[str] = []
            steps_failed: list[str] = []
            total_ms = 1

        return HookResult()

    monkeypatch.setattr(
        "atelier.core.capabilities.tool_supervision.post_edit_hooks.run_post_edit_hooks",
        fake_hooks,
    )

    payload = _result(
        _call(
            "edit",
            {
                "post_edit_hooks": True,
                "edits": [
                    {
                        "file_path": "edit.txt",
                        "old_string": "world",
                        "new_string": "atelier",
                    }
                ],
            },
        )
    )

    assert payload["failed"] == []
    assert target.read_text(encoding="utf-8") == "hello hooks"
    file_events = [event for event in mcp_server._get_ledger().events if event.kind == "file_edit"]
    assert file_events[-1].payload["path"] == "edit.txt"
    assert "hello hooks" in file_events[-1].payload["diff"]
    assert "hello atelier" not in file_events[-1].payload["diff"]

def test_code_context_external_scope_surface_returns_external_hits_only(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _write_gateway_scip_fixture(
        tmp_path,
        symbol_id="scip-requests-get",
        artifact_name="external-python.scip",
        file_path="external/requests/api.py",
        symbol_name="get",
        qualified_name="requests.get",
        source="def get(url: str) -> str:\n    return url\n",
    )

    repo_payload = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "get"})
    external_payload = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "get", "scope": "external"})

    assert repo_payload["items"] == []
    assert [item["qualified_name"] for item in external_payload["items"]] == ["requests.get"]
    assert external_payload["items"][0]["origin"] == "external"


def test_edit_symbol_rejects_external_target_cleanly(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = store_root
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _write_gateway_scip_fixture(
        tmp_path,
        symbol_id="scip-requests-get",
        artifact_name="external-python.scip",
        file_path="external/requests/api.py",
        symbol_name="get",
        qualified_name="requests.get",
        source="def get(url: str) -> str:\n    return url\n",
    )

    payload = tool_smart_edit(
        {
            "edits": [
                {
                    "kind": "symbol",
                    "symbol_id": "scip-requests-get",
                    "mode": "replace",
                    "new_body": "def get(url: str) -> str:\n    return 'patched'\n",
                }
            ]
        }
    )

    assert payload["rolled_back"] is True
    assert payload["failed"][0]["error"] == "external_symbol_edit_not_allowed"


def test_code_context_workspace_search_returns_repo_tagged_hits_and_repo_filter(
    store_root: Path,
    tmp_path: Path,
) -> None:
    _ = store_root
    billing_root = tmp_path.parent / "billing"
    _write_workspace_fixture_repo(tmp_path, module_name="atelier")
    _write_workspace_fixture_repo(billing_root, module_name="billing")
    _write_workspace_fixture_config(tmp_path, billing_root)

    payload = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "SharedConfig", "budget_tokens": 4000})
    billing_only = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "SharedConfig",
            "repo": "billing",
            "budget_tokens": 4000,
        }
    )

    assert [(item["repo_name"], item["file_path"]) for item in payload["items"]] == [
        ("atelier", "src/config.py"),
        ("billing", "src/config.py"),
    ]
    assert [item["repo_name"] for item in billing_only["items"]] == ["billing"]


def test_code_context_workspace_symbol_filter_and_external_origin_metadata(
    store_root: Path,
    tmp_path: Path,
) -> None:
    _ = store_root
    billing_root = tmp_path.parent / "billing"
    _write_workspace_fixture_repo(tmp_path, module_name="atelier")
    _write_workspace_fixture_repo(billing_root, module_name="billing")
    _write_workspace_fixture_config(tmp_path, billing_root)
    _write_gateway_scip_fixture(
        billing_root,
        symbol_id="scip-requests-get",
        artifact_name="external-python.scip",
        file_path="external/requests/api.py",
        symbol_name="get",
        qualified_name="requests.get",
        source="def get(url: str) -> str:\n    return url\n",
    )

    default_symbol = tool_code({"op": "symbol", "repo_root": str(tmp_path), "symbol_name": "SharedConfig"})
    billing_symbol = tool_code(
        {
            "op": "symbol",
            "repo_root": str(tmp_path),
            "symbol_name": "SharedConfig",
            "repo": "billing",
        }
    )
    external_payload = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "get",
            "scope": "external",
            "repo": "billing",
        }
    )

    assert default_symbol["repo_name"] == "atelier"
    assert billing_symbol["repo_name"] == "billing"
    assert billing_symbol["qualified_name"] == "SharedConfig"
    assert external_payload["items"][0]["repo_name"] == "billing"
    assert external_payload["items"][0]["origin"] == "external"


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
    assert all("snippet" not in item for item in searched["items"])
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

    impact = _result(_call("code", {"op": "impact", "repo_root": str(tmp_path), "path": "a.py"}))
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

    payload = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "OrderService",
            "snippet": "head",
            "snippet_lines": 2,
            "file_glob": "src/*.py",
            "scope": "repo",
            "budget_tokens": 4000,
        }
    )

    assert payload["cache_hit"] is False
    assert payload["provenance"] == "local"
    assert payload["provenance_breakdown"] == {"local": len(payload["items"])}
    assert payload["items"][0]["file_path"] == "src/orders.py"
    assert (
        payload["items"][0]["snippet"] == "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:"
    )


def test_tool_code_search_dispatches_mode_without_gateway_ranking_logic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_search.return_value = {
        "items": [{"symbol_name": "issue_access_token", "provenance": "local"}],
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 10,
        "total_tokens": 80,
        "mode": "semantic",
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "create login token for authenticated user",
            "mode": "semantic",
            "budget_tokens": 220,
        }
    )

    assert payload["mode"] == "semantic"
    fake_engine.tool_search.assert_called_once_with(
        "create login token for authenticated user",
        limit=20,
        mode="semantic",
        kind=None,
        language=None,
        snippet="none",
        snippet_lines=8,
        file_glob=None,
        scope="repo",
        budget_tokens=220,
    )


def test_tool_code_search_dispatches_deleted_scope_filters_without_gateway_history_logic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_search.return_value = {
        "items": [
            {
                "symbol_name": "LegacyCheckout",
                "provenance": "graveyard",
                "deleted_at_sha": "abc123",
                "rename_target": "modern.py",
            }
        ],
        "cache_hit": False,
        "provenance": "graveyard",
        "tokens_saved": 11,
        "total_tokens": 120,
        "mode": "auto",
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "ModernCheckout",
            "scope": "deleted",
            "since": "2025-01-01",
            "touched_by": "history@example.com",
            "budget_tokens": 220,
        }
    )

    assert payload["provenance"] == "graveyard"
    assert payload["items"][0]["rename_target"] == "modern.py"
    fake_engine.tool_search.assert_called_once_with(
        "ModernCheckout",
        limit=20,
        mode="auto",
        kind=None,
        language=None,
        snippet="none",
        snippet_lines=8,
        file_glob=None,
        scope="deleted",
        since="2025-01-01",
        touched_by="history@example.com",
        budget_tokens=220,
    )


def test_tool_code_blame_dispatches_additively_without_gateway_aggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_blame.return_value = {
        "symbol_name": "risk_score",
        "qualified_name": "risk_score",
        "file_path": "service.py",
        "freshness": "fresh",
        "last_author": "carol@example.com",
        "last_commit_sha": "abc123",
        "local_edits": False,
        "distinct_authors": 2,
        "cache_hit": False,
        "provenance": "blame",
        "tokens_saved": 12,
        "total_tokens": 150,
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = tool_code(
        {
            "op": "blame",
            "repo_root": str(tmp_path),
            "query": "risk_score",
            "include_churn": False,
            "budget_tokens": 220,
        }
    )

    assert payload["provenance"] == "blame"
    assert payload["symbol_name"] == "risk_score"
    fake_engine.tool_blame.assert_called_once_with(
        query="risk_score",
        symbol_id=None,
        qualified_name=None,
        symbol_name=None,
        file_path=None,
        include_churn=False,
        budget_tokens=220,
    )


def test_tool_code_include_churn_remains_additive_for_non_blame_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine = MagicMock()
    fake_engine.tool_search.return_value = {
        "items": [{"symbol_name": "OrderService", "file_path": "src/orders.py", "provenance": "local"}],
        "cache_hit": False,
        "provenance": "local",
        "tokens_saved": 10,
        "total_tokens": 100,
        "mode": "auto",
    }
    monkeypatch.setattr(
        "atelier.gateway.adapters.mcp_server._code_context_engine",
        lambda repo_root=".": fake_engine,
    )

    payload = tool_code(
        {
            "op": "search",
            "repo_root": str(tmp_path),
            "query": "OrderService",
            "include_churn": False,
            "budget_tokens": 220,
        }
    )

    assert payload["provenance"] == "local"
    fake_engine.tool_search.assert_called_once_with(
        "OrderService",
        limit=20,
        mode="auto",
        kind=None,
        language=None,
        snippet="none",
        snippet_lines=8,
        file_glob=None,
        scope="repo",
        budget_tokens=220,
    )


def test_code_context_usages_surface_groups_references(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "checkout.py").write_text(
        "from src.orders import OrderService\n\n"
        "def checkout(items: list[int]) -> int:\n"
        "    return OrderService().calculate_total(items)\n",
        encoding="utf-8",
    )

    payload = _result(_call("code", {"op": "usages", "repo_root": str(tmp_path), "query": "OrderService"}))

    assert payload["cache_hit"] is False
    assert payload["group_by"] == "file"
    assert payload["target"]["qualified_name"] == "OrderService"
    assert "src/checkout.py" in payload["references"]
    assert payload["references"]["src/checkout.py"][0]["provenance"] == "treesitter"


def test_code_context_call_graph_surface_is_additive(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from a import alpha\n\ndef beta():\n    return alpha()\n", encoding="utf-8")
    _write_gateway_scip_fixture(tmp_path, symbol_id="scip-alpha", include_call_graph=True)

    callers = _result(_call("code", {"op": "callers", "repo_root": str(tmp_path), "query": "alpha"}))
    callees = _result(_call("code", {"op": "callees", "repo_root": str(tmp_path), "query": "beta", "snapshot": True}))

    assert callers["cache_hit"] is False
    assert callers["provenance"] == "scip"
    assert callers["data_status"] == "available"
    assert callers["related"][0]["qualified_name"] == "beta"
    assert callees["snapshot"]["direction"] == "callees"
    assert callees["edges"][0]["callee_symbol_id"] == "scip-alpha"


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


def test_code_context_pattern_search_surface_is_cached(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = store_root
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("requests.get(url)\n", encoding="utf-8")

    monkeypatch.setattr(
        "atelier.core.capabilities.code_context.engine.AstGrepAdapter.search",
        lambda self, *, pattern, language=None, file_glob=None, limit=20: PatternSearchResult(
            matches=[
                PatternMatch(
                    file_path="src/app.py",
                    line=1,
                    column=0,
                    end_line=1,
                    end_column=17,
                    snippet="requests.get(url)",
                    captures={"URL": "url"},
                )
            ],
            truncated=False,
            total_matches=1,
        ),
    )

    first = _result(
        _call(
            "code",
            {
                "op": "pattern",
                "repo_root": str(tmp_path),
                "pattern": "requests.get($URL)",
                "budget_tokens": 220,
            },
        )
    )
    cached = _result(
        _call(
            "code",
            {
                "op": "pattern",
                "repo_root": str(tmp_path),
                "pattern": "requests.get($URL)",
                "budget_tokens": 220,
            },
        )
    )

    assert first["cache_hit"] is False
    assert first["provenance"] == "ast-grep"
    assert first["matches"][0]["captures"] == {"URL": "url"}
    assert first["total_tokens"] <= 220
    assert cached["cache_hit"] is True
    assert cached["provenance"] == "cached"


def test_code_context_cache_diagnostics_surface_is_additive(store_root: Path, tmp_path: Path) -> None:
    _ = store_root
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )

    _result(
        _call(
            "code",
            {
                "op": "search",
                "repo_root": str(tmp_path),
                "query": "OrderService",
                "budget_tokens": 4000,
            },
        )
    )
    _result(
        _call(
            "code",
            {
                "op": "symbol",
                "repo_root": str(tmp_path),
                "qualified_name": "OrderService",
                "file_path": "src/orders.py",
                "budget_tokens": 4000,
            },
        )
    )

    status = _result(_call("code", {"op": "cache_status", "repo_root": str(tmp_path), "budget_tokens": 200}))
    invalidated = _result(
        _call(
            "code",
            {
                "op": "cache_invalidate",
                "repo_root": str(tmp_path),
                "cache_tool": "search",
                "budget_tokens": 200,
            },
        )
    )

    assert status["entries_by_tool"] == {"code.search": 1, "code.symbol": 1}
    assert "items" not in status
    assert "matches" not in status
    assert invalidated["scope"]["cache_tool"] == "search"
    assert invalidated["invalidated_entries"] == 1


def test_code_context_pattern_rewrite_reindexes_changed_files(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = store_root
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("requests.get(url)\n", encoding="utf-8")

    def fake_rewrite(self, *, pattern, rewrite, language=None, file_glob=None, dry_run=True):  # type: ignore[no-untyped-def]
        before = target.read_text(encoding="utf-8")
        after = before.replace("requests.get(url)", "requests.get(url, timeout=30)")
        diff = "--- a/src/app.py\n+++ b/src/app.py\n@@\n-requests.get(url)\n+requests.get(url, timeout=30)\n"
        if not dry_run:
            target.write_text(after, encoding="utf-8")
        return PatternRewriteResult(diff=diff, files_changed=["src/app.py"])

    reindexed: list[list[str]] = []

    monkeypatch.setattr("atelier.core.capabilities.code_context.engine.AstGrepAdapter.rewrite", fake_rewrite)
    monkeypatch.setattr(
        CodeContextEngine,
        "_reindex_files",
        lambda self, file_paths: reindexed.append(list(file_paths)),
        raising=False,
    )

    preview = _result(
        _call(
            "code",
            {
                "op": "pattern",
                "repo_root": str(tmp_path),
                "pattern": "requests.get($URL)",
                "rewrite": "requests.get($URL, timeout=30)",
                "dry_run": True,
            },
        )
    )
    applied = _result(
        _call(
            "code",
            {
                "op": "pattern",
                "repo_root": str(tmp_path),
                "pattern": "requests.get($URL)",
                "rewrite": "requests.get($URL, timeout=30)",
                "dry_run": False,
            },
        )
    )

    assert "--- a/src/app.py" in preview["diff"]
    assert applied["files_changed"] == ["src/app.py"]
    assert reindexed == [["src/app.py"]]
    assert target.read_text(encoding="utf-8") == "requests.get(url, timeout=30)\n"


def test_code_context_pattern_returns_structured_tool_unavailable(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = store_root
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("requests.get(url)\n", encoding="utf-8")

    payload = {
        "error": "tool_unavailable",
        "tool": "ast-grep",
        "expected_binary": "ast-grep",
        "message": "ast-grep is unavailable",
        "checked": [],
        "hint": "install ast-grep",
    }
    monkeypatch.setattr(
        "atelier.core.capabilities.code_context.engine.AstGrepAdapter.search",
        lambda self, *, pattern, language=None, file_glob=None, limit=20: (_ for _ in ()).throw(
            AstGrepToolUnavailable(payload)
        ),
    )

    result = _result(_call("code", {"op": "pattern", "repo_root": str(tmp_path), "pattern": "requests.get($URL)"}))

    assert result["error"] == "tool_unavailable"
    assert result["expected_binary"] == "ast-grep"


# ---------------------------------------------------------------------------
# Remaining-gap regression tests (Issues 4, 13, 14 and shell failure fix)
# ---------------------------------------------------------------------------


def test_path_safety_module_is_importable_and_has_protected_parts() -> None:
    """Centralised PROTECTED_PARTS frozenset must exist and cover the canonical dirs."""
    from atelier.core.capabilities.tool_supervision.path_safety import PROTECTED_PARTS

    required = {".git", ".atelier", "node_modules", ".venv"}
    assert required <= set(PROTECTED_PARTS), f"Missing entries: {required - set(PROTECTED_PARTS)}"


def test_batch_edit_and_rich_edit_share_path_safety_constant() -> None:
    """Both edit modules must reference the same PROTECTED_PARTS set (no local forks)."""
    from atelier.core.capabilities.tool_supervision import batch_edit, rich_edit
    from atelier.core.capabilities.tool_supervision.path_safety import PROTECTED_PARTS

    # Neither module should define its own _PROTECTED_PARTS
    assert not hasattr(batch_edit, "_PROTECTED_PARTS"), "batch_edit still has local _PROTECTED_PARTS"
    assert not hasattr(rich_edit, "_PROTECTED_PARTS"), "rich_edit still has local _PROTECTED_PARTS"

    # Both modules imported PROTECTED_PARTS from path_safety
    assert batch_edit.PROTECTED_PARTS is PROTECTED_PARTS
    assert rich_edit.PROTECTED_PARTS is PROTECTED_PARTS


def test_trace_compact_receipt_always_present(store_root: Path) -> None:
    """tool_record_trace must always return ok, trace_id, stored — the compact receipt."""
    _ = store_root
    payload = _result(
        _call(
            "trace",
            {
                "agent": "atelier:code",
                "domain": "mcp-server",
                "task": "Verify compact receipt",
                "status": "success",
            },
        )
    )
    assert payload.get("ok") is True, f"'ok' missing or False in trace receipt: {payload}"
    assert payload.get("stored") is True, f"'stored' missing or False in trace receipt: {payload}"
    assert isinstance(payload.get("trace_id"), str) and payload["trace_id"], (
        f"'trace_id' missing or empty in trace receipt: {payload}"
    )


def test_route_decide_summary_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tool_route op=decide must return a _summary dict with required fields."""
    root = tmp_path / ".atelier"
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    import atelier.gateway.adapters.mcp_server as m

    m._current_ledger = None

    payload = _result(
        _call(
            "route",
            {
                "op": "decide",
                "user_goal": "Fix a bug in the parser",
                "repo_root": ".",
                "task_type": "debug",
                "risk_level": "medium",
                "step_type": "edit",
                "step_index": 1,
            },
        )
    )

    assert "_summary" in payload, f"'_summary' key missing from route decide response: {list(payload)}"
    summary = payload["_summary"]
    assert "recommended_route" in summary, f"_summary missing 'recommended_route': {summary}"
    assert "required_validation" in summary, f"_summary missing 'required_validation': {summary}"
    assert "risk" in summary, f"_summary missing 'risk': {summary}"


def test_shell_failure_preserves_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """For failing commands, the tail of stdout must be preserved even when output is long."""
    from atelier.gateway.adapters.mcp_server import _run_shell_tool

    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))

    # Generate 300 numbered lines then exit 1 — only tail should survive truncation
    result = _run_shell_tool(
        "python3 -c \""
        "import sys; "
        "[print(f'line-{i}') for i in range(300)]; "
        "sys.exit(1)"
        "\"",
        max_lines=60,
    )

    assert result["exit_code"] == 1
    stdout = result["stdout"]
    # The last line must be visible (line-299)
    assert "line-299" in stdout, (
        f"tail not preserved for failing command; stdout tail:\n{stdout[-500:]}"
    )
