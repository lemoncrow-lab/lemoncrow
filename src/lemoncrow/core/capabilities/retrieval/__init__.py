"""Domain-neutral retrieval surface of the LemonCrow context engine."""

from lemoncrow.core.capabilities.retrieval.protocol import Retriever, default_retriever_factory

__all__ = [
    "Retriever",
    "default_retriever_factory",
]
