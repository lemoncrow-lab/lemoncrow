from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = ROOT / "experiments" / "retrieval_symbol_vote"
MODULE_PATH = EXPERIMENT_DIR / "eval_frozen_codebase_reranker.py"
sys.path.insert(0, str(EXPERIMENT_DIR))
SPEC = importlib.util.spec_from_file_location("eval_frozen_codebase_reranker", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_reranked_indices_preserve_original_when_top_is_unchanged() -> None:
    assert MODULE._reranked_indices([3.0, 2.0, 1.0], blend=1.0, margin=0.0) == [0, 1, 2]


def test_reranked_indices_apply_margin_gate() -> None:
    assert MODULE._reranked_indices([1.0, 1.1, 0.0], blend=1.0, margin=0.2) == [0, 1, 2]
    assert MODULE._reranked_indices([1.0, 1.3, 0.0], blend=1.0, margin=0.2) == [1, 0, 2]


def test_metrics_count_missing_as_zero_reciprocal_rank() -> None:
    metrics = MODULE._metrics([1, 2, None, 4])
    assert metrics["n"] == 4
    assert metrics["mrr"] == (1.0 + 0.5 + 0.0 + 0.25) / 4
    assert metrics["recall"] == 0.75


def test_repository_url_supports_generic_and_lemoncrow_prefixes() -> None:
    assert MODULE._repository_url("django__django") == "https://github.com/django/django.git"
    assert Path(MODULE._repository_url("lemoncrow__lemoncrow")).resolve() == ROOT.resolve()


def test_failure_result_preserves_denominator_metadata() -> None:
    probe = MODULE.GoldProbe(
        case_id="semantic:repo:task-1",
        gold_kind="semantic",
        repo="repo",
        query="find retries",
        gold_paths=("src/retry.py",),
    )
    result = MODULE._result_for_probe(probe, "timeout")
    assert result["gold_kind"] == "semantic"
    assert result["repo"] == "repo"
    assert result["status"] == "timeout"


def test_gold_loader_reads_pairs_without_mutating_them(tmp_path: Path) -> None:
    path = tmp_path / "heldout.json"
    path.write_text(
        json.dumps(
            {
                "gold_kind": "semantic",
                "pairs": [["find retries", "task-1", "repo"]],
                "true_map": {"task-1": ["src/retry.py"]},
                "repos": {"repo": {"ws": "/tmp/repo", "db": "/tmp/index.sqlite"}},
            }
        ),
        encoding="utf-8",
    )
    probes, repositories = MODULE._load_gold([path])
    assert probes[0].query == "find retries"
    assert probes[0].gold_paths == ("src/retry.py",)
    assert repositories["repo"]["db"] == "/tmp/index.sqlite"
