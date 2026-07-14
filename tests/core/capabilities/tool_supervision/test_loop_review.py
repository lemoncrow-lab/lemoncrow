"""Unit tests for the MCP-boundary spiral nudge (loop_review)."""

from __future__ import annotations

from lemoncrow.pro.capabilities.tool_supervision.loop_review import (
    SessionLoopTracker,
    call_signature,
    repeat_nudge,
)


def test_identical_repeat_sensitive_call_trips_at_threshold() -> None:
    tracker = SessionLoopTracker()
    counts = [tracker.record("bash", {"command": "pytest -q"}) for _ in range(4)]
    assert counts == [1, 2, 3, 4]
    assert repeat_nudge("bash", 3) is None  # one below threshold
    nudge = repeat_nudge("bash", 4)
    assert nudge is not None
    assert "[loop]" in nudge and "bash" in nudge


def test_read_is_not_tracked() -> None:
    # Re-reading a file after an edit is normal iteration -> must never nudge.
    assert call_signature("read", {"path": "a.py"}) is None
    tracker = SessionLoopTracker()
    assert all(tracker.record("read", {"path": "a.py"}) == 0 for _ in range(8))
    assert repeat_nudge("read", 0) is None


def test_unlisted_tool_is_not_tracked() -> None:
    assert call_signature("memory", {"action": "recall"}) is None
    tracker = SessionLoopTracker()
    assert tracker.record("memory", {"action": "recall"}) == 0


def test_distinct_arguments_do_not_accumulate() -> None:
    tracker = SessionLoopTracker()
    counts = [tracker.record("grep", {"content_regex": f"needle{i}"}) for i in range(5)]
    assert counts == [1, 1, 1, 1, 1]
    assert repeat_nudge("grep", max(counts)) is None


def test_signature_distinguishes_tool_and_args() -> None:
    assert call_signature("grep", {"q": "x"}) != call_signature("search", {"q": "x"})
    assert call_signature("bash", {"command": "a"}) != call_signature("bash", {"command": "b"})
    assert call_signature("bash", {"command": "a"}) == call_signature("bash", {"command": "a"})


def test_signature_count_is_bounded() -> None:
    tracker = SessionLoopTracker()
    for i in range(700):
        tracker.record("bash", {"command": f"cmd-{i}"})
    # The hottest signature still trips after eviction of cold ones.
    for _ in range(4):
        count = tracker.record("bash", {"command": "hot"})
    assert repeat_nudge("bash", count) is not None
    assert len(tracker._counts) <= 512  # bound assertion
