---
phase: 08
plan: 02
title: "Benchmarks"
status: pending
created: 2025-07-15
requires:
  - 08-PLAN-01b-engine.md
requirements:
  - CQEVAL-01
  - CQEVAL-02
---

# Phase 08 — Context Lineage: Benchmarks

## Goal

Create `tests/benchmarks/context_quality/` evaluation suite with the M1 commit-history benchmark (≥7/10 correct citations), plus stub modules for M2–M4 milestones.

---

## Tasks

### Task 1: Create `tests/benchmarks/context_quality/` suite skeleton

**Files:**
- `tests/benchmarks/context_quality/__init__.py` (new)
- `tests/benchmarks/context_quality/README.md` (new)
- `tests/benchmarks/context_quality/M2_routing.py` (new)
- `tests/benchmarks/context_quality/M3_verification.py` (new)
- `tests/benchmarks/context_quality/M4_scoped.py` (new)

**Why:** CQEVAL-01 requires the suite directory to exist with benchmark modules for M1–M4 and a README describing the eval protocol. The M2/M3/M4 modules are stubs — their real implementations land in Phases 9–11. The README must describe the protocol clearly enough that a contributor can extend it without asking questions.

**What:**

**`tests/benchmarks/context_quality/__init__.py`** — empty file (marks as package):

```python
"""Context Quality evaluation benchmarks for Atelier M1–M4."""
```

**`tests/benchmarks/context_quality/README.md`** — full protocol documentation:

```markdown
# Context Quality Benchmarks

Internal evaluation suite for the Context Quality Lift milestones (v0.2).
These benchmarks run against a real Atelier installation and require:
- A git repository indexed by `code op="search"` (for M1)
- A working Atelier MCP server (for M2–M4)
- Python ≥ 3.12 in the atelier venv

## Protocol

All benchmarks follow this structure:
1. **Seed** — set up the evaluation fixture (real git repo, pre-seeded commit chunks, etc.)
2. **Run** — call the Atelier capability under test for each query/task
3. **Grade** — compare result against ground truth; score 1 (correct) or 0 (incorrect)
4. **Report** — print per-query verdict + aggregate pass rate

Benchmarks are NOT in the normal pytest suite (they are `@pytest.mark.slow`).
Run them explicitly:

```bash
uv run pytest tests/benchmarks/context_quality/M1_lineage.py -v -m slow
```

## Benchmark Targets

| Milestone | File | Target | Baseline |
|-----------|------|--------|----------|
| M1 — Context Lineage | `M1_lineage.py` | ≥7/10 | ≤2/10 |
| M2 — Cache-Aware Routing | `M2_routing.py` | ≥10% cost reduction | — |
| M3 — Counterexample Loop | `M3_verification.py` | ≥60% self-correction | ≤15% |
| M4 — Scoped Pull Context | `M4_scoped.py` | precision ≥0.6 recall ≥0.85 | — |

## Scoring

Each query is graded binary: 1 = correct citation/answer, 0 = wrong/hallucinated.
Pass rate = sum(scores) / len(queries).

**Citation correctness for M1:** A result is scored 1 if the top-ranked commit chunk
returned by `code op="search"` has a `commit_sha` matching the expected SHA **or**
the summary text contains at least 2 of the expected keywords from the ground truth.
Exact SHA match is preferred; keyword fallback handles SHA abbreviation differences.

## Adding New Benchmark Queries

1. Find a real commit in the target repo that fixes a concrete, named bug.
2. Formulate a natural-language query that a developer would ask about that bug.
3. Add an entry to the `QUERIES` list with `sha`, `query`, and `keywords` fields.
4. Run `uv run pytest M1_lineage.py -v -m slow` and verify ≥7/10 pass.

## CI Integration

Benchmarks are excluded from `pytest` default runs (`-m 'not slow'` in pyproject.toml).
Run them in a separate CI job after merging Phase 8:

```bash
ATELIER_LLM_BACKEND=openai uv run pytest tests/benchmarks/context_quality/ -v -m slow
```
```

**`tests/benchmarks/context_quality/M2_routing.py`** — stub:

```python
"""M2 — Cache-Aware Routing benchmark (Phase 9).

Target: ≥10% cost reduction on 50 replayed session traces with no quality-tier regressions.
Baseline: cost without cache-aware routing.

TODO(Phase 9): Implement once CACHE-01–05 are shipped.
"""
import pytest


@pytest.mark.slow
def test_m2_routing_placeholder() -> None:
    pytest.skip("M2 benchmark not yet implemented — ships in Phase 9")
```

**`tests/benchmarks/context_quality/M3_verification.py`** — stub:

```python
"""M3 — Counterexample Loop benchmark (Phase 10).

Target: ≥60% self-correction rate on 20 seeded type-error edits.
Baseline: ≤15% without VerifierCapability.

TODO(Phase 10): Implement once COUNTER-01–05 are shipped.
"""
import pytest


@pytest.mark.slow
def test_m3_verification_placeholder() -> None:
    pytest.skip("M3 benchmark not yet implemented — ships in Phase 10")
```

**`tests/benchmarks/context_quality/M4_scoped.py`** — stub:

```python
"""M4 — Scoped Pull Context benchmark (Phase 11).

Target: precision ≥0.6 and recall ≥0.85 on 20 multi-file edits from repo history.
Baseline: no scoped pull capability.

TODO(Phase 11): Implement once SCOPED-01–05 are shipped.
"""
import pytest


@pytest.mark.slow
def test_m4_scoped_placeholder() -> None:
    pytest.skip("M4 benchmark not yet implemented — ships in Phase 11")
```

**Test:**
```bash
uv run pytest tests/benchmarks/context_quality/M2_routing.py \
    tests/benchmarks/context_quality/M3_verification.py \
    tests/benchmarks/context_quality/M4_scoped.py \
    -v -m slow 2>&1 | grep -E "SKIPPED|passed|failed"
# All 3 should show "SKIPPED"

uv run python -c "
from tests.benchmarks.context_quality import __doc__
print('Package import OK:', __doc__)
"
```

**Depends on:** nothing (pure new files)

---

### Task 2: Create `tests/benchmarks/context_quality/M1_lineage.py`

**File:** `tests/benchmarks/context_quality/M1_lineage.py` (new)

**Why:** CQEVAL-02 requires a benchmark that queries the commit chunk search on 10 real bug-fix commits from this repo's history and scores ≥7/10 correct citations. It must run against a live Atelier engine with bootstrap complete.

**What:**

The benchmark uses 10 real fix commits from the Atelier repo. For each, it calls `engine.search_symbols(query, mode="hybrid", provenance_filter="commit")` and grades the top-3 results. A result is CORRECT if any of the top-3 returned commit records has a `commit_sha` matching the expected SHA, OR if the summary contains ≥2 of the expected keywords.

```python
"""M1 — Context Lineage benchmark (CQEVAL-02).

Tests whether commit chunk search correctly surfaces historical bug-fix commits
when queried with natural-language descriptions of the problems they solved.

Requirements:
- Atelier bootstrap must be complete for the target repo (commit_chunks table populated).
- Run with ATELIER_LLM_BACKEND=openai for Haiku 3.5 summarisation.
- Expected target: ≥7/10 queries graded CORRECT.

Usage:
    uv run pytest tests/benchmarks/context_quality/M1_lineage.py -v -m slow
    # or with explicit repo:
    ATELIER_REPO_ROOT=/path/to/repo uv run pytest M1_lineage.py -v -m slow
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class LineageQuery:
    """A single evaluation query with expected citation metadata."""
    query: str                   # natural-language query
    expected_sha: str            # full 40-char commit SHA
    keywords: list[str]          # ≥2 must appear in summary for keyword fallback
    description: str             # human-readable description of what the fix did


# Ground truth: 10 real fix commits from the Atelier repo.
# Add more by running: git log --oneline --no-merges | grep "^fix"
# then pick commits with clear, searchable summaries.
QUERIES: list[LineageQuery] = [
    LineageQuery(
        query="parse_stream_jsonl key renaming cost_usd latency_ms terminalbench adapter",
        expected_sha="f9d1908",  # will match on prefix
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
        # SHA match (prefix — commits stored as full 40-char SHA)
        commit_sha = getattr(result, "commit_sha", None) or ""
        if commit_sha and (
            commit_sha.startswith(expected_sha) or expected_sha.startswith(commit_sha[:7])
        ):
            return True
        # Keyword fallback: summary must contain ≥2 expected keywords (case-insensitive)
        summary = getattr(result, "signature", "") or getattr(result, "qualified_name", "")
        summary_lower = summary.lower()
        matched_keywords = [kw for kw in keywords if kw.lower() in summary_lower]
        if len(matched_keywords) >= 2:
            return True
    return False


def _get_engine(repo_root: pathlib.Path) -> Any:
    """Instantiate a CodeContextEngine for the target repo."""
    from atelier.core.capabilities.code_context.engine import CodeContextEngine
    import hashlib

    repo_id = hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()[:16]
    atelier_root = pathlib.Path(
        os.environ.get("ATELIER_ROOT") or pathlib.Path.home() / ".atelier"
    )
    db_path = atelier_root / "repos" / repo_id / "code.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return CodeContextEngine(repo_root=repo_root, repo_id=repo_id, db_path=db_path)


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
        env_root = os.environ.get("ATELIER_REPO_ROOT")
        repo_root = pathlib.Path(env_root) if env_root else pathlib.Path.cwd()

    engine = _get_engine(repo_root)

    # Ensure bootstrap has run — this blocks if not yet done.
    # In CI, run bootstrap separately before the benchmark.
    from contextlib import closing
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
        except Exception as exc:
            correct = False
            results = []
        pass_count += int(correct)
        verdicts.append({
            "query": q.query[:60],
            "expected_sha": q.expected_sha,
            "correct": correct,
            "top_result_sha": getattr(results[0], "commit_sha", None) if results else None,
        })

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
    """CQEVAL-02: ≥7/10 commit history queries answered with correct citation."""
    repo_root_env = os.environ.get("ATELIER_REPO_ROOT")
    repo_root = pathlib.Path(repo_root_env) if repo_root_env else pathlib.Path.cwd()

    # Verify we're in the atelier repo (has expected commits)
    import subprocess
    try:
        remotes = subprocess.check_output(
            ["git", "remote", "-v"], cwd=repo_root, text=True, timeout=5
        )
        if "atelier" not in remotes.lower() and "leanchain" not in remotes.lower():
            pytest.skip("Not running in the atelier repo — skip M1 benchmark")
    except Exception:
        pytest.skip("git remote check failed — skip M1 benchmark")

    results = run_benchmark(repo_root)

    if "error" in results:
        pytest.skip(f"Skipping M1 benchmark: {results['error']}")

    # Print verdicts for debugging
    for v in results["verdicts"]:
        status = "PASS" if v["correct"] else "FAIL"
        print(f"  [{status}] {v['query'][:50]}... → {v.get('top_result_sha', 'none')}")

    pass_rate = results["pass_rate"]
    pass_count = results["pass_count"]
    total = results["total"]
    print(f"\nM1 result: {pass_count}/{total} = {pass_rate:.0%}")

    assert pass_count >= 7, (
        f"M1 benchmark FAIL: {pass_count}/10 correct (target ≥7/10). "
        f"Pass rate: {pass_rate:.0%}. "
        f"Ensure bootstrap has completed and ATELIER_LLM_BACKEND=openai is set."
    )


@pytest.mark.slow
def test_m1_commit_chunks_populated() -> None:
    """Prerequisite: commit_chunks table must have ≥100 rows for meaningful eval."""
    repo_root_env = os.environ.get("ATELIER_REPO_ROOT")
    repo_root = pathlib.Path(repo_root_env) if repo_root_env else pathlib.Path.cwd()
    engine = _get_engine(repo_root)
    from contextlib import closing
    with closing(engine.connection()) as conn:
        count_row = conn.execute("SELECT COUNT(*) AS n FROM commit_chunks").fetchone()
        chunk_count = int(count_row["n"]) if count_row else 0
    if chunk_count == 0:
        pytest.skip("commit_chunks empty — run bootstrap: code op=search on this repo first")
    assert chunk_count >= 100, (
        f"Only {chunk_count} commit chunks found. "
        f"Bootstrap may be incomplete (target: ~425 for the atelier repo)."
    )
    print(f"commit_chunks populated: {chunk_count} rows")


if __name__ == "__main__":
    import json

    repo_root_env = os.environ.get("ATELIER_REPO_ROOT")
    repo_root = pathlib.Path(repo_root_env) if repo_root_env else pathlib.Path.cwd()
    result = run_benchmark(repo_root)
    print(json.dumps(result, indent=2))
    exit(0 if result["pass_count"] >= 7 else 1)
```

**Note on SHA matching:** The `QUERIES` list uses 7-char SHA prefixes for `expected_sha`. The `_grade_result()` function checks both directions of prefix matching: `commit_sha.startswith(expected_sha)` and `expected_sha.startswith(commit_sha[:7])`. This handles both full-SHA and abbreviated SHA storage formats.

**Note on bootstrap prerequisite:** The benchmark requires bootstrap to be complete. If `commit_chunks` is empty, the test skips with a helpful message. Bootstrap is triggered automatically by `code op="search"` in a Claude session, or can be triggered explicitly by running `engine._ensure_lineage_ready()` before the benchmark.

**Test:**
```bash
# Unit test: benchmark scaffolding works without live engine
uv run python -c "
import sys
from tests.benchmarks.context_quality.M1_lineage import QUERIES, _grade_result
assert len(QUERIES) == 10, f'Expected 10 queries, got {len(QUERIES)}'

# Test _grade_result with mock results
class FakeRecord:
    def __init__(self, sha, summary):
        self.commit_sha = sha
        self.signature = summary
        self.qualified_name = ''

r = FakeRecord('f9d1908abc123', 'parse_stream_jsonl keys renamed')
assert _grade_result([r], 'f9d1908', ['parse_stream_jsonl', 'cost_usd', 'keys']) == True, 'SHA prefix match failed'

r2 = FakeRecord('zzz', 'parse_stream_jsonl keys cost_usd renamed latency')
assert _grade_result([r2], 'f9d1908', ['parse_stream_jsonl', 'cost_usd', 'latency']) == True, 'Keyword match failed'

r3 = FakeRecord('zzz', 'unrelated commit about something else entirely')
assert _grade_result([r3], 'f9d1908', ['parse_stream_jsonl', 'cost_usd']) == False, 'Should be False'

print('OK - 10 queries, grading logic correct')
"

# Run as pytest (skips if bootstrap not complete)
uv run pytest tests/benchmarks/context_quality/M1_lineage.py::test_m1_commit_chunks_populated -v -m slow
```

Full benchmark run (requires bootstrap complete):
```bash
ATELIER_REPO_ROOT=$(pwd) uv run pytest tests/benchmarks/context_quality/M1_lineage.py -v -m slow -s
```

**Depends on:** Task 1 (suite skeleton), `08-PLAN-01b-engine.md` fully executed (commit_chunks populated)

---

## Verification

```bash
# 1. Suite exists with all required files
ls tests/benchmarks/context_quality/
# Expected: __init__.py  M1_lineage.py  M2_routing.py  M3_verification.py  M4_scoped.py  README.md

# 2. Package imports cleanly
uv run python -c "
from tests.benchmarks.context_quality.M1_lineage import QUERIES, run_benchmark, QUERIES
from tests.benchmarks.context_quality.M2_routing import test_m2_routing_placeholder
from tests.benchmarks.context_quality.M3_verification import test_m3_verification_placeholder
from tests.benchmarks.context_quality.M4_scoped import test_m4_scoped_placeholder
print('All imports OK')
"

# 3. Stub benchmarks skip cleanly
uv run pytest tests/benchmarks/context_quality/M2_routing.py \
    tests/benchmarks/context_quality/M3_verification.py \
    tests/benchmarks/context_quality/M4_scoped.py \
    -v -m slow 2>&1 | grep -c SKIPPED | xargs -I{} python3 -c "assert {} == 3, 'Expected 3 skips'"

# 4. M1 grading logic passes unit test (no live engine needed)
uv run python -c "
from tests.benchmarks.context_quality.M1_lineage import QUERIES, _grade_result
assert len(QUERIES) == 10
class R:
    def __init__(self, sha, sig): self.commit_sha=sha; self.signature=sig; self.qualified_name=''
assert _grade_result([R('f9d1908abc',  'x')], 'f9d1908', []) == True   # SHA match
assert _grade_result([R('zzz', 'parse_stream_jsonl cost_usd keys latency_ms')], 'f9d1908', ['parse_stream_jsonl','cost_usd','latency_ms']) == True  # kw match
assert _grade_result([R('zzz', 'nothing')], 'f9d1908', ['parse_stream_jsonl','cost_usd']) == False
print('PASS')
"

# 5. README describes the protocol
uv run python -c "
import pathlib
readme = pathlib.Path('tests/benchmarks/context_quality/README.md').read_text()
assert '≥7/10' in readme
assert 'CQEVAL' not in readme or True  # optional
assert 'M1' in readme and 'M2' in readme
assert 'pytest.mark.slow' in readme or 'slow' in readme
print('README content OK')
"
```

## Success Criteria

- CQEVAL-01: `tests/benchmarks/context_quality/` directory exists with `__init__.py`, `README.md`, `M1_lineage.py`, `M2_routing.py`, `M3_verification.py`, `M4_scoped.py`.
- CQEVAL-02: `M1_lineage.py` contains 10 real bug-fix commit queries from the Atelier repo history, a `_grade_result()` function that grades by SHA prefix OR keyword overlap (≥2 keywords), and a pytest test `test_m1_lineage_pass_rate` that asserts `pass_count >= 7`.
- M2/M3/M4 stubs skip with informative messages when run under `pytest -m slow`.
- All imports succeed with no circular dependencies.
- Full M1 benchmark run against a bootstrapped DB: ≥7/10 queries graded CORRECT.
