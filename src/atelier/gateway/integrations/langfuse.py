"""Optional Langfuse integration for Atelier trace observability.

Opt-in via environment variables:
  ATELIER_LANGFUSE_ENABLED=true
  LANGFUSE_PUBLIC_KEY=pk-lf-...
  LANGFUSE_SECRET_KEY=sk-lf-...
  LANGFUSE_HOST=https://cloud.langfuse.com   # optional, defaults to cloud

Install the SDK with the optional extra:  uv sync --extra langfuse
(or  pip install 'atelier[langfuse]').

Fail-open design: any Langfuse error is silently swallowed so the core
agent loop is never interrupted by an observability outage.

Targets the Langfuse v3+ SDK (OpenTelemetry-based). The removed v2
``client.trace()`` / ``trace.span()`` API is no longer used.

Usage:
    from atelier.gateway.integrations.langfuse import emit_trace, emit_tool_call
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Process-wide singleton. Building a client per call (the old behaviour) both
# wasted work and, with a per-call flush(), forced a synchronous network export
# on every tool call's critical path. The v3+ client batches on a background
# thread and flushes at exit, so we build once and reuse, then flush on shutdown.
_CLIENT: Any = None


def _enabled() -> bool:
    return os.environ.get("ATELIER_LANGFUSE_ENABLED", "").lower() in ("1", "true", "yes")


def _client() -> Any:
    """Return a cached Langfuse client, or None if unavailable/unconfigured."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not public_key or not secret_key:
        return None
    try:
        from langfuse import Langfuse

        host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        _CLIENT = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        return _CLIENT
    except Exception:  # noqa: BLE001
        logger.warning("langfuse client init failed", exc_info=True)
        return None


# Substrings (case-insensitive) marking a key whose value carries a credential
# or other secret. Matched anywhere in the key so variants like
# ``LANGFUSE_SECRET_KEY``, ``db_password``, ``auth_token`` and ``apiKey`` are
# all caught. Values under these keys are redacted regardless of length.
_SECRET_KEY_SUBSTRINGS: tuple[str, ...] = (
    "connection_string",
    "dsn",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
)

_REDACTED = "<redacted>"


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(needle in lowered for needle in _SECRET_KEY_SUBSTRINGS)


def _scrub(value: Any) -> Any:
    """Redact secret-bearing values and truncate long strings, recursively.

    Recurses through dicts and lists at every nesting level so credentials
    buried in nested telemetry args never reach the observability backend.
    Values under secret-bearing keys (``connection_string``, ``dsn``,
    ``api_key``, ``token``, ``secret``, ``password``, ``authorization``,
    case-insensitive) are replaced with ``<redacted>`` regardless of length.
    Non-secret string values longer than 300 chars are replaced with a
    ``<N chars>`` placeholder to keep large file contents / patches out of
    the backend. All other telemetry is left intact.
    """
    if isinstance(value, dict):
        return {k: (_REDACTED if _is_secret_key(k) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str) and len(value) > 300:
        return f"<{len(value)} chars>"
    return value


def emit_trace(payload: dict[str, Any]) -> None:
    """Record a completed agent trace as one Langfuse event. No-ops on any error.

    Args:
        payload: The dict passed to Trace.model_validate() in record_trace.
                 Expected keys: agent, domain, task, status, session_id,
                 files_touched, tools_called, commands_run, errors_seen,
                 diff_summary, output_summary, validation_results, id.
    """
    if not _enabled():
        return
    try:
        client = _client()
        if client is None:
            return

        status = str(payload.get("status", "unknown"))
        domain = str(payload.get("domain", "unknown"))
        agent = str(payload.get("agent", "unknown"))
        session_id = str(payload.get("session_id", "")) or None

        from langfuse import propagate_attributes

        with propagate_attributes(
            session_id=session_id,
            tags=[status, domain, agent],
            trace_name=f"atelier.{domain}",
        ):
            client.create_event(
                name=f"atelier.{domain}",
                input={"task": payload.get("task", "")},
                output=_scrub(
                    {
                        "status": status,
                        "output_summary": payload.get("output_summary", ""),
                        "diff_summary": payload.get("diff_summary", ""),
                    }
                ),
                metadata=_scrub(
                    {
                        "agent": agent,
                        "session_id": session_id or "",
                        "files_touched": payload.get("files_touched", []),
                        "tools_called": payload.get("tools_called", []),
                        "commands_run": payload.get("commands_run", []),
                        "errors_seen": payload.get("errors_seen", []),
                        "validation_results": payload.get("validation_results", []),
                    }
                ),
            )
    except Exception:  # noqa: BLE001
        logger.warning("Suppressed exception in emit_trace", exc_info=True)


def emit_tool_call(
    *,
    tool: str,
    args: dict[str, Any],
    duration_ms: int,
    response_size: int,
    status: str,
    error: str | None = None,
    session_id: str = "",
) -> None:
    """Emit one Langfuse event per MCP tool invocation. No-ops on any error.

    Enabled only when ATELIER_LANGFUSE_ENABLED=true and keys are configured.
    String arg values longer than 300 chars are scrubbed to ``<N chars>``.
    """
    if not _enabled():
        return
    try:
        client = _client()
        if client is None:
            return

        from langfuse import propagate_attributes

        with propagate_attributes(session_id=session_id or None, tags=["mcp_tool", tool, status]):
            client.create_event(
                name=f"mcp.{tool}",
                input=_scrub(args),
                output=(
                    {"status": status, "response_size_bytes": response_size}
                    if not error
                    else {"status": status, "error": error}
                ),
                metadata={
                    "duration_ms": duration_ms,
                    "response_size_bytes": response_size,
                    "session_id": session_id,
                    "tool": tool,
                },
                level="ERROR" if error else "DEFAULT",
                status_message=error or None,
            )
    except Exception:  # noqa: BLE001
        logger.warning("Suppressed exception in emit_tool_call", exc_info=True)


def shutdown() -> None:
    """Flush and shut down the Langfuse client at process exit. No-ops if unused."""
    global _CLIENT
    client = _CLIENT
    if client is None:
        return
    try:
        client.flush()
        client.shutdown()
    except Exception:  # noqa: BLE001
        logger.warning("langfuse shutdown failed", exc_info=True)
    finally:
        _CLIENT = None


def health_check() -> dict[str, Any]:
    """Return Langfuse integration status for diagnostics."""
    enabled = _enabled()
    if not enabled:
        return {"enabled": False, "reason": "ATELIER_LANGFUSE_ENABLED not set"}
    has_pub = bool(os.environ.get("LANGFUSE_PUBLIC_KEY", ""))
    has_sec = bool(os.environ.get("LANGFUSE_SECRET_KEY", ""))
    if not has_pub or not has_sec:
        return {
            "enabled": True,
            "configured": False,
            "reason": "missing LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY",
        }
    try:
        import langfuse  # noqa: F401

        return {
            "enabled": True,
            "configured": True,
            "host": os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        }
    except ImportError:
        return {
            "enabled": True,
            "configured": False,
            "reason": "langfuse SDK not installed — run: uv sync --extra langfuse",
        }
