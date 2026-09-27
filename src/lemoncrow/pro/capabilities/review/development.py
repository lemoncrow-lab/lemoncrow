"""Change-level Development: observable multi-agent work bound to one Change.

Review answers "is this Change right?"; Development answers "how was it
produced?" -- by which sessions, with which intent, decisions, edits and
checks.  The two connect causally (a Review feedback handoff can cause a fix)
but never share storage: development events are evidence, annotations are
review work, and nothing here ever creates a comment.

Shape::

    Change (review_sessions.id == change_id)
      ├── development_sessions     who took part, why (purpose) and how (role)
      ├── development_events       append-only, per-Change ``sequence``
      ├── development_projections  incremental reducer state (live)
      └── development_snapshots    revision -> event watermark (frozen)

Three invariants carry the design:

* **Purpose never grants authorship.**  A session is an ``author`` only while
  exact edit evidence for this Change exists; verification makes a
  ``verifier``; an explicitly accepted handoff makes an ``investigator``.
  Presence (same workspace, same hour) is not contribution.
* **Snapshots are immutable.**  ``add_revision`` records the event watermark
  in the same transaction that creates the Snapshot, and the Snapshot's
  Development is ``reduce(events <= watermark)`` forever after.
* **The projection is deterministic and incremental.**  Appends apply only the
  new events to the stored reducer state; no LLM call, no full rescan.

Everything stored here is observable, sanitized and bounded: no diffs, no
stdout/stderr, no tool arguments, no model prompts/responses and no hidden
reasoning.  Text passes through ``redact`` before it is persisted.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from lemoncrow_client.kit.redaction import redact

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ActorType, ReviewActivityKind
    from lemoncrow.pro.capabilities.review.store import ReviewStore

DevelopmentEventKind = Literal[
    "intent",
    "plan",
    "decision",
    "fact",
    "question",
    "blocker",
    "edit",
    "revert",
    "command",
    "test",
    "validation",
    "message",
    "handoff",
    "task_started",
    "task_completed",
]
DEVELOPMENT_EVENT_KINDS: tuple[str, ...] = (
    "intent",
    "plan",
    "decision",
    "fact",
    "question",
    "blocker",
    "edit",
    "revert",
    "command",
    "test",
    "validation",
    "message",
    "handoff",
    "task_started",
    "task_completed",
)
DEVELOPMENT_PURPOSES: tuple[str, ...] = (
    "implement",
    "verify",
    "investigate",
    "review",
    "ask",
    "benchmark",
    "system",
    "",
)
"""Why a session ran.  Descriptive only -- it never establishes authorship."""

DEVELOPMENT_ROLES: tuple[str, ...] = ("author", "verifier", "investigator", "observer", "excluded")
"""What a session's evidence proves.  Only the first three appear in Development."""

VISIBLE_ROLES = frozenset({"author", "verifier", "investigator"})
DEVELOPMENT_BINDINGS: tuple[str, ...] = ("explicit", "provenance", "observed")
_BINDING_RANK = {"observed": 0, "provenance": 1, "explicit": 2}
_ROLE_RANK = {"excluded": -1, "observer": 0, "investigator": 1, "verifier": 2, "author": 3}
_ROLE_ACTIVITY = {
    "author": "started authoring this Change",
    "verifier": "started verifying this Change",
    "investigator": "contributed supporting findings",
}
_HOST_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode": "OpenCode",
    "lemoncode": "LemonCode",
    "cursor": "Cursor",
    "copilot": "Copilot",
    "gemini": "Gemini",
    "hermes": "Hermes",
    "antigravity": "Antigravity",
    "pi": "Pi",
    "ci": "CI",
    "human": "Human",
}


def host_label(host: str) -> str:
    key = host.strip().lower()
    if not key:
        return "Unknown"
    return _HOST_LABELS.get(key) or key[:1].upper() + key[1:]


PROJECTION_VERSION = 1

_MAX_SUMMARY = 500
_MAX_DETAIL = 1600
_MAX_BRIEF_DETAIL = 400
_MAX_DECISIONS = 60
_MAX_OPEN_ITEMS = 60
_MAX_CHECKS = 400
_MAX_PATHS = 2000
_MAX_SESSION_PATHS = 200
_MAX_CAUSATIONS = 200
_MAX_CAUSATION_PATHS = 50
_MAX_PUSH_EVENTS = 200
# Events are folded from SQLite in pages of this many rows: memory stays
# bounded while every fold still covers the whole requested range.
_FOLD_PAGE = 2000
_ACTIVE_WINDOW = timedelta(minutes=15)

_VERIFY_KINDS = frozenset({"test", "validation", "command"})
_CHANGE_KINDS = frozenset({"edit", "revert"})
_DECISION_KINDS = frozenset({"decision", "fact"})
_OPEN_KINDS = frozenset({"question", "blocker"})
_RESOLVED_STATUSES = frozenset({"resolved", "closed", "answered", "cleared"})

# Kinds each evidence-backed role may contribute.  An author's whole observable
# narrative is Development; a verifier contributes checks and lifecycle; an
# investigator contributes only what was explicitly handed off / accepted.
ADMITTED_KINDS: Mapping[str, frozenset[str]] = {
    "author": frozenset(DEVELOPMENT_EVENT_KINDS),
    "verifier": frozenset({"test", "validation", "command", "task_started", "task_completed"}),
    "investigator": frozenset({"handoff", "fact", "decision"}),
}
# Review/Ask sessions are Review work.  If one really edited source, only the
# edits and checks are Development; its conversation stays in Review.
REVIEW_PURPOSE_KINDS = frozenset({"edit", "revert", "test", "validation"})


# --------------------------------------------------------------------------- #
# Records                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DevelopmentSession:
    """One agent/human session bound to one Change."""

    change_id: str
    host: str
    session_id: str
    parent_session_id: str = ""
    model: str = ""
    purpose: str = ""
    role: str = "observer"
    binding: str = "observed"
    task: str = ""
    status: str = ""
    started_at: str = ""
    ended_at: str = ""
    cursor_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> str:
        return session_key(self.host, self.session_id)


@dataclass(frozen=True)
class DevelopmentDraft:
    """A sanitized event before the store assigns its Change-local sequence."""

    kind: str
    at: str
    source_event_id: str
    host: str = ""
    session_id: str = ""
    path: str = ""
    symbol: str = ""
    target_unit_key: str = ""
    summary: str = ""
    detail: str = ""
    status: str = ""
    role: str = ""
    source: str = "run_ledger"
    causation_id: str = ""
    parent_event_id: str = ""
    annotation_id: str = ""
    task_id: str = ""
    order: int = 0
    """Position inside its source; a deterministic tie-break, never persisted."""


@dataclass(frozen=True)
class DevelopmentEvent:
    """One append-only, sanitized Development fact."""

    change_id: str
    sequence: int
    event_id: str
    kind: str
    at: str
    source_event_id: str
    host: str = ""
    session_id: str = ""
    path: str = ""
    symbol: str = ""
    target_unit_key: str = ""
    summary: str = ""
    detail: str = ""
    status: str = ""
    role: str = ""
    source: str = ""
    causation_id: str = ""
    parent_event_id: str = ""
    annotation_id: str = ""
    task_id: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class AppendResult:
    inserted: tuple[DevelopmentEvent, ...]
    cursor: int
    conflicted: bool = False


class DevelopmentUnavailable(RuntimeError):
    """This ReviewStore backend has no Development stream (e.g. hosted Postgres)."""


class DevelopmentCursorConflict(RuntimeError):
    """Another writer advanced a session's ingestion cursor first."""


# --------------------------------------------------------------------------- #
# Sanitization and identity                                                   #
# --------------------------------------------------------------------------- #


def session_key(host: str, session_id: str) -> str:
    return f"{host.strip().lower()}:{session_id.strip()}"


def clean_text(value: object, limit: int = _MAX_SUMMARY, *, single_line: bool = True) -> str:
    """Redact credentials / reasoning markers, collapse, and bound one text field."""

    text = str(redact(str(value or ""))).strip()
    if single_line:
        text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def clean_detail(value: object) -> str:
    return clean_text(value, _MAX_DETAIL, single_line=False)


def normalize_at(value: object) -> str:
    """Return aware-UTC ISO-8601, or ``""`` when *value* carries no instant."""

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def parse_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def stable_id(*parts: object) -> str:
    raw = "\0".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="surrogateescape")).hexdigest()[:32]


def event_id_for(change_id: str, source_event_id: str) -> str:
    return "dev_" + stable_id(change_id, source_event_id)[:24]


def _draft_sort_key(draft: DevelopmentDraft) -> tuple[str, str, str, int, str]:
    # Undated drafts sort first; ``normalize_at`` output is lexically ordered.
    return (draft.at, draft.host, draft.session_id, draft.order, draft.source_event_id)


def sanitize_draft(draft: DevelopmentDraft) -> DevelopmentDraft | None:
    """Final gate before persistence: kind whitelist, redaction and bounds."""

    if draft.kind not in DEVELOPMENT_EVENT_KINDS or not draft.source_event_id:
        return None
    summary = clean_text(draft.summary)
    if not summary:
        return None
    return replace(
        draft,
        at=normalize_at(draft.at),
        host=clean_text(draft.host, 64).lower(),
        session_id=clean_text(draft.session_id, 200),
        path=clean_text(draft.path, 1000),
        symbol=clean_text(draft.symbol, 300),
        target_unit_key=clean_text(draft.target_unit_key, 600),
        summary=summary,
        detail=clean_detail(draft.detail),
        status=clean_text(draft.status, 32).lower(),
        role=draft.role if draft.role in DEVELOPMENT_ROLES else "",
        source=clean_text(draft.source, 32),
        causation_id=clean_text(draft.causation_id, 200),
        parent_event_id=clean_text(draft.parent_event_id, 200),
        annotation_id=clean_text(draft.annotation_id, 200),
        task_id=clean_text(draft.task_id, 200),
        source_event_id=clean_text(draft.source_event_id, 200),
    )


# --------------------------------------------------------------------------- #
# Verification command classification                                         #
# --------------------------------------------------------------------------- #

_TEST_COMMAND = re.compile(
    r"\b(pytest|py\.test|jest|vitest|mocha|playwright\s+test|go\s+test|cargo\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|make\s+test|tox|nox|rspec|phpunit|dotnet\s+test|"
    r"gradlew?\s+test|mvn\s+test)\b"
)
_VALIDATION_COMMAND = re.compile(
    r"\b(mypy|pyright|tsc|ruff|eslint|clippy|golangci(?:-lint)?|black\s+--check|prettier\s+--check|"
    r"cargo\s+(?:check|build)|go\s+(?:build|vet)|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:build|typecheck|lint|check)|"
    r"make\s+(?:lint|typecheck|check|pre-commit|build)|pre-commit\s+run|vite\s+build|git\s+diff\s+--check)\b"
)
_COMMAND_STATUS_PREFIX = re.compile(r"^[✓✗✔✖xX]\s+")


def classify_check(command: str) -> str:
    """``test`` | ``validation`` | ``""`` for a shell command (``""`` = chatter)."""

    text = _COMMAND_STATUS_PREFIX.sub("", command.strip())
    if _TEST_COMMAND.search(text):
        return "test"
    if _VALIDATION_COMMAND.search(text):
        return "validation"
    return ""


def check_key(event: DevelopmentEvent) -> str:
    if event.task_id:
        return event.task_id
    text = _COMMAND_STATUS_PREFIX.sub("", event.summary).removesuffix("…")
    text = re.sub(r"^(?:Next validation:\s*)", "", text)
    return " ".join(text.lower().split())[:240]


# --------------------------------------------------------------------------- #
# Deterministic reducer                                                       #
# --------------------------------------------------------------------------- #


def empty_state() -> dict[str, Any]:
    return {
        "v": PROJECTION_VERSION,
        "cursor": 0,
        "event_count": 0,
        "sessions": {},
        "intent": None,
        "headline": None,
        "plan": None,
        "decisions": [],
        "open_items": {},
        "checks": {},
        "paths": {},
        "causations": {},
        "latest": None,
    }


def _brief(event: DevelopmentEvent) -> dict[str, Any]:
    return {
        "id": event.event_id,
        "sequence": event.sequence,
        "kind": event.kind,
        "summary": event.summary,
        "detail": event.detail[:_MAX_BRIEF_DETAIL],
        "status": event.status,
        "path": event.path,
        "host": event.host,
        "session_id": event.session_id,
        "role": event.role,
        "at": event.at,
        "causation_id": event.causation_id,
        "annotation_id": event.annotation_id,
    }


def _append_unique(items: list[str], value: str, cap: int) -> None:
    if value and value not in items and len(items) < cap:
        items.append(value)


def _open_key(event: DevelopmentEvent) -> str:
    text = re.sub(r"^(?:open question|question|blocker)\s*:\s*", "", event.summary, flags=re.IGNORECASE)
    return event.kind + ":" + " ".join(text.lower().split())[:240]


def apply_event(state: dict[str, Any], event: DevelopmentEvent) -> None:
    """Fold one event into *state* in O(1) amortized time.  Mutates *state*."""

    state["cursor"] = max(int(state.get("cursor") or 0), event.sequence)
    state["event_count"] = int(state.get("event_count") or 0) + 1
    brief = _brief(event)
    key = session_key(event.host, event.session_id) if event.session_id else ""
    session: dict[str, Any] | None = None
    if key:
        sessions: dict[str, Any] = state["sessions"]
        session = sessions.get(key)
        if session is None:
            session = {
                "host": event.host,
                "session_id": event.session_id,
                "first_at": event.at,
                "last_at": event.at,
                "first_sequence": event.sequence,
                "last_sequence": event.sequence,
                "event_count": 0,
                "edits": 0,
                "reverts": 0,
                "paths": [],
                "evidence": {"edit": False, "verify": False, "support": False},
                "status": "",
                "task": "",
                "intent": "",
                "latest_message": "",
            }
            sessions[key] = session
        session["event_count"] += 1
        session["last_sequence"] = event.sequence
        if event.at and (not session["last_at"] or event.at > session["last_at"]):
            session["last_at"] = event.at
        if event.at and (not session["first_at"] or event.at < session["first_at"]):
            session["first_at"] = event.at

    kind = event.kind
    if kind in _CHANGE_KINDS and event.path:
        paths: dict[str, Any] = state["paths"]
        row = paths.get(event.path)
        if row is None and len(paths) < _MAX_PATHS:
            row = {"path": event.path, "status": "", "sessions": [], "edits": 0, "last_at": "", "last_sequence": 0}
            paths[event.path] = row
        if row is not None:
            row["status"] = "reverted" if kind == "revert" else "changed"
            row["edits"] += 1
            row["last_at"] = event.at or row["last_at"]
            row["last_sequence"] = event.sequence
            if key:
                _append_unique(row["sessions"], key, 32)
        if session is not None:
            session["evidence"]["edit"] = True
            session["edits" if kind == "edit" else "reverts"] += 1
            _append_unique(session["paths"], event.path, _MAX_SESSION_PATHS)
    elif kind in _VERIFY_KINDS:
        checks: dict[str, Any] = state["checks"]
        name = check_key(event)
        if event.status in _RESOLVED_STATUSES:
            checks.pop(name, None)
        elif name:
            checks.pop(name, None)  # re-insert: dict order == recency
            checks[name] = {**brief, "name": name}
            while len(checks) > _MAX_CHECKS:
                checks.pop(next(iter(checks)))
        if session is not None:
            session["evidence"]["verify"] = True
    elif kind in _DECISION_KINDS or kind == "plan":
        if kind == "plan":
            state["plan"] = brief
        else:
            decisions: list[dict[str, Any]] = state["decisions"]
            decisions.append(brief)
            if len(decisions) > _MAX_DECISIONS:
                del decisions[: len(decisions) - _MAX_DECISIONS]
        if session is not None and event.role == "investigator":
            session["evidence"]["support"] = True
    elif kind in _OPEN_KINDS:
        items: dict[str, Any] = state["open_items"]
        name = _open_key(event)
        if event.status in _RESOLVED_STATUSES:
            items.pop(name, None)
        else:
            items.pop(name, None)
            items[name] = brief
            while len(items) > _MAX_OPEN_ITEMS:
                items.pop(next(iter(items)))
    elif kind == "intent":
        if session is not None and not session["intent"]:
            session["intent"] = event.summary
        if state.get("intent") is None and event.role in {"author", ""}:
            state["intent"] = brief
    elif kind == "message":
        if session is not None:
            session["latest_message"] = event.summary
        if event.role == "author":
            state["headline"] = brief
    elif kind in {"task_started", "task_completed"}:
        if session is not None:
            session["status"] = "running" if kind == "task_started" else (event.status or "done")
            if kind == "task_started" and not session["task"]:
                session["task"] = event.summary
    elif kind == "handoff":
        if session is not None and event.role == "investigator":
            session["evidence"]["support"] = True

    if kind == "handoff" and event.causation_id:
        causation = _causation(state, event.causation_id, event)
        if causation is not None:
            _append_unique(causation["annotation_ids"], event.annotation_id, 50)
            if key:
                _append_unique(causation["sessions"], key, 16)
            if not causation["summary"]:
                causation["summary"] = event.summary
            causation["handoff_status"] = event.status
    elif event.causation_id:
        causation = _causation(state, event.causation_id, event)
        if causation is not None:
            causation["last_sequence"] = event.sequence
            causation["last_at"] = event.at or causation["last_at"]
            if key:
                _append_unique(causation["sessions"], key, 16)
            if kind in _CHANGE_KINDS:
                causation["edits"] += 1
                _append_unique(causation["paths"], event.path, _MAX_CAUSATION_PATHS)
            elif kind in _VERIFY_KINDS and event.status in {"pass", "fail"}:
                causation["checks"][event.status] += 1

    state["latest"] = brief


def _causation(state: dict[str, Any], causation_id: str, event: DevelopmentEvent) -> dict[str, Any] | None:
    causations: dict[str, Any] = state["causations"]
    row = causations.get(causation_id)
    if row is None:
        if len(causations) >= _MAX_CAUSATIONS:
            return None
        row = {
            "causation_id": causation_id,
            "annotation_ids": [],
            "sessions": [],
            "summary": "",
            "handoff_status": "",
            "edits": 0,
            "paths": [],
            "checks": {"pass": 0, "fail": 0},
            "first_sequence": event.sequence,
            "last_sequence": event.sequence,
            "first_at": event.at,
            "last_at": event.at,
        }
        causations[causation_id] = row
    return row


def reduce_events(events: Sequence[DevelopmentEvent], state: dict[str, Any] | None = None) -> dict[str, Any]:
    folded = state if state is not None else empty_state()
    for event in events:
        apply_event(folded, event)
    return folded


def _session_role(summary: Mapping[str, Any]) -> str:
    evidence = summary.get("evidence") or {}
    if evidence.get("edit"):
        return "author"
    if evidence.get("verify"):
        return "verifier"
    if evidence.get("support"):
        return "investigator"
    return "observer"


def _count(value: int, one: str, many: str = "") -> str:
    return f"{value} {one if value == 1 else (many or one + 's')}"


def _join(names: Sequence[str]) -> str:
    items = list(dict.fromkeys(name for name in names if name))
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _files(paths: Sequence[str], total: int) -> str:
    names = [path.rsplit("/", 1)[-1] for path in paths[:3]]
    more = max(0, total - len(names))
    text = _join(names)
    return f"{text} and {more} more" if more else text


def _labels(contributors: Sequence[Mapping[str, Any]]) -> list[str]:
    """Host labels, disambiguated by model when one host appears twice."""

    hosts = [
        host_label(str(item.get("host") or "")) + (" subagent" if item.get("parent_session_id") else "")
        for item in contributors
    ]
    labels: list[str] = []
    for item, host in zip(contributors, hosts, strict=True):
        model = str(item.get("model") or "")
        labels.append(f"{host} ({model})" if hosts.count(host) > 1 and model else host)
    return labels


def _narrative(
    scope: str,
    contributors: Sequence[Mapping[str, Any]],
    *,
    file_count: int,
    tallies: Mapping[str, int],
    counts: Mapping[str, int],
    feedback: Sequence[Mapping[str, Any]],
    unattributed: Sequence[str],
) -> list[str]:
    """A short written account of the projection, one sentence per fact.

    Deterministic templates over the same reduced state the rest of the
    projection shows -- no model call, nothing the numbers do not say.
    """

    lines: list[str] = []
    lead = "Since this Snapshot, " if scope == "since" else ""
    authors = [item for item in contributors if item.get("role") == "author"]
    if authors:
        labels = _labels(authors)
        changed = f"{lead}{_join(labels)} changed {_count(file_count, 'file')}"
        if len(authors) == 1:
            paths = list(authors[0].get("paths") or [])
            detail = _files(paths, int(authors[0].get("path_count") or len(paths)))
            lines.append(f"{changed}: {detail}." if detail else f"{changed}.")
        else:
            parts = [
                f"{label}: {_files(list(item.get('paths') or []), int(item.get('path_count') or 0))}"
                for label, item in zip(labels, authors, strict=True)
                if item.get("paths")
            ]
            lines.append(f"{changed} — {'; '.join(parts[:4])}." if parts else f"{changed}.")

    passed, failed, pending = (int(tallies.get(key) or 0) for key in ("pass", "fail", "pending"))
    total = passed + failed + pending
    if total:
        runners = _labels([item for item in contributors if sum(dict(item.get("checks") or {}).values()) > 0])
        if failed:
            sentence = f"{failed} of {_count(total, 'check')} failed"
            if pending:
                sentence += f" and {pending} {'is' if pending == 1 else 'are'} still pending"
        elif passed and pending:
            sentence = f"{_count(passed, 'check')} passed and {pending} {'is' if pending == 1 else 'are'} still pending"
        elif passed:
            sentence = f"All {_count(passed, 'check')} passed" if passed > 1 else "1 check passed"
        else:
            sentence = f"{_count(pending, 'check')} {'is' if pending == 1 else 'are'} still pending"
        if runners:
            sentence += f" (run by {_join(runners)})"
        lines.append((lead + sentence[:1].lower() + sentence[1:] if lead and not authors else sentence) + ".")

    investigators = [item for item in contributors if item.get("role") == "investigator"]
    if investigators:
        names = _labels(investigators)
        article = "A " if len(set(names)) == 1 and names[0].endswith(" subagent") else ""
        lines.append(f"{article}{_join(names)} contributed supporting findings.")

    decisions, open_items = int(counts.get("decisions") or 0), int(counts.get("open_items") or 0)
    if decisions and open_items:
        lines.append(f"{_count(decisions, 'decision')} recorded; {_count(open_items, 'item')} still open.")
    elif decisions:
        lines.append(f"{_count(decisions, 'decision')} recorded; nothing is left open.")
    elif open_items:
        lines.append(f"{_count(open_items, 'item')} still open.")

    for item in feedback[:3]:
        edits = int(item.get("edit_count") or 0)
        included = item.get("included_in_revision")
        if edits and included is not None and scope != "since":
            lines.append(f"Review feedback was addressed in Snapshot {included} ({_count(edits, 'edit')}).")
        elif edits:
            lines.append(f"Review feedback is being addressed ({_count(edits, 'edit')} so far).")
        else:
            targets = _join([host_label(str(key).split(":", 1)[0]) for key in item.get("sessions") or []])
            lines.append(f"Review feedback was sent{f' to {targets}' if targets else ''}; no follow-up edits yet.")

    if unattributed:
        verb = "has" if len(unattributed) == 1 else "have"
        lines.append(
            f"{_count(len(unattributed), 'changed file')} {verb} no recorded author: "
            f"{_files(list(unattributed), len(unattributed))}."
        )
    working = [item for item in contributors if item.get("active")]
    if scope == "since" and working:
        names = _labels(working)
        lines.append(f"{_join(names)} {'is' if len(set(names)) == 1 else 'are'} still working.")
    return lines


def render_projection(
    state: Mapping[str, Any],
    *,
    scope: str,
    source: str,
    sessions: Mapping[str, DevelopmentSession] | None = None,
    attributions: Sequence[Mapping[str, Any]] = (),
    snapshot_paths: Sequence[str] | None = None,
    revision_watermarks: Sequence[tuple[int, int]] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Public DevelopmentProjection shape, identical for events and legacy input.

    ``attributions`` are the Snapshot's exact edit-time contributor rows; a
    session they name that the event stream never saw is still an author (its
    paths come from the attribution).  ``snapshot_paths`` enables the honest
    gap: changed source no exact evidence explains is ``unattributed``, never
    silently credited to whichever agent is nearby.
    """

    meta = sessions or {}
    raw_sessions: Mapping[str, Any] = state.get("sessions") or {}
    checks: Mapping[str, Any] = state.get("checks") or {}
    contributors: dict[str, dict[str, Any]] = {}
    for key, summary in raw_sessions.items():
        role = _session_role(summary)
        if role not in VISIBLE_ROLES:
            continue
        info = meta.get(key)
        tally = {"pass": 0, "fail": 0, "pending": 0}
        for check in checks.values():
            if session_key(str(check.get("host") or ""), str(check.get("session_id") or "")) != key:
                continue
            bucket = _check_bucket(str(check.get("status") or ""))
            tally[bucket] += 1
        last_at = str(summary.get("last_at") or "")
        status = str(summary.get("status") or "")
        active = False
        if now is not None and status not in {"done", "pass", "fail", "failed"}:
            last = parse_at(last_at)
            active = last is not None and now - last <= _ACTIVE_WINDOW
        paths = list(summary.get("paths") or [])
        contributors[key] = {
            "key": key,
            "host": str(summary.get("host") or ""),
            "session_id": str(summary.get("session_id") or ""),
            "model": info.model if info is not None else "",
            "purpose": info.purpose if info is not None else "",
            "parent_session_id": info.parent_session_id if info is not None else "",
            "role": role,
            "paths": paths[:12],
            "path_count": len(paths),
            "edit_count": int(summary.get("edits") or 0),
            "checks": tally,
            "first_at": str(summary.get("first_at") or ""),
            "last_at": last_at,
            "status": status,
            "task": str(summary.get("task") or "") or (info.task if info is not None else ""),
            "intent": str(summary.get("intent") or ""),
            "latest_message": str(summary.get("latest_message") or ""),
            "active": active,
            "evidence": "events",
            "first_sequence": int(summary.get("first_sequence") or 0),
        }

    for attribution in attributions:
        if str(attribution.get("certainty") or "") != "exact":
            continue
        host = str(attribution.get("host") or "").strip().lower()
        session_id = str(attribution.get("session_id") or "").strip()
        path = str(attribution.get("scope_key") or "").strip()
        if not host or not session_id:
            continue
        key = session_key(host, session_id)
        row = contributors.get(key)
        if row is None:
            info = meta.get(key)
            row = {
                "key": key,
                "host": host,
                "session_id": session_id,
                "model": str(attribution.get("model") or "") or (info.model if info is not None else ""),
                "purpose": info.purpose if info is not None else "",
                "parent_session_id": info.parent_session_id if info is not None else "",
                "role": "author",
                "paths": [],
                "path_count": 0,
                "edit_count": 0,
                "checks": {"pass": 0, "fail": 0, "pending": 0},
                "first_at": "",
                "last_at": "",
                "status": "",
                "task": info.task if info is not None else "",
                "intent": "",
                "latest_message": "",
                "active": False,
                "evidence": "attribution",
                "first_sequence": 1 << 30,
            }
            contributors[key] = row
        row["role"] = "author"
        if not row["model"]:
            row["model"] = str(attribution.get("model") or "")
        if path and path not in row["paths"] and path != "":
            if len(row["paths"]) < 12:
                row["paths"].append(path)
            row["path_count"] = max(int(row["path_count"]), len(row["paths"]))

    role_order = {"author": 0, "verifier": 1, "investigator": 2}
    ordered = sorted(
        contributors.values(),
        key=lambda item: (role_order.get(str(item["role"]), 9), int(item["first_sequence"]), str(item["key"])),
    )
    for item in ordered:
        item.pop("first_sequence", None)

    tallies = {"pass": 0, "fail": 0, "pending": 0}
    check_rows: list[dict[str, Any]] = []
    for check in checks.values():
        bucket = _check_bucket(str(check.get("status") or ""))
        tallies[bucket] += 1
        check_rows.append(
            {
                "name": str(check.get("name") or check.get("summary") or ""),
                "summary": str(check.get("summary") or ""),
                "kind": str(check.get("kind") or ""),
                "status": bucket,
                "detail": str(check.get("detail") or ""),
                "host": str(check.get("host") or ""),
                "session_id": str(check.get("session_id") or ""),
                "at": str(check.get("at") or ""),
                "sequence": int(check.get("sequence") or 0),
            }
        )
    severity = {"fail": 0, "pending": 1, "pass": 2}
    check_rows.sort(key=lambda item: (severity[item["status"]], -int(item["sequence"])))

    raw_paths: Mapping[str, Any] = state.get("paths") or {}
    snapshot_set = frozenset(snapshot_paths or ())
    changed_paths = [
        {
            "path": path,
            "status": str(row.get("status") or "changed"),
            "sessions": list(row.get("sessions") or []),
            "edits": int(row.get("edits") or 0),
            "last_at": str(row.get("last_at") or ""),
            "in_snapshot": (path in snapshot_set) if snapshot_paths is not None else None,
        }
        for path, row in sorted(raw_paths.items())
    ][:500]
    attributed = {path for path in raw_paths}
    for item in ordered:
        if item["role"] == "author":
            attributed.update(item["paths"])
    unattributed = sorted(path for path in snapshot_set if path not in attributed) if snapshot_paths is not None else []

    decisions = list(reversed(list(state.get("decisions") or [])))[:20]
    open_items = list(reversed(list((state.get("open_items") or {}).values())))[:20]
    intent = state.get("intent") or None
    headline_row = state.get("headline") or None
    authors = [item for item in ordered if item["role"] == "author"]
    file_count = len(raw_paths) or len({path for item in authors for path in item["paths"]})
    # With several authors, the handoff line is the lead author's latest
    # visible message (most edits, then most recent) -- deterministic, and
    # never whichever agent happened to speak last.
    lead = max(
        (
            summary
            for key, summary in raw_sessions.items()
            if _session_role(summary) == "author" and str(summary.get("latest_message") or "")
        ),
        key=lambda summary: (int(summary.get("edits") or 0), int(summary.get("last_sequence") or 0)),
        default=None,
    )
    if lead is not None:
        headline = str(lead.get("latest_message") or "")
    elif headline_row:
        headline = str(headline_row.get("summary") or "")
    elif intent:
        headline = str(intent.get("summary") or "")
    elif authors:
        headline = (
            f"{len(authors)} contributor{'s' if len(authors) != 1 else ''} changed "
            f"{file_count} file{'s' if file_count != 1 else ''}."
        )
    else:
        headline = ""

    feedback: list[dict[str, Any]] = []
    watermarks = sorted(revision_watermarks, key=lambda item: item[1])
    for causation in (state.get("causations") or {}).values():
        last_sequence = int(causation.get("last_sequence") or 0)
        included = next((number for number, watermark in watermarks if watermark >= last_sequence), None)
        feedback.append(
            {
                "causation_id": str(causation.get("causation_id") or ""),
                "annotation_ids": list(causation.get("annotation_ids") or []),
                "sessions": list(causation.get("sessions") or []),
                "summary": str(causation.get("summary") or ""),
                "handoff_status": str(causation.get("handoff_status") or ""),
                "edit_count": int(causation.get("edits") or 0),
                "paths": list(causation.get("paths") or []),
                "checks": dict(causation.get("checks") or {"pass": 0, "fail": 0}),
                "first_at": str(causation.get("first_at") or ""),
                "last_at": str(causation.get("last_at") or ""),
                "included_in_revision": included,
            }
        )

    counts = {
        "authors": len(authors),
        "verifiers": sum(1 for item in ordered if item["role"] == "verifier"),
        "investigators": sum(1 for item in ordered if item["role"] == "investigator"),
        "files": file_count,
        "decisions": len(state.get("decisions") or []),
        "open_items": len(state.get("open_items") or {}),
    }
    event_count = int(state.get("event_count") or 0)
    narrative = _narrative(
        scope,
        ordered,
        file_count=file_count,
        tallies=tallies,
        counts=counts,
        feedback=feedback,
        unattributed=unattributed,
    )
    return {
        "scope": scope,
        "source": source,
        "available": bool(event_count or ordered),
        "headline": headline,
        "narrative": narrative,
        "intent": str(intent.get("summary") or "") if intent else "",
        "plan": dict(state["plan"]) if state.get("plan") else None,
        "contributors": ordered,
        "counts": counts,
        "decisions": decisions,
        "open_items": open_items,
        "verification": {**tallies, "checks": check_rows[:40]},
        "changed_paths": changed_paths,
        "unattributed_paths": unattributed[:200],
        "feedback": feedback,
        "latest_event": dict(state["latest"]) if state.get("latest") else None,
        "event_count": event_count,
        "cursor": int(state.get("cursor") or 0),
    }


def _check_bucket(status: str) -> str:
    if status == "pass":
        return "pass"
    if status == "fail":
        return "fail"
    return "pending"


# --------------------------------------------------------------------------- #
# Legacy compatibility: frozen ReviewPacket.journal -> same projection        #
# --------------------------------------------------------------------------- #

_LEGACY_KIND = {
    "intent": "intent",
    "plan": "plan",
    "decision": "decision",
    "fact": "fact",
    "question": "question",
    "blocker": "blocker",
    "edit": "edit",
    "validation": "validation",
    "command": "command",
    "message": "message",
}


def legacy_events(
    change_id: str,
    journal: Sequence[Mapping[str, Any]],
    *,
    provenance: Mapping[str, Any] | None = None,
) -> list[DevelopmentEvent]:
    """Project a frozen ``ReviewPacket.journal`` into Development events.

    Old Snapshots carried one exact author session's bounded journal.  Rows are
    attributed to that session (row host/session first, packet provenance
    second) and pass through the same sanitizer and reducer as live events, so
    the Reader consumes one shape for both.  Tool chatter and non-verification
    commands are dropped here exactly as the ledger ingester drops them.
    """

    exact = provenance is not None and str(provenance.get("certainty") or "") == "exact"
    default_host = str((provenance or {}).get("host") or "").strip().lower() if exact else ""
    default_session = str((provenance or {}).get("session_id") or "").strip() if exact else ""
    ordered = sorted(
        (item for item in journal if isinstance(item, Mapping)),
        key=lambda item: (normalize_at(item.get("at")), str(item.get("id") or "")),
    )
    out: list[DevelopmentEvent] = []
    for index, item in enumerate(ordered):
        kind = _LEGACY_KIND.get(str(item.get("kind") or ""))
        if kind is None:
            continue
        summary = str(item.get("summary") or "")
        status = str(item.get("status") or "").lower()
        if kind == "command":
            check = classify_check(summary)
            if not check:
                continue
            kind = check
        if kind == "edit" and status == "reverted":
            kind = "revert"
        if kind == "validation" and summary.startswith("Next validation"):
            status = "pending"
        draft = sanitize_draft(
            DevelopmentDraft(
                kind=kind,
                at=str(item.get("at") or ""),
                source_event_id="legacy:" + str(item.get("id") or stable_id(index, summary)),
                host=str(item.get("host") or default_host),
                session_id=str(item.get("session_id") or default_session),
                path=str(item.get("path") or ""),
                summary=summary,
                detail=str(item.get("detail") or ""),
                status="pass" if status == "pass" else "fail" if status == "fail" else status,
                role="author",
                source="legacy_journal",
            )
        )
        if draft is None:
            continue
        out.append(_materialize(change_id, len(out) + 1, draft, created_at=draft.at))
    return out


def _materialize(change_id: str, sequence: int, draft: DevelopmentDraft, *, created_at: str) -> DevelopmentEvent:
    return DevelopmentEvent(
        change_id=change_id,
        sequence=sequence,
        event_id=event_id_for(change_id, draft.source_event_id),
        kind=draft.kind,
        at=draft.at,
        source_event_id=draft.source_event_id,
        host=draft.host,
        session_id=draft.session_id,
        path=draft.path,
        symbol=draft.symbol,
        target_unit_key=draft.target_unit_key,
        summary=draft.summary,
        detail=draft.detail,
        status=draft.status,
        role=draft.role,
        source=draft.source,
        causation_id=draft.causation_id,
        parent_event_id=draft.parent_event_id,
        annotation_id=draft.annotation_id,
        task_id=draft.task_id,
        created_at=created_at,
    )


def event_payload(event: DevelopmentEvent) -> dict[str, Any]:
    return {
        "id": event.event_id,
        "sequence": event.sequence,
        "kind": event.kind,
        "at": event.at,
        "host": event.host,
        "session_id": event.session_id,
        "role": event.role,
        "path": event.path,
        "symbol": event.symbol,
        "target_unit_key": event.target_unit_key,
        "summary": event.summary,
        "detail": event.detail,
        "status": event.status,
        "source": event.source,
        "causation_id": event.causation_id,
        "parent_event_id": event.parent_event_id,
        "annotation_id": event.annotation_id,
        "task_id": event.task_id,
    }


# --------------------------------------------------------------------------- #
# Storage                                                                     #
# --------------------------------------------------------------------------- #


def _row_to_event(row: sqlite3.Row) -> DevelopmentEvent:
    return DevelopmentEvent(
        change_id=str(row["change_id"]),
        sequence=int(row["sequence"]),
        event_id=str(row["event_id"]),
        kind=str(row["kind"]),
        at=str(row["at"]),
        source_event_id=str(row["source_event_id"]),
        host=str(row["host"]),
        session_id=str(row["session_id"]),
        path=str(row["path"]),
        symbol=str(row["symbol"]),
        target_unit_key=str(row["target_unit_key"]),
        summary=str(row["summary"]),
        detail=str(row["detail"]),
        status=str(row["status"]),
        role=str(row["role"]),
        source=str(row["source"]),
        causation_id=str(row["causation_id"]),
        parent_event_id=str(row["parent_event_id"]),
        annotation_id=str(row["annotation_id"]),
        task_id=str(row["task_id"]),
        created_at=str(row["created_at"]),
    )


def _row_to_session(row: sqlite3.Row) -> DevelopmentSession:
    return DevelopmentSession(
        change_id=str(row["change_id"]),
        host=str(row["host"]),
        session_id=str(row["session_id"]),
        parent_session_id=str(row["parent_session_id"]),
        model=str(row["model"]),
        purpose=str(row["purpose"]),
        role=str(row["role"]),
        binding=str(row["binding"]),
        task=str(row["task"]),
        status=str(row["status"]),
        started_at=str(row["started_at"]),
        ended_at=str(row["ended_at"]),
        cursor_json=str(row["cursor_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def merge_session(existing: DevelopmentSession | None, incoming: DevelopmentSession) -> DevelopmentSession:
    """Monotonic session merge.

    Role only ever strengthens with evidence (``excluded`` is sticky), binding
    only strengthens, and an explicit purpose is never overwritten by an
    inferred one.  Blank incoming scalars never erase recorded facts.
    """

    if existing is None:
        return incoming
    role = existing.role
    if existing.role != "excluded" and incoming.role == "excluded":
        role = "excluded" if existing.role in {"observer", ""} else existing.role
    elif _ROLE_RANK.get(incoming.role, 0) > _ROLE_RANK.get(existing.role, 0):
        role = incoming.role
    binding = (
        incoming.binding
        if _BINDING_RANK.get(incoming.binding, 0) > _BINDING_RANK.get(existing.binding, 0)
        else existing.binding
    )
    if existing.binding == "explicit" and incoming.binding != "explicit":
        purpose = existing.purpose or incoming.purpose
    else:
        purpose = incoming.purpose or existing.purpose
    return DevelopmentSession(
        change_id=existing.change_id,
        host=existing.host,
        session_id=existing.session_id,
        parent_session_id=incoming.parent_session_id or existing.parent_session_id,
        model=incoming.model or existing.model,
        purpose=purpose,
        role=role,
        binding=binding,
        task=incoming.task or existing.task,
        status=incoming.status or existing.status,
        started_at=existing.started_at or incoming.started_at,
        ended_at=incoming.ended_at or existing.ended_at,
        cursor_json=incoming.cursor_json if incoming.cursor_json not in {"", "{}"} else existing.cursor_json,
        created_at=existing.created_at or incoming.created_at,
        updated_at=incoming.updated_at or existing.updated_at,
    )


@dataclass(frozen=True)
class CursorUpdate:
    host: str
    session_id: str
    expected: str | None
    """Stored cursor this update was computed from; ``None`` skips the check."""


class DevelopmentStore:
    """Development tables on the local SQLite ReviewStore.

    Hosted Postgres Review has no local agent ledgers to observe, so its store
    raises :class:`DevelopmentUnavailable` and callers fall back to the frozen
    packet-journal projection.
    """

    def __init__(self, store: ReviewStore) -> None:
        self._store = store

    @contextlib.contextmanager
    def _tx(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """One Review-store transaction (SQLite locally, PostgreSQL when hosted).

        The SQL here is the portable subset both backends accept; the hosted
        store scopes it to its tenant. A driver error surfaces as
        :class:`DevelopmentUnavailable` so readers fall back to the frozen
        journal instead of failing the Review.
        """

        errors = tuple(getattr(self._store, "DRIVER_ERRORS", (sqlite3.Error,)))
        self._store._ensure_ready()
        try:
            with self._store._transaction() as conn:
                if write:
                    self._store._lock_for_write(conn, key="development")
                yield conn
        except errors as exc:
            raise DevelopmentUnavailable(f"Development storage failed: {exc}") from exc

    def available(self) -> bool:
        try:
            with self._tx() as conn:
                conn.execute("SELECT 1 FROM development_projections LIMIT 1").fetchall()
        except (DevelopmentUnavailable, sqlite3.Error):
            return False
        return True

    # ----- sessions ------------------------------------------------------- #

    def list_sessions(self, change_id: str) -> tuple[DevelopmentSession, ...]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM development_sessions WHERE change_id = ? ORDER BY created_at ASC, host ASC, session_id ASC",
                (change_id,),
            ).fetchall()
        return tuple(_row_to_session(row) for row in rows)

    def get_session(self, change_id: str, host: str, session_id: str) -> DevelopmentSession | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM development_sessions WHERE change_id = ? AND host = ? AND session_id = ?",
                (change_id, host.strip().lower(), session_id.strip()),
            ).fetchone()
        return None if row is None else _row_to_session(row)

    def upsert_session(self, session: DevelopmentSession) -> DevelopmentSession:
        with self._tx(write=True) as conn:
            return self._upsert_session(conn, session)

    def _upsert_session(self, conn: sqlite3.Connection, session: DevelopmentSession) -> DevelopmentSession:
        from lemoncrow.pro.capabilities.review.store import utc_now

        now = utc_now()
        host = session.host.strip().lower()
        session_id = session.session_id.strip()
        if not host or not session_id:
            raise ValueError("development session requires host and session_id")
        if session.purpose not in DEVELOPMENT_PURPOSES:
            raise ValueError(f"unknown development purpose {session.purpose!r}")
        if session.role not in DEVELOPMENT_ROLES:
            raise ValueError(f"unknown development role {session.role!r}")
        if session.binding not in DEVELOPMENT_BINDINGS:
            raise ValueError(f"unknown development binding {session.binding!r}")
        row = conn.execute(
            "SELECT * FROM development_sessions WHERE change_id = ? AND host = ? AND session_id = ?",
            (session.change_id, host, session_id),
        ).fetchone()
        existing = None if row is None else _row_to_session(row)
        incoming = replace(
            session,
            host=host,
            session_id=session_id,
            task=clean_text(session.task, 300),
            model=clean_text(session.model, 120),
            created_at=session.created_at or now,
            updated_at=now,
        )
        merged = merge_session(existing, incoming)
        conn.execute(
            """
            INSERT INTO development_sessions (
                change_id, host, session_id, parent_session_id, model, purpose, role, binding,
                task, status, started_at, ended_at, cursor_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(change_id, host, session_id) DO UPDATE SET
                parent_session_id = excluded.parent_session_id,
                model = excluded.model,
                purpose = excluded.purpose,
                role = excluded.role,
                binding = excluded.binding,
                task = excluded.task,
                status = excluded.status,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                cursor_json = excluded.cursor_json,
                updated_at = excluded.updated_at
            """,
            (
                merged.change_id,
                merged.host,
                merged.session_id,
                merged.parent_session_id,
                merged.model,
                merged.purpose,
                merged.role,
                merged.binding,
                merged.task,
                merged.status,
                merged.started_at,
                merged.ended_at,
                merged.cursor_json or "{}",
                merged.created_at,
                merged.updated_at,
            ),
        )
        self._record_lifecycle(conn, existing, merged)
        return merged

    # ----- lifecycle activity --------------------------------------------- #

    def _record_activity(
        self,
        conn: sqlite3.Connection,
        change_id: str,
        *,
        kind: ReviewActivityKind,
        actor_type: ActorType,
        actor_id: str,
        subject_id: str,
        summary: str,
        detail: Mapping[str, Any],
    ) -> None:
        from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent

        row = conn.execute("SELECT current_revision_id FROM review_sessions WHERE id = ?", (change_id,)).fetchone()
        self._store._record_activity_in_transaction(
            conn,
            ReviewActivityEvent(
                id="",
                review_id=change_id,
                revision_id="" if row is None else str(row["current_revision_id"] or ""),
                kind=kind,
                actor_id=actor_id,
                actor_type=actor_type,
                subject_type="development_session",
                subject_id=subject_id,
                summary=summary,
                detail_json=json.dumps(dict(detail), sort_keys=True),
            ),
        )

    def _record_lifecycle(
        self, conn: sqlite3.Connection, before: DevelopmentSession | None, after: DevelopmentSession
    ) -> None:
        """Put the few session transitions a reviewer cares about into Review activity.

        Only visible contributors produce activity, and only on a transition:
        becoming (or being promoted to) author/verifier/investigator, and
        finishing. Every other development event stays in the stream.
        """

        if after.role not in VISIBLE_ROLES:
            return
        label = host_label(after.host)
        base = {
            "host": after.host,
            "session_id": after.session_id,
            "model": after.model,
            "purpose": after.purpose,
            "parent_session_id": after.parent_session_id,
        }
        prior = before.role if before is not None else ""
        if _ROLE_RANK[after.role] > _ROLE_RANK.get(prior, 0):
            self._record_activity(
                conn,
                after.change_id,
                kind="development.contributor",
                actor_type="agent",
                actor_id=label,
                subject_id=after.key,
                summary=f"{label} {_ROLE_ACTIVITY[after.role]}",
                detail={**base, "role": after.role, "previous_role": prior or "none"},
            )
        if after.ended_at and not (before is not None and before.ended_at):
            self._record_activity(
                conn,
                after.change_id,
                kind="development.session_ended",
                actor_type="agent",
                actor_id=label,
                subject_id=after.key,
                summary=f"{label} finished ({after.status or 'ended'})",
                detail={**base, "role": after.role, "status": after.status, "ended_at": after.ended_at},
            )

    def _record_handoffs(self, conn: sqlite3.Connection, change_id: str, inserted: Sequence[DevelopmentEvent]) -> None:
        """One activity row per Review handoff (not per annotation it carries)."""

        groups: dict[tuple[str, str, str], list[DevelopmentEvent]] = {}
        for event in inserted:
            if event.kind == "handoff" and event.source == "review" and event.causation_id:
                groups.setdefault((event.causation_id, event.host, event.session_id), []).append(event)
        for (causation, host, session_id), events in groups.items():
            label = events[0].summary.split(":", 1)[0].strip() or "Review feedback sent"
            count = len(events)
            noun = "comment" if count == 1 else "comments"
            self._record_activity(
                conn,
                change_id,
                kind="development.handoff",
                actor_type="human",
                actor_id="",
                subject_id=session_key(host, session_id),
                summary=f"{label} to {host_label(host)} ({count} {noun})",
                detail={
                    "host": host,
                    "session_id": session_id,
                    "causation_id": causation,
                    "status": events[0].status,
                    "annotation_ids": [event.annotation_id for event in events if event.annotation_id],
                },
            )

    # ----- events --------------------------------------------------------- #

    def append(
        self,
        change_id: str,
        drafts: Sequence[DevelopmentDraft],
        *,
        sessions: Sequence[DevelopmentSession] = (),
        cursor_checks: Sequence[CursorUpdate] = (),
    ) -> AppendResult:
        """Append sanitized drafts idempotently and advance the projection.

        One write transaction covers session upserts, the event rows and the
        reducer update, so a crash can never leave a cursor ahead of its events
        or a projection behind its cursor.  ``cursor_checks`` is optimistic
        concurrency for ledger ingestion: when another writer already advanced
        a session's cursor the whole batch is refused (``conflicted``) and the
        next sync recomputes from the newer cursor.
        """

        from lemoncrow.pro.capabilities.review.store import utc_now

        clean = [item for item in (sanitize_draft(draft) for draft in drafts) if item is not None]
        clean.sort(key=_draft_sort_key)
        now = utc_now()
        with self._tx(write=True) as conn:
            for check in cursor_checks:
                if check.expected is None:
                    continue
                row = conn.execute(
                    "SELECT cursor_json FROM development_sessions WHERE change_id = ? AND host = ? AND session_id = ?",
                    (change_id, check.host.strip().lower(), check.session_id.strip()),
                ).fetchone()
                current = "{}" if row is None else str(row["cursor_json"])
                if current != check.expected:
                    return AppendResult(inserted=(), cursor=self._cursor(conn, change_id), conflicted=True)
            for session in sessions:
                self._upsert_session(conn, session)
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS n FROM development_events WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            sequence = int(row["n"]) if row is not None else 0
            inserted: list[DevelopmentEvent] = []
            for draft in clean:
                event = _materialize(change_id, sequence + 1, draft, created_at=now)
                cursor = conn.execute(
                    """
                    INSERT INTO development_events (
                        change_id, sequence, event_id, host, session_id, kind, at, path, symbol,
                        target_unit_key, summary, detail, status, role, source, source_event_id,
                        causation_id, parent_event_id, annotation_id, task_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(change_id, source_event_id) DO NOTHING
                    """,
                    (
                        event.change_id,
                        event.sequence,
                        event.event_id,
                        event.host,
                        event.session_id,
                        event.kind,
                        event.at,
                        event.path,
                        event.symbol,
                        event.target_unit_key,
                        event.summary,
                        event.detail,
                        event.status,
                        event.role,
                        event.source,
                        event.source_event_id,
                        event.causation_id,
                        event.parent_event_id,
                        event.annotation_id,
                        event.task_id,
                        event.created_at,
                    ),
                )
                if cursor.rowcount:
                    sequence += 1
                    inserted.append(event)
            if inserted:
                self._apply_projection(conn, change_id, inserted)
                self._record_handoffs(conn, change_id, inserted)
            return AppendResult(inserted=tuple(inserted), cursor=sequence)

    def _cursor(self, conn: sqlite3.Connection, change_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS n FROM development_events WHERE change_id = ?",
            (change_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def cursor(self, change_id: str) -> int:
        """Last appended sequence: the live-state Development cursor."""

        with self._tx() as conn:
            return self._cursor(conn, change_id)

    def _fold(
        self,
        conn: sqlite3.Connection,
        change_id: str,
        *,
        after: int = 0,
        upto: int | None = None,
        after_at: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``reduce`` every event in ``(after, upto]`` (optionally ``at > after_at``), page by page."""

        folded = state if state is not None else empty_state()
        clauses = ["change_id = ?", "sequence > ?"]
        if upto is not None:
            clauses.append("sequence <= ?")
        if after_at:
            clauses.append("at > ?")
        sql = f"SELECT * FROM development_events WHERE {' AND '.join(clauses)} ORDER BY sequence ASC LIMIT ?"
        position = after
        while True:
            params: list[Any] = [change_id, position]
            if upto is not None:
                params.append(upto)
            if after_at:
                params.append(after_at)
            params.append(_FOLD_PAGE)
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                apply_event(folded, _row_to_event(row))
            if len(rows) < _FOLD_PAGE:
                return folded
            position = int(rows[-1]["sequence"])

    def _apply_projection(self, conn: sqlite3.Connection, change_id: str, events: Sequence[DevelopmentEvent]) -> None:
        from lemoncrow.pro.capabilities.review.store import utc_now

        row = conn.execute(
            "SELECT cursor, state_json FROM development_projections WHERE change_id = ?",
            (change_id,),
        ).fetchone()
        state: dict[str, Any] | None = None
        if row is not None:
            with contextlib.suppress(TypeError, ValueError):
                loaded = json.loads(str(row["state_json"]) or "{}")
                if isinstance(loaded, dict) and loaded.get("v") == PROJECTION_VERSION:
                    state = loaded
        if state is None or int(state.get("cursor") or 0) != events[0].sequence - 1:
            # Missing, stale-version or gapped state: one bounded rebuild from
            # the durable log. Appends normally never take this path.
            state = self._fold(conn, change_id)
        else:
            reduce_events(events, state)
        conn.execute(
            """
            INSERT INTO development_projections (change_id, cursor, state_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(change_id) DO UPDATE SET
                cursor = excluded.cursor, state_json = excluded.state_json, updated_at = excluded.updated_at
            """,
            (change_id, int(state["cursor"]), json.dumps(state, ensure_ascii=False, separators=(",", ":")), utc_now()),
        )

    def live_state(self, change_id: str) -> dict[str, Any]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state_json FROM development_projections WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            cursor = self._cursor(conn, change_id)
            if row is not None:
                with contextlib.suppress(TypeError, ValueError):
                    loaded = json.loads(str(row["state_json"]) or "{}")
                    if (
                        isinstance(loaded, dict)
                        and loaded.get("v") == PROJECTION_VERSION
                        and int(loaded.get("cursor") or 0) == cursor
                    ):
                        return loaded
            if cursor == 0:
                return empty_state()
            return self._fold(conn, change_id)

    def snapshot_watermark(self, revision_id: str) -> int | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT watermark FROM development_snapshots WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        return None if row is None else int(row["watermark"])

    def revision_watermarks(self, change_id: str) -> dict[str, int]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT revision_id, watermark FROM development_snapshots WHERE change_id = ?",
                (change_id,),
            ).fetchall()
        return {str(row["revision_id"]): int(row["watermark"]) for row in rows}

    def frozen_state(self, change_id: str, revision_id: str) -> tuple[dict[str, Any], int] | None:
        """``reduce(events <= watermark)`` for one Snapshot; cached once computed.

        Events at or below a watermark are immutable, so the cache can never
        go stale; a reducer version bump simply recomputes.
        """

        with self._tx() as conn:
            row = conn.execute(
                "SELECT watermark, projection_json FROM development_snapshots WHERE revision_id = ? AND change_id = ?",
                (revision_id, change_id),
            ).fetchone()
            if row is None:
                return None
            watermark = int(row["watermark"])
            cached = str(row["projection_json"] or "")
            if cached:
                with contextlib.suppress(TypeError, ValueError):
                    loaded = json.loads(cached)
                    if isinstance(loaded, dict) and loaded.get("v") == PROJECTION_VERSION:
                        return loaded, watermark
            state = self._fold(conn, change_id, upto=watermark)
        with contextlib.suppress(DevelopmentUnavailable):
            with self._tx(write=True) as conn:
                conn.execute(
                    "UPDATE development_snapshots SET projection_json = ? "
                    "WHERE revision_id = ? AND watermark = ? AND projection_json = ''",
                    (json.dumps(state, ensure_ascii=False, separators=(",", ":")), revision_id, watermark),
                )
        return state, watermark

    def since_state(
        self, change_id: str, *, after: int = 0, after_at: str = "", revision_id: str = ""
    ) -> dict[str, Any]:
        """Every event strictly after a watermark (or instant, for legacy Snapshots).

        With a *revision_id* the fold is checkpointed on that Snapshot's row, so
        each request only folds events appended since the previous one. The
        checkpoint only ever advances (compare-and-set on ``since_cursor``), and
        events at or below it are immutable, so it cannot go stale.
        """

        checkpoint = 0
        with self._tx() as conn:
            live = self._cursor(conn, change_id)
            base: dict[str, Any] | None = None
            start = after
            if revision_id and not after_at:
                row = conn.execute(
                    "SELECT watermark, since_cursor, since_json FROM development_snapshots "
                    "WHERE revision_id = ? AND change_id = ?",
                    (revision_id, change_id),
                ).fetchone()
                if row is not None and int(row["watermark"]) == after:
                    checkpoint = int(row["since_cursor"])
                    if after <= checkpoint <= live and str(row["since_json"] or ""):
                        with contextlib.suppress(TypeError, ValueError):
                            loaded = json.loads(str(row["since_json"]))
                            if isinstance(loaded, dict) and loaded.get("v") == PROJECTION_VERSION:
                                base, start = loaded, checkpoint
                if base is not None and start == live:
                    return base
            # lc-debt: a legacy Snapshot (created before the stream, so no
            # watermark row) re-folds its time-filtered events on every read.
            # Upgrade path: checkpoint it like a watermark Snapshot once legacy
            # Changes see real traffic.
            state = self._fold(conn, change_id, after=start, upto=live, after_at=after_at, state=base)
        if revision_id and not after_at and live > checkpoint:
            with contextlib.suppress(DevelopmentUnavailable):
                with self._tx(write=True) as conn:
                    conn.execute(
                        "UPDATE development_snapshots SET since_cursor = ?, since_json = ? "
                        "WHERE revision_id = ? AND change_id = ? AND watermark = ? AND since_cursor = ?",
                        (
                            live,
                            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                            revision_id,
                            change_id,
                            after,
                            checkpoint,
                        ),
                    )
        return state

    def list_events(
        self,
        change_id: str,
        *,
        after: int = 0,
        upto: int | None = None,
        before: int | None = None,
        limit: int = 50,
        kinds: Sequence[str] = (),
        roles: Sequence[str] = (),
        path: str = "",
        session: tuple[str, str] | None = None,
        after_at: str = "",
    ) -> list[DevelopmentEvent]:
        clauses = ["change_id = ?", "sequence > ?"]
        params: list[Any] = [change_id, after]
        if after_at:
            clauses.append("at > ?")
            params.append(after_at)
        if upto is not None:
            clauses.append("sequence <= ?")
            params.append(upto)
        if before is not None:
            clauses.append("sequence < ?")
            params.append(before)
        if kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if roles:
            clauses.append(f"role IN ({','.join('?' for _ in roles)})")
            params.extend(roles)
        if path:
            clauses.append("path = ?")
            params.append(path)
        if session is not None:
            clauses.append("host = ? AND session_id = ?")
            params.extend(session)
        params.append(max(1, min(int(limit), 200)))
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM development_events WHERE {' AND '.join(clauses)} ORDER BY sequence DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def handoffs_for(self, change_id: str, host: str, session_id: str) -> list[tuple[str, str]]:
        """``(at, causation_id)`` of every handoff addressed to one session."""

        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT at, causation_id FROM development_events
                 WHERE change_id = ? AND kind = 'handoff' AND host = ? AND session_id = ? AND causation_id != ''
                 ORDER BY sequence ASC
                """,
                (change_id, host.strip().lower(), session_id.strip()),
            ).fetchall()
        return [(str(row["at"]), str(row["causation_id"])) for row in rows]

    def event_count(self, change_id: str) -> int:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM development_events WHERE change_id = ?",
                (change_id,),
            ).fetchone()
        return int(row["n"]) if row is not None else 0


# --------------------------------------------------------------------------- #
# Explicit sink: bindings and pushed events                                   #
# --------------------------------------------------------------------------- #


def bind_session(
    development: DevelopmentStore,
    change_id: str,
    *,
    host: str,
    session_id: str,
    purpose: str = "",
    parent_session_id: str = "",
    task: str = "",
    model: str = "",
) -> DevelopmentSession:
    """Explicitly bind a session to a Change.  Binding is scope, never authorship."""

    return development.upsert_session(
        DevelopmentSession(
            change_id=change_id,
            host=host,
            session_id=session_id,
            parent_session_id=parent_session_id,
            model=model,
            purpose=purpose,
            role="observer",
            binding="explicit",
            task=task,
        )
    )


def push_events(
    development: DevelopmentStore,
    change_id: str,
    raw_events: Sequence[Mapping[str, Any]],
    *,
    source: str = "api",
) -> AppendResult:
    """Append explicitly reported events (hosts, LemonCrow runtime, reviewers).

    Roles are derived, never taken from the caller: an ``edit`` makes its
    session an author; checks make a verifier; a fact/decision/handoff with an
    ``accepted_by`` author session makes an investigator.  Anything else from
    a session with no evidence is refused rather than silently promoted.
    """

    if len(raw_events) > _MAX_PUSH_EVENTS:
        raise ValueError(f"at most {_MAX_PUSH_EVENTS} events per request")
    known = {item.key: item for item in development.list_sessions(change_id)}
    drafts: list[DevelopmentDraft] = []
    sessions: dict[str, DevelopmentSession] = {}
    for index, raw in enumerate(raw_events):
        kind = str(raw.get("kind") or "").strip()
        if kind not in DEVELOPMENT_EVENT_KINDS:
            raise ValueError(f"unknown development event kind {kind!r}")
        host = str(raw.get("host") or "").strip().lower()
        session_id = str(raw.get("session_id") or "").strip()
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            raise ValueError("development event requires a summary")
        if kind != "handoff" and (not host or not session_id):
            raise ValueError("development event requires host and session_id")
        key = session_key(host, session_id) if session_id else ""
        record = sessions.get(key) or known.get(key)
        if record is not None and record.purpose in {"benchmark", "system"}:
            raise ValueError("benchmark/system sessions do not contribute to Development")
        accepted_by = str(raw.get("accepted_by") or "").strip()
        role = record.role if record is not None else "observer"
        if kind in _CHANGE_KINDS:
            role = "author"
        elif kind in _VERIFY_KINDS:
            role = "author" if role == "author" else "verifier"
        elif accepted_by and kind in {"fact", "decision", "handoff"}:
            accepted = known.get(accepted_by) or sessions.get(accepted_by)
            if accepted is None or accepted.role != "author":
                raise ValueError("accepted_by must name an author session of this Change")
            role = "author" if role == "author" else "investigator"
        elif kind == "handoff":
            role = role if role in {"author", "verifier"} else ""
        elif role == "author" or (role == "verifier" and kind in ADMITTED_KINDS["verifier"]):
            pass
        else:
            # Investigators contribute only what an author explicitly accepted.
            raise ValueError(f"session {key or '(none)'} has no Development evidence for a {kind!r} event")
        if record is not None and record.purpose in {"review", "ask"} and kind not in REVIEW_PURPOSE_KINDS:
            raise ValueError("Review/Ask sessions contribute only edits and checks to Development")
        if key and role in VISIBLE_ROLES:
            base = record or DevelopmentSession(change_id=change_id, host=host, session_id=session_id)
            updated = replace(base, role=role if _ROLE_RANK[role] > _ROLE_RANK.get(base.role, 0) else base.role)
            sessions[key] = updated
        status = str(raw.get("status") or "")
        if role == "investigator" and kind in {"fact", "decision", "handoff"}:
            status = status or "accepted"
        source_event_id = str(raw.get("source_event_id") or "").strip() or stable_id(
            source, kind, host, session_id, raw.get("at") or "", summary, raw.get("path") or ""
        )
        drafts.append(
            DevelopmentDraft(
                kind=kind,
                at=str(raw.get("at") or "") or _now_iso(),
                source_event_id=f"{source}:{source_event_id}",
                host=host,
                session_id=session_id,
                path=str(raw.get("path") or ""),
                symbol=str(raw.get("symbol") or ""),
                target_unit_key=str(raw.get("target_unit_key") or ""),
                summary=summary,
                detail=str(raw.get("detail") or ""),
                status=status,
                role=role,
                source=source,
                causation_id=str(raw.get("causation_id") or ""),
                parent_event_id=str(raw.get("parent_event_id") or ""),
                annotation_id=str(raw.get("annotation_id") or ""),
                task_id=str(raw.get("task_id") or ""),
                order=index,
            )
        )
    return development.append(change_id, drafts, sessions=tuple(sessions.values()))


def record_feedback_handoff(
    development: DevelopmentStore,
    change_id: str,
    *,
    host: str,
    session_id: str,
    operation_id: str,
    annotations: Sequence[tuple[str, str]],
    state: str,
    causation_id: str = "",
    label: str = "Review feedback sent",
) -> AppendResult:
    """Link Review feedback to the development it causes.

    One ``handoff`` per annotation shares ``causation_id`` (default
    ``feedback:<operation>``); the receiving session is explicitly bound with
    purpose ``implement``.  That binding scopes later ingestion and stamps the
    session's later events with the causation -- it never makes the session an
    author by itself, and it never creates or changes a Review comment.
    """

    causation = causation_id or f"feedback:{operation_id}"
    at = _now_iso()
    drafts = [
        DevelopmentDraft(
            kind="handoff",
            at=at,
            source_event_id=f"review:{operation_id}:{host}:{session_id}:{annotation_id or index}",
            host=host,
            session_id=session_id,
            summary=f"{label}: {title}" if title else label,
            status=state,
            role="",
            source="review",
            causation_id=causation,
            annotation_id=annotation_id,
            order=index,
        )
        for index, (annotation_id, title) in enumerate(annotations)
    ]
    binding = DevelopmentSession(
        change_id=change_id,
        host=host,
        session_id=session_id,
        purpose="implement",
        role="observer",
        binding="explicit",
    )
    return development.append(change_id, drafts, sessions=(binding,))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Read model used by the API                                                  #
# --------------------------------------------------------------------------- #


def development_view(
    development: DevelopmentStore | None,
    *,
    change_id: str,
    revision_id: str,
    revision_number: int,
    revision_created_at: str,
    packet: Mapping[str, Any] | None,
    attributions: Sequence[Mapping[str, Any]],
    revision_numbers: Mapping[str, int],
    historical: bool,
    live_enabled: bool,
) -> dict[str, Any]:
    """``{snapshot, since}`` for one exact Snapshot of one Change.

    ``snapshot`` is immutable: events up to the Snapshot's watermark (or, for
    Snapshots recorded before the stream existed, the frozen packet journal).
    ``since`` is explicitly *newer* work and is never merged into it.
    """

    snapshot_paths = [
        str(item.get("path") or "")
        for item in ((packet or {}).get("files") or [])
        if isinstance(item, Mapping) and item.get("path")
    ]
    sessions: dict[str, DevelopmentSession] = {}
    frozen: tuple[dict[str, Any], int] | None = None
    watermarks: dict[str, int] = {}
    cursor = 0
    reason = ""
    if development is not None:
        try:
            sessions = {item.key: item for item in development.list_sessions(change_id)}
            frozen = development.frozen_state(change_id, revision_id)
            watermarks = development.revision_watermarks(change_id)
            cursor = development.cursor(change_id)
        except (DevelopmentUnavailable, sqlite3.Error):
            development = None
            reason = "Development events are not available on this Review backend"
    ordered_watermarks = [
        (revision_numbers[revision], watermark)
        for revision, watermark in watermarks.items()
        if revision in revision_numbers
    ]

    source = "events"
    watermark: int | None = None
    if frozen is not None and int(frozen[0].get("event_count") or 0) > 0:
        state, watermark = frozen
    else:
        if frozen is not None:
            watermark = frozen[1]
        journal = [item for item in ((packet or {}).get("journal") or []) if isinstance(item, Mapping)]
        provenance = (packet or {}).get("provenance")
        legacy = legacy_events(
            change_id,
            journal,
            provenance=provenance if isinstance(provenance, Mapping) else None,
        )
        state = reduce_events(legacy)
        source = "legacy_journal" if legacy else ("attribution" if attributions else "none")
    snapshot = render_projection(
        state,
        scope="snapshot",
        source=source,
        sessions=sessions,
        attributions=attributions,
        snapshot_paths=snapshot_paths,
        revision_watermarks=ordered_watermarks,
    )
    snapshot.update({"revision_id": revision_id, "revision_number": revision_number, "watermark": watermark})

    since: dict[str, Any] | None = None
    if development is not None:
        try:
            if watermark is not None:
                since_state = development.since_state(change_id, after=watermark, revision_id=revision_id)
            else:
                since_state = development.since_state(change_id, after_at=normalize_at(revision_created_at) or "9999")
        except (DevelopmentUnavailable, sqlite3.Error):
            since_state = empty_state()
        since = render_projection(
            since_state,
            scope="since",
            source="events",
            sessions=sessions,
            revision_watermarks=ordered_watermarks,
            now=datetime.now(UTC),
        )
        snapshot_set = frozenset(snapshot_paths)
        since["newer_paths"] = [item["path"] for item in since["changed_paths"] if item["path"] not in snapshot_set][
            :200
        ]
        since["active"] = any(item["active"] for item in since["contributors"])
    if not snapshot["available"] and not reason:
        reason = (
            "No exact development evidence is attached to this Snapshot yet."
            if live_enabled
            else "Live development evidence is not available in this deployment."
        )
    return {
        "review_id": change_id,
        "revision_id": revision_id,
        "revision_number": revision_number,
        "historical": historical,
        "live": bool(live_enabled and development is not None),
        "cursor": str(cursor),
        "snapshot": snapshot,
        "since": since,
        "reason": reason,
    }


_TIMELINE_FILTERS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "all": ((), ()),
    "authors": ((), ("author",)),
    "verification": (("test", "validation", "command"), ()),
    "decisions": (("plan", "decision", "fact", "question", "blocker", "handoff"), ()),
    "changes": (("edit", "revert"), ()),
}
TIMELINE_FILTERS: tuple[str, ...] = tuple(_TIMELINE_FILTERS)


def timeline_filter(name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if name not in _TIMELINE_FILTERS:
        raise ValueError(f"filter must be one of {', '.join(TIMELINE_FILTERS)}")
    return _TIMELINE_FILTERS[name]


def legacy_timeline(
    events: Sequence[DevelopmentEvent],
    *,
    kinds: Sequence[str],
    roles: Sequence[str],
    before: int | None,
    limit: int,
    path: str = "",
) -> list[DevelopmentEvent]:
    rows = [
        event
        for event in events
        if (not kinds or event.kind in kinds)
        and (not roles or event.role in roles)
        and (before is None or event.sequence < before)
        and (not path or event.path == path)
    ]
    rows.sort(key=lambda event: event.sequence, reverse=True)
    return rows[: max(1, min(limit, 200))]


__all__ = [
    "ADMITTED_KINDS",
    "DEVELOPMENT_EVENT_KINDS",
    "DEVELOPMENT_PURPOSES",
    "DEVELOPMENT_ROLES",
    "PROJECTION_VERSION",
    "REVIEW_PURPOSE_KINDS",
    "TIMELINE_FILTERS",
    "VISIBLE_ROLES",
    "AppendResult",
    "CursorUpdate",
    "DevelopmentCursorConflict",
    "DevelopmentDraft",
    "DevelopmentEvent",
    "DevelopmentSession",
    "DevelopmentStore",
    "DevelopmentUnavailable",
    "apply_event",
    "bind_session",
    "check_key",
    "classify_check",
    "clean_text",
    "development_view",
    "empty_state",
    "event_payload",
    "legacy_events",
    "legacy_timeline",
    "normalize_at",
    "push_events",
    "record_feedback_handoff",
    "reduce_events",
    "render_projection",
    "sanitize_draft",
    "session_key",
    "stable_id",
    "timeline_filter",
]
