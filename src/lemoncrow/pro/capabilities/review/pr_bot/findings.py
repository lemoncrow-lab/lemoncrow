"""One finding as the bot tracks it from push to push.

An AI finding (``ai_review.ConfirmedFinding``) lives for one run. The bot needs it
to survive the next push without a model call, so it keeps the fields a comment
needs and the identity the next run matches on. ``state`` uses the vocabulary of
the Review dispositions (``session_models.FINDING_DISPOSITIONS``): ``new`` and
``carried`` are an *open* finding, ``fixed`` is ``fixed``, ``stale`` is
``superseded_stale`` (the code it was about no longer exists).

Severity is the product's own: the validated category decides it
(``criticality.normalize_finding_severity``), never the model's wording. A finding
with no rating is shown as P1, because unrated is not low.
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

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def changed(self, **fields: Any) -> BotFinding:
        return replace(self, **fields)

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
            ("suggestion", self.suggestion),
            ("unverified", self.unverified),
        ):
            if value:
                wire[key] = value
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
        suggestion=_text(raw, "suggestion", MAX_SUGGESTION),
        unverified=raw.get("unverified") is True,
    )


def severity_for(category: str) -> str:
    """P0/P1/P2 for a validated finding category; an unrated category reads as P1, never low."""

    rated = normalize_finding_severity("ai_review", category=category)
    return rated if rated in SEVERITIES else "p1"


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
