"""MCP memory op-dispatch handler with late-bound runtime operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool


@dataclass(frozen=True, slots=True)
class MemoryHandlerHooks:
    recall: Callable[..., dict[str, Any]]
    store_fact: Callable[..., dict[str, Any]]
    vote_fact: Callable[..., dict[str, Any]]
    symbol_recall: Callable[[], Any]


_HooksFactory = Callable[[], MemoryHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_memory_handler_hooks(factory: _HooksFactory) -> None:
    """Install the process composition used by the memory handler."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> MemoryHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("memory handler hooks are not configured")
    return factory()


@mcp_tool(
    name="memory",
    description=("Memory op-dispatch for fact storage/voting and recall."),
    param_aliases={"agent_id": "agent", "top_k": "k"},
)
def tool_memory(
    op: Annotated[
        Literal[
            "recall",
            "recall_symbol",
            "store_fact",
            "vote_fact",
        ],
        Field(
            description=(
                "recall/recall_symbol need query; "
                "store_fact needs subject+fact+citations+reason+scope; "
                "vote_fact needs fact+direction+reason."
            )
        ),
    ],
    agent: Annotated[
        str | None,
        Field(description="Memory namespace; defaults to shared."),
    ] = None,
    query: Annotated[str | None, Field(description="Search query used by recall.")] = None,
    k: Annotated[int, Field(description="Max results to return for recall.")] = 5,
    subject: Annotated[
        str | None,
        Field(description="Fact subject for store_fact (for example: testing, workflow preference)."),
    ] = None,
    fact: Annotated[
        str | None,
        Field(description="Exact fact text for store_fact and vote_fact."),
    ] = None,
    citations: Annotated[
        str | None,
        Field(description="Source citations for store_fact."),
    ] = None,
    reason: Annotated[
        str | None,
        Field(description="Detailed rationale for store_fact and vote_fact."),
    ] = None,
    scope: Annotated[
        Literal["repository", "user"] | None,
        Field(description="Fact scope for store_fact/vote_fact."),
    ] = None,
    direction: Annotated[
        Literal["upvote", "downvote"] | None,
        Field(description="Vote direction for vote_fact."),
    ] = None,
) -> dict[str, Any] | None:
    """Memory op-dispatch: recall, recall_symbol, store_fact, or vote_fact."""
    hooks = _hooks()

    def require(name: str, current: str | None) -> str:
        if not current:
            raise ValueError(f"{name} is required for memory op={op}")
        return current

    if op == "recall":
        return hooks.recall(
            agent_id=agent,
            query=require("query", query),
            top_k=k,
        )
    if op == "store_fact":
        return hooks.store_fact(
            agent_id=agent,
            subject=require("subject", subject),
            fact=require("fact", fact),
            citations=citations or "",
            reason=reason or "",
            scope=require("scope", scope),
        )
    if op == "vote_fact":
        return hooks.vote_fact(
            agent_id=agent,
            fact=require("fact", fact),
            direction=require("direction", direction),
            reason=require("reason", reason),
            scope=scope,
        )
    if op == "recall_symbol":
        return cast(
            dict[str, Any],
            hooks.symbol_recall().recall_symbol(
                query=require("query", query),
                agent_id=agent,
                top_k=k,
            ),
        )
    raise ValueError(f"unsupported memory op: {op}")


__all__ = ["MemoryHandlerHooks", "configure_memory_handler_hooks", "tool_memory"]
