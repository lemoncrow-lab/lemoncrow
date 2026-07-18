"""Verify-before-done: shared core + Codex/OpenCode host adapters.

The Claude adapter (transcript scanning) is covered by
test_verify_before_done_hook.py; these exercise the host-agnostic
``verify_gate`` core and the Codex/OpenCode builders that feed it off the run
ledger. Direct function calls -- fast, no subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime as pr
from lemoncrow.pro.capabilities import verify_gate
from lemoncrow.pro.capabilities.verify_gate import VerifySignals


@pytest.fixture(autouse=True)
def _clean_verify_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate fire-once state under tmp and pin toggles to defaults so ambient
    # harness env (bench mode, opt-outs) can't skew the decision.
    monkeypatch.setenv("TMPDIR", str(tmp_path / "vstate"))
    monkeypatch.delenv("LEMONCROW_VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("LEMONCROW_VERIFY_COMPLETENESS", raising=False)
    monkeypatch.delenv("LEMONCROW_VERIFY_SKIP_SUFFIXES", raising=False)
    monkeypatch.delenv("LEMONCROW_BENCH_MODE", raising=False)


def _seed_ledger(root: Path, ws: Path, sid: str, events: list[dict], *, prompt: str = "") -> dict:
    """Write the workspace session-state bridge + run ledger both hosts read."""
    payload = {"session_id": sid, "cwd": str(ws)}
    ss = pr._opencode_session_state_path(root, payload)
    ss.parent.mkdir(parents=True, exist_ok=True)
    state = {"session_id": sid, "host": "opencode"}
    if prompt:
        state["last_user_prompt"] = prompt
    ss.write_text(json.dumps(state), encoding="utf-8")
    rf = pr._codex_run_file(root, sid)
    rf.parent.mkdir(parents=True, exist_ok=True)
    rf.write_text(json.dumps({"session_id": sid, "events": events, "files_touched": []}), encoding="utf-8")
    return payload


def _edit(path: str, diff: str = "-x\n+y") -> dict:
    return {"kind": "file_edit", "summary": f"edit {path}", "payload": {"path": path, "event": "edit", "diff": diff}}


def _cmd_mcp(command: str, ok: bool = True) -> dict:
    # MCP-server shape: command text lives in the event summary, not payload.
    return {"kind": "command_result", "summary": command, "payload": {"ok": ok}}


def _cmd_hook(command: str, ok: bool = True) -> dict:
    # PostToolUse-hook shape: command text in payload.command.
    return {"kind": "command_result", "summary": "✓ x", "payload": {"command": command, "ok": ok}}


# --- shared core -----------------------------------------------------------
def test_decide_blocks_on_unverified_source_edit() -> None:
    result = verify_gate.decide(VerifySignals(edited=["app/core.py"]))
    assert result == {"decision": "block", "reason": "FIXME (verify): edited core.py, run test/verification."}


def test_decide_silent_when_tests_ran() -> None:
    assert verify_gate.decide(VerifySignals(edited=["app/core.py"], verified=True)) is None


def test_decide_silent_for_exercised_data_deliverable() -> None:
    # A data artifact (no test suite) that was exercised by a command clears the bar.
    assert verify_gate.decide(VerifySignals(edited=["out/report.csv"], checked=True)) is None


def test_decide_still_blocks_code_even_when_a_snippet_ran() -> None:
    # `checked` only clears non-code deliverables; a code edit keeps the test bar.
    assert verify_gate.decide(VerifySignals(edited=["app/core.py"], checked=True)) is not None


def test_decide_fires_once_per_dedup_key() -> None:
    sig = VerifySignals(edited=["app/core.py"])
    assert verify_gate.decide(sig, dedup_key="k1") is not None
    assert verify_gate.decide(sig, dedup_key="k1") is None  # same nudge, already shown


def test_disabled_env_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_VERIFY_BEFORE_DONE", "0")
    assert verify_gate.disabled() is True


# --- Codex adapter (systemMessage nudge -- Codex Stop rejects unsupported decisions) ---
def test_codex_verify_nudges_on_unverified_edit(tmp_path: Path) -> None:
    # Edit with NO post-edit run at all: the only shape that still nudges (any
    # post-edit run -- even a snippet -- now counts as verification).
    root, ws = tmp_path / ".lc", tmp_path / "proj"
    ws.mkdir()
    payload = _seed_ledger(root, ws, "cx", [_cmd_mcp("ls -la"), _edit(str(ws / "mod.py"))])
    out = pr.build_codex_verify_output(root, {**payload, "hook_event_name": "Stop"})
    assert out["systemMessage"].startswith("FIXME (verify): edited mod.py")


def test_codex_verify_silent_after_snippet_run(tmp_path: Path) -> None:
    # Parity with the Claude hook: a post-edit custom-script run verifies.
    root, ws = tmp_path / ".lc", tmp_path / "proj"
    ws.mkdir()
    payload = _seed_ledger(root, ws, "cx", [_edit(str(ws / "mod.py")), _cmd_mcp("python repro.py")])
    out = pr.build_codex_verify_output(root, {**payload, "hook_event_name": "Stop"})
    assert out.get("no_output") is True


def test_codex_verify_silent_after_pytest(tmp_path: Path) -> None:
    root, ws = tmp_path / ".lc", tmp_path / "proj"
    ws.mkdir()
    payload = _seed_ledger(root, ws, "cx", [_edit(str(ws / "mod.py")), _cmd_hook("pytest -q")])
    out = pr.build_codex_verify_output(root, {**payload, "hook_event_name": "Stop"})
    assert out.get("no_output") is True


def test_codex_verify_silent_without_ledger(tmp_path: Path) -> None:
    root, ws = tmp_path / ".lc", tmp_path / "proj"
    ws.mkdir()
    out = pr.build_codex_verify_output(root, {"session_id": "nope", "cwd": str(ws), "hook_event_name": "Stop"})
    assert out.get("no_output") is True


# --- OpenCode adapter (block emulated via continuePrompt) ------------------
def test_opencode_verify_blocks_on_unverified_edit(tmp_path: Path) -> None:
    root, ws = tmp_path / ".lc", tmp_path / "proj"
    ws.mkdir()
    payload = _seed_ledger(root, ws, "oc", [_edit(str(ws / "mod.py"))])
    out = pr.build_opencode_verify_output(root, payload)
    assert out["decision"] == "block"
    assert out["reason"].startswith("FIXME (verify): edited mod.py")


def test_opencode_idle_emits_continue_prompt(tmp_path: Path) -> None:
    root, ws = tmp_path / ".lc", tmp_path / "proj"
    ws.mkdir()
    payload = _seed_ledger(root, ws, "oc", [_edit(str(ws / "mod.py"))])
    out = pr.build_opencode_stop_output(root, {**payload, "usage": {"input_tokens": 10, "output_tokens": 5}})
    assert out["continuePrompt"].startswith("FIXME (verify): edited mod.py")


def test_opencode_user_prompt_captures_issue_text(tmp_path: Path) -> None:
    root, ws = tmp_path / ".lc", tmp_path / "proj"
    ws.mkdir()
    payload = {"session_id": "oc", "cwd": str(ws), "prompt": "the real issue text"}
    pr.build_opencode_user_prompt_output(root, payload)
    state = json.loads(pr._opencode_session_state_path(root, payload).read_text())
    assert state["last_user_prompt"] == "the real issue text"


def test_ledger_reader_accepts_both_command_shapes(tmp_path: Path) -> None:
    root, ws = tmp_path / ".lc", tmp_path / "proj"
    ws.mkdir()
    _seed_ledger(root, ws, "s", [_edit(str(ws / "mod.py")), _cmd_mcp("pytest -q")])
    sig = pr._verify_signals_from_run_ledger(root, "s", "")
    assert sig.verified is True  # command text read from the MCP-shape summary
    _seed_ledger(root, ws, "s", [_edit(str(ws / "mod.py")), _cmd_hook("pytest -q")])
    assert pr._verify_signals_from_run_ledger(root, "s", "").verified is True
