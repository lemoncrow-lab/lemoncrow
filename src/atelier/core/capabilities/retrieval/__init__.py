"""Domain-neutral retrieval surface of the Atelier context engine."""

from atelier.core.capabilities.retrieval.protocol import Retriever, default_retriever_factory

__all__ = [
    "Retriever",
    "default_retriever_factory",
]
