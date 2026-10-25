"""The machine-readable state the summary comment carries from one run to the next.

A CI job leaves nothing behind, so the only durable store a pull request review has
is the pull request itself. The summary comment ends with one hidden block::

    <!-- lemoncrow:state v1
    {"schema":1,"head_sha":"...","findings":[...],...}
    -->

It holds the reviewed head and base, the findings with their ids, fingerprints and
states, the units (reviewed targets) the next push is compared against, and the
usage so far. Third-party tools read it instead of scraping the comment text, and
the next run reads it to re-review only what changed.

The JSON is written so it can never end its own comment or mention anyone: every ``--``,
``<``, ``>`` and ``@`` is a ``\\u`` escape, which any JSON parser reads back unchanged.

Bounds are explicit. A comment holds 65,536 characters, so :func:`shrink` removes
detail in fixed steps (never silently dropping an open P0/P1 finding) and the state
says what it lost: ``units_truncated`` and ``findings_dropped``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from lemoncrow.pro.capabilities.review.pr_bot.findings import (
    SEVERITY_RANK,
    BotFinding,
    finding_from_wire,
)
from lemoncrow.pro.capabilities.review.pr_bot.impact import Impact
from lemoncrow.pro.capabilities.review.pr_bot.suppression import (
    MAX_SUPPRESSIONS,
    Suppression,
    suppression_from_wire,
)

SCHEMA: Final[int] = 1
SUMMARY_MARKER: Final[str] = "<!-- lemoncrow:summary v1 -->"
FINDING_MARKER: Final[str] = "<!-- lemoncrow:finding {finding_id} -->"
FINDING_MARKER_PREFIX: Final[str] = "<!-- lemoncrow:finding "
#: The finding's fingerprint, stable across rewording (``findings.fingerprint_for``): what lets a later run recognise
#: an inline comment it posted for the same concern when it no longer has the finding's id (a lost summary).
FINGERPRINT_MARKER: Final[str] = "<!-- lemoncrow:fingerprint {fingerprint} -->"
#: On the bot's "learned" reply in a finding's thread: which suppression it confirms, so it is said once per thread (G9).
LEARNED_MARKER: Final[str] = "<!-- lemoncrow:learned {suppression_id} -->"
#: On the bot's reply in a finding's thread when feedback on a must-fix was recorded and refused, so it is said once per
#: thread and suppression, like the "learned" one.
DECLINED_MARKER: Final[str] = "<!-- lemoncrow:declined {suppression_id} -->"
STATE_OPEN: Final[str] = "<!-- lemoncrow:state v1\n"
STATE_CLOSE: Final[str] = "\n-->"
_STATE = re.compile(r"<!-- lemoncrow:state v1\n(.*?)\n-->", re.DOTALL)
_SHA = re.compile(r"^[0-9a-f]{40}$")
#: What the change diagram (G17) leaves in the state: ``<drawing version>:<16 hex of the drawn section>``, or
#: ``<version>:none`` when the Architecture Delta was read and nothing was worth drawing. Anything else is no record.
DIAGRAM_STATE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{1,3}:(?:none|[0-9a-f]{16})$")

#: Findings and reviewed units the state may carry. Past them the state says so.
# lc-debt: the state rides in one 65,536-character comment, so at most 60 findings and 700 reviewed units are
# carried (past that the rest are re-reviewed on the next push); upgrade: keep the units in a workflow artifact or a git ref.
MAX_FINDINGS: Final[int] = 60
MAX_UNITS: Final[int] = 700
MAX_STATE_BYTES: Final[int] = 200_000
#: Hex characters kept of a unit key and of its content fingerprint (48 and 40 bits).
UNIT_KEY_CHARS: Final[int] = 12
UNIT_FINGERPRINT_CHARS: Final[int] = 10


def unit_token(unit_key: str, content_fingerprint: str) -> tuple[str, str]:
    """The compact ``(key, fingerprint)`` pair one reviewable unit is compared by across pushes."""

    return unit_key.split(":", 1)[-1][:UNIT_KEY_CHARS], content_fingerprint[:UNIT_FINGERPRINT_CHARS]


@dataclass(frozen=True, slots=True)
class Usage:
    """Model usage. ``None`` means a call did not report it; it is never read as zero.

    ``context_tokens`` is LemonCrow's own estimate of the repository context (callers, definitions, tests,
    configuration) it put in the prompts. It is *part of* ``input_tokens`` when the provider reports them, and is
    shown beside them so the cost of context is visible; it is an estimate (four characters per token), never a
    provider count, and it is 0 when no context was sent.
    """

    calls: int = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    cost_usd: float | None = 0.0
    context_tokens: int = 0

    def plus(self, other: Usage) -> Usage:
        def add(left: int | None, right: int | None) -> int | None:
            return None if left is None or right is None else left + right

        cost = None if self.cost_usd is None or other.cost_usd is None else self.cost_usd + other.cost_usd
        return Usage(
            self.calls + other.calls,
            add(self.input_tokens, other.input_tokens),
            add(self.output_tokens, other.output_tokens),
            cost,
            self.context_tokens + other.context_tokens,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            # Absent when no context was sent, so a state written before G4 round-trips unchanged.
            **({"context_tokens": self.context_tokens} if self.context_tokens else {}),
        }

    @classmethod
    def from_wire(cls, raw: object) -> Usage:
        if not isinstance(raw, dict):
            return cls()

        def count(key: str) -> int | None:
            value = raw.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        cost = raw.get("cost_usd")
        known = isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
        calls = count("calls")
        return cls(
            calls or 0,
            count("input_tokens"),
            count("output_tokens"),
            float(cost) if known else None,  # type: ignore[arg-type]
            count("context_tokens") or 0,
        )


@dataclass(frozen=True, slots=True)
class ReviewState:
    """What one finished run knew, enough for the next run to continue from it."""

    head_sha: str
    base_sha: str = ""
    merge_base_sha: str = ""
    pull_request: str = ""
    depth: str = "auto"
    model: str = ""
    #: Digest of everything that makes a reviewed unit's result reusable: the rules, the
    #: content-access policy, the depth and the prompt version. A change re-reviews all.
    policy: str = ""
    complete: bool = True
    reviews: int = 1
    #: ``{unit key token: fingerprint token}`` of every unit the AI stage covered.
    reviewed_units: Mapping[str, str] = field(default_factory=dict)
    units_truncated: bool = False
    findings: tuple[BotFinding, ...] = ()
    findings_dropped: int = 0
    usage: Usage = field(default_factory=Usage)
    usage_total: Usage = field(default_factory=Usage)
    #: Chat answers given on this pull request (G6) and what their model calls cost. They are kept apart from
    #: ``reviews`` and ``usage_total`` so a review and a chat answer, which can run at the same time, never overwrite
    #: each other's numbers; both count toward the pull request's budget (:attr:`runs`, :attr:`spent`).
    chats: int = 0
    chat_usage: Usage = field(default_factory=Usage)
    #: What people told the bot not to flag on this pull request (G9). Kept beside the findings it held back so a
    #: later push that reports the same concern is held back the same way.
    suppressions: tuple[Suppression, ...] = ()
    #: A small digest of the change diagram this summary shows (G17), so a re-run on an unchanged head reuses the drawn
    #: block instead of recomputing it, and rewrites the comment only when the drawing differs. ``""``: none was drawn
    #: (turned off, too large, not computed, or dropped to fit the comment).
    diagram: str = ""
    #: The deterministic impact label the summary shows (:class:`impact.Impact`), as :meth:`Impact.to_state` writes it:
    #: ``high:security+persistence:12:340``. Kept so a summary that is re-rendered without a review (a spent budget)
    #: still says it, and so a re-run on an unchanged head rewrites the comment when the rule gives another answer.
    #: ``""``: none was computed.
    impact: str = ""

    def open_findings(self) -> tuple[BotFinding, ...]:
        return tuple(finding for finding in self.findings if finding.is_open)

    @property
    def runs(self) -> int:
        """Model-using runs on this pull request so far: reviews and chat answers. ``max-runs-per-pr`` caps this."""

        return self.reviews + self.chats

    @property
    def spent(self) -> Usage:
        """Everything this pull request has cost so far, reviews and chat answers. ``max-usd-per-pr`` caps its cost."""

        return self.usage_total.plus(self.chat_usage)


def verdict_counts(findings: tuple[BotFinding, ...] | list[BotFinding]) -> dict[str, int]:
    counts = {"p0": 0, "p1": 0, "p2": 0}
    for finding in findings:
        if finding.is_open and finding.severity in counts:
            counts[finding.severity] += 1
    return counts


def _units_text(units: Mapping[str, str]) -> str:
    return " ".join(f"{key}.{value}" for key, value in sorted(units.items()))


def _units_from_text(text: object) -> dict[str, str]:
    if not isinstance(text, str):
        return {}
    units: dict[str, str] = {}
    for token in text.split():
        key, _, value = token.partition(".")
        if key and value:
            units[key] = value
    return units


def state_wire(state: ReviewState) -> dict[str, Any]:
    counts = verdict_counts(state.findings)
    return {
        "schema": SCHEMA,
        "pull_request": state.pull_request,
        "head_sha": state.head_sha,
        "base_sha": state.base_sha,
        "merge_base_sha": state.merge_base_sha,
        "depth": state.depth,
        "model": state.model,
        "policy": state.policy,
        "complete": state.complete,
        "reviews": state.reviews,
        "verdict": counts,
        "findings": [finding.to_wire() for finding in state.findings],
        "findings_dropped": state.findings_dropped,
        "units": _units_text(state.reviewed_units),
        "units_truncated": state.units_truncated,
        "usage": state.usage.to_wire(),
        "usage_total": state.usage_total.to_wire(),
        # Absent until the first chat answer, so a state written before G6 round-trips unchanged.
        **({"chats": state.chats, "chat_usage": state.chat_usage.to_wire()} if state.chats else {}),
        **({"suppressions": [row.to_wire() for row in state.suppressions]} if state.suppressions else {}),
        # Absent until a diagram was drawn or ruled out, so a state written before G17 round-trips unchanged.
        **({"diagram": state.diagram} if state.diagram else {}),
        # Absent until an impact was computed, so a state written before it round-trips unchanged.
        **({"impact": state.impact} if state.impact else {}),
    }


def dump_state(state: ReviewState) -> str:
    """The hidden block. Valid JSON that cannot terminate its own HTML comment."""

    text = json.dumps(state_wire(state), separators=(",", ":"), ensure_ascii=True)
    # `@` too: model text in the block must not be able to mention anyone.
    text = text.replace("--", "-\\u002d").replace("<", "\\u003c").replace(">", "\\u003e").replace("@", "\\u0040")
    return f"{STATE_OPEN}{text}{STATE_CLOSE}"


def strip_state(body: str) -> str:
    """*body* without the hidden state block, for a terminal."""

    return _STATE.sub("", body).rstrip() + "\n"


def _sha(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) and _SHA.match(value) else ""


def parse_state(body: str) -> ReviewState | None:
    """The state block in a summary comment body, or ``None`` when absent or untrustworthy.

    Exactly one block is required: model text is escaped before it reaches a comment,
    so a second block means someone else wrote it. Anything out of vocabulary reads as
    no state, which costs a full re-review and never a wrong carry-forward.
    """

    matches = _STATE.findall(body)
    if len(matches) != 1 or len(matches[0]) > MAX_STATE_BYTES:
        return None
    try:
        raw = json.loads(matches[0])
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return None
    head = _sha(raw, "head_sha")
    if not head:
        return None
    rows = raw.get("findings")
    findings = tuple(
        item
        for item in (finding_from_wire(row) for row in (rows[: MAX_FINDINGS * 2] if isinstance(rows, list) else ()))
        if item is not None
    )

    def text(key: str, limit: int) -> str:
        value = raw.get(key)
        return value[:limit] if isinstance(value, str) else ""

    reviews = raw.get("reviews")
    dropped = raw.get("findings_dropped")
    chats = raw.get("chats")
    held = raw.get("suppressions")
    drawn = raw.get("diagram")
    weighed = raw.get("impact")
    return ReviewState(
        head_sha=head,
        base_sha=_sha(raw, "base_sha"),
        merge_base_sha=_sha(raw, "merge_base_sha"),
        pull_request=text("pull_request", 200),
        depth=text("depth", 20) or "auto",
        model=text("model", 200),
        policy=text("policy", 80),
        complete=raw.get("complete") is not False,
        reviews=reviews if isinstance(reviews, int) and not isinstance(reviews, bool) and reviews > 0 else 1,
        reviewed_units=_units_from_text(raw.get("units")),
        units_truncated=raw.get("units_truncated") is True,
        findings=findings,
        findings_dropped=dropped if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped > 0 else 0,
        usage=Usage.from_wire(raw.get("usage")),
        usage_total=Usage.from_wire(raw.get("usage_total")),
        chats=chats if isinstance(chats, int) and not isinstance(chats, bool) and 0 < chats < 1_000_000 else 0,
        chat_usage=Usage.from_wire(raw.get("chat_usage")),
        suppressions=tuple(
            row
            for row in (
                suppression_from_wire(item) for item in (held[:MAX_SUPPRESSIONS] if isinstance(held, list) else ())
            )
            if row is not None
        ),
        diagram=drawn if isinstance(drawn, str) and DIAGRAM_STATE.match(drawn) else "",
        impact=weighed if isinstance(weighed, str) and Impact.from_state(weighed) is not None else "",
    )


def with_state(body: str, state: ReviewState) -> str:
    """*body* with its hidden state block replaced by *state*'s; the readable part is untouched.

    For a change to the state that does not change what the comment says (a chat answer's bookkeeping): the visible
    summary was rendered from facts the state does not hold, so it is not re-rendered from the state.
    """

    return _STATE.sub(lambda _match: dump_state(state), body, count=1)


# --------------------------------------------------------------------------- #
# fitting the state into a comment
# --------------------------------------------------------------------------- #

#: Detail levels, in the order they are applied. Level 0 keeps everything.
MAX_DETAIL: Final[int] = 4
#: At the last level, findings kept beyond the open P0/P1 ones (which are always kept).
MAX_KEPT_AT_LIMIT: Final[int] = 25


def _priority(finding: BotFinding) -> tuple[int, int, str]:
    """What survives a cap first: open before closed, then P0 before P2."""

    return (0 if finding.is_open else 1, SEVERITY_RANK.get(finding.severity, 1), finding.id)


def shrink(state: ReviewState, level: int) -> ReviewState:
    """*state* with detail removed up to *level* (0: none, :data:`MAX_DETAIL`: the least it can carry).

    1. every finding drops its attribution trailer, and closed findings (fixed, stale) keep only their identity and title;
    2. P2 findings drop their long text;
    3. every finding's text is cut to a short form and the reviewed-unit list to :data:`MAX_UNITS` / 4;
    4. the unit list is dropped (the next run re-reviews) and every finding keeps only its title and place;
       open P0/P1 findings are all kept, other findings are capped so 25 remain in total.
    """

    findings = list(state.findings)
    if level >= 1:
        # The attribution trailers go first: they are a courtesy, and the posted comments already carry theirs.
        findings = [f.changed(used=()) for f in findings]
        findings = [f.changed(explanation="", consequence="", suggestion="") if not f.is_open else f for f in findings]
    if level >= 2:
        findings = [
            (
                f.changed(explanation=f.explanation[:200], consequence=f.consequence[:100], suggestion="")
                if f.severity == "p2"
                else f
            )
            for f in findings
        ]
    if level >= 3:
        findings = [
            f.changed(
                explanation=f.explanation[:300],
                consequence=f.consequence[:150],
                # Whole or none: a cut replacement is a different edit.
                suggestion=f.suggestion if len(f.suggestion) <= 600 else "",
            )
            for f in findings
        ]
    units = dict(state.reviewed_units)
    truncated = state.units_truncated
    if level >= 3 and len(units) > MAX_UNITS // 4:
        units = dict(sorted(units.items())[: MAX_UNITS // 4])
        truncated = True
    held = state.suppressions
    if level >= 2:
        held = tuple(replace(row, note="") for row in held)
    if level >= 3:
        held = tuple(replace(row, title=row.title[:60]) for row in held)
    dropped = state.findings_dropped
    if level >= MAX_DETAIL:
        important = {f.id for f in findings if f.is_open and f.severity in {"p0", "p1"}}
        rest = sorted((f for f in findings if f.id not in important), key=_priority)
        keep = important | {f.id for f in rest[: max(0, MAX_KEPT_AT_LIMIT - len(important))]}
        dropped += len(findings) - len(keep)
        # Titles and places only: enough to carry the finding, and open P0/P1 are never among the dropped.
        findings = [f.changed(explanation="", consequence="", suggestion="") for f in findings if f.id in keep]
        units, truncated = {}, True
    return replace(
        state,
        findings=tuple(findings),
        reviewed_units=units,
        units_truncated=truncated,
        findings_dropped=dropped,
        suppressions=held,
    )


def cap_findings(findings: list[BotFinding]) -> tuple[list[BotFinding], int]:
    """At most :data:`MAX_FINDINGS`, open P0/P1 first; the number dropped is returned, never hidden."""

    if len(findings) <= MAX_FINDINGS:
        return findings, 0
    kept = sorted(findings, key=_priority)[:MAX_FINDINGS]
    kept_ids = {finding.id for finding in kept}
    return [finding for finding in findings if finding.id in kept_ids], len(findings) - len(kept)


def cap_units(units: Mapping[str, str]) -> tuple[dict[str, str], bool]:
    """At most :data:`MAX_UNITS` reviewed units. Truncated units look changed next time, so they are re-reviewed."""

    if len(units) <= MAX_UNITS:
        return dict(units), False
    return dict(sorted(units.items())[:MAX_UNITS]), True
