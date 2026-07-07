"""Cost-quality proof gate capability (WP-32).

Combines WP-19 context savings, WP-28 routing evals, WP-29 host capability
contracts, WP-30 trace confidence, and WP-31 route execution contracts into
one auditable proof report that can pass or fail a release gate.

Gate thresholds
---------------
- context_reduction_pct >= 50.0
- cost_per_accepted_patch < premium_only_baseline_cost_per_accepted_patch
- accepted_patch_rate >= premium_only_baseline_accepted_patch_rate - 0.03
- routing_regression_rate <= 0.02
- cheap_success_rate >= configured_min_cheap_success_rate
- every benchmark case links to trace evidence

Failed cheap attempts count against total cost and regression rate — they
cannot disappear into "savings."
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

_HOSTS = ("claude", "codex", "copilot", "opencode", "antigravity")

# Feature boundary labels (per WP-32 spec)
FeatureBoundaryLabel = str  # "Host-native" | "Atelier augmentation" | "Future-only"

# Trace confidence levels (per WP-30)
TraceConfidenceLevel = str  # "full_live" | "mcp_live" | "wrapper_live" | "imported" | "manual"

# Per-host trace confidence from WP-30 host-capability-matrix
_HOST_TRACE_CONFIDENCE: dict[str, TraceConfidenceLevel] = {
    "claude": "full_live",
    "codex": "wrapper_live",
    "copilot": "mcp_live",
    "opencode": "wrapper_live",
    "antigravity": "mcp_live",
}

# Per-feature boundary labels from WP-29 / WP-31 implementation boundary
_FEATURE_LABELS: dict[str, FeatureBoundaryLabel] = {
    "context_compression": "Atelier augmentation",
    "routing_decision": "Atelier augmentation",
    "verification": "Atelier augmentation",
    "trace_capture": "Atelier augmentation",
    "model_selection": "Host-native",
    "edit_application": "Host-native",
    "compaction": "Host-native",
    "agent_orchestration": "Host-native",
    "provider_model_override": "Future-only",
}


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
        description="Per-feature boundary label: Host-native | Atelier augmentation | Future-only.",
    )

    # Thresholds used
    config: ProofGateConfig = Field(default_factory=ProofGateConfig, description="Gate thresholds used for this run.")

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


class ProofGateCapability:
    """Assembles and evaluates the cost-quality proof gate."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            from atelier.core.foundation.paths import default_store_root

            root = default_store_root()
        self._root = Path(root)

    def _load_host_enforcement_matrix(self) -> list[HostEnforcementSnapshot]:
        """Build host enforcement snapshot from WP-31 contracts."""
        from atelier.core.capabilities.quality_router.execution_contract import (
            route_execution_contract,
        )

        snapshots: list[HostEnforcementSnapshot] = []
        for host in _HOSTS:
            contract = route_execution_contract(host)
            snapshots.append(
                HostEnforcementSnapshot(
                    host=host,
                    mode=contract.mode,
                    can_block_start=contract.can_block_start,
                    can_force_model=contract.can_force_model,
                    can_require_verification=contract.can_require_verification,
                    fallback_mode=contract.fallback_mode,
                    trace_confidence=_HOST_TRACE_CONFIDENCE.get(host, "manual"),
                    provider_enforced_disabled=contract.provider_enforced_disabled,
                )
            )
        return snapshots

    def run(
        self,
        *,
        session_id: str,
        context_reduction_pct: float,
        benchmark_cases: list[BenchmarkCase],
        config: ProofGateConfig | None = None,
        save: bool = True,
    ) -> ProofReport:
        """Evaluate the proof gate and return a ProofReport.

        Parameters
        ----------
        session_id:
            Stable identifier for this proof run.
        context_reduction_pct:
            Context reduction percentage from WP-19 savings bench.
        benchmark_cases:
            Per-prompt cases with tier, cost, acceptance, and evidence IDs.
            Failed cheap attempts must be included — they cannot be elided.
        config:
            Gate thresholds.  Defaults to ``ProofGateConfig()`` production values.
        save:
            If True, write ``proof-report.json`` and ``proof-report.md`` to
            ``.atelier/proof/``.
        """
        if config is None:
            config = ProofGateConfig()

        # --- compute metrics ---
        total = len(benchmark_cases)
        accepted_count = sum(1 for c in benchmark_cases if c.accepted)
        total_cost = sum(max(0.0, c.cost_usd) for c in benchmark_cases)
        cheap_cases = [c for c in benchmark_cases if c.tier == "cheap"]
        cheap_accepted = sum(1 for c in cheap_cases if c.accepted)
        regressions = sum(1 for c in benchmark_cases if c.regression)

        cost_per_accepted_patch = total_cost / accepted_count if accepted_count > 0 else total_cost
        accepted_patch_rate = accepted_count / total if total > 0 else 0.0
        routing_regression_rate = regressions / total if total > 0 else 0.0
        cheap_success_rate = cheap_accepted / len(cheap_cases) if cheap_cases else 0.0

        # --- evaluate thresholds ---
        failed: list[str] = []

        if context_reduction_pct < config.context_reduction_pct_min:
            failed.append("context_reduction_pct")

        if cost_per_accepted_patch >= config.premium_only_baseline_cost_per_accepted_patch:
            failed.append("cost_per_accepted_patch")

        if accepted_patch_rate < config.premium_only_baseline_accepted_patch_rate - 0.03:
            failed.append("accepted_patch_rate")

        if routing_regression_rate > config.routing_regression_rate_max:
            failed.append("routing_regression_rate")

        if cheap_cases and cheap_success_rate < config.min_cheap_success_rate:
            failed.append("cheap_success_rate")

        # --- every benchmark case must link to trace evidence ---
        missing_trace = [c.case_id for c in benchmark_cases if not (c.trace_id or "").strip()]
        if missing_trace:
            failed.append("missing_trace_evidence")

        # --- assemble report ---
        report = ProofReport(
            session_id=session_id,
            status="pass" if not failed else "fail",
            failed_thresholds=failed,
            context_reduction_pct=round(context_reduction_pct, 4),
            cost_per_accepted_patch=round(cost_per_accepted_patch, 6),
            accepted_patch_rate=round(accepted_patch_rate, 6),
            routing_regression_rate=round(routing_regression_rate, 6),
            cheap_success_rate=round(cheap_success_rate, 6),
            benchmark_cases=benchmark_cases,
            host_enforcement_matrix=self._load_host_enforcement_matrix(),
            feature_boundary_labels=dict(_FEATURE_LABELS),
            config=config,
        )

        if save:
            self._save(report)

        return report

    def load(self) -> ProofReport | None:
        """Load the last saved proof report from ``.atelier/proof/proof-report.json``."""
        proof_json = self._root / "proof" / "proof-report.json"
        if not proof_json.exists():
            return None
        data: dict[str, Any] = json.loads(proof_json.read_text(encoding="utf-8"))
        return ProofReport.model_validate(data)

    def _save(self, report: ProofReport) -> None:
        """Persist proof report to ``.atelier/proof/``."""
        proof_dir = self._root / "proof"
        proof_dir.mkdir(parents=True, exist_ok=True)

        json_path = proof_dir / "proof-report.json"
        md_path = proof_dir / "proof-report.md"

        json_path.write_text(
            json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        md_path.write_text(_render_markdown(report), encoding="utf-8")


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _render_markdown(report: ProofReport) -> str:
    lines: list[str] = [
        "# Cost-Quality Proof Report",
        "",
        f"**Run ID:** `{report.session_id}`  ",
        f"**Status:** {'✅ PASS' if report.status == 'pass' else '❌ FAIL'}  ",
        f"**Generated:** {report.created_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "## Metrics",
        "",
        "| Threshold | Measured | Limit | Pass? |",
        "|-----------|----------|-------|-------|",
        (
            f"| context_reduction_pct | {report.context_reduction_pct:.1f}% "
            f"| ≥ {report.config.context_reduction_pct_min:.1f}% "
            f"| {'✅' if 'context_reduction_pct' not in report.failed_thresholds else '❌'} |"
        ),
        (
            f"| cost_per_accepted_patch | ${report.cost_per_accepted_patch:.4f} "
            f"| < ${report.config.premium_only_baseline_cost_per_accepted_patch:.4f} "
            f"| {'✅' if 'cost_per_accepted_patch' not in report.failed_thresholds else '❌'} |"
        ),
        (
            f"| accepted_patch_rate | {report.accepted_patch_rate:.3f} "
            f"| ≥ {report.config.premium_only_baseline_accepted_patch_rate - 0.03:.3f} "
            f"| {'✅' if 'accepted_patch_rate' not in report.failed_thresholds else '❌'} |"
        ),
        (
            f"| routing_regression_rate | {report.routing_regression_rate:.3f} "
            f"| ≤ {report.config.routing_regression_rate_max:.2f} "
            f"| {'✅' if 'routing_regression_rate' not in report.failed_thresholds else '❌'} |"
        ),
        (
            f"| cheap_success_rate | {report.cheap_success_rate:.3f} "
            f"| ≥ {report.config.min_cheap_success_rate:.2f} "
            f"| {'✅' if 'cheap_success_rate' not in report.failed_thresholds else '❌'} |"
        ),
        "",
    ]

    if report.failed_thresholds:
        lines += [
            "## Failed Thresholds",
            "",
            *[f"- `{t}`" for t in report.failed_thresholds],
            "",
        ]

    lines += [
        "## Host Enforcement Matrix",
        "",
        "| Host | Mode | Block Start | Force Model | Require Verification | Trace Confidence |",
        "|------|------|-------------|-------------|----------------------|-----------------|",
    ]
    for h in report.host_enforcement_matrix:
        lines.append(
            f"| {h.host} | {h.mode} | {h.can_block_start} "
            f"| {h.can_force_model} | {h.can_require_verification} "
            f"| {h.trace_confidence} |"
        )

    lines += [
        "",
        "## Feature Boundary Labels",
        "",
        "| Feature | Label |",
        "|---------|-------|",
    ]
    for feature, label in sorted(report.feature_boundary_labels.items()):
        lines.append(f"| {feature} | {label} |")

    lines += [
        "",
        "## Benchmark Cases",
        "",
        "| Case ID | Tier | Accepted | Cost USD | Regression | Trace ID |",
        "|---------|------|----------|----------|------------|----------|",
    ]
    for c in report.benchmark_cases:
        lines.append(
            f"| {c.case_id} | {c.tier} | {c.accepted} "
            f"| ${c.cost_usd:.4f} | {c.regression} "
            f"| {c.trace_id or 'MISSING'} |"
        )

    lines.append("")
    return "\n".join(lines)


__all__ = [
    "BenchmarkCase",
    "HostEnforcementSnapshot",
    "ProofGateCapability",
    "ProofGateConfig",
    "ProofReport",
]
