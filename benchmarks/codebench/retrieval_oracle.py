"""Candidate-oracle metrics for multi-channel code retrieval.

The production retrieval pipeline has several independent recall channels.  This
module evaluates them *before* learned reranking so an experiment can distinguish
candidate-generation failures from ranking failures.

The module is intentionally dependency-light and deterministic.  It accepts a
JSONL trace format, emits per-case scores plus an aggregate report, and is also
importable by training/evaluation harnesses.

Trace example::

    {
      "case_id": "django-001",
      "query": "database timezone override",
      "repo": "django__django",
      "gold_files": ["django/db/backends/mysql/operations.py"],
      "channels": {
        "lexical": [{"path": "...", "score": 12.3}],
        "semantic": ["django/db/backends/mysql/operations.py"]
      },
      "actual_ranking": ["django/db/backends/base/operations.py", "..."]
    }

Paths are compared after slash normalization.  Exact relative-path equality is
preferred; suffix matching is supported for harnesses that report absolute paths
while gold data stores repository-relative paths.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_CUTOFFS = (16, 32, 64, 100)


def normalize_path(value: str) -> str:
    """Normalize a candidate/gold path without resolving it on the host."""

    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    # PurePosixPath collapses duplicate separators and harmless ``.`` segments.
    # Do not call resolve(): traces can refer to repositories absent on this host.
    if not normalized:
        return ""
    return str(PurePosixPath(normalized))


def paths_match(candidate: str, gold: str) -> bool:
    """Return whether two trace paths identify the same repository file."""

    left = normalize_path(candidate)
    right = normalize_path(gold)
    if not left or not right:
        return False
    if left == right:
        return True
    # Boundary-aware suffix matching avoids treating ``foo.py`` as a suffix of
    # ``notfoo.py`` while still matching absolute-path versus repo-relative data.
    return left.endswith(f"/{right}") or right.endswith(f"/{left}")


@dataclass(frozen=True)
class RankedCandidate:
    """One candidate emitted by one retrieval channel."""

    path: str
    score: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Any) -> RankedCandidate:
        if isinstance(raw, str):
            return cls(path=normalize_path(raw))
        if not isinstance(raw, Mapping):
            raise TypeError(f"candidate must be a path string or mapping, got {type(raw).__name__}")
        path = normalize_path(str(raw.get("path") or raw.get("file_path") or ""))
        score_raw = raw.get("score")
        score = float(score_raw) if score_raw is not None else None
        details = {str(key): value for key, value in raw.items() if key not in {"path", "file_path", "score"}}
        return cls(path=path, score=score, details=details)


@dataclass(frozen=True)
class ChannelTrace:
    """A channel's ranked output, deduplicated by normalized path."""

    name: str
    candidates: tuple[RankedCandidate, ...]

    @classmethod
    def from_raw(cls, name: str, raw_candidates: Sequence[Any]) -> ChannelTrace:
        seen: set[str] = set()
        candidates: list[RankedCandidate] = []
        for raw in raw_candidates:
            candidate = RankedCandidate.from_raw(raw)
            if not candidate.path or candidate.path in seen:
                continue
            seen.add(candidate.path)
            candidates.append(candidate)
        return cls(name=name, candidates=tuple(candidates))

    def paths(self, *, limit: int | None = None) -> list[str]:
        values = [candidate.path for candidate in self.candidates]
        return values if limit is None else values[: max(0, limit)]


@dataclass(frozen=True)
class RetrievalTrace:
    """One labelled retrieval query and all candidate-stage evidence."""

    case_id: str
    query: str
    repo: str
    gold_files: tuple[str, ...]
    channels: tuple[ChannelTrace, ...]
    actual_ranking: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> RetrievalTrace:
        raw_channels = raw.get("channels") or {}
        if not isinstance(raw_channels, Mapping):
            raise TypeError("trace.channels must be a mapping of channel name to ranked candidates")
        channels: list[ChannelTrace] = []
        for name, candidates in raw_channels.items():
            if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
                raise TypeError(f"trace channel {name!r} must contain a candidate sequence")
            channels.append(ChannelTrace.from_raw(str(name), candidates))

        gold_raw = raw.get("gold_files") or raw.get("golds") or ()
        if not isinstance(gold_raw, Sequence) or isinstance(gold_raw, (str, bytes)):
            raise TypeError("trace.gold_files must be a sequence")
        gold_files = tuple(path for value in gold_raw if (path := normalize_path(str(value))))
        if not gold_files:
            raise ValueError("trace must contain at least one non-empty gold file")

        actual_raw = raw.get("actual_ranking") or raw.get("ranking") or ()
        if not isinstance(actual_raw, Sequence) or isinstance(actual_raw, (str, bytes)):
            raise TypeError("trace.actual_ranking must be a sequence")
        actual = tuple(_dedupe_paths(_coerce_path(value) for value in actual_raw))

        known = {
            "case_id",
            "query",
            "repo",
            "repo_prefix",
            "gold_files",
            "golds",
            "channels",
            "actual_ranking",
            "ranking",
            "metadata",
        }
        metadata = dict(raw.get("metadata") or {})
        metadata.update({str(key): value for key, value in raw.items() if key not in known})
        return cls(
            case_id=str(raw.get("case_id") or raw.get("id") or ""),
            query=str(raw.get("query") or ""),
            repo=str(raw.get("repo") or raw.get("repo_prefix") or ""),
            gold_files=gold_files,
            channels=tuple(channels),
            actual_ranking=actual,
            metadata=metadata,
        )


def _coerce_path(raw: Any) -> str:
    if isinstance(raw, str):
        return normalize_path(raw)
    if isinstance(raw, Mapping):
        return normalize_path(str(raw.get("path") or raw.get("file_path") or ""))
    return ""


def _dedupe_paths(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for path in paths:
        normalized = normalize_path(path)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def first_gold_rank(ranking: Sequence[str], gold_files: Sequence[str], *, limit: int | None = None) -> int | None:
    """Return the 1-based rank of the first matching gold file."""

    golds = [normalize_path(path) for path in gold_files if normalize_path(path)]
    bounded = ranking if limit is None else ranking[: max(0, limit)]
    seen: set[str] = set()
    rank = 0
    for raw_path in bounded:
        path = normalize_path(raw_path)
        if not path or path in seen:
            continue
        seen.add(path)
        rank += 1
        if any(paths_match(path, gold) for gold in golds):
            return rank
    return None


def candidate_union(channels: Sequence[ChannelTrace], *, per_channel_limit: int | None = None) -> list[str]:
    """Build a deterministic, first-seen union in supplied channel order."""

    return _dedupe_paths(path for channel in channels for path in channel.paths(limit=per_channel_limit))


@dataclass(frozen=True)
class QueryOracleScore:
    case_id: str
    query: str
    repo: str
    gold_files: tuple[str, ...]
    channel_ranks: Mapping[str, int | None]
    union_rank: int | None
    actual_rank: int | None
    actual_rr: float
    candidate_recall_at: Mapping[int, float]
    oracle_rr_at: Mapping[int, float]
    unique_rescue_channels: tuple[str, ...]
    failure_class: str
    union_size: int


def score_trace(trace: RetrievalTrace, *, cutoffs: Sequence[int] = DEFAULT_CUTOFFS) -> QueryOracleScore:
    """Score one trace and classify its first failing retrieval stage."""

    resolved_cutoffs = tuple(sorted({int(value) for value in cutoffs if int(value) > 0}))
    if not resolved_cutoffs:
        raise ValueError("at least one positive cutoff is required")

    channel_ranks = {channel.name: first_gold_rank(channel.paths(), trace.gold_files) for channel in trace.channels}
    union = candidate_union(trace.channels)
    union_rank = first_gold_rank(union, trace.gold_files)
    actual_rank = first_gold_rank(trace.actual_ranking, trace.gold_files)
    actual_rr = 1.0 / actual_rank if actual_rank else 0.0

    candidate_recall = {
        cutoff: float(first_gold_rank(union, trace.gold_files, limit=cutoff) is not None) for cutoff in resolved_cutoffs
    }
    # With a perfect reranker, any gold present in the candidate set moves to
    # rank one.  Oracle reciprocal rank at a cutoff is therefore identical to
    # candidate recall at that cutoff; retaining both names makes reports clear.
    oracle_rr = dict(candidate_recall)

    rescuers = tuple(name for name, rank in channel_ranks.items() if rank is not None)
    unique_rescue = rescuers if len(rescuers) == 1 else ()
    if union_rank is None:
        failure_class = "candidate_miss"
    elif actual_rank == 1:
        failure_class = "hit_at_1"
    else:
        failure_class = "ranking_miss"

    return QueryOracleScore(
        case_id=trace.case_id,
        query=trace.query,
        repo=trace.repo,
        gold_files=trace.gold_files,
        channel_ranks=channel_ranks,
        union_rank=union_rank,
        actual_rank=actual_rank,
        actual_rr=actual_rr,
        candidate_recall_at=candidate_recall,
        oracle_rr_at=oracle_rr,
        unique_rescue_channels=unique_rescue,
        failure_class=failure_class,
        union_size=len(union),
    )


@dataclass(frozen=True)
class AggregateOracleScore:
    cases: int
    actual_mrr: float
    actual_hit1: float
    candidate_recall_at: Mapping[int, float]
    oracle_mrr_at: Mapping[int, float]
    failure_counts: Mapping[str, int]
    unique_rescues: Mapping[str, int]
    per_channel: Mapping[str, Mapping[str, float | int]]


def aggregate_scores(scores: Sequence[QueryOracleScore]) -> AggregateOracleScore:
    """Macro-average oracle diagnostics across query traces."""

    case_count = len(scores)
    if case_count == 0:
        return AggregateOracleScore(
            cases=0,
            actual_mrr=0.0,
            actual_hit1=0.0,
            candidate_recall_at={},
            oracle_mrr_at={},
            failure_counts={},
            unique_rescues={},
            per_channel={},
        )

    cutoffs = sorted({cutoff for score in scores for cutoff in score.candidate_recall_at})
    candidate_recall = {
        cutoff: sum(score.candidate_recall_at.get(cutoff, 0.0) for score in scores) / case_count for cutoff in cutoffs
    }
    oracle_mrr = {
        cutoff: sum(score.oracle_rr_at.get(cutoff, 0.0) for score in scores) / case_count for cutoff in cutoffs
    }

    failure_counts = Counter(score.failure_class for score in scores)
    unique_rescues = Counter(channel for score in scores for channel in score.unique_rescue_channels)

    channel_names = sorted({name for score in scores for name in score.channel_ranks})
    per_channel: dict[str, Mapping[str, float | int]] = {}
    for name in channel_names:
        ranks = [score.channel_ranks.get(name) for score in scores]
        reciprocal = [1.0 / rank if rank else 0.0 for rank in ranks]
        per_channel[name] = {
            "mrr": sum(reciprocal) / case_count,
            "hit1": sum(rank == 1 for rank in ranks) / case_count,
            "recall": sum(rank is not None for rank in ranks) / case_count,
            "unique_rescues": unique_rescues.get(name, 0),
        }

    return AggregateOracleScore(
        cases=case_count,
        actual_mrr=sum(score.actual_rr for score in scores) / case_count,
        actual_hit1=sum(score.actual_rank == 1 for score in scores) / case_count,
        candidate_recall_at=candidate_recall,
        oracle_mrr_at=oracle_mrr,
        failure_counts=dict(sorted(failure_counts.items())),
        unique_rescues=dict(sorted(unique_rescues.items())),
        per_channel=per_channel,
    )


def _load_traces(path: Path) -> list[RetrievalTrace]:
    traces: list[RetrievalTrace] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSONL at {path}:{line_number}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError(f"trace at {path}:{line_number} must be a JSON object")
        traces.append(RetrievalTrace.from_raw(raw))
    return traces


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score multi-channel retrieval candidate traces")
    parser.add_argument("--input", type=Path, required=True, help="JSONL retrieval trace file")
    parser.add_argument("--out", type=Path, required=True, help="Aggregate JSON output")
    parser.add_argument("--per-case", type=Path, default=None, help="Optional per-query score JSONL output")
    parser.add_argument(
        "--cutoffs",
        default=",".join(str(value) for value in DEFAULT_CUTOFFS),
        help="Comma-separated candidate cutoffs (default: 16,32,64,100)",
    )
    args = parser.parse_args(argv)

    cutoffs = tuple(int(value.strip()) for value in args.cutoffs.split(",") if value.strip())
    traces = _load_traces(args.input)
    scores = [score_trace(trace, cutoffs=cutoffs) for trace in traces]
    aggregate = aggregate_scores(scores)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(_jsonable(aggregate), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.per_case is not None:
        args.per_case.parent.mkdir(parents=True, exist_ok=True)
        with args.per_case.open("w", encoding="utf-8") as handle:
            for score in scores:
                handle.write(json.dumps(_jsonable(score), sort_keys=True) + "\n")

    print(json.dumps(_jsonable(aggregate), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
