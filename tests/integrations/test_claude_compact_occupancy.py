"""Regression: a compaction boundary invalidates old Claude usage for live context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations.claude.plugin.hooks import compact, session_start, user_prompt
from lemoncrow.core.foundation.claude_window_state import mutate_claude_window_state, read_claude_window_state


def _append(transcript: Path, entry: dict) -> None:
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _usage(tokens: int, model: str = "claude-sonnet-4-5") -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


_BOUNDARY = {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "manual"}}


@pytest.mark.parametrize("occupancy_fn", [user_prompt._context_occupancy, compact._context_occupancy])
def test_compact_boundary_invalidates_old_usage_until_fresh_turn(tmp_path: Path, occupancy_fn) -> None:
    transcript = tmp_path / "transcript.jsonl"
    _append(transcript, _usage(380_000))
    assert occupancy_fn(str(transcript)) == (380_000, "claude-sonnet-4-5")
    _append(transcript, _BOUNDARY)
    # Claude injects a summary and command-status messages before any new API
    # response. Old turns remain in the transcript but cannot be live context.
    _append(transcript, {"type": "user", "message": {"role": "user", "content": "Compacted"}})
    assert occupancy_fn(str(transcript)) == (0, None)
    _append(transcript, _usage(28_000))
    assert occupancy_fn(str(transcript)) == (28_000, "claude-sonnet-4-5")
    _append(transcript, _BOUNDARY)
    assert occupancy_fn(str(transcript)) == (0, None)
    _append(transcript, _usage(33_000))
    assert occupancy_fn(str(transcript)) == (33_000, "claude-sonnet-4-5")


def test_postcompact_resets_advice_state_without_losing_pending_savings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "store"))
    before = {
        "prompt_topic_history": ["long history"],
        "last_compact_notice_count": 40,
        "last_occupancy": 380_000,
        "drift_streak": 4,
        "precompact_pending": True,
        "precompact_occupancy": 380_000,
        "precompact_model": "claude-sonnet-4-5",
    }
    mutate_claude_window_state(tmp_path, "abc", lambda state: state.update(before))
    monkeypatch.setattr(compact, "_append_compact_event", lambda *args: None)
    monkeypatch.setattr(compact, "_read_compact_manifest", lambda sid: None)
    monkeypatch.setattr(compact, "_restore_optimizer_notices_postcompact", lambda *args: None)

    compact._handle_post_compact("abc", "manual")
    state = read_claude_window_state(tmp_path, "abc")
    assert state["compaction_epoch"] == 1
    assert state["prompt_topic_history"] == []
    for key in ("last_compact_notice_count", "last_occupancy", "drift_streak"):
        assert key not in state
    assert state["precompact_pending"] is True
    assert state["precompact_occupancy"] == 380_000


def test_prompt_does_not_nudge_again_on_old_precompact_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "store"))
    transcript = tmp_path / "transcript.jsonl"
    _append(transcript, _usage(380_000))
    mutate_claude_window_state(
        tmp_path,
        "abc",
        lambda state: state.update(
            precompact_pending=True,
            precompact_occupancy=380_000,
            prompt_topic_history=["old context"],
            last_occupancy=380_000,
        ),
    )
    _append(transcript, _BOUNDARY)

    # Even if a stale pending flag has already cleared, no precompact usage
    # can trigger a new warning after the explicit compact boundary.
    for i in range(4):
        assert (
            user_prompt._maybe_emit_compaction_advice(
                f"compaction injected prompt {i}", str(transcript), "continue", "abc"
            )
            is None
        )
    _append(transcript, _usage(30_000))
    assert (
        user_prompt._maybe_emit_compaction_advice("real user followup", str(transcript), "real user followup", "abc")
        is None
    )
    state = read_claude_window_state(tmp_path, "abc")
    assert not state.get("precompact_pending")
    assert state["last_occupancy"] == 30_000


def test_sessionstart_compact_fallback_resets_old_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "store"))
    mutate_claude_window_state(
        tmp_path,
        "abc",
        lambda state: state.update(
            prompt_topic_history=["old context"],
            last_occupancy=380_000,
            last_compact_notice_count=9,
            drift_streak=4,
        ),
    )
    # A direct state update corresponds to the SessionStart(compact) path;
    # test the observable reset with the real hook main.
    import io

    monkeypatch.setattr(
        session_start.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "abc",
                    "source": "compact",
                    "cwd": str(tmp_path),
                }
            )
        ),
    )
    assert session_start.main() == 0
    state = read_claude_window_state(tmp_path, "abc")
    assert state["prompt_topic_history"] == []
    assert "last_occupancy" not in state
    assert "last_compact_notice_count" not in state
    assert "drift_streak" not in state
