from __future__ import annotations

from typing import Any

from lemoncrow.gateway.adapters import mcp_server


def test_code_search_target_view_is_pointer_first() -> None:
    payload = {
        "items": [
            {
                "symbol_id": "sym-1",
                "symbol_name": "classify_command",
                "qualified_name": "classify_command",
                "kind": "function",
                "file_path": "src/pkg/bash_exec.py",
                "start_line": 133,
                "end_line": 172,
                "signature": "def classify_command(command: str) -> CommandPolicyDecision:",
                "content_hash": "hidden",
            }
        ],
        "mode": "lexical",
        "provenance": "cached",
    }

    result = mcp_server._code_search_target_view(payload)

    # No advisory metadata: no view/has_more_context/suggested_next/role.
    assert "view" not in result
    assert "has_more_context" not in result
    assert "suggested_next" not in result
    assert result["items"] == [
        {
            "kind": "function",
            "name": "classify_command",
            "qualified_name": "classify_command",
            "path": "src/pkg/bash_exec.py",
            "line": 133,
            "end_line": 172,
            "signature": "def classify_command(command: str) -> CommandPolicyDecision:",
        }
    ]


def test_code_search_graph_view_keeps_relationships_out_of_items() -> None:
    class FakeEngine:
        def tool_usages(self, **_: Any) -> dict[str, Any]:
            return {
                "references": [
                    {"path": "tests/test_run_tool.py", "line": 9, "edge_kind": "import"},
                    {"path": "src/pkg/runner.py", "line": 20, "edge_kind": "call"},
                ]
            }

        def tool_callers(self, **_: Any) -> dict[str, Any]:
            return {"related": [{"name": "run_tool", "path": "src/pkg/runner.py", "line": 18}]}

        def tool_callees(self, **_: Any) -> dict[str, Any]:
            return {"related": [{"name": "parse_command", "path": "src/pkg/parser.py", "line": 4}]}

    search_payload = {
        "items": [
            {
                "symbol_id": "sym-1",
                "symbol_name": "classify_command",
                "qualified_name": "classify_command",
                "kind": "function",
                "file_path": "src/pkg/bash_exec.py",
                "start_line": 133,
            }
        ],
        "mode": "lexical",
        "provenance": "cached",
    }

    result = mcp_server._code_search_graph_view(
        FakeEngine(),
        query="classify_command",
        search_payload=search_payload,
        view="graph",
        limit=20,
        depth=1,
        budget_tokens=900,
    )

    assert "items" not in result
    assert "view" not in result
    assert "mode" not in result
    assert "provenance" not in result
    assert result["target"]["name"] == "classify_command"
    assert result["related"]["imports"] == [{"path": "tests/test_run_tool.py", "line": 9, "edge_kind": "import"}]
    assert result["related"]["usages"] == [{"path": "src/pkg/runner.py", "line": 20, "edge_kind": "call"}]
    assert result["related"]["callers"][0]["name"] == "run_tool"
    assert result["related"]["callees"][0]["name"] == "parse_command"
