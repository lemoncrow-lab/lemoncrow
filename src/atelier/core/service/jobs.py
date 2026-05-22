"""Job definitions for the Atelier worker system (P6).

Job status lifecycle:
    pending → running → succeeded | failed → dead (after max_attempts exhausted)

All job types are strings so they can be stored in the DB and extended without
schema changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Job(BaseModel):
    """In-memory representation of a queued job."""

    model_config = ConfigDict(extra="forbid")

    id: str
    job_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 3
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# Supported job type constants.
JOB_EXTRACT_REASONBLOCK = "extract_reasonblock_from_trace"
JOB_ANALYZE_FAILURES = "analyze_failures"
JOB_GENERATE_EVAL = "generate_eval_from_failure_cluster"
JOB_COMPUTE_EMBEDDINGS = "compute_embeddings"
JOB_CONSOLIDATE_BLOCKS = "consolidate_reasonblocks"
JOB_RETENTION_CLEANUP = "retention_cleanup"
JOB_BOOTSTRAP_CONTEXT = "bootstrap_context"

KNOWN_JOB_TYPES: frozenset[str] = frozenset(
    {
        JOB_EXTRACT_REASONBLOCK,
        JOB_ANALYZE_FAILURES,
        JOB_GENERATE_EVAL,
        JOB_COMPUTE_EMBEDDINGS,
        JOB_CONSOLIDATE_BLOCKS,
        JOB_RETENTION_CLEANUP,
        JOB_BOOTSTRAP_CONTEXT,
    }
)


__all__ = [
    "JOB_ANALYZE_FAILURES",
    "JOB_COMPUTE_EMBEDDINGS",
    "JOB_CONSOLIDATE_BLOCKS",
    "JOB_BOOTSTRAP_CONTEXT",
    "JOB_EXTRACT_REASONBLOCK",
    "JOB_GENERATE_EVAL",
    "JOB_RETENTION_CLEANUP",
    "KNOWN_JOB_TYPES",
    "Job",
]
