"""Read-only, source-derived file-level search relevance evaluation.

Unlike a router-only check, this runs the production LiteIndex.search scorer
against existing SQLite indexes. Source definitions establish *silver* gold
paths independently of the search output; this is NOT human-reviewed ground
truth and is not a full measure of natural-language retrieval quality.

Only existing SQLite indexes opened in mode=ro are queried. No index refresh,
source mutation, network calls, embeddings, persisted query logs or source-text
exports. Aggregate metrics are printed without queries, private symbols or
source snippets. Scratch state is in-process only.

Example:
  PYTHONPATH=client/src python benchmarks/workspace_search_file_relevance.py \\
    --root ~/Projects/Olaryn --cases-per-repo 12 --global-cases-per-repo 2
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import math
import re
import sqlite3
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow_client.lite.index import LiteIndex, _LiteConnection, _present_overlay
from lemoncrow_client.lite.workspace_search_plan import choose_first_wave, hints, normalize_member_ranks
from lemoncrow_client.workspace_members import WorkspaceRepo, discover_members

ROOT_REPOS: tuple[str, ...] = ("Authward", "Billing", "Trevra", "Beseam", "LemonCrow", "Design-System")
_RE_CODE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+([A-Za-z_][A-Za-z_0-9]{13,})\b",
    re.M,
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SKIPPED_SEGMENTS = frozenset(
    {
        "tests",
        "test",
        "fixtures",
        "migrations",
        "docs",
        "benchmarks",
        "vendor",
        "dist",
        "node_modules",
        "__pycache__",
        ".worktrees",
    }
)


@dataclass(frozen=True)
class GoldCase:
    repo: WorkspaceRepo
    path: str
    identifier: str


class ExistingIndex(LiteIndex):
    """Read-only view of an existing index; never refresh or migrate it."""

    def maybe_refresh(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None

    def _connect(self, *, initialize: bool = False, readonly: bool = False) -> _LiteConnection:
        if initialize or not readonly:
            raise AssertionError("this evaluation must never initialize or write an index")
        # A SQLite URI with mode=ro prevents writes, even if a future search
        # function changes. No immutable=1: valid WAL content must remain visible.
        connection = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=10, factory=_LiteConnection)
        try:
            connection.row_factory = sqlite3.Row
            _present_overlay(connection)
            connection.execute("PRAGMA query_only=ON")
        except BaseException:
            connection.close()
            raise
        return connection


def source_cases(repo: WorkspaceRepo, *, cap: int) -> list[GoldCase]:
    """Derive unique definitions from tracked source, not from search results."""
    tracked = (
        subprocess.run(["git", "ls-files", "-z"], cwd=repo.root, capture_output=True, timeout=40, check=True)
        .stdout.decode("utf-8", "replace")
        .split("\0")
    )
    definitions: defaultdict[str, set[str]] = defaultdict(set)
    for rel in tracked:
        if not rel.endswith((".py", ".ts", ".tsx")) or rel.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
            continue
        if any(segment.lower() in _SKIPPED_SEGMENTS for segment in rel.split("/")):
            continue
        path = repo.root / rel
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 200_000:
                continue
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if rel.endswith(".py"):
            try:
                root = ast.parse(contents)
            except SyntaxError:
                continue
            names = [
                node.name
                for node in ast.walk(root)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and len(node.name) > 13
                and not node.name.startswith("_")
            ]
        else:
            names = _RE_CODE.findall(contents)
        for name in names:
            definitions[name].add(rel)
    # Hash-based deterministic sample, independent of ranker/SQLite output.
    # Every identifier must appear in exactly one tracked source path.
    unique = [(name, next(iter(paths))) for name, paths in definitions.items() if len(paths) == 1]
    unique.sort(key=lambda item: hashlib.sha256(f"source-gold-v1:{repo.relpath}:{item[0]}:{item[1]}".encode()).digest())
    return [GoldCase(repo, path, name) for name, path in unique[:cap]]


def phrasing(identifier: str) -> str:
    words = _CAMEL.sub(" ", identifier).replace("_", " ")
    return f"where is {words.lower()} implemented"


def rank(found: list[str], expected: str, cutoff: int = 10) -> int | None:
    try:
        position = found.index(expected) + 1
    except ValueError:
        return None
    return position if position <= cutoff else None


def summary(positions: list[int | None]) -> dict[str, Any]:
    n = len(positions)
    return {
        "cases": n,
        "hit_1": sum(p == 1 for p in positions),
        "hit_3": sum(p is not None and p <= 3 for p in positions),
        "hit_5": sum(p is not None and p <= 5 for p in positions),
        "hit_10": sum(p is not None for p in positions),
        "mrr_10": round(sum(1.0 / p for p in positions if p) / n, 4) if n else None,
    }


def legacy_member_ranks(results: list[tuple[WorkspaceRepo, list[dict[str, Any]]]], query: str) -> list[dict[str, Any]]:
    """Frozen, read-only comparison to the pre-evaluation rank fusion.

    Never used for serving.  Retain this to reproduce both scores from the
    same source-derived cases and identical existing index snapshots.
    """

    relevance = {item.repo.root: item.relevance for item in hints(query, [repo for repo, _ in results])}
    output: list[dict[str, Any]] = []
    for repo, entries in results:

        def value(entry: dict[str, Any]) -> float:
            try:
                number = float(entry.get("score") or 0)
            except (ValueError, TypeError):
                return 0.0
            return max(0.0, number) if math.isfinite(number) else 0.0

        ordered = sorted(entries, key=lambda item: (-value(item), str(item.get("path") or "")))
        best = value(ordered[0]) if ordered else 0.0
        for position, hit in enumerate(ordered):
            score = value(hit)
            relative = score / best if best else 0.0
            prior = min(1.0, max(-0.25, relevance[repo.root] / 16.0))
            output.append(
                {**hit, "workspace_score": round(0.70 / (1.0 + 0.15 * position) + 0.25 * relative + 0.05 * prior, 6)}
            )
    return sorted(output, key=lambda item: (-item["workspace_score"], str(item.get("path") or "")))


def evaluate(root: Path, *, cases_per_repo: int, global_cases_per_repo: int, global_start: int = 0) -> dict[str, Any]:
    membership = discover_members(root)
    if membership.truncated:
        raise RuntimeError("workspace membership truncated; do not publish accuracy numbers")
    discovered = {r.relpath: r for r in membership.members}
    indexed = [r for r in membership.members if (r.root / ".lemoncrow/workspace/runtime-lite-v3.sqlite3").is_file()]
    indexes = {repo.relpath: ExistingIndex(repo.root) for repo in indexed}
    instances = {name: discovered[name] for name in ROOT_REPOS if name in discovered and name in indexes}
    if len(instances) != len(ROOT_REPOS):
        raise RuntimeError("an expected repository or existing index is missing")
    gold = {name: source_cases(repo, cap=cases_per_repo) for name, repo in instances.items()}
    if any(len(cases) < global_start + global_cases_per_repo for cases in gold.values()):
        raise RuntimeError("insufficient source definitions for requested sample")
    # The gold path is independently extracted from current tracked source.
    # We report absent index coverage separately instead of silently filtering.
    indexed_paths: dict[str, set[str]] = {}
    schema_versions: dict[str, int] = {}
    for name, index in indexes.items():
        with contextlib.closing(sqlite3.connect(f"{index.db_path.as_uri()}?mode=ro", uri=True, timeout=10)) as conn:
            indexed_paths[name] = {str(row[0]) for row in conn.execute("SELECT path FROM files")}
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            schema_versions[name] = int(row[0]) if row else -1

    local: defaultdict[str, list[int | None]] = defaultdict(list)
    local_indexed: defaultdict[str, list[int | None]] = defaultdict(list)
    missing: Counter[str] = Counter()
    local_times: list[float] = []
    global_complete: defaultdict[str, list[int | None]] = defaultdict(list)
    global_legacy: defaultdict[str, list[int | None]] = defaultdict(list)
    global_indexed: defaultdict[str, list[int | None]] = defaultdict(list)
    global_fast: defaultdict[str, list[int | None]] = defaultdict(list)
    fast_skipped = 0
    fast_expanded = 0
    stale_gold = 0
    for name, cases in gold.items():
        index = indexes[name]
        for case in cases:
            if case.path not in indexed_paths[name]:
                missing[name] += 1
            for mode, query in (("identifier", case.identifier), ("natural", phrasing(case.identifier))):
                started = time.perf_counter()
                result = index.search(query, limit=10)
                local_times.append((time.perf_counter() - started) * 1000)
                position = rank([str(hit["path"]) for hit in result.get("files", [])], case.path)
                local[mode].append(position)
                if case.path in indexed_paths[name]:
                    local_indexed[mode].append(position)

    for name, cases in gold.items():
        for case in cases[global_start : global_start + global_cases_per_repo]:
            for mode, query in (("identifier", case.identifier), ("natural", phrasing(case.identifier))):
                groups: list[tuple[WorkspaceRepo, list[dict[str, Any]]]] = []
                wave = set(choose_first_wave(query, membership.members))
                for repo in indexed:
                    payload = indexes[repo.relpath].search(query, limit=10)
                    hits = [{**hit, "path": repo.prefixed(str(hit["path"]))} for hit in payload["files"]]
                    groups.append((repo, hits))
                expected = case.repo.prefixed(case.path)
                exhaustive = normalize_member_ranks(groups, query)
                position = rank([hit["path"] for hit in exhaustive[:10]], expected)
                global_complete[mode].append(position)
                legacy = legacy_member_ranks(groups, query)
                global_legacy[mode].append(rank([hit["path"] for hit in legacy[:10]], expected))
                if case.path in indexed_paths[name]:
                    global_indexed[mode].append(position)
                first_wave = [group for group in groups if group[0].relpath in wave]
                first_has_hit = any(hits for _repo, hits in first_wave)
                if not first_has_hit:
                    fast_expanded += 1
                    fast_results = exhaustive
                else:
                    fast_skipped += case.repo.relpath not in wave
                    fast_results = normalize_member_ranks(first_wave, query)
                global_fast[mode].append(rank([hit["path"] for hit in fast_results[:10]], expected))
                if case.path not in indexed_paths[name]:
                    stale_gold += 1
    by_repo = {
        name: {
            "cases": len(cases),
            "source_gold_absent_from_index": missing[name],
            "index_schema": schema_versions[name],
        }
        for name, cases in gold.items()
    }
    return {
        "method": "source-derived silver file ground truth, not independently reviewed",
        "read_only_existing_indexes": True,
        "workspace_members": len(membership.members),
        "available_indexes": len(indexes),
        "gold_repositories": len(gold),
        "global_start": global_start,
        "global_cases_per_repo": global_cases_per_repo,
        "local": {mode: summary(positions) for mode, positions in local.items()},
        "local_with_source_present_in_index": {mode: summary(positions) for mode, positions in local_indexed.items()},
        "cross_repo_complete": {mode: summary(positions) for mode, positions in global_complete.items()},
        "cross_repo_legacy": {mode: summary(positions) for mode, positions in global_legacy.items()},
        "cross_repo_with_source_present_in_index": {
            mode: summary(positions) for mode, positions in global_indexed.items()
        },
        "cross_repo_fast": {mode: summary(positions) for mode, positions in global_fast.items()},
        "fast_skipped_gold_owner_on_first_wave_hit": fast_skipped,
        "fast_expanded_after_no_first_wave_hit": fast_expanded,
        "cross_repo_gold_absent_from_index": stale_gold,
        "local_search_latency_ms_median": round(statistics.median(local_times), 2) if local_times else None,
        "by_repo": by_repo,
        "caveats": [
            "No source or query values exported",
            "No index refresh; existing indexes may be stale",
            "Static source-derived labels, not human judgments",
            "Cross-repo simulation searches only repositories with existing indexes",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cases-per-repo", type=int, default=12)
    parser.add_argument("--global-cases-per-repo", type=int, default=2)
    parser.add_argument("--global-start", type=int, default=0, help="Disjoint, hash-selected source-gold slice")
    args = parser.parse_args()
    if not (
        1 <= args.cases_per_repo <= 50
        and 0 <= args.global_start <= args.cases_per_repo
        and 0 <= args.global_cases_per_repo <= args.cases_per_repo - args.global_start
    ):
        parser.error("cases-per-repo: 1..50; global-start and global-cases must fit within it")
    print(
        json.dumps(
            evaluate(
                args.root.resolve(),
                cases_per_repo=args.cases_per_repo,
                global_cases_per_repo=args.global_cases_per_repo,
                global_start=args.global_start,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
