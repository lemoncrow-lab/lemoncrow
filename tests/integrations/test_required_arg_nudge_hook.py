"""Tests for the required-argument nudge folded into PostToolUseFailure.

Mirrors tests/integrations/test_bash_output_shrink_hook.py: the hook is a
standalone script reading a JSON payload on stdin and printing an optional
JSON decision on stdout, exercised as a subprocess with crafted payloads and
per-test tmp_path isolation for LemonCrow state. Covers the first-occurrence
required-argument branch specifically; the pre-existing repeat-failure
behavior in the same file is unchanged by it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"
HOOK = HOOKS / "post_tool_use_failure.py"


def _run(
    payload: dict, tmp_path: Path, env_extra: dict | None = None, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CLAUDE_WORKSPACE_ROOT": str(tmp_path),
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


def _failure_payload(stderr: str, command: str = "python3 run.py") -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": {"stderr": stderr}}


_ENABLED = {"LEMONCROW_REQUIRED_ARG_NUDGE": "1"}
_MISSING_KWARG = "TypeError: SentenceTransformerWrapper.encode() missing 1 required keyword-only argument: 'task_name'"
_MISSING_POSITIONAL = "TypeError: foo() missing 2 required positional arguments: 'a' and 'b'"


def test_disabled_by_default_even_on_matching_error(tmp_path: Path) -> None:
    proc = _run(_failure_payload(_MISSING_KWARG), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_enabled_fires_on_missing_keyword_argument(tmp_path: Path) -> None:
    proc = _run(_failure_payload(_MISSING_KWARG), tmp_path, env_extra=_ENABLED)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "ask"
    assert "read what it controls" in out["reason"].lower()


def test_enabled_fires_on_missing_positional_argument(tmp_path: Path) -> None:
    proc = _run(_failure_payload(_MISSING_POSITIONAL), tmp_path, env_extra=_ENABLED)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "ask"


def test_enabled_stays_silent_on_unrelated_error(tmp_path: Path) -> None:
    proc = _run(
        _failure_payload("TypeError: unsupported operand type(s) for +: 'int' and 'str'"),
        tmp_path,
        env_extra=_ENABLED,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_enabled_does_not_crash_on_string_tool_response(tmp_path: Path) -> None:
    """Malformed (non-dict) tool_response must not crash the nudge branch --
    it falls through to the pre-existing repeat-count logic untouched."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": _MISSING_KWARG}
    proc = _run(payload, tmp_path, env_extra=_ENABLED)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_malformed_stdin_exits_zero_with_no_output(tmp_path: Path) -> None:
    proc = _run({}, tmp_path, env_extra=_ENABLED, stdin_text="not json at all {{{")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_off_variants_all_stay_disabled(tmp_path: Path) -> None:
    for i, off_value in enumerate(("0", "false", "", "no")):
        # Fresh workspace per iteration: reusing one tmp_path across 4 calls
        # with the identical command+error would itself trip the pre-existing
        # repeat-failure threshold (3rd identical failure) -- a false failure
        # unrelated to whether THIS nudge is on or off.
        iter_path = tmp_path / f"iter{i}"
        iter_path.mkdir()
        proc = _run(_failure_payload(_MISSING_KWARG), iter_path, env_extra={"LEMONCROW_REQUIRED_ARG_NUDGE": off_value})
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "", f"unexpectedly fired for LEMONCROW_REQUIRED_ARG_NUDGE={off_value!r}"


def test_repeat_failure_logic_still_works_unaffected(tmp_path: Path) -> None:
    """Pre-existing 3rd-identical-failure behavior must survive the merge,
    with the nudge left disabled (default)."""
    payload = _failure_payload("boom: same error every time", command="run_flaky_thing")
    payload["session_id"] = ""
    last = None
    for _ in range(3):
        last = _run(payload, tmp_path)
        assert last.returncode == 0, last.stderr
    assert last is not None
    out = json.loads(last.stdout)
    assert out["decision"] == "ask"
    assert "failed 3 times" in out["reason"]
