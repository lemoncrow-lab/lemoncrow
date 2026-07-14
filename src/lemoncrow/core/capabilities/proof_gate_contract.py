"""Public contract models for the proof gate (cost-quality report).

These pydantic report shapes are the caller-facing API (data contract, not IP).
They live here (open) because pydantic cannot be mypyc-compiled, so the pro
proof-gate logic compiles to native ``.so`` while callers import the same types.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

FeatureBoundaryLabel = str  # "Host-native" | "LemonCrow augmentation" | "Future-only"
TraceConfidenceLevel = str  # "full_live" | "mcp_live" | "wrapper_live" | "imported" | "manual"


class BenchmarkCase(BaseModel):
    """One prompt/patch benchmark case with evidence links."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(description="Stable benchmark case identifier.")
    task_type: str = Field(default="coding", description="Type of task being benchmarked.")
    tier: str = Field(description="Route tier used: cheap | mid | premium | deterministic.")
    accepted: bool = Field(description="Whether the patch was accepted.")
    cost_usd: float = Field(description="Total cost for this case in USD.")
    escalated: bool = Field(default=False, description="Whether routing escalated to a higher tier.")
    regression: bool = Field(default=False, description="Whether this case caused a regression.")
    trace_id: str | None = Field(default=None, description="Trace evidence ID.")
    session_id: str | None = Field(default=None, description="Eval run evidence ID.")
    verifier_outcome: str | None = Field(default=None, description="Verifier outcome: pass | fail | skipped.")
    route_decision_id: str | None = Field(default=None, description="Route decision ID linking to routing evidence.")


class HostEnforcementSnapshot(BaseModel):
    """Per-host enforcement matrix snapshot (from WP-31)."""

    model_config = ConfigDict(extra="forbid")

    host: str
    mode: str
    can_block_start: bool
    can_force_model: bool
    can_require_verification: bool
    fallback_mode: str
    trace_confidence: TraceConfidenceLevel
    provider_enforced_disabled: bool = True


class ProofGateConfig(BaseModel):
    """Configurable thresholds for the proof gate."""

    model_config = ConfigDict(extra="forbid")

    context_reduction_pct_min: float = Field(
        default=50.0, description="Minimum context reduction percentage (WP-19 threshold)."
    )
    premium_only_baseline_cost_per_accepted_patch: float = Field(
        default=1.0,
        description=("Baseline cost per accepted patch if all tasks used premium tier. Routing must beat this."),
    )
    premium_only_baseline_accepted_patch_rate: float = Field(
        default=0.80,
        description=(
            "Baseline accepted-patch rate if all tasks used premium tier. Routing must stay within 0.03 of this."
        ),
    )
    routing_regression_rate_max: float = Field(default=0.02, description="Maximum routing regression rate (2%).")
    min_cheap_success_rate: float = Field(default=0.60, description="Minimum cheap-tier success rate.")


class ProofReport(BaseModel):
    """Final cost-quality proof report (WP-32)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(description="Stable identifier for this proof run.")
    status: str = Field(description="Gate outcome: pass | fail.")
    failed_thresholds: list[str] = Field(
        default_factory=list,
        description="Names of thresholds that failed. Empty when status=pass.",
    )

    # Metrics
    context_reduction_pct: float = Field(description="Measured context reduction percentage.")
    cost_per_accepted_patch: float = Field(description="Measured cost per accepted patch.")
    accepted_patch_rate: float = Field(description="Fraction of cases with accepted patches.")
    routing_regression_rate: float = Field(description="Fraction of cases with regressions.")
    cheap_success_rate: float = Field(description="Success rate on cheap-tier cases.")

    # Evidence
    benchmark_cases: list[BenchmarkCase] = Field(
        default_factory=list, description="Per-benchmark prompt results with evidence links."
    )
    host_enforcement_matrix: list[HostEnforcementSnapshot] = Field(
        default_factory=list, description="Per-host enforcement contracts (WP-31 snapshot)."
    )
    feature_boundary_labels: dict[str, FeatureBoundaryLabel] = Field(
        default_factory=dict,
        description="Per-feature boundary label: Host-native | LemonCrow augmentation | Future-only.",
    )

    # Thresholds used
    config: ProofGateConfig = Field(default_factory=ProofGateConfig, description="Gate thresholds used for this run.")

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
