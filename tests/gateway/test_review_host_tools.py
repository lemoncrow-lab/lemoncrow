from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review import delivery
from lemoncrow.pro.environment import host_tools


def test_review_agents_found_without_login_shell_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for case_index, (agent, directory) in enumerate(
        [
            ("claude", ".local/bin"),
            ("codex", ".local/bin"),
            ("opencode", ".opencode/bin"),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        with monkeypatch.context() as case_monkeypatch:
            case_monkeypatch.setattr(host_tools.sys, "platform", "darwin")
            case_monkeypatch.setattr(Path, "home", lambda _home=case_tmp_path: _home)
            case_monkeypatch.setenv("PATH", "/usr/bin:/bin")
            location = case_tmp_path / directory
            location.mkdir(parents=True)
            cli = location / agent
            cli.write_text("#!/usr/bin/env review-test-runtime\n")
            cli.chmod(493)
            interpreter = case_tmp_path / ".local/bin/review-test-runtime"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("#!/bin/sh\nprintf 'agent runtime found\\n'\n")
            interpreter.chmod(493)
            assert host_tools.host_executable(agent) == str(cli)
            assert delivery.new_session_supported(agent) == (True, "")
            result = subprocess.run(
                [str(cli), "--version"],
                env=host_tools.host_tool_environment(),
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == "agent runtime found\n"
            assert os.environ["PATH"] == "/usr/bin:/bin"


def test_missing_agent_remains_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "host_executable", lambda name: None)
    assert delivery.new_session_supported("codex") == (False, "codex CLI is not installed")
