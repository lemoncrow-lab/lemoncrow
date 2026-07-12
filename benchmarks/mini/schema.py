"""Pydantic models for the LemonCrow mini eval suite.

Defines the case definition (:class:`MiniEvalCase`), the per-case result
(:class:`MiniEvalCaseResult`), and the aggregate report (:class:`MiniEvalReport`)
used by the deterministic, cheap mini benchmark that backs ``lc benchmark mini``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MiniEvalCase(BaseModel):
    """One mini eval task: a prompt plus a deterministic verification command."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable case identifier.")
    title: str = Field(description="Short human-readable case title.")
    prompt: str = Field(description="Instruction handed to the agent.")
    starting_git_sha: str = Field(
        description="Git ref to reset to before running. 'HEAD' means do not reset.",
    )
    allowed_files: list[str] = Field(
        default_factory=list,
        description="Glob patterns the agent is allowed to change. Empty means no files may change.",
    )
    command_to_verify: str = Field(
        description="Shell command that passes when it exits 0.",
    )
    expected_success_condition: str = Field(
        description="Human description of what success looks like.",
    )
    max_cost_usd: float = Field(
        default=0.05,
        description="Soft cost ceiling for this case in USD.",
    )
    tags: list[str] = Field(default_factory=list, description="Free-form labels.")


class MiniEvalCaseResult(BaseModel):
    """Outcome of running a single :class:`MiniEvalCase`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: Literal["accepted", "failed", "skipped", "error"]
    trace_id: str | None = None
    selected_route: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    estimated_cost_usd: float = 0.0
    patch_created: bool = False
    tests_passed: bool = False
    accepted: bool = False
    regression: bool = False
    file_boundary_respected: bool = True
    notes: str = ""


class MiniEvalReport(BaseModel):
    """Aggregate report across all run mini eval cases."""

    model_config = ConfigDict(extra="forbid")

    suite: str = "mini"
    status: Literal["pass", "fail", "dry_run"]
    started_at: str
    finished_at: str
    total_tasks: int
    accepted_tasks: int
    failed_tasks: int
    accepted_patch_rate: float
    total_cost_usd: float
    cost_per_accepted_patch: float
    cheap_success_rate: float
    routing_regression_rate: float
    context_reduction_pct: float | None = None
    trace_coverage_pct: float
    cases: list[MiniEvalCaseResult] = Field(default_factory=list)


__all__ = [
    "MiniEvalCase",
    "MiniEvalCaseResult",
    "MiniEvalReport",
]
