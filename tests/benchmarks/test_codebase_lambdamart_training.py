from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = ROOT / "experiments" / "retrieval_symbol_vote"
MODULE_PATH = EXPERIMENT_DIR / "train_codebase_lambdamart.py"
sys.path.insert(0, str(EXPERIMENT_DIR))
SPEC = importlib.util.spec_from_file_location("train_codebase_lambdamart", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_target_family_groups_test_and_source_files() -> None:
    assert MODULE._target_family("src/lemoncrow/code_context/engine.py") == "engine"
    assert MODULE._target_family("tests/code_context/test_engine.py") == "engine"
    assert MODULE._target_family("frontend/foo/widget.spec.ts") == "widget"


def test_select_variants_spreads_across_available_queries() -> None:
    values = [f"query-{index}" for index in range(9)]
    selected = MODULE._select_variants(values, 3)
    assert selected == ["query-0", "query-4", "query-8"]


def test_training_manifest_rejects_gold_named_input(tmp_path: Path) -> None:
    path = tmp_path / "benchmark-gold.json"
    path.write_text('{"repositories": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="must not be a benchmark gold file"):
        MODULE._load_manifest(path)


def test_query_key_is_case_insensitive_and_stable() -> None:
    assert MODULE._query_key("Find Retry Policy") == MODULE._query_key(" find retry policy ")


def test_excluded_path_rejects_benchmark_gold_payloads() -> None:
    assert MODULE._EXCLUDED_PATH_RE.search("benchmarks/codebench/data/bench_pairs_def_gold.json")
    assert not MODULE._EXCLUDED_PATH_RE.search("benchmarks/codebench/eval_retrieval_oracle.py")


def test_intent_policy_enables_only_validation_safe_intents() -> None:
    labels = [
        {"labels": [1, 0]},  # definition baseline is already correct
        {"labels": [0, 1]},  # prose needs reranking
    ]
    scores = [
        [0.0, 1.0],  # reranking definition would regress rank 1 -> rank 2
        [0.0, 1.0],  # reranking prose improves rank 2 -> rank 1
    ]
    metadata = [{"intent": "definition"}, {"intent": "prose"}]

    enabled, baseline, learned, report = MODULE._choose_intent_policy(
        labels,
        scores,
        metadata,
        blend=1.0,
        margin=0.0,
    )

    assert enabled == ["prose"]
    assert baseline == [1, 2]
    assert learned == [1, 1]
    assert report["learned"]["mrr"] > report["baseline"]["mrr"]
    assert report["per_intent"]["definition"]["enabled"] is False
    assert report["per_intent"]["prose"]["enabled"] is True
