"""Terminal Review actions and status over the Reader API, for both ``lc review`` command lines.

The installed ``lc`` (this stdlib-only client) and the full CLI of a source checkout
(``lemoncrow.gateway.cli.commands.review``) run the same scriptable loop through this one module:
``--mark``, ``--comment``, ``--feedback``, ``--since-my-review``, ``--finish`` and ``lc review status``.
Every operation is a Reader API call (``/api/reviews/...``) on the configured server, the same calls the
browser makes, so the module takes a *request* callable instead of a transport and needs nothing beyond
the standard library.

Human text says ``Revision N`` with N one-based; JSON keeps the server's raw zero-based numbers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from .errors import ClientError, ErrorCode
from .review_words import part_count, revision_name

if TYPE_CHECKING:
    from .config import ClientConfig

__all__ = [
    "AGENT_SESSION_ENVS",
    "ANNOTATION_KINDS",
    "EXIT_ERROR",
    "EXIT_NOT_READY",
    "EXIT_NO_REVIEW",
    "EXIT_READY",
    "MARK_STATES",
    "STATUS_EXIT_CODES_HELP",
    "TerminalError",
    "agent_session_host",
    "current_review_row",
    "parse_line_target",
    "render_state",
    "render_status",
    "revision_label",
    "run_status",
    "session_request",
    "status_main",
    "terminal_state",
]

#: ``request(method, path, body)``: one Reader API call; raises :class:`ClientError` on failure.
Request = Callable[[str, str, "Mapping[str, Any] | None"], Mapping[str, Any]]
Progress = Callable[[str, str], None]

# Mirrors ``session_models.MARK_STATES`` and ``ANNOTATION_KINDS`` (this client cannot import the model);
# pinned equal by ``tests/gateway/test_cli_review.py``.
MARK_STATES: Final[tuple[str, ...]] = (
    "unreviewed",
    "reviewed",
    "needs_changes",
    "changed_since_review",
    "unknown",
    "later",
)
ANNOTATION_KINDS: Final[tuple[str, ...]] = ("comment", "request_change", "suggestion", "looks_good")

# Mirrors ``lemoncrow.core.foundation.paths.AGENT_SESSION_ENVS``; pinned equal by the same test file.
AGENT_SESSION_ENVS: Final[tuple[tuple[str, str], ...]] = (
    ("CLAUDECODE", "claude"),
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_SESSION_ID", "codex"),
    ("OPENCODE_SESSION_ID", "opencode"),
    ("GITHUB_COPILOT_SESSION_ID", "copilot"),
    ("CURSOR_SESSION_ID", "cursor"),
    ("HERMES_SESSION_ID", "hermes"),
    ("ANTIGRAVITY_SESSION_ID", "antigravity"),
    ("AGY_SESSION_ID", "antigravity"),
    ("LEMONCROW_REMOTE_MCP_SESSION", "remote-mcp"),
)

#: ``lc review status`` exit codes, the same with and without ``--json``.
EXIT_READY: Final[int] = 0
EXIT_NOT_READY: Final[int] = 1
EXIT_NO_REVIEW: Final[int] = 2
EXIT_ERROR: Final[int] = 3
STATUS_EXIT_CODES_HELP: Final[str] = (
    "Exit status: 0 ready, 1 not ready yet (work remains), 2 no Review found for this repository, 3 error."
)
STATUS_SCHEMA: Final[int] = 1

_LINE_TARGET = re.compile(r"^(?P<path>.+?):L(?P<start>\d+)(?:-L?(?P<end>\d+))?$")
#: The Review list read for ``status`` is one page; older Reviews are named with ``--review``.
_STATUS_LIST_LIMIT: Final[int] = 100


class TerminalError(Exception):
    """A terminal Review action that cannot proceed, in one sentence for the person."""


def agent_session_host() -> str:
    """The coding-agent host running this process, or ``""`` without evidence (never a guess)."""

    for name, host in AGENT_SESSION_ENVS:
        if os.environ.get(name, "").strip():
            return host
    return ""


def revision_label(number: object) -> str:
    """``Revision N`` for a person: the server's zero-based number, counted from one."""

    return revision_name(number)


def parse_line_target(target: str) -> tuple[str, int, int]:
    """``"src/app.py:L10-L20"`` -> ``("src/app.py", 10, 20)``; a bare path is refused, never defaulted to line 1."""

    match = _LINE_TARGET.match(target.strip())
    if match is None:
        raise ValueError(f"{target!r} is not a line target; write PATH:L10 or PATH:L10-L20")
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1:
        raise ValueError(f"{target!r} starts before line 1")
    if end < start:
        raise ValueError(f"{target!r} ends before it starts")
    return match.group("path"), start, end


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    try:
        return int(str(value)) if value is not None and value != "" else 0
    except ValueError:
        return 0


def _judged_parts(progress: Mapping[str, Any]) -> int:
    """Parts marked Reviewed or Needs changes on the current revision: a judged part counts as reviewed."""

    return _int(progress.get("reviewed")) + _int(progress.get("needs_changes"))


def annotation_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Reader annotation JSON plus the ``location`` the terminal prints."""

    start = _int(row.get("start_line"))
    end = _int(row.get("end_line")) or start
    path = str(row.get("path") or "")
    location = path if start <= 0 else f"{path}:L{start}" + (f"-L{end}" if end != start else "")
    return {**row, "location": location}


def attached_terminal_refusal(reference: object, url: object) -> str:
    """Why ``--mark``/``--comment``/``--reply-to`` do nothing on a Review attached to hosted (RC5)."""

    where = str(reference or "a hosted Review")
    return (
        f"this Review is attached to hosted {where}, which owns judgments and comments; "
        "terminal --mark, --comment and --reply-to are not queued for sync. "
        + (f"Act on the hosted Review at {url}" if url else "Act on the hosted Review")
        + ", or in the Reader, where your own writes queue for `lc review hosted sync`"
    )


_READ_STEPS: Final[dict[str, tuple[str, str, str]]] = {
    "": ("state_overview", "Loading review overview", "Review overview loaded"),
    "/units": ("state_units", "Loading parts and marks", "Parts and marks loaded"),
    "/annotations": ("state_annotations", "Loading review comments", "Review comments loaded"),
}


def terminal_state(
    request: Request,
    review_id: str,
    *,
    mark_targets: Sequence[str] = (),
    mark_state: str = "reviewed",
    show_units: bool = False,
    show_marks: bool = False,
    comment_body: str | None = None,
    comment_target: str | None = None,
    comment_kind: str = "comment",
    reply_to: str | None = None,
    show_comments: bool = False,
    since_my_review: bool = False,
    send_feedback: bool = False,
    finish_review: bool = False,
    reopen_review: bool = False,
    discard_review: bool = False,
    progress: Progress | None = None,
    warn: Callable[[str], object] = lambda _message: None,
) -> dict[str, Any]:
    """Apply terminal actions through the same Reader API the browser uses and return the Review's state.

    Raises :class:`TerminalError` for an action that cannot run and lets :class:`ClientError` through.
    """

    base = f"/api/reviews/{quote(review_id, safe='')}"

    def call(method: str, suffix: str = "", body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        step = _READ_STEPS.get(suffix) if method == "GET" else None
        if step is not None and progress is not None:
            progress(step[0], step[1])
        result = dict(request(method, f"{base}{suffix}", body))
        if step is not None and progress is not None:
            progress(f"{step[0]}_done", step[2])
        return result

    def parallel_reads(suffixes: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        # Progress stays on the caller thread; every request owns its own client.
        for suffix in suffixes:
            if progress is not None:
                progress(_READ_STEPS[suffix][0], _READ_STEPS[suffix][1])
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=len(suffixes), thread_name_prefix="review-state") as pool:
            futures = {pool.submit(request, "GET", f"{base}{suffix}", None): suffix for suffix in suffixes}
            for future in as_completed(futures):
                suffix = futures[future]
                results[suffix] = dict(future.result())
                if progress is not None:
                    progress(f"{_READ_STEPS[suffix][0]}_done", _READ_STEPS[suffix][2])
        return results

    has_actions = bool(
        mark_targets or comment_body is not None or send_feedback or finish_review or reopen_review or discard_review
    )
    initial_reads = {} if has_actions else parallel_reads(("", "/units", "/annotations"))

    # Snapshot the reviewer-owned frontier before this invocation mutates any judgments: a verdict discarded by
    # capture belongs to the revision the reviewer arrived at and is still reported on the command that
    # advances their frontier past it.
    overview_before = call("GET") if has_actions else initial_reads[""]
    revision_before = _dict(overview_before.get("revision"))
    expected_revision_id = str(revision_before.get("id") or "")
    if not expected_revision_id:
        raise TerminalError("Review server did not return the current Revision identity")
    attached = _dict(overview_before.get("session")).get("hosted")
    if isinstance(attached, Mapping) and (mark_targets or comment_body is not None):
        # Hosted owns judgments and comments once attached (RC5); a terminal write never enters the sync queue.
        raise TerminalError(attached_terminal_refusal(attached.get("reference"), attached.get("url")))

    def refuse_queued(response: Mapping[str, Any]) -> None:
        """A write the server queued for hosted (attached while this ran) is withdrawn, never left queued."""

        if response.get("queued") is not True:
            return
        raw_actions = response.get("actions")
        stranded: list[str] = []
        for action in raw_actions if isinstance(raw_actions, list) else ():
            action_id = str(action.get("id") or "") if isinstance(action, Mapping) else ""
            if not action_id:
                continue
            try:
                call("POST", f"/hosted/actions/{quote(action_id, safe='')}", {"op": "discard"})
            except ClientError:
                stranded.append(action_id)
        message = attached_terminal_refusal(response.get("reference"), None)
        if stranded:
            message += f"; could not withdraw queued action(s) {', '.join(stranded)}: discard them in the Reader"
        else:
            message += "; the write it queued was withdrawn"
        raise TerminalError(message)

    needs_frontier = bool(since_my_review or has_actions)
    frontier_before = call("GET", "/frontier") if needs_frontier else {}

    path_marks: list[dict[str, Any]] = []
    downgraded: list[dict[str, str]] = []
    shadowed: list[dict[str, Any]] = []
    for target in mark_targets:
        marked = call(
            "POST", "/marks", {"unit_key": target, "state": mark_state, "expected_revision_id": expected_revision_id}
        )
        refuse_queued(marked)
        raw_rows = marked.get("marks")
        rows = [dict(item) for item in raw_rows if isinstance(item, Mapping)] if isinstance(raw_rows, list) else []
        labels = [str(item.get("label") or item.get("path") or item.get("unit_key") or "") for item in rows]
        widened = len(rows) > 1 or bool(
            rows and str(rows[0].get("unit_key") or "") != target and labels and labels[0] != target
        )
        if widened:
            warn(f"{target}: recorded on {part_count(len(rows))}" + (f" -- {', '.join(labels)}" if labels else ""))
            path_marks.append(
                {
                    "target": target,
                    "covered": [
                        {"unit_key": str(item.get("unit_key") or ""), "label": label}
                        for item, label in zip(rows, labels, strict=True)
                    ],
                }
            )
        for item in rows:
            if item.get("downgraded"):
                note = str(item.get("note") or "requested verdict was downgraded")
                downgraded.append(
                    {
                        "target": target,
                        "requested": mark_state,
                        "recorded": str(item.get("recorded") or ""),
                        "reason": note,
                    }
                )
                warn(f"{target}: {note}")
        raw_shadowed = marked.get("shadowed")
        if isinstance(raw_shadowed, list) and raw_shadowed:
            shadowed.append({"target": target, "shadowed": raw_shadowed})
            warn(
                f"{target}: also the label of another part; the repository file wins. "
                "Use the other part's key to mark it instead."
            )

    created_annotation: dict[str, Any] | None = None
    if comment_body is not None:
        if reply_to:
            current = call("GET", "/annotations")
            raw_annotations = current.get("annotations")
            parent = (
                next(
                    (
                        dict(item)
                        for item in raw_annotations
                        if isinstance(item, Mapping) and item.get("id") == reply_to
                    ),
                    None,
                )
                if isinstance(raw_annotations, list)
                else None
            )
            if parent is None:
                raise TerminalError(f"no comment {reply_to!r} exists in Review {review_id}")
            path = str(parent.get("path") or "")
            start = _int(parent.get("start_line"))
            end = _int(parent.get("end_line")) or start
        else:
            if not comment_target:
                raise TerminalError("--comment needs --on PATH:L10 (or --reply-to an existing comment)")
            try:
                path, start, end = parse_line_target(comment_target)
            except ValueError as exc:
                raise TerminalError(str(exc)) from exc
        created = call(
            "POST",
            "/annotations",
            {
                "path": path,
                "body": comment_body,
                "kind": comment_kind,
                "parent_id": reply_to or "",
                "file_level": start <= 0,
                "range": {"start_line": start, "end_line": end},
                "expected_revision_id": expected_revision_id,
            },
        )
        refuse_queued(created)
        raw_created = created.get("annotation")
        if isinstance(raw_created, Mapping):
            created_annotation = annotation_row(raw_created)

    feedback = call("POST", "/feedback/export", {}) if send_feedback else None

    closure: dict[str, Any] | None = None
    if finish_review or reopen_review or discard_review:
        wanted = "open" if reopen_review else ("archived" if discard_review else "finished")
        finish_body: dict[str, Any] = {"status": wanted}
        if wanted != "open":
            finish_body["expected_revision_id"] = expected_revision_id
        closure = call("POST", "/finish", finish_body)

    mutated = bool(mark_targets or comment_body is not None or finish_review or reopen_review or discard_review)
    final_reads = (
        parallel_reads(("", "/units", "/annotations") if mutated else ("/units", "/annotations"))
        if has_actions
        else initial_reads
    )
    overview = final_reads[""] if mutated else overview_before
    raw_units = final_reads["/units"].get("units")
    units = [dict(item) for item in raw_units if isinstance(item, Mapping)] if isinstance(raw_units, list) else []
    annotations_payload = final_reads["/annotations"]
    raw_annotations = annotations_payload.get("annotations")
    annotations = (
        [annotation_row(item) for item in raw_annotations if isinstance(item, Mapping)]
        if isinstance(raw_annotations, list)
        else []
    )

    unit_kinds: dict[str, int] = {}
    mark_counts: dict[str, int] = {}
    marks: list[dict[str, Any]] = []
    stale_marks = 0
    for unit in units:
        kind = str(unit.get("kind") or "")
        unit_kinds[kind] = unit_kinds.get(kind, 0) + 1
        recorded = str(unit.get("state") or "unreviewed")
        if recorded != "unreviewed":
            mark_counts[recorded] = mark_counts.get(recorded, 0) + 1
            content_changed = bool(unit.get("changed_since_mark"))
            stale_marks += int(content_changed)
            marks.append(
                {
                    "state": recorded,
                    "content": "changed" if content_changed else "same",
                    "kind": kind,
                    "path": str(unit.get("path") or ""),
                    "symbol": str(unit.get("symbol") or ""),
                    "label": str(unit.get("label") or unit.get("path") or unit.get("unit_key") or ""),
                    "unit_key": str(unit.get("unit_key") or ""),
                }
            )

    session = _dict(overview.get("session"))
    revision = _dict(overview.get("revision"))
    progress_data = _dict(overview.get("progress"))
    stats = _dict(overview.get("stats"))
    discarded_value = frontier_before.get("discarded_verdicts")
    if not isinstance(discarded_value, list) or not discarded_value:
        discarded_value = overview.get("discarded")
    state: dict[str, Any] = {
        "review_id": str(session.get("ref") or f"r/{review_id}"),
        "revision_id": str(revision.get("ref") or (f"rr/{revision.get('id')}" if revision.get("id") else "")),
        "revision_number": _int(revision.get("revision_number")),
        "range_mode": str(revision.get("range_mode") or session.get("range_mode") or ""),
        "degraded": [
            str(item.get("name") or "") for item in (overview.get("degraded") or []) if isinstance(item, Mapping)
        ],
        "file_count": _int(stats.get("files")) or unit_kinds.get("file", 0),
        "unit_count": len(units),
        "unit_kinds": unit_kinds,
        "target_count": _int(overview.get("target_count") or progress_data.get("target_count")),
        "target_progress": progress_data,
        "mark_counts": mark_counts,
        "stale_marks": stale_marks,
        "downgraded_marks": downgraded,
        "annotation_counts": _dict(annotations_payload.get("counts")),
        "discarded_verdicts": list(discarded_value) if isinstance(discarded_value, list) else [],
    }
    if path_marks:
        state["path_marks"] = path_marks
    if shadowed:
        state["shadowed_marks"] = shadowed
    if created_annotation is not None:
        state["created_annotation"] = created_annotation
    if show_units:
        state["units"] = units
    if show_marks:
        state["marks"] = marks
    if show_comments:
        state["annotations"] = annotations
    if feedback is not None:
        state["feedback"] = feedback
    if closure is not None:
        state["closure"] = closure
    if since_my_review:
        state["frontier"] = frontier_before
    return state


# --------------------------------------------------------------------------- #
# terminal text for a tracked Review
# --------------------------------------------------------------------------- #


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def render_state(state: Mapping[str, Any]) -> str:
    """The tracked-Review block printed under a capture: one plain headline, then only what needs a reader.

    The headline counts parts, the same things the Reader counts. Raw unit kinds, Revision ids and
    the range mode stay in ``--json``.
    """

    target_progress = _dict(state.get("target_progress"))
    target_count = _int(state.get("target_count"))
    reviewed = _judged_parts(target_progress)
    lines = [
        "",
        f"{revision_label(state.get('revision_number'))} · {_plural(_int(state.get('file_count')), 'file')} · "
        f"{part_count(target_count)} · {reviewed} reviewed",
    ]
    attention = [
        f"{count} {label}"
        for count, label in (
            (_int(target_progress.get("needs_changes")), "need changes"),
            (_int(target_progress.get("changed_since_review")), "changed since your review"),
            # Set aside with "Decide later": not judged, so it is said out loud rather than counted as done.
            (_int(target_progress.get("later")), "left for later"),
        )
        if count
    ]
    stale = _int(state.get("stale_marks"))
    if stale:
        # Never let a count read as "this much is done" when the content underneath it has since changed.
        attention.append(f"{stale} marked before the content changed")
    if attention:
        lines.append("  " + " · ".join(attention))
    comments = _dict(state.get("annotation_counts"))
    if comments:
        # In the headline, not only under --comments: an objection a reviewer has to ask for gets committed over.
        order = ("open", "orphaned", "obsolete", "resolved")
        parts = [f"{comments[name]} {name}" for name in order if comments.get(name)]
        parts.extend(f"{count} {name}" for name, count in sorted(comments.items()) if name not in order and count)
        if parts:
            lines.append(f"  Comments  {' · '.join(parts)}")

    discarded = state.get("discarded_verdicts") or []
    if discarded:
        # A verdict a human recorded and this run deleted cannot be discovered by looking again later.
        lines.append("")
        lines.append(f"VERDICTS DISCARDED  ({len(discarded)})")
        for item in discarded:
            kind = item.get("kind") or "?"
            lines.append(f"  {item.get('state', ''):<20} {kind:<7} {item.get('label', '')}")
            lines.append(f"    {item.get('reason', '')}")

    created = state.get("created_annotation")
    if isinstance(created, Mapping):
        lines.append("")
        lines.append(f"COMMENT RECORDED  {created.get('id', '')}")
        symbol = str(created.get("symbol") or "")
        lines.append(
            f"  {created.get('kind', ''):<15} {created.get('location', '')}" + (f"  {symbol}" if symbol else "")
        )
        if not created.get("anchored"):
            lines.append(f"  not anchored: {created.get('anchor_detail', '')}")

    units = state.get("units")
    if isinstance(units, list):
        lines.append("")
        # Units, not parts: one part's file, symbol and hunk each carry a key, and --mark takes any of them.
        lines.append(f"UNITS  ({len(units)} keys for files, symbols and hunks; --mark takes any of them)")
        for unit in units:
            lines.append(f"  {unit.get('unit_key', ''):<30} {unit.get('kind', ''):<7} {unit.get('label', '')}")

    marks = state.get("marks")
    if isinstance(marks, list):
        lines.append("")
        lines.append("MARKS")
        if not marks:
            lines.append("  none recorded")
        for mark in marks:
            # kind + label, never the bare path: file, hunk and symbol marks on one file are different claims.
            kind = mark.get("kind") or "?"
            lines.append(f"  {mark['state']:<20} {mark['content']:<8} {kind:<7} {mark['label']}")

    annotations = state.get("annotations")
    if isinstance(annotations, list):
        lines.extend(render_comments(annotations))

    frontier = state.get("frontier")
    if isinstance(frontier, Mapping):
        lines.extend(render_frontier(frontier, state))

    downgraded = state.get("downgraded_marks") or []
    if downgraded:
        lines.append("")
        lines.append("NOT RECORDED AS ASKED")
        for item in downgraded:
            lines.append(f"  {item['target']}: {item['reason']}")

    closure = state.get("closure")
    if isinstance(closure, Mapping):
        lines.extend(render_closure(closure))

    feedback = state.get("feedback")
    if isinstance(feedback, Mapping):
        lines.extend(render_feedback(feedback))
    return "\n".join(lines)


def render_closure(closure: Mapping[str, Any]) -> list[str]:
    """A human status choice beside the exact target-based remainder."""

    status = str(closure.get("status") or "")
    verb = {"finished": "REVIEW FINISHED", "archived": "REVIEW DISCARDED"}.get(status, "REVIEW REOPENED")
    lines = ["", f"{verb}  {part_count(_int(closure.get('target_count')))}"]
    outstanding = [
        ("still unreviewed", _int(closure.get("unreviewed_targets"))),
        ("changed since reviewed", _int(closure.get("changed_since_review"))),
        ("still needs changes", _int(closure.get("needs_changes"))),
        ("unknown identity", _int(closure.get("unknown_targets"))),
        ("left for later", _int(closure.get("later_targets"))),
        ("comments still open", _int(closure.get("open_comments"))),
        ("comments orphaned", _int(closure.get("orphaned_comments"))),
    ]
    lines.append(f"  {'reviewed':<24} {_int(closure.get('reviewed_targets'))}")
    for label, count in outstanding:
        if count:
            lines.append(f"  {label:<24} {count}")
    if status in ("finished", "archived") and not any(count for _, count in outstanding):
        lines.append("  nothing outstanding")
    if status == "archived":
        lines.append("  read-only now; retention may permanently delete this review and its evidence")
    return lines


def render_feedback(feedback: Mapping[str, Any]) -> list[str]:
    """``--feedback``: the bundle, and the fact that nothing was delivered.

    Not boilerplate: a reviewer who read ``FEEDBACK BUNDLE`` and assumed the agent had been told would stop
    watching for a reply that is never coming.
    """

    counts = (
        f"{_int(feedback.get('open'))} open · {_int(feedback.get('orphaned'))} orphaned · "
        f"{_int(feedback.get('resolved'))} resolved"
    )
    return [
        "",
        f"FEEDBACK BUNDLE  ({counts})",
        "  rendered here and nowhere else — nothing was sent to any agent, host or pull request",
        "",
        *str(feedback.get("markdown") or "").rstrip("\n").splitlines(),
    ]


_COMMENT_BODY_CHARS: Final[int] = 96


def render_comments(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The ``--comments`` block: every comment, and *how* it still points anywhere (its anchor rung, in words).

    A comment re-found by a heuristic rung and one whose file never changed are different claims; an unanchored
    or orphaned comment prints its reason rather than being filtered out.
    """

    lines = ["", "COMMENTS"]
    if not rows:
        lines.append("  none recorded")
        return lines
    for row in rows:
        body = " ".join(str(row.get("body") or "").split())
        if len(body) > _COMMENT_BODY_CHARS:
            body = body[: _COMMENT_BODY_CHARS - 1] + "…"
        marker = "  " if not row.get("parent_id") else "    ↳ "
        source = str(row.get("source") or "human").upper().replace("_", " ")
        lines.append(
            f"{marker}{source:<11} {row.get('state', ''):<10} {row.get('kind', ''):<15} {row.get('location', '')}"
        )
        if row.get("title"):
            lines.append(f"    {row['title']}")
        lines.append(f"    {body}")
        anchor = f"    anchor: {row.get('anchor_method_label', '')}"
        if row.get("symbol"):
            # Re-derived on every relocation: where the comment sits *now*.
            anchor = f"{anchor} · in {row['symbol']}"
        elif row.get("origin_symbol"):
            # Nothing was located, so the stored name is history, not an address.
            anchor = f"{anchor} · originally in {row['origin_symbol']}"
        lines.append(anchor)
        if not row.get("anchored"):
            lines.append(f"      {row.get('anchor_detail') or 'no reason recorded'}")
    return lines


_FRONTIER_GROUPS: Final[tuple[tuple[str, str], ...]] = (
    ("changed_since_review", "CHANGED SINCE MY REVIEW"),
    ("new", "NEW SINCE MY LAST REVIEW"),
    ("unresolved", "STILL UNRESOLVED"),
    ("unchanged_reviewed", "UNCHANGED REVIEWED"),
)
_FRONTIER_ROWS: Final[int] = 12


def render_frontier(frontier: Mapping[str, Any], state: Mapping[str, Any]) -> list[str]:
    """The ``--since-my-review`` block: what your marks are worth on this Revision, costliest group first."""

    def rows(key: str) -> list[Mapping[str, Any]]:
        value = frontier.get(key)
        return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []

    current = _int(state.get("revision_number"))
    previous_raw = frontier.get("previous_revision_number")
    previous = _int(previous_raw) if previous_raw is not None else None
    here = revision_label(current)
    titles = dict(_FRONTIER_GROUPS)
    if previous is None:
        # No mark yet: not "first Revision", which would date the reviewer's position to the tree's history.
        # With no last review nothing is "new since" it either, and no judgment exists to change or keep:
        # every unit is simply not reviewed yet.
        headline = f"SINCE MY REVIEW  {here}  (nothing reviewed yet)"
        counts = {"not yet reviewed": len(rows("new")) + _int(frontier.get("not_yet_reviewed"))}
        titles["new"] = "NOT REVIEWED YET"
    else:
        if previous == current:
            headline = f"SINCE MY REVIEW  {here}  (unchanged since your last review)"
        elif previous > current:
            # An undo puts the tree back on a Revision already on file.
            headline = f"SINCE MY REVIEW  {here}  (the tree went back; you last saw {revision_label(previous)})"
        else:
            headline = f"SINCE MY REVIEW  {here}  (you last saw {revision_label(previous)})"
        counts = {
            "changed since my review": len(rows("changed_since_review")),
            "new": len(rows("new")),
            "still unresolved": len(rows("unresolved")),
            "unchanged reviewed": len(rows("unchanged_reviewed")),
            "not yet reviewed": _int(frontier.get("not_yet_reviewed")),
        }

    # Counts are units (files, symbols and hunks), the granularity a mark binds to; the summary line counts parts.
    lines = ["", headline]
    for label, count in counts.items():
        lines.append(f"  {label:<24} {count}")
    open_comments = _int(frontier.get("open_annotations"))
    orphaned = _int(frontier.get("orphaned_annotations"))
    if open_comments or orphaned:
        lines.append(f"  {'comments':<24} {open_comments} open · {orphaned} orphaned")

    for key, _ in _FRONTIER_GROUPS:
        group = rows(key)
        if not group:
            continue
        lines.append("")
        lines.append(f"{titles[key]}  ({len(group)})")
        if key == "unchanged_reviewed":
            lines.append(
                f"  {len(group)} {'judgment' if len(group) == 1 else 'judgments'} still valid — nothing to re-read"
            )
            continue
        for row in group[:_FRONTIER_ROWS]:
            lines.append(f"  {row.get('label', '')}")
        if len(group) > _FRONTIER_ROWS:
            lines.append(f"  … {len(group) - _FRONTIER_ROWS} more")

    left = rows("removed_units")
    if left:
        # A unit that left the review takes any verdict on it out of every other list on this screen.
        lines.append("")
        lines.append(f"LEFT THE REVIEW  ({len(left)})")
        for row in left[:_FRONTIER_ROWS]:
            line = f"  {(row.get('kind') or '?'):<7} {row.get('label', '')}"
            verdict = str(row.get("discarded_state") or "")
            if verdict:
                line = f"{line}  — your '{verdict}' verdict went with it"
            lines.append(line)
        if len(left) > _FRONTIER_ROWS:
            lines.append(f"  … {len(left) - _FRONTIER_ROWS} more")
        if any(row.get("discarded_state") for row in left):
            lines.append("  no longer in the change set — see VERDICTS DISCARDED above")
        else:
            lines.append("  no longer in the change set — nothing here to re-read")

    # Every anchor attempt, including the ones that landed on the line they started on.
    moves = rows("annotation_moves")
    if moves:
        lines.append("")
        lines.append(f"COMMENT ANCHORS  ({len(moves)})")
        for move in moves[:_FRONTIER_ROWS]:
            where = (
                f"L{move.get('from_line')}→L{move.get('to_line')}"
                if move.get("from_line") != move.get("to_line")
                else "same line"
            )
            lines.append(f"  {move.get('status', ''):<11} {move.get('path', '')} {where}")
            lines.append(f"    {move.get('method_label', '')} — {move.get('detail', '')}")
        if len(moves) > _FRONTIER_ROWS:
            lines.append(f"  … {len(moves) - _FRONTIER_ROWS} more")

    notes = frontier.get("notes")
    for note in notes if isinstance(notes, list) else ():
        lines.append(f"  note: {note}")
    return lines


# --------------------------------------------------------------------------- #
# lc review status
# --------------------------------------------------------------------------- #


def _branch(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "symbolic-ref", "--quiet", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    ref = completed.stdout.strip()
    return ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""


def current_review_row(rows: Sequence[Mapping[str, Any]], repo_root: Path, branch: str) -> Mapping[str, Any] | None:
    """The Review ``lc review`` last captured for this checkout: open first, then this branch's active one.

    Matched by the server's recorded worktree root, so another clone or worktree of the same repository is
    never answered for this one. ``None`` when the server has no Review for this checkout.
    """

    resolved = str(repo_root.resolve())
    mine = [row for row in rows if str(row.get("worktree_root") or "") == resolved]
    if not mine:
        return None

    def rank(row: Mapping[str, Any]) -> tuple[bool, bool, bool, str]:
        on_branch = bool(branch) and str(row.get("branch") or "") == branch
        return (
            str(row.get("status") or "") == "open",
            on_branch,
            on_branch and row.get("branch_active") is True,
            str(row.get("updated_at") or ""),
        )

    return max(mine, key=rank)


def _matches(row: Mapping[str, Any], reference: str) -> bool:
    wanted = reference.removeprefix("r/")
    return wanted in {str(row.get("id") or ""), str(row.get("short_id") or "")} or reference == row.get("ref")


def review_status(request: Request, row: Mapping[str, Any], *, reader_url: str) -> dict[str, Any]:
    """Everything ``lc review status`` reports for one Review, with its verdict and exit code."""

    review_id = str(row.get("id") or "")
    base = f"/api/reviews/{quote(review_id, safe='')}"

    def readiness() -> Mapping[str, Any]:
        try:
            return {"available": True, **request("GET", f"{base}/readiness", None)}
        except ClientError as exc:
            # A server without readiness (or one that cannot evaluate it) leaves the verdict to the counts.
            return {"available": False, "error": exc.message}

    def projection() -> Mapping[str, Any]:
        try:
            return dict(request("GET", f"{base}/projection", None))
        except ClientError:
            # A server that cannot build the Brief still answers the counts; Risk is then left out.
            return {}

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="review-status") as pool:
        overview_future = pool.submit(request, "GET", base, None)
        annotations_future = pool.submit(request, "GET", f"{base}/annotations", None)
        readiness_future = pool.submit(readiness)
        projection_future = pool.submit(projection)
        overview = dict(overview_future.result())
        annotations = dict(annotations_future.result())
        ready_payload = dict(readiness_future.result())
        brief = dict(projection_future.result())

    session = _dict(overview.get("session"))
    revision = _dict(overview.get("revision"))
    progress = _dict(overview.get("progress"))
    stats = _dict(overview.get("stats"))
    counts = _dict(annotations.get("counts"))
    feedback = _dict(annotations.get("feedback"))
    total = _int(overview.get("target_count") or progress.get("target_count"))
    targets = {
        "total": total,
        "reviewed": _int(progress.get("reviewed")),
        "needs_changes": _int(progress.get("needs_changes")),
        "changed_since_review": _int(progress.get("changed_since_review")),
        "unreviewed": _int(progress.get("unreviewed")),
        "unknown": _int(progress.get("unknown")),
        "later": _int(progress.get("later")),
    }
    comments = {
        "open": _int(counts.get("open")),
        "orphaned": _int(counts.get("orphaned")),
        "resolved": _int(counts.get("resolved")),
        "obsolete": _int(counts.get("obsolete")),
    }
    comments["unresolved"] = comments["open"] + comments["orphaned"]
    feedback_state = {
        "unsent": _int(feedback.get("unpublished")),
        "in_flight": _int(feedback.get("in_flight")),
        "sent": _int(feedback.get("published")),
        "addressed": _int(feedback.get("addressed")),
    }
    raw_reasons = ready_payload.get("blocking_reasons")
    reasons = [dict(item) for item in raw_reasons if isinstance(item, Mapping)] if isinstance(raw_reasons, list) else []
    work_remains = bool(
        targets["reviewed"] < total
        or targets["needs_changes"]
        or targets["changed_since_review"]
        or comments["unresolved"]
        or feedback_state["unsent"]
    )
    available = ready_payload.get("available") is True
    # The server's readiness is the verdict; without it, the work the counts show decides.
    ready = ready_payload.get("ready") is True if available else not work_remains
    risk = _risk_status(_dict(_dict(brief.get("summary")).get("risk")))
    actions: list[str] = []
    for item in reasons:
        action = str(item.get("action") or "")
        if action and action not in actions:
            actions.append(action)
    return {
        "schema": STATUS_SCHEMA,
        "state": "ready" if ready else "not_ready",
        "exit_code": EXIT_READY if ready else EXIT_NOT_READY,
        "review": {
            "id": review_id,
            "ref": str(session.get("ref") or row.get("ref") or f"r/{review_id}"),
            "title": str(overview.get("title") or row.get("title") or ""),
            "status": str(session.get("status") or row.get("status") or ""),
            "range_mode": str(session.get("range_mode") or row.get("range_mode") or ""),
            "branch": str(row.get("branch") or ""),
        },
        "revision": {
            "id": str(revision.get("id") or ""),
            "ref": str(revision.get("ref") or ""),
            "number": _int(revision.get("revision_number")),
            "latest_number": _int(overview.get("latest_revision_number") or revision.get("revision_number")),
            "label": revision_label(revision.get("revision_number")),
        },
        "files": _int(stats.get("files")),
        "targets": targets,
        "comments": comments,
        "feedback": feedback_state,
        "work_remains": work_remains,
        **risk,
        "readiness": {
            "available": available,
            "ready": (ready_payload.get("ready") is True) if available else None,
            "blocker_count": _int(ready_payload.get("blocker_count")),
            "actions": actions,
            "blocking_reasons": reasons,
            **({"error": str(ready_payload.get("error") or "")} if not available else {}),
        },
        "reader_url": reader_url,
    }


def _risk_status(risk: Mapping[str, Any]) -> dict[str, Any]:
    """``risk`` and ``understanding`` from the Brief's ``summary.risk``; empty when the server sent none."""

    if not risk.get("level"):
        return {}
    understanding = _dict(risk.get("understanding"))
    return {
        "risk": {
            "level": str(risk.get("level") or ""),
            "label": str(risk.get("label") or ""),
            "reason": str(risk.get("reason") or ""),
            "text": str(risk.get("text") or ""),
            "facts": [
                {
                    "text": str(fact.get("text") or ""),
                    "kind": str(fact.get("kind") or ""),
                    "level": str(fact.get("level") or ""),
                    "target_ids": [str(item) for item in fact.get("target_ids") or ()],
                }
                for fact in risk.get("facts") or ()
                if isinstance(fact, Mapping)
            ],
            "not_measured": [str(item) for item in risk.get("not_measured") or ()],
        },
        "understanding": {
            "reviewed": _int(understanding.get("reviewed")),
            "total": _int(understanding.get("total")),
            "risky": understanding.get("risky") is True,
            "text": str(understanding.get("text") or ""),
        },
    }


def render_status(status: Mapping[str, Any]) -> str:
    """``lc review status`` for a person."""

    state = str(status.get("state") or "")
    if state in {"no_review", "error"}:
        return str(status.get("message") or state)
    review = _dict(status.get("review"))
    revision = _dict(status.get("revision"))
    targets = _dict(status.get("targets"))
    comments = _dict(status.get("comments"))
    feedback = _dict(status.get("feedback"))
    readiness = _dict(status.get("readiness"))
    latest = _int(revision.get("latest_number"))
    number = _int(revision.get("number"))
    revision_text = f"{number + 1}" + (f" (latest is {latest + 1})" if latest != number else "")
    total = _int(targets.get("total"))
    judged = _judged_parts(targets)
    reviewed_parts = [f"{judged} of {part_count(total)}"]
    if targets.get("needs_changes"):
        reviewed_parts.append(f"{_int(targets.get('needs_changes'))} need changes")
    if targets.get("changed_since_review"):
        reviewed_parts.append(f"{_int(targets.get('changed_since_review'))} changed since your review")
    if targets.get("later"):
        reviewed_parts.append(f"{_int(targets.get('later'))} left for later")
    unresolved = _int(comments.get("unresolved"))
    comment_text = (
        f"{unresolved} unresolved ({_int(comments.get('open'))} open · {_int(comments.get('orphaned'))} orphaned)"
        if unresolved
        else "none unresolved"
    )
    feedback_counts = [_int(feedback.get(key)) for key in ("unsent", "in_flight", "sent", "addressed")]
    feedback_text = (
        f"{feedback_counts[0]} unsent · {feedback_counts[1]} in flight · "
        f"{feedback_counts[2]} sent · {feedback_counts[3]} addressed"
        if any(feedback_counts)
        else "none"
    )
    # Not reviewed: no judgment on this revision, changed-since-review parts included.
    unreviewed = max(0, total - judged)
    actions = [
        # Finishing is the last step: name the parts still in the way of it.
        f"{part_count(unreviewed)} not reviewed, then {item}" if item == "Finish review" and unreviewed else str(item)
        for item in readiness.get("actions") or ()
        if item
    ]
    if state == "ready":
        ready_text = "yes"
    elif actions:
        ready_text = "no — " + " · ".join(actions[:4]) + (f" · {len(actions) - 4} more" if len(actions) > 4 else "")
    else:
        ready_text = "no — work remains"
    if not readiness.get("available"):
        ready_text += " (readiness unavailable; judged from the counts above)"
    title = str(review.get("title") or "").replace("\n", " ")
    risk = _dict(status.get("risk"))
    understanding = _dict(status.get("understanding"))
    rows = [
        ("Review", f"{review.get('ref', '')} · {review.get('status') or 'open'}"),
        *([("Title", title)] if title else []),
        ("Revision", revision_text),
        ("Reviewed", " · ".join(reviewed_parts)),
        *([("Risk", str(risk.get("text") or ""))] if risk.get("text") else []),
        *([("Understanding", str(understanding.get("text") or ""))] if risk.get("text") else []),
        ("Comments", comment_text),
        ("Feedback", feedback_text),
        ("Ready", ready_text),
        ("Reader", str(status.get("reader_url", ""))),
    ]
    width = max(len(label) for label, _value in rows) + 2
    return "\n".join(f"{label:<{width}}{value}" for label, value in rows)


def run_status(
    request: Request,
    list_reviews: Callable[[int], Sequence[Mapping[str, Any]]],
    reader_url: Callable[[str], str],
    *,
    repo_root: Path,
    review_ref: str = "",
    as_json: bool = False,
    write: Callable[[str], object],
) -> int:
    """``lc review status``: write the report (text or one JSON document) and return its exit code."""

    payload: dict[str, Any]
    try:
        rows = list(list_reviews(_STATUS_LIST_LIMIT))
        if review_ref:
            row = next((item for item in rows if _matches(item, review_ref)), None)
            if row is None:
                # lc-debt: one page of the Review list; upgrade: a server lookup by reference.
                row = {"id": review_ref.removeprefix("r/"), "ref": review_ref}
        else:
            row = current_review_row(rows, repo_root, _branch(repo_root))
        if row is None:
            payload = {
                "schema": STATUS_SCHEMA,
                "state": "no_review",
                "exit_code": EXIT_NO_REVIEW,
                "repo_root": str(repo_root),
                "message": (
                    f"No Review found for {repo_root}. Run `lc review` to capture one "
                    "(or name one with `lc review status --review REF`)."
                ),
            }
        else:
            ref = str(row.get("ref") or f"r/{row.get('id', '')}")
            payload = review_status(request, row, reader_url=reader_url(ref))
    except ClientError as exc:
        state = "no_review" if review_ref and exc.code is ErrorCode.REVIEW_UNKNOWN else "error"
        payload = {
            "schema": STATUS_SCHEMA,
            "state": state,
            "exit_code": EXIT_NO_REVIEW if state == "no_review" else EXIT_ERROR,
            "error": exc.server_code,
            "message": f"Review status failed: {exc.message}",
        }
    write(json.dumps(payload, sort_keys=True) if as_json else render_status(payload))
    return int(payload["exit_code"])


def session_request(config: ClientConfig) -> Request:
    """A :data:`Request` over this client's own Review session: one short-lived session per call (thread-safe)."""

    from .review_session import ReviewSession

    def request(method: str, path: str, body: Mapping[str, Any] | None) -> Mapping[str, Any]:
        remote = ReviewSession(config)
        try:
            return remote.reader_request(method, path, body=body, timeout_s=config.tool_timeout_s)
        finally:
            remote.close()

    return request


def status_main(
    config: ClientConfig, *, review_ref: str = "", as_json: bool = False, write: Callable[[str], object]
) -> int:
    """``lc review status`` against the configured server, for both command lines."""

    from .review_session import ReviewSession

    if config.hosted and not config.authenticated:
        payload = {
            "schema": STATUS_SCHEMA,
            "state": "error",
            "exit_code": EXIT_ERROR,
            "error": ErrorCode.UNAUTHENTICATED.value,
            "message": "Hosted Review requires sign-in; run `lc auth login` first",
        }
        write(json.dumps(payload, sort_keys=True) if as_json else str(payload["message"]))
        return EXIT_ERROR

    def list_reviews(limit: int) -> Sequence[Mapping[str, Any]]:
        remote = ReviewSession(config)
        try:
            return remote.list_reviews(limit=limit, timeout_s=config.request_timeout_s)
        finally:
            remote.close()

    def reader_url(reference: str) -> str:
        remote = ReviewSession(config)
        try:
            return remote.review_url(reference)
        finally:
            remote.close()

    return run_status(
        session_request(config),
        list_reviews,
        reader_url,
        repo_root=config.repo_root,
        review_ref=review_ref,
        as_json=as_json,
        write=write,
    )
