"""Product telemetry emission entry points."""

from __future__ import annotations

import logging
import time
from typing import Any

from atelier.core.service.telemetry.config import (
    posthog_key,
    posthog_otlp_url,
    remote_enabled,
    save_telemetry_config,
)
from atelier.core.service.telemetry.local_store import LocalTelemetryStore
from atelier.core.service.telemetry.schema import validate_event_props
from atelier.core.service.telemetry.scrubber import scrub_props

logger = logging.getLogger("atelier.product.telemetry")


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
        logger.debug("telemetry.otel_init_failed", extra={"error": str(exc)})
        return False


def emit_product(event: str, **props: Any) -> None:
    _emit(event, props, remote=True)


def emit_product_local(event: str, **props: Any) -> None:
    _emit(event, props, remote=False)


def set_remote_enabled(value: bool) -> None:
    save_telemetry_config(remote_enabled=value)
    if not value:
        try:
            from atelier.core.service.telemetry.exporters.otel import shutdown_otel

            shutdown_otel()
        except Exception:
            logger.warning(
                "Suppressed exception at emit.py:48",
                exc_info=True,
            )


def _emit(event: str, props: dict[str, Any], *, remote: bool) -> None:
    try:
        filtered, dropped = validate_event_props(event, props)
        if filtered is None:
            logger.debug("telemetry.unknown_event", extra={"event": event})
            return
        if dropped:
            logger.debug("telemetry.dropped_props", extra={"event": event, "dropped": sorted(dropped)})
        scrubbed = scrub_props(filtered)
        exported = False
        if remote and remote_enabled():
            exported = _export_remote(event, scrubbed)
        LocalTelemetryStore().write_event(event=event, props=scrubbed, exported=exported, ts=time.time())
    except Exception as exc:
        logger.debug("telemetry.emit_failed", extra={"event": event, "error": str(exc)})


def _export_remote(event: str, props: dict[str, Any]) -> bool:
    try:
        from atelier.core.service.telemetry.exporters.otel import emit_product_log

        return emit_product_log(event, props)
    except Exception as exc:
        logger.debug("telemetry.remote_export_failed", extra={"event": event, "error": str(exc)})
        return False
