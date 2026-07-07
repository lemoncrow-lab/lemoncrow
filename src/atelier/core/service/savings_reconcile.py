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

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_POLL_SECONDS = 300.0
_DISABLED_VALUES = {"0", "false", "no", "off"}


def _reconcile_enabled() -> bool:
    raw = os.getenv("ATELIER_SERVICE_SAVINGS_RECONCILE", "1").strip().lower()
    return raw not in _DISABLED_VALUES


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
                reconcile_savings_aggregate(self._root)
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
