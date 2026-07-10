"""Atelier-native code context engine.

The code vertical of the engine's domain-neutral one-shot retrieval
contract (:class:`atelier.core.capabilities.retrieval.Retriever`).
``CodeRetriever`` is the protocol-facing name for ``CodeContextEngine``.
"""

from atelier.core.capabilities.code_context.budget import BudgetPacker
from atelier.core.capabilities.code_context.cache import RetrievalCache
from atelier.core.capabilities.code_context.engine import CodeContextEngine
from atelier.core.capabilities.code_context.models import (
    ContextPack,
    IndexStats,
    SymbolRecord,
    TextMatch,
)

# Protocol-facing alias: the code vertical of the neutral Retriever contract.
CodeRetriever = CodeContextEngine

__all__ = [
    "BudgetPacker",
    "CodeContextEngine",
    "CodeRetriever",
    "ContextPack",
    "IndexStats",
    "RetrievalCache",
    "SymbolRecord",
    "TextMatch",
]
