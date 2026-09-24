"""The tool execution matrix, as data.

Source of truth: ``docs-internal/enterprise/hosted-mcp-thin-client.md``
section 5 ("Tool execution matrix"), which the enterprise plan's Phase 11B
adopts. Site is a property of the tool, not of a product tier, so the table
belongs in one place that both the routes and the authorizer read.

Every tool name registered by the public ``lemoncrow`` MCP server is classified
here. ``tests/test_matrix.py`` asserts, against the real registry when the
public package is importable, that the classification is total and disjoint --
so a tool added upstream fails the build instead of silently defaulting to
"reachable over the network".
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from .errors import AgentAction, ErrorCode, ServerError

__all__ = [
    "SECURITY_EXCLUDED_TOOLS",
    "SERVER_ADMIN_TOOLS",
    "SERVER_TOOLS",
    "Site",
    "require_server_side",
    "site_of",
]


class Site(StrEnum):
    """Where a tool executes."""

    #: Runs on the server, reachable at ``POST /v1/tools/{name}``.
    SERVER = "server"
    #: Runs on the server but administers the index; privileged.
    SERVER_ADMIN = "server_admin"
    #: Runs in the client (Mode A: the laptop; Mode B: the remote workspace).
    CLIENT = "client"
    #: Client enumerates and uploads; the server builds. Not a tool RPC --
    #: it is the ``/v1/blobs`` + ``/v1/views`` sync protocol.
    CLIENT_UPLOAD_SERVER_BUILD = "client_upload_server_build"
    #: Drives a local browser/display. Never server-side, at any privilege.
    LOCAL_CAPTURE = "local_capture"


#: Server-side tools, verbatim from the design's matrix.
SERVER_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "code_search",  # needs the index
        "read",  # resolves ranges/outlines/symbols from the index
        "relations",  # link index
        "graph",  # link index
        "search",  # semantic / embeddings
        "context",  # already remote-routable via RemoteClient
        "memory",
        "verify",
        "trace",
        "rescue",
        "compact",
        "orient",
        "web_fetch",  # keeps egress where policy can see it
        "statusline_segment",  # session state moves server-side
        "review_rationale",  # review_*, except local capture
        "review_evidence",
        "review_feedback_addressed",
    }
)

#: Index administration. Not in the design's matrix because it is not part of
#: the agent-facing surface; it administers server-side state, so it is exposed
#: server-side and gated on the ``admin`` role.
SERVER_ADMIN_TOOLS: Final[frozenset[str]] = frozenset({"cache"})

#: Public tools intentionally absent from the enterprise thin client. They
#: spawn another MCP/LemonCrow process locally, which the enterprise client is
#: structurally forbidden to do.
SECURITY_EXCLUDED_TOOLS: Final[frozenset[str]] = frozenset({"mcp", "agent", "workflow"})

#: Client-side tools, verbatim from the one-process design matrix.
CLIENT_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "bash",  # subprocesses, process groups, cwd
        "edit",  # writes the working tree; pushes changed blobs after
        "grep",  # ripgrep over the working tree, no index needed
        "blame",  # git history is local
        "scan",  # ast-grep binary over local files
        "codemod",
        "sql",  # local DSNs and .env discovery
    }
)

#: ``index`` in the matrix is "client -> server": the client enumerates and
#: uploads, the server builds. There is no ``POST /v1/tools/index``; the work
#: happens through the sync protocol.
SYNC_TOOLS: Final[frozenset[str]] = frozenset({"index"})

#: Review capture drives a local browser. The design excludes it from the
#: server-side review surface. No such tool is registered upstream today; the
#: deny list exists so that if one appears it is excluded by construction
#: rather than by whoever next edits SERVER_TOOLS.
LOCAL_CAPTURE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "review_capture",
        "review_screenshot",
        "review_screenshot_capture",
    }
)

_SITES: Final[Mapping[str, Site]] = {
    **{name: Site.SERVER for name in SERVER_TOOLS},
    **{name: Site.SERVER_ADMIN for name in SERVER_ADMIN_TOOLS},
    **{name: Site.CLIENT for name in CLIENT_TOOLS},
    **{name: Site.CLIENT_UPLOAD_SERVER_BUILD for name in SYNC_TOOLS},
    **{name: Site.LOCAL_CAPTURE for name in LOCAL_CAPTURE_TOOLS},
}

#: Everything this server will ever dispatch, at any privilege level.
DISPATCHABLE_TOOLS: Final[frozenset[str]] = SERVER_TOOLS | SERVER_ADMIN_TOOLS


def site_of(tool: str) -> Site | None:
    """Classification for ``tool``, or ``None`` when it is unclassified.

    ``None`` is a refusal, never a default-allow: :func:`require_server_side`
    turns it into ``tool_unknown``.
    """
    return _SITES.get(tool)


def require_server_side(tool: str) -> Site:
    """Assert ``tool`` may be dispatched here, or raise the actionable refusal."""
    site = site_of(tool)
    if site is None:
        raise ServerError(
            ErrorCode.TOOL_UNKNOWN,
            f"unknown tool: {tool}",
            details={"tool": tool},
        )
    if site is Site.SERVER or site is Site.SERVER_ADMIN:
        return site
    if site is Site.CLIENT:
        raise ServerError(
            ErrorCode.TOOL_NOT_SERVER_SIDE,
            f"{tool} executes on the client; the server does not run it",
            details={"tool": tool, "site": site.value},
            action=AgentAction.USE_CLIENT_TOOL,
        )
    if site is Site.CLIENT_UPLOAD_SERVER_BUILD:
        raise ServerError(
            ErrorCode.TOOL_NOT_SERVER_SIDE,
            f"{tool} is performed through the sync protocol, not a tool call",
            details={"tool": tool, "site": site.value, "endpoint": "/v1/blobs"},
            action=AgentAction.USE_SYNC_PROTOCOL,
        )
    raise ServerError(
        ErrorCode.TOOL_NOT_SERVER_SIDE,
        f"{tool} requires local capture and is never dispatched server-side",
        details={"tool": tool, "site": site.value},
        action=AgentAction.USE_CLIENT_TOOL,
    )
