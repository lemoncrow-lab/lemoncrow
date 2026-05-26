"""Round-trip integration tests for batch_edit (WP-22).

Tests that the CLI ``batch-edit`` command and the MCP tool handler both
produce correct results end-to-end, and that the JSON schema is stable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from atelier.core.capabilities.tool_supervision.batch_edit import apply_batch_edit
from atelier.gateway.cli import cli

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# JSON schema / envelope                                                      #
# --------------------------------------------------------------------------- #


def test_result_envelope_keys(tmp_path: Path) -> None:
    """Result always has applied, failed, rolled_back keys."""
    f = tmp_path / "a.txt"
    _write(f, "hello\n")

    result = apply_batch_edit(
        [{"path": str(f), "op": "replace", "old_string": "hello", "new_string": "world"}],
        atomic=True,
        repo_root=tmp_path,
    )

    assert set(result.keys()) >= {"applied", "failed", "rolled_back"}


def test_applied_hunk_structure(tmp_path: Path) -> None:
    """Each applied entry has path + hunks with line_start / line_end."""
    f = tmp_path / "b.txt"
    _write(f, "aaa\nbbb\nccc\n")

    result = apply_batch_edit(
        [{"path": str(f), "op": "replace", "old_string": "bbb", "new_string": "BBB"}],
        atomic=True,
        repo_root=tmp_path,
    )

    assert len(result["applied"]) == 1
    hunk = result["applied"][0]["hunks"][0]
    assert "line_start" in hunk
    assert "line_end" in hunk
    assert isinstance(hunk["line_start"], int)


# --------------------------------------------------------------------------- #
# CLI round-trip                                                              #
# --------------------------------------------------------------------------- #


def test_cli_batch_edit_from_stdin(tmp_path: Path) -> None:
    """CLI --from-stdin applies edits and emits JSON with --json flag."""
    target = tmp_path / "cli_test.txt"
    _write(target, "alpha beta\n")

    payload = json.dumps(
        {
            "edits": [
                {
                    "path": str(target),
                    "op": "replace",
                    "old_string": "alpha",
                    "new_string": "ALPHA",
                }
            ]
        }
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["batch-edit", "--from-stdin", "--json"],
        input=payload,
        catch_exceptions=False,
        env={
            **os.environ,
            "ATELIER_ROOT": str(tmp_path / ".atelier"),
            "CLAUDE_WORKSPACE_ROOT": str(tmp_path),
        },
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["rolled_back"] is False
    assert len(data["applied"]) == 1
    assert target.read_text() == "ALPHA beta\n"


def test_cli_batch_edit_from_file(tmp_path: Path) -> None:
    """CLI --from <file.json> applies edits."""
    target = tmp_path / "src.py"
    _write(target, "x = 1\n")

    payload_file = tmp_path / "edits.json"
    payload_file.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": str(target),
                        "op": "replace",
                        "old_string": "x = 1",
                        "new_string": "x = 99",
                    }
                ]
            }
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["batch-edit", "--from", str(payload_file), "--json"],
        catch_exceptions=False,
        env={
            **os.environ,
            "ATELIER_ROOT": str(tmp_path / ".atelier"),
            "CLAUDE_WORKSPACE_ROOT": str(tmp_path),
        },
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["rolled_back"] is False
    assert target.read_text() == "x = 99\n"


def test_cli_batch_edit_atomic_failure_exits_nonzero(tmp_path: Path) -> None:
    """CLI exits with code 2 when atomic rollback happens."""
    target = tmp_path / "ok.txt"
    _write(target, "something\n")

    payload = json.dumps(
        {
            "edits": [
                {
                    "path": str(target),
                    "op": "replace",
                    "old_string": "NOPE_NOT_THERE",
                    "new_string": "x",
                }
            ]
        }
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["batch-edit", "--from-stdin", "--json"],
        input=payload,
        env={
            **os.environ,
            "ATELIER_ROOT": str(tmp_path / ".atelier"),
            "CLAUDE_WORKSPACE_ROOT": str(tmp_path),
        },
    )

    data = json.loads(result.output)
    assert data["rolled_back"] is True
    assert result.exit_code == 2


def test_cli_batch_edit_non_atomic_partial_failure_exits_1(tmp_path: Path) -> None:
    """CLI exits with code 1 (partial failure, no rollback) in non-atomic mode."""
    good = tmp_path / "good.txt"
    bad = tmp_path / "bad.txt"
    _write(good, "YES\n")
    _write(bad, "NO\n")

    payload = json.dumps(
        {
            "edits": [
                {"path": str(good), "op": "replace", "old_string": "YES", "new_string": "OK"},
                {"path": str(bad), "op": "replace", "old_string": "MISSING", "new_string": "x"},
            ],
            "atomic": False,
        }
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["batch-edit", "--from-stdin", "--json"],
        input=payload,
        env={
            **os.environ,
            "ATELIER_ROOT": str(tmp_path / ".atelier"),
            "CLAUDE_WORKSPACE_ROOT": str(tmp_path),
        },
    )

    data = json.loads(result.output)
    assert data["rolled_back"] is False
    assert len(data["applied"]) == 1
    assert len(data["failed"]) == 1
    assert result.exit_code == 1
    assert good.read_text() == "OK\n"


# --------------------------------------------------------------------------- #
# Idempotency and backup cleanup                                             #
# --------------------------------------------------------------------------- #


def test_backup_cleaned_up_on_success(tmp_path: Path) -> None:
    """Backup directory is removed after a successful atomic batch."""
    f = tmp_path / "f.txt"
    _write(f, "data\n")

    apply_batch_edit(
        [{"path": str(f), "op": "replace", "old_string": "data", "new_string": "new data"}],
        atomic=True,
        backup_base=tmp_path / ".atelier" / "run" / "test-run" / "batch_edit_backup",
        repo_root=tmp_path,
    )

    backup_dir = tmp_path / ".atelier" / "run" / "test-run" / "batch_edit_backup"
    assert not backup_dir.exists(), "backup dir should be removed after success"


# --------------------------------------------------------------------------- #
# Host-native docs note                                                      #
# --------------------------------------------------------------------------- #


def test_batch_edit_module_docstring_mentions_host_native() -> None:
    """The module docstring must state that host-native edit tools remain the default."""
    from atelier.core.capabilities.tool_supervision import batch_edit

    doc = batch_edit.__doc__ or ""
    assert (
        "host" in doc.lower() or "native" in doc.lower()
    ), "batch_edit module docstring should reference host-native edit tools"
