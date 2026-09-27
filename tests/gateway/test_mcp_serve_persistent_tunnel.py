"""``lc mcp serve --persistent`` — named-tunnel (Cloudflare) backend.

Two layers: unit tests against ``_persistent_tunnel.py``'s functions (each
``subprocess.run``/``subprocess.Popen`` call mocked individually — none of
this can hit a real Cloudflare account/domain in CI), and CLI-level tests
exercising ``mcp_serve_cmd``'s ``--persistent``/``--hostname``/
``--reset-tunnel`` wiring with the whole orchestration function mocked.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli.commands import _mcp_service as svc
from lemoncrow.gateway.cli.commands import _persistent_tunnel as pt
from lemoncrow.gateway.cli.commands.mcp_serve import mcp_serve_cmd


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeTunnelProc:
    """Stand-in for the cloudflared Popen handle; records cleanup calls.

    Mirrors real ``subprocess.Popen`` lifecycle, not just its method names:
    ``wait()`` blocks until ``terminate()``/``kill()`` actually ends the
    "process" (an ``Event``, not an immediate return of 0), and
    ``returncode`` stays ``None`` until then. A fake that returns from
    ``wait()`` before anything told it to exit made the tunnel-watchdog
    thread in mcp_serve.py race the test's own shutdown path.
    """

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None
        self._exited = threading.Event()

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0
        self._exited.set()

    def wait(self, timeout: float | None = None) -> int:
        # Persistent mode is tunnel-only now. CLI tests model a tunnel process
        # that exits cleanly instead of relying on uvicorn.run() to end the
        # foreground command. Timeout-based waits still model cleanup.
        if timeout is None and self.returncode is None:
            self.returncode = 0
            self._exited.set()
        if not self._exited.wait(timeout):
            assert timeout is not None
            raise subprocess.TimeoutExpired(cmd="cloudflared", timeout=timeout)
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self._exited.set()

    def poll(self) -> int | None:
        return self.returncode


# ── TunnelState persistence ─────────────────────────────────────────────────
def test_tunnel_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "tunnel" / "state.json"
    state = pt.TunnelState(
        tunnel_name="lemoncrow-chatgpt",
        tunnel_id="abc-123",
        hostname="mcp.example.com",
        credentials_path="/home/x/.cloudflared/abc-123.json",
    )
    pt.save_tunnel_state(path, state)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    loaded = pt.load_tunnel_state(path)
    assert loaded == state


def test_load_tunnel_state_missing_file_returns_none(tmp_path: Path) -> None:
    assert pt.load_tunnel_state(tmp_path / "nope.json") is None


def test_load_tunnel_state_corrupt_json_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert pt.load_tunnel_state(path) is None


def test_load_tunnel_state_missing_fields_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"tunnel_name": "x"}), encoding="utf-8")
    assert pt.load_tunnel_state(path) is None


def test_reset_tunnel_state_removes_file_and_reports(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    assert pt.reset_tunnel_state(path) is True
    assert not path.exists()
    assert pt.reset_tunnel_state(path) is False  # nothing left to remove


# ── login ────────────────────────────────────────────────────────────────────
def test_is_logged_in_checks_cert_path(tmp_path: Path) -> None:
    cert = tmp_path / ".cloudflared" / "cert.pem"
    assert pt.is_logged_in(cert) is False
    cert.parent.mkdir(parents=True)
    cert.write_text("cert", encoding="utf-8")
    assert pt.is_logged_in(cert) is True


def test_run_cloudflared_login_invokes_bare_command_inherits_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return _completed(0)

    monkeypatch.setattr(pt.subprocess, "run", _fake_run)
    pt.run_cloudflared_login("cloudflared")
    assert calls == [(["cloudflared", "tunnel", "login"], {})]  # no capture kwargs -> inherits stdio


def test_run_cloudflared_login_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: _completed(1))
    with pytest.raises(pt.TunnelSetupError):
        pt.run_cloudflared_login("cloudflared")


# ── find / create tunnel ─────────────────────────────────────────────────────
def test_find_existing_tunnel_returns_none_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: _completed(0, stdout="[]"))
    assert pt.find_existing_tunnel("cloudflared", "lemoncrow-chatgpt") is None


def test_find_existing_tunnel_parses_json_and_checks_credentials_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = tmp_path / ".cloudflared" / "cert.pem"
    cert.parent.mkdir(parents=True)
    creds = cert.parent / "tunnel-id-999.json"
    creds.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pt, "default_cert_path", lambda: cert)

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _completed(0, stdout=json.dumps([{"ID": "tunnel-id-999", "Name": "lemoncrow-chatgpt"}]))

    monkeypatch.setattr(pt.subprocess, "run", _fake_run)
    result = pt.find_existing_tunnel("cloudflared", "lemoncrow-chatgpt")
    assert result == ("tunnel-id-999", str(creds))
    assert calls == [["cloudflared", "tunnel", "list", "--name", "lemoncrow-chatgpt", "-o", "json"]]


def test_find_existing_tunnel_raises_when_credentials_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = tmp_path / ".cloudflared" / "cert.pem"
    monkeypatch.setattr(pt, "default_cert_path", lambda: cert)
    monkeypatch.setattr(
        pt.subprocess,
        "run",
        lambda cmd, **kw: _completed(0, stdout=json.dumps([{"ID": "missing-creds-id"}])),
    )
    with pytest.raises(pt.TunnelSetupError, match="credentials file"):
        pt.find_existing_tunnel("cloudflared", "lemoncrow-chatgpt")


def test_find_existing_tunnel_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: _completed(1, stderr="boom"))
    with pytest.raises(pt.TunnelSetupError):
        pt.find_existing_tunnel("cloudflared", "lemoncrow-chatgpt")


def test_create_tunnel_parses_id_and_credentials_path(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = (
        "Tunnel credentials written to /home/x/.cloudflared/new-id-1.json. "
        "cloudflared chose this file based on where your origin certificate was found.\n"
        "Created tunnel lemoncrow-chatgpt with id new-id-1\n"
    )
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _completed(0, stdout=stdout)

    monkeypatch.setattr(pt.subprocess, "run", _fake_run)
    tunnel_id, credentials_path = pt.create_tunnel("cloudflared", "lemoncrow-chatgpt")
    assert tunnel_id == "new-id-1"
    assert credentials_path == "/home/x/.cloudflared/new-id-1.json"
    assert calls == [["cloudflared", "tunnel", "create", "lemoncrow-chatgpt"]]


def test_create_tunnel_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: _completed(1, stderr="already exists or similar"))
    with pytest.raises(pt.TunnelSetupError):
        pt.create_tunnel("cloudflared", "lemoncrow-chatgpt")


def test_create_tunnel_raises_when_output_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: _completed(0, stdout="unexpected output shape"))
    with pytest.raises(pt.TunnelSetupError):
        pt.create_tunnel("cloudflared", "lemoncrow-chatgpt")


# ── route dns ────────────────────────────────────────────────────────────────
def test_route_dns_success_overwrites_existing_record(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _completed(0)

    monkeypatch.setattr(pt.subprocess, "run", _fake_run)
    pt.route_dns("cloudflared", "lemoncrow-chatgpt", "mcp.example.com")
    # --overwrite-dns: re-pointing a hostname off another tunnel must succeed,
    # instead of erroring on the existing record and being silently tolerated.
    assert calls == [
        ["cloudflared", "tunnel", "route", "dns", "--overwrite-dns", "lemoncrow-chatgpt", "mcp.example.com"]
    ]


def test_route_dns_raises_on_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: _completed(1, stderr="a CNAME record already exists"))
    with pytest.raises(pt.TunnelSetupError):
        pt.route_dns("cloudflared", "lemoncrow-chatgpt", "mcp.example.com")


def test_route_dns_raises_on_other_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: _completed(1, stderr="network unreachable"))
    with pytest.raises(pt.TunnelSetupError):
        pt.route_dns("cloudflared", "lemoncrow-chatgpt", "mcp.example.com")


# ── tunnel run process ───────────────────────────────────────────────────────
def test_start_named_tunnel_process_builds_supervised_logged_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class _FakePopen:
        def __init__(self, cmd: list[str], **kw: Any) -> None:
            calls.append((cmd, kw))

    monkeypatch.setattr(pt.subprocess, "Popen", _FakePopen)
    proc = pt.start_named_tunnel_process("cloudflared", "tunnel-id-1", 8788, "/creds/tunnel-id-1.json")
    assert isinstance(proc, _FakePopen)
    [(cmd, kwargs)] = calls
    assert cmd == [
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--grace-period",
        "2s",
        "run",
        "--credentials-file",
        "/creds/tunnel-id-1.json",
        "--url",
        "http://localhost:8788",
        "tunnel-id-1",
    ]
    # No stdout/stderr redirection: systemd/launchd or the foreground terminal
    # receives cloudflared's own diagnostics.
    assert kwargs == {"text": True}


# ── shared tunnel orchestration ─────────────────────────────────────────────
def test_provision_shared_tunnel_reuses_saved_state_but_routes_each_hostname(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    state = pt.TunnelState(pt.SHARED_TUNNEL_NAME, "existing-id", "*", "/c.json")
    pt.save_tunnel_state(pt.shared_tunnel_state_path(), state)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("saved shared tunnel should skip login/find/create")

    monkeypatch.setattr(pt, "is_logged_in", _boom)
    monkeypatch.setattr(pt, "find_existing_tunnel", _boom)
    monkeypatch.setattr(pt, "create_tunnel", _boom)
    routed: list[tuple[str, str]] = []
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: routed.append((ref, hostname)))

    resolved = pt.provision_shared_tunnel(hostname="b.example.com", binary="cloudflared", narrate=lambda _msg: None)
    assert resolved == state
    assert routed == [("existing-id", "b.example.com")]


def test_provision_shared_tunnel_first_time_full_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    order: list[str] = []
    monkeypatch.setattr(pt, "is_logged_in", lambda: (order.append("is_logged_in"), False)[1])
    monkeypatch.setattr(pt, "run_cloudflared_login", lambda binary: order.append("login"))
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: (order.append("find"), None)[1])
    monkeypatch.setattr(
        pt,
        "create_tunnel",
        lambda binary, name: (order.append("create"), ("new-id", "/new.json"))[1],
    )
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: order.append("route"))

    state = pt.provision_shared_tunnel(hostname="a.example.com", binary="cloudflared", narrate=lambda _msg: None)
    assert order == ["is_logged_in", "login", "find", "create", "route"]
    assert state == pt.TunnelState(pt.SHARED_TUNNEL_NAME, "new-id", "*", "/new.json")
    path = pt.shared_tunnel_state_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert pt.load_shared_tunnel_state() == state


def test_provision_shared_tunnel_reuses_cloudflare_tunnel_skips_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: ("found-id", "/found.json"))
    monkeypatch.setattr(pt, "create_tunnel", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no create")))
    routed: list[tuple[str, str]] = []
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: routed.append((ref, hostname)))

    state = pt.provision_shared_tunnel(hostname="a.example.com", binary="cloudflared", narrate=lambda _msg: None)
    assert state.tunnel_id == "found-id"
    assert routed == [("found-id", "a.example.com")]


def test_setup_shared_tunnel_starts_cloudflared_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    state = pt.TunnelState(pt.SHARED_TUNNEL_NAME, "shared-id", "*", "/shared.json")
    monkeypatch.setattr(pt, "provision_shared_tunnel", lambda **kwargs: state)
    started: list[tuple[str, str, int, str]] = []
    sentinel = object()

    def _start(binary: str, ref: str, port: int, creds: str) -> Any:
        started.append((binary, ref, port, creds))
        return sentinel

    monkeypatch.setattr(pt, "start_named_tunnel_process", _start)
    result = pt.setup_shared_tunnel(
        port=7420,
        hostname="a.example.com",
        binary="cloudflared",
        narrate=lambda _msg: None,
    )
    assert result is sentinel
    assert started == [("cloudflared", "shared-id", 7420, "/shared.json")]


# ── CLI wiring ───────────────────────────────────────────────────────────────


def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.mcp_serve._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr("lemoncrow.gateway.cli.commands._mcp_service.supervisor_kind", lambda: None)
    monkeypatch.setattr(svc, "start_gateway_process", lambda **_kwargs: _FakeTunnelProc())
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.mcp_serve._wait_gateway_ready", lambda *_args, **_kwargs: None)


def test_persistent_conflicts_with_no_tunnel() -> None:
    result = CliRunner().invoke(mcp_serve_cmd, ["--persistent", "--no-tunnel"])
    assert result.exit_code != 0
    assert "--persistent cannot be combined with --no-tunnel" in result.output


def test_reset_tunnel_requires_persistent() -> None:
    result = CliRunner().invoke(mcp_serve_cmd, ["--reset-tunnel"])
    assert result.exit_code != 0
    assert "--reset-tunnel requires --persistent" in result.output


def test_persistent_first_run_without_hostname_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _cli_env(monkeypatch, tmp_path)
    result = CliRunner().invoke(mcp_serve_cmd, ["--persistent"])
    assert result.exit_code != 0
    assert "needs --hostname" in result.output


def test_persistent_first_hostname_creates_shared_tunnel_and_routes_dns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    created: list[str] = []
    monkeypatch.setattr(
        pt, "create_tunnel", lambda binary, name: (created.append(name), ("shared-id", "/shared.json"))[1]
    )
    routed: list[tuple[str, str]] = []
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: routed.append((ref, hostname)))
    started: list[tuple[str, str, int, str]] = []

    def _start(binary: str, ref: str, port: int, creds: str) -> _FakeTunnelProc:
        started.append((binary, ref, port, creds))
        return _FakeTunnelProc()

    monkeypatch.setattr(pt, "start_named_tunnel_process", _start)
    result = CliRunner().invoke(
        mcp_serve_cmd,
        ["--persistent", "--hostname", "a.example.com", "--foreground"],
    )
    assert result.exit_code == 0, result.output
    assert created == [pt.SHARED_TUNNEL_NAME]
    assert routed == [("shared-id", "a.example.com")]
    assert started == [("/usr/bin/cloudflared", "shared-id", 7421, "/shared.json")]
    state = pt.load_shared_tunnel_state()
    assert state is not None
    assert state.tunnel_name == pt.SHARED_TUNNEL_NAME
    assert state.tunnel_id == "shared-id"


def test_second_hostname_reuses_shared_tunnel_and_only_adds_dns_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cli_env(monkeypatch, tmp_path)
    state = pt.TunnelState(
        tunnel_name=pt.SHARED_TUNNEL_NAME,
        tunnel_id="shared-id",
        hostname="*",
        credentials_path="/shared.json",
    )
    pt.save_tunnel_state(pt.shared_tunnel_state_path(), state)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("shared tunnel should not be created or looked up again")

    monkeypatch.setattr(pt, "is_logged_in", _boom)
    monkeypatch.setattr(pt, "find_existing_tunnel", _boom)
    monkeypatch.setattr(pt, "create_tunnel", _boom)
    routed: list[tuple[str, str]] = []
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: routed.append((ref, hostname)))
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda *args: _FakeTunnelProc())
    result = CliRunner().invoke(
        mcp_serve_cmd,
        ["--persistent", "--hostname", "b.example.com", "--foreground"],
    )
    assert result.exit_code == 0, result.output
    assert routed == [("shared-id", "b.example.com")]


def test_persistent_without_hostname_is_ambiguous_with_multiple_connectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cli_env(monkeypatch, tmp_path)
    from lemoncrow.gateway.mcp_connectors import ConnectorBinding, save_connector

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    save_connector(ConnectorBinding("a.example.com", str(first)))
    save_connector(ConnectorBinding("b.example.com", str(second)))
    result = CliRunner().invoke(mcp_serve_cmd, ["--persistent"])
    assert result.exit_code != 0
    assert "several connector hostnames are configured" in result.output


def test_persistent_oauth_state_remains_scoped_per_hostname(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _cli_env(monkeypatch, tmp_path)
    from lemoncrow.gateway.adapters.mcp_oauth import default_state_path

    assert default_state_path(pt.hostname_slug("a.example.com")) != default_state_path(
        pt.hostname_slug("b.example.com")
    )


def test_reset_tunnel_recreates_the_one_shared_tunnel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _cli_env(monkeypatch, tmp_path)
    pt.save_tunnel_state(
        pt.shared_tunnel_state_path(),
        pt.TunnelState(pt.SHARED_TUNNEL_NAME, "old-id", "*", "/old.json"),
    )
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    monkeypatch.setattr(pt, "create_tunnel", lambda binary, name: ("new-id", "/new.json"))
    monkeypatch.setattr(pt, "route_dns", lambda *args: None)
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda *args: _FakeTunnelProc())
    result = CliRunner().invoke(
        mcp_serve_cmd,
        ["--persistent", "--hostname", "a.example.com", "--reset-tunnel", "--foreground"],
    )
    assert result.exit_code == 0, result.output
    state = pt.load_shared_tunnel_state()
    assert state is not None and state.tunnel_id == "new-id"


def test_shared_tunnel_setup_failure_never_starts_cloudflared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    monkeypatch.setattr(
        pt,
        "create_tunnel",
        lambda binary, name: (_ for _ in ()).throw(pt.TunnelSetupError("boom: create failed")),
    )
    started: list[bool] = []
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda *args: started.append(True))
    result = CliRunner().invoke(
        mcp_serve_cmd,
        ["--persistent", "--hostname", "a.example.com", "--foreground"],
    )
    assert result.exit_code != 0
    assert "boom: create failed" in result.output
    assert started == []
