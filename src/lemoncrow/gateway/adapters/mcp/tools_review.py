"""Human-review MCP handlers with late-bound host/session composition.

The handlers register independently from ``mcp_server``. Runtime identity and
workspace policy are supplied by the composition root so embedded callers that
patch the legacy private helpers keep the same behavior during decomposition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.gateway.adapters.mcp.framework import mcp_tool


@dataclass(frozen=True, slots=True)
class ReviewHandlerHooks:
    resolved_host_session: Callable[[], tuple[str, str]]
    get_mcp_model: Callable[[], str]
    workspace_root: Callable[[], Path]
    session_worktree_root: Callable[[Path], Path | None]


_HooksFactory = Callable[[], ReviewHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_review_handler_hooks(factory: _HooksFactory) -> None:
    """Install the process composition used by review handlers."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> ReviewHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("review handler hooks are not configured")
    return factory()


def _active_repo_root(hooks: ReviewHandlerHooks) -> Path:
    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root

    workspace = hooks.workspace_root()
    active_root = hooks.session_worktree_root(workspace) or workspace
    return detect_repo_root(active_root)


@mcp_tool(name="review_rationale")
def tool_review_rationale(
    entries: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "Compact author rationale for non-obvious code decisions. One call after the code is final; "
                "each item: {path, body, optional title, symbol, line, end_line, evidence[]}. "
                "This records intent only — never correctness or approval."
            )
        ),
    ],
) -> dict[str, Any]:
    """Record the author's own rationale for later human review."""
    hooks = _hooks()
    session_id, host = hooks.resolved_host_session()
    if host != "claude" or not session_id:
        raise ValueError("review_rationale currently requires an exact Claude Code session")

    from lemoncrow.pro.capabilities.review.rationale import record_author_rationales

    records = record_author_rationales(
        default_store_root(),
        _active_repo_root(hooks),
        host=host,
        session_id=session_id,
        model=hooks.get_mcp_model(),
        entries=entries,
    )
    return {
        "status": "recorded",
        "session_id": session_id,
        "count": len(records),
        "rationales": [
            {"id": record.id, "path": record.path, "title": record.title, "symbol": record.symbol} for record in records
        ],
        "note": "author intent only; human review remains required",
    }


@mcp_tool(name="review_evidence")
def tool_review_evidence(
    entries: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "Outcome evidence produced while implementing. One call after verification; each item has exactly one "
                "of {file, url}, plus optional {title, path, kind, capture}. Use path to associate proof with a changed "
                "source file. For a loopback preview URL, set capture=true and LemonCrow captures a screenshot itself. "
                "Verification artifacts may additionally include status=PASS|FAIL|NOT_RUN|UNKNOWN and detail. "
                "Supported kinds: screenshot, video, playwright_trace, document, live_preview."
            )
        ),
    ],
) -> dict[str, Any]:
    """Capture author-produced proof without creating or advancing a review."""
    hooks = _hooks()
    session_id, host = hooks.resolved_host_session()
    if host != "claude" or not session_id:
        raise ValueError("review_evidence currently requires an exact Claude Code session")

    from lemoncrow.pro.capabilities.review.evidence_capture import record_agent_evidence

    records = record_agent_evidence(
        default_store_root(),
        _active_repo_root(hooks),
        host=host,
        session_id=session_id,
        model=hooks.get_mcp_model(),
        entries=entries,
    )
    return {
        "status": "recorded",
        "session_id": session_id,
        "count": len(records),
        "evidence": [
            {"id": record.id, "kind": record.kind, "title": record.title, "path": record.path} for record in records
        ],
        "note": "captured for the matching review revision; human review remains required",
    }


@mcp_tool(name="review_feedback_addressed")
def tool_review_feedback_addressed(
    annotation_ids: Annotated[
        list[str],
        Field(
            description=(
                "LemonCrow annotation IDs from human feedback that you actually addressed. "
                "Call only after making the changes and running relevant verification. "
                "This records an author claim for re-review; it never resolves human comments."
            )
        ),
    ],
) -> dict[str, Any]:
    """Let the exact authoring agent session say which delivered comments it addressed."""
    hooks = _hooks()
    session_id, host = hooks.resolved_host_session()
    if not host or not session_id:
        raise ValueError("review_feedback_addressed requires an exact author session")

    from lemoncrow.pro.capabilities.review.delivery import mark_feedback_addressed
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    records = mark_feedback_addressed(
        ReviewStore(default_store_root()),
        _active_repo_root(hooks),
        annotation_ids,
        host=host,
        session_id=session_id,
    )
    return {
        "status": "addressed",
        "session_id": session_id,
        "count": len(records),
        "annotation_ids": [record.id for record in records],
        "note": "author claim only; each human comment remains open until the reviewer resolves it",
    }


__all__ = [
    "ReviewHandlerHooks",
    "configure_review_handler_hooks",
    "tool_review_evidence",
    "tool_review_feedback_addressed",
    "tool_review_rationale",
]
