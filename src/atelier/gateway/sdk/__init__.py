"""Python SDK and client interfaces."""

from __future__ import annotations

from atelier.gateway.sdk.client import (
    AtelierClient,
    ContextResult,
    FailureAnalysisResult,
    LessonDecisionResult,
    LessonInboxResult,
    MemoryArchiveResult,
    MemoryRecallResult,
    MemoryUpsertBlockResult,
    ReasoningContextRecalledPassage,
    ReasoningContextTokenBreakdown,
    SavingsSummary,
)
from atelier.gateway.sdk.local import LocalClient
from atelier.gateway.sdk.mcp import MCPClient
from atelier.gateway.sdk.remote import RemoteClient

__all__ = [
    "AtelierClient",
    "ContextResult",
    "FailureAnalysisResult",
    "LessonDecisionResult",
    "LessonInboxResult",
    "LocalClient",
    "MCPClient",
    "MemoryArchiveResult",
    "MemoryRecallResult",
    "MemoryUpsertBlockResult",
    "ReasoningContextRecalledPassage",
    "ReasoningContextTokenBreakdown",
    "RemoteClient",
    "SavingsSummary",
]
