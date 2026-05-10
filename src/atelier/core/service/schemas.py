"""Pydantic request/response schemas for the service API.

Kept separate from core models to allow the API to evolve independently.
All response models use ``extra="forbid"`` for forward-compat serialization.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---- shared ----------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- /v1/reasoning/context -------------------------------------------------


class ReasoningContextRequest(_Strict):
    task: str
    domain: str | None = None
    files: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    max_blocks: int = 5
    token_budget: int | None = 2000
    dedup: bool = True
    include_telemetry: bool = False
    agent_id: str | None = None
    recall: bool = True


class ReasoningContextResponse(_Strict):
    context: str
    tokens_used: int | None = None
    tokens_saved_vs_naive: int | None = None
    recalled_passages: list[dict[str, str | float]] = Field(default_factory=list)
    tokens_breakdown: dict[str, int] | None = None


# ---- /v1/reasoning/check-plan ----------------------------------------------


class CheckPlanRequest(_Strict):
    task: str
    plan: list[str]
    domain: str | None = None
    files: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# ---- /v1/reasoning/rescue --------------------------------------------------


class RescueRequest(_Strict):
    task: str
    error: str
    domain: str | None = None
    files: list[str] = Field(default_factory=list)
    recent_actions: list[str] = Field(default_factory=list)


# ---- /v1/rubrics/run -------------------------------------------------------


class RunRubricRequest(_Strict):
    rubric_id: str
    checks: dict[str, bool | None]


# ---- /v1/traces ------------------------------------------------------------


class RecordTraceRequest(_Strict):
    agent: str
    domain: str
    task: str
    status: str
    files_touched: list[str | dict[str, Any]] = Field(default_factory=list)
    commands_run: list[str | dict[str, Any]] = Field(default_factory=list)
    tools_called: list[dict[str, Any]] = Field(default_factory=list)
    errors_seen: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    output_summary: str = ""
    validation_results: list[dict[str, Any]] = Field(default_factory=list)


class TraceEventRequest(_Strict):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class FinishTraceRequest(_Strict):
    status: str
    diff_summary: str = ""
    output_summary: str = ""


class RecordTraceResponse(_Strict):
    id: str


# ---- /v1/extract/reasonblock -----------------------------------------------


class ExtractReasonBlockRequest(_Strict):
    trace_id: str
    save: bool = False


# ---- /v1/failures/analyze --------------------------------------------------


class AnalyzeFailuresRequest(_Strict):
    domain: str | None = None
    limit: int = 100


# ---- /v1/reasonblocks ------------------------------------------------------


class UpsertBlockRequest(_Strict):
    id: str
    title: str
    domain: str
    situation: str
    triggers: list[str] = Field(default_factory=list)
    procedure: list[str] = Field(default_factory=list)
    dead_ends: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    failure_signals: list[str] = Field(default_factory=list)


class PatchBlockRequest(_Strict):
    status: str | None = None
    title: str | None = None


# ---- /v1/rubrics -----------------------------------------------------------


class UpsertRubricRequest(_Strict):
    id: str
    domain: str
    required_checks: list[str] = Field(default_factory=list)
    block_if_missing: list[str] = Field(default_factory=list)
    warning_checks: list[str] = Field(default_factory=list)
    escalation_conditions: list[str] = Field(default_factory=list)


# ---- /v1/evals -------------------------------------------------------------


class RunEvalsRequest(_Strict):
    domain: str | None = None
    limit: int = 50


# ---- health ----------------------------------------------------------------


class HealthResponse(_Strict):
    status: str
    version: str = "0.1.0"


class ReadyResponse(_Strict):
    status: str
    storage: dict[str, object]


# ---- /api/hosts -------------------------------------------------------


class HostRegisterRequest(_Strict):
    """Request to register a new host."""

    atelier_version: str


class HostRegisterResponse(_Strict):
    """Response from host registration."""

    host_id: str
    fingerprint: dict[str, Any]
    registered_at: str
    atelier_version: str


class HostListItemResponse(_Strict):
    """Single host in list response."""

    host_id: str
    label: str
    status: str
    active_domains: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    last_seen: str | None = None
    atelier_version: str | None = None
    description: str | None = None
    install_command: str | None = None


class HostDetailResponse(_Strict):
    """Detailed host information."""

    host_id: str
    label: str
    description: str | None = None
    fingerprint: dict[str, Any]
    status: str
    active_domains: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    last_seen: str | None = None
    registered_at: str | None = None
    atelier_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HostStatusRequest(_Strict):
    """Request to update host status."""

    active_domains: list[str] = Field(default_factory=list)
    available_mcp_tools: list[str] = Field(default_factory=list)


class HostStatusResponse(_Strict):
    """Current host status."""

    host_id: str
    last_seen: str
    active_domains: list[str] = Field(default_factory=list)
    available_mcp_tools: list[str] = Field(default_factory=list)
    atelier_version: str | None = None


# ---- /api/benchmarks -------------------------------------------------------


class BenchmarkRequest(_Strict):
    """Request to run a benchmark."""

    bundle_id: str
    iterations: int = 3


class BenchmarkMetricsResponse(_Strict):
    """Benchmark metrics."""

    success_rate: float
    tokens_used: int
    cost: float
    time_elapsed: float
    tokens_per_task: float = 0.0
    cost_per_task: float = 0.0
    tasks_per_second: float = 0.0
    task_count: int = 0
    failed_task_count: int = 0


class BenchmarkScenarioResponse(_Strict):
    """Single benchmark scenario result."""

    name: str
    description: str = ""
    metrics: BenchmarkMetricsResponse
    errors: list[str] = Field(default_factory=list)
    timestamp: str


class BenchmarkComparisonResponse(_Strict):
    """Comparison between two scenarios."""

    base_scenario: str
    compared_scenario: str
    success_rate_improvement: float = 0.0
    tokens_reduction: float = 0.0
    cost_reduction: float = 0.0
    speed_improvement: float = 0.0


class BenchmarkResponse(_Strict):
    """Benchmark result."""

    id: str
    bundle_id: str
    bundle_version: str = ""
    iterations: int
    scenarios: dict[str, BenchmarkScenarioResponse]
    comparisons: dict[str, BenchmarkComparisonResponse]
    created_at: str
    duration: float
    notes: str = ""


class BenchmarkListResponse(_Strict):
    """List of benchmark results."""

    benchmarks: list[BenchmarkResponse]
    total: int


# ---- /api/integrations/hosts -----------------------------------------------


class HostIntegrationRequest(_Strict):
    """Request to get host integration config."""

    host_id: str


class HostIntegrationResponse(_Strict):
    """Host integration configuration."""

    host_id: str
    name: str
    version: str
    description: str
    detection: dict[str, Any] = Field(default_factory=dict)
    recommended_domains: list[str] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    installation: dict[str, str] = Field(default_factory=dict)
    prompt_templates: list[dict[str, str]] = Field(default_factory=list)


class HostIntegrationListResponse(_Strict):
    """List of available host integrations."""

    hosts: list[HostIntegrationResponse]
    total: int
