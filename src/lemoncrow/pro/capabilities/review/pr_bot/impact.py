"""A deterministic impact label for the summary, and the named risks a review examined.

Two things a reader wants before they read a single finding: *how much could this change break*, and *what did the
review actually look at*. Both are answered here from the change itself, by fixed rules, with no model call and no model
text:

* :class:`Impact` is ``Low``, ``Medium`` or ``High``. It is the blast radius of the pull request, not a judgment of its
  quality: a careful change to the auth code is still ``High impact``. It is derived from the Snapshot's changed targets
  (how many files and lines, which path policy and risk signals they carry) and shown with the reasons, so a reader can
  check it. It never feeds an approval, a status or ``fail-on``.
* :class:`RiskRow` is one changed piece of code that carries a risk signal (the same deterministic signals that decide
  what the investigator is asked about, ``ai_review.generate_candidates``) with what became of it: ``found`` (an open
  finding is about it), ``cleared`` (a model call about it returned and the review reported nothing) or ``pending`` (no
  call about it has returned yet). The words for ``cleared`` are "examined, nothing found", never "safe": a model that
  does not find a bug has not proved there is none.

The impact rule, in full (the points are added, then the level is read off):

=======================================  ======
what the change shows                    points
=======================================  ======
a path your policy marks critical/high   4
security-sensitive code                  2
payments or billing code                 2
persistence or a migration               2
an API or wire contract                  1
concurrency or shared state              1
a critical repository rule applies       1
a sizeable change (8+ files / 250+ lines)  1
a large change (25+ files / 1,000+ lines)  2
=======================================  ======

``High`` is 4 points or more, ``Medium`` 2 or 3, ``Low`` 0 or 1. Generated, vendored, mechanical and comment-only or
formatting-only changes count for nothing, so a lockfile bump is not a large change. The signals are the Review's own
(``ai_review``'s risk markers on path, symbol and the queue's reasons; ``criticality_policy`` from the trusted base's
``review.focus``), and payments is the one family this module adds, by path only. The inputs are all in the Snapshot, so
the same pull request gets the same label on every run and on every machine.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

# lc-debt: the weights, thresholds and the payments word list are a first rule that no real pull request has tuned, and the
# Risks examined list is rebuilt each review rather than kept in the hidden state; upgrade: tune them with the real-PR
# benchmark, and keep the rows in the state if the budget notice should repeat them.
#: Impact levels, lowest first.
LEVELS: Final[tuple[str, ...]] = ("low", "medium", "high")
#: Points each reason adds, in the order reasons are listed.
POINTS: Final[Mapping[str, int]] = {
    "critical_path": 4,
    "security": 2,
    "payments": 2,
    "persistence": 2,
    "api": 1,
    "concurrency": 1,
    "rule": 1,
    "large": 2,
    "sizeable": 1,
}
HIGH_AT: Final[int] = 4
MEDIUM_AT: Final[int] = 2
#: A change at or above either bound is sizeable / large (files, changed lines).
SIZEABLE: Final[tuple[int, int]] = (8, 250)
LARGE: Final[tuple[int, int]] = (25, 1_000)
#: What each reason is called in the summary.
REASON_WORDS: Final[Mapping[str, str]] = {
    "critical_path": "a path marked critical or high",
    "security": "security-sensitive code",
    "payments": "payments code",
    "persistence": "persistence or a migration",
    "api": "an API contract",
    "concurrency": "concurrency or shared state",
    "rule": "code a critical rule covers",
    "large": "a large change",
    "sizeable": "a sizeable change",
}
#: Candidate category (``ai_review.FindingCategory``) -> impact reason. Performance, verification and correctness are
#: about how well the code works, not what it can break, so they add nothing.
_CATEGORY_REASON: Final[Mapping[str, str]] = {
    "security": "security",
    "persistence_migration": "persistence",
    "api_compatibility": "api",
    "concurrency_state": "concurrency",
}
#: Code that nobody needs to read for risk: ``ai_review`` skips the same ones when it looks for risk signals.
_SKIPPED_CODES: Final[frozenset[str]] = frozenset(
    {"generated_or_vendor", "routine_formatting_only", "routine_comment_only"}
)
_PAYMENTS: Final = re.compile(r"(?<![a-z0-9])(?:payment|billing|invoice|checkout|stripe|ledger)s?(?![a-z0-9])")
_STATE: Final = re.compile(
    r"^(?P<level>low|medium|high):(?P<reasons>-|[a-z_]+(?:\+[a-z_]+)*):(?P<files>\d{1,6}):(?P<lines>\d{1,9})$"
)


@dataclass(frozen=True, slots=True)
class Impact:
    """The label, the reasons behind it (a subset of :data:`POINTS`, in its order) and the size it saw."""

    level: str
    reasons: tuple[str, ...]
    files: int
    lines: int

    @property
    def label(self) -> str:
        return f"{self.level.capitalize()} impact"

    @property
    def score(self) -> int:
        return sum(POINTS[reason] for reason in self.reasons)

    def to_state(self) -> str:
        """The compact form kept in the summary's hidden state: ``high:security+persistence:12:340``."""

        return f"{self.level}:{'+'.join(self.reasons) or '-'}:{self.files}:{self.lines}"

    @classmethod
    def from_state(cls, text: object) -> Impact | None:
        """An impact read back from the state, or ``None`` for anything that is not exactly what :meth:`to_state` writes."""

        found = _STATE.match(text) if isinstance(text, str) else None
        if found is None:
            return None
        reasons = () if found["reasons"] == "-" else tuple(found["reasons"].split("+"))
        if any(reason not in POINTS for reason in reasons) or list(reasons) != [r for r in POINTS if r in reasons]:
            return None
        impact = cls(found["level"], reasons, int(found["files"]), int(found["lines"]))
        return impact if impact.level == level_for(impact.score) else None

    def to_wire(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "label": self.label,
            "reasons": list(self.reasons),
            "score": self.score,
            "files": self.files,
            "lines": self.lines,
        }

    def summary(self) -> str:
        """``reason, reason · 12 files, 340 changed lines``: what the label rests on, from fixed words and numbers."""

        why = ", ".join(REASON_WORDS[reason] for reason in self.reasons)
        size = f"{self.files} file{'' if self.files == 1 else 's'}, {self.lines} changed line{'' if self.lines == 1 else 's'}"
        return f"{why} · {size}" if why else size


def level_for(score: int) -> str:
    return "high" if score >= HIGH_AT else "medium" if score >= MEDIUM_AT else "low"


def _codes(target: Any) -> frozenset[str]:
    return frozenset(reason.code for reason in target.priority.reasons)


def counted(targets: Iterable[Any]) -> list[Any]:
    """The targets that count toward the size and the policy: not generated, vendored, mechanical, comment- or format-only."""

    return [
        target
        for target in targets
        if getattr(target, "structural_level", "normal") != "mechanical" and not _codes(target) & _SKIPPED_CODES
    ]


def assess(targets: Sequence[Any], candidates: Sequence[Any]) -> Impact:
    """The impact of a change: its *targets* (``ReviewTarget``) and the risk *candidates* ``generate_candidates`` found in them.

    Pure and order-independent: the same targets and candidates always give the same :class:`Impact`.
    """

    from lemoncrow.pro.capabilities.review.rules import is_critical_rule_ref

    kept = counted(targets)
    files = len({target.path for target in kept})
    lines = sum(max(target.additions, 0) + max(target.deletions, 0) for target in kept)
    found: set[str] = set()
    if any("criticality_policy" in _codes(target) for target in kept):
        found.add("critical_path")
    if any(_PAYMENTS.search(target.path.lower()) for target in kept):
        found.add("payments")
    for candidate in candidates:
        reason = _CATEGORY_REASON.get(candidate.category)
        if reason is not None:
            found.add(reason)
        elif candidate.category == "critical_rule" and (
            not candidate.rule_refs or any(is_critical_rule_ref(ref) for ref in candidate.rule_refs)
        ):
            found.add("rule")
    if files >= LARGE[0] or lines >= LARGE[1]:
        found.add("large")
    elif files >= SIZEABLE[0] or lines >= SIZEABLE[1]:
        found.add("sizeable")
    reasons = tuple(reason for reason in POINTS if reason in found)
    return Impact(level_for(sum(POINTS[reason] for reason in reasons)), reasons, files, lines)


# --------------------------------------------------------------------------- #
# the risks a review examined
# --------------------------------------------------------------------------- #

#: What each candidate category is called in the list.
CATEGORY_WORDS: Final[Mapping[str, str]] = {
    "security": "security",
    "persistence_migration": "persistence or a migration",
    "api_compatibility": "an API contract",
    "concurrency_state": "concurrency or shared state",
    "performance": "performance",
    "verification": "failing or unknown checks",
    "correctness": "code that needs renewed scrutiny",
    "critical_rule": "a critical rule",
}
#: Rows the list names before it says ``+N more``.
MAX_ROWS: Final[int] = 7
OUTCOMES: Final[tuple[str, ...]] = ("found", "pending", "cleared")


@dataclass(frozen=True, slots=True)
class Signal:
    """One changed target that carries a risk signal and what this review did about it.

    ``status`` is ``examined`` (a model call about it returned in this run), ``earlier`` (its code is unchanged since a
    review that examined it, so it was not asked again) or ``pending`` (no call about it has returned: the depth limit,
    a failed call or a spent budget left it for the next run). ``categories`` is what to name: for an examined target the
    categories a call actually covered, else every category the signal carries. ``rules`` are the keys of the critical
    rules that cover it. ``rank`` orders the list (the highest candidate score).
    """

    target_id: str
    scope: str
    categories: tuple[str, ...]
    status: str
    rules: tuple[str, ...] = ()
    rank: int = 0


@dataclass(frozen=True, slots=True)
class RiskRow:
    """A :class:`Signal` with its outcome against the findings the review now tracks."""

    scope: str
    categories: tuple[str, ...]
    rules: tuple[str, ...]
    outcome: str
    findings: int = 0
    earlier: bool = False
    rank: int = 0


def _scope(target: Any) -> str:
    place = f"{target.path}:{target.start_line}-{target.end_line}"
    return f"{place} ({target.symbol})" if getattr(target, "symbol", "") else place


def signals_for(
    targets: Sequence[Any],
    candidates: Sequence[Any],
    *,
    changed: Collection[str],
    asked: Mapping[str, Collection[str]],
    reviewed: Collection[str],
    rule_keys: Mapping[str, str],
) -> tuple[Signal, ...]:
    """The risk signals of *targets* and the status of each.

    *candidates* are the deterministic risk candidates of all the targets, *changed* the targets this run asked about,
    *asked* ``{target id: categories a model call about it covered}`` for them, *reviewed* the targets whose current code
    a review has finished with (including this one) and *rule_keys* ``{rule ref: stable key}``.
    """

    by_id = {target.target_id: target for target in targets}
    categories: dict[str, dict[str, int]] = {}
    rules: dict[str, dict[str, None]] = {}
    for candidate in candidates:
        for target_id in candidate.target_ids:
            if target_id not in by_id:
                continue
            ranked = categories.setdefault(target_id, {})
            ranked[candidate.category] = max(ranked.get(candidate.category, 0), int(candidate.score))
            for ref in candidate.rule_refs:
                if ref in rule_keys:
                    rules.setdefault(target_id, {})[rule_keys[ref]] = None
    out: list[Signal] = []
    for target_id, ranked in categories.items():
        every = tuple(sorted(ranked, key=lambda category: (-ranked[category], category)))
        covered = tuple(category for category in every if category in asked.get(target_id, ()))
        if target_id in changed:
            status, shown = ("examined", covered) if covered else ("pending", every)
        elif target_id in reviewed:
            status, shown = "earlier", every
        else:
            status, shown = "pending", every
        out.append(
            Signal(
                target_id,
                _scope(by_id[target_id]),
                shown,
                status,
                tuple(rules.get(target_id, ())),
                max(ranked.values()),
            )
        )
    return tuple(sorted(out, key=lambda signal: (-signal.rank, signal.scope, signal.target_id)))


def risk_rows(signals: Sequence[Signal], findings: Iterable[Any]) -> tuple[RiskRow, ...]:
    """Every signal with its outcome, found first, then pending, then cleared; a row is ``found`` if a finding is open on it."""

    open_on: dict[str, int] = {}
    for finding in findings:
        if finding.is_open:
            for target_id in finding.target_ids:
                open_on[target_id] = open_on.get(target_id, 0) + 1
    rows = [
        RiskRow(
            signal.scope,
            signal.categories,
            signal.rules,
            "found" if open_on.get(signal.target_id) else "pending" if signal.status == "pending" else "cleared",
            open_on.get(signal.target_id, 0),
            signal.status == "earlier",
            signal.rank,
        )
        for signal in signals
    ]
    return tuple(sorted(rows, key=lambda row: (OUTCOMES.index(row.outcome), -row.rank, row.scope)))


def counts(rows: Sequence[RiskRow]) -> dict[str, int]:
    """``{"found": n, "pending": n, "cleared": n}``."""

    return {outcome: sum(1 for row in rows if row.outcome == outcome) for outcome in OUTCOMES}


__all__ = [
    "CATEGORY_WORDS",
    "HIGH_AT",
    "LEVELS",
    "MAX_ROWS",
    "MEDIUM_AT",
    "OUTCOMES",
    "POINTS",
    "REASON_WORDS",
    "Impact",
    "RiskRow",
    "Signal",
    "assess",
    "counted",
    "counts",
    "level_for",
    "risk_rows",
    "signals_for",
]
