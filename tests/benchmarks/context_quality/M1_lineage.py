"""M1 — Context Lineage benchmark (CQEVAL-02).

Tests whether commit chunk search correctly surfaces historical bug-fix commits
when queried with natural-language descriptions of the problems they solved.

Requirements:
- LemonCrow bootstrap must be complete for the target repo (commit_chunks table populated).
- Run with LEMONCROW_LLM_BACKEND=openai for Haiku 3.5 summarisation.
- Expected target: >=7/10 queries graded CORRECT.

Usage:
    uv run pytest tests/benchmarks/context_quality/M1_lineage.py -v -m slow
    # or with explicit repo:
    LEMONCROW_REPO_ROOT=/path/to/repo uv run pytest M1_lineage.py -v -m slow
"""

from __future__ import annotations

import hashlib
import os
import pathlib
from contextlib import closing
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class LineageQuery:
    """A single evaluation query with expected citation metadata."""

    query: str
    expected_sha: str
    keywords: list[str]
    description: str


# Ground truth: 10 real fix commits from the LemonCrow repo.
# Add more by running: git log --oneline --no-merges | grep "^fix"
# then pick commits with clear, searchable summaries.
QUERIES: list[LineageQuery] = [
    LineageQuery(
        query="parse_stream_jsonl key renaming cost_usd latency_ms terminalbench adapter",
        expected_sha="f9d1908",
        keywords=["parse_stream_jsonl", "cost_usd", "latency_ms", "rename", "keys"],
        description="fix(tb-02): rename parse_stream_jsonl keys to cost_usd/latency_ms/latency_api_ms",
    ),
    LineageQuery(
        query="ModelRouter score returns None null guard bench-off mode",
        expected_sha="21e8628",
        keywords=["ModelRouter", "score", "None", "bench", "null"],
        description="fix(bench): null guards for ModelRouter.score() when bench-off",
    ),
    LineageQuery(
        query="CrossVendorRouteAdvisor recommend null guard bench-off MODE-01",
        expected_sha="fa13519",
        keywords=["CrossVendorRouteAdvisor", "recommend", "bench", "null", "MODE-01"],
        description="fix(bench): null guard in CrossVendorRouteAdvisor.recommend() for bench-off",
    ),
    LineageQuery(
        query="inflated token savings sidecar field names unify correct calculation",
        expected_sha="fce2110",
        keywords=["token", "savings", "inflated", "sidecar", "field"],
        description="fix(savings): correct inflated token savings, unify sidecar field names",
    ),
    LineageQuery(
        query="shell chars_omitted divided by 4 token savings calculation fix",
        expected_sha="370bc6f",
        keywords=["chars_omitted", "token", "savings", "shell", "4"],
        description="fix(shell): use actual chars_omitted // 4 for token savings",
    ),
    LineageQuery(
        query="sidecar session_id bridge routing savings host flag",
        expected_sha="da43675",
        keywords=["sidecar", "session_id", "bridge", "savings", "host"],
        description="fix(savings): route sidecar via session_id bridge, not --host flag",
    ),
    LineageQuery(
        query="one-shot workspace bridge Claude session id fix",
        expected_sha="78cc861",
        keywords=["workspace", "bridge", "session", "id", "one-shot"],
        description="fix: use one-shot workspace bridge for Claude session id",
    ),
    LineageQuery(
        query="MCP session registration server startup SessionStart hook",
        expected_sha="235ac8c",
        keywords=["MCP", "session", "startup", "SessionStart", "register"],
        description="fix: register MCP session at server startup so SessionStart hook finds us",
    ),
    LineageQuery(
        query="savings pipeline sidecar-first savings_input_rate restored fix",
        expected_sha="8f69444",
        keywords=["savings", "pipeline", "sidecar", "savings_input_rate", "restored"],
        description="fix(savings): fix savings pipeline — sidecar-first, savings_input_rate restored",
    ),
    LineageQuery(
        query="session stats inflation prevent emit saved blocks prefer transcript token counting",
        expected_sha="f7fa2b8",
        keywords=["session", "stats", "inflation", "transcript", "token"],
        description="fix(token-counting): correct session stats — prevent inflation, emit saved blocks",
    ),
]


def _grade_result(results: list[Any], expected_sha: str, keywords: list[str]) -> bool:
    """Return True if any top-3 result matches by SHA prefix or keyword overlap."""
    for result in results[:3]:
        commit_sha = getattr(result, "commit_sha", None) or ""
        if commit_sha and (commit_sha.startswith(expected_sha) or expected_sha.startswith(commit_sha[:7])):
            return True
        summary = getattr(result, "signature", "") or getattr(result, "qualified_name", "")
        summary_lower = summary.lower()
        matched_keywords = [kw for kw in keywords if kw.lower() in summary_lower]
        if len(matched_keywords) >= 2:
            return True
    return False


def _get_engine(repo_root: pathlib.Path) -> Any:
    """Instantiate a CodeContextEngine for the target repo."""
    from lemoncrow.core.capabilities.code_context.engine import CodeContextEngine

    repo_id = hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()[:16]
    lemoncrow_root = pathlib.Path(os.environ.get("LEMONCROW_ROOT") or pathlib.Path.home() / ".lemoncrow")
    db_path = lemoncrow_root / "repos" / repo_id / "code.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return CodeContextEngine(repo_root=repo_root, db_path=db_path, autosync_enabled=False)
    finally:
        pass


def run_benchmark(repo_root: pathlib.Path | None = None) -> dict[str, Any]:
    """Run all M1 queries and return a result dict.

    Returns:
        {
            "pass_count": int,
            "total": int,
            "pass_rate": float,
            "verdicts": list[{"query": str, "correct": bool, "expected_sha": str}],
        }
    """
    if repo_root is None:
        env_root = os.environ.get("LEMONCROW_REPO_ROOT")
        repo_root = pathlib.Path(env_root) if env_root else pathlib.Path.cwd()

    engine = _get_engine(repo_root)

    with closing(engine.connection()) as conn:
        count_row = conn.execute("SELECT COUNT(*) AS n FROM commit_chunks").fetchone()
        chunk_count = int(count_row["n"]) if count_row else 0

    if chunk_count == 0:
        return {
            "pass_count": 0,
            "total": len(QUERIES),
            "pass_rate": 0.0,
            "error": "commit_chunks table is empty — run bootstrap first",
            "verdicts": [],
        }

    verdicts = []
    pass_count = 0
    for q in QUERIES:
        try:
            results = engine.search_symbols(
                q.query,
                mode="hybrid",
                limit=10,
                provenance_filter="commit",
            )
            correct = _grade_result(results, q.expected_sha, q.keywords)
        except Exception:  # noqa: BLE001 — safety net: count as incorrect rather than crash benchmark
            correct = False
            results = []
        pass_count += int(correct)
        verdicts.append(
            {
                "query": q.query[:60],
                "expected_sha": q.expected_sha,
                "correct": correct,
                "top_result_sha": getattr(results[0], "commit_sha", None) if results else None,
            }
        )

    return {
        "pass_count": pass_count,
        "total": len(QUERIES),
        "pass_rate": pass_count / len(QUERIES),
        "verdicts": verdicts,
    }


# ---------------------------------------------------------------------------
# pytest integration
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_m1_lineage_pass_rate() -> None:
    """CQEVAL-02: >=7/10 commit history queries answered with correct citation."""
    repo_root_env = os.environ.get("LEMONCROW_REPO_ROOT")
    repo_root = pathlib.Path(repo_root_env) if repo_root_env else pathlib.Path.cwd()

    import subprocess

    try:
        remotes = subprocess.check_output(["git", "remote", "-v"], cwd=repo_root, text=True, timeout=5)
        if "lemoncrow" not in remotes.lower() and "leanchain" not in remotes.lower():
            pytest.skip("Not running in the lemoncrow repo — skip M1 benchmark")
    except (subprocess.CalledProcessError, OSError):
        pytest.skip("git remote check failed — skip M1 benchmark")

    results = run_benchmark(repo_root)

    if "error" in results:
        pytest.skip(f"Skipping M1 benchmark: {results['error']}")

    for v in results["verdicts"]:
        status = "PASS" if v["correct"] else "FAIL"
        print(f"  [{status}] {v['query'][:50]}... -> {v.get('top_result_sha', 'none')}")

    pass_rate = results["pass_rate"]
    pass_count = results["pass_count"]
    total = results["total"]
    print(f"\nM1 result: {pass_count}/{total} = {pass_rate:.0%}")

    assert pass_count >= 7, (
        f"M1 benchmark FAIL: {pass_count}/10 correct (target >=7/10). "
        f"Pass rate: {pass_rate:.0%}. "
        f"Ensure bootstrap has completed and LEMONCROW_LLM_BACKEND=openai is set."
    )


@pytest.mark.slow
def test_m1_commit_chunks_populated() -> None:
    """Prerequisite: commit_chunks table must have >=100 rows for meaningful eval."""
    repo_root_env = os.environ.get("LEMONCROW_REPO_ROOT")
    repo_root = pathlib.Path(repo_root_env) if repo_root_env else pathlib.Path.cwd()
    engine = _get_engine(repo_root)
    with closing(engine.connection()) as conn:
        count_row = conn.execute("SELECT COUNT(*) AS n FROM commit_chunks").fetchone()
        chunk_count = int(count_row["n"]) if count_row else 0
    if chunk_count == 0:
        pytest.skip("commit_chunks empty — run bootstrap: code op=search on this repo first")
    assert (
        chunk_count >= 100
    ), f"Only {chunk_count} commit chunks found. Bootstrap may be incomplete (target: ~425 for the lemoncrow repo)."
    print(f"commit_chunks populated: {chunk_count} rows")


if __name__ == "__main__":
    import json

    repo_root_env = os.environ.get("LEMONCROW_REPO_ROOT")
    repo_root = pathlib.Path(repo_root_env) if repo_root_env else pathlib.Path.cwd()
    result = run_benchmark(repo_root)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["pass_count"] >= 7 else 1)
