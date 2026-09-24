"""Transport-neutral repeated-tool-call loop review policy and state."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

loop_tracker_sessions: dict[str, Any] = {}
MAX_LOOP_TRACKER_SESSIONS = 64
LOOP_TRACKER_LOCK = threading.Lock()


def loop_review_enabled() -> bool:
    """Whether the spiral nudge runs (operator off-switch, default on)."""
    return os.environ.get("LEMONCROW_LOOP_REVIEW", "").strip().lower() not in ("0", "false", "no", "off")


def loop_nudge_for_call(
    name: str,
    args: dict[str, Any],
    *,
    session_id_getter: Callable[[], str],
) -> str | None:
    """Return a soft nudge when an identical call repeats past threshold."""
    from lemoncrow.pro.capabilities.tool_supervision.loop_review import (
        SessionLoopTracker,
        call_signature,
        repeat_nudge,
    )

    if call_signature(name, args) is None:
        return None
    session_id = session_id_getter() or "_global"
    with LOOP_TRACKER_LOCK:
        tracker = loop_tracker_sessions.get(session_id)
        if tracker is None:
            tracker = SessionLoopTracker()
            loop_tracker_sessions[session_id] = tracker
            if len(loop_tracker_sessions) > MAX_LOOP_TRACKER_SESSIONS:
                for stale in list(loop_tracker_sessions)[: len(loop_tracker_sessions) - MAX_LOOP_TRACKER_SESSIONS]:
                    if stale != session_id:
                        loop_tracker_sessions.pop(stale, None)
        count = tracker.record(name, args)
    return repeat_nudge(name, count)


__all__ = [
    "LOOP_TRACKER_LOCK",
    "MAX_LOOP_TRACKER_SESSIONS",
    "loop_nudge_for_call",
    "loop_review_enabled",
    "loop_tracker_sessions",
]
