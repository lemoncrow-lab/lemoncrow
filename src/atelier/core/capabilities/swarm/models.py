"""Data models for the Atelier swarm harness."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


SwarmRunStatus = Literal["pending", "running", "success", "failed", "stopped"]
SwarmChildStatus = Literal["pending", "running", "success", "failed", "stopped"]


class SwarmValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    passed: bool
    exit_code: int
    detail: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    duration_seconds: float = 0.0


class SwarmChildState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child_id: str
    label: str
    status: SwarmChildStatus = "pending"
    pid: int | None = None
    exit_code: int | None = None
    worktree_path: str
    atelier_root: str
    run_dir: str
    spec_path: str
    result_path: str
    stdout_path: str
    stderr_path: str
    metadata_path: str
    files_changed: list[str] = Field(default_factory=list)
    validation_results: list[SwarmValidationCheck] = Field(default_factory=list)
    summary: str = ""
    error: str = ""
    token_count: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    score: float | None = None
    score_breakdown: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SwarmRunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: SwarmRunStatus = "pending"
    repo_root: str
    base_worktree: str
    base_ref: str
    worktree_pool: str
    spec_source_path: str
    copied_spec_path: str
    child_command: list[str]
    validation_commands: list[str] = Field(default_factory=list)
    runs: int
    keep_worktrees: bool = True
    detached: bool = False
    coordinator_pid: int | None = None
    winner_child_id: str | None = None
    ranking_notes: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    dirty_paths: list[str] = Field(default_factory=list)
    children: list[SwarmChildState] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
