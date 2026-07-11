"""Service telemetry APIs.

This package intentionally preserves the existing audit/request-timing helpers
while adding the product telemetry entry point used by CLI, MCP, API, and the
dashboard. Product telemetry is local-first and privacy allowlisted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from lemoncrow.core.service.telemetry.emit import (
    emit_product,
    emit_product_local,
    init_product_telemetry,
)
from lemoncrow.core.service.telemetry.exporters import shutdown_otel

logger = logging.getLogger("lemoncrow.service")


def emit_audit(
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    store: Any = None,
) -> None:
    """Write an audit entry.

    If the store supports ``write_audit_log``, calls it. Otherwise falls
    back to a structured log line. Never logs credential values.
    """
    entry: dict[str, Any] = {
        "actor": actor,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
    }
    if store is not None and hasattr(store, "write_audit_log"):
        try:
            store.write_audit_log(**entry)
        except Exception:  # pragma: no cover
            logging.exception("Recovered from broad exception handler")
            logger.warning("audit_log write failed; falling back to log", extra=entry)
    else:
        logger.info("audit", extra=entry)


@contextmanager
def timed_request(endpoint: str) -> Generator[None]:
    """Context manager that logs request duration."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.debug("request", extra={"endpoint": endpoint, "elapsed_ms": round(elapsed_ms, 1)})


__all__ = [
    "emit_audit",
    "emit_product",
    "emit_product_local",
    "init_product_telemetry",
    "shutdown_otel",
    "timed_request",
]
