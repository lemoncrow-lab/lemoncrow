"""``lc chatgpt serve --persistent`` — named-tunnel (Cloudflare) backend.

Two layers: unit tests against ``_persistent_tunnel.py``'s functions (each
``subprocess.run``/``subprocess.Popen`` call mocked individually — none of
this can hit a real Cloudflare account/domain in CI), and CLI-level tests
exercising ``chatgpt_serve_cmd``'s ``--persistent``/``--hostname``/
``--reset-tunnel`` wiring with the whole orchestration function mocked.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from click.testing import CliRunner

from lemoncrow.gateway.cli.commands import _persistent_tunnel as pt
from lemoncrow.gateway.cli.commands.chatgpt import chatgpt_group


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeTunnelProc:
    """Stand-in for the cloudflared Popen handle; records cleanup calls."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


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
def test_start_named_tunnel_process_builds_correct_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class _FakePopen:
        def __init__(self, cmd: list[str], **kw: Any) -> None:
            calls.append((cmd, kw))
            self.stderr = iter(())  # empty iterable — drain thread exits immediately

    monkeypatch.setattr(pt.subprocess, "Popen", _FakePopen)
    proc = pt.start_named_tunnel_process("cloudflared", "tunnel-id-1", 8788, "/creds/tunnel-id-1.json")
    assert isinstance(proc, _FakePopen)
    [(cmd, kwargs)] = calls
    assert cmd == [
        "cloudflared",
        "tunnel",
        "run",
        "--credentials-file",
        "/creds/tunnel-id-1.json",
        "--url",
        "http://localhost:8788",
        "tunnel-id-1",
    ]
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.PIPE


# ── setup_persistent_tunnel orchestration ───────────────────────────────────
def test_setup_persistent_tunnel_reuses_existing_state_skips_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("should not be called when existing_state is provided")

    monkeypatch.setattr(pt, "is_logged_in", _boom)
    monkeypatch.setattr(pt, "run_cloudflared_login", _boom)
    monkeypatch.setattr(pt, "find_existing_tunnel", _boom)
    monkeypatch.setattr(pt, "create_tunnel", _boom)
    monkeypatch.setattr(pt, "route_dns", _boom)

    started: list[tuple[str, str, int, str]] = []
    sentinel = object()

    def _fake_start(binary: str, tunnel_ref: str, port: int, credentials_path: str) -> Any:
        started.append((binary, tunnel_ref, port, credentials_path))
        return sentinel

    monkeypatch.setattr(pt, "start_named_tunnel_process", _fake_start)

    state = pt.TunnelState(
        tunnel_name="lemoncrow-chatgpt", tunnel_id="existing-id", hostname="mcp.example.com", credentials_path="/c.json"
    )
    result = pt.setup_persistent_tunnel(
        port=8788,
        hostname="mcp.example.com",
        existing_state=state,
        state_path=tmp_path / "state.json",
        binary="cloudflared",
        narrate=lambda _msg: None,
    )
    assert result is sentinel
    assert started == [("cloudflared", "existing-id", 8788, "/c.json")]
    assert not (tmp_path / "state.json").exists()  # nothing re-saved on the reuse path


def test_setup_persistent_tunnel_first_time_full_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    call_order: list[str] = []
    monkeypatch.setattr(pt, "is_logged_in", lambda: (call_order.append("is_logged_in"), False)[1])
    monkeypatch.setattr(pt, "run_cloudflared_login", lambda binary: call_order.append("login"))
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: (call_order.append("find"), None)[1])

    def _fake_create(binary: str, name: str) -> tuple[str, str]:
        call_order.append("create")
        return "new-tunnel-id", "/home/x/.cloudflared/new-tunnel-id.json"

    monkeypatch.setattr(pt, "create_tunnel", _fake_create)
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: call_order.append("route"))

    started: list[tuple[str, str, int, str]] = []
    sentinel = object()

    def _fake_start(binary: str, tunnel_ref: str, port: int, credentials_path: str) -> Any:
        call_order.append("run")
        started.append((binary, tunnel_ref, port, credentials_path))
        return sentinel

    monkeypatch.setattr(pt, "start_named_tunnel_process", _fake_start)

    state_path = tmp_path / "chatgpt" / "tunnel" / "state.json"
    result = pt.setup_persistent_tunnel(
        port=9999,
        hostname="mcp.example.com",
        existing_state=None,
        state_path=state_path,
        binary="cloudflared",
        narrate=lambda _msg: None,
    )
    assert result is sentinel
    assert call_order == ["is_logged_in", "login", "find", "create", "route", "run"]
    assert started == [("cloudflared", "new-tunnel-id", 9999, "/home/x/.cloudflared/new-tunnel-id.json")]

    saved = pt.load_tunnel_state(state_path)
    assert saved == pt.TunnelState(
        tunnel_name=pt.tunnel_name_for("mcp.example.com"),
        tunnel_id="new-tunnel-id",
        hostname="mcp.example.com",
        credentials_path="/home/x/.cloudflared/new-tunnel-id.json",
    )
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600


def test_setup_persistent_tunnel_skips_login_when_already_logged_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "run_cloudflared_login", lambda binary: (_ for _ in ()).throw(AssertionError("no login")))
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    monkeypatch.setattr(pt, "create_tunnel", lambda binary, name: ("id-1", "/c.json"))
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: None)
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda binary, ref, port, cred: object())

    pt.setup_persistent_tunnel(
        port=8788,
        hostname="mcp.example.com",
        existing_state=None,
        state_path=tmp_path / "state.json",
        binary="cloudflared",
        narrate=lambda _msg: None,
    )  # must not raise


def test_setup_persistent_tunnel_reuses_found_tunnel_skips_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: ("found-id", "/found/creds.json"))
    monkeypatch.setattr(
        pt, "create_tunnel", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("create should be skipped"))
    )
    routed: list[str] = []
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: routed.append(ref))
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda binary, ref, port, cred: object())

    state_path = tmp_path / "state.json"
    pt.setup_persistent_tunnel(
        port=8788,
        hostname="mcp.example.com",
        existing_state=None,
        state_path=state_path,
        binary="cloudflared",
        narrate=lambda _msg: None,
    )
    # route_dns is called with the tunnel's per-hostname name (accepted by `route
    # dns` just like an ID would be), not the resolved UUID — see setup_persistent_tunnel.
    assert routed == [pt.tunnel_name_for("mcp.example.com")]
    saved = pt.load_tunnel_state(state_path)
    assert saved is not None
    assert saved.tunnel_id == "found-id"
    assert saved.credentials_path == "/found/creds.json"


# ── CLI wiring ───────────────────────────────────────────────────────────────
def test_persistent_conflicts_with_no_tunnel() -> None:
    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--no-tunnel"])
    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_reset_tunnel_requires_persistent() -> None:
    result = CliRunner().invoke(chatgpt_group, ["serve", "--reset-tunnel"])
    assert result.exit_code != 0
    assert "requires --persistent" in result.output


def test_persistent_first_run_without_hostname_errors_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    calls: list[Any] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(("run", a, kw)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append(("popen", a, kw)))
    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent"])
    assert result.exit_code != 0
    assert "needs --hostname" in result.output
    assert calls == []


def test_persistent_first_run_with_hostname_full_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")

    call_order: list[str] = []
    monkeypatch.setattr(pt, "is_logged_in", lambda: (call_order.append("is_logged_in"), True)[1])
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: (call_order.append("find"), None)[1])

    def _fake_create(binary: str, name: str) -> tuple[str, str]:
        call_order.append("create")
        assert binary == "/usr/bin/cloudflared"
        assert name == pt.tunnel_name_for("mcp.example.com")
        return "fresh-id", "/home/x/.cloudflared/fresh-id.json"

    monkeypatch.setattr(pt, "create_tunnel", _fake_create)

    routed: list[tuple[str, str]] = []

    def _fake_route(binary: str, ref: str, hostname: str) -> None:
        call_order.append("route")
        routed.append((ref, hostname))

    monkeypatch.setattr(pt, "route_dns", _fake_route)

    proc = _FakeTunnelProc()

    def _fake_start(binary: str, ref: str, port: int, cred: str) -> Any:
        call_order.append("run")
        return proc

    monkeypatch.setattr(pt, "start_named_tunnel_process", _fake_start)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    result = CliRunner().invoke(
        chatgpt_group, ["serve", "--persistent", "--hostname", "mcp.example.com", "--port", "9001"]
    )
    assert result.exit_code == 0, result.output
    assert call_order == ["is_logged_in", "find", "create", "route", "run"]
    assert routed == [(pt.tunnel_name_for("mcp.example.com"), "mcp.example.com")]
    assert proc.terminated  # cleaned up via the same finally-block lifecycle as the quick tunnel

    state_path = pt.tunnel_state_path_for("mcp.example.com")
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600
    saved = pt.load_tunnel_state(state_path)
    assert saved == pt.TunnelState(
        tunnel_name=pt.tunnel_name_for("mcp.example.com"),
        tunnel_id="fresh-id",
        hostname="mcp.example.com",
        credentials_path="/home/x/.cloudflared/fresh-id.json",
    )


def test_persistent_second_run_uses_saved_state_skips_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")

    state_path = pt.tunnel_state_path_for("mcp.example.com")
    pt.save_tunnel_state(
        state_path,
        pt.TunnelState(
            tunnel_name=pt.tunnel_name_for("mcp.example.com"),
            tunnel_id="saved-id",
            hostname="mcp.example.com",
            credentials_path="/saved/creds.json",
        ),
    )

    def _boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("setup steps must be skipped on a second run with saved state")

    monkeypatch.setattr(pt, "is_logged_in", _boom)
    monkeypatch.setattr(pt, "find_existing_tunnel", _boom)
    monkeypatch.setattr(pt, "create_tunnel", _boom)
    monkeypatch.setattr(pt, "route_dns", _boom)

    started: list[tuple[str, str, int, str]] = []
    proc = _FakeTunnelProc()

    def _fake_start(binary: str, ref: str, port: int, cred: str) -> Any:
        started.append((binary, ref, port, cred))
        return proc

    monkeypatch.setattr(pt, "start_named_tunnel_process", _fake_start)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    # No --hostname needed: read from persisted state.
    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--port", "9002"])
    assert result.exit_code == 0, result.output
    assert started == [("/usr/bin/cloudflared", "saved-id", 9002, "/saved/creds.json")]


def test_persistent_finds_existing_tunnel_skips_create(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The "create fails because already exists" scenario, realized as a
    check-before-create: `find_existing_tunnel` matches, so `create_tunnel`
    is never invoked at all."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")

    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: ("existing-id", "/existing/creds.json"))
    monkeypatch.setattr(
        pt, "create_tunnel", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("create should be skipped"))
    )
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: None)
    proc = _FakeTunnelProc()
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda binary, ref, port, cred: proc)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--hostname", "mcp.example.com"])
    assert result.exit_code == 0, result.output
    saved = pt.load_tunnel_state(pt.tunnel_state_path_for("mcp.example.com"))
    assert saved is not None
    assert saved.tunnel_id == "existing-id"


def test_second_hostname_gets_its_own_tunnel_and_leaves_the_first_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bug this isolation exists for: serving project B must not re-point
    project A's hostname onto a shared tunnel — Cloudflare treats two
    cloudflared processes on one tunnel id as replicas and load-balances edge
    traffic across them, landing A's requests on B's local port."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    pt.save_tunnel_state(
        pt.tunnel_state_path_for("a.example.com"),
        pt.TunnelState(
            tunnel_name=pt.tunnel_name_for("a.example.com"),
            tunnel_id="tunnel-a",
            hostname="a.example.com",
            credentials_path="/a.json",
        ),
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    created: list[str] = []
    monkeypatch.setattr(pt, "create_tunnel", lambda binary, name: (created.append(name), ("tunnel-b", "/b.json"))[1])
    routed: list[tuple[str, str]] = []
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: routed.append((ref, hostname)))
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda binary, ref, port, cred: _FakeTunnelProc())
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--hostname", "b.example.com"])
    assert result.exit_code == 0, result.output
    assert created == [pt.tunnel_name_for("b.example.com")]  # its own tunnel, not A's
    assert routed == [(pt.tunnel_name_for("b.example.com"), "b.example.com")]  # A's DNS untouched

    a_state = pt.load_tunnel_state(pt.tunnel_state_path_for("a.example.com"))
    assert a_state is not None and a_state.tunnel_id == "tunnel-a"  # A's state survived B's run
    b_state = pt.load_tunnel_state(pt.tunnel_state_path_for("b.example.com"))
    assert b_state is not None and b_state.tunnel_id == "tunnel-b"


def test_persistent_oauth_store_is_scoped_per_hostname(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Concurrent connectors must not share one OAuth file: every mutation
    rewrites the whole file, so a shared store is last-writer-wins."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    monkeypatch.setattr(pt, "create_tunnel", lambda binary, name: ("id-x", "/x.json"))
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: None)
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda binary, ref, port, cred: _FakeTunnelProc())
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    seen: list[Path] = []
    import lemoncrow.gateway.cli.commands.chatgpt as chatgpt_mod

    real_create = chatgpt_mod.__dict__.get("create_protected_mcp_app")
    assert real_create is None  # imported inside the command, so patch at the source module
    from lemoncrow.gateway.adapters import mcp_oauth

    original = mcp_oauth.create_protected_mcp_app

    def _spy(*, pairing_code: str, state_path: Path) -> Any:
        seen.append(state_path)
        return original(pairing_code=pairing_code, state_path=state_path)

    monkeypatch.setattr(mcp_oauth, "create_protected_mcp_app", _spy)

    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--hostname", "a.example.com"])
    assert result.exit_code == 0, result.output
    assert seen == [tmp_path / ".lemoncrow" / "chatgpt" / "oauth-a-example-com.json"]


def test_legacy_oauth_store_migrates_to_the_first_persistent_hostname(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A connector paired before the per-hostname split must keep working:
    its client_id lives in the shared ``oauth.json``, so that file moves onto
    the first hostname served instead of being left orphaned (which shows up
    in ChatGPT as "Unknown client_id")."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    legacy = tmp_path / ".lemoncrow" / "chatgpt" / "oauth.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"clients": {"cid-1": {"client_id": "cid-1"}}}), encoding="utf-8")

    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    monkeypatch.setattr(pt, "create_tunnel", lambda binary, name: ("id-x", "/x.json"))
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: None)
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda binary, ref, port, cred: _FakeTunnelProc())
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--hostname", "a.example.com"])
    assert result.exit_code == 0, result.output
    migrated = tmp_path / ".lemoncrow" / "chatgpt" / "oauth-a-example-com.json"
    assert json.loads(migrated.read_text(encoding="utf-8"))["clients"]["cid-1"]["client_id"] == "cid-1"
    assert not legacy.exists()  # moved, not copied — no token bleed into a second connector's store


def test_persistent_without_hostname_errors_when_several_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    for host, tid in (("a.example.com", "id-a"), ("b.example.com", "id-b")):
        pt.save_tunnel_state(
            pt.tunnel_state_path_for(host),
            pt.TunnelState(tunnel_name=pt.tunnel_name_for(host), tunnel_id=tid, hostname=host, credentials_path="/c"),
        )
    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent"])
    assert result.exit_code != 0
    assert "several hostnames are configured" in result.output
    assert "a.example.com" in result.output and "b.example.com" in result.output


def test_legacy_shared_state_migrates_to_its_hostname_keeping_the_same_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    legacy = pt.default_tunnel_state_dir() / "state.json"
    pt.save_tunnel_state(
        legacy,
        pt.TunnelState(
            tunnel_name=pt.LEGACY_TUNNEL_NAME,
            tunnel_id="legacy-id",
            hostname="old.example.com",
            credentials_path="/legacy.json",
        ),
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")

    def _boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("migrated state must be reused, not reconfigured")

    monkeypatch.setattr(pt, "is_logged_in", _boom)
    monkeypatch.setattr(pt, "find_existing_tunnel", _boom)
    monkeypatch.setattr(pt, "create_tunnel", _boom)
    monkeypatch.setattr(pt, "route_dns", _boom)
    started: list[str] = []
    monkeypatch.setattr(
        pt, "start_named_tunnel_process", lambda binary, ref, port, cred: (started.append(ref), _FakeTunnelProc())[1]
    )
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    # No --hostname: the migrated state is the only one configured.
    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent"])
    assert result.exit_code == 0, result.output
    assert started == ["legacy-id"]  # same Cloudflare tunnel as before the migration
    assert not legacy.exists()
    migrated = pt.load_tunnel_state(pt.tunnel_state_path_for("old.example.com"))
    assert migrated is not None and migrated.tunnel_name == pt.LEGACY_TUNNEL_NAME


def test_reset_tunnel_reconfigures_that_hostname_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    state_path = pt.tunnel_state_path_for("old.example.com")
    pt.save_tunnel_state(
        state_path,
        pt.TunnelState(
            tunnel_name=pt.tunnel_name_for("old.example.com"),
            tunnel_id="old-id",
            hostname="old.example.com",
            credentials_path="/c.json",
        ),
    )
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    monkeypatch.setattr(pt, "create_tunnel", lambda binary, name: ("new-id", "/new.json"))
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: None)
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda binary, ref, port, cred: _FakeTunnelProc())
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--reset-tunnel"])
    assert result.exit_code == 0, result.output
    assert "Reset persistent-tunnel state" in result.output
    reconfigured = pt.load_tunnel_state(state_path)
    assert reconfigured is not None and reconfigured.tunnel_id == "new-id"  # same hostname, fresh tunnel


def test_reset_tunnel_without_hostname_and_nothing_configured_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--reset-tunnel"])
    assert result.exit_code != 0
    assert "needs --hostname" in result.output


def test_persistent_banner_shows_stable_hostname_not_quick_tunnel_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    monkeypatch.setattr(pt, "create_tunnel", lambda binary, name: ("banner-id", "/b.json"))
    monkeypatch.setattr(pt, "route_dns", lambda binary, ref, hostname: None)
    monkeypatch.setattr(pt, "start_named_tunnel_process", lambda binary, ref, port, cred: _FakeTunnelProc())
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)

    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--hostname", "mcp.example.com"])
    assert result.exit_code == 0, result.output
    assert "MCP server URL for ChatGPT:  https://mcp.example.com/mcp" in result.output
    assert "stable — this URL does not change across restarts" in result.output
    assert "rotates" not in result.output
    assert "trycloudflare.com" not in result.output


def test_persistent_setup_failure_exits_1_with_clear_message_no_half_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.chatgpt._resolve_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(pt, "is_logged_in", lambda: True)
    monkeypatch.setattr(pt, "find_existing_tunnel", lambda binary, name: None)
    monkeypatch.setattr(
        pt, "create_tunnel", lambda binary, name: (_ for _ in ()).throw(pt.TunnelSetupError("boom: create failed"))
    )
    served: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: served.append(True))

    result = CliRunner().invoke(chatgpt_group, ["serve", "--persistent", "--hostname", "mcp.example.com"])
    assert result.exit_code == 1
    assert "boom: create failed" in result.output
    assert served == []  # never reaches uvicorn.run — no half-serving
    assert pt.load_tunnel_state(pt.tunnel_state_path_for("mcp.example.com")) is None  # nothing persisted on failure
