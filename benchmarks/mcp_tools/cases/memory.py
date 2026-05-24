"""Benchmark cases for the `memory` MCP tool."""

from __future__ import annotations

from benchmarks.mcp_tools.harness import BenchCase


def _assert_store_fact(result: dict[str, object]) -> None:
    assert "id" in result, "store_fact response must contain 'id'"
    assert "fact" in result, "store_fact response must contain 'fact'"
    assert "scope" in result, "store_fact response must contain 'scope'"
    assert result["fact"] == "user prefers concise commit messages"


def _assert_vote_fact(result: dict[str, object]) -> None:
    assert "id" in result, "vote_fact response must contain 'id'"
    assert "fact" in result, "vote_fact response must contain 'fact'"
    assert "direction" in result, "vote_fact response must contain 'direction'"
    assert result["direction"] == "upvote"


def _assert_recall(result: dict[str, object]) -> None:
    assert "passages" in result, "recall response must contain 'passages'"
    passages = result["passages"]
    assert isinstance(passages, list), "'passages' must be a list"


MEMORY_CASES: list[BenchCase] = [
    BenchCase(
        op="store_fact",
        label="memory/store_fact",
        args={
            "op": "store_fact",
            "agent_id": "bench:agent",
            "subject": "preferences",
            "fact": "user prefers concise commit messages",
            "scope": "user",
            "citations": 'User input: "user prefers concise commit messages"',
            "reason": "benchmark seed fact",
        },
        assert_keys=["id", "subject", "fact", "scope", "citations", "reason"],
        custom_assert=_assert_store_fact,
        baseline_tokens=600,
    ),
    BenchCase(
        op="vote_fact",
        label="memory/vote_fact",
        args={
            "op": "vote_fact",
            "agent_id": "bench:agent",
            "fact": "user prefers concise commit messages",
            "direction": "upvote",
            "reason": "verified by benchmark case",
            "scope": "user",
        },
        assert_keys=["id", "fact", "direction", "reason", "scope"],
        custom_assert=_assert_vote_fact,
        baseline_tokens=500,
    ),
    BenchCase(
        op="recall",
        label="memory/recall",
        args={
            "op": "recall",
            "agent_id": "bench:agent",
            "query": "concise commit messages",
            "top_k": 3,
        },
        assert_keys=["passages"],
        custom_assert=_assert_recall,
        baseline_tokens=1000,
    ),
]
