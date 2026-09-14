#!/usr/bin/env python3
"""Profile the Review Reader on a real Git range.

The benchmark intentionally separates two costs:

1. capture/persist: build the immutable review packet and durable raw units;
2. reader projection: load the stored revision and derive non-overlapping
   ReviewTargets, outline rows, progress, and revision-delta bookkeeping.

Example:

    uv run python benchmarks/review/bench_reader.py \
      --repo-root . --base HEAD~100 --head HEAD --no-impact \
      --out /tmp/review-reader-perf.json

Use ``--with-impact`` when you explicitly want code-intelligence/impact analysis
included in the capture timing. The projection timing is the same reader path
used by the browser regardless of how expensive capture was.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.review.revisions import compute_frontier
from lemoncrow.pro.capabilities.review.sources.local import read_packet_json
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.targets import (
    build_review_outline,
    derive_review_targets,
    review_progress,
    revision_target_delta,
)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _capture(
    repo: Path,
    store_root: Path,
    *,
    base: str,
    head: str,
    limit: int,
    with_impact: bool,
) -> tuple[float, int]:
    cmd = [
        sys.executable,
        "-m",
        "lemoncrow.gateway.cli",
        "--root",
        str(store_root),
        "review",
        "--repo-root",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--limit",
        str(limit),
        "--json",
        "--track",
        "--no-provenance",
    ]
    if not with_impact:
        cmd.append("--no-impact")

    before_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    started = time.perf_counter()
    completed = subprocess.run(
        cmd,
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    if completed.returncode != 0:
        raise RuntimeError(f"review capture failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
    after_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # Linux reports KiB. On macOS ru_maxrss is bytes, but this benchmark is
    # comparative and keeps the raw platform value explicit in the JSON key.
    peak_rss = max(before_rss, after_rss)
    return elapsed_ms, peak_rss


def _projection(store_root: Path, repeat: int) -> dict[str, Any]:
    store = ReviewStore(store_root)
    started = time.perf_counter()
    sessions = store.list_sessions(status="", limit=10)
    if len(sessions) != 1:
        raise RuntimeError(f"isolated benchmark store should contain one review, found {len(sessions)}")
    session = sessions[0]
    revisions = store.list_revisions(session.id)
    if not revisions:
        raise RuntimeError("benchmark review has no revision")
    revision = revisions[-1]
    units = store.list_units(revision.id)
    marks = store.list_marks(session.id, reviewer_id=session.reviewer_id)
    annotations = store.list_annotations(session.id)
    packet = read_packet_json(store, revision)
    frontier = compute_frontier(
        session.id,
        session.reviewer_id,
        revision,
        units,
        marks,
        annotations=annotations,
    )
    load_ms = (time.perf_counter() - started) * 1000

    # One unreported warm-up removes import/cache noise while keeping all work
    # inside the same process and against exactly the same immutable revision.
    derive_review_targets(units, frontier.entries, packet, annotations=annotations)

    samples: list[float] = []
    targets = ()
    for _ in range(max(1, repeat)):
        started = time.perf_counter()
        targets = derive_review_targets(units, frontier.entries, packet, annotations=annotations)
        samples.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    outline = build_review_outline(targets)
    outline_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    progress = review_progress(targets)
    progress_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    unchanged_delta = revision_target_delta(targets, targets)
    delta_ms = (time.perf_counter() - started) * 1000

    packet_files = packet.get("files", ()) if isinstance(packet, dict) else ()
    changed_lines = 0
    if isinstance(packet_files, list):
        for row in packet_files:
            if isinstance(row, dict):
                changed_lines += int(row.get("additions") or 0) + int(row.get("deletions") or 0)

    return {
        "review_id": session.id,
        "revision_id": revision.id,
        "files": len(packet_files),
        "changed_lines": changed_lines,
        "raw_units": len(units),
        "targets": len(targets),
        "outline_rows": len(outline),
        "annotations": len(annotations),
        "load_projection_inputs_ms": round(load_ms, 3),
        "derive_samples_ms": [round(value, 3) for value in samples],
        "derive_median_ms": round(statistics.median(samples), 3),
        "derive_p95_ms": round(_percentile(samples, 0.95), 3),
        "outline_ms": round(outline_ms, 3),
        "progress_ms": round(progress_ms, 3),
        "unchanged_delta_ms": round(delta_ms, 3),
        "progress_target_count": progress.target_count,
        "delta_preserved": len(unchanged_delta.preserved),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Review Reader capture and target projection on a real repo."
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--base", default="HEAD~100")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--repeat", type=int, default=9)
    impact = parser.add_mutually_exclusive_group()
    impact.add_argument(
        "--with-impact", action="store_true", help="Include impact/code-intelligence analysis in capture."
    )
    impact.add_argument("--no-impact", action="store_true", help="Explicitly disable impact analysis (the default).")
    parser.add_argument("--store-root", default="", help="Use this isolated store instead of a temporary directory.")
    parser.add_argument("--keep-store", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    repo = Path(args.repo_root).resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"not a Git repository: {repo}")
    base_sha = _git(repo, "rev-parse", args.base)
    head_sha = _git(repo, "rev-parse", args.head)
    shortstat = _git(repo, "diff", "--shortstat", f"{args.base}..{args.head}")

    temporary = not bool(args.store_root)
    store_root = (
        Path(args.store_root).resolve() if args.store_root else Path(tempfile.mkdtemp(prefix="lc-review-bench-"))
    )
    if store_root.exists() and any(store_root.iterdir()):
        raise SystemExit(f"benchmark store must be empty: {store_root}")

    try:
        capture_ms, child_peak_rss_raw = _capture(
            repo,
            store_root,
            base=args.base,
            head=args.head,
            limit=max(1, args.limit),
            with_impact=bool(args.with_impact),
        )
        projection = _projection(store_root, max(1, args.repeat))
        result: dict[str, Any] = {
            "repo_root": str(repo),
            "base": args.base,
            "head": args.head,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "git_shortstat": shortstat,
            "impact_enabled": bool(args.with_impact),
            "capture_persist_ms": round(capture_ms, 3),
            "capture_child_peak_rss_raw": child_peak_rss_raw,
            **projection,
        }

        rendered = json.dumps(result, indent=2, sort_keys=True)
        print(rendered)
        if args.out:
            Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    finally:
        if temporary and not args.keep_store:
            shutil.rmtree(store_root, ignore_errors=True)
        elif args.keep_store:
            print(f"[bench] kept store at {store_root}", file=sys.stderr)


if __name__ == "__main__":
    main()
