"""Post-edit hooks: vcs_status step (jj, falling back to git)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import lemoncrow.pro.capabilities.tool_supervision.post_edit_hooks as peh


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_vcs_status_prefers_jj_when_it_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(peh, "_has", lambda tool: tool in ("jj", "git"))

    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[0] == "jj"
        return _FakeProc(0, "M foo.py\nA bar.py\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = peh.run_post_edit_hooks(
        ["foo.py"],
        repo_root=Path("/tmp"),
        config=peh.HookConfig(
            run_format=False, run_organize_imports=False, run_lint_autofix=False, run_diagnostics=False
        ),
    )

    assert calls, "expected jj to be tried"
    assert result.vcs_source == "jj"
    assert result.vcs_status == ["M foo.py", "A bar.py"]
    assert "vcs_status" in result.steps_ran


def test_vcs_status_falls_back_to_git_when_jj_fails(monkeypatch) -> None:
    monkeypatch.setattr(peh, "_has", lambda tool: tool in ("jj", "git"))

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "jj":
            return _FakeProc(1, "")  # broken/absent jj working copy
        assert cmd[0] == "git"
        return _FakeProc(0, " M foo.py\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = peh.run_post_edit_hooks(
        ["foo.py"],
        repo_root=Path("/tmp"),
        config=peh.HookConfig(
            run_format=False, run_organize_imports=False, run_lint_autofix=False, run_diagnostics=False
        ),
    )

    assert result.vcs_source == "git"
    assert result.vcs_status == [" M foo.py"]
    assert "vcs_status" in result.steps_ran


def test_vcs_status_skipped_when_neither_binary_present(monkeypatch) -> None:
    monkeypatch.setattr(peh, "_has", lambda tool: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))

    result = peh.run_post_edit_hooks(
        ["foo.py"],
        repo_root=Path("/tmp"),
        config=peh.HookConfig(
            run_format=False, run_organize_imports=False, run_lint_autofix=False, run_diagnostics=False
        ),
    )

    assert result.vcs_status == []
    assert result.vcs_source is None
    assert "vcs_status" in result.steps_skipped


def test_vcs_status_disabled_via_config(monkeypatch) -> None:
    monkeypatch.setattr(peh, "_has", lambda tool: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))

    result = peh.run_post_edit_hooks(
        ["foo.py"],
        repo_root=Path("/tmp"),
        config=peh.HookConfig(
            run_format=False,
            run_organize_imports=False,
            run_lint_autofix=False,
            run_diagnostics=False,
            run_vcs_status=False,
        ),
    )

    assert result.vcs_status == []
    assert "vcs_status" not in result.steps_ran
    assert "vcs_status" not in result.steps_skipped
