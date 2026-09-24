"""Collect and score per-channel retrieval traces from CodeContextEngine.

This benchmark is the measurement layer for ranking work.  It records the
existing retrieval channels independently, the final production explore order,
and candidate-oracle diagnostics without changing serving behavior.

Examples::

    # Small deterministic sample using provisioned benchmark repositories
    LEMONCROW_CODE_EMBEDDER=bge uv run python \
      benchmarks/codebench/eval_retrieval_oracle.py --sample 50

    # One repository and one gold set
    uv run python benchmarks/codebench/eval_retrieval_oracle.py \
      --repo django --gold benchmarks/codebench/data/bench_pairs_def_gold.json \
      --out-dir reports/benchmark/retrieval-oracle/django

The output directory contains immutable-friendly JSONL traces, per-case scores,
and an aggregate report.  Callers should choose a unique directory per run.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from lemoncrow.core.foundation.paths import workspace_key
from lemoncrow.pro.capabilities.code_context import CodeContextEngine
from lemoncrow.pro.capabilities.code_context.models import SymbolRecord

from benchmarks.codebench.retrieval_oracle import (
    DEFAULT_CUTOFFS,
    RetrievalTrace,
    aggregate_scores,
    normalize_path,
    score_trace,
)

_DEFAULT_GOLDS = (
    "bench_pairs_def_gold.json",
    "bench_pairs_content_gold.json",
    "bench_pairs_semantic_gold.json",
    "bench_pairs_swebench_gold.json",
    "bench_pairs_sessions_gold.json",
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, set | frozenset | tuple | list):
        return [_jsonable(item) for item in value]
    return value


def _dedupe_paths(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = normalize_path(value)
        if not path or path in seen:
            continue
        seen.add(path)
        output.append(path)
    return output


def _symbol_file_candidates(symbols: Sequence[SymbolRecord], *, limit: int) -> list[dict[str, Any]]:
    """Collapse score-ranked symbols to first/best evidence per file."""

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for symbol in symbols:
        path = normalize_path(symbol.file_path)
        if not path or path in seen:
            continue
        seen.add(path)
        output.append(
            {
                "path": path,
                "score": float(symbol.score) if symbol.score is not None else None,
                "symbol_id": symbol.symbol_id,
                "symbol_name": symbol.symbol_name,
                "qualified_name": symbol.qualified_name,
                "kind": symbol.kind,
                "start_line": symbol.start_line,
            }
        )
        if len(output) >= limit:
            break
    return output


def _ranked_details(paths: Sequence[str], details: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for path in paths:
        normalized = normalize_path(path)
        detail = details.get(path) or details.get(normalized) or {}
        score_raw = detail.get("confidence")
        output.append(
            {
                "path": normalized,
                "score": float(score_raw) if score_raw is not None else None,
                **{str(key): _jsonable(value) for key, value in detail.items()},
            }
        )
    return output


def _payload_ranking(payload: Mapping[str, Any]) -> list[str]:
    """Mirror the public result's primary-first, recall-tail navigation order."""

    primary: list[str] = []
    raw_files = payload.get("files") or ()
    if isinstance(raw_files, Sequence) and not isinstance(raw_files, (str, bytes)):
        for entry in raw_files:
            if isinstance(entry, Mapping):
                primary.append(str(entry.get("path") or entry.get("file_path") or ""))
            elif isinstance(entry, str):
                primary.append(entry)

    tails: list[str] = []
    for key in ("fused_recall", "additional_relevant_files", "deep_recall"):
        raw = payload.get(key) or ()
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            tails.extend(str(value) for value in raw)
    return _dedupe_paths([*primary, *tails])


def _resolve_gold_paths(explicit: Sequence[Path]) -> list[Path]:
    if explicit:
        return [path.resolve() for path in explicit]
    base = Path("benchmarks/codebench/data")
    return [(base / name).resolve() for name in _DEFAULT_GOLDS if (base / name).exists()]


def _load_cases(
    gold_paths: Sequence[Path], *, repo_filter: str, sample: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cases_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    repos: dict[str, Any] = {}
    seen: set[tuple[str, str, str, str]] = set()

    for gold_path in gold_paths:
        raw = json.loads(gold_path.read_text(encoding="utf-8"))
        kind = str(raw.get("gold_kind") or gold_path.stem)
        true_map = raw.get("true_map") or {}
        repos.update(raw.get("repos") or {})
        for pair in raw.get("pairs") or ():
            if not isinstance(pair, Sequence) or len(pair) != 3:
                continue
            query, task_id, prefix = (str(pair[0]), str(pair[1]), str(pair[2]))
            if repo_filter and repo_filter not in prefix:
                continue
            key = (kind, prefix, task_id, query)
            if key in seen:
                continue
            seen.add(key)
            gold_files = _dedupe_paths(str(value) for value in true_map.get(task_id, ()) if value)
            if not gold_files:
                continue
            cases_by_repo[prefix].append(
                {
                    "case_id": f"{kind}:{task_id}",
                    "query": query,
                    "repo": prefix,
                    "gold_files": gold_files,
                    "gold_kind": kind,
                    "task_id": task_id,
                    "gold_source": str(gold_path),
                }
            )

    selected: list[dict[str, Any]] = []
    if sample > 0:
        per_repo = max(1, sample // max(1, len(cases_by_repo)))
        for prefix in sorted(cases_by_repo):
            selected.extend(sorted(cases_by_repo[prefix], key=lambda item: (item["query"], item["case_id"]))[:per_repo])
        selected = selected[:sample]
    else:
        for prefix in sorted(cases_by_repo):
            selected.extend(cases_by_repo[prefix])
    return selected, repos


def _db_for(meta: Mapping[str, Any]) -> Path:
    if meta.get("db"):
        return Path(str(meta["db"])).expanduser().resolve()
    workspace = Path(str(meta["ws"])).expanduser().resolve()
    return Path("/tmp") / workspace_key(workspace) / "code_context.sqlite"


def _channel_call(name: str, fn: Any) -> tuple[Any, float, str | None]:
    started = time.perf_counter()
    try:
        return fn(), (time.perf_counter() - started) * 1000.0, None
    except Exception as exc:  # benchmark records a failed channel; other channels continue
        return None, (time.perf_counter() - started) * 1000.0, f"{type(exc).__name__}: {exc}"


def _collect_trace(
    engine: CodeContextEngine,
    case: Mapping[str, Any],
    *,
    max_candidates: int,
    max_files: int,
    auto_index: bool,
    include_source: bool,
) -> dict[str, Any]:
    query = str(case["query"])
    plan = engine._hef_parse_query(query)
    channels: dict[str, list[Any]] = {}
    latency_ms: dict[str, float] = {}
    errors: dict[str, str] = {}

    lexical, elapsed, error = _channel_call(
        "lexical",
        lambda: engine.search_symbols(
            query,
            limit=max_candidates,
            mode="lexical",
            snippet="none",
            auto_index=auto_index,
            _candidate_files=set(),
        ),
    )
    latency_ms["lexical"] = elapsed
    if error:
        errors["lexical"] = error
    channels["lexical"] = _symbol_file_candidates(lexical or [], limit=max_candidates)

    zoekt, elapsed, error = _channel_call(
        "zoekt", lambda: engine._zoekt_candidate_files(query, max_files=max_candidates)
    )
    latency_ms["zoekt"] = elapsed
    if error:
        errors["zoekt"] = error
    channels["zoekt"] = list(zoekt or [])

    semantic, elapsed, error = _channel_call(
        "semantic", lambda: engine._semantic_candidate_evidence(query, max_files=max_candidates)
    )
    latency_ms["semantic"] = elapsed
    if error:
        errors["semantic"] = error
    channels["semantic"] = [
        {"path": path, "score": evidence.best_score, **evidence.payload()}
        for path, evidence in (semantic or {}).items()
    ]

    exact, elapsed, error = _channel_call("exact", lambda: engine._hef_exact_symbol_candidates(plan))
    latency_ms["exact"] = elapsed
    if error:
        errors["exact"] = error
    exact_paths, exact_details = exact or ([], {})
    channels["exact"] = _ranked_details(exact_paths, exact_details)

    anchors, elapsed, error = _channel_call("anchors", lambda: engine._hef_anchor_zoekt_candidates(plan))
    latency_ms["anchors"] = elapsed
    if error:
        errors["anchors"] = error
    anchor_paths, anchor_details = anchors or ([], {})
    channels["anchors"] = _ranked_details(anchor_paths, anchor_details)

    line, elapsed, error = _channel_call("line", lambda: engine._hef_line_fts_candidates(plan))
    latency_ms["line"] = elapsed
    if error:
        errors["line"] = error
    line_paths, line_details = line or ([], {})
    channels["line"] = _ranked_details(line_paths, line_details)

    actual, elapsed, error = _channel_call(
        "actual",
        lambda: engine.tool_explore(
            query,
            max_files=max_files,
            max_symbols=min(30, max(max_files * 3, 20)),
            include_source=include_source,
            skeletonize=include_source,
            complete_families=False,
            budget_tokens=9_000 if include_source else 2_000,
            auto_index=auto_index,
        ),
    )
    latency_ms["actual"] = elapsed
    if error:
        errors["actual"] = error
    actual_payload = actual if isinstance(actual, Mapping) else {}

    return {
        "case_id": case["case_id"],
        "query": query,
        "repo": case["repo"],
        "gold_files": case["gold_files"],
        "channels": channels,
        "actual_ranking": _payload_ranking(actual_payload),
        "metadata": {
            "gold_kind": case["gold_kind"],
            "task_id": case["task_id"],
            "gold_source": case["gold_source"],
            "intent": plan.intent,
            "latency_ms": {key: round(value, 3) for key, value in latency_ms.items()},
            "errors": errors,
            "actual_experiment": actual_payload.get("experiment"),
        },
    }


def _git_version() -> dict[str, Any]:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
        return {"sha": sha, "dirty": dirty}
    except Exception:
        return {"sha": "unknown", "dirty": True}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect candidate-oracle traces from LemonCrow code search")
    parser.add_argument("--gold", type=Path, action="append", default=[], help="Gold JSON; repeatable")
    parser.add_argument("--repo", default="", help="Repository-prefix substring filter")
    parser.add_argument("--sample", type=int, default=0, help="Deterministic total query sample (0 = all)")
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--max-files", type=int, default=8, help="Final production explore file cap (max 8)")
    parser.add_argument("--auto-index", action="store_true", help="Build missing indexes instead of skipping")
    parser.add_argument(
        "--include-source",
        action="store_true",
        help="Collect final production ranking with source sections, matching one-shot serving.",
    )
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--cutoffs", default=",".join(str(value) for value in DEFAULT_CUTOFFS))
    args = parser.parse_args(argv)

    gold_paths = _resolve_gold_paths(args.gold)
    if not gold_paths:
        raise SystemExit("no gold files found; pass --gold")
    cases, repo_metadata = _load_cases(gold_paths, repo_filter=args.repo, sample=max(0, args.sample))
    if not cases:
        raise SystemExit("no matching labelled cases")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or Path("reports/benchmark/retrieval-oracle") / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / "traces.jsonl"
    score_path = out_dir / "scores.jsonl"
    aggregate_path = out_dir / "aggregate.json"

    cutoffs = tuple(int(value.strip()) for value in args.cutoffs.split(",") if value.strip())
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case["repo"])].append(case)

    scores = []
    processed = 0
    with trace_path.open("w", encoding="utf-8") as trace_handle, score_path.open("w", encoding="utf-8") as score_handle:
        for prefix in sorted(grouped):
            meta = repo_metadata.get(prefix)
            if not isinstance(meta, Mapping) or not meta.get("ws"):
                print(f"[oracle] skip {prefix}: missing repository metadata", file=sys.stderr, flush=True)
                continue
            workspace = Path(str(meta["ws"])).expanduser().resolve()
            db_path = _db_for(meta)
            if not workspace.is_dir():
                print(f"[oracle] skip {prefix}: workspace missing at {workspace}", file=sys.stderr, flush=True)
                continue
            if not args.auto_index and not db_path.exists():
                print(f"[oracle] skip {prefix}: index missing at {db_path}", file=sys.stderr, flush=True)
                continue

            print(f"[oracle] repo={prefix} cases={len(grouped[prefix])}", file=sys.stderr, flush=True)
            engine = CodeContextEngine(workspace, db_path=db_path, autosync_enabled=False)
            if args.auto_index:
                print(f"[oracle] indexing {prefix} -> {db_path}", file=sys.stderr, flush=True)
                # Call the code-context indexer directly. ``tool_index`` first
                # refreshes the routed symbol-intel store, which can hold a writer
                # connection against a brand-new custom benchmark DB. The oracle
                # collector only needs the local index and must not introduce that
                # unrelated lifecycle into its baseline timing.
                engine.index_repo(force=False, require_lock=True)
            else:
                engine._schema_ready = True
            with contextlib.suppress(Exception):
                engine._symbol_centrality_map()

            for case in grouped[prefix]:
                trace_raw = _collect_trace(
                    engine,
                    case,
                    max_candidates=max(1, args.max_candidates),
                    max_files=max(1, min(8, args.max_files)),
                    auto_index=args.auto_index,
                    include_source=args.include_source,
                )
                trace_handle.write(json.dumps(_jsonable(trace_raw), sort_keys=True) + "\n")
                trace_handle.flush()
                score = score_trace(RetrievalTrace.from_raw(trace_raw), cutoffs=cutoffs)
                scores.append(score)
                score_handle.write(json.dumps(_jsonable(score.__dict__), sort_keys=True) + "\n")
                score_handle.flush()
                processed += 1
                if processed % 20 == 0:
                    print(f"[oracle] processed={processed}/{len(cases)}", file=sys.stderr, flush=True)

    aggregate = aggregate_scores(scores)
    report = {
        "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "git": _git_version(),
        "config": {
            "gold_files": [str(path) for path in gold_paths],
            "repo_filter": args.repo,
            "sample": args.sample,
            "max_candidates": args.max_candidates,
            "max_files": args.max_files,
            "include_source": args.include_source,
            "cutoffs": cutoffs,
            "code_embedder": os.environ.get("LEMONCROW_CODE_EMBEDDER", ""),
            "code_embed_model": os.environ.get("LEMONCROW_CODE_EMBED_MODEL", ""),
            "zoekt_mode": os.environ.get("LEMONCROW_ZOEKT_MODE", ""),
        },
        "requested_cases": len(cases),
        "processed_cases": processed,
        "metrics": _jsonable(aggregate.__dict__),
        "artifacts": {"traces": str(trace_path), "scores": str(score_path)},
    }
    aggregate_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
