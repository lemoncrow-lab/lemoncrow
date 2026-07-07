"""Product telemetry emission entry points.

Telemetry is best-effort and MUST NOT sit on the hot path. Producers only
validate + scrub (cheap, in-memory) and hand the event to a single background
worker draining a bounded queue; the SQLite write, prune/VACUUM, and any remote
export all happen off-thread. A full queue drops events rather than blocking a
caller or growing memory. Under pytest (or an explicit override) emission runs
synchronously so ``emit`` -> read-back assertions stay deterministic.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from typing import Any

from atelier.core.service.telemetry.config import (
    posthog_key,
    posthog_otlp_url,
    remote_enabled,
)
from atelier.core.service.telemetry.local_store import LocalTelemetryStore
from atelier.core.service.telemetry.schema import validate_event_props
from atelier.core.service.telemetry.scrubber import scrub_props

logger = logging.getLogger("atelier.product.telemetry")

_MAX_QUEUE = 4096
_BATCH_MAX = 256
_QueueItem = "tuple[str, dict[str, Any], bool] | None"
_queue: queue.Queue[tuple[str, dict[str, Any], bool] | None] | None = None
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_atexit_registered = False


def init_product_telemetry(*, service_version: str = "0.1.0") -> bool:
    if not remote_enabled():
        return False
    try:
        from atelier.core.service.telemetry.exporters.otel import init_otel

        key = posthog_key()
        if not key:
            return False
        return init_otel(
            endpoint=posthog_otlp_url(),
            service_version=service_version,
            headers={"Authorization": f"Bearer {key}"},
        )
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        logger.debug("telemetry.otel_init_failed", extra={"error": str(exc)})
        return False


def emit_product(event: str, **props: Any) -> None:
    _dispatch(event, props, remote=True)


def emit_product_local(event: str, **props: Any) -> None:
    _dispatch(event, props, remote=False)


def _telemetry_sync_mode() -> bool:
    """Whether to write inline instead of on the background worker.

    Explicit ``ATELIER_TELEMETRY_SYNC`` wins; otherwise synchronous under pytest
    so emit-then-read-back assertions stay deterministic.
    """
    override = os.environ.get("ATELIER_TELEMETRY_SYNC")
    if override is not None and override.strip():
        return override.strip().lower() not in ("0", "false", "off", "no")
    return "PYTEST_CURRENT_TEST" in os.environ


def _dispatch(event: str, props: dict[str, Any], *, remote: bool) -> None:
    # Validate + scrub on the caller so allowlist violations surface here and the
    # props are frozen before hand-off. Everything with I/O cost runs off-thread.
    try:
        filtered, dropped = validate_event_props(event, props)
        if filtered is None:
            logger.debug("telemetry.unknown_event", extra={"event": event})
            return
        if dropped:
            logger.debug("telemetry.dropped_props", extra={"event": event, "dropped": sorted(dropped)})
        scrubbed = scrub_props(filtered)
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        logger.debug("telemetry.emit_failed", extra={"event": event, "error": str(exc)})
        return

    if _telemetry_sync_mode():
        _write_one(event, scrubbed, remote=remote)
        return

    q = _ensure_worker()
    try:
        q.put_nowait((event, scrubbed, remote))
    except queue.Full:
        # Best-effort: never block the hot path or grow memory. Drop + note it.
        logger.debug("telemetry.queue_full_dropped", extra={"event": event})


def _ensure_worker() -> queue.Queue[tuple[str, dict[str, Any], bool] | None]:
    global _queue, _worker, _atexit_registered
    with _worker_lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=_MAX_QUEUE)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain_loop, name="atelier-telemetry", daemon=True)
            _worker.start()
        if not _atexit_registered:
            atexit.register(flush_product_telemetry)
            _atexit_registered = True
        return _queue


def _drain_loop() -> None:
    q = _queue
    if q is None:
        return
    while True:
        first = q.get()
        got = 1
        if first is None:
            q.task_done()
            return
        batch: list[tuple[str, dict[str, Any], bool]] = [first]
        stop = False
        while len(batch) < _BATCH_MAX:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            got += 1
            if item is None:
                stop = True
                break
            batch.append(item)
        _flush_batch(batch)
        for _ in range(got):
            q.task_done()
        if stop:
            return


def _flush_batch(batch: list[tuple[str, dict[str, Any], bool]]) -> None:
    now = time.time()
    records: list[dict[str, Any]] = []
    for event, scrubbed, remote in batch:
        exported = _export_remote(event, scrubbed) if (remote and remote_enabled()) else False
        records.append({"event": event, "props": scrubbed, "exported": exported, "ts": now})
    try:
        LocalTelemetryStore().write_events(records)
    except Exception:
        logging.exception("Recovered from broad exception handler")


def _write_one(event: str, scrubbed: dict[str, Any], *, remote: bool) -> None:
    try:
        exported = _export_remote(event, scrubbed) if (remote and remote_enabled()) else False
        LocalTelemetryStore().write_event(event=event, props=scrubbed, exported=exported, ts=time.time())
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        logger.debug("telemetry.emit_failed", extra={"event": event, "error": str(exc)})


def flush_product_telemetry(timeout: float = 2.0) -> None:
    """Best-effort bounded drain of queued telemetry (auto-registered at exit)."""
    q = _queue
    if q is None:
        return
    deadline = time.monotonic() + max(0.0, timeout)
    while q.unfinished_tasks > 0 and time.monotonic() < deadline:
        time.sleep(0.01)


def _export_remote(event: str, props: dict[str, Any]) -> bool:
    try:
        from atelier.core.service.telemetry.exporters.otel import emit_product_log

        return emit_product_log(event, props)
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        logger.debug("telemetry.remote_export_failed", extra={"event": event, "error": str(exc)})
        return False
