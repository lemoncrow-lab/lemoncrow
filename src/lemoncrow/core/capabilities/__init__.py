"""LemonCrow core capabilities package."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "BudgetPlan",
    "CapabilityNode",
    "CapabilityRegistry",
    "ContextBlock",
    "ContextCompressionCapability",
    "ContextReuseCapability",
    "FailureAnalysisCapability",
    "LessonPromoterCapability",
    "PromptBudgetOptimizer",
    "ProofGateCapability",
    "QualityRouterCapability",
    "SemanticFileMemoryCapability",
    "TelemetryEvent",
    "TelemetrySubstrate",
    "ToolSupervisionCapability",
]


def __getattr__(name: str) -> Any:
    mapping = {
        "BudgetPlan": ("lemoncrow.core.capabilities.budget_optimizer", "BudgetPlan"),
        "ContextBlock": ("lemoncrow.core.capabilities.budget_optimizer", "ContextBlock"),
        "PromptBudgetOptimizer": (
            "lemoncrow.core.capabilities.budget_optimizer",
            "PromptBudgetOptimizer",
        ),
        "QualityRouterCapability": (
            "lemoncrow.core.capabilities.quality_router.capability",
            "QualityRouterCapability",
        ),
        "ContextCompressionCapability": (
            "lemoncrow.core.capabilities.context_compression",
            "ContextCompressionCapability",
        ),
        "FailureAnalysisCapability": (
            "lemoncrow.core.capabilities.failure_analysis",
            "FailureAnalysisCapability",
        ),
        "LessonPromoterCapability": (
            "lemoncrow.core.capabilities.lesson_promotion",
            "LessonPromoterCapability",
        ),
        "ContextReuseCapability": (
            "lemoncrow.core.capabilities.context_reuse",
            "ContextReuseCapability",
        ),
        "ProofGateCapability": (
            "lemoncrow.core.capabilities.proof_gate.capability",
            "ProofGateCapability",
        ),
        "CapabilityNode": ("lemoncrow.core.capabilities.registry", "CapabilityNode"),
        "CapabilityRegistry": ("lemoncrow.core.capabilities.registry", "CapabilityRegistry"),
        "SemanticFileMemoryCapability": (
            "lemoncrow.core.capabilities.semantic_file_memory",
            "SemanticFileMemoryCapability",
        ),
        "TelemetryEvent": ("lemoncrow.core.capabilities.telemetry", "TelemetryEvent"),
        "TelemetrySubstrate": (
            "lemoncrow.core.capabilities.telemetry",
            "TelemetrySubstrate",
        ),
        "ToolSupervisionCapability": (
            "lemoncrow.core.capabilities.tool_supervision",
            "ToolSupervisionCapability",
        ),
    }
    if name not in mapping:
        raise AttributeError(name)
    module_name, symbol = mapping[name]
    return getattr(import_module(module_name), symbol)
