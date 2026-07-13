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


def test_rejected_edit_after_verified_change_does_not_reblock(tmp_path: Path) -> None:
    # A real edit gets verified by a passing test run. A LATER edit attempt
    # that the user rejects (tool_result is_error=True, nothing written) must
    # not re-poison last_edit_idx and re-block on every subsequent Stop even
    # though nothing on disk changed since the verified edit.
    t = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant_with_id("Bash", {"command": "pytest -q"}, "tu1"),
        _tool_result("tu1", False),
        _assistant_with_id("Edit", {"file_path": "app/other.py"}, "tu2"),
        _tool_result("tu2", True),
    )
    assert not _blocked(_run(t))


def test_repeated_stop_with_no_new_edit_does_not_reblock(tmp_path: Path) -> None:
    # Same unresolved edit, no new edit/verification since the last Stop -- the
    # second call is the exact same nudge again and must be suppressed.
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "app/core.py"})))
    assert _blocked(_run(t))
    assert not _blocked(_run(t))


def test_new_edit_after_suppressed_nudge_fires_again(tmp_path: Path) -> None:
    # A genuinely new edit event appended after a suppressed repeat must still
    # get its own one-time nudge -- suppression is per-nudge, not permanent.
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "app/core.py"})))
    assert _blocked(_run(t))
    assert not _blocked(_run(t))
    t.write_text(
        t.read_text(encoding="utf-8") + "\n" + json.dumps(_assistant(("Edit", {"file_path": "app/core.py"}))),
        encoding="utf-8",
    )
    assert _blocked(_run(t))


def test_docs_only_edit_allows(tmp_path: Path) -> None:
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "README.md"})))
    assert not _blocked(_run(t))


def test_text_deliverable_without_verification_blocks(tmp_path: Path) -> None:
    # A written data artifact (csv/txt/json/...) with no verification run is the
    # over-claim failure this hook must catch, not just source edits.
    t = _transcript(tmp_path, _assistant(("Write", {"file_path": "/app/result.csv"})))
    assert _blocked(_run(t))


def test_text_deliverable_with_pytest_allows(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path,
        _assistant(("Write", {"file_path": "/app/out.json"})),
        _assistant(("Bash", {"command": "python -m pytest -q"})),
    )
    assert not _blocked(_run(t))


def test_docs_deliverable_blocks_in_bench_mode(tmp_path: Path) -> None:
    # A .md deliverable is graded in a benchmark -> nag under bench mode, while
    # test_docs_only_edit_allows pins that ordinary (non-bench) docs edits do not.
    t = _transcript(tmp_path, _assistant(("Write", {"file_path": "/app/answer.md"})))
    assert _blocked(_run(t, env_extra={"LEMONCROW_BENCH_MODE": "on"}))


def test_binary_deliverable_does_not_block(tmp_path: Path) -> None:
    # Binary artifacts (.npy/.bin/...) are out of scope -- text/data deliverables only.
    t = _transcript(tmp_path, _assistant(("Write", {"file_path": "/app/stolen.npy"})))
    assert not _blocked(_run(t))


def test_text_deliverable_exercised_by_command_allows(tmp_path: Path) -> None:
    # Running a command that names the produced artifact IS the check for a
    # suite-less data task -- must not nag (closes the _TEST_RUN false-positive).
    t = _transcript(
        tmp_path,
        _assistant(("Write", {"file_path": "/app/result.csv"})),
        _assistant(("Bash", {"command": "python eval.py /app/result.csv"})),
    )
    assert not _blocked(_run(t))


def test_code_edit_run_by_name_still_blocks(tmp_path: Path) -> None:
    # Code keeps the strict test-runner bar -- running the file by name in a
    # snippet is not enough (misses regressions a withheld suite catches).
    t = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant(("Bash", {"command": "python app/core.py"})),
    )
    assert _blocked(_run(t))


def test_skip_suffixes_env_excludes_configured_types(tmp_path: Path) -> None:
    # LEMONCROW_VERIFY_SKIP_SUFFIXES lets the user keep archival docs / data dumps
    # out of the nudge -- overriding text and (bench) doc classification alike.
    csv = _transcript(tmp_path, _assistant(("Write", {"file_path": "/app/result.csv"})))
    assert not _blocked(_run(csv, env_extra={"LEMONCROW_VERIFY_SKIP_SUFFIXES": ".csv, md"}))
    md = _transcript(tmp_path, _assistant(("Write", {"file_path": "/app/answer.md"})))
    assert not _blocked(_run(md, env_extra={"LEMONCROW_BENCH_MODE": "on", "LEMONCROW_VERIFY_SKIP_SUFFIXES": "md"}))


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
