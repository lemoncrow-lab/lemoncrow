"""``lc runtime mcp serve|client|service|gateway``: remote MCP owned by the client.

No LemonCrow server and no full package: the gateway runs inside the client, the
tunnel is cloudflared, and the persistent units run ``python -m lemoncrow_client``.
Supervisor commands are recorded, never executed; cloudflared is never run.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.cli import main as client_main
from lemoncrow_client.remote import cli as remote_cli
from lemoncrow_client.remote import service, tunnel
from lemoncrow_client.remote.gateway import GatewayServer, runtime_mode
from lemoncrow_client.remote.state import (
    ConnectorBinding,
    connector_file,
    connector_slug,
    load_or_create_pairing_code,
    pairing_path,
    save_connector,
    state_path,
)

HOSTNAME = "lc-cli.example.com"


def _closed_port_url() -> str:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "alpha.py").write_text("def alpha():\n    return 'widget'\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "i"],
        check=True,
        env=env,
    )
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "home"
    target.mkdir()
    monkeypatch.setenv("HOME", str(target))
    for name in ("LEMONCROW_ROOT", "LEMONCROW_HOME", "LEMONCROW_INSTALL_MODE", "LEMONCROW_MCP_GATEWAY_PORT"):
        monkeypatch.delenv(name, raising=False)
    return target


@pytest.fixture
def store_root(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = home / ".lemoncrow"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_HOME", str(root))
    monkeypatch.setenv("LEMONCROW_URL", _closed_port_url())
    return root.resolve()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(target)
    return target.resolve()


class _Supervisor:
    """Records supervisor commands instead of running systemctl/launchctl."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def supervisor(monkeypatch: pytest.MonkeyPatch) -> _Supervisor:
    recorder = _Supervisor()
    monkeypatch.setattr(service, "_run", recorder)
    monkeypatch.setattr(service, "supervisor_kind", lambda: "systemd")
    monkeypatch.setattr(service, "_is_linux", lambda: True)
    return recorder


def _run(arguments: list[str]) -> tuple[int, str]:
    sink = io.StringIO()
    code = client_main(["runtime", "mcp", *arguments], stdout=sink)
    return code, sink.getvalue()


def _post(port: int, payload: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        connection.request(
            "POST",
            "/mcp",
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Host": "random-words.trycloudflare.com", **(headers or {})},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


_INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def test_serve_no_tunnel_runs_runtime_lite_for_the_working_tree(
    store_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def probe(server: GatewayServer, tunnel_process: object, sink: object) -> int:
        seen["tunnel"] = tunnel_process
        server.start()
        # Any Host reaches the one served workspace; OAuth still gates /mcp.
        seen["refused"] = _post(server.port, _INITIALIZE)[0]
        return 0

    monkeypatch.setattr(remote_cli, "_block", probe)
    code, output = _run(["serve", "--no-tunnel"])
    assert code == 0, output
    assert seen == {"tunnel": None, "refused": 401}
    pairing = load_or_create_pairing_code(pairing_path(store_root))
    assert pairing in output and "/mcp" in output


def test_serve_no_auth_answers_mcp_from_runtime_lite(
    store_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def probe(server: GatewayServer, tunnel_process: object, sink: object) -> int:
        server.start()
        status, raw = _post(server.port, _INITIALIZE)
        seen["status"] = status
        seen["server"] = json.loads(raw)["result"]["serverInfo"]["name"]
        return 0

    monkeypatch.setattr(remote_cli, "_block", probe)
    code, output = _run(["serve", "--no-tunnel", "--no-auth"])
    assert code == 0, output
    assert seen == {"status": 200, "server": "lemoncrow-runtime-lite"}
    assert "NO AUTHENTICATION" in output


def test_serve_no_auth_never_opens_a_public_tunnel(
    store_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[object] = []
    monkeypatch.setattr(tunnel, "resolve_cloudflared", lambda _root: started.append("resolve") or "/bin/cloudflared")
    monkeypatch.setattr(tunnel, "start_quick_tunnel", lambda *_args: started.append("start") or (None, None))
    seen: dict[str, Any] = {}

    def probe(server: GatewayServer, tunnel_process: object, sink: object) -> int:
        seen["tunnel"] = tunnel_process
        return 0

    monkeypatch.setattr(remote_cli, "_block", probe)
    code, output = _run(["serve", "--no-auth"])
    assert code == 0, output
    assert started == [] and seen == {"tunnel": None}
    assert "Loopback only" in output
    assert "cloudflared tunnel --url" not in output

    code, output = _run(["serve", "--no-auth", "--tunnel"])
    assert code != 0
    assert "--no-auth is loopback only" in output
    assert started == []


def test_direct_loopback_discovery_keeps_the_port(
    store_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def probe(server: GatewayServer, tunnel_process: object, sink: object) -> int:
        server.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
        try:
            connection.request("GET", "/.well-known/oauth-authorization-server")
            seen["metadata"] = json.loads(connection.getresponse().read())
        finally:
            connection.close()
        seen["origin"] = f"http://127.0.0.1:{server.port}"
        return 0

    monkeypatch.setattr(remote_cli, "_block", probe)
    code, output = _run(["serve", "--no-tunnel"])
    assert code == 0, output
    assert seen["metadata"]["issuer"] == seen["origin"]
    assert seen["metadata"]["token_endpoint"] == f"{seen['origin']}/token"


@pytest.mark.parametrize("code", ["", "   ", "short"])
def test_serve_refuses_a_guessable_one_off_pairing_code(store_root: Path, repo: Path, code: str) -> None:
    status, output = _run(["serve", "--no-tunnel", "--pairing-code", code])
    assert status == 2
    assert "at least 8" in output


def test_serve_accepts_the_explicit_tunnel_flag(store_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "resolve_cloudflared", lambda store_root: None)
    code, output = _run(["serve", "--tunnel"])
    assert code == 1 and "cloudflared is required" in output


def test_serve_refuses_a_non_loopback_bind(store_root: Path, repo: Path) -> None:
    code, output = _run(["serve", "--no-tunnel", "--host", "192.0.2.1"])
    assert code == 2
    assert "loopback" in output


def test_quick_tunnel_death_stops_the_gateway(store_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _DeadTunnel:
        returncode = 3

        def wait(self, timeout: float | None = None) -> int:
            return 3

        def poll(self) -> int:
            return 3

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    monkeypatch.setattr(tunnel, "resolve_cloudflared", lambda store_root: "/opt/cloudflared")
    monkeypatch.setattr(
        tunnel,
        "start_quick_tunnel",
        lambda binary, port, timeout=30.0: (_DeadTunnel(), "https://random-words.trycloudflare.com"),
    )
    code, output = _run(["serve"])
    assert code == 1
    assert "https://random-words.trycloudflare.com/mcp" in output
    assert "tunnel process exited" in output


def test_serve_without_cloudflared_explains_how_to_get_it(
    store_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tunnel, "resolve_cloudflared", lambda store_root: None)
    code, output = _run(["serve"])
    assert code == 1
    assert "cloudflared" in output and "--no-tunnel" in output


def _fake_tunnel(monkeypatch: pytest.MonkeyPatch, store_root: Path) -> Path:
    credentials = store_root.parent / ".cloudflared" / "tid.json"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("{}", encoding="utf-8")
    state = tunnel.TunnelState(
        tunnel_name="lemoncrow-mcp", tunnel_id="tid", hostname="*", credentials_path=str(credentials)
    )

    def provision(*, store_root: Path, hostname: str, binary: str, narrate: Any, reset: bool = False) -> Any:
        tunnel.save_tunnel_state(tunnel.tunnel_state_path(store_root), state)
        return state

    monkeypatch.setattr(tunnel, "resolve_cloudflared", lambda store_root: "/opt/cloudflared")
    monkeypatch.setattr(tunnel, "provision_shared_tunnel", provision)
    return credentials


def test_persistent_installs_units_that_run_the_client_gateway(
    store_root: Path, repo: Path, home: Path, supervisor: _Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tunnel(monkeypatch, store_root)
    code, output = _run(["serve", "--persistent", "--hostname", HOSTNAME])
    assert code == 0, output

    assert json.loads(connector_file(store_root, HOSTNAME).read_text())["workspace"] == str(repo)
    pairing = load_or_create_pairing_code(pairing_path(store_root, connector_slug(HOSTNAME)))
    assert pairing in output and f"https://{HOSTNAME}/mcp" in output

    units = home / ".config" / "systemd" / "user"
    gateway_unit = (units / "lemoncrow-mcp-gateway.service").read_text()
    assert f"{sys.executable} -m lemoncrow_client runtime mcp gateway --port 7421" in gateway_unit
    assert f"LEMONCROW_HOME={store_root}" in gateway_unit
    assert "lemoncrow_server_core" not in gateway_unit and "mcp-gateway" not in gateway_unit.split("ExecStart")[1]
    tunnel_unit = (units / "lemoncrow-mcp-tunnel.service").read_text()
    assert "/opt/cloudflared tunnel --no-autoupdate" in tunnel_unit and "http://127.0.0.1:7421" in tunnel_unit
    assert ["systemctl", "--user", "restart", "lemoncrow-mcp-gateway.service"] in supervisor.commands


def test_repair_migrates_a_server_gateway_unit_to_the_client(
    store_root: Path, repo: Path, home: Path, supervisor: _Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = _fake_tunnel(monkeypatch, store_root)
    tunnel.save_tunnel_state(
        tunnel.tunnel_state_path(store_root),
        tunnel.TunnelState("lemoncrow-mcp", "tid", "*", str(credentials)),
    )
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(repo)), store_root=store_root)
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    legacy = units / "lemoncrow-mcp-gateway.service"
    legacy.write_text("[Service]\nExecStart=/py -m lemoncrow_server_core mcp-gateway --port 7421 --backend-port 7420\n")

    code, output = _run(["service", "repair"])
    assert code == 0, output
    assert "-m lemoncrow_client runtime mcp gateway --port 7421" in legacy.read_text()
    assert ["systemctl", "--user", "restart", "lemoncrow-mcp-gateway.service"] in supervisor.commands


def test_repair_without_connectors_changes_nothing(store_root: Path, supervisor: _Supervisor) -> None:
    code, output = _run(["service", "repair"])
    assert code == 0
    assert "nothing to repair" in output
    assert supervisor.commands == []


def test_service_code_restart_and_remove(
    store_root: Path, repo: Path, home: Path, supervisor: _Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(repo)), store_root=store_root)
    scope = connector_slug(HOSTNAME)
    first = load_or_create_pairing_code(pairing_path(store_root, scope))
    code, output = _run(["service", "code"])
    assert (code, output.strip()) == (0, first)

    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    for name in ("lemoncrow-mcp-gateway.service", "lemoncrow-mcp-tunnel.service"):
        (units / name).write_text("[Service]\n")
    code, output = _run(["service", "restart", HOSTNAME, "--new-pairing-code"])
    assert code == 0, output
    rotated = load_or_create_pairing_code(pairing_path(store_root, scope))
    assert rotated != first and rotated in output
    assert ["systemctl", "--user", "restart", "lemoncrow-mcp-tunnel.service"] in supervisor.commands

    code, output = _run(["service", "remove", HOSTNAME])
    assert code == 0, output
    assert not connector_file(store_root, HOSTNAME).exists()
    assert not pairing_path(store_root, scope).exists()
    assert not (units / "lemoncrow-mcp-gateway.service").exists()


def test_service_uninstall_removes_units_and_keeps_connector_state(
    store_root: Path, repo: Path, home: Path, supervisor: _Supervisor
) -> None:
    save_connector(ConnectorBinding(hostname=HOSTNAME, workspace=str(repo)), store_root=store_root)
    pairing = load_or_create_pairing_code(pairing_path(store_root, connector_slug(HOSTNAME)))
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    for name in ("lemoncrow-mcp-gateway.service", "lemoncrow-mcp-tunnel.service"):
        (units / name).write_text("[Service]\n")

    code, output = _run(["service", "uninstall"])
    assert code == 0, output
    assert not list(units.iterdir())
    assert ["systemctl", "--user", "disable", "--now", "lemoncrow-mcp-gateway.service"] in supervisor.commands
    # A later reinstall restores the same connector with `service repair`.
    assert connector_file(store_root, HOSTNAME).exists()
    assert load_or_create_pairing_code(pairing_path(store_root, connector_slug(HOSTNAME))) == pairing

    code, output = _run(["service", "uninstall"])
    assert code == 0 and "No persistent MCP services" in output


def test_client_command_mints_one_stable_public_client(store_root: Path) -> None:
    code, first = _run(["client", "--hostname", HOSTNAME])
    assert code == 0, first
    code, second = _run(["client", "--hostname", HOSTNAME])
    client_id = json.loads(state_path(store_root, connector_slug(HOSTNAME)).read_text())
    assert first == second
    assert any(row["client_id"] in first for row in client_id["clients"].values())
    assert "https://claude.ai/api/mcp/auth_callback" in first
    code, output = _run(["client", "--redirect-uri", "http://example.com/cb"])
    assert code == 2 and "https" in output


def test_hosted_mode_comes_from_the_runtime_settings_file(store_root: Path) -> None:
    (store_root / "runtime").mkdir()
    (store_root / "runtime" / "env").write_text("LEMONCROW_INSTALL_MODE=hosted\n", encoding="utf-8")
    # A supervised gateway inherits no shell environment, only LEMONCROW_HOME.
    assert runtime_mode({"LEMONCROW_HOME": str(store_root)}) == "hosted"
    assert runtime_mode({"LEMONCROW_HOME": str(store_root), "LEMONCROW_INSTALL_MODE": "lite"}) == "lite"
    assert runtime_mode({"LEMONCROW_HOME": str(store_root / "missing")}) == "lite"


def test_gateway_mode_matches_the_stdio_runtime_for_a_legacy_hosted_install(store_root: Path) -> None:
    from lemoncrow_client.config import load_config

    (store_root / "runtime").mkdir()
    (store_root / "runtime" / "env").write_text("LEMONCROW_URL=https://lemoncrow.example\n", encoding="utf-8")
    environment = {"LEMONCROW_HOME": str(store_root), "HOME": str(store_root)}

    assert load_config(dict(environment), product="runtime").install_mode == "hosted"
    assert runtime_mode(environment) == "hosted"
    loopback = {**environment, "LEMONCROW_URL": "http://127.0.0.1:7420"}
    assert runtime_mode(loopback) == "lite"


def test_quick_tunnel_url_ignores_the_control_plane() -> None:
    assert tunnel.extract_tunnel_url("INF |  https://calm-river-12.trycloudflare.com   |") == (
        "https://calm-river-12.trycloudflare.com"
    )
    assert tunnel.extract_tunnel_url("failed to request quick Tunnel: https://api.trycloudflare.com") is None
    assert tunnel.extract_tunnel_url("see https://developers.cloudflare.com/") is None


def test_default_tunnel_name_derives_from_hostname() -> None:
    assert tunnel.default_tunnel_name(hostname="My-MacBook-Pro.local") == "lemoncrow-mcp-my-macbook-pro-local"
    assert tunnel.default_tunnel_name(hostname="pankaj_rog") == "lemoncrow-mcp-pankaj-rog"
    assert tunnel.default_tunnel_name(hostname="") == "lemoncrow-mcp"


def test_default_tunnel_name_respects_environment() -> None:
    env = {"LEMONCROW_MCP_TUNNEL_NAME": "custom-tunnel-name"}
    assert tunnel.default_tunnel_name(environment=env) == "custom-tunnel-name"


def test_shared_tunnel_is_created_once_and_every_hostname_is_routed(store_root: Path, home: Path) -> None:
    (home / ".cloudflared").mkdir()
    (home / ".cloudflared" / "cert.pem").write_text("cert", encoding="utf-8")
    calls = home / "calls.log"
    fake = home / "cloudflared"
    expected_name = tunnel.default_tunnel_name()
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        'case "$2" in\n'
        "  list) echo '[]' ;;\n"
        f"  create) echo 'Created tunnel {expected_name} with id abc-123'; "
        f"echo 'Tunnel credentials written to {home}/.cloudflared/abc-123.json.'; "
        f"touch '{home}/.cloudflared/abc-123.json' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    seen: list[str] = []

    first = tunnel.provision_shared_tunnel(
        store_root=store_root, hostname="a.example.com", binary=str(fake), narrate=seen.append
    )
    second = tunnel.provision_shared_tunnel(
        store_root=store_root, hostname="b.example.com", binary=str(fake), narrate=seen.append
    )
    assert first == second == tunnel.load_tunnel_state(tunnel.tunnel_state_path(store_root))
    assert first.tunnel_id == "abc-123"
    assert calls.read_text().splitlines() == [
        f"tunnel list --name {expected_name} -o json",
        f"tunnel create {expected_name}",
        "tunnel route dns --overwrite-dns abc-123 a.example.com",
        "tunnel route dns --overwrite-dns abc-123 b.example.com",
    ]
    assert oct(tunnel.tunnel_state_path(store_root).stat().st_mode & 0o777) == "0o600"


def test_dns_route_rejects_wrong_cloudflare_zone_without_rewriting_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "_certificate_dns_zone", lambda: None)
    commands: list[tuple[str, ...]] = []

    def cloudflared(binary: str, *args: str) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(
            [binary, *args],
            0,
            "",
            "2026-11-04T01:40:27Z INF lc.olaryn.com.beseam.com is already configured to route to your tunnel",
        )

    monkeypatch.setattr(tunnel, "_cloudflared", cloudflared)
    with pytest.raises(tunnel.TunnelSetupError, match="instead of"):
        tunnel.route_dns("cloudflared", "tunnel-123", "lc.olaryn.com")
    assert commands == [("tunnel", "route", "dns", "--overwrite-dns", "tunnel-123", "lc.olaryn.com")]


def test_dns_route_refuses_wrong_certificate_zone_before_creating_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "_certificate_dns_zone", lambda: "beseam.com")
    called = []
    monkeypatch.setattr(tunnel, "_cloudflared", lambda *args: called.append(args))
    with pytest.raises(tunnel.TunnelSetupError, match="no DNS record was created"):
        tunnel.route_dns("cloudflared", "tunnel-123", "lc.olaryn.com")
    assert not called


def test_dns_route_accepts_exact_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "_certificate_dns_zone", lambda: None)
    monkeypatch.setattr(
        tunnel,
        "_cloudflared",
        lambda binary, *args: subprocess.CompletedProcess(
            [binary, *args], 0, "", "INF lc.olaryn.com is already configured to route to your tunnel"
        ),
    )
    tunnel.route_dns("cloudflared", "tunnel-123", "lc.olaryn.com")


def test_gateway_command_refuses_a_public_bind(store_root: Path) -> None:
    code, output = _run(["gateway", "--host", "192.0.2.1"])
    assert code == 2 and "loopback" in output


def test_systemd_units_escape_specifiers_and_variables() -> None:
    units = service.render_systemd_units(
        gateway=["/opt/100%/python", "-m", "lemoncrow_client", "--label", "$HOME and spaces"],
        tunnel_command=["/opt/cloudflared", "tunnel", "run", "50%"],
        pinned={"LEMONCROW_HOME": "/data/50%/state"},
    )
    gateway_unit = units[service.GATEWAY_UNIT]
    assert 'ExecStart="/opt/100%%/python" -m lemoncrow_client --label "$$HOME and spaces"\n' in gateway_unit
    assert 'Environment="LEMONCROW_HOME=/data/50%%/state"\n' in gateway_unit
    assert 'ExecStart=/opt/cloudflared tunnel run "50%%"\n' in units[service.TUNNEL_UNIT]


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("logged_in", [False, True])
def test_shared_tunnel_recovers_missing_credentials(store_root, home, monkeypatch, cached, logged_in):
    cert = home / ".cloudflared/cert.pem"
    cert.parent.mkdir()
    cert.write_text("cert")
    credentials = cert.parent / "abc-123.json"
    tunnel_name = "lemoncrow-mcp" if cached else tunnel.default_tunnel_name()
    state = tunnel.TunnelState(tunnel_name, "abc-123", "*", str(credentials))
    if cached:
        tunnel.save_tunnel_state(tunnel.tunnel_state_path(store_root), state)
    login_calls = []
    if not logged_in:
        cert.unlink()

    def login(command, **kwargs):
        login_calls.append(command)
        cert.write_text("cert")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(tunnel.subprocess, "run", login)
    calls = []
    secret = {"AccountTag": "account", "TunnelID": "abc-123", "TunnelSecret": "private-secret"}

    def cloudflared(binary, *args):
        calls.append(args)
        if args[1] == "list":
            return subprocess.CompletedProcess([], 0, '[{"id":"abc-123"}]', "")
        assert args[:3] == ("tunnel", "token", "--cred-file")
        Path(args[3]).write_text(json.dumps(secret))
        return subprocess.CompletedProcess([], 0, "private-secret", "")

    monkeypatch.setattr(tunnel, "_cloudflared", cloudflared)
    routed = []
    monkeypatch.setattr(tunnel, "route_dns", lambda *args: routed.append(args))
    messages = []
    for _ in range(2):
        assert (
            tunnel.provision_shared_tunnel(
                store_root=store_root, hostname="lc.example.com", binary="cloudflared", narrate=messages.append
            )
            == state
        )
    assert login_calls == ([] if logged_in else [["cloudflared", "tunnel", "login"]])
    assert json.loads(credentials.read_text()) == secret
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert sum(args[1] == "token" for args in calls) == 1
    assert len(routed) == 2
    assert "private-secret" not in "".join(messages)
    assert sorted(p.name for p in cert.parent.iterdir()) == ["abc-123.json", "cert.pem"]


@pytest.mark.parametrize("failure", ["exit", "missing", "invalid", "wrong-tunnel"])
def test_credential_recovery_failure_preserves_tunnel(store_root, home, monkeypatch, failure):
    cert = home / ".cloudflared/cert.pem"
    cert.parent.mkdir()
    cert.write_text("cert")
    credentials = cert.parent / "abc-123.json"
    state = tunnel.TunnelState("lemoncrow-mcp", "abc-123", "*", str(credentials))
    path = tunnel.tunnel_state_path(store_root)
    tunnel.save_tunnel_state(path, state)

    def cloudflared(binary, *args):
        assert args[:3] == ("tunnel", "token", "--cred-file")
        if failure == "invalid":
            Path(args[3]).write_text("private-secret")
        elif failure == "wrong-tunnel":
            Path(args[3]).write_text(
                json.dumps({"AccountTag": "account", "TunnelID": "different", "TunnelSecret": "private-secret"})
            )
        return subprocess.CompletedProcess([], 1 if failure == "exit" else 0, "private-secret", "private-secret")

    monkeypatch.setattr(tunnel, "_cloudflared", cloudflared)
    monkeypatch.setattr(tunnel, "route_dns", lambda *args: pytest.fail("must not route DNS after failed recovery"))
    with pytest.raises(tunnel.TunnelSetupError, match="existing tunnel was preserved") as error:
        tunnel.provision_shared_tunnel(
            store_root=store_root, hostname="lc.example.com", binary="cloudflared", narrate=lambda _: None
        )
    assert "private-secret" not in str(error.value)
    assert not credentials.exists()
    assert tunnel.load_tunnel_state(path) == state
    assert [p.name for p in cert.parent.iterdir()] == ["cert.pem"]
