"""One finding as the bot tracks it from push to push.

An AI finding (``ai_review.ConfirmedFinding``) lives for one run. The bot needs it
to survive the next push without a model call, so it keeps the fields a comment
needs and the identity the next run matches on. ``state`` uses the vocabulary of
the Review dispositions (``session_models.FINDING_DISPOSITIONS``): ``new`` and
``carried`` are an *open* finding, ``fixed`` is ``fixed``, ``stale`` is
``superseded_stale`` (the code it was about no longer exists).

Severity is the product's own, one rule for every surface (:func:`severity_for`):

* **P0, must fix before merge**: a violation of a critical rule the repository wrote, or a
  *verified* finding the investigator rated ``high`` in the correctness, security or
  persistence (data-loss) categories. Verified means a ref beyond the bare target pointer
  backs it (the exact Snapshot source, a recorded check, an attached artifact or a static
  finding), and a carried finding whose re-check failed is no longer verified.
* **P1, should fix**: every other ``high`` finding, and ``medium`` findings that are verified.
  A finding with no rating keeps its category's own severity (P1, or P2 for performance and
  verification), because unrated is not low.
* **P2, optional**: everything else, including every ``low`` finding.

The model's rating only nominates; it can lower a finding but never makes one P0 alone, and
its wording is never read. The verdict line, the badges, the held-back block, the inline
comments, the commit status and ``fail-on`` all read the severity this rule stored on the
finding, so none of them can disagree with another.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from lemoncrow.pro.capabilities.review.criticality import normalize_finding_severity

SEVERITIES: Final[tuple[str, ...]] = ("p0", "p1", "p2")
SEVERITY_BADGE: Final[Mapping[str, str]] = {"p0": "P0", "p1": "P1", "p2": "P2"}
SEVERITY_MEANING: Final[Mapping[str, str]] = {"p0": "must fix", "p1": "should fix", "p2": "optional"}
SEVERITY_RANK: Final[Mapping[str, int]] = {"p0": 0, "p1": 1, "p2": 2}
STATES: Final[tuple[str, ...]] = ("new", "carried", "fixed", "stale")
OPEN_STATES: Final[frozenset[str]] = frozenset({"new", "carried"})
PLACEMENTS: Final[tuple[str, ...]] = ("", "inline", "summary")
#: Investigator impact ratings, lowest first.
IMPACTS: Final[tuple[str, ...]] = ("low", "medium", "high")
#: The categories where a verified high-impact finding is a must-fix: wrong results, security holes and
#: data loss. Concurrency, API and performance findings can be P1 at most, however they are rated.
# lc-debt: hard-coded; there is no loader for PR-bot settings, only the CLI options and the rules in
# `.lemoncrow/review.yaml`; upgrade: a `review.pr_bot.p0_categories` key once such a loader exists.
P0_CATEGORIES: Final[frozenset[str]] = frozenset({"correctness", "security", "persistence_migration"})

#: Text kept per finding. Truncation is marked with an ellipsis and the originals
#: are bounded by the engine (title 160, explanation 900, consequence 400).
MAX_TITLE: Final[int] = 160
MAX_EXPLANATION: Final[int] = 900
MAX_CONSEQUENCE: Final[int] = 400
MAX_SUGGESTION: Final[int] = 4000

_SHA = re.compile(r"^[0-9a-f]{4,40}$")


#: Longest span one inline comment covers. A finding about a whole function would
#: otherwise highlight the function; the comment sits on the first changed lines.
# lc-debt: a finding names targets, not lines (the model returns none), so its comment sits on the first changed lines
# of its target; upgrade: have the investigator cite the line and check it against the Snapshot.
MAX_INLINE_SPAN: Final[int] = 8


@dataclass(frozen=True, slots=True)
class Located:
    """Where a finding's comment goes: the first changed lines of its target, on the side the target changed."""

    path: str
    line: int
    end_line: int
    side: str = "new"


def locate_target(target: Any) -> Located:
    """The comment location for a ``ReviewTarget`` (duck-typed: ``path``, ``spans``, ``start_line``, ``end_line``)."""

    spans = [span for span in target.spans if span.start_line > 0]
    side = "new" if not spans or any(span.side == "new" for span in spans) else "old"
    own = sorted((span for span in spans if span.side == side), key=lambda span: (span.start_line, span.end_line))
    if own:
        start = own[0].start_line
        end = max(start, own[0].end_line)
    elif target.start_line > 0:
        start, end = target.start_line, max(target.start_line, target.end_line)
    else:
        return Located(target.path, 0, 0, side)
    return Located(target.path, start, min(end, start + MAX_INLINE_SPAN - 1), side)


@dataclass(frozen=True, slots=True)
class BotFinding:
    """A tracked finding. ``line == 0`` means it has no line GitHub shows in the diff."""

    id: str
    fingerprint: str
    severity: str
    category: str
    state: str
    path: str
    line: int = 0
    end_line: int = 0
    side: str = "new"
    target_ids: tuple[str, ...] = ()
    title: str = ""
    explanation: str = ""
    consequence: str = ""
    first_seen: str = ""
    last_seen: str = ""
    resolved_in: str = ""
    thread_id: int = 0
    thread_url: str = ""
    placement: str = ""
    suggestion: str = ""
    unverified: bool = False
    #: The investigator's impact rating and whether evidence beyond the target backs the finding: the two
    #: inputs, with the category, that :func:`severity_for` turns into ``severity``.
    impact: str = ""
    verified: bool = False
    #: Where ``suggestion`` applies: lines ``suggestion_start..suggestion_end`` of ``path`` at the reviewed head.
    suggestion_start: int = 0
    suggestion_end: int = 0

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def has_suggestion(self) -> bool:
        """Whether a verified replacement is attached: text, and the new-side lines it replaces."""

        return bool(self.suggestion) and 0 < self.suggestion_start <= self.suggestion_end and self.side == "new"

    @property
    def span(self) -> tuple[int, int]:
        """The lines its inline comment covers: the lines a suggestion replaces, else its target's first changed lines."""

        if self.has_suggestion:
            return self.suggestion_start, self.suggestion_end
        return self.line, max(self.line, self.end_line)

    def changed(self, **fields: Any) -> BotFinding:
        return replace(self, **fields)

    def rated(self) -> BotFinding:
        """This finding with its severity re-derived. A failed re-check removes the verification, and with it any P0 it earned."""

        return self.changed(
            severity=severity_for(self.category, impact=self.impact, verified=self.verified and not self.unverified)
        )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "severity": self.severity,
            "category": self.category,
            "state": self.state,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "side": self.side,
            "targets": list(self.target_ids),
            "title": self.title,
            "explanation": self.explanation,
            "consequence": self.consequence,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }
        # Sparse: a field at its default is left out, so a wire row stays small.
        for key, value in (
            ("resolved_in", self.resolved_in),
            ("thread_id", self.thread_id),
            ("thread_url", self.thread_url),
            ("placement", self.placement),
            ("unverified", self.unverified),
            ("impact", self.impact),
            ("verified", self.verified),
        ):
            if value:
                wire[key] = value
        if self.has_suggestion:
            wire["suggestion"] = self.suggestion
            wire["suggestion_start"] = self.suggestion_start
            wire["suggestion_end"] = self.suggestion_end
        return wire


def _text(raw: Mapping[str, Any], key: str, limit: int) -> str:
    value = raw.get(key)
    return value[:limit] if isinstance(value, str) else ""


def _count(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10_000_000 else 0


def _sha(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) and _SHA.match(value) else ""


def finding_from_wire(raw: object) -> BotFinding | None:
    """A finding from a state row, or ``None`` when the row is not a well-formed finding.

    The state comes from a comment on the pull request, so every field is checked:
    a row that is out of vocabulary is dropped rather than trusted.
    """

    if not isinstance(raw, dict):
        return None
    identity = raw.get("id")
    severity = raw.get("severity")
    state = raw.get("state")
    placement = raw.get("placement", "")
    if (
        not isinstance(identity, str)
        or not 0 < len(identity) <= 80
        or severity not in SEVERITIES
        or state not in STATES
        or placement not in PLACEMENTS
        or not isinstance(raw.get("path"), str)
    ):
        return None
    targets = raw.get("targets")
    impact = raw.get("impact")
    # A suggestion is whole or absent: truncating a replacement would post a different, wrong edit.
    suggestion = raw.get("suggestion")
    fix = suggestion if isinstance(suggestion, str) and 0 < len(suggestion) <= MAX_SUGGESTION else ""
    fix_start, fix_end = _count(raw, "suggestion_start"), _count(raw, "suggestion_end")
    if not fix or not 0 < fix_start <= fix_end:
        fix, fix_start, fix_end = "", 0, 0
    return BotFinding(
        id=identity,
        fingerprint=_text(raw, "fingerprint", 80),
        severity=str(severity),
        category=_text(raw, "category", 40),
        state=str(state),
        path=_text(raw, "path", 1024),
        line=_count(raw, "line"),
        end_line=_count(raw, "end_line"),
        side="old" if raw.get("side") == "old" else "new",
        target_ids=(
            tuple(item[:200] for item in targets[:16] if isinstance(item, str)) if isinstance(targets, list) else ()
        ),
        title=_text(raw, "title", MAX_TITLE),
        explanation=_text(raw, "explanation", MAX_EXPLANATION),
        consequence=_text(raw, "consequence", MAX_CONSEQUENCE),
        first_seen=_sha(raw, "first_seen"),
        last_seen=_sha(raw, "last_seen"),
        resolved_in=_sha(raw, "resolved_in"),
        thread_id=_count(raw, "thread_id"),
        thread_url=_text(raw, "thread_url", 300),
        placement=str(placement),
        suggestion=fix,
        unverified=raw.get("unverified") is True,
        impact=impact if impact in IMPACTS else "",
        verified=raw.get("verified") is True,
        suggestion_start=fix_start,
        suggestion_end=fix_end,
    )


def severity_for(category: str, *, impact: str = "", verified: bool = False) -> str:
    """The severity of a finding: the one rule behind every badge, verdict and status (see the module docstring).

    ``category`` is the finding's validated category, ``impact`` the investigator's rating (``""`` when it gave
    none) and ``verified`` whether evidence beyond the bare target backs it. Unverified speculation is never P0:
    a ``high`` rating without that evidence, or outside :data:`P0_CATEGORIES`, is P1.
    """

    if category == "critical_rule":
        return "p0"
    if impact == "high":
        return "p0" if verified and category in P0_CATEGORIES else "p1"
    if impact == "medium":
        return "p1" if verified else "p2"
    if impact == "low":
        return "p2"
    # Unrated keeps the category's own severity (never P0 from here, never below P2).
    rated = normalize_finding_severity("ai_review", category=category)
    return rated if rated in {"p1", "p2"} else "p1"


def fingerprint_for(category: str, target_ids: Sequence[str], rule_refs: Sequence[str] = ()) -> str:
    """The looser identity a finding is matched on when the model words it differently.

    The engine's own finding id includes the title and evidence, so a re-review of a
    changed target can mint a new id for the same concern. Category, targets and
    rules are the scope the engine already treats as one concern when it dedupes.
    """

    document = "\0".join((category, ",".join(sorted(target_ids)), ",".join(sorted(rule_refs))))
    return "fp-" + hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]


def trimmed(text: str, limit: int) -> str:
    """*text* on one line's worth of whitespace, cut to *limit* with a visible ellipsis."""

    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def sort_key(finding: BotFinding) -> tuple[int, str, int, str]:
    """Severity first (P0 before P1 before P2), then location."""

    return (SEVERITY_RANK.get(finding.severity, 1), finding.path, finding.line, finding.id)
