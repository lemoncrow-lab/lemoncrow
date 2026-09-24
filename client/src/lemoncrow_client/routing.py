"""The tool execution matrix, as data.

This module is the public client's execution contract. The proprietary server
keeps its own copy in ``lemoncrow_server_core.matrix``; ``tests/test_routing.py``
asserts the two agree tool-for-tool whenever the server package is importable.
Enterprise architecture and deployment rationale intentionally live outside the
public documentation tree.

**Routing is a table, not a branch.** There is exactly one place where "does
this run here or there" is decided, every row carries the design's own reason
in ``reason``, and every row is exercised by a test. A tool added without a row
is refused by name rather than defaulting to either side -- defaulting to the
server would send a local-only tool over the network, and defaulting to the
client would silently drop the index.

Three fields do real work beyond the site:

``pushes_blobs``
    The tool writes the working tree, so the blob service pushes what changed
    and commits an overlay revision before the call returns. That is what makes
    "edit then read" a protocol guarantee instead of a race.
``offline``
    A server-sited tool with a bounded local answer when MCP initialization
    could not reach the server. Exactly one row has it -- ``read``. The answer
    is flagged degraded; it is never an index rebuild.
``executor``
    The name of a local executor, bound in :mod:`lemoncrow_client.localtools`.
    A name, not a callable, so this module imports nothing that spawns a
    process and the table stays readable as data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .errors import AgentAction, ClientError, ErrorCode

__all__ = [
    "ROUTES",
    "SECURITY_EXCLUDED_TOOLS",
    "Route",
    "Site",
    "route_for",
    "tool_names",
]


class Site(StrEnum):
    """Where a tool executes."""

    #: In this process, on the developer's machine.
    CLIENT = "client"
    #: At ``POST /v1/tools/{name}`` on the LemonCrow server.
    SERVER = "server"
    #: Server-side, but it administers the index; the server gates it on the
    #: ``admin`` role and refuses it for an ordinary developer credential.
    SERVER_ADMIN = "server_admin"
    #: The client enumerates and uploads; the server builds. Not a tool RPC at
    #: all -- it is the ``/v1/views`` + ``/v1/blobs`` sync protocol.
    CLIENT_UPLOAD_SERVER_BUILD = "client_upload_server_build"


@dataclass(frozen=True, slots=True)
class Route:
    """One row of the execution matrix."""

    tool: str
    site: Site
    #: The design matrix's "Why" column, verbatim.
    reason: str
    #: Local executor name, for rows that run here. Empty for server rows.
    executor: str = ""
    #: The tool writes the working tree: push changed content afterwards.
    pushes_blobs: bool = False
    #: Local executor used when the server never came up. Empty for rows with
    #: no honest local answer -- which is most of them.
    offline: str = ""

    @property
    def local(self) -> bool:
        return self.site is Site.CLIENT

    @property
    def remote(self) -> bool:
        return self.site in (Site.SERVER, Site.SERVER_ADMIN)


# Public tools deliberately absent from the enterprise thin client. Each can
# create another MCP/LemonCrow process, which violates the one-process client
# contract. They may return only after a future server-side implementation
# exists; there is no local fallback.
SECURITY_EXCLUDED_TOOLS: Final[frozenset[str]] = frozenset({"mcp", "agent", "workflow"})


def _rows() -> tuple[Route, ...]:
    return (
        # -- client ------------------------------------------------------- #
        Route("bash", Site.CLIENT, "subprocesses, process groups, cwd", executor="bash"),
        Route(
            "edit",
            Site.CLIENT,
            "writes the working tree; pushes changed blobs after",
            executor="edit",
            pushes_blobs=True,
        ),
        Route("grep", Site.CLIENT, "ripgrep over the working tree, no index needed", executor="grep"),
        Route("blame", Site.CLIENT, "git history is local", executor="blame"),
        Route("scan", Site.CLIENT, "ast-grep binary over local files", executor="scan"),
        Route(
            "codemod",
            Site.CLIENT,
            "ast-grep binary over local files",
            executor="codemod",
            # A codemod rewrites files in place, so the server's view has to be
            # brought forward exactly as it is after an edit.
            pushes_blobs=True,
        ),
        Route("sql", Site.CLIENT, "local DSNs and .env discovery", executor="sql"),
        # -- client uploads, server builds -------------------------------- #
        Route(
            "index",
            Site.CLIENT_UPLOAD_SERVER_BUILD,
            "client enumerates and uploads; server builds",
            executor="index",
        ),
        # -- server ------------------------------------------------------- #
        Route("code_search", Site.SERVER, "needs the index"),
        Route(
            "read",
            Site.SERVER,
            "resolves ranges/outlines/symbols from the index; falls back to a client pull on a blob miss",
            offline="read_from_disk",
        ),
        Route("relations", Site.SERVER, "link index"),
        Route("graph", Site.SERVER, "link index"),
        Route("search", Site.SERVER, "embeddings"),
        Route("context", Site.SERVER, "already remote-routable via RemoteClient"),
        Route("memory", Site.SERVER, "already remote-routable via RemoteClient"),
        Route("verify", Site.SERVER, "already remote-routable via RemoteClient"),
        Route("trace", Site.SERVER, "already remote-routable via RemoteClient"),
        Route("rescue", Site.SERVER, "already remote-routable via RemoteClient"),
        Route("compact", Site.SERVER, "already remote-routable via RemoteClient"),
        Route("orient", Site.SERVER, "already remote-routable via RemoteClient"),
        Route("web_fetch", Site.SERVER, "pure network; keeps egress on the server where policy can see it"),
        Route("statusline_segment", Site.SERVER, "session state moves server-side"),
        Route("review_rationale", Site.SERVER, "server, except screenshot capture"),
        Route("review_evidence", Site.SERVER, "server, except screenshot capture"),
        Route("review_feedback_addressed", Site.SERVER, "server, except screenshot capture"),
        # -- server, privileged ------------------------------------------- #
        Route("cache", Site.SERVER_ADMIN, "administers server-side index state"),
    )


#: The table. Ordered by tool name so a diff against the design reads cleanly.
ROUTES: Final[Mapping[str, Route]] = {route.tool: route for route in sorted(_rows(), key=lambda r: r.tool)}


def tool_names() -> tuple[str, ...]:
    return tuple(sorted(ROUTES))


def route_for(tool: str) -> Route:
    """The row for ``tool``, or a refusal naming it.

    Never a default. An unrouted tool is a build error that has escaped into a
    session, and answering it either way would be a guess about where the
    developer's source code is.
    """
    route = ROUTES.get(tool)
    if route is None:
        raise ClientError(
            ErrorCode.TOOL_UNKNOWN,
            f"unknown tool: {tool}",
            details={"tool": tool},
            action=AgentAction.ABANDON,
        )
    return route
