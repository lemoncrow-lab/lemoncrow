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

SCHEMA: Final[int] = 1
SUMMARY_MARKER: Final[str] = "<!-- lemoncrow:summary v1 -->"
FINDING_MARKER: Final[str] = "<!-- lemoncrow:finding {finding_id} -->"
FINDING_MARKER_PREFIX: Final[str] = "<!-- lemoncrow:finding "
STATE_OPEN: Final[str] = "<!-- lemoncrow:state v1\n"
STATE_CLOSE: Final[str] = "\n-->"
_STATE = re.compile(r"<!-- lemoncrow:state v1\n(.*?)\n-->", re.DOTALL)
_SHA = re.compile(r"^[0-9a-f]{40}$")

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

    def open_findings(self) -> tuple[BotFinding, ...]:
        return tuple(finding for finding in self.findings if finding.is_open)


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
    )


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

    1. closed findings (fixed, stale) keep only their identity and title;
    2. P2 findings drop their long text;
    3. every finding's text is cut to a short form and the reviewed-unit list to :data:`MAX_UNITS` / 4;
    4. the unit list is dropped (the next run re-reviews) and every finding keeps only its title and place;
       open P0/P1 findings are all kept, other findings are capped so 25 remain in total.
    """

    findings = list(state.findings)
    if level >= 1:
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
            f.changed(explanation=f.explanation[:300], consequence=f.consequence[:150], suggestion=f.suggestion[:600])
            for f in findings
        ]
    units = dict(state.reviewed_units)
    truncated = state.units_truncated
    if level >= 3 and len(units) > MAX_UNITS // 4:
        units = dict(sorted(units.items())[: MAX_UNITS // 4])
        truncated = True
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
        state, findings=tuple(findings), reviewed_units=units, units_truncated=truncated, findings_dropped=dropped
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
