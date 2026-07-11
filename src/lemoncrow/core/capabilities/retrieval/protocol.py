"""Domain-neutral one-shot retrieval contract.

The engine's defining promise: a single call returns everything the model
needs for the task, packed to a token budget -- follow-up queries are
allowed, retry loops are never required. Code retrieval
(:class:`lemoncrow.core.capabilities.code_context.CodeContextEngine`) is the
first conforming retriever; any corpus (docs, tickets, chat memory) can
plug in by conforming to :class:`Retriever`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Retriever(Protocol):
    """One-shot context retriever over a corpus.

    Contract:

    - ``source_id`` is a stable identifier for the corpus (a repo, a
      docset, a mailbox) used for caching, telemetry, and memory scoping.
    - ``retrieve`` is ONE-SHOT: a single call returns a budget-packed
      context payload (matches, related items, pointers) sufficient for
      the model to act. Callers may issue further queries, but a
      conforming retriever must never *require* a grep-then-read loop.
    """

    @property
    def source_id(self) -> str: ...

    def retrieve(
        self,
        query: str,
        *,
        budget_tokens: int = 2000,
        max_items: int = 8,
        seeds: list[str] | None = None,
    ) -> dict[str, Any]: ...


def default_retriever_factory(root: str | Path) -> Retriever:
    """Default retriever: the code vertical.

    Imported lazily so the neutral retrieval surface never drags
    tree-sitter / code-intel dependencies into non-code deployments.
    """
    from lemoncrow.core.capabilities.code_context import CodeContextEngine

    return CodeContextEngine(root)
