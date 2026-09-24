from __future__ import annotations

import json
from pathlib import Path

from benchmarks.codebench.retrieval_oracle import (
    ChannelTrace,
    RetrievalTrace,
    aggregate_scores,
    candidate_union,
    first_gold_rank,
    main,
    normalize_path,
    paths_match,
    score_trace,
)


def test_path_normalization_and_boundary_aware_suffix_matching() -> None:
    assert normalize_path(r".\src\\api\client.py") == "src/api/client.py"
    assert paths_match("/tmp/repo/src/api/client.py", "src/api/client.py")
    assert not paths_match("src/api/notclient.py", "client.py")


def test_channel_trace_deduplicates_preserving_first_score() -> None:
    channel = ChannelTrace.from_raw(
        "semantic",
        [
            {"path": "src/a.py", "score": 0.9, "symbol_id": "a"},
            {"file_path": "./src/a.py", "score": 0.2, "symbol_id": "other"},
            "src/b.py",
        ],
    )

    assert channel.paths() == ["src/a.py", "src/b.py"]
    assert channel.candidates[0].score == 0.9
    assert channel.candidates[0].details == {"symbol_id": "a"}


def test_candidate_union_is_deterministic_and_deduplicated() -> None:
    channels = [
        ChannelTrace.from_raw("lexical", ["src/a.py", "src/b.py"]),
        ChannelTrace.from_raw("semantic", ["src/b.py", "src/c.py"]),
    ]

    assert candidate_union(channels) == ["src/a.py", "src/b.py", "src/c.py"]
    assert candidate_union(channels, per_channel_limit=1) == ["src/a.py", "src/b.py"]


def test_first_gold_rank_deduplicates_and_honors_limit() -> None:
    ranking = ["src/a.py", "./src/a.py", "/repo/src/gold.py"]

    assert first_gold_rank(ranking, ["src/gold.py"]) == 2
    assert first_gold_rank(ranking, ["src/gold.py"], limit=2) is None


def test_score_trace_separates_candidate_and_ranking_failures() -> None:
    trace = RetrievalTrace.from_raw(
        {
            "case_id": "case-1",
            "query": "find behavior",
            "repo": "repo",
            "gold_files": ["src/gold.py"],
            "channels": {
                "lexical": ["src/wrong.py"],
                "semantic": ["src/gold.py"],
            },
            "actual_ranking": ["src/wrong.py", "src/gold.py"],
        }
    )

    score = score_trace(trace, cutoffs=(1, 2, 4))

    assert score.channel_ranks == {"lexical": None, "semantic": 1}
    assert score.union_rank == 2
    assert score.actual_rank == 2
    assert score.actual_rr == 0.5
    assert score.candidate_recall_at == {1: 0.0, 2: 1.0, 4: 1.0}
    assert score.oracle_rr_at == score.candidate_recall_at
    assert score.unique_rescue_channels == ("semantic",)
    assert score.failure_class == "ranking_miss"

    miss = score_trace(
        RetrievalTrace.from_raw(
            {
                "case_id": "case-2",
                "query": "missing",
                "gold_files": ["src/gold.py"],
                "channels": {"lexical": ["src/wrong.py"]},
                "actual_ranking": ["src/wrong.py"],
            }
        ),
        cutoffs=(4,),
    )
    assert miss.failure_class == "candidate_miss"


def test_aggregate_scores_reports_oracle_and_unique_rescues() -> None:
    traces = [
        RetrievalTrace.from_raw(
            {
                "case_id": "a",
                "query": "a",
                "gold_files": ["a.py"],
                "channels": {"lexical": ["a.py"], "semantic": ["x.py"]},
                "actual_ranking": ["a.py"],
            }
        ),
        RetrievalTrace.from_raw(
            {
                "case_id": "b",
                "query": "b",
                "gold_files": ["b.py"],
                "channels": {"lexical": ["x.py"], "semantic": ["b.py"]},
                "actual_ranking": ["x.py", "b.py"],
            }
        ),
        RetrievalTrace.from_raw(
            {
                "case_id": "c",
                "query": "c",
                "gold_files": ["c.py"],
                "channels": {"lexical": ["x.py"], "semantic": ["y.py"]},
                "actual_ranking": ["x.py"],
            }
        ),
    ]

    aggregate = aggregate_scores([score_trace(trace, cutoffs=(1, 4)) for trace in traces])

    assert aggregate.cases == 3
    assert aggregate.actual_mrr == 0.5
    assert aggregate.actual_hit1 == 1 / 3
    assert aggregate.candidate_recall_at == {1: 1 / 3, 4: 2 / 3}
    assert aggregate.oracle_mrr_at == aggregate.candidate_recall_at
    assert aggregate.failure_counts == {"candidate_miss": 1, "hit_at_1": 1, "ranking_miss": 1}
    assert aggregate.unique_rescues == {"lexical": 1, "semantic": 1}
    assert aggregate.per_channel["semantic"]["mrr"] == 1 / 3


def test_cli_writes_aggregate_and_per_case(tmp_path: Path) -> None:
    input_path = tmp_path / "traces.jsonl"
    output_path = tmp_path / "aggregate.json"
    per_case_path = tmp_path / "cases.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "case_id": "case",
                "query": "find gold",
                "gold_files": ["src/gold.py"],
                "channels": {"lexical": ["src/gold.py"]},
                "actual_ranking": ["src/gold.py"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--input",
                str(input_path),
                "--out",
                str(output_path),
                "--per-case",
                str(per_case_path),
                "--cutoffs",
                "1,8",
            ]
        )
        == 0
    )

    aggregate = json.loads(output_path.read_text(encoding="utf-8"))
    assert aggregate["cases"] == 1
    assert aggregate["actual_mrr"] == 1.0
    assert aggregate["candidate_recall_at"] == {"1": 1.0, "8": 1.0}
    cases = [json.loads(line) for line in per_case_path.read_text(encoding="utf-8").splitlines()]
    assert cases[0]["failure_class"] == "hit_at_1"
