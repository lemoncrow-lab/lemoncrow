"""Job definitions for the LemonCrow worker system (P6).

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
JOB_EXTRACT_PLAYBOOK = "extract_playbook_from_trace"
JOB_COMPUTE_EMBEDDINGS = "compute_embeddings"
JOB_CONSOLIDATE_BLOCKS = "consolidate_playbooks"
JOB_OPTIMIZE = "optimize_runtime"
JOB_RETENTION_CLEANUP = "retention_cleanup"
JOB_BOOTSTRAP_CONTEXT = "bootstrap_context"
JOB_INGEST_SESSION_FILE = "ingest_session_file"
JOB_INGEST_SESSION_DIRECTORY = "ingest_session_directory"

KNOWN_JOB_TYPES: frozenset[str] = frozenset(
    {
        JOB_EXTRACT_PLAYBOOK,
        JOB_COMPUTE_EMBEDDINGS,
        JOB_CONSOLIDATE_BLOCKS,
        JOB_OPTIMIZE,
        JOB_RETENTION_CLEANUP,
        JOB_BOOTSTRAP_CONTEXT,
        JOB_INGEST_SESSION_FILE,
        JOB_INGEST_SESSION_DIRECTORY,
    }
)


__all__ = [
    "JOB_BOOTSTRAP_CONTEXT",
    "JOB_COMPUTE_EMBEDDINGS",
    "JOB_CONSOLIDATE_BLOCKS",
    "JOB_EXTRACT_PLAYBOOK",
    "JOB_INGEST_SESSION_DIRECTORY",
    "JOB_INGEST_SESSION_FILE",
    "JOB_OPTIMIZE",
    "JOB_RETENTION_CLEANUP",
    "KNOWN_JOB_TYPES",
    "Job",
]
