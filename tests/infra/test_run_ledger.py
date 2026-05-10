"""Tests for RunLedger V2 expansions and persistence round-trip."""

from __future__ import annotations

from pathlib import Path

from atelier.infra.runtime.run_ledger import RunLedger


def test_ledger_records_basic_fields(tmp_path: Path) -> None:
    led = RunLedger(
        agent="codex",
        task="Fix live state change",
        domain="state.change",
        root=tmp_path,
    )
    led.set_plan(["step 1", "step 2"])
    led.add_hypothesis("url slug is stable")
    led.add_hypothesis("display name changed", rejected=True)
    led.add_verified_fact("canonical identifier found")
    led.add_open_question("does readback match intent?")
    led.set_blocker("no canonical identifier resolver")
    led.set_next_validation("read_after_write_completed")
    led.record_command("pytest", ok=False, error_signature="abc123")
    led.record_command("pytest", ok=False, error_signature="abc123")
    led.record_tool_call("api.write", args=None, args_signature="sigA")
    led.record_test("test_foo", passed=True)
    led.record_file_event("src/a.py", "edit")
    led.record_alert("repeated_command_failure", "high", "pytest x2")

    snap = led.snapshot()
    assert snap["task"] == "Fix live state change"
    assert snap["current_plan"] == ["step 1", "step 2"]
    assert "abc123" in snap["errors_seen"]
    assert "api.write" in snap["tools_called"]
    assert "test_foo" in snap["tests_run"]
    assert snap["next_required_validation"] == "read_after_write_completed"
    # Latest blocker preserved (high-sev alert overrides previous)
    assert snap["current_blockers"]
    assert "repeated_command_failure" in snap["current_blockers"][0]


def test_ledger_persist_and_load_roundtrip(tmp_path: Path) -> None:
    led = RunLedger(agent="codex", task="t", domain="d", root=tmp_path)
    led.record_command("pytest", ok=False, error_signature="sig1")
    led.record_alert("monitor_x", "medium", "blah")
    path = led.persist()

    loaded = RunLedger.load(path)
    assert loaded.task == "t"
    assert loaded.domain == "d"
    assert "pytest" in loaded.commands_run
    assert "sig1" in loaded.errors_seen
    assert any(e.kind == "watchdog_alert" for e in loaded.events)


def test_ledger_close_marks_status(tmp_path: Path) -> None:
    led = RunLedger(root=tmp_path)
    assert led.status == "running"
    led.close("complete")
    assert led.status == "complete"
