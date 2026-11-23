"""The plain rows a person reads in a signed record's Details (SPEC 4.18), from the payload alone.

One reading of the payload serves the Verify dialog, the Details list and the PDF.
A row appears only when the payload holds the fact it states: nothing is filled in
for a section that was not signed (a v1 record has fewer rows than a v2 one), and
no row claims what the record cannot back (see SPEC 7.1 P6-1 rows F, G).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any, Final

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.record_retraction import RetractState

#: Footer of every record surface (SPEC 4.18).
LEGAL_SENTENCE: Final[str] = (
    "This is a technical signature that proves who reviewed what. It is not a legal e-signature."
)
INTRO: Final[str] = "Proof of what you reviewed, tied to this exact version. It stops being valid if the code changes."

_STATE_WORDS: Final[Mapping[str, str]] = {
    "reviewed": "looks good",
    "needs_changes": "needs a change",
    "later": "decide later",
    "unanswered": "not answered",
}
_VERDICT_WORDS: Final[Mapping[str, str]] = {"approved": "Approve", "changes_requested": "Request changes"}
_KEY_WORDS: Final[Mapping[str, str]] = {"user_key": "personal key", "organization_key": "organization key"}
_NOT_CHECKED: Final[str] = "whether the code behaves correctly · callers in other repositories · runtime paths"
_MAX_LISTED: Final[int] = 8
_DASH: Final[str] = "\u2013"


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def utc_text(instant: str) -> str:
    """``2026-11-04 14:02:11 UTC`` for an aware ISO instant ("" when unreadable)."""

    parsed = parse_timestamp(instant)
    return "" if parsed is None else parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _count(value: Any) -> int:
    """A count from the signed payload; anything that is not a whole number counts as none."""

    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _capped(items: Sequence[str]) -> str:
    shown = list(items[:_MAX_LISTED])
    if len(items) > _MAX_LISTED:
        shown.append(f"{len(items) - _MAX_LISTED} more")
    return "; ".join(shown)


def _ranges(spans: Sequence[tuple[int, int]]) -> str:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return ", ".join(str(a) if a == b else f"{a}{_DASH}{b}" for a, b in merged)


def _lines(start: int, end: int) -> str:
    return f"line {start}" if start == end else f"lines {start}{_DASH}{end}"


def minute_text(instant: str) -> str:
    """``2026-11-05 14:02 UTC`` for an aware ISO instant ("" when unreadable)."""

    parsed = parse_timestamp(instant)
    return "" if parsed is None else parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def retract_row(retract: RetractState, *, viewer_id: str = "") -> str:
    """The Retract row (SPEC 4.18): "until 2026-11-05 14:02 UTC (signed time + 24 h)" while the window is open."""

    if retract.state == "retracted":
        who = "you" if viewer_id and retract.retracted_by == viewer_id else f"@{retract.retracted_by}"
        return f"retracted at {minute_text(retract.retracted_at)} · by {who}"
    until = minute_text(retract.until)
    if not until:
        return ""
    word = "until" if retract.state == "open" else "closed at"
    return f"{word} {until}" + (" (signed time + 24 h)" if retract.full_window else "")


def detail_rows(
    statement: Mapping[str, Any],
    *,
    viewer_id: str = "",
    own_key_id: str = "",
    retract: RetractState | None = None,
) -> list[dict[str, str]]:
    """The plain rows. "you" appears only where it is true: the viewer's own id decided, the viewer's own key signed.

    *retract* is given only for a signed verdict of a person; a landing or merge record has no Retract row.
    """

    predicate = _map(statement.get("predicate"))
    snapshot = _map(predicate.get("snapshot"))
    decision = _map(predicate.get("decision"))
    signing = _map(predicate.get("signing"))
    review = _map(predicate.get("review"))
    actor = _map(decision.get("actor"))
    rows: list[dict[str, str]] = []

    def add(label: str, value: str) -> None:
        if value:
            rows.append({"label": label, "value": value})

    number = snapshot.get("number")
    head = _text(snapshot.get("head_sha"))[:7]
    # The payload counts Snapshots from zero, like the API; people read Revision 1 first.
    revision = f"Revision {number + 1}" if isinstance(number, int) and not isinstance(number, bool) else ""
    add("Version", " · ".join(part for part in (revision, head) if part))

    parts = _map(predicate.get("parts"))
    if parts:
        counts = _map(parts.get("counts"))
        decisions = [row for row in _list(parts.get("decisions")) if isinstance(row, Mapping)]
        # Named as the record line names them ("3 decisions answered"), and a count of zero is left out (review-20.3).
        add(
            "Decisions",
            _capped(
                [f"{_text(row.get('label'))} · {_STATE_WORDS.get(_text(row.get('state')), '')}" for row in decisions]
            ),
        )
        not_confirmed = [_text(_map(row).get("label")) for row in _list(parts.get("not_confirmed"))]
        add("Not checked yet", _capped([f"{label} · quick check not checked yet" for label in not_confirmed]))
        answered = sum(1 for row in decisions if row.get("state") in {"reviewed", "needs_changes"})
        tallies = (
            (answered, "decision answered", "decisions answered"),
            (_count(counts.get("quick_check")), "quick check", "quick checks"),
            (_count(counts.get("accepted_by_rule")), "accepted by rule", "accepted by rule"),
            (_count(parts.get("later")), "left for later", "left for later"),
        )
        add("Parts", " · ".join(f"{n} {one if n == 1 else many}" for n, one, many in tallies if n > 0))

    coverage = _map(predicate.get("seen_coverage"))
    seen = [row for row in _list(coverage.get("seen")) if isinstance(row, Mapping)]
    skipped = _list(coverage.get("skipped"))
    if seen or skipped:
        by_path: dict[str, list[tuple[int, int]]] = {}
        for row in seen:
            start, end = row.get("start"), row.get("end")
            if isinstance(start, int) and isinstance(end, int):
                by_path.setdefault(_text(row.get("path")), []).append((start, end))
        listed = [f"{path}: seen lines {_ranges(spans)}" for path, spans in sorted(by_path.items())]
        if skipped:
            listed.append(f"wrapped up without seeing {len(skipped)} changed block{'s' if len(skipped) != 1 else ''}")
        add("Coverage", _capped(listed) + " (reported by the reviewer, not proof of reading)")

    if "ai_hints" in predicate:
        hints = [row for row in _list(_map(predicate.get("ai_hints")).get("hints")) if isinstance(row, Mapping)]
        by_file: dict[str, list[str]] = {}
        for row in hints:
            by_file.setdefault(_text(row.get("path")), []).append(
                _lines(int(row.get("start_line") or 0), int(row.get("end_line") or 0))
            )
        listed = [
            f"{path}: {len(spans)} hint{'s' if len(spans) != 1 else ''} ({', '.join(spans)})"
            for path, spans in sorted(by_file.items())
        ]
        add("AI in this record", (_capped(listed) or "no hints raised on this revision") + " · never decisions")

    change = _map(predicate.get("change"))
    repo = _text(review.get("repository"))
    session_id = _text(change.get("agent_session_id"))
    host = _text(change.get("agent_host"))
    certainty = _text(change.get("provenance_certainty"))
    where = f"{repo} @ {head}" if repo and head else repo or head
    agent = f"{host or 'agent'} session {session_id}" if session_id else ""
    if agent and certainty not in {"", "none", "exact"}:
        agent += f" ({certainty})"
    add("Change", " · ".join(part for part in (where, revision, agent) if part))

    generation = _text(_map(predicate.get("policy_generation")).get("digest")).removeprefix("policy:")
    add("Policy", f"policy generation {generation[:12]}" if generation else "")

    outcome = _text(decision.get("outcome"))
    actor_id = _text(actor.get("id"))
    who = "you" if viewer_id and actor_id == viewer_id else actor_id
    if decision.get("kind") == "human_outcome":
        add(
            "Your verdict",
            " · ".join(part for part in (_VERDICT_WORDS.get(outcome, outcome), f"by {who}" if who else "") if part),
        )
    else:
        add("Decision", " · ".join(part for part in (outcome.replace("_", " "), f"by {who}" if who else "") if part))

    add("Signed at", utc_text(_text(predicate.get("issued_at"))))
    if signing.get("state") == "signed":
        kind = _KEY_WORDS.get(_text(signing.get("mode")), _text(signing.get("mode")))
        key_id = _text(signing.get("key_id"))
        add(
            "Signed by",
            " · ".join(
                part for part in ("you" if own_key_id and key_id == own_key_id else "", f"{kind} {key_id}") if part
            ),
        )
        add("Saved as", "a signed file (in-toto)")
        if retract is not None:
            add("Retract", retract_row(retract, viewer_id=viewer_id))
    else:
        add("Signed by", "nobody · this record is not signed")
    add("Not checked", _NOT_CHECKED)
    return rows


__all__ = ["INTRO", "LEGAL_SENTENCE", "detail_rows", "minute_text", "retract_row", "utc_text"]
