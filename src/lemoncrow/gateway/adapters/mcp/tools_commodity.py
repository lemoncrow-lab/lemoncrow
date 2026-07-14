"""Commodity MCP tools (public surface): general-purpose agent tools with no
LemonCrow-engine coupling -- safe to ship in the open-source client.

Each tool registers into the shared ``framework.TOOLS`` registry at import time,
so ``mcp_server`` only needs to import this module for the tools to be available.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from lemoncrow.gateway.adapters.mcp.deferral import (
    _defer_web_fetch_enabled,
    _deferral_supported,
    _deferred_completion_executor,
    _DeferredResult,
)
from lemoncrow.gateway.adapters.mcp.framework import mcp_tool


@mcp_tool(
    name="web_fetch",
    description=(
        "Fetch a public HTTP/HTTPS page for research. Requests Markdown when available, "
        "converts HTML to clean Markdown by default, blocks private/local URLs, caches 5 minutes."
    ),
    hidden_params=("timeout_s", "include_meta", "max_chars"),
    param_aliases={"output_format": "type", "format": "type"},
)
def tool_web_fetch(
    url: Annotated[str, Field(description="Public HTTP/HTTPS URL to fetch.")],
    type: Annotated[
        Literal["auto", "markdown", "text", "html"],
        Field(description="Return format. auto prefers Markdown and converts HTML to Markdown."),
    ] = "auto",
    max_chars: Annotated[
        int | None,
        Field(
            description=(
                "Max returned chars, clamped 1,000-100,000. Omit = auto-size to the page. Number = force that cap."
            )
        ),
    ] = None,
    timeout_s: Annotated[
        float,
        Field(description="Network timeout in seconds. Clamped to a safe upper bound."),
    ] = 20.0,
    include_meta: Annotated[
        bool,
        Field(description="Include minimal debug metadata in the internal payload."),
    ] = False,
    query: Annotated[
        str | None,
        Field(
            description=(
                "Optional search term. Long page → keep the sections/table-rows most "
                "relevant to the term (embedding rank when a local embedder is configured, "
                "else keyword coverage), not a blind head cut — jump straight to one "
                "row/section of a long table or doc. Omit = head truncation."
            )
        ),
    ] = None,
    summary: Annotated[
        bool,
        Field(
            description=(
                "true = bounded gist instead of the rendered page: internal-LLM summary when "
                "configured, else type-aware extractive gist. Full page always spilled first; "
                "the footer names the spill path — recover via `read`. Ignored when `query` "
                "or `type='html'` is given — the more specific request wins."
            )
        ),
    ] = False,
) -> dict[str, Any] | _DeferredResult:
    """Fetch a public web page and return coding-agent-friendly content.

    Returns: {content, format, tokens_saved}; the MCP layer renders `content` directly.
    """
    from lemoncrow.core.capabilities.web_fetch import fetch_url

    # Phase 3: on the stdio worker, run the fetch on the shared asyncio reactor so
    # the worker frees immediately; the reactor future's completion fires the
    # deferral continuation (bounced to a small pool so finalize never blocks the
    # loop). Same SSRF-validated fetch, just off the worker. Kill switch:
    # LEMONCROW_MCP_DEFER_WEB_FETCH=0.
    if _defer_web_fetch_enabled() and _deferral_supported():
        from lemoncrow.core.capabilities.web_fetch import async_fetch_url
        from lemoncrow.gateway.adapters._io_reactor import get_io_reactor

        future = get_io_reactor().submit(
            async_fetch_url(
                url,
                output_format=type,
                max_chars=max_chars,
                timeout_s=timeout_s,
                include_meta=include_meta,
                query=query,
                summary=summary,
            )
        )

        def _collect() -> dict[str, Any]:
            return cast(dict[str, Any], future.result())

        def _register(cb: Callable[[], None]) -> bool:
            if future.done():
                return False
            future.add_done_callback(lambda _f: _deferred_completion_executor().submit(cb))
            return True

        return _DeferredResult(collect=_collect, register=_register)

    return fetch_url(
        url,
        output_format=type,
        max_chars=max_chars,
        timeout_s=timeout_s,
        include_meta=include_meta,
        query=query,
        summary=summary,
    )


@mcp_tool(name="mcp")
def tool_mcp(
    op: Annotated[
        Literal["call", "list"],
        Field(
            description=(
                "'list' = catalog of OTHER configured stdio MCP servers + their tools "
                "(name, one-line description, required/optional params). 'call' "
                "(default) = invoke one."
            )
        ),
    ] = "call",
    server: Annotated[
        str | None,
        Field(description="Server name from `mcp(op='list')`. Required for op='call'."),
    ] = None,
    tool: Annotated[
        str | None,
        Field(description="Tool name on that server. Required for op='call'."),
    ] = None,
    params: Annotated[
        dict[str, Any] | None,
        Field(description="Args passed through to the proxied tool."),
    ] = None,
    refresh: Annotated[
        bool,
        Field(description="op='list': re-discover configs + re-query tool lists, skip the cache."),
    ] = False,
) -> dict[str, Any]:
    """Proxy: discover + call tools on OTHER configured stdio MCP servers (never LemonCrow's own).

    ``mcp(op="list")`` first — the configured servers/tools (reads the same
    `.mcp.json` / `.claude/mcp.json` the host uses) — then
    ``mcp(server=..., tool=..., params={...})``. Servers spawn on first use,
    reused after. Oversized proxied results compact/spill like
    `bash`/`code_search`; the canonical footer names the recovery path —
    nothing silently lost.
    """
    from lemoncrow.gateway.adapters import mcp_proxy

    normalized = (op or "call").strip().lower()
    if normalized == "list":
        return mcp_proxy.catalog(refresh=refresh)
    if normalized != "call":
        raise ValueError(f"unsupported mcp op: {op!r}; use 'list' or 'call'")

    def require(name: str, current: str | None) -> str:
        if not current:
            raise ValueError(f"{name} is required for mcp op={normalized!r}")
        return current

    return {
        "content": mcp_proxy.call(require("server", server), require("tool", tool), params or {}),
    }
