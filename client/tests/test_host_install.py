"""Native MCP registration for standard coding agents."""

from __future__ import annotations

import io
import subprocess
from collections.abc import Sequence
from pathlib import Path

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

    monkeypatch.setattr("lemoncrow_client.cli._external_runtime_executable", lambda: None)
    monkeypatch.setattr(host_install, "_which", fake_which)
    monkeypatch.setattr(host_install, "_run", fake_run)
    return calls


@pytest.mark.parametrize("host", ["claude", "codex", "opencode", "cursor", "pi", "lemoncode"])
def test_registration_pins_installed_runtime_over_review_launcher(tmp_path, monkeypatch, host):
    from lemoncrow_client import host_trial

    runtime = tmp_path / "runtime" / "client" / "bin" / "lemoncrow-client"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\n")
    runtime.chmod(0o755)
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_RUNTIME_CLIENT", raising=False)
    monkeypatch.setattr(host_install.sys, "argv", ["-c"])
    monkeypatch.setattr(host_install, "_which", lambda _: "/old/review/bin/lemoncrow-client")
    expected = (str(runtime), "runtime", "lite-mcp", "--host", host)
    assert host_install._client_mcp_command(host) == expected
    assert host_trial._runtime_mcp_invocation(host) == (expected[0], list(expected[1:]))


def test_interpreter_owned_runtime_wins_during_pi_refresh(tmp_path, monkeypatch):
    runtime = tmp_path / "bin" / "lemoncrow-runtime-client"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\n")
    monkeypatch.setattr(host_install.sys, "executable", str(runtime.parent / "python"))
    monkeypatch.setattr(host_install.sys, "argv", ["-c"])
    monkeypatch.setattr("lemoncrow_client.cli._external_runtime_executable", lambda: Path("/stale/client"))
    assert host_install._client_mcp_command("pi")[0] == str(runtime)


def test_claude_uses_native_user_scope_mcp_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude"})
    sink = io.StringIO()
    assert main(["runtime", "enable", "claude"], sink) == 0
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
                "runtime",
                "lite-mcp",
                "--host",
                "claude",
            ),
            True,
        ),
    ]


def test_hosted_enable_is_explicit_and_uses_thin_client_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude"})
    sink = io.StringIO()
    assert main(["runtime", "enable", "claude", "--hosted"], sink) == 0
    addition = next(argv for argv, check in calls if check)
    assert addition[-4:] == ("runtime", "mcp", "--host", "claude")
    assert "runtime mcp --host claude" in sink.getvalue()


def test_codex_uses_native_mcp_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"codex"})
    assert main(["runtime", "enable", "codex"], io.StringIO()) == 0
    assert calls[-1] == (
        (
            "/usr/bin/codex",
            "mcp",
            "add",
            "lc",
            "--",
            "/opt/lemoncrow/bin/lemoncrow-client",
            "runtime",
            "lite-mcp",
            "--host",
            "codex",
        ),
        True,
    )


def test_opencode_uses_native_global_mcp_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"opencode"})
    assert main(["runtime", "enable", "opencode"], io.StringIO()) == 0
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
                "runtime",
                "lite-mcp",
                "--host",
                "opencode",
            ),
            True,
        )
    ]


def test_auto_registers_every_detected_supported_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude", "codex"})
    sink = io.StringIO()
    assert main(["runtime", "enable", "auto"], sink) == 0
    additions = [argv for argv, check in calls if check]
    assert [argv[0] for argv in additions] == ["/usr/bin/claude", "/usr/bin/codex"]
    assert "opencode" not in sink.getvalue()


def test_dry_run_never_invokes_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude"})
    sink = io.StringIO()
    assert main(["runtime", "enable", "claude", "--dry-run"], sink) == 0
    assert calls == []
    assert "claude mcp add" in sink.getvalue()
    assert "lemoncrow-client runtime lite-mcp --host claude" in sink.getvalue()


def test_explicit_missing_host_fails_without_installing_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, set())
    sink = io.StringIO()
    assert main(["runtime", "enable", "opencode"], sink) == 1
    assert calls == []
    assert "not found" in sink.getvalue()


def test_auto_without_supported_hosts_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hosts(monkeypatch, set())
    sink = io.StringIO()
    assert main(["runtime", "enable", "auto"], sink) == 2
    assert "no supported coding agent found" in sink.getvalue()


def test_runtime_enable_requires_an_explicit_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude"})
    sink = io.StringIO()
    assert main(["runtime", "enable"], sink) == 2
    assert calls == []
    assert "requires an explicit host" in sink.getvalue()


def test_removed_top_level_runtime_aliases_are_not_invokable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude"})
    for argv in (["install", "--agent", "claude"], ["mcp"], ["routes"]):
        sink = io.StringIO()
        assert main(argv, sink) == 2
        assert "It is a Runtime command: lc rt " in sink.getvalue()
    assert calls == []


def test_runtime_mcp_help_is_nested_and_non_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("lemoncrow_client.mcpserver.serve", lambda: calls.append("serve") or 0)
    sink = io.StringIO()
    assert main(["runtime", "mcp", "--help"], sink) == 0
    assert "runtime mcp [--host NAME]" in sink.getvalue()
    assert calls == []


def _hosted_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_URL", "https://lemoncrow.example")
    monkeypatch.setenv("LEMONCROW_INSTALL_MODE", "hosted")


def test_runtime_mcp_serves_runtime_lite_without_a_hosted_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("lemoncrow_client.mcpserver.serve", lambda: calls.append("thin") or 0)
    monkeypatch.setattr("lemoncrow_client.lite.mcp.serve", lambda: calls.append("lite") or 0)
    assert main(["runtime", "mcp", "--host", "claude"], io.StringIO()) == 0
    assert calls == ["lite"]


def test_runtime_mcp_sets_explicit_host_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_serve() -> int:
        import os

        seen.append(os.environ.get("LEMONCROW_AGENT", ""))
        return 0

    _hosted_runtime(monkeypatch)
    monkeypatch.setattr("lemoncrow_client.mcpserver.serve", fake_serve)
    assert main(["runtime", "mcp", "--host", "cursor"], io.StringIO()) == 0
    assert seen == ["cursor"]


def test_runtime_routes_is_nested_under_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import lemoncrow_client.routing as routing

    sink = io.StringIO()
    assert main(["runtime", "routes"], sink) == 0
    assert set(routing.ROUTES).intersection(sink.getvalue().split())


def test_runtime_status_uses_native_read_only_host_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude", "codex"})
    sink = io.StringIO()

    assert main(["runtime", "status"], sink) == 0

    queried = [argv for argv, check in calls if not check]
    assert ("/usr/bin/claude", "mcp", "get", "lc") in queried
    assert ("/usr/bin/codex", "mcp", "get", "lc", "--json") in queried
    assert "claude: enabled · Unknown mode" in sink.getvalue()
    assert "codex: enabled · Unknown mode" in sink.getvalue()
    assert "opencode: host not installed" in sink.getvalue()


def test_runtime_status_reports_stable_modes_in_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_which(name: str) -> str | None:
        if name == "lemoncrow-client":
            return "/opt/lemoncrow/bin/lemoncrow-client"
        if name in {"claude", "codex", "opencode"}:
            return f"/usr/bin/{name}"
        return None

    def fake_run(argv: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append((tuple(argv), check))
        if argv[0].endswith("claude"):
            output = "command: lemoncrow-client runtime lite-mcp --host claude"
        elif argv[0].endswith("codex"):
            output = '{"command":"lemoncrow-client","args":["runtime","mcp","--host","codex"]}'
        else:
            output = "lc connected"
        return subprocess.CompletedProcess(list(argv), 0, stdout=output, stderr="")

    monkeypatch.setattr(host_install, "_which", fake_which)
    monkeypatch.setattr(host_install, "_run", fake_run)
    sink = io.StringIO()

    assert main(["runtime", "status", "--json"], sink) == 0
    payload = __import__("json").loads(sink.getvalue())
    assert {row["host"]: row["mode"] for row in payload["integrations"]} == {
        "claude": "local_lite",
        "codex": "hosted",
        "opencode": "unknown",
    }


def test_runtime_disable_uses_native_remove_for_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"claude"})
    sink = io.StringIO()

    assert main(["runtime", "disable", "claude"], sink) == 0

    assert calls == [
        (("/usr/bin/claude", "mcp", "remove", "--scope", "user", "lc"), False),
    ]
    assert "Runtime disabled" in sink.getvalue()


def test_runtime_disable_dry_run_does_not_invoke_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_hosts(monkeypatch, {"codex"})
    sink = io.StringIO()

    assert main(["runtime", "disable", "codex", "--dry-run"], sink) == 0

    assert calls == []
    assert "codex mcp remove lc" in sink.getvalue()


def test_runtime_disable_opencode_preserves_no_direct_config_write_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_hosts(monkeypatch, {"opencode"})
    sink = io.StringIO()

    assert main(["runtime", "disable", "opencode"], sink) == 1

    assert calls == []
    assert "native CLI has no MCP remove command" in sink.getvalue()
