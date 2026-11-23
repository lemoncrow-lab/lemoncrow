"""Authenticated Review invalidations driven by filesystem notifications."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

from watchfiles import awatch

QUIET_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
#: How often a polled stream re-reads its projection (see ``filesystem=False``).
POLL_SECONDS = 2.0


def _frame(event: str, state: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(state, separators=(',', ':'))}\n\n"


async def _polled_events(
    project: Callable[[set[tuple[Any, str]]], dict[str, Any]],
    *,
    event: str,
    settle: bool,
) -> AsyncIterator[str]:
    """The same stream from periodic projections, holding no thread between reads.

    A settling stream reports ``settled`` once two consecutive reads agree on the fingerprint.
    """

    def observe(previous_fingerprint: object) -> dict[str, Any]:
        current = project(set())
        if settle:
            current = {**current, "settled": current.get("fingerprint") == previous_fingerprint}
        return current

    previous = await asyncio.to_thread(observe, object())
    yield _frame(event, previous)
    idle = 0.0
    while True:
        await asyncio.sleep(POLL_SECONDS)
        current = await asyncio.to_thread(observe, previous.get("fingerprint"))
        if current != previous:
            previous = current
            idle = 0.0
            yield _frame(event, current)
            continue
        idle += POLL_SECONDS
        if idle >= HEARTBEAT_SECONDS:
            idle = 0.0
            yield ": keepalive\n\n"


async def state_events(
    roots: list[Path],
    project: Callable[[set[tuple[Any, str]]], dict[str, Any]],
    *,
    event: str,
    watch_filter: Callable[[Any, str], bool] | None = None,
    settle: bool = False,
    filesystem: bool = True,
) -> AsyncIterator[str]:
    """Send an initial projection, changes and keepalives; close the watcher on exit.

    Source gets a second observation after a quiet second, preserving the
    automatic-capture stability check without repeatedly reading an idle tree.

    ``filesystem=False`` polls ``project`` instead of watching ``roots``. A filesystem
    watcher occupies one worker thread of the event loop's shared thread limiter for as
    long as its stream is open, the limiter every synchronous request handler of the same
    application waits on; a server holding many open streams (hosted: every open Reader
    tab of every tenant) would starve its own requests. State kept in a database never
    produces a filesystem notification anyway.
    """
    if not filesystem:
        async for frame in _polled_events(project, event=event, settle=settle):
            yield frame
        return
    watcher = cast(
        AsyncGenerator[set[tuple[Any, str]]],
        awatch(
            *roots, watch_filter=watch_filter, debounce=200, step=50, force_polling=False, recursive=True
        ).__aiter__(),
    )
    pending: asyncio.Future[set[tuple[Any, str]]] = asyncio.ensure_future(anext(watcher))
    previous: dict[str, Any] | None = None
    quiet = settle
    try:
        current = await asyncio.to_thread(project, set())
        if settle:
            current = {**current, "settled": False}
        previous = current
        yield _frame(event, current)
        while True:
            ready, _ = await asyncio.wait({pending}, timeout=QUIET_SECONDS if quiet else HEARTBEAT_SECONDS)
            if ready:
                try:
                    changes = pending.result()
                except StopAsyncIteration:
                    return
                pending = asyncio.ensure_future(anext(watcher))
                current = await asyncio.to_thread(project, changes)
                quiet = settle
                if settle:
                    current = {**current, "settled": False}
                if current != previous or settle:
                    previous = current
                    yield f"event: {event}\ndata: {json.dumps(current, separators=(',', ':'))}\n\n"
            elif quiet:
                current = await asyncio.to_thread(project, set())
                # An edit arriving while the source was read is not a stable observation.
                if not pending.done():
                    current = {**current, "settled": current.get("fingerprint") == previous.get("fingerprint")}
                    previous = current
                    yield f"event: {event}\ndata: {json.dumps(current, separators=(',', ':'))}\n\n"
                    quiet = not current["settled"]
            else:
                yield ": keepalive\n\n"
    finally:
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await pending
        await watcher.aclose()
