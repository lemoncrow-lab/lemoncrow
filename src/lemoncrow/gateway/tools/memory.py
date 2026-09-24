"""Transport-independent orchestration for LemonCrow memory tool operations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def get_memory_block(store: Any, agent_id: str | None, label: str) -> dict[str, Any] | None:
    """Retrieve one editable MemoryBlock by label and return its wire shape."""
    block = store.get_block(agent_id, label)
    return block.model_dump(mode="json") if block is not None else None


def archive_memory(
    archival_recall: Any,
    *,
    agent_id: str | None,
    text: str,
    source: str,
    source_ref: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Archive long-term memory text for later recall."""
    passage = archival_recall.archive(
        agent_id=agent_id,
        text=text,
        source=source,
        source_ref=source_ref,
        tags=tags or [],
    )
    return {"id": passage.id, "dedup_hit": passage.dedup_hit}


def session_recall_passages(root: str | Path, query: str, top_k: int) -> list[dict[str, Any]]:
    """Return past-session recall hits shaped like memory passages."""
    from lemoncrow.core.capabilities import session_recall

    return [
        {
            "id": str(hit.get("session") or ""),
            "text": str(hit.get("text") or ""),
            "source_ref": str(hit.get("session") or ""),
            "tags": list(hit.get("tags") or []),
        }
        for hit in session_recall.recall(root, query, top_k=top_k)
    ]


def recall_memory(
    service: Any,
    *,
    agent_id: str | None,
    query: str,
    top_k: int = 5,
    tags: list[str] | None = None,
    since: str | None = None,
    session_passages: Callable[[str, int], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Merge durable memory recall with optional past-session recall."""
    result = service.recall(
        agent_id=agent_id,
        query=query,
        top_k=top_k,
        tags=tags or None,
        since=since,
    ).model_dump(mode="json")
    mem_passages = result.get("passages")
    if not isinstance(mem_passages, list):
        mem_passages = []
    session = session_passages(query, top_k) if session_passages is not None else []
    if session:
        # Keep past-session recall visible while respecting the advertised top_k
        # maximum across the two independently-ranked stores.
        reserve = min(len(session), max(1, top_k // 3))
        passages = mem_passages[: max(0, top_k - reserve)] + session[:reserve]
    else:
        passages = mem_passages[:top_k]
    result["passages"] = passages
    if not passages:
        result["hint"] = (
            "No matching memories yet — memory accrues as you work. Store durable facts with "
            "memory(op=store_fact); past-session recall improves as sessions are indexed."
        )
    return result


def store_memory_fact(
    service: Any,
    *,
    agent_id: str | None,
    subject: str,
    fact: str,
    citations: str,
    reason: str,
    scope: str,
    field_redactor: Callable[[str, str], str],
) -> dict[str, Any]:
    """Store a durable fact after applying the host's field-aware redaction."""
    return service.store_fact(
        agent_id=agent_id,
        subject=field_redactor(subject, "subject"),
        fact=field_redactor(fact, "fact"),
        citations=field_redactor(citations, "citations"),
        reason=field_redactor(reason, "reason"),
        scope=scope,
    ).model_dump(mode="json")


def vote_memory_fact(
    service: Any,
    *,
    agent_id: str | None,
    fact: str,
    direction: str,
    reason: str,
    scope: str | None,
    field_redactor: Callable[[str, str], str],
) -> dict[str, Any]:
    """Vote on an existing stored fact after host redaction."""
    return service.vote_fact(
        agent_id=agent_id,
        fact=field_redactor(fact, "fact"),
        direction=direction,
        reason=field_redactor(reason, "reason"),
        scope=scope,
    ).model_dump(mode="json")


__all__ = [
    "archive_memory",
    "get_memory_block",
    "recall_memory",
    "session_recall_passages",
    "store_memory_fact",
    "vote_memory_fact",
]
