"""Optimization Advisor capability.

The advisor is intentionally deterministic in v0: it uses captured Atelier
traces, policy presets, and conservative replay heuristics to explain likely
cost/quality trade-offs without silently changing runtime behavior.

Three-layer optimization stack
-------------------------------
``session_optimizer`` (sibling module, not in this package)
    **Per-session, real-time.**  Computes trace costs, emits budget-guardrail
    guidance, and powers session-start/stop notices.  Used by hooks, the CLI,
    and the API.  This package depends on it for low-level cost utilities.

``optimization/`` (this package)
    **Cross-session, historical.**  Analyses accumulated trace history →
    identifies policy candidates (routing tier, compaction settings) →
    optionally automates policy writes and PR proposals.  Runs non-inferiority
    tests before applying changes.

``optimization/audit.py`` (moved from ``optimization_audit``)
    **Static audit + quality trends for the HTTP dashboard.**  Audits the
    prompt-surface inventory (config files, rubrics, seed blocks) and computes
    multi-signal quality scores over recent traces.  Only consumed by the API.
"""

from atelier.core.capabilities.optimization.audit import (
    build_context_audit,
    build_session_quality_summary,
)
from atelier.core.capabilities.optimization.automation import (
    PROPOSAL_ARTIFACT_PATH,
    OptimizationProposalPrBot,
    run_optimization_cycle,
)
from atelier.core.capabilities.optimization.compaction_types import (
    ALL_COMPACTION_TYPES,
    CompactionType,
)
from atelier.core.capabilities.optimization.complexity import (
    ComplexityScore,
    ComplexitySignals,
    score_complexity,
    score_trace_complexity,
)
from atelier.core.capabilities.optimization.non_inferiority import (
    NonInferiorityVerdict,
    TerminalBenchArmSummary,
    evaluate_non_inferiority,
    evaluate_non_inferiority_from_runs,
    load_terminalbench_records,
    summarize_terminalbench_arm,
    wilson_interval,
)
from atelier.core.capabilities.optimization.optimizer import (
    Candidate,
    OptimizationResult,
    append_history,
    load_history,
    optimize_from_traces,
)
from atelier.core.capabilities.optimization.policy import (
    AutomationConfig,
    BenchmarkEvidence,
    CompactionPolicy,
    Policy,
    RoutingPolicy,
    load_automation_config,
    load_current_policy,
    preset_policy,
    save_automation_config,
    save_policy,
)

__all__ = [
    "ALL_COMPACTION_TYPES",
    "PROPOSAL_ARTIFACT_PATH",
    "AutomationConfig",
    "BenchmarkEvidence",
    "Candidate",
    "CompactionPolicy",
    "CompactionType",
    "ComplexityScore",
    "ComplexitySignals",
    "NonInferiorityVerdict",
    "OptimizationProposalPrBot",
    "OptimizationResult",
    "Policy",
    "RoutingPolicy",
    "TerminalBenchArmSummary",
    "append_history",
    "build_context_audit",
    "build_session_quality_summary",
    "evaluate_non_inferiority",
    "evaluate_non_inferiority_from_runs",
    "load_automation_config",
    "load_current_policy",
    "load_history",
    "load_terminalbench_records",
    "optimize_from_traces",
    "preset_policy",
    "run_optimization_cycle",
    "save_automation_config",
    "save_policy",
    "score_complexity",
    "score_trace_complexity",
    "summarize_terminalbench_arm",
    "wilson_interval",
]
