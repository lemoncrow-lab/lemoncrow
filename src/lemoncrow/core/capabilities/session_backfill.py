"""Backfill the local savings ledger from sessions LemonCrow never saw.

The live ledger (``sessions/**/savings.jsonl``) only grows while LemonCrow's
own hooks/MCP server are active *during* a session -- it cannot know what an
already-completed session run without LemonCrow (or before it was installed)
would have saved. :mod:`session_replay` already reconstructs that
counterfactual per session (:func:`estimate_savings`, the same engine behind
``lc session replay``); this module is the missing write path -- it persists
one synthetic row per qualifying historical session into the SAME ledger
format the live path writes, dated to when the session actually happened, so
``lc savings`` / ``lc account cap`` / the statusline immediately reflect it
after :func:`reconcile_savings_aggregate` runs.

Idempotent and additive only: a session already carrying a ``savings.jsonl``
(written live, or by a prior backfill run) is always skipped, so re-running
never double-counts and never touches real measured data.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lemoncrow.core.capabilities.session_replay import SUPPORTED_HOSTS, Replay, estimate_savings, load_replays

__all__ = ["SUPPORTED_HOSTS", "BackfillResult", "BackfilledSession", "backfill_host_savings"]

_DEFAULT_MIN_SAVED_USD = 0.01


@dataclass
class BackfilledSession:
    host: str
    session_id: str
    saved_usd: float
    calls_saved: int
    when: datetime


@dataclass
class BackfillResult:
    scanned: int = 0
    already_tracked: int = 0
    below_threshold: int = 0
    ran_with_lemoncrow: int = 0
    unparseable: int = 0
    load_error: str | None = None
    backfilled: list[BackfilledSession] = field(default_factory=list)

    @property
    def total_saved_usd(self) -> float:
        return round(sum(s.saved_usd for s in self.backfilled), 4)


def _opencode_session_time(session_id: str) -> datetime | None:
    """``time_created`` for an opencode session (ms epoch), or None."""
    from lemoncrow.core.capabilities.session_replay import _opencode_db_path
    from lemoncrow.gateway.hosts.session_parsers.opencode import find_opencode_sessions

    db = _opencode_db_path()
    if db is None:
        return None
    try:
        for row in find_opencode_sessions(db):
            if str(row.get("id") or "") == session_id:
                ms = row.get("time_created")
                if ms is not None:
                    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
    except (sqlite3.Error, TypeError, ValueError, OSError):
        pass
    return None


def _session_when(replay: Replay) -> datetime:
    """Best-effort real timestamp for the session, so backfilled rows land in
    the correct historical day-bucket instead of all piling onto \"today\"."""
    if replay.host == "opencode":
        when = _opencode_session_time(replay.session_id)
        if when is not None:
            return when
    if replay.source_path:
        try:
            mtime = Path(replay.source_path).stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=UTC)
        except OSError:
            pass
    return datetime.now(UTC)


def _sidecar_path(root: Path, host: str, session_id: str, when: datetime) -> Path:
    # Mirrors the live layout (paths.session_dir) but pinned to the session's
    # REAL date -- session_dir()'s "last 3 days" search would otherwise mint
    # today's folder for sessions months old.
    return (
        root
        / "sessions"
        / when.strftime("%Y")
        / when.strftime("%m")
        / when.strftime("%d")
        / host
        / session_id
        / "savings.jsonl"
    )


def _existing_sidecar(root: Path, host: str, session_id: str) -> Path | None:
    """Any savings.jsonl already written for this session, in WHATEVER day
    folder it landed in.

    The date-pinned ``_sidecar_path`` check alone re-backfills a session whose
    transcript mtime moved (``claude --resume``, a copy/restore that didn't
    preserve timestamps) between runs, double-counting it. ``find_session_dir``
    globs by the high-entropy id alone, so a prior live or backfilled sidecar is
    recognised regardless of the date folder it was filed under.
    """
    from lemoncrow.core.foundation.paths import find_session_dir

    found = find_session_dir(root, session_id)
    if found is not None and found.name == session_id and found.parent.name == host:
        sidecar = found / "savings.jsonl"
        if sidecar.exists():
            return sidecar
    return None


def backfill_host_savings(
    root: Path,
    host: str,
    *,
    limit: int,
    min_saved_usd: float = _DEFAULT_MIN_SAVED_USD,
    dry_run: bool = False,
) -> BackfillResult:
    """Reconstruct and persist savings for up to *limit* recent ``host`` sessions.

    Only vanilla sessions (never touched by LemonCrow) with an estimated
    saving above *min_saved_usd* get a row; everything else is skipped and
    counted in the result for transparency. ``dry_run`` computes the same
    result without writing anything.
    """
    result = BackfillResult()
    try:
        replays = load_replays(host=host, last=max(1, limit), store_root=root)
    except Exception as exc:  # noqa: BLE001 -- a broken/foreign store must never crash the scan, but surface it
        result.load_error = str(exc).strip() or exc.__class__.__name__
        return result
    for replay in replays:
        result.scanned += 1
        if not replay.session_id:
            result.unparseable += 1
            continue
        when = _session_when(replay)
        sidecar = _sidecar_path(root, host, replay.session_id, when)
        if sidecar.exists() or _existing_sidecar(root, host, replay.session_id) is not None:
            result.already_tracked += 1
            continue
        try:
            estimate = estimate_savings(replay)
        except Exception:  # noqa: BLE001 -- one bad transcript must not abort the scan
            result.unparseable += 1
            continue
        if estimate.get("ran_with_lemoncrow"):
            # Already used LemonCrow: either it has its own live sidecar
            # (caught above) or its savings are attributed elsewhere (e.g. a
            # subagent, billed to the parent) -- never estimate on top.
            result.ran_with_lemoncrow += 1
            continue
        saved_usd = round(float(estimate.get("saved_usd") or 0.0), 4)
        if saved_usd < min_saved_usd:
            result.below_threshold += 1
            continue
        calls_saved = int(estimate.get("calls_saved") or 0)
        row = {
            "tool": "code_search",
            "kind": "backfill",
            # Clamp into _price_savings_row's honored band (0 < tokens <= 2M):
            # tokens==0 or >2M make that function drop cost_saved_usd entirely,
            # so the CLI would report "$X backfilled" that lc savings then prices
            # at $0 (the same trap _RECONCILE_PLACEHOLDER_TOKENS dodges).
            "tokens": max(1, min(int(estimate.get("collapsed_output_tokens") or 0), 2_000_000)),
            "calls": calls_saved,
            "model": estimate.get("model") or replay.model or "",
            "ts": when.replace(tzinfo=None).isoformat(),
            "cost_saved_usd": saved_usd,
        }
        if not dry_run:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(row) + "\n", encoding="utf-8")
        result.backfilled.append(
            BackfilledSession(
                host=host, session_id=replay.session_id, saved_usd=saved_usd, calls_saved=calls_saved, when=when
            )
        )
    return result
