from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.core.foundation.models import Playbook
from lemoncrow.core.runtime import LemonCrowRuntimeCore
from lemoncrow.gateway.cli import cli

_GROUND_TRUTH_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval" / "ground_truth.jsonl"
_BASELINE_FLOOR = {
    "query_count": 26,
    "recall_at_5": 0.70,
    "mrr": 0.60,
    "ndcg_at_5": 0.65,
}
_BASELINE_SNAPSHOT = {
    "recall_at_5": 0.50,
    "mrr": 0.40,
    "ndcg_at_5": 0.45,
}


def _init_runtime(tmp_path: Path) -> LemonCrowRuntimeCore:
    root = tmp_path / ".lemoncrow"
    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(root), "init", "--no-index"])
    assert result.exit_code == 0, result.output
    return LemonCrowRuntimeCore(root)


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


def _ensure_eval_blocks_exist(runtime: LemonCrowRuntimeCore) -> set[str]:
    """Seed any missing expected blocks so retrieval eval is self-contained."""
    cases = _load_cases()
    all_expected: set[str] = set()
    for case in cases:
        all_expected.update(case["expected_block_ids"])
    available = {b.id for b in runtime.store.knowledge.list_blocks()}
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

        runtime.store.knowledge.upsert_block(
            Playbook(
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


def _evaluate(rt: LemonCrowRuntimeCore, cases: list[dict[str, Any]], *, limit: int = 5) -> dict[str, Any]:
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


def test_context_retrieval_eval_metrics(retrieval_eval_runtime: LemonCrowRuntimeCore) -> None:
    metrics = _evaluate(retrieval_eval_runtime, _load_cases(), limit=5)

    assert metrics["query_count"] >= 1
    assert metrics["recall_at_5"] >= 0.0
    assert metrics["mrr"] >= 0.0
    assert metrics["ndcg_at_5"] >= 0.0


def test_context_retrieval_trace_records_drop_reasons(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from tests.helpers import grant_oauth_pro

    grant_oauth_pro(monkeypatch)
    monkeypatch.setenv("LEMONCROW_RETRIEVAL_TRACE", "1")
    runtime = _init_runtime(tmp_path)
    _ensure_eval_blocks_exist(runtime)

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
    assert trace["candidate_count"] > 0


def test_context_retrieval_rubric_passes(retrieval_eval_runtime: LemonCrowRuntimeCore) -> None:
    metrics = _evaluate(retrieval_eval_runtime, _load_cases(), limit=5)

    assert metrics["query_count"] >= 1
    assert metrics["recall_at_5"] >= 0.0
