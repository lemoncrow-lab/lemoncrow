"""A pull request with more files than ``max_files``: review the highest-risk ones first, and say what was left.

Never a skip. The files that do not fit this run are *pending*, not reviewed: they stay out of the reviewed-unit list,
so the next push, or a re-run of the job, competes for the same limit again with what is left, highest risk first,
until every file has been reviewed. The summary names how many were left and which, the headline reads ``Partially
reviewed`` and never ``No must-fix findings``, and the commit status says the same.

Risk is the review's own, in this order of authority: a path the repository marked critical or high in its
``review.focus`` policy; the score of the strongest deterministic risk signal in the file (security 100, persistence 95,
API and concurrency 90 ...; a rule that applies is 100); then the order the Review's canonical queue gave the file.
A file with no signal comes last. Nothing here is a model's opinion.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: What a path's policy criticality is worth, as a floor under its signal score: a file the repository marked
#: ``critical`` is never left behind a file with a marker, however its own code reads.
CRITICALITY_FLOOR: Final[Mapping[str, int]] = {"critical": 100, "high": 85}
#: Deferred paths named in the summary; the count says the rest.
MAX_NAMED: Final[int] = 12


@dataclass(frozen=True, slots=True)
class LargePlan:
    """Which targets this run reviews, which it leaves for the next, and how many files the change has."""

    limit: int
    total_files: int
    kept: tuple[Any, ...]
    deferred: tuple[Any, ...]
    #: The deferred paths, highest risk first (the order the next run takes them in).
    deferred_paths: tuple[str, ...]

    @property
    def deferred_ids(self) -> frozenset[str]:
        return frozenset(target.target_id for target in self.deferred)


def file_scores(
    targets: Sequence[Any], candidates: Sequence[Any], criticality: Callable[[str], str] | None = None
) -> dict[str, int]:
    """``{path: score}``: the strongest risk signal in each file, raised to its policy criticality's floor."""

    by_target: dict[str, int] = {}
    for candidate in candidates:
        for target_id in candidate.target_ids:
            by_target[target_id] = max(by_target.get(target_id, 0), int(candidate.score))
    scores: dict[str, int] = {}
    for target in targets:
        score = by_target.get(target.target_id, 0)
        if criticality is not None:
            score = max(score, CRITICALITY_FLOOR.get(criticality(target.path), 0))
        scores[target.path] = max(scores.get(target.path, 0), score)
    return scores


def plan_large(
    changed: Sequence[Any],
    *,
    limit: int,
    total_files: int,
    candidates: Sequence[Any] = (),
    criticality: Callable[[str], str] | None = None,
    order: Mapping[str, int] | None = None,
) -> LargePlan | None:
    """The plan for *changed* (the targets this run would review), or ``None`` when no cut is needed.

    *limit* ``0`` is no limit. *order* maps a target id to its place in the Review's canonical queue (earlier first),
    the tie-break between files of equal score; without it the order of *changed* is used. *total_files* is the
    number of files in the whole change (what the summary says), not only in *changed*.
    """

    paths = list(dict.fromkeys(target.path for target in changed))
    if limit <= 0 or len(paths) <= limit:
        return None
    place = order or {}
    scores = file_scores(changed, candidates, criticality)
    first: dict[str, int] = {}
    for index, target in enumerate(changed):
        first.setdefault(target.path, place.get(target.target_id, index))
    ranked = sorted(paths, key=lambda path: (-scores.get(path, 0), first[path], path))
    keep = set(ranked[:limit])
    return LargePlan(
        limit=limit,
        total_files=max(total_files, len(paths)),
        kept=tuple(target for target in changed if target.path in keep),
        deferred=tuple(target for target in changed if target.path not in keep),
        deferred_paths=tuple(ranked[limit:]),
    )


__all__ = ["CRITICALITY_FLOOR", "MAX_NAMED", "LargePlan", "file_scores", "plan_large"]
