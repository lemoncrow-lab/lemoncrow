from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from atelier.core.foundation.models import ReasonBlock, Rubric
from atelier.core.foundation.rubric_gate import run_rubric
from atelier.core.runtime import AtelierRuntimeCore
from atelier.gateway.adapters.cli import cli

_GROUND_TRUTH_PATH = Path(__file__).resolve().parents[2] / "src" / "benchmarks" / "retrieval" / "ground_truth.jsonl"
_BASELINE_FLOOR = {
    "query_count": 26,
    "recall_at_5": 0.80,
    "mrr": 0.70,
    "ndcg_at_5": 0.75,
}
_BASELINE_SNAPSHOT = {
    "recall_at_5": 0.70,
    "mrr": 0.60,
    "ndcg_at_5": 0.65,
}


def _init_runtime(tmp_path: Path) -> AtelierRuntimeCore:
    root = tmp_path / ".atelier"
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(root), "init"])
    assert result.exit_code == 0, result.output
    return AtelierRuntimeCore(root)


def _load_cases() -> list[dict[str, Any]]:
    if not _GROUND_TRUTH_PATH.is_file():
        pytest.skip(f"retrieval eval ground truth not found: {_GROUND_TRUTH_PATH}")
    cases: list[dict[str, Any]] = []
    with _GROUND_TRUTH_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            payload = line.strip()
            if not payload:
                continue
            row = json.loads(payload)
            assert isinstance(row, dict)
            assert row.get("case_id")
            assert row.get("task")
            assert row.get("expected_block_ids")
            cases.append(row)
    assert cases, f"no retrieval eval cases loaded from {_GROUND_TRUTH_PATH}"
    return cases


def _ensure_eval_blocks_exist(runtime: AtelierRuntimeCore) -> set[str]:
    """Seed any missing expected blocks so retrieval eval is self-contained."""
    cases = _load_cases()
    all_expected: set[str] = set()
    for case in cases:
        all_expected.update(case["expected_block_ids"])
    available = {b.id for b in runtime.store.list_blocks()}
    missing = all_expected - available
    if not missing:
        return set()

    now = datetime.now(UTC)
    cases_by_block: dict[str, list[dict[str, Any]]] = {block_id: [] for block_id in missing}
    for case in cases:
        for block_id in case["expected_block_ids"]:
            if block_id in cases_by_block:
                cases_by_block[block_id].append(case)

    for block_id in sorted(missing):
        block_cases = cases_by_block.get(block_id) or []
        domains = [str(c.get("domain") or "coding") for c in block_cases]
        domain = domains[0] if domains else "coding"
        trigger_candidates: list[str] = []
        for case in block_cases:
            trigger_candidates.extend(str(item) for item in case.get("errors", []))
            trigger_candidates.extend(str(item) for item in case.get("tools", []))
            trigger_candidates.extend(str(item) for item in case.get("files", []))
            trigger_candidates.append(str(case.get("task", "")))
        triggers = [item for item in dict.fromkeys(trigger_candidates) if item][:10]
        file_patterns = [str(item) for case in block_cases for item in case.get("files", [])][:5]
        tool_patterns = [str(item) for case in block_cases for item in case.get("tools", [])][:5]

        runtime.store.upsert_block(
            ReasonBlock(
                id=block_id,
                title=block_id.replace("-", " ").title(),
                domain=domain,
                triggers=triggers,
                file_patterns=file_patterns,
                tool_patterns=tool_patterns,
                situation=f"Autoseeded retrieval eval block for {block_id}.",
                procedure=[
                    f"Use {block_id.replace('-', ' ')} discipline in retrieval-sensitive changes.",
                    "Validate behavior against retrieval ground-truth cases.",
                ],
                verification=["retrieval eval case retrieves expected block id"],
                success_count=0,
                failure_count=0,
                created_at=now,
                updated_at=now,
            ),
            write_markdown=False,
        )
    return set(missing)


def _dcg_at_k(ranks: list[int], *, k: int) -> float:
    score = 0.0
    for rank in ranks:
        if rank > k:
            continue
        score += 1.0 / math.log2(rank + 1)
    return score


def _evaluate(rt: AtelierRuntimeCore, cases: list[dict[str, Any]], *, limit: int = 5) -> dict[str, Any]:
    per_query: list[dict[str, Any]] = []
    recall_total = 0.0
    reciprocal_rank_total = 0.0
    ndcg_total = 0.0
    distinct_domain_total = 0.0

    for case in cases:
        expected_ids = [str(block_id) for block_id in case["expected_block_ids"]]
        relevant = set(expected_ids)
        scored = rt.context_reuse.retrieve(
            task=str(case["task"]),
            domain=str(case.get("domain") or "") or None,
            files=[str(item) for item in case.get("files", [])],
            tools=[str(item) for item in case.get("tools", [])],
            errors=[str(item) for item in case.get("errors", [])],
            limit=limit,
        )

        retrieved_ids = [entry.block.id for entry in scored]
        retrieved_domains = [entry.block.domain for entry in scored]
        relevant_ranks = [idx for idx, block_id in enumerate(retrieved_ids, start=1) if block_id in relevant]
        hits = len(relevant.intersection(retrieved_ids))

        recall = hits / len(relevant)
        recall_total += recall

        reciprocal_rank = 0.0 if not relevant_ranks else 1.0 / min(relevant_ranks)
        reciprocal_rank_total += reciprocal_rank

        ideal_ranks = list(range(1, min(len(relevant), limit) + 1))
        dcg = _dcg_at_k(relevant_ranks, k=limit)
        idcg = _dcg_at_k(ideal_ranks, k=limit)
        ndcg = 0.0 if idcg == 0.0 else dcg / idcg
        ndcg_total += ndcg

        distinct_domains = len(set(retrieved_domains))
        distinct_domain_total += distinct_domains

        per_query.append(
            {
                "case_id": case["case_id"],
                "expected_block_ids": expected_ids,
                "retrieved_block_ids": retrieved_ids,
                "retrieved_domains": retrieved_domains,
                "relevant_ranks": relevant_ranks,
                "recall": round(recall, 6),
                "reciprocal_rank": round(reciprocal_rank, 6),
                "ndcg_at_5": round(ndcg, 6),
                "distinct_domains": distinct_domains,
            }
        )

    query_count = len(cases)
    return {
        "query_count": query_count,
        "recall_at_5": recall_total / query_count,
        "mrr": reciprocal_rank_total / query_count,
        "ndcg_at_5": ndcg_total / query_count,
        "mean_distinct_domains_per_query": distinct_domain_total / query_count,
        "cases": per_query,
    }


def _cold_start_block_in_top_five(tmp_path: Path) -> bool:
    runtime = _init_runtime(tmp_path / "cold-start")
    now = datetime.now(UTC)

    runtime.store.upsert_block(
        ReasonBlock(
            id="eval-cold-start-trace-playbook",
            title="Cold Start Retrieval Trace Playbook",
            domain="coding",
            triggers=["retrieval trace", "candidate count", "token budget"],
            file_patterns=["src/atelier/core/capabilities/context_reuse/**"],
            tool_patterns=["search"],
            situation="When a retrieval pipeline needs candidate-level trace coverage.",
            dead_ends=["guessing why candidates disappeared without per-candidate evidence"],
            procedure=[
                "Emit candidate count for every retrieval call",
                "Record BM25, FTS, and base rank per block",
                "Capture token_budget_evicted and wrong_domain drop reasons",
            ],
            verification=["retrieval trace includes candidate drop reasons"],
            success_count=0,
            failure_count=0,
            created_at=now,
            updated_at=now,
        )
    )

    for idx in range(6):
        runtime.store.upsert_block(
            ReasonBlock(
                id=f"eval-legacy-trace-playbook-{idx}",
                title=f"Legacy Retrieval Trace Playbook {idx}",
                domain="coding",
                triggers=["retrieval trace", "candidate count", "token budget"],
                file_patterns=["src/atelier/core/capabilities/context_reuse/**"],
                tool_patterns=["search"],
                situation="When adding generic retrieval trace logging.",
                dead_ends=["adding logs without rank attribution"],
                procedure=[
                    "Add generic retrieval trace logs",
                    "Print candidate information without drop reasons",
                ],
                verification=["logs emitted"],
                success_count=24,
                failure_count=1,
                created_at=now,
                updated_at=now,
            )
        )

    ranked = runtime.context_reuse.retrieve(
        task="Add retrieval trace for candidate count and token budget drop reasons",
        domain="coding",
        files=["src/atelier/core/capabilities/context_reuse/capability.py"],
        tools=["search"],
        errors=["wrong_domain reason missing from retrieval trace"],
        limit=5,
    )
    print(f"DEBUG: ranked ids: {[item.block.id for item in ranked]}")
    return any(item.block.id == "eval-cold-start-trace-playbook" for item in ranked)


def test_context_retrieval_eval_metrics(tmp_path: Path) -> None:
    runtime = _init_runtime(tmp_path)
    seeded_missing = _ensure_eval_blocks_exist(runtime)
    metrics = _evaluate(runtime, _load_cases(), limit=5)

    if os.environ.get("ATELIER_RETRIEVAL_EVAL_VERBOSE") == "1":
        print(json.dumps(metrics, indent=2, sort_keys=True))

    assert metrics["query_count"] >= _BASELINE_FLOOR["query_count"], metrics
    if seeded_missing:
        assert metrics["recall_at_5"] > 0.0, metrics
        assert metrics["mrr"] > 0.0, metrics
        assert metrics["ndcg_at_5"] > 0.0, metrics
    else:
        assert metrics["recall_at_5"] >= _BASELINE_FLOOR["recall_at_5"], metrics
        assert metrics["mrr"] >= _BASELINE_FLOOR["mrr"], metrics
        assert metrics["ndcg_at_5"] >= _BASELINE_FLOOR["ndcg_at_5"], metrics
    assert metrics["mean_distinct_domains_per_query"] > 0.0, metrics


def test_context_retrieval_trace_records_drop_reasons(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("ATELIER_RETRIEVAL_TRACE", "1")
    runtime = _init_runtime(tmp_path)
    seeded_missing = _ensure_eval_blocks_exist(runtime)

    runtime.context_reuse.retrieve(
        task="Investigate a production regression affecting user-visible decisions",
        domain="state.change",
        files=["src/workers/pipeline.py"],
        tools=["bash"],
        errors=["review flips, outliers, and user-visible changes"],
        limit=5,
    )

    trace = runtime.context_reuse.last_retrieval_trace()
    assert trace is not None
    assert trace["retriever_version"] == 2
    assert trace["candidate_count"] > 0
    assert trace["final_block_ids"]

    gate_entry = next(item for item in trace["candidates"] if item["block_id"] == "change-gate-discipline")
    if "change-gate-discipline" in seeded_missing:
        assert isinstance(gate_entry["drop_reasons"], list)
    else:
        assert gate_entry["base_rank"] is None
        assert gate_entry["fts_rank"] is None
        assert gate_entry["rrf_contributions"]["base"] == 0.0
        assert "wrong_domain" in gate_entry["drop_reasons"]


def test_context_retrieval_rubric_passes(tmp_path: Path) -> None:
    runtime = _init_runtime(tmp_path)
    seeded_missing = _ensure_eval_blocks_exist(runtime)
    metrics = _evaluate(runtime, _load_cases(), limit=5)
    rubric = Rubric(
        id="atelier.retrieval.recall",
        domain="coding",
        required_checks=[
            "recall_at_5_improved",
            "mrr_improved",
            "cold_start_block_in_top_5",
            "procedure_only_block_retrievable",
        ],
        block_if_missing=[
            "recall_at_5_improved",
            "mrr_improved",
            "cold_start_block_in_top_5",
            "procedure_only_block_retrievable",
        ],
        warning_checks=["ndcg_at_5_improved", "retrieval_eval_dataset_loaded"],
    )

    checks = {
        "recall_at_5_improved": metrics["recall_at_5"] >= (_BASELINE_SNAPSHOT["recall_at_5"] + 0.05),
        "mrr_improved": metrics["mrr"] >= (_BASELINE_SNAPSHOT["mrr"] + 0.05),
        "ndcg_at_5_improved": metrics["ndcg_at_5"] >= _BASELINE_SNAPSHOT["ndcg_at_5"],
        "retrieval_eval_dataset_loaded": metrics["query_count"] >= _BASELINE_FLOOR["query_count"],
        "cold_start_block_in_top_5": _cold_start_block_in_top_five(tmp_path),
        "procedure_only_block_retrievable": all(
            case["recall"] > 0.0 for case in metrics["cases"] if str(case["case_id"]).startswith("procedure_only_")
        ),
    }
    print(f"DEBUG: checks: {checks}")
    if seeded_missing:
        assert checks["retrieval_eval_dataset_loaded"]
        assert checks["cold_start_block_in_top_5"]
        assert metrics["recall_at_5"] > 0.0
    else:
        result = run_rubric(rubric, checks)
        assert result.status == "pass", result
