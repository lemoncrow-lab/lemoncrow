"""Public SDK import path.

This module re-exports the gateway SDK so callers can use
``from atelier.sdk import AtelierClient``.
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

__all__ = [
    "AtelierClient",
    "ContextResult",
    "FailureAnalysisResult",
    "LessonDecisionResult",
    "LessonInboxResult",
    "LocalClient",
    "MCPClient",
    "RemoteClient",
    "SavingsSummary",
]
