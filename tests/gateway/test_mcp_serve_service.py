"""Persistent remote MCP uses one thin-client gateway plus one shared tunnel."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli.commands import _mcp_service as svc
from lemoncrow.gateway.cli.commands import _persistent_tunnel as pt
from lemoncrow.gateway.cli.commands.mcp_serve import mcp_serve_cmd, mcp_service_group
from lemoncrow.gateway.mcp_connectors import ConnectorBinding, load_all_connectors, save_connector


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture
def systemd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    unit_dir = tmp_path / "systemd"
    monkeypatch.setenv("LEMONCROW_MCP_ALLOW_SERVICE", "1")
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr(svc, "SYSTEMD_USER_DIR", unit_dir)
    monkeypatch.setattr(svc, "supervisor_kind", lambda: "systemd")
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: None)
    return unit_dir


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "show-user" in cmd:
            return _completed(0, "Linger=yes")
        if "show" in cmd:
            return _completed(0, "ActiveState=active\nUnitFileState=enabled\nMainPID=123\n")
        return _completed(0)

    monkeypatch.setattr(svc, "_run", _fake_run)
    return calls


def _persistent_serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> Any:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.mcp_serve._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: ("shared-tid", "/creds/shared.json"))
    return CliRunner().invoke(mcp_serve_cmd, ["--persistent", *args])


def test_persistent_installs_one_direct_cloudflared_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    result = _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com")
    assert result.exit_code == 0, result.output

    unit = systemd_dir / svc.SHARED_SYSTEMD_UNIT
    assert unit.exists()
    content = unit.read_text(encoding="utf-8")
    command = shlex.split(next(line for line in content.splitlines() if line.startswith("ExecStart=")).split("=", 1)[1])
    assert command == [
        "/usr/bin/cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--grace-period",
        "2s",
        "run",
        "--credentials-file",
        "/creds/shared.json",
        "--url",
        "http://127.0.0.1:7421",
        "shared-tid",
    ]
    assert "lemoncrow mcp serve" not in content
    assert f"After=network-online.target {svc.SHARED_GATEWAY_SYSTEMD_UNIT}" in content
    assert "lemoncrow-local-server.service" not in content
    assert "Requires=" not in content and "BindsTo=" not in content
    assert "TimeoutStopSec=5" in content

    gateway = systemd_dir / svc.SHARED_GATEWAY_SYSTEMD_UNIT
    assert gateway.exists()
    gateway_content = gateway.read_text(encoding="utf-8")
    assert "lemoncrow_server_core mcp-gateway --port 7421 --backend-port 7420" in gateway_content
    assert "lemoncrow-local-server.service" not in gateway_content
    assert ["systemctl", "--user", "enable", svc.SHARED_GATEWAY_SYSTEMD_UNIT] in run_calls
    assert ["systemctl", "--user", "enable", svc.SHARED_SYSTEMD_UNIT] in run_calls
    assert ["systemctl", "--user", "restart", svc.SHARED_GATEWAY_SYSTEMD_UNIT] in run_calls
    assert ["systemctl", "--user", "restart", svc.SHARED_SYSTEMD_UNIT] in run_calls


def test_second_hostname_reuses_same_unit_and_shared_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    routed: list[str] = []
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: routed.append(hostname))
    first = _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com")
    second = _persistent_serve(monkeypatch, tmp_path, "--hostname", "b.example.com")
    assert first.exit_code == second.exit_code == 0
    assert routed == ["a.example.com", "b.example.com"]
    assert [binding.hostname for binding in load_all_connectors()] == ["a.example.com", "b.example.com"]
    assert sorted(path.name for path in systemd_dir.glob("lemoncrow-mcp-*.service")) == sorted(
        [svc.SHARED_GATEWAY_SYSTEMD_UNIT, svc.SHARED_SYSTEMD_UNIT]
    )
    assert run_calls.count(["systemctl", "--user", "restart", svc.SHARED_SYSTEMD_UNIT]) == 1


def test_pairing_code_stays_per_hostname_and_stable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    first = _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com")
    second = _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com")
    assert first.exit_code == second.exit_code == 0

    def code(output: str) -> str:
        return next(line for line in output.splitlines() if "Pairing code:" in line).split(":", 1)[1].strip()

    assert code(first.output) == code(second.output)


class _ImmediateProc:
    returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def poll(self) -> int | None:
        return self.returncode


def test_foreground_runs_only_cloudflared_and_installs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    started: list[tuple[str, str, int, str]] = []

    def start(binary: str, ref: str, port: int, creds: str) -> _ImmediateProc:
        started.append((binary, ref, port, creds))
        return _ImmediateProc()

    gateway = _ImmediateProc()
    monkeypatch.setattr(svc, "start_gateway_process", lambda **_kwargs: gateway)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.mcp_serve._wait_gateway_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pt, "start_named_tunnel_process", start)
    result = _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com", "--foreground")
    assert result.exit_code == 0, result.output
    assert started == [("/usr/bin/cloudflared", "shared-tid", 7421, "/creds/shared.json")]
    assert gateway.returncode == 0
    assert not systemd_dir.exists()


def test_service_list_shows_connectors_behind_one_shared_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    assert _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com").exit_code == 0
    assert _persistent_serve(monkeypatch, tmp_path, "--hostname", "b.example.com").exit_code == 0
    result = CliRunner().invoke(mcp_service_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "Persistent MCP connectors · 2" in result.output
    assert "shared tunnel  active" in result.output
    assert "https://a.example.com/mcp" in result.output
    assert "https://b.example.com/mcp" in result.output


def test_service_repair_recreates_unit_from_durable_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    save_connector(ConnectorBinding("a.example.com", str(workspace)))
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")
    pt.save_tunnel_state(
        pt.shared_tunnel_state_path(),
        pt.TunnelState("lemoncrow-mcp", "shared-id", "*", str(credentials)),
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.mcp_serve._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.mcp_serve._persistent_origin_port", lambda: 7421)
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.mcp_serve._persistent_backend_port", lambda: 7420)

    result = CliRunner().invoke(mcp_service_group, ["repair"])

    assert result.exit_code == 0, result.output
    assert "Repaired shared MCP tunnel" in result.output
    content = (systemd_dir / svc.SHARED_SYSTEMD_UNIT).read_text(encoding="utf-8")
    assert "--credentials-file " + str(credentials) in content
    assert "--url http://127.0.0.1:7421 shared-id" in content
    gateway_content = (systemd_dir / svc.SHARED_GATEWAY_SYSTEMD_UNIT).read_text(encoding="utf-8")
    assert "mcp-gateway --port 7421 --backend-port 7420" in gateway_content
    assert ["systemctl", "--user", "restart", svc.SHARED_SYSTEMD_UNIT] in run_calls


def test_service_repair_is_noop_without_connectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))

    result = CliRunner().invoke(mcp_service_group, ["repair"])

    assert result.exit_code == 0, result.output
    assert "nothing to repair" in result.output
    assert not systemd_dir.exists()
    assert run_calls == []


def test_start_stop_restart_target_gateway_and_tunnel(systemd_dir: Path, run_calls: list[list[str]]) -> None:
    systemd_dir.mkdir(parents=True)
    (systemd_dir / svc.SHARED_SYSTEMD_UNIT).write_text("[Unit]\n", encoding="utf-8")
    (systemd_dir / svc.SHARED_GATEWAY_SYSTEMD_UNIT).write_text("[Unit]\n", encoding="utf-8")
    for action in ("start", "stop", "restart"):
        result = CliRunner().invoke(mcp_service_group, [action])
        assert result.exit_code == 0, result.output
        assert ["systemctl", "--user", action, svc.SHARED_SYSTEMD_UNIT] in run_calls
        assert ["systemctl", "--user", action, svc.SHARED_GATEWAY_SYSTEMD_UNIT] in run_calls


def test_restart_can_rotate_one_connector_pairing_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    assert _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com").exit_code == 0
    before = CliRunner().invoke(mcp_service_group, ["code", "a.example.com"])
    rotated = CliRunner().invoke(mcp_service_group, ["restart", "a.example.com", "--new-pairing-code"])
    after = CliRunner().invoke(mcp_service_group, ["code", "a.example.com"])
    assert before.exit_code == rotated.exit_code == after.exit_code == 0
    assert before.output.strip() != after.output.strip()
    assert after.output.strip() in rotated.output
    assert ["systemctl", "--user", "restart", svc.SHARED_SYSTEMD_UNIT] in run_calls


def test_remove_one_connector_keeps_shared_service_when_another_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    assert _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com").exit_code == 0
    assert _persistent_serve(monkeypatch, tmp_path, "--hostname", "b.example.com").exit_code == 0
    result = CliRunner().invoke(mcp_service_group, ["remove", "a.example.com"])
    assert result.exit_code == 0, result.output
    assert [binding.hostname for binding in load_all_connectors()] == ["b.example.com"]
    assert (systemd_dir / svc.SHARED_SYSTEMD_UNIT).exists()


def test_remove_last_connector_removes_shared_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, systemd_dir: Path, run_calls: list[list[str]]
) -> None:
    assert _persistent_serve(monkeypatch, tmp_path, "--hostname", "a.example.com").exit_code == 0
    result = CliRunner().invoke(mcp_service_group, ["remove", "a.example.com"])
    assert result.exit_code == 0, result.output
    assert load_all_connectors() == []
    assert not (systemd_dir / svc.SHARED_SYSTEMD_UNIT).exists()
    assert not (systemd_dir / svc.SHARED_GATEWAY_SYSTEMD_UNIT).exists()
    assert ["systemctl", "--user", "disable", "--now", svc.SHARED_SYSTEMD_UNIT] in run_calls
    assert ["systemctl", "--user", "disable", "--now", svc.SHARED_GATEWAY_SYSTEMD_UNIT] in run_calls


def test_registration_is_skipped_in_tests_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_MCP_ALLOW_SERVICE", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "guard")
    assert svc.supervisor_kind() is None
