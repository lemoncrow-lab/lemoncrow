"""OpenTelemetry exporter for product telemetry events."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

logger: logging.Logger | None = None
_PROVIDER: Any = None
_RESOURCE: Any = None
"""Resource of the live pipeline, needed by the pre-1.37 SDK record shim."""

_INIT_ARGS: dict[str, Any] | None = None
"""Arguments of the last *successful* ``init_otel`` call.

Replayed by :func:`_reinit` so an exporter rebuilt after a *deferred* first
init keeps targeting the configured OTLP backend (endpoint *and* auth headers)
instead of silently falling back to the unauthenticated localhost default.
Kept across ``shutdown_otel`` only so an explicit re-init has something to
replay -- the lazy path is fenced off by :data:`_SHUTDOWN`.
"""

_SHUTDOWN = False
"""True once ``shutdown_otel`` has torn the pipeline down.

While set, :func:`_reinit` refuses to rebuild anything: a late
``emit_product_log`` (e.g. from the background telemetry worker, or an atexit
hook) must NOT resurrect a ``LoggerProvider`` +
``BatchLogRecordProcessor`` that nobody will ever shut down. Doing so during
interpreter teardown is what raises "cannot schedule new futures after
interpreter shutdown" out of ``Resource.create()`` (GH #40), and any events
buffered in the resurrected processor are dropped anyway.

Cleared by an explicit :func:`init_otel` call (directly or via
``init_product_telemetry``), so a process that deliberately re-arms telemetry
after a shutdown is never permanently blocked.
"""

_last_check_failed_at: float | None = None
"""Timestamp (monotonic) of the last failed TCP check, or None."""

_CHECK_COOLDOWN_SECONDS = 5.0
"""Minimum seconds between TCP reachability retries after a failure.

When the collector is unreachable we cache the negative result so
that rapid-fire ``emit_product_log`` calls (e.g. during service
startup) don't all hammer the network in parallel.
"""

_TELEMETRY_ROOT = "lemoncrow.product.telemetry"
"""Root of the telemetry logger namespace (shared with ``emit.py``)."""

_logger = logging.getLogger(f"{_TELEMETRY_ROOT}.diagnostics")
"""Diagnostics sink for this module.

Deliberately *not* ``lemoncrow.product.telemetry.otel``: that name is the OTel
export logger built by :func:`init_otel`, which carries a ``LoggingHandler``
and ``propagate = False``. Logging diagnostics there would feed telemetry
failures straight back into the telemetry pipeline.

Everything here is logged at DEBUG with ``exc_info=True`` -- invisible at
default verbosity, full traceback under ``logging.basicConfig(level=DEBUG)``.
"""


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
    """Apply the noise filter so OTel / HTTP-client messages are silenced.

    Idempotent: re-running never stacks duplicate filters. The filter is
    attached to every existing matching logger (covering loggers that carry
    their own handlers or disable propagation), to the named parent loggers
    (covering records logged directly through them), and to
    ``logging.lastResort`` (covering the lazily-created SDK child loggers
    whose records fall back to stderr when no root handler is configured) —
    so no global ``logging.getLogger`` monkeypatch is needed to catch loggers
    created later by the SDK.
    """
    for _ln, _lv in list(logging.Logger.manager.loggerDict.items()):
        if (
            isinstance(_lv, logging.Logger)
            and not any(f is _sdk_noise_filter for f in _lv.filters)
            and (_ln.startswith("opentelemetry") or _ln in ("urllib3.connectionpool", "requests"))
        ):
            _lv.addFilter(_sdk_noise_filter)

    # Attach to the named parent loggers so records logged directly through
    # them (e.g. "urllib3.connectionpool", "requests") are dropped at source.
    for nm in ("opentelemetry", "urllib3.connectionpool", "requests"):
        lg = logging.getLogger(nm)
        if not any(f is _sdk_noise_filter for f in lg.filters):
            lg.addFilter(_sdk_noise_filter)

    # Logger-level filters do NOT cascade to child loggers, and the SDK creates
    # its emitting loggers (opentelemetry.sdk._logs.*) lazily after init. With
    # no root handlers, their records fall back to ``logging.lastResort`` (the
    # implicit stderr handler). Filtering there suppresses those tracebacks for
    # all current + future descendants by name — no root handler, no getLogger
    # monkeypatch.
    if logging.lastResort is not None and not any(f is _sdk_noise_filter for f in logging.lastResort.filters):
        logging.lastResort.addFilter(_sdk_noise_filter)

    # Product telemetry is best-effort and must never write to the user's
    # terminal. Its own diagnostics are DEBUG-level, but a NullHandler on the
    # telemetry root additionally stops *any* record in that namespace from
    # falling through to ``logging.lastResort`` when the application configures
    # no handlers at all -- which is exactly how GH #40 leaked tracebacks into
    # `lc init` output. An explicitly configured root handler still receives
    # them, so `-v` / debug keeps the traceback.
    #
    # This is the single authoritative install for the whole
    # ``lemoncrow.product.telemetry`` namespace (``emit.py`` deliberately does
    # not repeat it). It lives here because ``_apply_silence`` is idempotent
    # and re-runs from ``init_otel``, so the guarantee is re-asserted even if a
    # late ``logging.config.dictConfig`` wiped the root's handlers. Importing
    # either module imports the other via the package ``__init__``, so there is
    # no load order in which this fails to run.
    telemetry_root = logging.getLogger(_TELEMETRY_ROOT)
    if not any(isinstance(h, logging.NullHandler) for h in telemetry_root.handlers):
        telemetry_root.addHandler(logging.NullHandler())


_apply_silence()


def init_otel(
    *,
    endpoint: str = "http://localhost:4318",
    service_version: str = "0.1.0",
    headers: dict[str, str] | None = None,
) -> bool:
    global logger, _PROVIDER, _RESOURCE, _INIT_ARGS, _SHUTDOWN, _last_check_failed_at
    import time as _time

    # An explicit init request re-arms telemetry after a shutdown: clear the
    # fence up front (even if this attempt then fails the reachability check)
    # so the normal deferred-init retry path works again. Only the *lazy*
    # path (:func:`_reinit`) stays fenced off after ``shutdown_otel``.
    _SHUTDOWN = False

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
        OTLPLogExporter._MAX_RETRY_TIMEOUT = 2  # type: ignore[attr-defined]
    except Exception:
        _logger.debug("telemetry.otel_import_failed", exc_info=True)
        return False

    from lemoncrow.core.foundation.identity import get_anon_id

    resource = Resource.create(
        {
            "service.name": "lemoncrow",
            "service.version": service_version,
            "machine.id": get_anon_id(),
        }
    )
    provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=_logs_endpoint(endpoint), timeout=2, headers=headers),
            schedule_delay_millis=1000,
            export_timeout_millis=2000,
        )
    )
    _logs.set_logger_provider(provider)

    logger = logging.getLogger("lemoncrow.product.telemetry.otel")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not any(isinstance(handler, LoggingHandler) for handler in logger.handlers):
        logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=provider))
    logger = logger
    _PROVIDER = provider
    _RESOURCE = resource
    _INIT_ARGS = {"endpoint": endpoint, "service_version": service_version, "headers": headers}
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


def _reinit() -> bool:
    """Rebuild the exporter after ``shutdown_otel`` (or a deferred first init).

    Replays the last *successful* configuration so a re-init keeps targeting
    the configured OTLP backend -- endpoint **and** ``Authorization`` header.
    Without this the lazy path fell back to ``otel_endpoint()``, i.e. the
    unauthenticated ``http://localhost:4318`` default, so events emitted after
    a deferred first init were posted to the wrong place. Falls back to the
    configured endpoint only when no init has ever succeeded in this process.

    Refuses to rebuild anything once ``shutdown_otel`` has run (GH #40): with
    a real, reachable backend in ``_INIT_ARGS`` a late emit would otherwise
    stand up a fresh ``LoggerProvider`` + ``BatchLogRecordProcessor`` during
    interpreter teardown -- never shut down, its buffer silently dropped, and
    ``Resource.create()`` liable to raise "cannot schedule new futures after
    interpreter shutdown". Only an explicit ``init_otel`` re-arms it.
    """
    if _SHUTDOWN:
        _logger.debug("telemetry pipeline is shut down -- refusing lazy re-init")
        return False
    if _INIT_ARGS is not None:
        return init_otel(**_INIT_ARGS)
    from lemoncrow.core.service.telemetry.config import otel_endpoint

    return init_otel(endpoint=otel_endpoint())


def _sdk_log_record_cls() -> type[Any] | None:
    """SDK-side ``LogRecord`` class when the caller must build one, else None.

    ``Logger.emit`` only gained API->SDK record conversion in
    **opentelemetry-sdk 1.37.0** (``LogRecord._from_api_log_record``). On
    <= 1.36 it wraps the API record verbatim in ``LogData``, and the OTLP
    encoder then dies with ``AttributeError: 'LogRecord' object has no
    attribute 'resource'`` inside the batch-export thread -- telemetry is
    silently lost.

    The dependency floor cannot simply be raised to 1.37: the ``memory-server``
    extra pulls letta, which hard-pins ``opentelemetry-sdk==1.30.0``, so that
    extra would stop resolving. Feature-detect and build the SDK record here
    instead.

    Returns None on >= 1.37 (the SDK converts) and on >= 1.43 (the class was
    renamed to ``ReadWriteLogRecord``, so the import fails).
    """
    try:
        # NB: `from <mod> import <name>` (not `import <mod> as x`) so the
        # lookup goes through sys.modules -- that is what makes the version
        # matrix testable by swapping the module.
        from opentelemetry.sdk._logs import (  # type: ignore[attr-defined]
            LogRecord as SdkLogRecord,
        )
    except ImportError:
        return None
    if hasattr(SdkLogRecord, "_from_api_log_record"):
        return None
    sdk_record_cls: type[Any] = SdkLogRecord
    return sdk_record_cls


def emit_product_log(event_name: str, props: dict[str, Any]) -> bool:
    if logger is None and not _reinit():
        return False
    if logger is None:
        return False
    try:
        from opentelemetry._logs import LogRecord
        from opentelemetry._logs.severity import SeverityNumber
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
        fields: dict[str, Any] = {
            "body": event_name,
            "attributes": flat_attrs,
            "span_id": span_context.span_id if span_context else 1,
            "trace_id": span_context.trace_id if span_context else 1,
            "trace_flags": span_context.trace_flags if span_context else TraceFlags(0),
            "severity_text": "DEBUG",
            "severity_number": SeverityNumber.DEBUG,
        }
        provider = _PROVIDER
        sdk_record_cls = _sdk_log_record_cls()
        record: Any
        if sdk_record_cls is None:
            record = LogRecord(**fields)
        else:
            # opentelemetry-sdk <= 1.36: no API->SDK conversion, so the record
            # must carry the resource itself or the OTLP encoder blows up.
            record = sdk_record_cls(resource=getattr(provider, "resource", None) or _RESOURCE, **fields)
        provider.get_logger("lemoncrow.product.telemetry.otel").emit(record)
        return True
    except Exception:
        _logger.debug("telemetry.emit_product_log_failed", exc_info=True)
        return False


def shutdown_otel() -> None:
    global logger, _PROVIDER, _RESOURCE, _SHUTDOWN, _last_check_failed_at
    # Drain any events still sitting in the async telemetry queue *before*
    # tearing down state below. Callers enqueue their final events (e.g.
    # "session_end") via the non-blocking emit_product() and immediately hit
    # this function in a `finally` block. Without this flush, the background
    # worker thread can race this teardown: it observes `logger is None`
    # (just cleared here) and calls init_otel() from scratch -- including
    # Resource.create()'s internal thread pool -- right as the interpreter is
    # exiting, raising "cannot schedule new futures after interpreter
    # shutdown". See GH #40.
    with contextlib.suppress(Exception):
        from lemoncrow.core.service.telemetry.emit import flush_product_telemetry

        flush_product_telemetry()
    provider = _PROVIDER
    # Fence off the lazy path *before* clearing `logger`: _reinit only runs
    # once `logger is None`, so raising the fence first leaves no window in
    # which a racing worker thread could rebuild the pipeline. After this
    # point emit_product_log must NOT resurrect it (see _SHUTDOWN / _reinit).
    _SHUTDOWN = True
    logger = None
    _PROVIDER = None
    _RESOURCE = None
    # _INIT_ARGS is kept only so an *explicit* init_otel re-arm still targets
    # the configured endpoint + auth headers rather than the localhost default.
    _last_check_failed_at = None  # clear negative cache
    if provider is not None:
        with contextlib.suppress(Exception):
            provider.shutdown()


def _logs_endpoint(endpoint: str) -> str:
    cleaned = endpoint.rstrip("/")
    if cleaned.endswith("/v1/logs"):
        return cleaned
    return f"{cleaned}/v1/logs"
