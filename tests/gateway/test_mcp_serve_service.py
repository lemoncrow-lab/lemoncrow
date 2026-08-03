"""``lc mcp serve --persistent`` boot-persistent service registration.

The registration path installs a *real* systemd/launchd user unit, so
``supervisor_kind()`` refuses to do it inside a test process unless
``LEMONCROW_MCP_ALLOW_SERVICE=1`` is set. Every test here sets it and points
the unit directory at ``tmp_path`` with ``_run`` stubbed out, so nothing ever
reaches the developer's own systemd.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from click.testing import CliRunner

from lemoncrow.gateway.cli.commands import _mcp_service as svc
from lemoncrow.gateway.cli.commands import _persistent_tunnel as pt
from lemoncrow.gateway.cli.commands.mcp_serve import mcp_serve_cmd, mcp_service_group


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture
def systemd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect unit installation to tmp_path and stub every systemctl call."""
    unit_dir = tmp_path / "systemd"
    monkeypatch.setenv("LEMONCROW_MCP_ALLOW_SERVICE", "1")
    monkeypatch.setattr(svc, "SYSTEMD_USER_DIR", unit_dir)
    monkeypatch.setattr(svc, "supervisor_kind", lambda: "systemd")
    monkeypatch.setattr(svc, "lemoncrow_binary", lambda: "/usr/bin/lemoncrow")
    return unit_dir


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        # `loginctl show-user` short-circuits _enable_linger when already on.
        return _completed(0, stdout="Linger=yes" if "show-user" in cmd else "ActiveState=active\nUnitFileState=enabled")

    monkeypatch.setattr(svc, "_run", _fake_run)
    return calls


def _persistent_serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> Any:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.mcp_serve._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: ("tid", "/creds.json"))
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: None)
    return CliRunner().invoke(mcp_serve_cmd, ["--persistent", *args])


# ── registration ─────────────────────────────────────────────────────────────
def test_persistent_installs_unit_and_does_not_serve_in_this_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    served: list[str] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append("ran"))
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda *a, **kw: pytest.fail("tunnel started in CLI"))

    result = _persistent_serve(monkeypatch, tmp_path, "--hostname", "mcp.example.com")

    assert result.exit_code == 0, result.output
    assert served == []  # the service serves, not this process
    unit = systemd_dir / "lemoncrow-mcp-mcp-example-com.service"
    assert unit.exists()
    assert ["systemctl", "--user", "enable", unit.name] in run_calls
    assert ["systemctl", "--user", "restart", unit.name] in run_calls
    assert "running as a background service" in result.output
    assert "https://mcp.example.com/mcp" in result.output


def test_installed_unit_runs_serve_foreground_in_the_starting_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    result = _persistent_serve(monkeypatch, tmp_path, "--hostname", "mcp.example.com", "--port", "9123")
    assert result.exit_code == 0, result.output

    content = (systemd_dir / "lemoncrow-mcp-mcp-example-com.service").read_text(encoding="utf-8")
    exec_start = next(line for line in content.splitlines() if line.startswith("ExecStart="))
    command = shlex.split(exec_start.split("=", 1)[1])
    assert command == [
        "/usr/bin/lemoncrow",
        "mcp",
        "serve",
        "--persistent",
        "--hostname",
        "mcp.example.com",
        "--foreground",
        "--port",
        "9123",
    ]
    assert f"WorkingDirectory={workspace}" in content
    assert "Restart=always" in content
    assert f"Environment={svc.SUPERVISED_ENV}=1" in content
    assert "WantedBy=default.target" in content  # starts again after a reboot


def test_pairing_code_is_stable_across_reregistration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    first = _persistent_serve(monkeypatch, tmp_path, "--hostname", "mcp.example.com")
    second = _persistent_serve(monkeypatch, tmp_path, "--hostname", "mcp.example.com")

    def _code(output: str) -> str:
        return next(line for line in output.splitlines() if "Pairing code:" in line).split(":", 1)[1].strip()

    assert _code(first.output) == _code(second.output)


def test_foreground_flag_serves_here_and_installs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    served: list[str] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append("ran"))
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda *a, **kw: _FakeProc())

    result = _persistent_serve(monkeypatch, tmp_path, "--hostname", "mcp.example.com", "--foreground")
    assert result.exit_code == 0, result.output
    assert served == ["ran"]
    assert not systemd_dir.exists()


def test_supervised_process_never_reregisters_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    monkeypatch.setenv(svc.SUPERVISED_ENV, "1")
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda *a, **kw: _FakeProc())

    result = _persistent_serve(monkeypatch, tmp_path, "--hostname", "mcp.example.com")
    assert result.exit_code == 0, result.output
    assert not systemd_dir.exists()
    assert run_calls == []


def test_one_off_pairing_code_cannot_configure_a_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    result = _persistent_serve(monkeypatch, tmp_path, "--hostname", "mcp.example.com", "--pairing-code", "x")
    assert result.exit_code != 0
    assert "--foreground" in result.output
    assert not systemd_dir.exists()


def test_without_a_supervisor_it_falls_back_to_serving_here(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    monkeypatch.setattr(svc, "supervisor_kind", lambda: None)
    served: list[str] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append("ran"))
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda *a, **kw: _FakeProc())

    result = _persistent_serve(monkeypatch, tmp_path, "--hostname", "mcp.example.com")
    assert result.exit_code == 0, result.output
    assert served == ["ran"]
    assert "serving in the foreground" in result.output


def test_registration_is_skipped_in_tests_without_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_MCP_ALLOW_SERVICE", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "guard")
    assert svc.supervisor_kind() is None


class _FakeProc:
    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None: ...


# ── lc mcp service ───────────────────────────────────────────────────────────
def _install_unit(unit_dir: Path, slug: str, hostname: str, workspace: str = "/w") -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"lemoncrow-mcp-{slug}.service").write_text(
        "[Unit]\n"
        f"Description=LemonCrow MCP server ({hostname})\n"
        "[Service]\n"
        f"WorkingDirectory={workspace}\n"
        f"ExecStart=/usr/bin/lemoncrow mcp serve --persistent --hostname {hostname} --foreground\n",
        encoding="utf-8",
    )


def test_service_list_shows_each_installed_server(systemd_dir: Path, run_calls: list[list[str]]) -> None:
    _install_unit(systemd_dir, "a-example-com", "a.example.com", workspace="/projects/a")
    _install_unit(systemd_dir, "b-example-com", "b.example.com", workspace="/projects/b")

    result = CliRunner().invoke(mcp_service_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "https://a.example.com/mcp" in result.output
    assert "https://b.example.com/mcp" in result.output
    assert "/projects/b" in result.output
    assert "active" in result.output


def test_service_start_stop_restart_target_the_named_host(systemd_dir: Path, run_calls: list[list[str]]) -> None:
    _install_unit(systemd_dir, "a-example-com", "a.example.com")
    _install_unit(systemd_dir, "b-example-com", "b.example.com")

    for action in ("start", "stop", "restart"):
        result = CliRunner().invoke(mcp_service_group, [action, "b.example.com"])
        assert result.exit_code == 0, result.output
        assert ["systemctl", "--user", action, "lemoncrow-mcp-b-example-com.service"] in run_calls


def test_service_action_without_a_host_is_ambiguous_when_several_exist(
    systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    _install_unit(systemd_dir, "a-example-com", "a.example.com")
    _install_unit(systemd_dir, "b-example-com", "b.example.com")

    result = CliRunner().invoke(mcp_service_group, ["restart"])
    assert result.exit_code != 0
    assert "several MCP services" in result.output
    assert not [call for call in run_calls if "restart" in call]


def test_single_service_needs_no_hostname(systemd_dir: Path, run_calls: list[list[str]]) -> None:
    _install_unit(systemd_dir, "only-example-com", "only.example.com")
    result = CliRunner().invoke(mcp_service_group, ["restart"])
    assert result.exit_code == 0, result.output
    assert ["systemctl", "--user", "restart", "lemoncrow-mcp-only-example-com.service"] in run_calls


def test_restart_can_rotate_the_pairing_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    _install_unit(systemd_dir, "only-example-com", "only.example.com")

    before = CliRunner().invoke(mcp_service_group, ["code", "only.example.com"])
    assert before.exit_code == 0, before.output
    rotated = CliRunner().invoke(mcp_service_group, ["restart", "--new-pairing-code"])
    assert rotated.exit_code == 0, rotated.output
    after = CliRunner().invoke(mcp_service_group, ["code", "only.example.com"])

    assert before.output.strip() != after.output.strip()
    assert after.output.strip() in rotated.output


def test_service_remove_deletes_the_unit_but_keeps_state(systemd_dir: Path, run_calls: list[list[str]]) -> None:
    _install_unit(systemd_dir, "only-example-com", "only.example.com")
    result = CliRunner().invoke(mcp_service_group, ["remove", "only.example.com"])
    assert result.exit_code == 0, result.output
    assert not (systemd_dir / "lemoncrow-mcp-only-example-com.service").exists()
    assert ["systemctl", "--user", "disable", "--now", "lemoncrow-mcp-only-example-com.service"] in run_calls


def test_service_command_without_any_installed_service_explains_how_to_get_one(
    systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    result = CliRunner().invoke(mcp_service_group, ["restart"])
    assert result.exit_code != 0
    assert "lc mcp serve --persistent" in result.output
