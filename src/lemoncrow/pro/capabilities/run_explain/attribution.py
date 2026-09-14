"""Bucket a run's recorded evidence into categories -- and name what was missing.

Why this module exists: "why did this run go badly" is normally answered by
reading a transcript backwards until something looks guilty. That produces a
confident story from whichever failure the reader noticed first. This builds
the opposite artifact: every signal is a record that exists on disk, carries a
path back to itself, and is categorised by a fixed table rather than by
judgement.

Two rules do the real work.

**No attribution by elimination.** A category with no observed record produces
no signal, and the absence is never read as evidence for another category. When
a whole *source* is missing -- no run ledger, no imported trace, no usage -- its
name lands in ``unresolved`` so "nothing failed" and "nothing was recorded" stay
distinguishable.

**Nothing is fabricated, including timestamps.** ``run.json`` is read as plain
JSON rather than through ``RunLedger.load``: the loader reconstructs each event
without its ``at`` field (``run_ledger.py:736-742``), so every event would come
back stamped with the moment the report was generated. A report whose whole
value is pointing at real evidence cannot afford invented times. The same
plain-JSON read is what ``resume_context.builder._load_ledger`` already does,
for the adjacent reason that the loader raises on a corrupt file.

The cost block is not recomputed here. It is ``usage.explain_run`` verbatim, so
there is exactly one accounting path in the codebase and this report cannot
drift from what ``lc usage explain`` says about the same session.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.run_explain.models import (
    ATTRIBUTION_CATEGORY_VALUES,
    CATEGORY_MEANING,
    SCHEMA_VERSION,
    AttributionCategory,
    AttributionReport,
    AttributionSignal,
)

DEFAULT_SIGNAL_LIMIT = 20
"""Signals kept per category. Matches the CLI's ``--limit`` default."""

MAX_SUMMARY_CHARS = 160

EVENT_RETENTION_FALLBACK = 8000
"""Mirrors ``run_ledger._MAX_RETAINED_EVENTS``; used only if that import fails."""

# The §2.4 patterns, verbatim. Deliberately narrow: an error string that matches
# neither is reported as ``unknown`` rather than forced into the nearest bucket.
_PROVIDER_ERROR_RE = re.compile(r"rate.?limit|429|overloaded|quota", re.IGNORECASE)
_MODEL_ERROR_RE = re.compile(r"context.?length|max.?tokens|truncat", re.IGNORECASE)

# Payload ``status`` strings that mean "this did not succeed".
_FAILED_STATUS_VALUES: tuple[str, ...] = (
    "blocked",
    "denied",
    "error",
    "errored",
    "fail",
    "failed",
    "failure",
    "timeout",
)

# Why a source is missing, in the reader's terms. Every name that can land in
# ``AttributionReport.unresolved`` has an entry here.
UNRESOLVED_REASON: dict[str, str] = {
    "run_ledger": "no readable run.json for this session; per-event evidence is unavailable",
    "trace": "no imported trace; provider errors and repeated failures are unavailable",
    "cost": "usage could not be read for this session; the cost breakdown is unavailable",
    "events_truncated": ("the run ledger hit its retention cap and evicted its oldest events; early evidence is gone"),
}

# (category, summary, evidence_path, at) before duplicate collapsing.
_Raw = tuple[str, str, str, str | None]


# --------------------------------------------------------------------------- #
# Normalisation                                                               #
# --------------------------------------------------------------------------- #


def _one_line(value: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Collapse *value* to a single line of at most *limit* characters.

    Command output and error signatures are multi-line free text; one signal
    that renders as forty lines would bury the other seven categories.
    """

    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _iso(value: Any) -> str | None:
    """Return *value* as a timestamp string, or ``None``.

    Never substitutes "now" for a missing timestamp: ``None`` is the honest
    answer, and the renderer prints it as an em dash.
    """

    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------------- #
# Sources                                                                     #
# --------------------------------------------------------------------------- #


def _load_ledger(root: Path, session_id: str) -> tuple[dict[str, Any] | None, Path | None]:
    """Return the ``run.json`` snapshot and its path, or ``(None, path_or_None)``.

    Plain JSON on purpose -- see the module docstring: ``RunLedger.load`` drops
    every event's ``at`` and raises on a corrupt file. Both are disqualifying
    for a report that exists to cite evidence. The path is returned even when
    the parse fails so the caller can still say which file it could not read.
    """

    run_file: Path | None = None
    try:
        from lemoncrow.core.foundation.paths import find_session_dir

        directory = find_session_dir(root, session_id)
        if directory is None:
            return None, None
        run_file = directory / "run.json"
        if not run_file.is_file():
            return None, run_file
        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except Exception:
        return None, run_file
    return (payload if isinstance(payload, dict) else None), run_file


def _retention_cap() -> int:
    """Return the ledger's live event cap, so truncation detection tracks it.

    Read from ``run_ledger`` rather than hardcoded because the cap is
    env-overridable (``LEMONCROW_MAX_LEDGER_EVENTS``); a stale constant here
    would either miss real truncation or cry wolf about it.
    """

    try:
        from lemoncrow.infra.runtime.run_ledger import _MAX_RETAINED_EVENTS

        cap = int(_MAX_RETAINED_EVENTS)
    except Exception:
        return EVENT_RETENTION_FALLBACK
    return cap if cap > 0 else EVENT_RETENTION_FALLBACK


def _events(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the snapshot's event dicts, tolerating any other shape."""

    raw = snapshot.get("events")
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


# --------------------------------------------------------------------------- #
# The §2.4 event -> category table                                            #
# --------------------------------------------------------------------------- #


def _payload_failed(payload: dict[str, Any]) -> bool:
    """Return whether *payload* records a failure, by its own fields only.

    Checks the flags the recorders actually write -- ``ok`` for
    ``command_result`` (``run_ledger.py:452-456``), ``passed`` for
    ``test_result`` (``run_ledger.py:482-486``), ``status`` for ``rubric_run``
    (``mcp_server.py:4403``) -- plus the generic shapes imported sessions carry.
    Absence of every flag means "no failure recorded", not "success".
    """

    for flag in ("ok", "passed", "success"):
        if payload.get(flag) is False:
            return True
    exit_code = payload.get("exit_code", payload.get("returncode"))
    if isinstance(exit_code, bool):
        return exit_code
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    if isinstance(exit_code, str) and exit_code.strip().lstrip("-").isdigit() and int(exit_code) != 0:
        return True
    status = payload.get("status")
    if isinstance(status, str) and status.strip().casefold() in _FAILED_STATUS_VALUES:
        return True
    for key in ("error", "error_signature"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if value is True:
            return True
    return _nested_failure(payload)


def _nested_failure(payload: dict[str, Any]) -> bool:
    """Return whether any single-level nested record reports ``passed is False``.

    ``validation`` events carry their outcome in a list of normalised result
    dicts (``mcp_server.py:4067-4083``), not on the payload root.
    """

    for value in payload.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict) and (item.get("passed") is False or item.get("ok") is False):
                return True
    return False


def _classify_event(kind: str, payload: dict[str, Any]) -> tuple[AttributionCategory, str] | None:
    """Return ``(category, prefix)`` for one ledger event, or ``None`` to ignore it.

    The table is §2.4's, in its order. ``None`` is the common answer: a healthy
    ``tool_call`` is not evidence of anything and must not become a signal, or
    every run would look equally suspicious.
    """

    if kind == "command_result":
        return ("shell", "command failed") if _payload_failed(payload) else None
    if kind in ("validation", "test_result"):
        return ("repository", "check failed") if _payload_failed(payload) else None
    if kind == "watchdog_alert":
        return ("lemoncrow", "watchdog alert")
    if kind == "rubric_run":
        status = payload.get("status")
        blocked = isinstance(status, str) and status.strip().casefold() == "blocked"
        return ("policy", "rubric blocked the run") if blocked else None
    if kind in ("route_decision", "model_recommendation"):
        return ("provider", kind.replace("_", " "))
    # "anything else that failed" -- reported, but explicitly unattributed.
    return ("unknown", f"{kind} reported a failure") if _payload_failed(payload) else None


def _signals_from_events(events: list[dict[str, Any]], run_path: Path | None) -> list[_Raw]:
    """Bucket ledger events, citing each one by its index in the persisted array."""

    anchor = str(run_path) if run_path is not None else "run.json"
    out: list[_Raw] = []
    for index, event in enumerate(events):
        kind = str(event.get("kind") or "").strip()
        if not kind:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        verdict = _classify_event(kind, payload)
        if verdict is None:
            continue
        category, prefix = verdict
        detail = _one_line(event.get("summary") or "")
        signature = _one_line(payload.get("error_signature") or "")
        if signature and signature not in detail:
            detail = f"{detail} ({signature})" if detail else signature
        summary = _one_line(f"{prefix}: {detail}" if detail else prefix)
        out.append((category, summary, f"{anchor}#events[{index}]", _iso(event.get("at"))))
    return out


def _classify_error(text: str) -> AttributionCategory:
    """Return the category of one ``errors_seen`` entry, per the §2.4 patterns.

    An entry matching neither pattern stays ``unknown``. Guessing ``model``
    because the string mentions a model name is exactly the failure mode this
    report is built to avoid.
    """

    if _PROVIDER_ERROR_RE.search(text):
        return "provider"
    if _MODEL_ERROR_RE.search(text):
        return "model"
    return "unknown"


def _signals_from_trace(trace: Any) -> list[_Raw]:
    """Bucket a trace's ``errors_seen`` and ``repeated_failures``.

    ``errors_seen`` entries carry no per-entry timestamp, so ``at`` is ``None``
    rather than the trace's own creation time -- that would date every error to
    the moment the session was imported.
    """

    trace_id = str(getattr(trace, "id", "") or "")
    anchor = f"trace:{trace_id}" if trace_id else "trace"
    out: list[_Raw] = []

    errors = getattr(trace, "errors_seen", None) or []
    for index, entry in enumerate(errors):
        text = _one_line(entry)
        if not text:
            continue
        out.append((_classify_error(text), f"recorded error: {text}", f"{anchor}#errors_seen[{index}]", None))

    failures = getattr(trace, "repeated_failures", None) or []
    for index, failure in enumerate(failures):
        signature = _one_line(getattr(failure, "signature", failure))
        if not signature:
            continue
        repeats = getattr(failure, "count", 0)
        repeats = repeats if isinstance(repeats, int) and repeats > 0 else 1
        out.append(
            (
                "model",
                _one_line(f"failure repeated {repeats}x without resolution: {signature}"),
                f"{anchor}#repeated_failures[{index}]",
                _iso(getattr(failure, "last_seen_at", None)),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Assembly                                                                    #
# --------------------------------------------------------------------------- #


def _collapse(raw: list[_Raw]) -> list[AttributionSignal]:
    """Collapse identical ``(category, summary)`` pairs, keeping the first citation.
    A loop that fails the same command 300 times is one finding, not 300. The
    surviving ``evidence_path`` points at the first occurrence and ``count``
    carries the rest (rendered ``xN``), so nothing is silently dropped.
    carries the rest, so nothing is silently dropped.
    """

    order: list[tuple[str, str]] = []
    first: dict[tuple[str, str], tuple[str, str | None]] = {}
    counts: dict[tuple[str, str], int] = {}
    for category, summary, evidence_path, at in raw:
        key = (category, summary)
        if key not in counts:
            order.append(key)
            first[key] = (evidence_path, at)
            counts[key] = 0
        counts[key] += 1
    signals: list[AttributionSignal] = []
    for key in order:
        category, summary = key
        evidence_path, at = first[key]
        signals.append(
            AttributionSignal(
                category=_as_category(category),
                summary=summary,
                evidence_path=evidence_path,
                at=at,
                count=counts[key],
            )
        )
    return signals


def _as_category(value: str) -> AttributionCategory:
    """Narrow *value* to the Literal, defaulting to ``unknown`` off the table."""

    if value in ATTRIBUTION_CATEGORY_VALUES:
        # Every producer above emits a table value; this keeps the narrowing
        # total so a future producer typo degrades instead of crashing.
        return value  # type: ignore[return-value]
    return "unknown"


def _trim(signals: list[AttributionSignal], limit: int) -> tuple[AttributionSignal, ...]:
    """Return *signals* sorted per §2.4 and capped at *limit* per category."""

    ordered = sorted(signals, key=lambda signal: (signal.category, -signal.count, signal.summary))
    kept: list[AttributionSignal] = []
    seen: dict[str, int] = {}
    for signal in ordered:
        taken = seen.get(signal.category, 0)
        if taken >= limit:
            continue
        seen[signal.category] = taken + 1
        kept.append(signal)
    return tuple(kept)


def _counts(signals: list[AttributionSignal]) -> tuple[tuple[str, int], ...]:
    """Return full per-category occurrence totals, biggest first.

    Counted *before* trimming and over ``count`` rather than over rows, so the
    totals stay true even when the renderer is showing five of eighty.
    """

    totals: dict[str, int] = {}
    for signal in signals:
        totals[signal.category] = totals.get(signal.category, 0) + signal.count
    return tuple(sorted(totals.items(), key=lambda item: (-item[1], item[0])))


def explain_run_attribution(
    root: Path,
    run_id: str,
    *,
    limit_per_category: int = DEFAULT_SIGNAL_LIMIT,
) -> AttributionReport:
    """Return what the recorded evidence says about one run.

    *run_id* is an exact session id or a unique prefix. ``ValueError`` on an
    unknown or ambiguous id is the only failure mode -- there is no honest
    report about a session that cannot be identified. Every other gap (missing
    ledger, missing trace, unreadable usage) degrades to a named entry in
    ``unresolved`` and a report that says less.
    """

    from lemoncrow.pro.capabilities.usage.explain import resolve_run_id

    root = Path(root)
    session_id = resolve_run_id(root, run_id)
    limit = max(1, int(limit_per_category))

    unresolved: list[str] = []
    raw: list[_Raw] = []

    snapshot, run_path = _load_ledger(root, session_id)
    if snapshot is None:
        unresolved.append("run_ledger")
        status = "unknown"
    else:
        events = _events(snapshot)
        if len(events) >= _retention_cap():
            unresolved.append("events_truncated")
        raw.extend(_signals_from_events(events, run_path))
        status = str(snapshot.get("status") or "").strip() or "unknown"

    trace = _load_trace(root, session_id)
    if trace is None:
        unresolved.append("trace")
    else:
        raw.extend(_signals_from_trace(trace))

    cost = _load_cost(root, session_id)
    if not cost:
        unresolved.append("cost")

    started_at = _iso((snapshot or {}).get("created_at")) or _iso(getattr(trace, "created_at", None))
    # There is no ``ended_at`` anywhere in the substrate. A terminal status is
    # the only record that a session closed itself (``RunLedger.close`` moves it
    # off "running"), so an ended_at is only claimed when one exists.
    closed = snapshot is not None and status not in ("", "running", "unknown")
    ended_at = _iso(snapshot.get("updated_at")) if closed and snapshot is not None else None
    if snapshot is not None and status == "running":
        raw.append(
            (
                "host",
                "run ledger still reads status=running: the session stopped without recording a terminal event",
                f"{run_path}#status",
                _iso(snapshot.get("updated_at")),
            )
        )

    signals = _collapse(raw)
    return AttributionReport(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        host=_host(snapshot, trace, cost),
        model=_model(trace, cost),
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        signals=_trim(signals, limit),
        category_counts=_counts(signals),
        cost=cost,
        unresolved=tuple(unresolved),
    )


def _load_trace(root: Path, session_id: str) -> Any | None:
    """Return the session's trace, or ``None``. Reuses the review correlator."""

    try:
        from lemoncrow.pro.capabilities.review.provenance import load_trace_for_session

        return load_trace_for_session(root, session_id)
    except Exception:
        return None


def _load_cost(root: Path, session_id: str) -> dict[str, Any]:
    """Return ``usage.explain_run``'s payload, or ``{}``.

    Delegated rather than recomputed: two accounting paths over the same
    session would eventually disagree, and the one that disagrees silently is
    always the one nobody is looking at.
    """

    try:
        from lemoncrow.pro.capabilities.usage.explain import explain_run

        payload = explain_run(root, session_id)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _host(snapshot: dict[str, Any] | None, trace: Any | None, cost: dict[str, Any]) -> str:
    """Resolve the host: usage's path-derived answer first, then the raw records."""

    for candidate in (
        cost.get("host"),
        getattr(trace, "host", None),
        (snapshot or {}).get("agent"),
    ):
        text = str(candidate or "").strip()
        if text and text != "unknown":
            return text
    return "unknown"


def _model(trace: Any | None, cost: dict[str, Any]) -> str:
    """Resolve the run's lead model; empty string when nothing recorded one."""

    for candidate in (cost.get("model"), getattr(trace, "model", None)):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


# --------------------------------------------------------------------------- #
# Render                                                                      #
# --------------------------------------------------------------------------- #


def render_attribution(report: AttributionReport) -> str:
    """Render *report* as plain text: evidence table, cost, and what was missing.

    Colour-free and Rich-free. The "NOT SEEN" block is not an afterthought --
    it is the half of the output that stops a reader concluding a cause from an
    empty category.
    """

    out: list[str] = [f"RUN EXPLAIN  {report.session_id}"]
    out.append(f"{report.host} · {report.model or 'model unrecorded'} · status={report.status}")
    out.append(f"started {report.started_at or '—'} · ended {report.ended_at or '—'}")

    _render_cost(out, report.cost)
    _render_signals(out, report)
    _render_unresolved(out, report.unresolved)

    out.append("")
    out.append("Each line cites a record on disk. Categories with no recorded evidence are omitted,")
    out.append("not cleared — absence here is absence of evidence, not evidence of absence.")
    return "\n".join(out)


def _render_cost(out: list[str], cost: dict[str, Any]) -> None:
    if not cost:
        return
    total = cost.get("total_cost_usd")
    tokens = cost.get("total_tokens")
    provenance = str(cost.get("cost_provenance") or "unknown")
    amount = "unpriced" if total is None else f"${float(total):.4f}"
    out.append("")
    out.append("COST")
    out.append(f"  {amount}  ·  {int(tokens or 0):,} tokens  ·  {provenance}")
    breakdown = cost.get("breakdown")
    if isinstance(breakdown, list):
        for entry in breakdown[:3]:
            if not isinstance(entry, dict) or not entry.get("tokens"):
                continue
            share = float(entry.get("share") or 0.0) * 100.0
            bucket_cost = entry.get("cost_usd")
            priced = "—" if bucket_cost is None else f"${float(bucket_cost):.4f}"
            bucket = str(entry.get("bucket") or "")
            out.append(f"    {bucket:<12} {share:5.1f}%  {priced}")


def _render_signals(out: list[str], report: AttributionReport) -> None:
    out.append("")
    if not report.signals:
        out.append("EVIDENCE")
        out.append("  no failure evidence was recorded for this run")
        return

    total = sum(count for _, count in report.category_counts)
    buckets = len(report.category_counts)
    out.append(
        f"EVIDENCE ({total} occurrence{'' if total == 1 else 's'} "
        f"across {buckets} categor{'y' if buckets == 1 else 'ies'})"
    )
    shown: dict[str, int] = {}
    for signal in report.signals:
        shown[signal.category] = shown.get(signal.category, 0) + signal.count
        multiplier = f"x{signal.count}" if signal.count > 1 else ""
        out.append(f"  {signal.category.upper():<11}{multiplier:<6}{signal.summary}")
        out.append(f"  {'':<11}{'':<6}{signal.evidence_path}  {signal.at or ''}".rstrip())
    for name, count in report.category_counts:
        if shown.get(name, 0) < count:
            out.append(f"  … {name}: showing {shown.get(name, 0)} of {count} occurrences (raise --limit)")
    out.append("")
    out.append("WHAT A CATEGORY MEANS")
    for name, _ in report.category_counts:
        out.append(f"  {name.upper():<11}{CATEGORY_MEANING.get(name, '')}")


def _render_unresolved(out: list[str], unresolved: tuple[str, ...]) -> None:
    if not unresolved:
        return
    out.append("")
    out.append("NOT SEEN")
    for name in unresolved:
        out.append(f"  {name:<18}{UNRESOLVED_REASON.get(name, 'source unavailable')}")


__all__ = [
    "DEFAULT_SIGNAL_LIMIT",
    "UNRESOLVED_REASON",
    "explain_run_attribution",
    "render_attribution",
]
