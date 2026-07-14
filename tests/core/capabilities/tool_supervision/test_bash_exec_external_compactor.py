"""classify_command's soft external-compactor rewrite path (on by default, detect-if-present)."""

from __future__ import annotations

import subprocess

import pytest

from lemoncrow.pro.capabilities.tool_supervision import bash_exec
from lemoncrow.pro.capabilities.tool_supervision import external_compactors as ec


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    ec.reset()
    monkeypatch.delenv(ec._ENV_ENABLED, raising=False)
    yield
    ec.reset()


def test_binary_absent_falls_back_to_allow_even_though_enabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(ec.shutil, "which", lambda name: None)
    assert bash_exec.classify_command("git status").action == "allow"


def test_explicit_opt_out_skips_detection_entirely(monkeypatch) -> None:
    monkeypatch.setenv(ec._ENV_ENABLED, "0")
    calls: list[str] = []

    def _tracking_which(name: str) -> str:
        calls.append(name)
        return "/usr/local/bin/rtk"

    monkeypatch.setattr(ec.shutil, "which", _tracking_which)
    assert bash_exec.classify_command("git status").action == "allow"
    assert calls == []  # never even probed once opted out


def test_binary_present_rewrites_automatically(monkeypatch) -> None:
    monkeypatch.setattr(ec.shutil, "which", lambda name: "/usr/local/bin/rtk")
    monkeypatch.setattr(
        ec.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="rtk 1.0\n", stderr=""),
    )
    decision = bash_exec.classify_command("git status")
    assert decision.action == "rewrite"
    assert decision.rewrite_target == "external_compactor"
    assert decision.rewrite_payload is not None
    assert decision.rewrite_payload["compactor"] == "rtk"
    assert decision.rewrite_payload["binary_path"] == "/usr/local/bin/rtk"
    assert decision.rewrite_payload["original_command"] == "git status"


def test_mutating_command_never_rewritten_even_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(ec.shutil, "which", lambda name: "/usr/local/bin/rtk")
    monkeypatch.setattr(
        ec.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="rtk 1.0\n", stderr=""),
    )
    assert bash_exec.classify_command("git commit -m x").action == "allow"
    assert bash_exec.classify_command("docker run x").action == "allow"
