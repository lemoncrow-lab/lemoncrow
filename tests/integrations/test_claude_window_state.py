"""Concurrent Claude windows never share mutable hook warning/compact state."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from integrations.claude.plugin.hooks import compact, user_prompt
from lemoncrow.core.foundation import claude_window_state as ws
from lemoncrow.pro.capabilities.context_dedup import current_epoch


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "store"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(ws, "host_window_id", lambda: None)
    return tmp_path


def test_warning_counters_are_independent_per_session(isolated: Path) -> None:
    transcript = isolated / "usage.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-5",
                    "usage": {"input_tokens": 150_000},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    first = user_prompt._maybe_emit_compaction_advice("work A", str(transcript), "work A", "session-A")
    second = user_prompt._maybe_emit_compaction_advice("work B", str(transcript), "work B", "session-B")
    assert first is not None and "/compact" in first
    assert second is not None and "/compact" in second
    assert user_prompt._maybe_emit_compaction_advice("work A", str(transcript), "work A", "session-A") is None
    assert ws.read_claude_window_state(isolated, "session-A")["prompt_count"] == 2
    assert ws.read_claude_window_state(isolated, "session-B")["prompt_count"] == 1


def test_window_launch_identity_survives_changed_session_id(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "launch-sid")
    ws.mutate_claude_window_state(
        isolated,
        "before-compact",
        lambda state: state.update(precompact_pending=True, precompact_occupancy=380_000),
    )
    assert ws.read_claude_window_state(isolated, "after-compact")["precompact_pending"] is True

    monkeypatch.setattr(compact, "_append_compact_event", lambda *args: None)
    monkeypatch.setattr(compact, "_read_compact_manifest", lambda sid: None)
    monkeypatch.setattr(compact, "_restore_optimizer_notices_postcompact", lambda *args: None)
    compact._handle_post_compact("after-compact", "manual")
    assert current_epoch() == 1
    assert ws.read_claude_window_state(isolated, "before-compact")["precompact_pending"] is True


def test_window_updates_are_cross_thread_atomic(isolated: Path) -> None:
    def increment(_n: int) -> None:
        ws.mutate_claude_window_state(
            isolated,
            "sid",
            lambda state: state.update(counter=int(state.get("counter", 0)) + 1),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, range(64)))
    assert ws.read_claude_window_state(isolated, "sid")["counter"] == 64
