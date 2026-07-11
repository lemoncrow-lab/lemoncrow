"""Python SDK and client interfaces."""

from __future__ import annotations

from lemoncrow.gateway.sdk.client import (
    ContextResult,
    LemonCrowClient,
    LessonDecisionResult,
    LessonInboxResult,
    MemoryArchiveResult,
    MemoryRecallResult,
    MemoryUpsertBlockResult,
    ReasoningContextRecalledPassage,
    ReasoningContextTokenBreakdown,
    SavingsSummary,
)
from lemoncrow.gateway.sdk.local import LocalClient
from lemoncrow.gateway.sdk.mcp import MCPClient
from lemoncrow.gateway.sdk.remote import RemoteClient

__all__ = [
    "ContextResult",
    "LemonCrowClient",
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
