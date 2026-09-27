"""A continuation brief for one agent session, small enough to actually paste.

Why this module exists: resuming work in a fresh agent session normally means
re-reading a transcript, which costs more context than it restores. The whole
value here is the *bound* -- every field carries a hard cap and the rendered
form is guaranteed to stay under 60 lines, so a 4-hour session with 500 edited
files and 100 recorded learnings produces the same size artifact as a 5-minute
one. An unbounded "session summary" is just the transcript again.

Everything is sourced from records that already exist. Session parsing is not
re-implemented: the read/edit split, the session lookup and the verification
evidence all come from ``review.provenance``, so a change to how a host records
file touches lands in one place. The only source read directly here is
``run.json``, for the two keys (``open_questions``, ``current_blockers``) no
other reader exposes.

Every signal degrades to a value. A session with no trace still yields a brief
from its run ledger; a session with neither is the one honest error, because
there is nothing to continue from.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.models import Trace
from lemoncrow.pro.capabilities.review.models import EvidenceRecord
from lemoncrow.pro.capabilities.review.provenance import (
    collect_evidence,
    load_trace_for_session,
    split_files_touched,
)

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Hard caps (spec 2.5). These are the product, not tuning knobs: the brief is  #
# useful precisely because its size does not scale with the session's size.    #
# --------------------------------------------------------------------------- #

MAX_DECISIONS = 8
MAX_FILES = 25
MAX_SYMBOLS = 15
MAX_UNRESOLVED = 8
MAX_TEXT_CHARS = 200
"""Applies to ``goal`` and to every ``decisions`` / ``unresolved`` entry."""

MAX_RENDER_LINES = 60
"""The rendered brief must stay strictly under this; see ``render_resume_context``."""

# Display caps, deliberately tighter than the data caps. The record keeps 25
# files because a consumer may want them all; the human-facing text shows the
# first few and says how many it dropped. The arithmetic below is what keeps
# the render provably under MAX_RENDER_LINES:
#   1 title + 1 meta + 1 blank
# + 2 goal            + 1 blank
# + 1 + 6 + 1 decisions   + 1 blank
# + 1 + 10 + 1 changed    + 1 blank
# + 1 + 6 + 1 inspected   + 1 blank
# + 1 + 1 symbols         + 1 blank
# + 1 + 6 tests           + 1 blank
# + 1 + 5 + 1 unresolved  + 1 blank
# + 1 footer                        = 57
_SHOW_DECISIONS = 6
_SHOW_CHANGED = 10
_SHOW_INSPECTED = 6
_SHOW_EVIDENCE = 6
_SHOW_UNRESOLVED = 5
_SHOW_SYMBOL_CHARS = 110

UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResumeContext:
    """The whole continuation brief for one session. Bounded by construction."""

    schema_version: int
    session_id: str
    host: str
    """``"unknown"`` rather than ``None``: a brief always answers every question."""
    model: str
    goal: str
    """``Trace.task``, else the run ledger's task, else ``"unknown"``."""
    decisions: tuple[str, ...]
    """<= 8 entries, each <= 200 chars: learnings first, then reasoning."""
    files_changed: tuple[str, ...]
    """<= 25, repo-relative where an anchor is known, sorted."""
    files_inspected: tuple[str, ...]
    """<= 25. Empty for every host but Claude -- reads are not recorded (risk R2)."""
    important_symbols: tuple[str, ...]
    """<= 15. Empty unless a repo root was supplied, keeping the default path index-free."""
    test_state: tuple[EvidenceRecord, ...]
    unresolved: tuple[str, ...]
    """<= 8: the run ledger's open questions then its current blockers."""
    generated_at: str
    """ISO-8601 UTC, timezone-aware."""

    def to_dict(self) -> dict[str, Any]:
        """Return the exact ``--json`` payload: asdict semantics, declaration order."""

        return asdict(self)


# --------------------------------------------------------------------------- #
# Text and path normalisation                                                 #
# --------------------------------------------------------------------------- #


def _one_line(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Collapse *value* to a single line of at most *limit* characters.

    Recorded reasoning and learnings are free text and routinely contain
    newlines; one entry that renders as twelve lines would defeat the cap that
    is the point of this module.
    """

    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _posix(path: str) -> str:
    """Return *path* as a plain posix string with no trailing separator."""

    text = str(path).strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _anchors(*candidates: str | Path | None) -> tuple[str, ...]:
    """Return the workspace prefixes to strip, most specific first."""

    seen: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        text = _posix(str(candidate))
        if text and text not in seen:
            seen.append(text)
    return tuple(sorted(seen, key=len, reverse=True))


def _relative(paths: Iterable[str], anchors: Sequence[str], limit: int) -> tuple[str, ...]:
    """Return *paths* repo-relative, deduplicated, sorted and capped at *limit*.

    Sorting before truncating is what makes the cap deterministic: there is no
    recorded ordering over touched files to prefer, so an arbitrary-but-stable
    slice beats one that changes between two runs over the same session.
    """

    out: set[str] = set()
    for raw in paths:
        text = _posix(raw)
        if not text:
            continue
        for anchor in anchors:
            if text.startswith(anchor + "/"):
                text = text[len(anchor) + 1 :]
                break
        if text:
            out.add(text)
    return tuple(sorted(out))[:limit]


def _capped(values: Iterable[str], limit: int) -> tuple[str, ...]:
    """Return the first *limit* distinct non-empty single-line entries."""

    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _one_line(value)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


# --------------------------------------------------------------------------- #
# Sources                                                                     #
# --------------------------------------------------------------------------- #


def _load_ledger(store_root: Path, session_id: str) -> dict[str, Any] | None:
    """Return the ``run.json`` snapshot for *session_id*, or ``None``.

    Read as plain JSON rather than through ``RunLedger.load``: the loader raises
    on a corrupt file and rebuilds an entire ledger object, and all this needs
    are two list keys. A missing or unreadable ledger is a gap, not a failure.
    """

    try:
        from lemoncrow.core.foundation.paths import find_session_dir

        directory = find_session_dir(store_root, session_id)
        if directory is None:
            return None
        run_file = directory / "run.json"
        if not run_file.is_file():
            return None
        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _ledger_strings(ledger: dict[str, Any] | None, key: str) -> tuple[str, ...]:
    """Return the string entries of ``ledger[key]``, tolerating any other shape."""

    if ledger is None:
        return ()
    raw = ledger.get(key)
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if isinstance(item, str))


def _ledger_text(ledger: dict[str, Any] | None, key: str) -> str:
    if ledger is None:
        return ""
    value = ledger.get(key)
    return value.strip() if isinstance(value, str) else ""


def _report_identity(store_root: Path, session_id: str) -> tuple[str, str]:
    """Return ``(vendor, model)`` from the persisted session report, or empties.

    Only consulted when no trace was found: ``SessionReport`` is the one reader
    that resolves a vendor and a starting model out of a bare run ledger.
    """

    try:
        from lemoncrow.infra.runtime.session_report import load_report

        report = load_report(session_id, store_root)
    except Exception:
        return "", ""
    if report is None:
        return "", ""
    return (report.vendor or "").strip(), (report.started_model or "").strip()


def _decisions(trace: Trace | None) -> tuple[str, ...]:
    """Return the session's recorded decisions: learnings first, then reasoning.

    Learnings lead because they are the host's own distillation; raw reasoning
    only fills whatever cap is left. Deduplication is case-folded because the
    same conclusion is routinely recorded in both places.
    """

    if trace is None:
        return ()
    texts: list[str] = [learning.text for learning in trace.learnings]
    texts.extend(trace.reasoning)
    return _capped(texts, MAX_DECISIONS)


def _important_symbols(store_root: Path, repo_root: Path | None) -> tuple[str, ...]:
    """Return the most consequential changed symbols, or ``()``.

    Gated on an explicit *repo_root* so the default brief never pays for pygit2,
    a code index or the impact detectors. Every failure below -- not a
    repository, no revision to resolve, no index -- yields an empty tuple: a
    continuation brief that cannot name a symbol is still a useful brief.
    """

    if repo_root is None:
        return ()
    try:
        from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root, resolve_rev_range
        from lemoncrow.pro.capabilities.review.packet import build_review_packet

        resolved = detect_repo_root(repo_root)
        packet = build_review_packet(
            resolved,
            resolve_rev_range(resolved),
            store_root=store_root,
            with_provenance=False,
            limit=MAX_SYMBOLS,
        )
    except Exception:
        return ()

    ranked = sorted(
        packet.symbols,
        key=lambda item: (
            item.centrality_rank if item.centrality_rank is not None else 1_000_000,
            -item.caller_count,
            item.file_path,
            item.symbol_name,
        ),
    )
    out: list[str] = []
    seen: set[str] = set()
    for symbol in ranked:
        name = symbol.symbol_name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= MAX_SYMBOLS:
            break
    return tuple(out)


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def build_resume_context(root: Path, session_id: str, *, repo_root: Path | None = None) -> ResumeContext:
    """Build the bounded continuation brief for *session_id*.

    A trace and a run ledger are independent records of the same session and
    either alone is enough: the trace carries the goal, the touched files and
    the decisions, the ledger carries the open questions and blockers. Only
    when neither exists is this an error -- there is then no session to resume,
    and inventing an empty brief would read as "this session did nothing".

    Pass *repo_root* to populate ``important_symbols``; leaving it ``None``
    keeps the whole call index-free.

    Raises:
        ValueError: no trace and no run ledger exist for *session_id*.
    """

    store_root = Path(root)
    wanted = str(session_id).strip()
    if not wanted:
        raise ValueError("a session id is required")

    trace = load_trace_for_session(store_root, wanted)
    ledger = _load_ledger(store_root, wanted)
    if trace is None and ledger is None:
        raise ValueError(f"no recorded session {wanted!r} under {store_root}")

    if trace is not None:
        reads, edits = split_files_touched(trace)
        workspace: str | None = trace.workspace_path
        host = (trace.host or "").strip() or (trace.agent or "").strip()
        model = (trace.model or "").strip()
        goal = (trace.task or "").strip()
    else:
        # Ledger-only session: files_touched there is edits-only (risk R2).
        reads, edits = (), _ledger_strings(ledger, "files_touched")
        workspace = _ledger_text(ledger, "workspace_path") or None
        vendor, started_model = _report_identity(store_root, wanted)
        host = _ledger_text(ledger, "agent") or vendor
        model = started_model
        goal = _ledger_text(ledger, "task")

    anchors = _anchors(repo_root, workspace, _ledger_text(ledger, "workspace_path"))
    # An evidence row is a claim about a specific run; without a trace there are
    # no recorded commands and therefore nothing that can be claimed.
    evidence = collect_evidence(store_root, trace, wanted) if trace is not None else ()

    return ResumeContext(
        schema_version=SCHEMA_VERSION,
        session_id=wanted,
        host=host or UNKNOWN,
        model=model or UNKNOWN,
        goal=_one_line(goal) or UNKNOWN,
        decisions=_decisions(trace),
        files_changed=_relative(edits, anchors, MAX_FILES),
        files_inspected=_relative(reads, anchors, MAX_FILES),
        important_symbols=_important_symbols(store_root, repo_root),
        test_state=evidence,
        unresolved=_capped(
            _ledger_strings(ledger, "open_questions") + _ledger_strings(ledger, "current_blockers"),
            MAX_UNRESOLVED,
        ),
        generated_at=datetime.now(tz=UTC).isoformat(),
    )


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #


def _section(out: list[str], title: str, rows: Sequence[str], show: int) -> None:
    """Append a capped section: a counted heading, *show* rows, then the remainder."""

    if not rows:
        return
    out.append("")
    out.append(f"{title} ({len(rows)})")
    out.extend(f"  {row}" for row in rows[:show])
    remaining = len(rows) - show
    if remaining > 0:
        out.append(f"  … {remaining} more")


def render_resume_context(context: ResumeContext) -> str:
    """Render *context* as plain text, guaranteed under ``MAX_RENDER_LINES`` lines.

    Deliberately colour-free and Rich-free: this text exists to be pasted into
    the next agent session, where ANSI escapes are noise that costs tokens.
    Sections with nothing to say are omitted entirely rather than printed empty,
    except for tests -- "nothing was recorded" is itself the finding there.
    """

    out: list[str] = [f"RESUME CONTEXT  {context.session_id}"]
    out.append(f"{context.host} · {context.model} · generated {context.generated_at}")
    out.append("")
    out.append("GOAL")
    out.append(f"  {context.goal}")

    _section(out, "DECISIONS", context.decisions, _SHOW_DECISIONS)
    _section(out, "FILES CHANGED", context.files_changed, _SHOW_CHANGED)
    _section(out, "FILES INSPECTED", context.files_inspected, _SHOW_INSPECTED)

    if context.important_symbols:
        out.append("")
        out.append(f"SYMBOLS ({len(context.important_symbols)})")
        out.append(f"  {_one_line(', '.join(context.important_symbols), _SHOW_SYMBOL_CHARS)}")

    out.append("")
    out.append("TESTS")
    if context.test_state:
        for record in context.test_state[:_SHOW_EVIDENCE]:
            detail = _one_line(record.detail, 60)
            out.append(f"  {record.name:<16}{record.status:<9}{detail}".rstrip())
    else:
        out.append("  no verification commands recorded for this session")

    _section(out, "UNRESOLVED", context.unresolved, _SHOW_UNRESOLVED)

    out.append("")
    out.append("Bounded brief — run `lc review` for the full change packet.")
    return "\n".join(out)


__all__ = [
    "MAX_DECISIONS",
    "MAX_FILES",
    "MAX_RENDER_LINES",
    "MAX_SYMBOLS",
    "MAX_TEXT_CHARS",
    "MAX_UNRESOLVED",
    "SCHEMA_VERSION",
    "ResumeContext",
    "build_resume_context",
    "render_resume_context",
]
