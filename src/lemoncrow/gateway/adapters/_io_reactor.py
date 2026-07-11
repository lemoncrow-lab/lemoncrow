"""Singleton asyncio reactor for the MCP server (Phase 3).

One daemon thread runs an event loop forever so I/O-bound coroutines (async
web_fetch) can be scheduled from the synchronous MCP worker threads via
``run_coroutine_threadsafe``. N concurrent network waits share this single loop
thread instead of pinning N pool workers; CPU-bound work stays on the thread
pools and the heavy HTML render is offloaded to the loop's default executor.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any


class _IOReactor:
    """Owns a private event loop running on a dedicated daemon thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        # NB: not named ``_thread`` -- mypyc emits that as a C struct field
        # ``__thread``, which gcc/clang reject as the reserved TLS keyword
        # (same convention as code_warm.CodeWarmer._worker).
        self._worker = threading.Thread(target=self._run, name="lemoncrow-io-reactor", daemon=True)
        self._worker.start()
        # Block until the loop is actually running so the first submit() can't
        # race run_coroutine_threadsafe against a not-yet-started loop.
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        """Schedule *coro* on the reactor loop; return a concurrent.futures.Future."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)


_REACTOR: _IOReactor | None = None
_REACTOR_LOCK = threading.Lock()


def get_io_reactor() -> _IOReactor:
    """Return the process-wide IO reactor, starting it on first use."""
    global _REACTOR
    if _REACTOR is None:
        with _REACTOR_LOCK:
            if _REACTOR is None:
                _REACTOR = _IOReactor()
    return _REACTOR
