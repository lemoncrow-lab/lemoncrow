from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli

# The marker text below is assembled at runtime (comment leader kept apart
# from the tag) so this test file is never harvested as real debt itself
# when someone runs `lc debt` here.
_TAG = "lc-debt:"


def _git_repo_with_marker(tmp_path: Path, note: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    marker_line = f"# {_TAG} {note}"
    (root / "mod.py").write_text(f"{marker_line}\ndef f():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def test_debt_fail_on_no_trigger_exits_nonzero_when_marker_has_no_trigger(tmp_path: Path, monkeypatch) -> None:
    root = _git_repo_with_marker(tmp_path, "revisit if slow")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["debt", "--fail-on-no-trigger"])
    assert result.exit_code == 1, result.output
    assert "no-trigger" in result.output


def test_debt_fail_on_no_trigger_exits_zero_when_all_markers_have_trigger(tmp_path: Path, monkeypatch) -> None:
    root = _git_repo_with_marker(tmp_path, "revisit if slow; move to a background job")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["debt", "--fail-on-no-trigger"])
    assert result.exit_code == 0, result.output


def test_debt_without_flag_still_exits_zero_on_no_trigger_markers(tmp_path: Path, monkeypatch) -> None:
    # Default behavior (no flag) must stay unchanged: informational only.
    root = _git_repo_with_marker(tmp_path, "revisit if slow")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["debt"])
    assert result.exit_code == 0, result.output
