"""Optimization Advisor capability.

The advisor is intentionally deterministic in v0: it uses captured Atelier
traces, policy presets, and conservative replay heuristics to explain likely
cost/quality trade-offs without silently changing runtime behavior.
"""

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
from atelier.core.capabilities.optimization.optimizer import (
    Candidate,
    OptimizationResult,
    append_history,
    load_history,
    optimize_from_traces,
)
from atelier.core.capabilities.optimization.policy import (
    CompactionPolicy,
    Policy,
    RoutingPolicy,
    load_current_policy,
    preset_policy,
    save_policy,
)

__all__ = [
    "ALL_COMPACTION_TYPES",
    "Candidate",
    "CompactionPolicy",
    "CompactionType",
    "ComplexityScore",
    "ComplexitySignals",
    "OptimizationResult",
    "Policy",
    "RoutingPolicy",
    "append_history",
    "load_current_policy",
    "load_history",
    "optimize_from_traces",
    "preset_policy",
    "save_policy",
    "score_complexity",
    "score_trace_complexity",
]
