"""Data models for the Atelier swarm harness."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


SwarmRunStatus = Literal["pending", "running", "success", "failed", "stopped"]
SwarmChildStatus = Literal["pending", "running", "success", "failed", "stopped"]
SwarmRunMode = Literal["single", "continuous"]
SwarmWaveStatus = Literal["running", "applied", "no-improvement", "stopped"]
SwarmPlanningMode = Literal["adaptive", "bounded", "open-ended"]


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


class SwarmArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = ""
    kind: str
    label: str
    path: str
    relative_path: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    exists: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SwarmAcceptedCommit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int
    child_id: str
    commit_ref: str
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    patch_path: str = ""
    score: float | None = None
    accepted_at: datetime = Field(default_factory=utcnow)
    artifacts: list[SwarmArtifactRef] = Field(default_factory=list)
    apply_commands: list[str] = Field(default_factory=list)


class SwarmWaveState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wave_index: int
    status: SwarmWaveStatus = "running"
    max_runs: int = 0
    planned_runs: int = 0
    planning_mode: SwarmPlanningMode = "adaptive"
    planning_reason: str = ""
    child_ids: list[str] = Field(default_factory=list)
    accepted_child_ids: list[str] = Field(default_factory=list)
    rejected_child_ids: list[str] = Field(default_factory=list)
    primary_winner_child_id: str | None = None
    accepted_commits: list[SwarmAcceptedCommit] = Field(default_factory=list)
    rejected_child_notes: dict[str, str] = Field(default_factory=dict)
    manifest_artifact: SwarmArtifactRef | None = None
    summary: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        child_ids = payload.get("child_ids") or []
        accepted_child_ids = payload.get("accepted_child_ids") or []
        payload.setdefault("max_runs", _coerce_int(payload.get("planned_runs"), len(child_ids)))
        if payload["max_runs"] <= 0:
            payload["max_runs"] = len(child_ids)
        payload.setdefault("planned_runs", len(child_ids) or _coerce_int(payload.get("max_runs"), 0))
        if not payload.get("primary_winner_child_id"):
            payload["primary_winner_child_id"] = payload.get("winner_child_id") or (
                accepted_child_ids[0] if accepted_child_ids else None
            )
        payload.setdefault("accepted_commits", [])
        payload.setdefault("rejected_child_notes", {})
        payload.setdefault("planning_reason", "")
        payload.setdefault("planning_mode", "adaptive")
        return payload


class SwarmChildState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child_id: str
    label: str
    wave_index: int = 1
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
    patch_path: str = ""
    files_changed: list[str] = Field(default_factory=list)
    validation_results: list[SwarmValidationCheck] = Field(default_factory=list)
    summary: str = ""
    error: str = ""
    accepted: bool = False
    acceptance_note: str = ""
    accepted_commit_ref: str = ""
    accepted_order: int | None = None
    export_artifacts: list[SwarmArtifactRef] = Field(default_factory=list)
    apply_commands: list[str] = Field(default_factory=list)
    stdout_preview: str = ""
    stderr_preview: str = ""
    current_activity: str = ""
    last_output_at: datetime | None = None
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
    mode: SwarmRunMode = "single"
    repo_root: str
    base_worktree: str
    base_ref: str
    base_snapshot_ref: str = ""
    worktree_pool: str
    integration_worktree: str = ""
    integration_base_ref: str = ""
    artifact_root: str = ""
    base_snapshot_artifact: SwarmArtifactRef | None = None
    export_artifacts: list[SwarmArtifactRef] = Field(default_factory=list)
    accepted_commits: list[SwarmAcceptedCommit] = Field(default_factory=list)
    transplant_commands: list[str] = Field(default_factory=list)
    spec_source_path: str
    copied_spec_path: str
    runner_name: str = "custom"
    runner_model: str = ""
    child_command: list[str]
    validation_commands: list[str] = Field(default_factory=list)
    runs: int = 0
    max_runs: int = 0
    planning_mode: SwarmPlanningMode = "adaptive"
    fan_out_reason: str = ""
    current_wave: int = 0
    stop_requested: bool = False
    stop_reason: str = ""
    keep_worktrees: bool = True
    detached: bool = False
    coordinator_pid: int | None = None
    coordinator_log_path: str = ""
    winner_child_id: str | None = None
    primary_winner_child_id: str | None = None
    accepted_child_ids: list[str] = Field(default_factory=list)
    ranking_notes: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    dirty_paths: list[str] = Field(default_factory=list)
    waves: list[SwarmWaveState] = Field(default_factory=list)
    children: list[SwarmChildState] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        legacy_runs = _coerce_int(payload.get("runs"), 0)
        payload.setdefault("max_runs", legacy_runs)
        if _coerce_int(payload.get("max_runs"), 0) <= 0:
            payload["max_runs"] = legacy_runs
        payload.setdefault("runs", _coerce_int(payload.get("max_runs"), legacy_runs))
        payload.setdefault("artifact_root", "")
        payload.setdefault("export_artifacts", [])
        payload.setdefault("accepted_commits", [])
        payload.setdefault("transplant_commands", [])
        payload.setdefault("fan_out_reason", "")
        payload.setdefault("planning_mode", "adaptive")
        payload.setdefault("base_snapshot_ref", payload.get("integration_base_ref") or payload.get("base_ref") or "")
        if not payload.get("primary_winner_child_id"):
            payload["primary_winner_child_id"] = payload.get("winner_child_id")
        if not payload.get("winner_child_id"):
            payload["winner_child_id"] = payload.get("primary_winner_child_id")
        payload.setdefault("dirty_paths", [])
        return payload
