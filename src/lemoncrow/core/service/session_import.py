"""Internal session-import worker for periodic maintenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lemoncrow.core.foundation.models import Trace

logger = logging.getLogger(__name__)
_IMPORT_PROGRESS_LOGGER = "lemoncrow.gateway.hosts.session_parsers"
_IMPORT_PROGRESS_HANDLER_FLAG = "_lemoncrow_import_progress_handler"
# Per-session usage reports for imported sessions (see report_imported_sessions).
# At most this many go out per run -- the ingest endpoint accepts 20 new sessions
# per install per 4 hours (MAX_ACTIVE_SESSIONS_PER_DEVICE in rollup.ts) -- and the
# next 30-minute run picks up whatever changed since.
_REPORT_LIMIT_PER_RUN = 20
# The endpoint ignores an occurred_at outside now +/- 48h and stamps server time
# instead, which would pile older history onto today's public per-day chart.
_REPORT_WINDOW = timedelta(hours=48)
_REPORT_MAX_TURNS = 10_000  # the endpoint rejects a larger turn_count outright


def _ensure_import_progress_logging() -> None:
    """Send parser diagnostics to stderr without accumulating handlers."""
    progress_logger = logging.getLogger(_IMPORT_PROGRESS_LOGGER)
    for handler in progress_logger.handlers:
        if getattr(handler, _IMPORT_PROGRESS_HANDLER_FLAG, False):
            if isinstance(handler, logging.StreamHandler):
                if getattr(handler.stream, "closed", False):
                    progress_logger.removeHandler(handler)
                    break
                handler.setStream(sys.stderr)
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _IMPORT_PROGRESS_HANDLER_FLAG, True)
    progress_logger.addHandler(handler)
    progress_logger.propagate = False
    if progress_logger.level == logging.NOTSET or progress_logger.level > logging.INFO:
        progress_logger.setLevel(logging.INFO)


def import_agent_sessions(root: Path, *, store: Any | None = None) -> dict[str, int]:
    """Import supported hosts independently, then refresh imported usage."""
    from lemoncrow.gateway.hosts.session_parsers.registry import iter_importer_classes
    from lemoncrow.infra.storage.factory import create_store

    store = store if store is not None else create_store(root)
    store.init()
    counts: dict[str, int] = {}
    imported_ids: list[str] = []
    for name, importer_cls in iter_importer_classes():
        try:
            importer = importer_cls(store)
            # Release the SQLite writer lock between hosts (GH #43).
            with store.history.batch_mode():
                ids = importer.import_all()
            counts[name] = len(ids)
            imported_ids.extend(ids)
        except Exception:
            logger.exception("session importer failed for host %s", name)
    try:
        report_imported_sessions(root, imported_ids, store=store)
    except Exception:
        logger.exception("usage report failed after session import")
    return counts


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _rollup_fields(trace: Trace, *, now: datetime) -> dict[str, Any] | None:
    """The savings-rollup arguments for one imported session, or ``None`` when
    there is nothing to report.

    An explicit allowlist of counts: no ``task``, prompt text, paths or cost ever
    reaches the payload, and the session id is hashed by the rollup client.
    Cost and savings stay 0 -- the ledger is the only source of those, and the
    endpoint rejects a first-seen session whose cost outruns its age.
    """
    started = _aware(trace.created_at)
    if now - started > _REPORT_WINDOW:
        return None
    stamps = [_aware(e.created_at) for e in trace.usage_entries if e.created_at is not None]
    fields: dict[str, Any] = {
        "session_id": trace.session_id or trace.id,
        "saved_usd": 0.0,
        "tokens_saved": 0,
        "calls_avoided": 0,
        "turn_count": min(sum(1 for e in trace.usage_entries if e.kind == "llm"), _REPORT_MAX_TURNS),
        "source": f"import:{trace.host or 'unknown'}",
        "occurred_at": started,
        # Same definitions as the daily rollup's real totals: input + output
        # tokens (no cache), tool calls, first-to-last turn wall time.
        "tokens_processed": trace.input_tokens + trace.output_tokens,
        "calls_made": sum(call.count for call in trace.tools_called),
        "time_spent_seconds": (max(stamps) - min(stamps)).total_seconds() if len(stamps) > 1 else 0.0,
    }
    if not (fields["turn_count"] or fields["tokens_processed"] or fields["calls_made"] or fields["time_spent_seconds"]):
        return None
    return fields


def report_imported_sessions(root: Path, trace_ids: list[str], *, store: Any) -> int:
    """Report freshly imported sessions through the public savings rollup.

    This is the existing rollup client, not a second uploader, so the
    destination and every opt-out come from it: the pytest guard, DO_NOT_TRACK /
    LEMONCROW_TELEMETRY, an administrator policy, then the saved destination.
    Off means nothing is read, sent or marked. An enterprise or custom
    destination receives these at its own ``/api/telemetry/rollup``, never at
    lemoncrow.com.

    Sessions already in the savings ledger are skipped, because the daily rollup
    reports those and the public totals would count them twice. A session whose
    payload hash matches what ``sync_status`` recorded is not resent; a changed
    one is, and the endpoint's per-``session_key`` upsert (last write wins) keeps
    that idempotent server-side too. The first failed post ends the run: the
    rest are tried when they are next imported.

    Returns how many sessions were accepted.
    """
    from lemoncrow.core.capabilities.savings_summary import ledger_session_ids
    from lemoncrow.core.service.telemetry.public_rollup import (
        FLUSH_TIMEOUT_SECONDS,
        public_rollup_endpoint,
        publish_public_savings_rollup,
    )

    ids = list(dict.fromkeys(trace_ids))
    if not ids or not public_rollup_endpoint():
        return 0
    history = store.history
    already_sent = history.synced_payload_hashes(ids)
    in_ledger = ledger_session_ids(root)
    now = datetime.now(UTC)
    accepted = 0
    for trace_id in ids:
        if accepted >= _REPORT_LIMIT_PER_RUN:
            break
        trace = history.get_trace(trace_id)
        if trace is None or any(key in in_ledger for key in (trace.id, trace.session_id) if key):
            continue
        fields = _rollup_fields(trace, now=now)
        if fields is None:
            continue
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if already_sent.get(trace_id) == digest:
            continue
        if not publish_public_savings_rollup(**fields, timeout_s=FLUSH_TIMEOUT_SECONDS):
            break
        history.mark_synced(trace_id, digest)
        accepted += 1
    return accepted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    _ensure_import_progress_logging()
    print(json.dumps(import_agent_sessions(args.root)))


if __name__ == "__main__":
    main()
