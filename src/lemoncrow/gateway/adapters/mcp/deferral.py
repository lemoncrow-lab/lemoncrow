"""Deferred-execution machinery for the MCP server.

A tool handler that starts external work (a background bash job, an async web
fetch) returns a ``_DeferredResult`` instead of blocking; the stdio worker later
fires a continuation to write the JSON-RPC response. Stdlib-only, no ``lemoncrow``
imports, so any tool module can depend on it.

Extracted verbatim from ``mcp_server.py`` (behaviour-preserving); ``mcp_server``
re-exports these names for backward compatibility.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any


class _DeferredResult:
    """Returned by a handler that has started external work and will produce its
    real result later. ``collect()`` yields the final result dict (called once,
    after the work is known complete, so it does not block); ``register(cb)``
    registers a completion callback, returning False if already complete."""

    def __init__(
        self,
        collect: Callable[[], dict[str, Any]],
        register: Callable[[Callable[[], None]], bool],
    ) -> None:
        self.collect = collect
        self.register = register


class _Deferred:
    """Sentinel returned by _handle telling _handle_and_write not to write now;
    the response will be produced by a watcher-fired continuation."""

    def __init__(
        self,
        src: _DeferredResult,
        finalize: Callable[[dict[str, Any]], dict[str, Any]],
        finalize_error: Callable[[Exception], dict[str, Any]],
    ) -> None:
        self.src = src
        self.finalize = finalize
        # Routes a failed deferred result (e.g. a web_fetch network/SSRF error)
        # through the same tool-error pipeline the synchronous path uses.
        self.finalize_error = finalize_error


def _defer_bash_enabled() -> bool:
    """Phase 2 deferred-bash kill switch. Default ENABLED; set LEMONCROW_MCP_DEFER_BASH
    to 0/false/no/off to fall back to the synchronous busy-poll."""
    raw = os.environ.get("LEMONCROW_MCP_DEFER_BASH", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


# Deferral is only safe where a continuation can later write the JSON-RPC response
# -- i.e. the stdio server worker path (_handle_and_write). _handle and the tool
# handlers are also called synchronously by the CLI / in-process runtime, which
# cannot process a deferred marker; this thread-local, set only by
# _handle_and_write, keeps those callers on the synchronous path.
_deferral_context: threading.local = threading.local()


def _deferral_supported() -> bool:
    return bool(getattr(_deferral_context, "active", False))


def _defer_web_fetch_enabled() -> bool:
    """Phase 3 deferred-web_fetch kill switch. Default ENABLED; set
    LEMONCROW_MCP_DEFER_WEB_FETCH to 0/false/no/off to fetch synchronously."""
    raw = os.environ.get("LEMONCROW_MCP_DEFER_WEB_FETCH", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


# Small pool that runs deferred completions (collect + finalize + write) off the
# reactor loop thread, so finalize work never blocks the event loop. Lazy so it
# is never created in CLI / in-process contexts that don't defer.
_DEFERRED_COMPLETION_EXECUTOR: ThreadPoolExecutor | None = None
_DEFERRED_COMPLETION_LOCK = threading.Lock()


def _deferred_completion_executor() -> ThreadPoolExecutor:
    global _DEFERRED_COMPLETION_EXECUTOR
    if _DEFERRED_COMPLETION_EXECUTOR is None:
        with _DEFERRED_COMPLETION_LOCK:
            if _DEFERRED_COMPLETION_EXECUTOR is None:
                _DEFERRED_COMPLETION_EXECUTOR = ThreadPoolExecutor(
                    max_workers=8, thread_name_prefix="lemoncrow-defer-fin"
                )
    return _DEFERRED_COMPLETION_EXECUTOR
