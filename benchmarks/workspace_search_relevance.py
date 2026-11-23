"""Read-only smoke eval of repository routing on a real multi-repo workspace.

Usage: PYTHONPATH=client/src python benchmarks/workspace_search_relevance.py --root ~/Projects/Olaryn

Reads only bounded, local README metadata, not private source files, and emits
repository labels and query coverage; no source text, embeddings or indexes.
The ten manually labeled queries are a diagnostic, not a statistically valid IR dataset.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from lemoncrow_client.lite.workspace_search_plan import choose_first_wave, hints
from lemoncrow_client.workspace_members import discover_members

CASES: tuple[tuple[str, str], ...] = (
    ("Authward", "oauth account linking error"),
    ("Billing", "stripe subscriptions invoice coupon"),
    ("Trevra", "email warmup campaign recovery"),
    ("LemonCrow", "code_search review mcp"),
    ("Secrets", "OpenBao AppRole SecretID backup"),
    ("Beseam", "shopify product visibility"),
    ("Foot-Measure-Artifacts/foot-measure-data", "shoe foot length measurements dataset"),
    ("Console", "admin dashboard user roles"),
    ("Analytics", "conversion attribution dashboard"),
    ("CI-Lifeboat", "github runner CI recovery"),
)


def evaluate(root: Path, *, limit: int = 8) -> dict[str, object]:
    membership = discover_members(root)
    if membership.truncated:
        raise RuntimeError("repository discovery was truncated; ranking evaluation would be misleading")
    by_name = {repo.relpath for repo in membership.members}
    misses = [name for name, _query in CASES if name not in by_name]
    if misses:
        raise RuntimeError(f"expected checkouts missing: {', '.join(misses)}")
    baseline_hits = 0
    improved_hits = 0
    times = []
    rows = []
    for owner, query in CASES:
        baseline = [hint.repo.relpath for hint in hints(query, membership.members)[:limit]]
        start = time.perf_counter()
        improved = choose_first_wave(query, membership.members, cap=limit)
        times.append((time.perf_counter() - start) * 1000)
        baseline_hits += owner in baseline
        improved_hits += owner in improved
        rows.append({"owner": owner, "query": query, "baseline": owner in baseline, "planned": owner in improved})
    return {
        "root": str(root),
        "repositories": len(membership.members),
        "labeled_queries": len(CASES),
        "first_wave": limit,
        "baseline_recall_at_8": baseline_hits,
        "planned_recall_at_8": improved_hits,
        "median_planning_ms": round(statistics.median(times), 3),
        "max_planning_ms": round(max(times), 3),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Existing multi-repository root (read-only)")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.limit <= 64:
        parser.error("limit must be 1-64")
    print(json.dumps(evaluate(args.root.resolve(), limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
