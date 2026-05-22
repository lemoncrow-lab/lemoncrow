from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner

from atelier.core.capabilities.cross_vendor_routing.configuration import (
    RouteConfig,
    save_route_config,
)
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
    "grep",
    "sql",
    "search",
    "compact",
    "code",
    "shell",
}


def _seed_store(root: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(root), "init"])
    assert result.exit_code == 0, result.output


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    response = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    assert response is not None
    return cast(dict[str, Any], response)


def _payload(response: dict[str, Any]) -> dict[str, Any]:
    assert "result" in response, response
    payload = json.loads(response["result"]["content"][0]["text"])
    assert isinstance(payload, dict)
    return payload


class _FakeRemoteClient:
    def __init__(self) -> None:
        self._blocks: dict[tuple[str, str], dict[str, Any]] = {}
        self._archives: list[dict[str, Any]] = []
        self._trace_count = 0

    def get_context(self, args: dict[str, Any]) -> dict[str, Any]:
        passages = [
            item
            for item in self._archives
            if args.get("agent_id") is None or item.get("agent_id") == args.get("agent_id")
        ]
        context = "Here are the relevant procedures."
        if passages:
            context += "\n<memory>Use archived guidance.</memory>"
        return {"context": context, "recalled_passages": passages}

    def memory(self, args: dict[str, Any]) -> dict[str, Any] | None:
        op = args["op"]
        if op == "block_upsert":
            key = (str(args["agent_id"]), str(args["label"]))
            version = int(self._blocks.get(key, {}).get("version", 0)) + 1
            block = {
                "id": f"block-{args['agent_id']}-{args['label']}",
                "agent_id": args["agent_id"],
                "label": args["label"],
                "value": args["value"],
                "version": version,
                "pinned": bool(args.get("pinned", False)),
            }
            self._blocks[key] = block
            return block
        if op == "block_get":
            return self._blocks.get((str(args["agent_id"]), str(args["label"])))
        if op == "archive":
            archived = {
                "id": f"mem-{len(self._archives) + 1}",
                "agent_id": args["agent_id"],
                "text": args["text"],
                "tags": list(args.get("tags", [])),
                "dedup_hit": False,
            }
            self._archives.append(archived)
            return archived
        if op == "recall":
            query = str(args.get("query", "")).lower()
            tags = {str(tag) for tag in args.get("tags", [])}
            passages = [
                item
                for item in self._archives
                if query in str(item["text"]).lower() and (not tags or tags.issubset(set(item.get("tags", []))))
            ]
            return {"passages": passages[: int(args.get("top_k", 5) or 5)]}
        raise ValueError(f"memory op not supported in remote mode: {op}")

    def rescue_failure(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "rescue": "Try a narrower reproduction.",
            "analysis": f"Investigate failure for {args.get('task', 'task')}.",
            "matched_blocks": ["read-after-write-verification"],
        }

    def record_trace(self, args: dict[str, Any]) -> dict[str, Any]:
        self._trace_count += 1
        return {
            "id": f"trace-{self._trace_count}",
            "session_id": "remote-session",
            "status": args.get("status", "success"),
        }

    def run_rubric_gate(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"status": "pass", "rubric_id": args.get("rubric_id")}


@pytest.fixture()
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".atelier"
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    _seed_store(root)

    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("ATELIER_EMBEDDER", "null")
    monkeypatch.setenv("ATELIER_DEV_MODE", "1")

    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    mcp_server._remote_client = None
    mcp_server._product_session_id = None
    mcp_server._product_session_started_at = None
    mcp_server._reset_runtime_cache_for_testing()
    mcp_server._remote_client = _FakeRemoteClient()

    mcp_server._last_plan_hash_by_session.clear()
    mcp_server._last_plan_by_session.clear()
    mcp_server._last_blocked_plan_hash_by_session.clear()
    return tmp_path


def test_tools_list_matches_registered_surface(mcp_env: Path) -> None:
    _ = mcp_env
    response = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == EXPECTED_TOOLS
    assert set(TOOLS) == EXPECTED_TOOLS


def test_tools_list_only_product_tools_without_dev_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".atelier"
    _seed_store(root)
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("ATELIER_DEV_MODE", raising=False)
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    response = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response is not None
    tools = response["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == NON_DEV_LLM_TOOLS
    assert names == STABLE_LLM_TOOLS
    assert not (names & DEV_LLM_TOOLS)
    assert all("passive" not in tool["description"] for tool in tools if tool["name"] in STABLE_LLM_TOOLS)


def test_non_remote_tool_calls_fallback_when_route_has_no_configured_vendor_keys(
    mcp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = mcp_env / "route-fallback.txt"
    target.write_text("hello route fallback\n", encoding="utf-8")

    root = Path(str(mcp_env / ".atelier"))
    save_route_config(root, RouteConfig(enabled_vendors=["anthropic"]))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    response = _call("read", {"file_path": str(target), "max_lines": 5})
    payload = _payload(response)

    assert payload["path"] == str(target)
    assert "hello route fallback" in json.dumps(payload)


@pytest.mark.slow  # Spawns a real atelier-mcp subprocess for end-to-end stdio
def test_stdio_server_round_trip_edits_and_searches_real_files(mcp_env: Path) -> None:
    target = mcp_env / "stdio.txt"
    target.write_text("hello world\n", encoding="utf-8")

    requests = (
        "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "edit",
                            "arguments": {
                                "edits": [
                                    {
                                        "path": str(target),
                                        "op": "replace",
                                        "old_string": "world",
                                        "new_string": "stdio",
                                    }
                                ]
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "search",
                            "arguments": {
                                "query": "stdio",
                                "file_path": str(mcp_env),
                                "mode": "chunks",
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    env = {
        **dict(subprocess.os.environ),
        "ATELIER_ROOT": str(mcp_env / ".atelier"),
        "CLAUDE_WORKSPACE_ROOT": str(mcp_env),
        "CLAUDE_CONFIG_DIR": str(mcp_env / ".claude"),
        "ATELIER_EMBEDDER": "null",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "atelier.gateway.adapters.mcp_server",
            "--root",
            str(mcp_env / ".atelier"),
        ],
        input=requests,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert responses[0]["result"]["serverInfo"]["name"] == "atelier-context"

    edit_payload = json.loads(responses[1]["result"]["content"][0]["text"])
    assert edit_payload["failed"] == []
    assert target.read_text(encoding="utf-8") == "hello stdio\n"

    search_payload = json.loads(responses[2]["result"]["content"][0]["text"])
    assert search_payload["matches"]


def test_memory_task_and_remote_memory_limits_e2e(mcp_env: Path) -> None:
    block = _payload(
        _call(
            "memory",
            {
                "op": "block_upsert",
                "agent_id": "atelier:code",
                "label": "mcp-e2e",
                "value": "Prefer JSON-RPC MCP tests with real side effects.",
                "pinned": True,
                "metadata": {"source": "pytest"},
            },
        )
    )
    assert block["version"] == 1

    fetched = _payload(
        _call(
            "memory",
            {
                "op": "block_get",
                "agent_id": "atelier:code",
                "label": "mcp-e2e",
            },
        )
    )
    assert fetched["value"].startswith("Prefer JSON-RPC MCP tests")
    assert fetched["pinned"] is True

    archived = _payload(
        _call(
            "memory",
            {
                "op": "archive",
                "agent_id": "atelier:code",
                "text": "Archived checkout retry guidance for MCP JSON-RPC task tests.",
                "source": "user",
                "source_ref": "pytest:e2e",
                "tags": ["agent:atelier:code", "mcp-e2e"],
            },
        )
    )
    assert archived["dedup_hit"] is False

    recalled = _payload(
        _call(
            "memory",
            {
                "op": "recall",
                "agent_id": "atelier:code",
                "query": "checkout retry guidance",
                "top_k": 3,
                "tags": ["mcp-e2e"],
            },
        )
    )
    assert recalled["passages"]
    assert recalled["passages"][0]["id"] == archived["id"]
    assert "checkout retry guidance" in recalled["passages"][0]["text"].lower()

    context = _payload(
        _call(
            "context",
            {
                "task": "Use checkout retry guidance in MCP JSON-RPC task tests.",
                "agent_id": "atelier:code",
            },
        )
    )
    assert "<memory>" in context["context"]
    assert context["recalled_passages"]
    assert archived["id"] in {item["id"] for item in context["recalled_passages"]}

    transcript_recall = _call(
        "memory",
        {
            "op": "transcript_recall",
            "query": "sqlite auto limit",
            "top_k": 2,
        },
    )
    assert transcript_recall["error"]["message"] == "memory op not supported in remote mode: transcript_recall"


def test_read_search_edit_and_compact_e2e(mcp_env: Path) -> None:
    target = mcp_env / "sample.py"
    target.write_text(
        "def alpha():\n    return 'needle'\n\ndef beta():\n    return 'secondary needle'\n",
        encoding="utf-8",
    )

    short_read = _payload(_call("read", {"file_path": str(target), "max_lines": 3}))
    assert short_read["language"] == "python"

    expanded_read = _payload(_call("read", {"file_path": str(target), "expand": True}))
    assert "def alpha" in str(expanded_read["content"])

    ranged_read = _payload(_call("read", {"file_path": str(target), "range": "2-2"}))
    assert "needle" in str(ranged_read["content"])

    ranked_search = _payload(_call("search", {"query": "needle", "file_path": str(mcp_env), "mode": "chunks"}))
    assert ranked_search["matches"]

    repo_map = _payload(
        _call(
            "search",
            {"query": "", "seed_files": [str(target)], "mode": "map", "budget_tokens": 200},
        )
    )
    assert repo_map["ranked_files"]

    native_search = _payload(
        _call(
            "grep",
            {
                "file_path": str(mcp_env),
                "content_regex": "secondary needle",
                "file_glob_patterns": ["*.py"],
                "output_mode": "file_paths_with_match_count",
                "lines_before": 1,
                "lines_after": 1,
            },
        )
    )
    assert native_search["_meta"]["fileMatchCount"] == 1

    rich_edit = _payload(
        _call(
            "edit",
            {
                "edits": [
                    {
                        "file_path": "sample.py#2",
                        "old_string": "return 'needle'",
                        "new_string": "return 'atelier'",
                    }
                ]
            },
        )
    )
    assert rich_edit["failed"] == []
    assert "atelier" in target.read_text(encoding="utf-8")

    legacy_edit = _payload(
        _call(
            "edit",
            {
                "edits": [
                    {
                        "path": str(target),
                        "op": "replace",
                        "old_string": "secondary needle",
                        "new_string": "secondary atelier",
                    }
                ]
            },
        )
    )
    assert legacy_edit["failed"] == []
    assert "secondary atelier" in target.read_text(encoding="utf-8")

    partial = mcp_env / "partial.txt"
    partial.write_text("YES\n", encoding="utf-8")
    non_atomic = _payload(
        _call(
            "edit",
            {
                "atomic": False,
                "edits": [
                    {
                        "path": str(partial),
                        "op": "replace",
                        "old_string": "YES",
                        "new_string": "OK",
                    },
                    {
                        "path": str(partial),
                        "op": "replace",
                        "old_string": "MISSING",
                        "new_string": "NO",
                    },
                ],
            },
        )
    )
    assert non_atomic["failed"]
    assert partial.read_text(encoding="utf-8") == "OK\n"


def test_edit_atomic_rollback_e2e(mcp_env: Path) -> None:
    good = mcp_env / "atomic.txt"
    good.write_text("original\n", encoding="utf-8")

    payload = _payload(
        _call(
            "edit",
            {
                "atomic": True,
                "edits": [
                    {
                        "path": str(good),
                        "op": "replace",
                        "old_string": "original",
                        "new_string": "changed",
                    },
                    {
                        "path": str(good),
                        "op": "replace",
                        "old_string": "missing",
                        "new_string": "boom",
                    },
                ],
            },
        )
    )

    assert payload["rolled_back"] is True
    assert good.read_text(encoding="utf-8") == "original\n"


def test_symbol_edit_descriptor_e2e(mcp_env: Path) -> None:
    target = mcp_env / "service.py"
    target.write_text(
        "class AuthService:\n" "    def verify(self, token: str) -> bool:\n" "        return token == 'ok'\n",
        encoding="utf-8",
    )

    payload = _payload(
        _call(
            "edit",
            {
                "edits": [
                    {
                        "kind": "symbol",
                        "name": "AuthService.verify",
                        "mode": "replace",
                        "new_body": ("def verify(self, token: str) -> bool:\n" "    return token.startswith('ok')"),
                    }
                ]
            },
        )
    )

    assert payload["failed"] == []
    assert payload["applied"][0]["kind"] == "symbol"
    # Post-edit hooks may run ruff format which normalises quotes; accept either.
    final = target.read_text(encoding="utf-8")
    assert "startswith('ok')" in final or 'startswith("ok")' in final


def test_sql_actions_e2e(mcp_env: Path) -> None:
    db_path = mcp_env / "data.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items(id integer primary key, name text)")
    conn.executemany("INSERT INTO items(name) VALUES(?)", [("Ada",), ("Grace",), ("Linus",)])
    conn.commit()
    conn.close()

    connect = _payload(_call("sql", {"action": "connect", "connection_string": f"sqlite:///{db_path}"}))
    assert connect["overview"]["table_count"] == 1

    schema = _payload(_call("sql", {"action": "schema", "connection_string": f"sqlite:///{db_path}"}))
    assert schema["tables"] == ["items"]

    table = _payload(
        _call(
            "sql",
            {"action": "table", "connection_string": f"sqlite:///{db_path}", "name": "items"},
        )
    )
    assert table["columns"][0]["name"] == "id"

    lint = _payload(
        _call(
            "sql",
            {
                "action": "lint",
                "connection_string": f"sqlite:///{db_path}",
                "sql": "SELECT * FROM items",
            },
        )
    )
    assert lint["ok"] is True

    query = _payload(
        _call(
            "sql",
            {
                "action": "query",
                "connection_string": f"sqlite:///{db_path}",
                "queries": [{"name": "items", "sql": "SELECT * FROM items ORDER BY id"}],
                "max_rows": 2,
            },
        )
    )
    assert query["isError"] is False
    assert query["results"][0]["row_count"] == 2
    assert query["results"][0]["rows"][0]["name"] == "Ada"


def test_context_route_rescue_verify_compact_and_trace_e2e(mcp_env: Path) -> None:
    context = _payload(
        _call(
            "context",
            {
                "task": "Add MCP JSON-RPC end-to-end tests",
                "domain": "coding",
                "files": ["tests/gateway/test_mcp_jsonrpc_e2e.py"],
            },
        )
    )
    assert isinstance(context.get("context"), str)

    rescue = _payload(
        _call(
            "rescue",
            {
                "task": "Run pytest",
                "error": "AssertionError: expected MCP payload",
                "recent_actions": ["run pytest", "run pytest"],
            },
        )
    )
    assert "rescue" in rescue
    assert "analysis" in rescue

    decision = _payload(
        _call(
            "route",
            {
                "op": "decide",
                "user_goal": "Harden MCP gateway end-to-end tests",
                "repo_root": str(mcp_env),
                "task_type": "test",
                "risk_level": "low",
                "changed_files": ["tests/gateway/test_mcp_jsonrpc_e2e.py"],
                "step_type": "verify",
                "step_index": 1,
                "evidence_summary": {"confidence": 0.9, "estimated_input_tokens": 200},
            },
        )
    )
    assert decision["id"].startswith("rd-")
    assert decision["tier"] in {"cheap", "mid", "premium", "deterministic"}

    verified_route = _payload(
        _call(
            "route",
            {
                "op": "verify",
                "route_decision_id": decision["id"],
                "changed_files": ["tests/gateway/test_mcp_jsonrpc_e2e.py"],
                "validation_results": [{"name": "pytest", "passed": True, "detail": "ok"}],
                "rubric_status": "pass",
                "required_verifiers": ["tests", "rubric"],
                "human_accepted": True,
            },
        )
    )
    assert verified_route["route_decision_id"] == decision["id"]
    assert verified_route["outcome"] in {"pass", "warn", "fail", "escalate"}

    rubric = _payload(
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
    assert rubric["status"] == "pass"

    compact_output = _payload(
        _call(
            "compact",
            {
                "op": "output",
                "content": "short MCP output",
                "content_type": "bash",
                "budget_tokens": 100,
            },
        )
    )
    assert compact_output["method"] == "passthrough"

    compact_advise = _payload(_call("compact", {"op": "advise"}))
    assert "should_compact" in compact_advise
    assert "suggested_prompt" in compact_advise

    trace = _payload(
        _call(
            "trace",
            {
                "agent": "codex",
                "domain": "coding",
                "task": "Exercise MCP JSON-RPC end-to-end coverage",
                "status": "success",
                "files_touched": ["tests/gateway/test_mcp_jsonrpc_e2e.py"],
                "commands_run": ["uv run python -m pytest tests/gateway/test_mcp_jsonrpc_e2e.py"],
                "validation_results": [{"name": "pytest", "passed": True, "detail": "ok"}],
                "prompt": "Add real MCP e2e coverage",
                "response": "Implemented end-to-end gateway tests",
                "bash_outputs": [{"command": "pytest", "stdout": "ok", "stderr": "", "ok": True}],
                "tool_outputs": [{"tool": "memory", "result": "stored"}],
            },
        )
    )
    assert trace["id"]
    assert trace["session_id"] == "remote-session"
