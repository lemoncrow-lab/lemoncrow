"""One finding as the bot tracks it from push to push.

An AI finding (``ai_review.ConfirmedFinding``) lives for one run. The bot needs it
to survive the next push without a model call, so it keeps the fields a comment
needs and the identity the next run matches on. ``state`` uses the vocabulary of
the Review dispositions (``session_models.FINDING_DISPOSITIONS``): ``new`` and
``carried`` are an *open* finding, ``fixed`` is ``fixed``, ``stale`` is
``superseded_stale`` (the code it was about no longer exists). ``unreported`` is the one state with no Review
disposition: the code it was about changed and a later review did not find the problem again, but nothing
showed that it was fixed. It is closed (it blocks nothing), its thread is left open for a person, and it comes
back as ``carried``, on the same thread, if a later review reports it. ``suppressed`` is the other state with no
Review disposition: a person said, with a reaction, a resolved thread, a reply or an ``ignore`` command, that the bot
should not flag it on this pull request (:mod:`suppression`). It is closed, counts toward no verdict, keeps its thread,
and a later push that reports the same concern is held back the same way.

Severity is the product's own, one rule for every surface (:func:`severity_for`):

* **P0, must fix before merge**: a violation of a critical rule the repository wrote (and the
  finding cites that rule), or a *verified* finding the investigator rated ``high`` in the
  correctness, security or persistence (data-loss) categories. Verified means evidence from
  beyond the finding's own changed code backs it: a recorded check, an attached artifact, a
  static finding, or an excerpt of a *different* file (a caller, a test, a definition). The
  exact source of the finding's own target does not count -- every target has it and the
  model is shown those bytes anyway. A carried finding whose re-check failed is no longer verified.
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

# The rule lives in `criticality` so the Reader can use it without importing the PR bot; the names stay importable here.
from lemoncrow.pro.capabilities.review.criticality import FINDING_IMPACTS
from lemoncrow.pro.capabilities.review.criticality import P0_CATEGORIES as P0_CATEGORIES
from lemoncrow.pro.capabilities.review.criticality import severity_for as severity_for
from lemoncrow.pro.capabilities.review.pr_bot import attribution

SEVERITIES: Final[tuple[str, ...]] = ("p0", "p1", "p2")
SEVERITY_BADGE: Final[Mapping[str, str]] = {"p0": "P0", "p1": "P1", "p2": "P2"}
SEVERITY_MEANING: Final[Mapping[str, str]] = {"p0": "must fix", "p1": "should fix", "p2": "optional"}
SEVERITY_RANK: Final[Mapping[str, int]] = {"p0": 0, "p1": 1, "p2": 2}
STATES: Final[tuple[str, ...]] = ("new", "carried", "fixed", "stale", "unreported", "suppressed")
OPEN_STATES: Final[frozenset[str]] = frozenset({"new", "carried"})
PLACEMENTS: Final[tuple[str, ...]] = ("", "inline", "summary")
#: Investigator impact ratings, lowest first.
IMPACTS: Final[tuple[str, ...]] = FINDING_IMPACTS

#: Text kept per finding. Truncation is marked with an ellipsis and the originals
#: are bounded by the engine (title 160, explanation 900, consequence 400).
MAX_TITLE: Final[int] = 160
MAX_EXPLANATION: Final[int] = 900
MAX_CONSEQUENCE: Final[int] = 400
MAX_SUGGESTION: Final[int] = 4000

_SHA = re.compile(r"^[0-9a-f]{4,40}$")
_DIGEST = re.compile(r"^[0-9]{1,2}\.[0-9a-f]{12}$")


#: Longest span one inline comment covers. A finding about a whole function would
#: otherwise highlight the function; the comment sits on the lines the investigator cited, else the first changed
#: lines of its target.
MAX_INLINE_SPAN: Final[int] = 8
#: Where a finding's comment was anchored: ``cited``, the lines the investigator named (checked against the
#: Snapshot head and the diff), or ``target``, the fallback, the first changed lines of its target, used when it
#: cited none or the claim failed its checks. ``""`` is a finding recorded before the field existed.
ANCHORS: Final[tuple[str, ...]] = ("", "cited", "target")


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
    #: How ``line`` was chosen (:data:`ANCHORS`), and the first changed line of the finding's target when it was
    #: located. The second is the baseline a later push compares against to see that the target's code has moved.
    anchor: str = ""
    target_line: int = 0
    #: Digest of the text of the lines the investigator cited, when a review checked them
    #: (``finding_replacement.lines_digest``): what lets a later push tell that the finding's own code changed.
    anchor_digest: str = ""
    #: The suppression (:class:`suppression.Suppression`) that holds this finding back, when ``state`` is
    #: ``suppressed``; and whether an allowed person gave it a thumbs-up (kept for analytics, it changes nothing).
    suppressed_by: str = ""
    confirmed: bool = False
    #: What the finding relied on, as :mod:`attribution` tokens read from its validated rule and evidence refs (never
    #: from its wording): the rule it cites, the repository-context excerpts it cites, the kinds of tool evidence. The
    #: attribution trailer is built from these and from nothing else; empty when it cites none of them.
    used: tuple[str, ...] = ()
    #: The scanner rule id(s) behind this finding (``Static check: <rule id>``): the Opengrep rule a raw scan result
    #: came from, or the rules of the ``static_finding`` evidence an investigator's finding cited. ``scan`` is set
    #: when the finding *is* a scanner result no investigator confirmed (``security``, P1 at most, never a P0).
    static_check: str = ""
    scan: bool = False

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def has_suggestion(self) -> bool:
        """Whether a verified replacement is attached: text, and the new-side lines it replaces."""

        return bool(self.suggestion) and 0 < self.suggestion_start <= self.suggestion_end and self.side == "new"

    @property
    def span(self) -> tuple[int, int]:
        """The lines its inline comment covers (or will): the lines a suggestion replaces, else the lines it cites.

        Once a comment exists its lines are ``line..end_line`` for good (:meth:`posted`), whatever a later push
        suggests, so the summary never points away from the thread.
        """

        if self.has_suggestion and not self.thread_id:
            return self.suggestion_start, self.suggestion_end
        return self.line, max(self.line, self.end_line)

    def posted(self, **fields: Any) -> BotFinding:
        """This finding once its comment exists: ``line..end_line`` become the lines the comment covers.

        A thread cannot be moved, so from here on that is where the finding is, in the summary and in the prompt.
        """

        start, end = self.span
        return self.changed(line=start, end_line=end, **fields)

    def changed(self, **fields: Any) -> BotFinding:
        return replace(self, **fields)

    def rated(self) -> BotFinding:
        """This finding with its severity re-derived. A failed re-check removes the verification, and with it any P0 it earned.

        A critical-rule violation is the exception: the repository's rule made it a gate, not the model's rating or
        its evidence, so a failed re-check suspends nothing and its severity (set once, from the rule it cited) stands.
        """

        if self.category == "critical_rule":
            return self
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
            ("anchor", self.anchor),
            ("target_line", self.target_line),
            ("anchor_digest", self.anchor_digest),
            ("suppressed_by", self.suppressed_by),
            ("confirmed", self.confirmed),
            ("static_check", self.static_check),
            ("scan", self.scan),
        ):
            if value:
                wire[key] = value
        if self.has_suggestion:
            wire["suggestion"] = self.suggestion
            wire["suggestion_start"] = self.suggestion_start
            wire["suggestion_end"] = self.suggestion_end
        if self.used:
            wire["used"] = list(self.used)
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
    anchor = raw.get("anchor")
    digest = raw.get("anchor_digest")
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
        anchor=anchor if isinstance(anchor, str) and anchor in ANCHORS else "",
        target_line=_count(raw, "target_line"),
        anchor_digest=digest if isinstance(digest, str) and _DIGEST.match(digest) else "",
        suppressed_by=_text(raw, "suppressed_by", 40),
        confirmed=raw.get("confirmed") is True,
        used=attribution.clean_tokens(raw.get("used")),
        static_check=_text(raw, "static_check", 200),
        scan=raw.get("scan") is True,
    )


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
