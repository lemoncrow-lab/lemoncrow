"""OpenTelemetry exporter for product telemetry events."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

logger: logging.Logger | None = None
_PROVIDER: Any = None
_last_check_failed_at: float | None = None
"""Timestamp (monotonic) of the last failed TCP check, or None."""

_CHECK_COOLDOWN_SECONDS = 5.0
"""Minimum seconds between TCP reachability retries after a failure.

When the collector is unreachable we cache the negative result so
that rapid-fire ``emit_product_log`` calls (e.g. during service
startup) don't all hammer the network in parallel.
"""

_logger = logging.getLogger("atelier.product.telemetry.otel")


class _OtelNoiseFilter(logging.Filter):
    """Silence OTel SDK / HTTP-client log messages that are harmless when the
    collector is temporarily unavailable:

    * ``Timeout was exceeded in force_flush()``
    * ``Exception while exporting logs`` + traceback
    * ``Failed to resolve`` / ``Connection refused`` etc.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        return not (name.startswith("opentelemetry") or name in ("urllib3.connectionpool", "requests"))


_sdk_noise_filter = _OtelNoiseFilter()


def _apply_silence() -> None:
    """Apply the noise filter to every existing (and future discoverable)
    logger whose name matches OTel / HTTP-client namespaces."""
    for _ln, _lv in list(logging.Logger.manager.loggerDict.items()):
        if (
            isinstance(_lv, logging.Logger)
            and not any(f is _sdk_noise_filter for f in _lv.filters)
            and (_ln.startswith("opentelemetry") or _ln in ("urllib3.connectionpool", "requests"))
        ):
            _lv.addFilter(_sdk_noise_filter)

    # Also catch NEW loggers by wrapping getLogger — all OTel SDK loggers
    # are created via logging.getLogger, so our wrapper injects the filter
    # before any handler can emit them.
    _orig_get_logger = logging.getLogger

    def _silencing_get_logger(name: str | None = None) -> logging.Logger:
        logger = _orig_get_logger(name)
        if not any(f is _sdk_noise_filter for f in logger.filters) and (
            isinstance(name, str)
            and (name.startswith("opentelemetry") or name in ("urllib3.connectionpool", "requests"))
        ):
            logger.addFilter(_sdk_noise_filter)
        return logger

    logging.getLogger = _silencing_get_logger  # type: ignore[misc]


_apply_silence()


def init_otel(*, endpoint: str = "http://localhost:4318", service_version: str = "0.1.0") -> bool:
    global logger, _PROVIDER, _last_check_failed_at
    import time as _time

    if logger is not None:
        return True

    # Negative cache: if we recently failed a TCP check, skip retrying for a
    # short window to avoid hammering the network (common during service
    # startup when many requests arrive before the collector is ready).
    if _last_check_failed_at is not None and _time.monotonic() - _last_check_failed_at < _CHECK_COOLDOWN_SECONDS:
        return False

    # Quick connectivity check — avoid creating the OTel pipeline (and its
    # background BatchLogRecordProcessor thread) when the collector is not
    # reachable.  This prevents two kinds of noise at startup:
    #   • force_flush() timeout warnings during shutdown
    #   • "Exception while exporting logs" tracebacks from the worker thread
    # If the check fails, logger stays None and the next emit retries.
    endpoint_ok = _check_endpoint_reachable(endpoint)
    if not endpoint_ok:
        _last_check_failed_at = _time.monotonic()
        _logger.debug("collector not reachable at %s — telemetry deferred", endpoint)
        return False

    try:
        from opentelemetry import _logs
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource

        # Limit OTLP exporter retries to avoid long hangs during shutdown.
        # Default is 64s, which can lead to > 2 minutes of total backoff.
        # Setting this to 2 limits it to one attempt plus minimal backoff.
        OTLPLogExporter._MAX_RETRY_TIMEOUT = 2
    except Exception:
        return False

    from atelier.core.foundation.identity import get_anon_id

    resource = Resource.create(
        {
            "service.name": "atelier",
            "service.version": service_version,
            "machine.id": get_anon_id(),
        }
    )
    provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=_logs_endpoint(endpoint), timeout=2),
            schedule_delay_millis=1000,
            export_timeout_millis=2000,
        )
    )
    _logs.set_logger_provider(provider)

    logger = logging.getLogger("atelier.product.telemetry.otel")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not any(isinstance(handler, LoggingHandler) for handler in logger.handlers):
        logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=provider))
    logger = logger
    _PROVIDER = provider
    _last_check_failed_at = None  # clear negative cache
    # Silence any OTel loggers that were created during the imports above.
    _apply_silence()
    return True


def _check_endpoint_reachable(endpoint: str) -> bool:
    """Return True if the OTel collector host:port is accepting TCP connections."""
    import socket

    raw = endpoint.removeprefix("http://").removeprefix("https://")
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    if ":" in raw:
        host, port_str = raw.rsplit(":", 1)
    else:
        host = raw
        port_str = "4318"
    try:
        port = int(port_str)
    except ValueError:
        return False
    try:
        sock = socket.create_connection((host, port), timeout=0.5)
        sock.close()
        return True
    except OSError:
        return False


def emit_product_log(event_name: str, props: dict[str, Any]) -> bool:
    if logger is None:
        from atelier.core.service.telemetry.config import otel_endpoint

        if not init_otel(endpoint=otel_endpoint()):
            return False
    if logger is None:
        return False
    try:
        from opentelemetry._logs.severity import SeverityNumber
        from opentelemetry.sdk._logs import LogRecord
        from opentelemetry.trace import TraceFlags, get_current_span

        # Flatten dict values to OTel-compatible types (str, int, float, bool)
        flat_attrs = {"event.name": event_name}
        for key, value in props.items():
            if isinstance(value, (dict, list, tuple)):
                flat_attrs[key] = json.dumps(value, ensure_ascii=False)
            else:
                flat_attrs[key] = value

        # Get current span context if available
        span = get_current_span()
        span_context = span.get_span_context() if span else None

        # Create LogRecord with proper span context
        record = LogRecord(
            body=event_name,
            attributes=flat_attrs,
            span_id=span_context.span_id if span_context else 1,
            trace_id=span_context.trace_id if span_context else 1,
            trace_flags=span_context.trace_flags if span_context else TraceFlags(0),
            severity_text="DEBUG",
            severity_number=SeverityNumber.DEBUG,
        )
        _PROVIDER.get_logger("atelier.product.telemetry.otel").emit(record)
        return True
    except Exception:
        return False


def shutdown_otel() -> None:
    global logger, _PROVIDER, _last_check_failed_at
    provider = _PROVIDER
    logger = None
    _PROVIDER = None
    _last_check_failed_at = None  # clear negative cache
    if provider is not None:
        with contextlib.suppress(Exception):
            provider.shutdown()


def _logs_endpoint(endpoint: str) -> str:
    cleaned = endpoint.rstrip("/")
    if cleaned.endswith("/v1/logs"):
        return cleaned
    return f"{cleaned}/v1/logs"
