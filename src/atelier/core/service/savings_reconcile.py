"""Daemon-owned savings-aggregate reconciler.

Keeps ``<root>/savings_aggregate.json`` — the persisted, day-bucketed
aggregate of every ``sessions/**/savings.jsonl`` — folded up to date so MCP
session processes and the statusline sidecar only ever fold the handful of
ledgers written since the last reconcile, never the whole store.

Pattern mirrors :mod:`atelier.core.service.code_warm`.  Gated by
``ATELIER_SERVICE_SAVINGS_RECONCILE`` (default on).  Set to one of
``0``/``false``/``no``/``off`` to disable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_POLL_SECONDS = 300.0
_DISABLED_VALUES = {"0", "false", "no", "off"}

# Day buckets older than this are pruned from savings_aggregate.json so the
# file cannot grow without bound. Override via ATELIER_SAVINGS_RETENTION_DAYS;
# <= 0 disables pruning.
_DEFAULT_RETENTION_DAYS = 365


def _reconcile_enabled() -> bool:
    raw = os.getenv("ATELIER_SERVICE_SAVINGS_RECONCILE", "1").strip().lower()
    return raw not in _DISABLED_VALUES


def _retention_days() -> int:
    raw = os.getenv("ATELIER_SAVINGS_RETENTION_DAYS", "").strip()
    try:
        return int(raw) if raw else _DEFAULT_RETENTION_DAYS
    except ValueError:
        return _DEFAULT_RETENTION_DAYS


def _prune_aggregate_days(root: Path, agg: dict[str, Any], *, retention_days: int) -> bool:
    """Drop per-session day buckets older than the retention window.

    Session entries are kept (their ``(mtime, size)`` stamp stops the next
    reconcile from re-folding the ledger and resurrecting the pruned days);
    only their old day buckets go. The pruned aggregate is persisted with the
    same atomic tmp + rename the reconciler itself uses. Returns whether
    anything was pruned.
    """
    if retention_days <= 0:
        return False
    st = time.gmtime(time.time() - retention_days * 86_400)
    cutoff_day = f"{st.tm_year:04d}-{st.tm_mon:02d}-{st.tm_mday:02d}"
    sessions = agg.get("sessions")
    if not isinstance(sessions, dict):
        return False
    changed = False
    for entry in sessions.values():
        if not isinstance(entry, dict):
            continue
        days = entry.get("days")
        if not isinstance(days, dict):
            continue
        for day in [d for d in days if d < cutoff_day]:
            del days[day]
            changed = True
    if not changed:
        return False
    from atelier.core.capabilities.savings_summary import _SAVINGS_AGGREGATE_FILENAME

    path = root / _SAVINGS_AGGREGATE_FILENAME
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(agg), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("savings aggregate prune persist failed", exc_info=True)
    return True


class _SavingsReconciler:
    """Background loop folding unfolded session ledgers into the aggregate."""

    def __init__(self, root: Path, *, poll_seconds: float = _POLL_SECONDS) -> None:
        self._root = root
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        # NB: not named ``_thread`` — mypyc emits that as a C struct field
        # ``__thread``, which clang rejects as the reserved TLS keyword.
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._loop,
            name="atelier-savings-reconciler",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        from atelier.core.capabilities.savings_summary import reconcile_savings_aggregate

        # First pass immediately, then poll for newly written ledgers.
        while True:
            try:
                agg = reconcile_savings_aggregate(self._root)
                _prune_aggregate_days(self._root, agg, retention_days=_retention_days())
            except Exception:
                logger.exception("savings reconciler: pass failed")
            if self._stop.wait(self._poll_seconds):
                return


_reconciler: _SavingsReconciler | None = None
_reconciler_lock = threading.Lock()


def start_savings_reconciler(root: Path) -> _SavingsReconciler | None:
    """Start the daemon savings reconciler (idempotent singleton).

    Returns ``None`` when disabled via ``ATELIER_SERVICE_SAVINGS_RECONCILE``;
    otherwise returns the singleton reconciler (already started).
    """
    global _reconciler
    if not _reconcile_enabled():
        logger.info("savings reconciler disabled via ATELIER_SERVICE_SAVINGS_RECONCILE")
        return None
    with _reconciler_lock:
        if _reconciler is None:
            _reconciler = _SavingsReconciler(root)
            _reconciler.start()
            logger.info("savings reconciler started")
        return _reconciler
