from __future__ import annotations

from pathlib import Path

from benchmarks.codebench.eval_retrieval_oracle import (
    _load_cases,
    _payload_ranking,
    _ranked_details,
    _symbol_file_candidates,
)
from lemoncrow.pro.capabilities.code_context.models import SymbolRecord


def _symbol(symbol_id: str, file_path: str, score: float) -> SymbolRecord:
    return SymbolRecord(
        symbol_id=symbol_id,
        repo_id="repo",
        file_path=file_path,
        language="python",
        symbol_name=symbol_id,
        qualified_name=symbol_id,
        kind="function",
        signature=f"def {symbol_id}()",
        start_byte=0,
        end_byte=1,
        start_line=1,
        end_line=2,
        content_hash=f"hash-{symbol_id}",
        score=score,
        provenance="local",
    )


def test_symbol_file_candidates_keeps_best_ranked_symbol_per_file() -> None:
    candidates = _symbol_file_candidates(
        [
            _symbol("a1", "src/a.py", 0.9),
            _symbol("a2", "src/a.py", 0.8),
            _symbol("b", "src/b.py", 0.7),
        ],
        limit=10,
    )

    assert [candidate["path"] for candidate in candidates] == ["src/a.py", "src/b.py"]
    assert candidates[0]["symbol_id"] == "a1"
    assert candidates[0]["score"] == 0.9


def test_ranked_details_preserves_confidence_and_jsonifies_sets() -> None:
    candidates = _ranked_details(
        ["src/a.py"],
        {"src/a.py": {"confidence": 0.75, "tokens": {"alpha", "beta"}}},
    )

    assert candidates[0]["score"] == 0.75
    assert sorted(candidates[0]["tokens"]) == ["alpha", "beta"]


def test_payload_ranking_keeps_primary_then_deduped_recall_tails() -> None:
    ranking = _payload_ranking(
        {
            "files": [{"file_path": "src/a.py"}, {"path": "src/b.py"}],
            "fused_recall": ["src/b.py", "src/c.py"],
            "additional_relevant_files": ["src/d.py"],
            "deep_recall": ["src/c.py", "src/e.py"],
        }
    )

    assert ranking == ["src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"]


def test_load_cases_samples_across_repositories(tmp_path: Path) -> None:
    gold = tmp_path / "gold.json"
    gold.write_text(
        """
        {
          "gold_kind": "definition",
          "pairs": [
            ["q2", "t2", "repo-b"],
            ["q1", "t1", "repo-a"],
            ["q3", "t3", "repo-a"]
          ],
          "true_map": {
            "t1": ["a.py"],
            "t2": ["b.py"],
            "t3": ["c.py"]
          },
          "repos": {
            "repo-a": {"ws": "/a"},
            "repo-b": {"ws": "/b"}
          }
        }
        """,
        encoding="utf-8",
    )

    cases, repos = _load_cases([gold], repo_filter="", sample=2)

    assert len(cases) == 2
    assert {case["repo"] for case in cases} == {"repo-a", "repo-b"}
    assert set(repos) == {"repo-a", "repo-b"}
