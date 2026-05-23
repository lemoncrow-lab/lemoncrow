"""Benchmark cases for the `context` MCP tool.

Covers 2 scenarios: basic retrieval and retrieval with file hints.

Baseline estimates:
  - basic: agent reads multiple source files + prior turn history manually (~3000 tokens)
  - with_files: agent opens + reads several files to build context (~5000 tokens)
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase


def _assert_context(result: dict[str, Any]) -> None:
    assert "context" in result, "response must have 'context'"
    assert "bootstrap" in result, "response must have 'bootstrap'"
    assert isinstance(result["context"], str), "'context' must be a string"
    assert len(result["context"]) > 0, "'context' must be non-empty"
    bootstrap = result["bootstrap"]
    assert isinstance(bootstrap, dict), "'bootstrap' must be a dict"
    assert "status" in bootstrap, "'bootstrap' must have 'status'"
    assert bootstrap["status"] in ("warm", "warming", "cold", "indexing", "error"), (
        f"unexpected bootstrap status: {bootstrap['status']}"
    )


def _assert_context_with_files(result: dict[str, Any]) -> None:
    _assert_context(result)
    # recalled_passages may be present or absent depending on agent_id
    if "recalled_passages" in result:
        assert isinstance(result["recalled_passages"], list), "recalled_passages must be list"


CONTEXT_CASES: list[BenchCase] = [
    BenchCase(
        op="get_context",
        label="context/basic",
        args={
            "task": "fix authentication token expiry bug",
            "domain": "security",
            "recall": False,
        },
        assert_keys=["context", "bootstrap"],
        custom_assert=_assert_context,
        # baseline = agent reads several source files manually to gather context
        baseline_tokens=3000,
    ),
    BenchCase(
        op="get_context",
        label="context/with_files",
        args={
            "task": "refactor smart search backend selection",
            "domain": "python",
            "files": ["src/atelier/core/capabilities/tool_supervision/smart_search.py"],
            "tools": ["grep", "search"],
            "recall": False,
            "max_blocks": 3,
        },
        assert_keys=["context", "bootstrap"],
        custom_assert=_assert_context_with_files,
        # baseline = agent manually reads files + relevant blocks (~5000 tokens)
        baseline_tokens=5000,
    ),
]
