"""Tests for the ContextCompressor and the preserved-fields invariants."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.infra.runtime.context_compressor import ContextCompressor, HandoverPacket
from lemoncrow.infra.runtime.run_ledger import RunLedger


def test_compressor_preserves_latest_error_and_alerts() -> None:
    led = RunLedger(task="t")
    led.record_command("pytest", ok=False, error_signature="errA")
    led.record_command("pytest", ok=False, error_signature="errA")  # repeated
    led.record_command("ruff", ok=False, error_signature="errB")
    led.record_alert("repeated_command_failure", "high", "pytest x2")
    led.record_alert("noise", "low", "ignore me")

    state = ContextCompressor().compress(led)
    # Distinct error fingerprints captured (deduped)
    assert "errA" in state.error_fingerprints
    assert "errB" in state.error_fingerprints
    # Low severity alert dropped
    assert all("noise" not in m for m in state.high_severity_alerts)
    # High severity alert preserved
    assert any("repeated_command_failure" in m for m in state.high_severity_alerts)
    # Blocker reflects latest alert
    assert state.current_blocker is not None


def test_compressor_tracks_files_with_last_action() -> None:
    led = RunLedger(task="t")
    led.record_file_event("a.py", "edit")
    led.record_file_event("a.py", "revert")
    led.record_file_event("b.py", "edit")
    state = ContextCompressor().compress(led)
    assert state.files_changed["a.py"] == "revert"
    assert state.files_changed["b.py"] == "edit"


def test_compressor_prompt_block_renders() -> None:
    led = RunLedger(task="t")
    led.record_command("pytest", ok=False, error_signature="x")
    state = ContextCompressor().compress(led)
    text = state.to_prompt_block()
    assert "LemonCrow compact state" in text


def test_compressor_preserves_recent_turns_playbooks_and_claude_hash(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("project instructions", encoding="utf-8")
    led = RunLedger(task="t")
    led.active_playbooks = ["rb-a", "rb-b"]
    for idx in range(12):
        led.record("agent_message", f"turn {idx}", {"idx": idx})

    state = ContextCompressor().compress(led, preserve_last_n_turns=10, workspace_root=tmp_path)

    assert len(state.recent_turns) == 10
    assert "turn 2" in state.recent_turns[0]
    assert state.pinned_playbooks == ["rb-a", "rb-b"]
    assert state.claude_md_hash is not None


def test_handover_packet_renders_markdown(tmp_path: Path) -> None:
    led = RunLedger(session_id="s1", task="Ship feature")
    led.record_file_event("src/app.py", "edit", diff="--- a\n+++ b\n")
    led.record_command("pytest", ok=False, error_signature="boom")
    led.set_next_validation("Run pytest after fixing boom")
    state = ContextCompressor().compress(led, workspace_root=tmp_path)

    markdown = HandoverPacket.from_ledger(led, state, workspace_root=tmp_path).to_markdown()

    assert "## Session Handover - s1" in markdown
    assert "### Goal: Ship feature" in markdown
    assert "edit: src/app.py" in markdown
    assert "boom" in markdown
