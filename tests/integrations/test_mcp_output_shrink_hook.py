"""Tests for the MCP output-shrink shadow-mode PostToolUse hook.

The hook is a standalone script that reads a JSON payload on stdin and prints
an optional JSON decision on stdout, so it is exercised as a subprocess with
crafted payloads -- isolating both LemonCrow state and the spill directory under
per-test tmp_path locations (matching tests/integrations/test_loop_discipline_hooks.py).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"
HOOK = HOOKS / "mcp_output_shrink.py"


def _run(
    payload: dict, tmp_path: Path, env_extra: dict | None = None, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LEMONCROW_ROOT": str(tmp_path / ".lemoncrow"),
        "LEMONCROW_MCP_SPILL_DIR": str(tmp_path / "spill"),
        **(env_extra or {}),
    }
    stdin = stdin_text if stdin_text is not None else json.dumps(payload)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return proc


def _spill_path_from_notice(text: str) -> Path:
    match = re.search(r"full: (\S+\.txt)\]", text)
    assert match is not None, text
    return Path(match.group(1))


def test_small_output_passes_through_untouched(tmp_path: Path) -> None:
    payload = {
        "tool_name": "mcp__someserver__sometool",
        "tool_response": "a small result",
    }
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_large_string_tool_response_is_shrunk_and_spilled(tmp_path: Path) -> None:
    big = "X" * 300_000
    payload = {"tool_name": "mcp__someserver__bigtool", "tool_response": big}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "expected updatedToolOutput on stdout"

    out = json.loads(proc.stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "[lc: shrunk" in updated
    assert len(updated) < len(big)

    spill_path = _spill_path_from_notice(updated)
    assert spill_path.read_text(encoding="utf-8") == big


def test_content_blocks_shape_tool_response_is_handled(tmp_path: Path) -> None:
    big = "Y" * 300_000
    payload = {
        "tool_name": "mcp__otherserver__searchtool",
        "tool_response": {"content": [{"type": "text", "text": big}]},
    }
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    spill_path = _spill_path_from_notice(updated)
    assert spill_path.read_text(encoding="utf-8") == big


def test_content_blocks_list_shape_tool_response_is_handled(tmp_path: Path) -> None:
    """tool_response may be a bare list of content blocks (no wrapping dict)."""
    big = "Z" * 300_000
    payload = {
        "tool_name": "mcp__otherserver__searchtool",
        "tool_response": [{"type": "text", "text": big}],
    }
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    spill_path = _spill_path_from_notice(updated)
    assert spill_path.read_text(encoding="utf-8") == big


def test_lemoncrow_bare_tool_name_is_skipped(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__lc__bash", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_lemoncrow_plugin_namespaced_tool_name_is_skipped(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__plugin_lemoncrow_lc__read", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_non_mcp_tool_name_is_skipped(tmp_path: Path) -> None:
    """Non-MCP tools (Bash, Edit, ...) are never this hook's concern."""
    payload = {"tool_name": "Bash", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_kill_switch_env_skips_even_large_non_lemoncrow_output(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__someserver__bigtool", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path, env_extra={"LEMONCROW_SHADOW_SHRINK": "0"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_threshold_env_override_shrinks_below_default_threshold(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__someserver__smallish", "tool_response": "X" * 5000}
    proc = _run(payload, tmp_path, env_extra={"LEMONCROW_SHADOW_SHRINK_CHARS": "1000"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()
    out = json.loads(proc.stdout)
    assert "[lc: shrunk" in out["hookSpecificOutput"]["updatedToolOutput"]


def test_threshold_env_zero_disables_shrinking(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__someserver__bigtool", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path, env_extra={"LEMONCROW_SHADOW_SHRINK_CHARS": "0"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_malformed_stdin_exits_zero_with_no_output(tmp_path: Path) -> None:
    proc = _run({}, tmp_path, stdin_text="not json at all {{{")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_empty_stdin_exits_zero_with_no_output(tmp_path: Path) -> None:
    proc = _run({}, tmp_path, stdin_text="")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_unrecognized_tool_response_shape_fails_open(tmp_path: Path) -> None:
    """A dict tool_response with neither 'content' nor 'text' is left untouched."""
    payload = {"tool_name": "mcp__someserver__weird", "tool_response": {"foo": "bar" * 100_000}}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
