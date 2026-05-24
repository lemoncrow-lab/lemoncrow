"""Public SDK import path.

This module re-exports the gateway SDK so callers can use
``from atelier.sdk import AtelierClient``.
It also exposes the drop-in middleware layer for external agent frameworks.

Usage (middleware)::

    from atelier.sdk import AtelierMiddleware

    mw = AtelierMiddleware(agent_name="bugfixer", task="Refactor the auth module")
    # LangChain: callbacks=[mw.langchain()]
    # OpenAI Agents SDK: hooks=mw.openai_hooks()
    # Raw Anthropic: tool_specs, dispatch = mw.anthropic_tools()
"""

from __future__ import annotations

from atelier.gateway.sdk import (
    AtelierClient,
    ContextResult,
    FailureAnalysisResult,
    LessonDecisionResult,
    LessonInboxResult,
    LocalClient,
    MCPClient,
    RemoteClient,
    SavingsSummary,
)
from atelier.sdk.middleware import AtelierMiddleware

__all__ = [
    "AtelierClient",
    "AtelierMiddleware",
    "ContextResult",
    "FailureAnalysisResult",
    "LessonDecisionResult",
    "LessonInboxResult",
    "LocalClient",
    "MCPClient",
    "RemoteClient",
    "SavingsSummary",
]

