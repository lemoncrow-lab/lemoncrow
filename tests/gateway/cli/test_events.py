"""Tests for the CLI event protocol dataclasses."""

from __future__ import annotations

from typing import get_args

from atelier.gateway.cli.events import (
    AssistantDelta,
    AssistantMessage,
    AtelierEvent,
    MemoryHit,
    PatchProposed,
    PermissionRequested,
    RouteSelected,
    RuntimeErrorEvent,
    SessionStarted,
    ToolFinished,
    ToolOutput,
    ToolRequested,
    ToolStarted,
    VerificationResult,
)


def _all_events() -> list[AtelierEvent]:
    return [
        SessionStarted(type="session.started", session_id="s1", project_root="/tmp"),
        AssistantDelta(type="assistant.delta", text="hi"),
        AssistantMessage(type="assistant.message", text="hello"),
        RouteSelected(type="route.selected", provider="openai", model="gpt-4o", reason="r"),
        MemoryHit(type="memory.hit", key="k", summary="s", score=0.9),
        ToolRequested(type="tool.requested", id="t1", name="read", args={"path": "x"}),
        ToolStarted(type="tool.started", id="t1", name="read"),
        ToolOutput(type="tool.output", id="t1", chunk="out"),
        ToolFinished(type="tool.finished", id="t1", name="read", ok=True, result="ok"),
        PatchProposed(type="patch.proposed", id="p1", files=["a.py"], diff="- a\n+ b"),
        PermissionRequested(type="permission.requested", id="t1", action="shell: ls"),
        VerificationResult(type="verification.result", ok=True, rubric="r", details="d"),
        RuntimeErrorEvent(type="error", message="boom"),
    ]


def test_all_events_construct() -> None:
    events = _all_events()
    assert len(events) == 13


def test_union_accepts_each_event_type() -> None:
    union_types = get_args(AtelierEvent)
    for event in _all_events():
        assert isinstance(event, union_types)
