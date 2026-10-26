"""Scoring for the real-PR benchmark: catch and noise against labels, and agreement with another reviewer.

Everything here is a pure function of its inputs, so the same findings, labels and thresholds always give the same
pairs and the same numbers.

**A finding catches a label** (:func:`match_labels`) by either of two rules, and the result says which one fired:

* ``location``: the finding is on the label's file and its changed region lies within the line tolerance of a line the
  fix edited. The finding's region is the lines its target changed, not only the line a comment would be posted on, so
  a finding about a function counts when the function holds the bug (Gate I's rule, and for the same reason).
* ``symbol``: the finding's text names an identifier the fix edited. This is for the bug that sits outside the lines the
  pull request changed (a caller left behind by a signature change, in this file or another): a reviewer can only
  comment on a changed line, so what ties the comment to the bug is the name.

Assignment is one to one and maximum: a finding catches at most one label and a label is caught once. A finding that
catches nothing is noise **only when the pull request is labelled**; on an unlabelled pull request nothing is known.

**Agreement with another reviewer** (:func:`compare_reviewers`) pairs findings with that reviewer's comments when they
are on the same file, within a line window, and their text is similar enough (:func:`similarity`). Proximity without
enough similarity is listed as a near miss for a person to settle, not counted either way. Agreement is *not* catch
rate: two reviewers can agree and both be wrong.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.matching import _assign
from lemoncrow.pro.capabilities.review.evaluation.real_prs import Label, MatchMode, Region

DEFAULT_LINE_WINDOW: Final[int] = 8
DEFAULT_SIMILARITY: Final[float] = 0.2
_MAX_TOKENS: Final[int] = 400


@dataclass(frozen=True, slots=True)
class Candidate:
    """One reviewer output to score: a finding of ours, or a comment of another reviewer's."""

    id: str
    path: str
    start_line: int
    end_line: int
    text: str
    severity: str = ""
    title: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "lines": [self.start_line, self.end_line],
            "severity": self.severity,
            "title": self.title,
        }


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #


def line_gap(start: int, end: int, other_start: int, other_end: int) -> int:
    """0 when the two inclusive ranges overlap, else the number of lines between them."""

    return max(0, other_start - end, start - other_end)


def _mentions(text: str, symbol: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol.lower())}(?![A-Za-z0-9_])", text.lower()))


def label_rule(
    label: Label,
    candidate: Candidate,
    *,
    mode: MatchMode,
    tolerance: int,
    pr_paths: Collection[str],
) -> tuple[str, int] | None:
    """``(rule, line gap)`` when *candidate* catches *label*, else ``None``. Location wins when both would."""

    allowed = label.line_tolerance if label.line_tolerance is not None else tolerance
    if mode in {"location", "either"}:
        best: int | None = None
        for region in label.regions:
            if region.path == candidate.path and candidate.start_line > 0:
                gap = line_gap(region.start_line, region.end_line, candidate.start_line, candidate.end_line)
                if gap <= allowed and (best is None or gap < best):
                    best = gap
        if best is not None:
            return "location", best
    if mode in {"symbol", "either"} and candidate.path in pr_paths:
        if any(_mentions(f"{candidate.title}\n{candidate.text}", symbol) for symbol in label.symbols):
            return "symbol", 0
    return None


@dataclass(frozen=True, slots=True)
class LabelMatch:
    label: Label
    candidate: Candidate
    rule: str
    gap: int

    def to_wire(self) -> dict[str, Any]:
        return {"label": self.label.id, "finding": self.candidate.id, "rule": self.rule, "line_gap": self.gap}


@dataclass(frozen=True, slots=True)
class LabelScore:
    matches: tuple[LabelMatch, ...]
    missed: tuple[Label, ...]
    unmatched: tuple[Candidate, ...]
    #: Same-file candidates that matched no label by either rule: where a person might disagree with the scoring.
    near_misses: tuple[tuple[str, str], ...] = ()
    #: Unmatched candidates that *would* catch a label another candidate already caught: the same bug reported twice
    #: (for example on two targets of one function). Noise to the reader, but not a wrong finding.
    duplicates: tuple[str, ...] = ()

    @property
    def caught(self) -> int:
        return len(self.matches)

    def to_wire(self) -> dict[str, Any]:
        return {
            "matches": [match.to_wire() for match in self.matches],
            "missed": [label.id for label in self.missed],
            "unmatched": [candidate.id for candidate in self.unmatched],
            "duplicates": list(self.duplicates),
            "near_misses": [{"label": label, "finding": finding} for label, finding in self.near_misses],
        }


def match_labels(
    labels: Sequence[Label],
    candidates: Sequence[Candidate],
    *,
    mode: MatchMode,
    tolerance: int,
    pr_paths: Collection[str],
) -> LabelScore:
    """One-to-one maximum assignment of candidates to labels, in a fixed order."""

    ordered = sorted(candidates, key=lambda item: (item.path, item.start_line, item.id))

    def options(label: Label) -> list[int]:
        rows: list[tuple[int, int, str, int]] = []
        for index, candidate in enumerate(ordered):
            rule = label_rule(label, candidate, mode=mode, tolerance=tolerance, pr_paths=pr_paths)
            if rule is not None:
                rows.append((0 if rule[0] == "location" else 1, rule[1], candidate.id, index))
        return [index for *_rest, index in sorted(rows)]

    pairs = _assign(list(labels), ordered, options)
    matches: list[LabelMatch] = []
    for label_index, found in sorted(pairs.items()):
        label = labels[label_index]
        rule = label_rule(label, ordered[found], mode=mode, tolerance=tolerance, pr_paths=pr_paths)
        assert rule is not None
        matches.append(LabelMatch(label, ordered[found], rule[0], rule[1]))
    used = set(pairs.values())
    missed = tuple(label for index, label in enumerate(labels) if index not in pairs)
    unmatched = tuple(candidate for index, candidate in enumerate(ordered) if index not in used)
    near = tuple(
        (label.id, candidate.id)
        for label in missed
        for candidate in unmatched
        if candidate.path in {region.path for region in label.regions}
    )
    duplicates = tuple(
        candidate.id
        for candidate in unmatched
        if any(
            label_rule(label, candidate, mode=mode, tolerance=tolerance, pr_paths=pr_paths) is not None
            for label in labels
        )
    )
    return LabelScore(tuple(matches), missed, unmatched, near, duplicates)


# --------------------------------------------------------------------------- #
# text similarity
# --------------------------------------------------------------------------- #

_STOPWORDS: Final[frozenset[str]] = frozenset("""
    the and for are but not you your this that with from have has had was were will would could should can may might
    into onto over under than then there here when where which while what who whom whose how why also just only very
    more most some any all each every both either neither such own same other another its it's their them they these
    those been being does did doing done use used uses using make makes made get gets got set sets new one two via per
    code change changes changed file files line lines function functions method methods class variable value values
    return returns returned call calls called pass passes passed check checks checked ensure ensures issue issues bug
    error errors fix fixes fixed missing causes cause caused possible potential likely currently now instead because
    suggestion suggested comment comments review reviewed note consider should would need needs needed
    """.split())
_URL = re.compile(r"https?://\S+")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def tokens(text: str) -> frozenset[str]:
    """Content words of *text*: lower-case, identifiers kept whole and split into parts, no stopwords or short words."""

    cleaned = _HTML_COMMENT.sub(" ", _URL.sub(" ", text))
    out: set[str] = set()
    for word in _WORD.findall(cleaned):
        whole = word.lower().strip("_")
        pieces = {piece.lower() for piece in _CAMEL.findall(word)} | {part for part in whole.split("_") if part}
        if (len(pieces) > 1 or "_" in word) and len(whole) >= 3:
            out.add(whole)
        out.update(piece for piece in pieces if len(piece) >= 3 and not piece.isdigit())
    return frozenset(sorted(token for token in out if token not in _STOPWORDS)[:_MAX_TOKENS])


def similarity(left: str, right: str) -> float:
    """Ochiai coefficient of the two texts' content words: ``|A & B| / sqrt(|A| * |B|)``, in ``[0, 1]``.

    It tolerates the length gap between a one-line finding and a long review comment better than Jaccard, and an empty
    side scores 0.
    """

    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


# --------------------------------------------------------------------------- #
# agreement with another reviewer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AgreementPair:
    finding: Candidate
    comment: Candidate
    similarity: float
    gap: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "finding": self.finding.id,
            "comment": self.comment.id,
            "similarity": round(self.similarity, 3),
            "line_gap": self.gap,
        }


@dataclass(frozen=True, slots=True)
class Agreement:
    agreed: tuple[AgreementPair, ...]
    ours_only: tuple[Candidate, ...]
    theirs_only: tuple[Candidate, ...]
    near_misses: tuple[AgreementPair, ...]
    line_window: int
    threshold: float

    def to_wire(self) -> dict[str, Any]:
        return {
            "line_window": self.line_window,
            "similarity_threshold": self.threshold,
            "agreed": [pair.to_wire() for pair in self.agreed],
            "ours_only": [item.to_wire() for item in self.ours_only],
            "theirs_only": [item.to_wire() for item in self.theirs_only],
            "near_misses": [pair.to_wire() for pair in self.near_misses],
        }


def compare_reviewers(
    ours: Sequence[Candidate],
    theirs: Sequence[Candidate],
    *,
    line_window: int = DEFAULT_LINE_WINDOW,
    threshold: float = DEFAULT_SIMILARITY,
) -> Agreement:
    """Pair our findings with their comments: same file, lines within *line_window*, similarity at least *threshold*."""

    ours_sorted = sorted(ours, key=lambda item: (item.path, item.start_line, item.id))
    theirs_sorted = sorted((item for item in theirs if item.start_line > 0), key=lambda i: (i.path, i.start_line, i.id))
    scored: dict[tuple[int, int], tuple[float, int]] = {}
    near: list[AgreementPair] = []
    for ti, comment in enumerate(theirs_sorted):
        for oi, finding in enumerate(ours_sorted):
            if finding.path != comment.path or finding.start_line <= 0:
                continue
            gap = line_gap(finding.start_line, finding.end_line, comment.start_line, comment.end_line)
            if gap > line_window:
                continue
            score = similarity(f"{finding.title}\n{finding.text}", comment.text)
            if score >= threshold:
                scored[(ti, oi)] = (score, gap)
            else:
                near.append(AgreementPair(finding, comment, score, gap))

    def options(comment_index: int) -> list[int]:
        rows = [
            (-score, gap, ours_sorted[oi].id, oi) for (ti, oi), (score, gap) in scored.items() if ti == comment_index
        ]
        return [oi for *_rest, oi in sorted(rows)]

    pairs = _assign(list(range(len(theirs_sorted))), ours_sorted, options)
    agreed = tuple(
        AgreementPair(ours_sorted[oi], theirs_sorted[ti], *scored[(ti, oi)]) for ti, oi in sorted(pairs.items())
    )
    used_ours = set(pairs.values())
    used_theirs = set(pairs)
    near_open = tuple(
        pair
        for pair in sorted(near, key=lambda p: (-p.similarity, p.gap, p.finding.id, p.comment.id))
        if ours_sorted.index(pair.finding) not in used_ours and theirs_sorted.index(pair.comment) not in used_theirs
    )
    return Agreement(
        agreed=agreed,
        ours_only=tuple(item for index, item in enumerate(ours_sorted) if index not in used_ours),
        theirs_only=tuple(item for index, item in enumerate(theirs_sorted) if index not in used_theirs),
        near_misses=near_open[:50],
        line_window=line_window,
        threshold=threshold,
    )


# --------------------------------------------------------------------------- #
# small statistics
# --------------------------------------------------------------------------- #


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """The Wilson score interval for a rate; ``None`` for no trials. With a few pull requests it is wide, on purpose."""

    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (``q`` in [0, 1]) of *values*; ``None`` for no values."""

    if not values:
        return None
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def regions_by_path(labels: Sequence[Label]) -> Mapping[str, tuple[Region, ...]]:
    out: dict[str, list[Region]] = {}
    for label in labels:
        for region in label.regions:
            out.setdefault(region.path, []).append(region)
    return {path: tuple(items) for path, items in out.items()}


__all__ = [
    "DEFAULT_LINE_WINDOW",
    "DEFAULT_SIMILARITY",
    "Agreement",
    "AgreementPair",
    "Candidate",
    "LabelMatch",
    "LabelScore",
    "compare_reviewers",
    "label_rule",
    "line_gap",
    "match_labels",
    "percentile",
    "regions_by_path",
    "similarity",
    "tokens",
    "wilson",
]
