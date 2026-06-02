"""Benchmark cases for the public `memory` MCP tool."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from benchmarks.mcp_tools.harness import BenchCase
from benchmarks.mcp_tools.repo_facts import collect_symbol_facts, unique_symbol_facts


def _repo_root() -> Path:
    value = os.environ.get("CLAUDE_WORKSPACE_ROOT")
    if value:
        return Path(value)
    return Path(__file__).resolve().parents[3]


def _assert_store_fact(result: dict[str, object], fact: str) -> None:
    assert "id" in result, "store_fact response must contain 'id'"
    assert result.get("fact") == fact, f"store_fact must round-trip fact={fact!r}"
    assert result.get("scope") == "repository", "generated store_fact cases use repository scope"


def _assert_vote_fact(result: dict[str, object], fact: str) -> None:
    assert "id" in result, "vote_fact response must contain 'id'"
    assert result.get("fact") == fact, f"vote_fact must target fact={fact!r}"
    assert result.get("direction") == "upvote", "generated vote cases must upvote"


def _assert_recall(result: dict[str, object], symbol_name: str, expected_path: str) -> None:
    passages = result.get("passages")
    assert isinstance(passages, list), "'passages' must be a list"
    assert passages, "recall must return at least one passage"
    text = " ".join(str(passage) for passage in passages)
    assert symbol_name in text, f"recall result must mention {symbol_name!r}"
    assert expected_path in text, f"recall result must mention {expected_path!r}"


def _store_assert(fact: str) -> Callable[[dict[str, object]], None]:
    def _assert(result: dict[str, object]) -> None:
        _assert_store_fact(result, fact)

    return _assert


def _vote_assert(fact: str) -> Callable[[dict[str, object]], None]:
    def _assert(result: dict[str, object]) -> None:
        _assert_vote_fact(result, fact)

    return _assert


def _recall_assert(symbol_name: str, expected_path: str) -> Callable[[dict[str, object]], None]:
    def _assert(result: dict[str, object]) -> None:
        _assert_recall(result, symbol_name, expected_path)

    return _assert


def _memory_fact(symbol_name: str, path: str) -> str:
    return f"Symbol {symbol_name} is defined in {path}"


def _build_memory_cases() -> list[BenchCase]:
    repo_root = _repo_root()
    unique_symbols = unique_symbol_facts(collect_symbol_facts(repo_root)[0])[:100]
    assert len(unique_symbols) == 100, "not enough unique symbols for generated memory cases"

    cases: list[BenchCase] = []
    for index, symbol in enumerate(unique_symbols, start=1):
        agent_id = f"bench:memory:{index:03d}"
        fact = _memory_fact(symbol.name, symbol.path)
        citations = f"{symbol.path}:{symbol.line}"
        reason = f"benchmark fact for {symbol.name}"
        archive_text = f"{fact}. Qualified name: {symbol.qualified_name}. Kind: {symbol.kind}."
        cases.append(
            BenchCase(
                op="store_fact",
                label=f"memory/store_fact/{index:03d}",
                args={
                    "op": "store_fact",
                    "agent_id": agent_id,
                    "subject": "code-intel",
                    "fact": fact,
                    "scope": "repository",
                    "citations": citations,
                    "reason": reason,
                },
                assert_keys=["id", "subject", "fact", "scope", "citations", "reason"],
                custom_assert=_store_assert(fact),
                baseline_tokens=700,
            )
        )
        cases.append(
            BenchCase(
                op="vote_fact",
                label=f"memory/vote_fact/{index:03d}",
                args={
                    "op": "vote_fact",
                    "agent_id": agent_id,
                    "fact": fact,
                    "direction": "upvote",
                    "reason": f"verified benchmark fact for {symbol.name}",
                    "scope": "repository",
                },
                assert_keys=["id", "fact", "direction", "reason", "scope"],
                custom_assert=_vote_assert(fact),
                baseline_tokens=500,
            )
        )
        cases.append(
            BenchCase(
                op="recall",
                label=f"memory/recall/{index:03d}",
                args={
                    "op": "recall",
                    "agent_id": agent_id,
                    "query": symbol.name,
                    "top_k": 3,
                    "_archive_text": archive_text,
                    "_archive_source_ref": f"{symbol.path}:{symbol.line}",
                    "_archive_tags": [symbol.kind, "benchmark", "code-intel"],
                },
                assert_keys=["passages"],
                custom_assert=_recall_assert(symbol.name, symbol.path),
                baseline_tokens=1000,
            )
        )
    return cases


MEMORY_CASES = _build_memory_cases()
