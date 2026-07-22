"""Tests for the required-argument nudge folded into PostToolUseFailure.

Mirrors tests/integrations/test_bash_output_shrink_hook.py: the hook is a
standalone script reading a JSON payload on stdin and printing an optional
JSON decision on stdout, exercised as a subprocess with crafted payloads and
per-test tmp_path isolation for LemonCrow state. Covers the required-argument
branch specifically -- counted by PATTERN match (LEMONCROW_REQUIRED_ARG_NUDGE_
THRESHOLD, default 3), since each guessed value produces a different exact
error and never trips the pre-existing exact-signature repeat-failure logic
in the same file (tested separately below, unaffected by this addition).
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


def _kwarg_error(guess: str) -> str:
    return f"TypeError: SentenceTransformerWrapper.encode() missing 1 required keyword-only argument: '{guess}'"


_THRESHOLD_1 = {"LEMONCROW_REQUIRED_ARG_NUDGE_THRESHOLD": "1"}
_MISSING_KWARG = _kwarg_error("task_name")
_MISSING_POSITIONAL = "TypeError: foo() missing 2 required positional arguments: 'a' and 'b'"


def test_default_threshold_needs_three_hits(tmp_path: Path) -> None:
    for guess in ("task_name", "other_name"):
        proc = _run(_failure_payload(_kwarg_error(guess)), tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "", f"fired early on guess={guess!r}"
    proc = _run(_failure_payload(_kwarg_error("third_name")), tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "ask"
    assert "read what it controls" in out["reason"].lower()


def test_threshold_1_fires_on_first_occurrence(tmp_path: Path) -> None:
    proc = _run(_failure_payload(_MISSING_KWARG), tmp_path, env_extra=_THRESHOLD_1)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "ask"
    assert "read what it controls" in out["reason"].lower()


def test_threshold_1_fires_on_missing_positional_argument(tmp_path: Path) -> None:
    proc = _run(_failure_payload(_MISSING_POSITIONAL), tmp_path, env_extra=_THRESHOLD_1)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["decision"] == "ask"


def test_stays_silent_on_unrelated_error(tmp_path: Path) -> None:
    proc = _run(
        _failure_payload("TypeError: unsupported operand type(s) for +: 'int' and 'str'"),
        tmp_path,
        env_extra=_THRESHOLD_1,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_does_not_crash_on_string_tool_response(tmp_path: Path) -> None:
    """Malformed (non-dict) tool_response must not crash the nudge branch --
    it falls through to the pre-existing repeat-count logic untouched."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": _MISSING_KWARG}
    proc = _run(payload, tmp_path, env_extra=_THRESHOLD_1)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_malformed_stdin_exits_zero_with_no_output(tmp_path: Path) -> None:
    proc = _run({}, tmp_path, env_extra=_THRESHOLD_1, stdin_text="not json at all {{{")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_unparseable_threshold_falls_back_to_default_three(tmp_path: Path) -> None:
    for i, bad_value in enumerate(("abc", "")):
        iter_path = tmp_path / f"iter{i}"
        iter_path.mkdir()
        proc = _run(
            _failure_payload(_MISSING_KWARG), iter_path, env_extra={"LEMONCROW_REQUIRED_ARG_NUDGE_THRESHOLD": bad_value}
        )
        assert proc.returncode == 0, proc.stderr
        # A single hit must never fire on an unparseable override -- falls
        # back to the default (3), not to threshold=1.
        assert proc.stdout == "", f"fired on first hit with unparseable threshold {bad_value!r}"


def test_non_positive_threshold_clamps_to_one(tmp_path: Path) -> None:
    for i, low_value in enumerate(("0", "-5")):
        iter_path = tmp_path / f"iter{i}"
        iter_path.mkdir()
        proc = _run(
            _failure_payload(_MISSING_KWARG), iter_path, env_extra={"LEMONCROW_REQUIRED_ARG_NUDGE_THRESHOLD": low_value}
        )
        assert proc.returncode == 0, proc.stderr
        # 0/negative parse fine as ints -- clamped to the minimum sensible
        # bound (1), so a single hit DOES fire, same as an explicit "1".
        assert proc.stdout != "", f"did not fire on first hit with threshold {low_value!r} (should clamp to 1)"
        out = json.loads(proc.stdout)
        assert out["decision"] == "ask"


def test_repeat_failure_logic_still_works_unaffected(tmp_path: Path) -> None:
    """Pre-existing 3rd-identical-failure behavior must survive the merge."""
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
