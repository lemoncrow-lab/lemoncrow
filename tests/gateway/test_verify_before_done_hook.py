"""Tests for the verify-before-done Stop hook.

Invoke the hook as a subprocess (matching test_live_review_hook.py) with a
synthetic Claude Code transcript JSONL and assert on the block decision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path("integrations/claude/plugin/hooks/verify_before_done.py")


def _assistant(*tool_uses: tuple[str, dict]) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": n, "input": i} for n, i in tool_uses],
        },
    }


def _transcript(tmp_path: Path, *entries: dict) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return p


def _run(transcript: Path, *, stop_active: bool = False, env_extra: dict | None = None) -> str:
    payload = {
        "session_id": "s1",
        "transcript_path": str(transcript),
        "stop_hook_active": stop_active,
    }
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _blocked(out: str) -> bool:
    return bool(out) and json.loads(out).get("decision") == "block"


def test_edit_without_verification_blocks(tmp_path: Path) -> None:
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "app/core.py"})))
    assert _blocked(_run(t))


def test_edit_with_pytest_allows(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant(("Bash", {"command": "python -m pytest tests/test_core.py -q"})),
    )
    assert not _blocked(_run(t))


def test_edit_with_django_runtests_allows(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path,
        _assistant(("mcp__lc__edit", {"edits": [{"file_path": "django/db/x.py", "new_string": "..."}]})),
        _assistant(("mcp__lc__bash", {"command": "cd tests && python runtests.py dbshell"})),
    )
    assert not _blocked(_run(t))


def test_code_run_snippet_does_not_count_as_verification(tmp_path: Path) -> None:
    # A python -c / repro-script run is NOT a real test: it checks only what the
    # author thought of and misses regressions, so the gate still blocks.
    t = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant(("Bash", {"command": "python repro.py"})),
    )
    assert _blocked(_run(t))


def test_python_c_snippet_does_not_count_as_verification(tmp_path: Path) -> None:
    # The exact requests-2931 regression shape: a `python -c` repro that passed but
    # missed a broken neighbor. Must block to push the model onto the real suite.
    t = _transcript(
        tmp_path,
        _assistant(("mcp__lc__edit", {"edits": [{"file_path": "requests/models.py", "new_string": "..."}]})),
        _assistant(("mcp__lc__bash", {"command": 'python -c "import requests; print(requests.get)"'})),
    )
    assert _blocked(_run(t))


def _assistant_with_id(name: str, tool_input: dict, tool_use_id: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
        },
    }


def _tool_result(tool_use_id: str, is_error: bool) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error, "content": "..."}],
        },
    }


def test_test_run_before_edit_does_not_count(tmp_path: Path) -> None:
    # A pre-edit test run proves nothing about the change: still blocks.
    t = _transcript(
        tmp_path,
        _assistant(("Bash", {"command": "pytest -q"})),
        _assistant(("Edit", {"file_path": "app/core.py"})),
    )
    assert _blocked(_run(t))


def test_failed_test_run_does_not_count(tmp_path: Path) -> None:
    # A test run whose tool_result is_error=True is a failed verification.
    t = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant_with_id("Bash", {"command": "pytest -q"}, "tu1"),
        _tool_result("tu1", True),
    )
    assert _blocked(_run(t))


def test_passing_test_run_after_edit_allows(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant_with_id("Bash", {"command": "pytest -q"}, "tu1"),
        _tool_result("tu1", False),
    )
    assert not _blocked(_run(t))


def test_docs_only_edit_allows(tmp_path: Path) -> None:
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "README.md"})))
    assert not _blocked(_run(t))


def test_no_edits_allows(tmp_path: Path) -> None:
    t = _transcript(tmp_path, _assistant(("Bash", {"command": "ls -la && grep -r foo ."})))
    assert not _blocked(_run(t))


def test_lint_only_still_blocks(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant(("Bash", {"command": "ruff check . && mypy src && black --check ."})),
    )
    assert _blocked(_run(t))


def test_stop_hook_active_does_not_block(tmp_path: Path) -> None:
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "app/core.py"})))
    assert not _blocked(_run(t, stop_active=True))


def test_disabled_env_does_not_block(tmp_path: Path) -> None:
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "app/core.py"})))
    assert not _blocked(_run(t, env_extra={"LEMONCROW_VERIFY_BEFORE_DONE": "0"}))
