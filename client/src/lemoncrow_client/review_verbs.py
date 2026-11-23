"""The terminal verbs of the redesigned Review: ``next``, ``open``, ``answer``, ``note``, ``send``, ``approve``, ``undo``, ``retract``, ``verify``.

SPEC section 6 (terminal parity, B9): every app action has a verb. Each verb makes the same Reader API
calls as the matching button, with the same guards (expected revision ids, the exact preview hash and
operation id for a send, the server-held undo window). Nothing is sent, answered or undone without the
person typing the verb; a coding agent's own shell is refused, as it is for ``--mark``.

Every function takes a *request* callable (``request(method, path, body)``), like ``review_terminal``,
so the same code runs against the configured server and against a test client.
Human text says ``Revision N`` with N one-based; ``--json`` keeps the server's raw numbers.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import secrets
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO
from urllib.parse import quote, unquote, urlsplit

from .errors import ClientError
from .review_terminal import Request, TerminalError, _branch, agent_session_host, current_review_row, revision_label

__all__ = [
    "VERBS",
    "answer_part",
    "next_up",
    "note_part",
    "parse_review_link",
    "plan_send",
    "retract_verdict",
    "send_review",
    "undo_send",
    "verbs_main",
]

VERBS: Final[frozenset[str]] = frozenset(
    {"next", "open", "answer", "note", "send", "approve", "undo", "retract", "verify"}
)
SCHEMA: Final[int] = 1
#: Done's "Next up" compares at most this many open reviews (`ReviewDoneScreen.tsx` NEXT_UP_CANDIDATES).
NEXT_UP_CANDIDATES: Final[int] = 5

#: ``answer`` words to the server's mark states, and the SPEC words the terminal prints for each.
ANSWER_STATES: Final[dict[str, str]] = {"good": "reviewed", "change": "needs_changes", "later": "later"}
ANSWER_WORDS: Final[dict[str, str]] = {
    "reviewed": "Looks good",
    "needs_changes": "Needs a change",
    "later": "Decide later",
}
#: ``--verdict`` words to the outcome kinds the server records, and SPEC's names for them.
VERDICTS: Final[dict[str, str]] = {
    "approve": "lgtm",
    "approve-with-suggestions": "lgtm",
    "request-changes": "changes_requested",
    "comment": "comment",
}
#: What the preview's presentation names: Approve with suggestions is its own verdict in the message (`feedback.py`).
CHOICES: Final[dict[str, str]] = {**VERDICTS, "approve-with-suggestions": "lgtm_with_suggestions"}
VERDICT_WORDS: Final[dict[str, str]] = {
    "lgtm": "Approve",
    "changes_requested": "Request changes",
    "comment": "Comment only",
    "lgtm_with_suggestions": "Approve with suggestions",
}
IMPORTANCE: Final[tuple[str, ...]] = ("must_fix", "suggestion", "question")
IMPORTANCE_WORDS: Final[dict[str, str]] = {"must_fix": "Must fix", "suggestion": "Suggestion", "question": "Question"}
_ANSWERED: Final[frozenset[str]] = frozenset({"reviewed", "needs_changes"})
_ACCEPTED_RULES: Final[frozenset[str]] = frozenset({"generated_files", "lockfiles"})


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _int(value: object) -> int:
    try:
        return int(str(value)) if value is not None and value != "" else 0
    except ValueError:
        return 0


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _base(review_id: str) -> str:
    return f"/api/reviews/{quote(review_id, safe='')}"


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def parse_review_link(text: str) -> tuple[str, str]:
    """``(review slug, tail)`` for a Review address, whatever form it was printed in.

    Accepts a full URL (what ``lc review`` and the GitHub round trip print), ``/r/<review>/...``,
    ``r/<review>`` (the reference ``lc review status`` prints), the prototype's ``#/r/...`` and a bare
    id. ``tail`` is the path under the review plus any query and fragment, exactly as written
    (``/one/auth``, ``/map?lens=data-path``, ``/all#part``), so the Reader opens the same place.
    """

    raw = text.strip()
    if not raw:
        raise TerminalError("name a Review: lc review open /r/<review>")
    if raw.startswith("#"):
        raw = raw[1:]
    split = urlsplit(raw)
    if split.scheme in {"http", "https"}:
        if not split.netloc:
            raise TerminalError(f"{text!r} is not a Review address")
        path, query, fragment = split.path, split.query, split.fragment
    elif split.scheme:
        raise TerminalError(f"{text!r} is not a Review address; write /r/<review> or a link from lc review")
    else:
        path, query, fragment = split.path, split.query, split.fragment
        if not path.startswith("/"):
            path = "/" + path if path.startswith("r/") else "/r/" + path
    segments = path.split("/")
    if len(segments) < 3 or segments[1] != "r" or not segments[2]:
        raise TerminalError(f"{text!r} is not a Review address; write /r/<review> or a link from lc review")
    slug = unquote(segments[2])
    tail = "/".join(segments[3:])
    tail = "/" + tail.rstrip("/") if tail.rstrip("/") else ""
    if query:
        tail += "?" + query
    if fragment:
        tail += "#" + fragment
    return slug, tail


# --------------------------------------------------------------------------- #
# next
# --------------------------------------------------------------------------- #


def _category(target: Mapping[str, Any]) -> str:
    """A part's answer category; unreadable answers are decisions (`readAnswer`)."""

    answer = _dict(target.get("answer"))
    category = answer.get("category")
    if category == "accepted_by_rule" and answer.get("rule") not in _ACCEPTED_RULES:
        return "decision"
    return str(category) if category in {"decision", "quick_check", "accepted_by_rule"} else "decision"


def describe_next_up(targets: Sequence[Mapping[str, Any]], feedback: Mapping[str, Any]) -> dict[str, Any]:
    """`describeNextUp` in `done/nextUp.ts`: is a step left, is it the reviewer's turn, how many decisions."""

    states = [str(item.get("state") or "") for item in targets]
    unjudged = states.count("unreviewed") + states.count("unknown") + states.count("later")
    changed = states.count("changed_since_review")
    needs = states.count("needs_changes")
    unsent = _int(feedback.get("unpublished"))
    with_agent_total = _int(feedback.get("published")) + _int(feedback.get("in_flight"))
    handed_off = with_agent_total > 0 and unsent == 0
    # `targetStep` returns nothing when no part needs a person, or when only requested changes remain
    # and they are already with the fixer.
    has_step = needs + changed + unjudged > 0 and (unjudged + changed > 0 or not handed_off)
    decisions = sum(
        1 for item in targets if _category(item) == "decision" and str(item.get("state") or "") not in _ANSWERED
    )
    return {"has_step": has_step, "your_turn": has_step and not handed_off, "decisions": decisions}


def pick_next_up(candidates: Sequence[dict[str, Any]], *, compared_all: bool) -> dict[str, Any] | None:
    """`pickNextUp`: fewest decisions first among reviews waiting on the reviewer, then directory order."""

    if not candidates:
        return None
    turn = [item for item in candidates if item["your_turn"]]
    pool = turn or list(candidates)
    best = pool[0]
    for item in pool[1:]:
        if item["decisions"] < best["decisions"]:
            best = item
    decisions = f" ({_plural(best['decisions'], 'decision')})" if best["decisions"] > 0 else ""
    strictly_quickest = len(turn) > 1 and any(
        item is not best and item["decisions"] > best["decisions"] for item in turn
    )
    if not best["your_turn"]:
        reason = "waiting on the coding agent" if best["has_step"] else "nothing left to judge"
    elif strictly_quickest:
        reason = f"your turn · {'quickest to finish' if compared_all else 'one of the quickest to finish'}{decisions}"
    else:
        reason = f"your turn{decisions}"
    return {**best, "reason": reason}


def snoozed_now(row: Mapping[str, Any]) -> bool:
    """The viewer's snooze on this review still holds (the server already ended it for a newer Snapshot)."""

    queue = row.get("queue")
    until = queue.get("snoozed_until") if isinstance(queue, Mapping) else ""
    if not isinstance(until, str) or not until:
        return False
    try:
        return datetime.fromisoformat(until.replace("Z", "+00:00")) > datetime.now(UTC)
    except ValueError:
        return False


def _same_review(row: Mapping[str, Any], reference: str) -> bool:
    wanted = reference.removeprefix("r/")
    return (
        bool(wanted) and wanted in {str(row.get("id") or ""), str(row.get("short_id") or "")}
    ) or reference == row.get("ref")


# lc-debt: a second copy of `done/nextUp.ts`, kept equal by `nextUpCases.json`; upgrade: one server next-up endpoint the web and the CLI both read.
def next_up(request: Request, *, after: str = "") -> dict[str, Any]:
    """The review Done's "Next up" would offer: the same open-review list, the same comparison, the same reason.

    *after* leaves one review out (the one just finished). Reads at most five reviews, two calls each.
    """

    listed = _list(request("GET", "/api/reviews?status_filter=open", None).get("reviews"))
    # A review the person snoozed is not offered (it comes back at its time or on a new Snapshot).
    pool = [row for row in listed if not (after and _same_review(row, after)) and not snoozed_now(row)]
    others = pool[:NEXT_UP_CANDIDATES]
    described: list[dict[str, Any]] = []
    for row in others:
        review_id = str(row.get("id") or "")
        try:
            targets = _list(request("GET", f"{_base(review_id)}/targets?order=recommended", None).get("targets"))
        except ClientError:
            continue  # a review whose parts cannot be read still opens; it just carries no reason
        try:
            feedback = _dict(request("GET", f"{_base(review_id)}/annotations", None).get("feedback"))
        except ClientError:
            feedback = {}
        described.append({"review": row, **describe_next_up(targets, feedback)})
    picked = pick_next_up(described, compared_all=len(pool) <= NEXT_UP_CANDIDATES and len(described) == len(others))
    if picked is None and others:
        picked = {"review": others[0], "has_step": False, "your_turn": False, "decisions": 0, "reason": ""}
    if picked is None:
        return {"schema": SCHEMA, "verb": "next", "next": None}
    row = picked["review"]
    ref = str(row.get("ref") or f"r/{row.get('id', '')}")
    return {
        "schema": SCHEMA,
        "verb": "next",
        "next": {
            "id": str(row.get("id") or ""),
            "ref": ref,
            "title": str(row.get("title") or ""),
            "reason": picked["reason"],
            "your_turn": picked["your_turn"],
            "decisions": picked["decisions"],
            "path": f"/{ref}",
        },
    }


# --------------------------------------------------------------------------- #
# a review's current state and its parts
# --------------------------------------------------------------------------- #


def _overview(request: Request, review_id: str, revision: int | None) -> tuple[dict[str, Any], str]:
    """The review's overview and its current revision id; refuses a revision the person did not mean."""

    overview = dict(request("GET", _base(review_id), None))
    current = _dict(overview.get("revision"))
    revision_id = str(current.get("id") or "")
    if not revision_id:
        raise TerminalError("Review server did not return the current Revision identity")
    if revision is not None and revision != _int(current.get("revision_number")) + 1:
        raise TerminalError(
            f"Revision {revision} is not the one open now: this review is on {revision_label(current.get('revision_number'))}. "
            "Nothing was recorded."
        )
    attached = _dict(overview.get("session")).get("hosted")
    if isinstance(attached, Mapping):
        raise TerminalError(
            f"this Review is attached to hosted {attached.get('reference') or 'a hosted Review'}, which owns judgments "
            "and notes; act on it in the Reader"
        )
    return overview, revision_id


def find_part(targets: Sequence[Mapping[str, Any]], text: str) -> dict[str, Any]:
    """The one part *text* names: its id, key or label, or a file path with a single part. Never a guess."""

    wanted = text.strip()
    if not wanted:
        raise TerminalError("name a part: its label, its key, or a file path with one part")
    for field in ("target_id", "unit_key", "label"):
        hits = [item for item in targets if str(item.get(field) or "") == wanted]
        if len(hits) == 1:
            return dict(hits[0])
        if len(hits) > 1:
            break
    else:
        hits = [item for item in targets if str(item.get("path") or "") == wanted]
        if not hits:
            hits = [item for item in targets if str(item.get("label") or "").lower() == wanted.lower()]
        if len(hits) == 1:
            return dict(hits[0])
        if not hits:
            raise TerminalError(f"no part matches {wanted!r}; `lc review --units --no-open` lists the keys")
    names = ", ".join(str(item.get("unit_key") or item.get("label") or "") for item in hits[:5])
    raise TerminalError(f"{wanted!r} names {len(hits)} parts; use one key: {names}")


def _part_name(part: Mapping[str, Any]) -> str:
    return str(part.get("label") or part.get("path") or part.get("unit_key") or "")


def _parts(request: Request, review_id: str) -> list[dict[str, Any]]:
    return _list(request("GET", f"{_base(review_id)}/targets?order=recommended", None).get("targets"))


# --------------------------------------------------------------------------- #
# answer and note
# --------------------------------------------------------------------------- #


def answer_part(
    request: Request, review_id: str, part: str, answer: str, *, revision: int | None = None
) -> dict[str, Any]:
    """Looks good, Needs a change or Decide later on one part: the call the three answer buttons make."""

    if answer not in ANSWER_STATES:
        raise TerminalError(f"answer must be one of {', '.join(ANSWER_STATES)}")
    _, revision_id = _overview(request, review_id, revision)
    target = find_part(_parts(request, review_id), part)
    state = ANSWER_STATES[answer]
    marked = dict(
        request(
            "POST",
            f"{_base(review_id)}/marks",
            {"unit_key": str(target["unit_key"]), "state": state, "expected_revision_id": revision_id},
        )
    )
    if marked.get("queued") is True:
        raise TerminalError("this Review is attached to hosted, which owns judgments; act on it in the Reader")
    return {
        "schema": SCHEMA,
        "verb": "answer",
        "part": {
            "unit_key": str(target["unit_key"]),
            "label": _part_name(target),
            "path": str(target.get("path") or ""),
        },
        "answer": answer,
        "state": state,
        "word": ANSWER_WORDS[state],
        "revision_id": revision_id,
        "marks": _list(marked.get("marks")),
    }


def _note_range(target: Mapping[str, Any]) -> tuple[int, int, str] | None:
    """The lines a note on this part anchors to: `targetCommentRange` in `ReviewReader.tsx`."""

    spans = _list(target.get("spans"))
    preferred = next((span for span in spans if span.get("side") == "new"), spans[0] if spans else None)
    if preferred is not None and _int(preferred.get("start_line")) >= 1:
        side = "additions" if preferred.get("side") == "new" else "deletions"
    elif _int(target.get("start_line")) > 0:
        side = "additions"
    else:
        return None
    start = _int(target.get("start_line"))
    if target.get("kind") == "symbol" and start > 0:
        return start, max(start, _int(target.get("end_line"))), side
    span_side = "old" if side == "deletions" else "new"
    span = next((item for item in spans if item.get("side") == span_side), None)
    if span is not None:
        return _int(span.get("start_line")), _int(span.get("end_line")), side
    if start > 0:
        return start, max(start, _int(target.get("end_line"))), side
    return None


def note_part(request: Request, review_id: str, part: str, text: str, *, revision: int | None = None) -> dict[str, Any]:
    """Add a note for the coding agent to a part: the call "Save note" makes. It leaves the part as Needs a change.

    Nothing is sent: notes go out only from Wrap up (`lc review send`).
    """

    body = text.strip()
    if not body:
        raise TerminalError("a note needs text")
    _, revision_id = _overview(request, review_id, revision)
    target = find_part(_parts(request, review_id), part)
    located = _note_range(target)
    created = dict(
        request(
            "POST",
            f"{_base(review_id)}/annotations",
            {
                "path": str(target.get("path") or ""),
                "start_line": located[0] if located else 0,
                "end_line": located[1] if located else 0,
                "side": located[2] if located else "additions",
                "body": body,
                "kind": "request_change",
                "file_level": located is None,
                # A comparison row is not a part of this revision: the Reader leaves its key out (and the part unmarked).
                **(
                    {}
                    if target.get("fingerprint_method") == "revision_compare"
                    else {"target_unit_key": str(target["unit_key"]), "mark_target": True}
                ),
                "expected_revision_id": revision_id,
            },
        )
    )
    if created.get("queued") is True:
        raise TerminalError("this Review is attached to hosted, which owns notes; act on it in the Reader")
    note = _dict(created.get("annotation"))
    return {
        "schema": SCHEMA,
        "verb": "note",
        "part": {
            "unit_key": str(target["unit_key"]),
            "label": _part_name(target),
            "path": str(target.get("path") or ""),
        },
        "note": {
            "id": str(note.get("id") or ""),
            "start_line": _int(note.get("start_line")),
            "end_line": _int(note.get("end_line")),
        },
        "revision_id": revision_id,
    }


# --------------------------------------------------------------------------- #
# send / approve
# --------------------------------------------------------------------------- #


def _expand_importance(specs: Sequence[str], note_ids: Sequence[str]) -> dict[str, str]:
    """``--importance ID=KIND`` (one note) or ``--importance KIND`` (every note without its own choice)."""

    chosen: dict[str, str] = {}
    default = ""
    for spec in specs:
        name, equals, kind = spec.partition("=")
        if not equals:
            name, kind = "", spec
        kind = kind.strip().replace("-", "_")
        if kind not in IMPORTANCE:
            raise TerminalError(f"importance must be one of must-fix, suggestion, question (got {spec!r})")
        if name:
            if name not in note_ids:
                raise TerminalError(f"{name!r} is not one of the notes this send would carry")
            chosen[name] = kind
        else:
            default = kind
    if default:
        for note_id in note_ids:
            chosen.setdefault(note_id, default)
    return chosen


def plan_send(
    request: Request,
    review_id: str,
    *,
    verdict: str,
    importance: Sequence[str] = (),
    summary: str = "",
    test_with_fix: bool = False,
    revision: int | None = None,
) -> dict[str, Any]:
    """Everything Wrap up shows before Send: the answers, the notes, the verdict, the exact message and why not.

    Reads and previews only. ``blockers`` is what keeps Send disabled in the Reader; ``plan['ready']`` is true
    only with none left. The exact server-prepared message (``markdown``) and its ``feedback_hash`` are what
    :func:`send_review` binds to.
    """

    if verdict not in VERDICTS:
        raise TerminalError(f"choose a verdict: {', '.join(VERDICTS)}")
    outcome = VERDICTS[verdict]
    choice = CHOICES[verdict]
    typed_summary = summary.strip()
    # The Reader writes the same suffix into the summary of an approval with suggestions.
    summary_text = (
        " · ".join(part for part in (typed_summary, "Approved with suggestions") if part)
        if choice == "lgtm_with_suggestions"
        else typed_summary
    )
    overview, revision_id = _overview(request, review_id, revision)
    session = _dict(overview.get("session"))
    targets_payload = request("GET", f"{_base(review_id)}/targets?order=recommended", None)
    targets = _list(targets_payload.get("targets"))
    blockers: list[str] = []
    if _dict(targets_payload).get("complete") is False:
        blockers.append("Still loading all parts")
    if session.get("status") not in {None, "", "open"}:
        blockers.append("This review is not open")

    answered = [
        item for item in targets if str(item.get("state") or "") in _ANSWERED and not item.get("changed_since_mark")
    ]
    unanswered = [
        item
        for item in targets
        if _category(item) == "decision"
        and (str(item.get("state") or "") not in _ANSWERED or item.get("changed_since_mark"))
    ]
    if unanswered:
        blockers.append(f"{_plural(len(unanswered), 'decision')} still need your answer")
    decisions = [item for item in targets if _category(item) == "decision"]
    quick = [item for item in targets if _category(item) == "quick_check"]
    unconfirmed_quick = [item for item in quick if item not in answered]
    # Only a person's decisions and quick checks suggest a verdict; accepted-by-rule parts never do.
    needs_change = sum(1 for item in (*decisions, *quick) if item in answered and item.get("state") == "needs_changes")
    suggested = ""
    if not unanswered:
        if needs_change:
            suggested = "changes_requested"
        elif decisions or (quick and not unconfirmed_quick):
            suggested = "lgtm"

    # The notes the server would send now, by its own preview, never a set built here.
    probe = dict(request("POST", f"{_base(review_id)}/feedback/export", {"expected_revision_id": revision_id}))
    if probe.get("revision_id") != revision_id:
        raise TerminalError("The feedback belongs to a different revision. Nothing was sent.")
    note_ids = list(_dict(probe.get("annotation_versions")))
    annotations = {
        str(item.get("id")): item
        for item in _list(request("GET", f"{_base(review_id)}/annotations", None).get("annotations"))
    }
    # A note the server would send but the notes list did not load is never stood in for.
    notes = [annotations[note_id] for note_id in note_ids if note_id in annotations]
    if len(notes) != len(note_ids):
        blockers.append("Some unsent comments were not loaded; review the feedback before sending")
    chosen = _expand_importance(importance, note_ids)
    missing = [note_id for note_id in note_ids if note_id not in chosen]
    if missing:
        blockers.append(f"Set the importance of {_plural(len(missing), 'note')}")

    delivery = _dict(probe.get("delivery"))
    kind = str(delivery.get("kind") or "")
    destination = str(delivery.get("label") or delivery.get("host") or "")
    unsent = _int(_dict(probe.get("status")).get("unpublished"))
    carries_notes = bool(note_ids) and unsent > 0
    if kind == "github":
        blockers.append("This review goes to GitHub, which asks for its own confirmation in the Reader")
    elif carries_notes and kind not in {"agent", "new_session"}:
        blockers.append(
            "The notes go to a person, not an agent session; copying them is a step in the Reader, with no Undo"
        )
    elif carries_notes and not delivery.get("supported"):
        blockers.append(str(delivery.get("reason") or "The feedback destination cannot receive these notes"))

    if choice == "changes_requested" and not note_ids and not typed_summary:
        blockers.append("Add a note or a summary explaining what needs to change")
    if choice == "lgtm_with_suggestions" and not note_ids:
        blockers.append("Approval with suggestions requires a note")
    must = sum(1 for note_id in note_ids if chosen.get(note_id) == "must_fix")
    mismatch = ""
    if must and outcome == "lgtm":
        mismatch = f"You have {_plural(must, 'must-fix note')}. {VERDICT_WORDS[choice]} anyway?"
    elif not must and outcome == "changes_requested":
        mismatch = "You have no must-fix notes. Request changes anyway?"

    presentation = {
        "verdict": choice,
        "importance": {note_id: chosen[note_id] for note_id in note_ids if note_id in chosen},
        "test_with_fix": bool(test_with_fix and kind in {"agent", "new_session"}),
        "summary": summary_text,
    }
    prepared: dict[str, Any] | None = None
    if carries_notes and not missing:
        prepared = dict(
            request(
                "POST",
                f"{_base(review_id)}/feedback/export",
                {"expected_revision_id": revision_id, "presentation": presentation},
            )
        )
        if prepared.get("revision_id") != revision_id or set(_dict(prepared.get("annotation_versions"))) != set(
            note_ids
        ):
            raise TerminalError("The feedback changed while it was being prepared. Nothing was sent.")
    return {
        "revision_id": revision_id,
        "revision_number": _int(_dict(overview.get("revision")).get("revision_number")),
        "outcome": outcome,
        "choice": choice,
        "verdict_word": VERDICT_WORDS[choice],
        "suggested": suggested,
        "decisions": {
            "total": len(decisions),
            "unanswered": [_part_name(item) for item in unanswered],
            "needs_change": needs_change,
        },
        "notes": [
            {
                "id": str(note.get("id") or ""),
                "path": str(note.get("path") or ""),
                "start_line": _int(note.get("start_line")),
                "body": str(note.get("body") or ""),
                "importance": chosen.get(str(note.get("id") or ""), ""),
            }
            for note in notes
        ],
        "destination": {"kind": kind, "label": destination},
        "carries_notes": carries_notes,
        "presentation": presentation,
        "prepared": prepared,
        "markdown": str(prepared.get("markdown") or "") if prepared else "",
        "feedback_hash": str(prepared.get("feedback_hash") or "") if prepared else "",
        "target_count": len(targets),
        "blockers": blockers,
        "mismatch": mismatch,
        "ready": not blockers,
    }


def send_review(
    request: Request,
    review_id: str,
    plan: Mapping[str, Any],
    *,
    confirm_mismatch: bool = False,
    expected_hash: str = "",
) -> dict[str, Any]:
    """Queue the send Wrap up's Send button queues, after the same re-checks. Call only once the person confirmed.

    Re-prepares the message and refuses unless it is byte-for-byte the previewed one (hash and operation id),
    re-reads the closure, then schedules the server-held send with the previewed hash and operation id. The
    server enforces the final compare and holds the delivery for its undo window.
    """

    if plan.get("blockers"):
        raise TerminalError(f"Not sent. {'; '.join(str(item) for item in plan['blockers'])}")
    if plan.get("mismatch") and not confirm_mismatch:
        raise TerminalError(f"Not sent. {plan['mismatch']} Run again with --confirm-mismatch to send it.")
    revision_id = str(plan["revision_id"])
    prepared = plan.get("prepared")
    shown_hash = str(plan.get("feedback_hash") or "")
    if expected_hash and expected_hash != shown_hash:
        raise TerminalError("Not sent. The preview changed since the one you checked; review the updated text.")
    feedback: dict[str, Any] | None = None
    if isinstance(prepared, Mapping):
        current = dict(
            request(
                "POST",
                f"{_base(review_id)}/feedback/export",
                {"expected_revision_id": revision_id, "presentation": plan["presentation"]},
            )
        )
        if (
            current.get("revision_id") != revision_id
            or current.get("feedback_hash") != prepared.get("feedback_hash")
            or current.get("operation_id") != prepared.get("operation_id")
        ):
            raise TerminalError("Not sent. Feedback changed after the preview. Review the updated text before sending.")
        feedback = {
            key: prepared[key]
            for key in ("revision_id", "operation_id", "feedback_hash", "presentation")
            if key in prepared
        }
    fresh = dict(request("GET", f"{_base(review_id)}/finish", None))
    if fresh.get("status") != "open" or _int(fresh.get("target_count")) != _int(plan.get("target_count")):
        raise TerminalError(
            "Not sent. The review changed while you were on Wrap up. Check the answers and run it again."
        )
    queued = dict(
        request(
            "POST",
            f"{_base(review_id)}/flow-send",
            {
                "expected_revision_id": revision_id,
                "client_operation_id": "fs-" + secrets.token_hex(16),
                "outcome": plan["outcome"],
                "summary": str(_dict(plan.get("presentation")).get("summary") or ""),
                "feedback": feedback,
            },
        )
    )
    return {"schema": SCHEMA, "verb": "send", "send": queued, "plan": _plan_public(plan)}


def _plan_public(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in {"prepared", "markdown"}}


def undo_send(request: Request, review_id: str) -> dict[str, Any]:
    """Undo the pending send: the call the Undo button makes. The server decides whether the window is open."""

    current = dict(request("GET", f"{_base(review_id)}/flow-send", None))
    state = str(current.get("state") or "none")
    if state != "pending":
        return {"schema": SCHEMA, "verb": "undo", "undone": False, "state": state, "send": current}
    try:
        done = dict(
            request("POST", f"{_base(review_id)}/flow-send/{quote(str(current['operation_id']), safe='')}/undo", None)
        )
    except ClientError as exc:
        # The window closed between the read and the call: the server's compare-and-swap said no.
        latest = dict(request("GET", f"{_base(review_id)}/flow-send", None))
        refused = "409" in exc.message or "window has closed" in exc.message or "can't be undone" in exc.message
        closed = (
            refused
            or str(latest.get("state") or "none") != "pending"
            or _seconds_until(str(latest.get("send_after") or "")) <= 0
        )
        return {
            "schema": SCHEMA,
            "verb": "undo",
            "undone": False,
            "state": str(latest.get("state") or "none"),
            "send": latest,
            "error": exc.message,
            "closed": closed,
        }
    return {
        "schema": SCHEMA,
        "verb": "undo",
        "undone": done.get("state") == "undone",
        "state": str(done.get("state")),
        "send": done,
    }


def _clock_utc(stamp: str, *, day: bool = False) -> str:
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        return ""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC" if day else "%H:%M UTC")


def retract_verdict(request: Request, review_id: str) -> dict[str, Any]:
    """Retract the signed record of this revision's verdict: the call the Retract button makes (SPEC 4.18).

    The server decides everything (the 24-hour window on its own clock, the same key, the same person); this only
    picks the newest record of a person's verdict on the current revision and asks.
    """

    status = dict(request("GET", f"{_base(review_id)}/record", None))
    mine = [
        row
        for row in _list(status.get("records"))
        if row.get("decision_kind") == "human_outcome" and row.get("revision_id") == status.get("snapshot_id")
    ]
    base: dict[str, Any] = {"schema": SCHEMA, "verb": "retract", "retracted": False}
    if not mine:
        return {**base, "state": "none"}
    latest = mine[0]
    number = latest.get("snapshot_number")
    base["revision"] = number + 1 if isinstance(number, int) else None
    base["head_sha"] = str(latest.get("head_sha") or "")[:7]
    if isinstance(latest.get("retracted"), Mapping):
        return {**base, "state": "already", "at": str(latest["retracted"].get("at") or "")}
    if not latest.get("retractable"):
        until = str(latest.get("retract_until") or "")
        closed = bool(until) and _seconds_until(until) <= 0
        return {**base, "state": "closed" if closed else "unavailable", "until": until}
    try:
        done = dict(
            request("POST", f"{_base(review_id)}/records/{quote(str(latest['record_id']), safe='')}/retract", None)
        )
    except ClientError as exc:
        return {**base, "state": "refused", "error": exc.message}
    when = dict(done.get("retraction") or {})
    return {
        **base,
        "retracted": True,
        "state": "retracted",
        "at": str(when.get("at") or ""),
        "record_id": latest["record_id"],
    }


def render_retract(result: Mapping[str, Any]) -> str:
    state = str(result.get("state") or "none")
    where = " · ".join(
        part
        for part in (
            f"Revision {result['revision']}" if result.get("revision") else "",
            str(result.get("head_sha") or ""),
            _clock_utc(str(result.get("at") or "")),
        )
        if part
    )
    if state == "retracted":
        return f"You retracted the earlier record · {where}. Sending again signs a new one: lc review send"
    if state == "already":
        return f"Already retracted · {where}. Sending again signs a new one: lc review send"
    if state == "closed":
        return f"Can't retract · the 24 hours from signing ended at {_clock_utc(str(result.get('until') or ''), day=True)}. Nothing was changed."
    if state == "refused":
        return f"Couldn't retract: {result.get('error')}. Nothing was changed."
    if state == "unavailable":
        return "Can't retract this record. Nothing was changed. Only the key that signed it, used by the person who sent it, can retract it."
    return "Nothing to retract. No signed record of your verdict exists for this revision. Nothing was changed."


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


def render_plan(plan: Mapping[str, Any]) -> str:
    """Wrap up in the terminal: answers, verdict, notes with importance, the exact message, what blocks Send."""

    decisions = _dict(plan.get("decisions"))
    revision = revision_label(plan.get("revision_number"))
    lines = [f"Wrap up · {revision}"]
    unanswered = [str(name) for name in decisions.get("unanswered") or []]
    lines.append(
        f"{_plural(_int(decisions.get('total')), 'decision')} · "
        + (f"{len(unanswered)} still need your answer" if unanswered else "all answered")
        + (f" · {_int(decisions.get('needs_change'))} need a change" if decisions.get("needs_change") else "")
    )
    for name in unanswered[:5]:
        lines.append(f"  not answered: {name}")
    if plan.get("suggested"):
        lines.append(f"Suggested from your answers: {VERDICT_WORDS[str(plan['suggested'])]}")
    lines.append(f"Verdict: {plan['verdict_word']}")
    notes = [dict(item) for item in plan.get("notes") or []]
    for note in notes:
        importance = IMPORTANCE_WORDS.get(str(note.get("importance")), "not set")
        where = str(note.get("path") or "") + (f":{note['start_line']}" if note.get("start_line") else "")
        lines.append(f"Note {note['id']} · {importance} · {where}")
        for body_line in str(note.get("body") or "").splitlines() or [""]:
            lines.append(f"    {body_line}")
    destination = _dict(plan.get("destination"))
    if plan.get("carries_notes") and plan.get("markdown"):
        lines += [
            "",
            f"What is sent to {destination.get('label') or 'the coding agent'} (exactly this):",
            str(plan["markdown"]).rstrip("\n"),
            "",
            f"Preview hash {plan['feedback_hash']}",
        ]
    elif not notes:
        lines.append("No notes to send. Your verdict can be recorded without notifying a coding agent.")
    if plan.get("mismatch"):
        lines.append(str(plan["mismatch"]))
    for blocker in plan.get("blockers") or []:
        lines.append(f"Can't send yet: {blocker}")
    return "\n".join(lines)


def render_sent(result: Mapping[str, Any]) -> str:
    """Done's undo-live lines (SPEC section 4.9)."""

    plan = _dict(result.get("plan"))
    send = _dict(result.get("send"))
    destination = _dict(plan.get("destination"))
    where = str(destination.get("label") or "the coding agent")
    revision = revision_label(plan.get("revision_number"))
    word = str(plan.get("verdict_word") or "")
    notes = len(plan.get("notes") or []) if plan.get("carries_notes") else 0
    if notes:
        head, what = (
            f"Sending to {where}…",
            f"Your verdict on {revision} ({word}) will be recorded and {_plural(notes, 'note')} sent",
        )
        tail = ""
    elif plan.get("outcome") == "lgtm":
        head, what = "Recording your approval…", f"Your verdict on {revision} ({word}) will be recorded"
        tail = " LemonCrow never commits or merges."
    else:
        head, what = "Recording your verdict…", f"Your verdict on {revision} ({word}) will be recorded"
        tail = " No notes will be sent."
    seconds = _seconds_until(str(send.get("send_after") or ""))
    return f"{head}\n{what} in {seconds} seconds.{tail}\nUndo with: lc review undo"


def _seconds_until(stamp: str) -> int:
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return 10
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, round((moment - datetime.now(UTC)).total_seconds()))


def render_undo(result: Mapping[str, Any]) -> str:
    state = str(result.get("state") or "none")
    if result.get("undone"):
        return "Undone. Nothing was sent. Back on Wrap up with your answers intact: lc review send"
    # The server refused the undo, or the send is already past its window: whichever state the worker is in.
    if result.get("error") and not result.get("closed"):
        # Not the server's "the window has closed": the send may still be waiting and undoable.
        return f"Couldn't undo: {result['error']}. The send may still be waiting; run lc review undo again."
    if result.get("closed") or state in {"sent", "dispatching", "failed", "uncertain"}:
        return "Sent · can't be undone now"
    if state == "undone":
        return "Already undone. Nothing was sent."
    return "Nothing to undo. No send is waiting."


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #

_USAGE: Final[dict[str, str]] = {
    "next": "lc review next [--after REVIEW] [--json] [--open|--no-open]",
    "open": "lc review open [LINK] [--json] [--open|--no-open] [--repo-root DIR]",
    "answer": "lc review answer PART good|change|later [--review REVIEW] [--revision N] [--json]",
    "note": 'lc review note PART "text" [--review REVIEW] [--revision N] [--json]',
    "send": (
        "lc review send --verdict approve|approve-with-suggestions|request-changes|comment [--importance [ID=]must-fix|suggestion|question]...\n"
        "       [--summary TEXT] [--test-with-fix] [--confirm-mismatch] [--preview-hash HASH] [--yes]\n"
        "       [--review REVIEW] [--revision N] [--json]"
    ),
    "approve": "lc review approve [same options as send, without --verdict]",
    "undo": "lc review undo [--review REVIEW] [--json]",
    "retract": "lc review retract [--review REVIEW] [--json]",
    "verify": "lc review verify RECORD [--keys FILE|URL] [--retraction FILE] [--repo-root DIR] [--json]",
}
_FLAGS: Final[dict[str, frozenset[str]]] = {
    "next": frozenset({"--json", "--open", "--no-open"}),
    "open": frozenset({"--json", "--open", "--no-open"}),
    "answer": frozenset({"--json"}),
    "note": frozenset({"--json"}),
    "send": frozenset({"--json", "--yes", "--test-with-fix", "--confirm-mismatch"}),
    "approve": frozenset({"--json", "--yes", "--test-with-fix", "--confirm-mismatch"}),
    "undo": frozenset({"--json"}),
    "retract": frozenset({"--json"}),
}
_VALUES: Final[dict[str, frozenset[str]]] = {
    "next": frozenset({"--after", "--repo-root"}),
    "open": frozenset({"--repo-root"}),
    "answer": frozenset({"--review", "--revision", "--repo-root"}),
    "note": frozenset({"--review", "--revision", "--repo-root"}),
    "send": frozenset(
        {"--review", "--revision", "--repo-root", "--verdict", "--importance", "--summary", "--preview-hash"}
    ),
    "approve": frozenset({"--review", "--revision", "--repo-root", "--importance", "--summary", "--preview-hash"}),
    "undo": frozenset({"--review", "--repo-root"}),
    "retract": frozenset({"--review", "--repo-root"}),
}
_JUDGMENT_VERBS: Final[frozenset[str]] = frozenset({"answer", "note", "send", "approve", "undo", "retract"})


def _parse(verb: str, arguments: Sequence[str]) -> tuple[list[str], dict[str, list[str]], set[str]]:
    positional: list[str] = []
    values: dict[str, list[str]] = {}
    flags: set[str] = set()
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        name, has_inline, inline = argument.partition("=")
        if name in _VALUES[verb]:
            if not has_inline:
                index += 1
                if index >= len(arguments):
                    raise TerminalError(f"{name} requires a value")
            values.setdefault(name, []).append(inline if has_inline else arguments[index])
        elif argument in _FLAGS[verb]:
            flags.add(argument)
        elif argument.startswith("-") and argument != "-":
            raise TerminalError(f"unknown option for lc review {verb}: {argument}\nUsage: {_USAGE[verb]}")
        else:
            positional.append(argument)
        index += 1
    return positional, values, flags


def _revision(values: Mapping[str, list[str]]) -> int | None:
    if "--revision" not in values:
        return None
    raw = values["--revision"][-1]
    if not raw.isdigit() or int(raw) < 1:
        raise TerminalError(f"--revision is the number you read (Revision 1 is 1), not {raw!r}")
    return int(raw)


#: `lc review verify` answers 0 to 5 for the six outcomes (valid, not valid, unsigned, key not allowed, code
#: changed, retracted). Anything that is not an outcome gets a code of its own, so a script never reads it as one.
_VERIFY_UNAVAILABLE = 10
_VERIFY_INPUT = 11
_VERIFY_USAGE = 12


def _verify_main(arguments: Sequence[str], sink: TextIO) -> int:
    """``lc review verify``: checks an Ed25519 signature, so it runs in the full package (this client has no crypto).

    Not a judgment (it records nothing), so it also runs inside an agent's shell. Without the full package it
    says so and changes nothing.
    """

    try:
        # By name, not by import statement: this client is standard-library only (test_packaging_audit), and the
        # full package is an optional neighbour that may not be installed.
        click = importlib.import_module("click")
        verify_cmd = importlib.import_module("lemoncrow.gateway.cli.commands.review_record_cli").verify_cmd
    except ImportError:
        sink.write(
            "lc review verify needs the full LemonCrow package, which checks the signature. "
            "Install it, or run `lc review verify` from it. Nothing was changed.\n"
        )
        return _VERIFY_UNAVAILABLE
    try:
        with contextlib.redirect_stdout(sink):  # type: ignore[type-var]
            verify_cmd.main(args=list(arguments), prog_name="lc review verify", standalone_mode=False)
    except SystemExit as exc:  # the command ends with its exit code
        return exc.code if isinstance(exc.code, int) else 1
    except click.UsageError as exc:
        sink.write(f"{exc.format_message()}\nUsage: {_USAGE['verify']}\n")
        return _VERIFY_USAGE
    except click.ClickException as exc:
        sink.write(f"{exc.format_message()}\n")
        return _VERIFY_INPUT
    return 0


def verbs_main(
    verb: str,
    arguments: Sequence[str],
    *,
    sink: TextIO,
    request: Request | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> int:
    """Run one terminal verb. Exit 0 done, 1 refused or failed, 2 a usage error or an agent's own shell."""

    if verb == "verify":
        return _verify_main(arguments, sink)
    try:
        positional, values, flags = _parse(verb, arguments)
    except TerminalError as exc:
        sink.write(f"{exc}\n")
        return 2
    as_json = "--json" in flags

    def out(document: Mapping[str, Any], text: str) -> None:
        sink.write((json.dumps(document, sort_keys=True) if as_json else text) + "\n")

    if verb in _JUDGMENT_VERBS:
        host = agent_session_host()
        if host:
            # These record a human's judgment; from the coding agent's own shell they would let it answer as the reviewer.
            sink.write(
                f"lc review {verb} records a human review judgment and cannot run inside an agent session ({host}). "
                "Use the Review Reader or your own terminal.\n"
            )
            return 2
    from .config import load_config
    from .review_session import ReviewSession

    try:
        repo_root = values.get("--repo-root", [""])[-1]
        config = load_config(product="review", cwd=Path(repo_root).expanduser() if repo_root else None)
        if config.hosted and not config.authenticated:
            sink.write("Hosted Review requires sign-in; run `lc auth login` first\n")
            return 1
        remote = ReviewSession(config)
        try:
            reader_url = remote.review_url
            call = request
            if call is None:
                from .review_terminal import session_request

                call = session_request(config)

            def resolve_review(reference: str) -> str:
                if reference:
                    slug, _tail = parse_review_link(reference)
                    return slug
                rows = remote.list_reviews(limit=100, timeout_s=config.request_timeout_s)
                row = current_review_row(rows, config.repo_root, _branch(config.repo_root))
                if row is None:
                    raise TerminalError(
                        f"No Review found for {config.repo_root}. Run `lc review` to capture one "
                        "(or name one with --review)."
                    )
                return str(row.get("id") or "")

            review_values = values.get("--review", [""])
            if verb == "next":
                result = next_up(call, after=values.get("--after", [""])[-1])
                chosen = result["next"]
                url = reader_url(chosen["ref"]) if chosen else ""
                if chosen:
                    chosen["url"] = url
                    text = f"Next up · {chosen['reason']}\n" if chosen["reason"] else "Next up\n"
                    text += f"{chosen['title']}\nReview  {url}"
                else:
                    text = "Queue clear. Nothing waits on you."
                out(result, text)
                _maybe_open(config, url, flags, as_json, sink, bool(chosen))
                return 0
            if verb == "open":
                if len(positional) > 1:
                    raise TerminalError(f"open takes one link\nUsage: {_USAGE['open']}")
                if positional:
                    slug, tail = parse_review_link(positional[0])
                else:
                    slug, tail = resolve_review(""), ""
                url = reader_url(slug) + tail
                out({"schema": SCHEMA, "verb": "open", "review": slug, "url": url}, f"Review  {url}")
                _maybe_open(config, url, flags, as_json, sink, True)
                return 0

            review_id = resolve_review(review_values[-1])
            revision = _revision(values)
            if verb == "answer":
                if len(positional) != 2:
                    raise TerminalError(f"Usage: {_USAGE['answer']}")
                result = answer_part(call, review_id, positional[0], positional[1], revision=revision)
                out(result, f"{result['word']} · {result['part']['label']}")
                return 0
            if verb == "note":
                if len(positional) != 2:
                    raise TerminalError(f"Usage: {_USAGE['note']}")
                result = note_part(call, review_id, positional[0], positional[1], revision=revision)
                out(
                    result,
                    f"Note saved · {result['part']['label']}. Nothing is sent until you send from Wrap up (lc review send).",
                )
                return 0
            if verb == "undo":
                result = undo_send(call, review_id)
                out(result, render_undo(result))
                return 0 if result["undone"] else 1
            if verb == "retract":
                result = retract_verdict(call, review_id)
                out(result, render_retract(result))
                return 0 if result["state"] in {"retracted", "already"} else 1
            return _send_main(verb, call, review_id, revision, positional, values, flags, as_json, sink, confirm)
        finally:
            remote.close()
    except TerminalError as exc:
        sink.write(f"{exc}\n")
        return 1 if not str(exc).startswith("Usage") else 2
    except ClientError as exc:
        sink.write(f"{exc.server_code}: {exc.message}\n")
        return 1


def _confirm_command(
    verb: str, review_id: str, verdict: str, plan: Mapping[str, Any], values: Mapping[str, list[str]], flags: set[str]
) -> str:
    """The exact command that sends the previewed message: same choices, plus the hash and --yes."""

    parts = ["lc", "review", verb]
    if verb != "approve":
        parts += ["--verdict", verdict]
    parts += ["--review", review_id]
    for note in plan.get("notes") or []:
        if note.get("importance"):
            parts += ["--importance", f"{note['id']}={str(note['importance']).replace('_', '-')}"]
    summary = (values.get("--summary") or [""])[-1]
    if summary:
        parts += ["--summary", summary]
    for flag in ("--test-with-fix", "--confirm-mismatch"):
        if flag in flags:
            parts.append(flag)
    parts += ["--preview-hash", str(plan["feedback_hash"]), "--yes"]
    return " ".join(shlex.quote(part) for part in parts)


def _send_main(
    verb: str,
    call: Request,
    review_id: str,
    revision: int | None,
    positional: Sequence[str],
    values: Mapping[str, list[str]],
    flags: set[str],
    as_json: bool,
    sink: TextIO,
    confirm: Callable[[str], bool] | None,
) -> int:
    if positional:
        raise TerminalError(f"Usage: {_USAGE[verb]}")
    verdict = "approve" if verb == "approve" else (values.get("--verdict") or [""])[-1]
    if not verdict:
        raise TerminalError(f"Usage: {_USAGE['send']}")
    plan = plan_send(
        call,
        review_id,
        verdict=verdict,
        importance=values.get("--importance", []),
        summary=(values.get("--summary") or [""])[-1],
        test_with_fix="--test-with-fix" in flags,
        revision=revision,
    )
    if not as_json:
        sink.write(render_plan(plan) + "\n")
    expected = (values.get("--preview-hash") or [""])[-1]
    if plan["blockers"]:
        if as_json:
            sink.write(
                json.dumps({"schema": SCHEMA, "verb": verb, "sent": False, "plan": _plan_public(plan)}, sort_keys=True)
                + "\n"
            )
        return 1
    if "--yes" in flags and plan["carries_notes"] and not expected:
        # --yes alone would send a message bound to a hash the person never saw: check it, then name it.
        command = _confirm_command(verb, review_id, verdict, plan, values, flags)
        message = (
            f"Not sent. --yes needs the preview hash of the message above, so it sends exactly what you checked "
            f"(Preview hash {plan['feedback_hash']}). Run:\n  {command}"
        )
        if as_json:
            sink.write(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "verb": verb,
                        "sent": False,
                        "feedback_hash": plan["feedback_hash"],
                        "command": command,
                        "plan": _plan_public(plan),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        else:
            sink.write(message + "\n")
        return 1
    if "--yes" not in flags:
        question = "Send exactly this?" if plan["carries_notes"] else f"Record {plan['verdict_word']}?"
        if confirm is None and sys.stdin.isatty() and not as_json:
            confirmed = input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
        else:
            confirmed = bool(confirm and confirm(question))
        if not confirmed:
            if as_json:
                sink.write(
                    json.dumps(
                        {"schema": SCHEMA, "verb": verb, "sent": False, "plan": _plan_public(plan)}, sort_keys=True
                    )
                    + "\n"
                )
            else:
                hint = " --preview-hash " + str(plan["feedback_hash"]) if plan["carries_notes"] else ""
                sink.write(f"Nothing sent. Run again with{hint} --yes to send exactly this.\n")
            return 1
    result = send_review(call, review_id, plan, confirm_mismatch="--confirm-mismatch" in flags, expected_hash=expected)
    sink.write((json.dumps(result, sort_keys=True) if as_json else render_sent(result)) + "\n")
    return 0


def _maybe_open(config: Any, url: str, flags: set[str], as_json: bool, sink: TextIO, present: bool) -> None:
    """Open the Reader at *url* unless asked not to; JSON output opens only with ``--open``."""

    if not present or "--no-open" in flags or (as_json and "--open" not in flags):
        return
    from .browser import launch_browser_url, open_by_hand
    from .review import _arm_local_browser

    armed = _arm_local_browser(config, url)
    if not launch_browser_url(url):
        sink.flush()
        sys.stderr.write(open_by_hand(url, armed) + "\n")
