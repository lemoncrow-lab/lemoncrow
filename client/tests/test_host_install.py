"""Native MCP registration for standard coding agents."""

from __future__ import annotations

import io
import subprocess
from collections.abc import Sequence

import pytest
from lemoncrow_client import host_install
from lemoncrow_client.cli import main


def _fake_hosts(monkeypatch: pytest.MonkeyPatch, available: set[str]) -> list[tuple[tuple[str, ...], bool]]:
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_which(name: str) -> str | None:
        if name == "lemoncrow-client":
            return "/opt/lemoncrow/bin/lemoncrow-client"
        if name in available:
            return f"/usr/bin/{name}"
        return None

    def fake_run(argv: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append((tuple(argv), check))
        return subprocess.CompletedProcess(list(argv), 0, stdout="ok", stderr="")

    monkeypatch.setattr(host_install, "_which", fake_which)
    monkeypatch.setattr(host_install, "_run", fake_run)
    return calls


def test_claude_uses_native_user_scope_mcp_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude"})
    sink = io.StringIO()
    assert main(["install", "--agent", "claude"], sink) == 0
    assert calls == [
        (("/usr/bin/claude", "mcp", "remove", "--scope", "user", "lc"), False),
        (
            (
                "/usr/bin/claude",
                "mcp",
                "add",
                "--scope",
                "user",
                "lc",
                "--",
                "/opt/lemoncrow/bin/lemoncrow-client",
                "mcp",
            ),
            True,
        ),
    ]


def test_codex_uses_native_mcp_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"codex"})
    assert main(["install", "--agent", "codex"], io.StringIO()) == 0
    assert calls[-1] == (
        (
            "/usr/bin/codex",
            "mcp",
            "add",
            "lc",
            "--",
            "/opt/lemoncrow/bin/lemoncrow-client",
            "mcp",
        ),
        True,
    )


def test_opencode_uses_native_global_mcp_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"opencode"})
    assert main(["install", "--agent", "opencode"], io.StringIO()) == 0
    assert calls == [
        (
            (
                "/usr/bin/opencode",
                "mcp",
                "add",
                "lc",
                "--global",
                "--",
                "/opt/lemoncrow/bin/lemoncrow-client",
                "mcp",
            ),
            True,
        )
    ]


def test_auto_registers_every_detected_supported_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude", "codex"})
    sink = io.StringIO()
    assert main(["install"], sink) == 0
    additions = [argv for argv, check in calls if check]
    assert [argv[0] for argv in additions] == ["/usr/bin/claude", "/usr/bin/codex"]
    assert "opencode" not in sink.getvalue()


def test_dry_run_never_invokes_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude"})
    sink = io.StringIO()
    assert main(["install", "--agent", "claude", "--dry-run"], sink) == 0
    assert calls == []
    assert "claude mcp add" in sink.getvalue()
    assert "lemoncrow-client mcp" in sink.getvalue()


def test_explicit_missing_host_fails_without_installing_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, set())
    sink = io.StringIO()
    assert main(["install", "--agent", "opencode"], sink) == 1
    assert calls == []
    assert "not found" in sink.getvalue()


def test_auto_without_supported_hosts_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hosts(monkeypatch, set())
    sink = io.StringIO()
    assert main(["install"], sink) == 2
    assert "no supported coding agent found" in sink.getvalue()
