"""Tests for the host-Bash output-shrink PostToolUse hook.

Mirrors tests/integrations/test_mcp_output_shrink_hook.py: the hook is a
standalone script reading a JSON payload on stdin and printing an optional
JSON decision on stdout, exercised as a subprocess with crafted payloads and
per-test tmp_path isolation for LemonCrow state and the spill store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"
HOOK = HOOKS / "bash_output_shrink.py"


def _run(
    payload: dict, tmp_path: Path, env_extra: dict | None = None, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LEMONCROW_ROOT": str(tmp_path / ".lemoncrow"),
        "LEMONCROW_MCP_SPILL_DIR": str(tmp_path / "spill"),
        "LEMONCROW_TOOL_OUTPUT_SPILL": "0",
        **(env_extra or {}),
    }
    stdin = stdin_text if stdin_text is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _bash_payload(command: str, stdout: str, stderr: str = "", return_code: int | None = 0) -> dict:
    tool_response: dict = {"stdout": stdout, "stderr": stderr}
    if return_code is not None:
        tool_response["returnCode"] = return_code
    return {"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": tool_response}


def _updated(proc: subprocess.CompletedProcess[str]) -> dict:
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    return out["hookSpecificOutput"]["updatedToolOutput"]


def test_small_output_passes_through_untouched(tmp_path: Path) -> None:
    proc = _run(_bash_payload("ls -la", "a short listing"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_large_generic_output_is_shrunk(tmp_path: Path) -> None:
    stdout = "\n".join(f"line {i:04d} of some long build log output" for i in range(3000))
    proc = _run(_bash_payload("bash build.sh", stdout), tmp_path)
    assert proc.returncode == 0, proc.stderr
    updated = _updated(proc)
    assert "[lemon: " in updated["stdout"]
    assert len(updated["stdout"]) < len(stdout)
    assert "omitted" in updated["stdout"]
    assert updated["returnCode"] == 0  # untouched fields pass through


def test_green_pytest_output_collapses_to_summary(tmp_path: Path) -> None:
    stdout = "\n".join(f"tests/test_mod_{i}.py::test_case_{i} PASSED" for i in range(80))
    stdout += "\n250 passed in 1.2s"
    proc = _run(_bash_payload("uv run pytest tests/", stdout), tmp_path)
    assert proc.returncode == 0, proc.stderr
    updated = _updated(proc)
    assert "250 passed in 1.2s" in updated["stdout"]
    assert "test_case_5 PASSED" not in updated["stdout"]


def test_successful_install_collapses_to_ok_line(tmp_path: Path) -> None:
    stdout = "\n".join(f"added package number-{i} in 0.{i:02d}s" for i in range(100))
    stdout += "\nInstalled 100 packages in 4.2s"
    proc = _run(_bash_payload("npm install", stdout, return_code=0), tmp_path)
    assert proc.returncode == 0, proc.stderr
    updated = _updated(proc)
    assert "ok: Installed 100 packages in 4.2s" in updated["stdout"]
    assert "suppressed on success" in updated["stdout"]


def test_unknown_exit_code_never_suppresses_a_mutator(tmp_path: Path) -> None:
    """Without a provable exit 0 the install output must NOT collapse to ok:."""
    stdout = "\n".join(f"added package number-{i} in 0.{i:02d}s" for i in range(100))
    stdout += "\nInstalled 100 packages in 4.2s"
    proc = _run(_bash_payload("npm install", stdout, return_code=None), tmp_path)
    assert proc.returncode == 0, proc.stderr
    if proc.stdout:  # if anything is emitted it must not be the ok: collapse
        assert "ok: Installed" not in _updated(proc)["stdout"]


def test_repeated_lines_are_deduped_with_count(tmp_path: Path) -> None:
    stdout = "\n".join(["Retrying connection to registry endpoint dot example"] * 60)
    proc = _run(_bash_payload("bash poll.sh", stdout), tmp_path)
    assert proc.returncode == 0, proc.stderr
    updated = _updated(proc)
    assert "(line repeated 60 times)" in updated["stdout"]


def test_failure_keeps_anomaly_window(tmp_path: Path) -> None:
    lines = [f"step {i:03d} completed normally with routine output" for i in range(300)]
    lines[150] = "ERROR: widget assembly could not be linked"
    proc = _run(_bash_payload("bash build.sh", "\n".join(lines), return_code=1), tmp_path)
    assert proc.returncode == 0, proc.stderr
    updated = _updated(proc)
    assert "ERROR: widget assembly could not be linked" in updated["stdout"]


def test_spill_hint_names_recovery_path(tmp_path: Path) -> None:
    stdout = "\n".join(f"line {i:04d} of some long build log output" for i in range(3000))
    proc = _run(
        _bash_payload("bash build.sh", stdout),
        tmp_path,
        env_extra={"LEMONCROW_TOOL_OUTPUT_SPILL": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    updated = _updated(proc)
    assert "[lemon: shrunk" in updated["stdout"]
    assert "read " in updated["stdout"]


def test_kill_switch_disables_hook(tmp_path: Path) -> None:
    stdout = "\n".join(f"line {i:04d} of some long build log output" for i in range(3000))
    proc = _run(_bash_payload("bash build.sh", stdout), tmp_path, env_extra={"LEMONCROW_HOST_BASH_SHRINK": "0"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_non_bash_tool_is_skipped(tmp_path: Path) -> None:
    payload = {"tool_name": "Edit", "tool_input": {"command": "x"}, "tool_response": {"stdout": "X" * 50_000}}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_malformed_stdin_exits_zero_with_no_output(tmp_path: Path) -> None:
    proc = _run({}, tmp_path, stdin_text="not json at all {{{")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_string_tool_response_fails_open(tmp_path: Path) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "X" * 50_000}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
