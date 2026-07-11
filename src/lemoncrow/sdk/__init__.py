"""Public SDK import path.

This module re-exports the gateway SDK so callers can use
``from lemoncrow.sdk import LemonCrowClient``.
It also exposes the drop-in middleware layer for external agent frameworks.

Usage (middleware)::

    from lemoncrow.sdk import LemonCrowMiddleware

    mw = LemonCrowMiddleware(agent_name="bugfixer", task="Refactor the auth module")
    # LangChain: callbacks=[mw.langchain()]
    # OpenAI Agents SDK: hooks=mw.openai_hooks()
    # Raw Anthropic: tool_specs, dispatch = mw.anthropic_tools()
    # Gemini ADK-style hooks: gemini_hooks = mw.gemini_adk()
"""

from __future__ import annotations

from lemoncrow.gateway.sdk import (
    ContextResult,
    LemonCrowClient,
    LessonDecisionResult,
    LessonInboxResult,
    LocalClient,
    MCPClient,
    RemoteClient,
    SavingsSummary,
)
from lemoncrow.sdk.gemini_adk import GeminiADKMiddleware
from lemoncrow.sdk.middleware import LemonCrowMiddleware

__all__ = [
    "ContextResult",
    "GeminiADKMiddleware",
    "LemonCrowClient",
    "LemonCrowMiddleware",
    "LessonDecisionResult",
    "LessonInboxResult",
    "LocalClient",
    "MCPClient",
    "RemoteClient",
    "SavingsSummary",
]
