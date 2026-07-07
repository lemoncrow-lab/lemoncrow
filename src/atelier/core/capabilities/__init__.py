"""Atelier core capabilities package."""

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
        "BudgetPlan": ("atelier.core.capabilities.budget_optimizer", "BudgetPlan"),
        "ContextBlock": ("atelier.core.capabilities.budget_optimizer", "ContextBlock"),
        "PromptBudgetOptimizer": (
            "atelier.core.capabilities.budget_optimizer",
            "PromptBudgetOptimizer",
        ),
        "QualityRouterCapability": (
            "atelier.core.capabilities.quality_router.capability",
            "QualityRouterCapability",
        ),
        "ContextCompressionCapability": (
            "atelier.core.capabilities.context_compression",
            "ContextCompressionCapability",
        ),
        "FailureAnalysisCapability": (
            "atelier.core.capabilities.failure_analysis",
            "FailureAnalysisCapability",
        ),
        "LessonPromoterCapability": (
            "atelier.core.capabilities.lesson_promotion",
            "LessonPromoterCapability",
        ),
        "ContextReuseCapability": (
            "atelier.core.capabilities.context_reuse",
            "ContextReuseCapability",
        ),
        "ProofGateCapability": (
            "atelier.core.capabilities.proof_gate.capability",
            "ProofGateCapability",
        ),
        "CapabilityNode": ("atelier.core.capabilities.registry", "CapabilityNode"),
        "CapabilityRegistry": ("atelier.core.capabilities.registry", "CapabilityRegistry"),
        "SemanticFileMemoryCapability": (
            "atelier.core.capabilities.semantic_file_memory",
            "SemanticFileMemoryCapability",
        ),
        "TelemetryEvent": ("atelier.core.capabilities.telemetry", "TelemetryEvent"),
        "TelemetrySubstrate": (
            "atelier.core.capabilities.telemetry",
            "TelemetrySubstrate",
        ),
        "ToolSupervisionCapability": (
            "atelier.core.capabilities.tool_supervision",
            "ToolSupervisionCapability",
        ),
    }
    if name not in mapping:
        raise AttributeError(name)
    module_name, symbol = mapping[name]
    return getattr(import_module(module_name), symbol)
