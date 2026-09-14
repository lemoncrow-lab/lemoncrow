"""Review reading order: which file a human should open first, and why.

Why this module exists: a diff lists files in whatever order the VCS walked
them, which is alphabetical at best and blast-radius-blind at worst. The
expensive part of a review is deciding where to spend attention, so that
decision is made once, here, from signals the packet already carries.

Three properties are the contract, and each one rules out an obvious
alternative implementation:

* **Pure.** Every input is a packet field; there is no I/O, no clock, no index
  lookup, no environment read. That is what lets the ranking be unit-tested
  against hand-built dataclasses in milliseconds, and what lets it run
  unchanged when the semantic-impact producer is disabled and hands it empty
  tuples.
* **Deterministic.** The tie-break ``(-score, category_rank, path)`` is a total
  order over distinct paths, so identical input yields a byte-identical order.
  Scores are rounded to 4dp *before* sorting, so the number a reader sees is
  the number that produced the position they see it in.
* **Explainable.** Every signal that moves a file up emits the human sentence
  that justifies it. A rank with no stated reason is a rank a reviewer cannot
  argue with, which is worse than no rank at all — so the only signals allowed
  to stay silent are the two structural priors (production, docs/config) that
  never promote a file past the point where it needs defending.

Weights are fixed constants, not tuning knobs. A configurable weight would make
two runs of the same command disagree, which defeats the whole point.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import (
    FILE_CATEGORY_ORDER,
    ChangedFile,
    ChangedSymbol,
    FileCategory,
    ImpactSite,
    ReviewOrderEntry,
)

# --- Fixed weights (spec §3 PR-3). Deliberately not configurable. -------------

_UNTOUCHED_SITE_WEIGHT = 3.0
_UNTOUCHED_SITE_CAP = 9.0

# A site the detector qualified rather than proved. It is worth a look, so it must
# move the file; it is not proof, so it must not move it as far as one that is.
_UNVERIFIED_SITE_WEIGHT = 1.0
_UNVERIFIED_SITE_CAP = 3.0

_CENTRALITY_WEIGHT = 4.0
_CENTRALITY_THRESHOLD = 0.9

_CALLER_WEIGHT = 0.5
_CALLER_CAP = 3.0

_CONTRACT_WEIGHT = 3.0
# The two impact kinds that mean "a caller outside this diff can now break".
_CONTRACT_KINDS = frozenset({"removed_symbol", "signature_change"})

_SIZE_DIVISOR = 200.0
_SIZE_CAP = 2.0

# Brand-new production modules have no historical callers by definition. Without
# one bounded fallback, a substantial new implementation can rank below reused
# test helpers simply because those helpers already exist in the code graph.
_NEW_PRODUCTION_WEIGHT = 2.5
_NEW_PRODUCTION_MIN_CHURN = 200
_NEW_PRODUCTION_REASON = "substantial new production surface"

_DELETED_WEIGHT = 1.0

_UNINSPECTED_WEIGHT = 2.0
_UNINSPECTED_REASON = "not inspected by the generating agent"

# Structural prior per category. "production" and docs/config move the score
# without emitting a reason: neither is a finding, and neither on its own can
# push a file above the threshold where a reader would demand a justification.
_CATEGORY_WEIGHT: dict[str, float] = {
    "production": 2.0,
    "test": 0.5,
    "generated": -5.0,
    "vendor": -5.0,
    "docs": -1.0,
    "config": -1.0,
}
_CATEGORY_REASON: dict[str, str] = {
    "generated": "generated/vendor — skim",
    "vendor": "generated/vendor — skim",
}
_CATEGORY_RANK: dict[str, int] = {name: index for index, name in enumerate(FILE_CATEGORY_ORDER)}
_UNRANKED_CATEGORY = len(FILE_CATEGORY_ORDER)


def _normalise(path: str) -> str:
    """Return *path* with Windows separators folded so every match is one shape."""

    return path.replace("\\", "/")


def _site_file(site_path: str) -> str:
    """Return the file part of an ``ImpactSite.path`` (``"rel/p.py:L12"`` → ``"rel/p.py"``).

    The suffix is only stripped when it really is ``:L<digits>``; a path that
    legitimately contains a colon is returned untouched rather than truncated.
    """

    head, separator, tail = _normalise(site_path).rpartition(":L")
    if separator and head and tail.isdigit():
        return head
    return _normalise(site_path)


def _owning_file(site: ImpactSite) -> str:
    """The changed file an impact site should be scored against.

    Not the file the site *is in*: an out-of-patch site lives, by definition, in
    a file this diff does not contain, so scoring it there credits nothing and
    the two strongest signals in the table -- "reaches outside the patch" and
    "public contract changed" -- silently never fire. ``source_path`` names the
    changed file that caused it; the site's own file is the fallback for an
    in-patch site and for a producer that could not attribute it.
    """

    return _normalise(site.source_path) if site.source_path else _site_file(site.path)


def _inspected_index(inspected: frozenset[str]) -> frozenset[str]:
    """Expand recorded read paths into every trailing path suffix they contain.

    ``ProvenanceRecord.files_inspected`` holds host-absolute paths while a
    ``ChangedFile.path`` is repo-relative, so equality never matches. Indexing
    the suffixes makes the lookup O(1) per file and biases the one direction
    that is safe: a basename collision makes a file look *inspected*, which
    withholds the "not inspected" bonus. Over-claiming that an agent skipped a
    file is the error worth avoiding.
    """

    suffixes: set[str] = set()
    for raw in inspected:
        normalised = _normalise(raw).strip()
        if not normalised:
            continue
        suffixes.add(normalised)
        segments = normalised.split("/")
        for start in range(1, len(segments)):
            suffix = "/".join(segments[start:])
            if suffix:
                suffixes.add(suffix)
    return frozenset(suffixes)


def _group_by_file(
    symbols: Sequence[ChangedSymbol],
    impact: Sequence[ImpactSite],
) -> tuple[dict[str, list[ChangedSymbol]], dict[str, list[ImpactSite]]]:
    """Bucket the two analysis outputs by normalised file path in one pass each."""

    by_symbol: dict[str, list[ChangedSymbol]] = {}
    for symbol in symbols:
        by_symbol.setdefault(_normalise(symbol.file_path), []).append(symbol)
    by_site: dict[str, list[ImpactSite]] = {}
    for site in impact:
        by_site.setdefault(_owning_file(site), []).append(site)
    return by_symbol, by_site


def _category_score(category: str) -> tuple[float, list[str]]:
    weight = _CATEGORY_WEIGHT.get(category, 0.0)
    reason = _CATEGORY_REASON.get(category)
    return weight, [reason] if reason else []


def _symbol_score(symbols: Sequence[ChangedSymbol]) -> tuple[float, list[str]]:
    """Score the centrality and caller-count signals PR-2 attaches to symbols.

    Both degrade to zero rather than to a guess: ``centrality_percentile`` is
    ``None`` and ``caller_count`` is ``-1`` when no index answered, and neither
    sentinel can accidentally clear its threshold.
    """

    total = 0.0
    reasons: list[str] = []

    central = any(
        symbol.centrality_percentile is not None and symbol.centrality_percentile >= _CENTRALITY_THRESHOLD
        for symbol in symbols
    )
    if central:
        total += _CENTRALITY_WEIGHT
        reasons.append("high-centrality symbol")

    callers = max((symbol.caller_count for symbol in symbols), default=0)
    if callers > 0:
        total += min(_CALLER_CAP, _CALLER_WEIGHT * callers)
        reasons.append(f"{callers} known callers")

    return total, reasons


def _impact_score(sites: Sequence[ImpactSite]) -> tuple[float, list[str]]:
    """Score reach: places this file's change lands that the diff does not show.

    A site the detector could not fully qualify is scored separately and named
    separately. "3 untouched impacted site(s)" is a factual claim, and a rank
    built partly on maybes that says it in those words is the ranking telling the
    same lie the finding refused to tell. The unverified sites still move the
    file -- they are worth opening -- just by less, and under their own sentence.

    ``public contract changed`` stays unconditional: the *definition* provably
    gained a required parameter. Only the question of who calls it is uncertain.
    """

    total = 0.0
    reasons: list[str] = []

    outside = [site for site in sites if not site.in_patch]
    confirmed = sum(1 for site in outside if not site.uncertainty)
    unverified = len(outside) - confirmed
    if confirmed:
        total += min(_UNTOUCHED_SITE_CAP, _UNTOUCHED_SITE_WEIGHT * confirmed)
        reasons.append(f"{confirmed} untouched impacted site(s)")
    if unverified:
        total += min(_UNVERIFIED_SITE_CAP, _UNVERIFIED_SITE_WEIGHT * unverified)
        reasons.append(f"{unverified} unverified impacted site(s)")

    if any(site.kind in _CONTRACT_KINDS for site in sites):
        total += _CONTRACT_WEIGHT
        reasons.append("public contract changed")

    return total, reasons


def _size_score(item: ChangedFile) -> tuple[float, list[str]]:
    churn = item.additions + item.deletions
    if churn <= 0:
        # No reason either: a zero contribution must never claim credit, or the
        # "every promoted file explains itself" invariant becomes unfalsifiable.
        return 0.0, []
    return min(_SIZE_CAP, churn / _SIZE_DIVISOR), [f"+{item.additions} -{item.deletions}"]


def _new_production_score(item: ChangedFile) -> tuple[float, list[str]]:
    """Promote only substantial newly-added production surfaces.

    Existing caller/centrality signals cannot exist yet for genuinely new code,
    while treating every added helper as high attention would flood the queue.
    The churn floor keeps this structural fallback narrow and explainable.
    """

    churn = item.additions + item.deletions
    if item.category != "production" or item.status != "added" or churn < _NEW_PRODUCTION_MIN_CHURN:
        return 0.0, []
    return _NEW_PRODUCTION_WEIGHT, [_NEW_PRODUCTION_REASON]


def _score_file(
    item: ChangedFile,
    symbols: Sequence[ChangedSymbol],
    sites: Sequence[ImpactSite],
    *,
    inspected_index: frozenset[str],
    apply_inspected: bool,
) -> tuple[float, tuple[str, ...]]:
    """Return the rounded score and sorted reasons for one changed file."""

    total = 0.0
    reasons: list[str] = []

    signals: list[tuple[float, list[str]]] = [
        _category_score(item.category),
        _impact_score(sites),
        _new_production_score(item),
        _size_score(item),
    ]
    # Test helper centrality measures the test graph, not production blast
    # radius. Tests can still be promoted by real impact/contract evidence.
    if item.category != "test":
        signals.insert(1, _symbol_score(symbols))

    for weight, notes in signals:
        total += weight
        reasons.extend(notes)
    if item.status == "deleted":
        total += _DELETED_WEIGHT
        reasons.append("file deleted")

    # Risk R2: several hosts record no reads at all, so an empty `inspected` set
    # means "nothing was recorded", not "the agent read nothing". Applying the
    # bonus there would add a constant to every impacted file and change no
    # relative order while printing a claim that is not evidenced.
    if apply_inspected and sites and _normalise(item.path) not in inspected_index:
        total += _UNINSPECTED_WEIGHT
        reasons.append(_UNINSPECTED_REASON)

    # Round before sorting so the printed score is the sort key. `or 0.0`
    # normalises a negative zero, which would otherwise render as "-0".
    return round(total, 4) or 0.0, tuple(sorted(set(reasons)))


def rank_files(
    files: Sequence[ChangedFile],
    symbols: Sequence[ChangedSymbol],
    impact: Sequence[ImpactSite],
    *,
    inspected: frozenset[str] = frozenset(),
) -> tuple[ReviewOrderEntry, ...]:
    """Rank *files* most-important-first, each entry carrying why it landed there.

    *symbols* and *impact* are the PR-2 outputs and may be empty: the ranking
    then falls back to the structural signals (category, churn, deletion) and
    stays useful. *inspected* is ``provenance.files_inspected``; an empty set
    disables the uninspected bonus entirely (risk R2).

    The result is a total order: ties break on category rank then on path, so
    two runs over the same packet produce the same tuple.
    """

    inspected_index = _inspected_index(inspected)
    apply_inspected = bool(inspected_index)
    by_symbol, by_site = _group_by_file(symbols, impact)

    scored: list[tuple[float, int, str, tuple[str, ...], FileCategory]] = []
    for item in files:
        key = _normalise(item.path)
        score, reasons = _score_file(
            item,
            by_symbol.get(key, ()),
            by_site.get(key, ()),
            inspected_index=inspected_index,
            apply_inspected=apply_inspected,
        )
        rank_hint = _CATEGORY_RANK.get(item.category, _UNRANKED_CATEGORY)
        scored.append((score, rank_hint, key, reasons, item.category))

    scored.sort(key=lambda row: (-row[0], row[1], row[2]))

    return tuple(
        ReviewOrderEntry(path=path, rank=rank, score=score, reasons=reasons, group=group)
        for rank, (score, _category_rank, path, reasons, group) in enumerate(scored, start=1)
    )


__all__ = ["rank_files"]
