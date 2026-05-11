"""Optional Langfuse integration for Atelier trace observability.

Opt-in via environment variables:
  ATELIER_LANGFUSE_ENABLED=true
  LANGFUSE_PUBLIC_KEY=pk-lf-...
  LANGFUSE_SECRET_KEY=sk-lf-...
  LANGFUSE_HOST=https://cloud.langfuse.com   # optional, defaults to cloud

Fail-open design: any Langfuse error is silently swallowed so the core
agent loop is never interrupted by an observability outage.

Usage (called automatically by atelier_record_trace):
    from atelier.gateway.integrations.langfuse import emit_trace
    emit_trace(trace_payload_dict)
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("ATELIER_LANGFUSE_ENABLED", "").lower() in ("1", "true", "yes")


def _make_client() -> Any:
    """Return a Langfuse client or None if the SDK is not installed / not configured."""
    try:
        from langfuse import Langfuse

        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
        host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        if not public_key or not secret_key:
            return None
        return Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return None


def emit_trace(payload: dict[str, Any]) -> None:
    """Send a completed agent trace to Langfuse. Silently no-ops on any error.

    Args:
        payload: The dict that was passed to Trace.model_validate() in record_trace.
                 Expected keys: agent, domain, task, status, session_id, files_touched,
                 tools_called, commands_run, errors_seen, diff_summary, output_summary,
                 validation_results, id.
    """
    if not _enabled():
        return
    try:
        client = _make_client()
        if client is None:
            return

        status = payload.get("status", "unknown")
        domain = payload.get("domain", "unknown")
        agent = payload.get("agent", "unknown")
        session_id = str(payload.get("session_id", ""))
        trace_id = str(payload.get("id", ""))

        client.trace(
            id=trace_id or None,
            name=f"atelier.{domain}",
            input={"task": payload.get("task", "")},
            output={
                "status": status,
                "output_summary": payload.get("output_summary", ""),
                "diff_summary": payload.get("diff_summary", ""),
            },
            metadata={
                "agent": agent,
                "session_id": session_id,
                "files_touched": payload.get("files_touched", []),
                "tools_called": payload.get("tools_called", []),
                "commands_run": payload.get("commands_run", []),
                "errors_seen": payload.get("errors_seen", []),
                "validation_results": payload.get("validation_results", []),
            },
            tags=[status, domain, agent],
            session_id=session_id or None,
        )
        client.flush()
    except Exception:
        logger.warning(
            "Suppressed exception at langfuse.py:86",
            exc_info=True,
        )


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
            "reason": "langfuse package not installed — run: uv add langfuse",
        }
