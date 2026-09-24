"""Savings/statusline MCP handler with late-bound runtime locations."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StatuslineHandlerHooks:
    lemoncrow_root: Callable[[], Path]
    session_sidecar: Callable[[], Path]


_HooksFactory = Callable[[], StatuslineHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_statusline_handler_hooks(factory: _HooksFactory) -> None:
    """Install the process composition used by the savings/statusline handler."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> StatuslineHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("statusline handler hooks are not configured")
    return factory()


@mcp_tool(name="statusline_segment")
def tool_statusline_segment(format: str = "segment") -> str:
    """Savings surface for the active session."""
    hooks = _hooks()
    fmt = (format or "segment").strip().lower()
    if fmt in {"markdown", "md", "json"}:
        from lemoncrow.core.capabilities.plugin_runtime import build_savings_report
        from lemoncrow.core.capabilities.savings_summary import render_savings_markdown

        payload = build_savings_report(hooks.lemoncrow_root())
        if fmt == "json":
            return json.dumps(payload, indent=2, sort_keys=True, default=str)
        return render_savings_markdown(payload)
    try:
        sidecar = hooks.session_sidecar()
        seg_path = sidecar.parent / "statusline_segment"
        sid = sidecar.parent.name
        from lemoncrow.core.capabilities.savings_summary import savings_segment

        seg = savings_segment(session_id=sid)
        if seg:
            seg_path.write_text(seg, encoding="utf-8")
            return seg
        if seg_path.exists():
            return seg_path.read_text(encoding="utf-8").strip()
    except Exception:
        _log.debug("tool_statusline_segment failed", exc_info=True)
    return ""


__all__ = ["StatuslineHandlerHooks", "configure_statusline_handler_hooks", "tool_statusline_segment"]
