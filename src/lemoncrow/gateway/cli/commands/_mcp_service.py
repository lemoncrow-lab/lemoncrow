"""Supervise persistent remote MCP transport.

Persistent MCP has three machine-wide processes regardless of connector count:

* ``lemoncrow-local-server.service`` owns indexed/server intelligence on 7420.
* ``lemoncrow-mcp-gateway.service`` owns the thin-client HTTP/MCP surface on 7421.
* ``lemoncrow-mcp-tunnel.service`` carries configured public hostnames to 7421.

The gateway uses ``lemoncrow-client`` routing, so client-side tools stay usable
while the indexed backend restarts. Per-host state is data only: hostname ->
workspace binding and OAuth files. There is no LemonCrow process per connector.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lemoncrow.infra.runtime.daemon_units import (
    LAUNCHD_USER_DIR,
    MCP_LABEL,
    SYSTEMD_USER_DIR,
    _is_linux,
    _is_macos,
    _subprocess_output,
    _systemd_user_bus_unavailable,
)

SHARED_SYSTEMD_UNIT = "lemoncrow-mcp-tunnel.service"
SHARED_GATEWAY_SYSTEMD_UNIT = "lemoncrow-mcp-gateway.service"
SHARED_LAUNCHD_LABEL = f"{MCP_LABEL}.tunnel"
SHARED_GATEWAY_LAUNCHD_LABEL = f"{MCP_LABEL}.gateway"
_LEGACY_UNIT_GLOB = "lemoncrow-mcp-*.service"


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceInfo:
    """One public connector projected onto the shared tunnel runtime."""

    name: str
    slug: str
    hostname: str
    workspace: str
    state: str
    enabled: str
    pid: int | None = None

    @property
    def is_systemd(self) -> bool:
        return self.name.endswith(".service")


def supervisor_kind() -> str | None:
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("LEMONCROW_MCP_ALLOW_SERVICE", "") != "1":
        return None
    if _is_macos() and shutil.which("launchctl") is not None:
        return "launchd"
    if _is_linux() and shutil.which("systemctl") is not None:
        return "systemd"
    return None


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def _enable_linger(narrate: Callable[[str], None]) -> None:
    if shutil.which("loginctl") is None:
        return
    user = getpass.getuser()
    show = _run(["loginctl", "show-user", user, "--property=Linger"])
    if show.returncode == 0 and "Linger=yes" in show.stdout:
        return
    result = _run(["loginctl", "enable-linger", user])
    if result.returncode == 0:
        narrate("Enabled linger — the tunnel also runs while you are logged out.")
    else:
        narrate(f"Could not enable linger; run manually: sudo loginctl enable-linger {user}")


def _gateway_python() -> str:
    explicit = os.environ.get("LEMONCROW_SERVER_PYTHON", "").strip()
    if explicit:
        return explicit
    tool_dir = os.environ.get("LEMONCROW_TOOL_DIR", "").strip()
    if tool_dir:
        candidate = Path(tool_dir).expanduser() / "bin" / "python"
        if candidate.is_file():
            return str(candidate)
    home = os.environ.get("LEMONCROW_HOME", "").strip() or os.environ.get("LEMONCROW_ROOT", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".lemoncrow"
    candidate = root / "uv-tools" / "lemoncrow" / "bin" / "python"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def _gateway_command(*, origin_port: int, backend_port: int) -> list[str]:
    return [
        _gateway_python(),
        "-m",
        "lemoncrow_server_core",
        "mcp-gateway",
        "--port",
        str(origin_port),
        "--backend-port",
        str(backend_port),
    ]


def start_gateway_process(*, origin_port: int, backend_port: int) -> subprocess.Popen[str]:
    """Start the thin-client HTTP gateway as an independent foreground child."""
    return subprocess.Popen(_gateway_command(origin_port=origin_port, backend_port=backend_port), text=True)


def _tunnel_command(*, binary: str, tunnel_ref: str, credentials_path: str, origin_port: int) -> list[str]:
    return [
        binary,
        "tunnel",
        "--no-autoupdate",
        # Cloudflare's default is 30s. The origin server already owns request
        # draining, and a persistent tunnel is restarted independently, so a
        # short tunnel drain avoids turning every local server update into a
        # 30-second outage.
        "--grace-period",
        "2s",
        "run",
        "--credentials-file",
        credentials_path,
        "--url",
        f"http://127.0.0.1:{origin_port}",
        tunnel_ref,
    ]


def register_shared_tunnel_service(
    *,
    binary: str,
    tunnel_ref: str,
    credentials_path: str,
    origin_port: int,
    backend_port: int,
    root: Path,
    narrate: Callable[[str], None],
) -> str:
    """Install/restart the persistent thin-client gateway and Cloudflare tunnel."""
    kind = supervisor_kind()
    tunnel_command = _tunnel_command(
        binary=binary,
        tunnel_ref=tunnel_ref,
        credentials_path=credentials_path,
        origin_port=origin_port,
    )
    gateway_command = _gateway_command(origin_port=origin_port, backend_port=backend_port)
    if kind == "systemd":
        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        gateway_path = SYSTEMD_USER_DIR / SHARED_GATEWAY_SYSTEMD_UNIT
        tunnel_path = SYSTEMD_USER_DIR / SHARED_SYSTEMD_UNIT
        gateway_content = f"""[Unit]
Description=LemonCrow MCP thin-client gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={shlex.join(gateway_command)}
Restart=always
RestartSec=1
TimeoutStopSec=5

[Install]
WantedBy=default.target
"""
        tunnel_content = f"""[Unit]
Description=LemonCrow MCP shared tunnel
After=network-online.target {SHARED_GATEWAY_SYSTEMD_UNIT}
Wants=network-online.target {SHARED_GATEWAY_SYSTEMD_UNIT}

[Service]
Type=simple
ExecStart={shlex.join(tunnel_command)}
Restart=always
RestartSec=5
TimeoutStopSec=5

[Install]
WantedBy=default.target
"""
        gateway_changed = not gateway_path.exists() or gateway_path.read_text(encoding="utf-8") != gateway_content
        tunnel_changed = not tunnel_path.exists() or tunnel_path.read_text(encoding="utf-8") != tunnel_content
        if gateway_changed:
            gateway_path.write_text(gateway_content, encoding="utf-8")
            narrate(f"Installed {gateway_path}")
        if tunnel_changed:
            tunnel_path.write_text(tunnel_content, encoding="utf-8")
            narrate(f"Installed {tunnel_path}")
        if gateway_changed or tunnel_changed:
            result = _run(["systemctl", "--user", "daemon-reload"])
            if result.returncode != 0:
                output = _subprocess_output(result)
                if _systemd_user_bus_unavailable(output):
                    raise ServiceError("systemd user bus is unavailable; run with --foreground from a login session")
                raise ServiceError(f"systemctl --user daemon-reload failed: {output.strip()}")
        for unit in (SHARED_GATEWAY_SYSTEMD_UNIT, SHARED_SYSTEMD_UNIT):
            result = _run(["systemctl", "--user", "enable", unit])
            if result.returncode != 0:
                raise ServiceError(f"systemctl --user enable {unit} failed: {_subprocess_output(result).strip()}")
        for unit, changed in ((SHARED_GATEWAY_SYSTEMD_UNIT, gateway_changed), (SHARED_SYSTEMD_UNIT, tunnel_changed)):
            active = _run(["systemctl", "--user", "is-active", "--quiet", unit])
            if changed or active.returncode != 0:
                result = _run(["systemctl", "--user", "restart", unit])
                if result.returncode != 0:
                    raise ServiceError(f"systemctl --user restart {unit} failed: {_subprocess_output(result).strip()}")
        _enable_linger(narrate)
        return SHARED_SYSTEMD_UNIT

    if kind == "launchd":
        LAUNCHD_USER_DIR.mkdir(parents=True, exist_ok=True)
        log_dir = root / "mcp"
        log_dir.mkdir(parents=True, exist_ok=True)
        gateway_plist = LAUNCHD_USER_DIR / f"{SHARED_GATEWAY_LAUNCHD_LABEL}.plist"
        tunnel_plist = LAUNCHD_USER_DIR / f"{SHARED_LAUNCHD_LABEL}.plist"
        for plist, label, command, log_name in (
            (gateway_plist, SHARED_GATEWAY_LAUNCHD_LABEL, gateway_command, "gateway.log"),
            (tunnel_plist, SHARED_LAUNCHD_LABEL, tunnel_command, "tunnel.log"),
        ):
            with plist.open("wb") as handle:
                plistlib.dump(
                    {
                        "Label": label,
                        "ProgramArguments": command,
                        "RunAtLoad": True,
                        "KeepAlive": True,
                        "StandardOutPath": str(log_dir / log_name),
                        "StandardErrorPath": str(log_dir / log_name),
                    },
                    handle,
                )
            _run(["launchctl", "unload", str(plist)])
            result = _run(["launchctl", "load", str(plist)])
            if result.returncode != 0:
                raise ServiceError(f"launchctl load failed for {label}: {_subprocess_output(result).strip()}")
            narrate(f"Installed {plist}")
        return SHARED_LAUNCHD_LABEL

    raise ServiceError("no systemd/launchd user session available")


def repair_shared_tunnel_service(*, narrate: Callable[[str], None]) -> str | None:
    """Recreate the shared tunnel service from durable local state.

    Source and bundle installs replace the LemonCrow tool environment and the
    local server unit. Connector bindings, named-tunnel state, and Cloudflare
    credentials deliberately survive those installs. Reconcile the service
    from those durable files so reinstall cannot leave configured connectors
    offline merely because the supervisor unit disappeared.

    This never provisions Cloudflare resources or changes DNS.
    """
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.gateway.cli.commands._persistent_tunnel import load_shared_tunnel_state
    from lemoncrow.gateway.cli.commands.mcp_serve import (
        _persistent_backend_port,
        _persistent_origin_port,
        _resolve_cloudflared,
    )
    from lemoncrow.gateway.mcp_connectors import load_all_connectors

    if not load_all_connectors():
        return None
    state = load_shared_tunnel_state()
    if state is None:
        raise ServiceError("persistent MCP connectors exist but shared tunnel state is missing")
    credentials = Path(state.credentials_path).expanduser()
    if not credentials.is_file():
        raise ServiceError(f"shared tunnel credentials are missing: {credentials}")
    binary = _resolve_cloudflared()
    if binary is None:
        raise ServiceError("cloudflared is required to repair the persistent MCP tunnel")
    return register_shared_tunnel_service(
        binary=binary,
        tunnel_ref=state.tunnel_id,
        credentials_path=str(credentials.resolve()),
        origin_port=_persistent_origin_port(),
        backend_port=_persistent_backend_port(),
        root=default_store_root(),
        narrate=narrate,
    )


def installed_units() -> list[str]:
    return [unit for unit in (SHARED_GATEWAY_SYSTEMD_UNIT, SHARED_SYSTEMD_UNIT) if (SYSTEMD_USER_DIR / unit).exists()]


def installed_labels() -> list[str]:
    return [
        label
        for label in (SHARED_GATEWAY_LAUNCHD_LABEL, SHARED_LAUNCHD_LABEL)
        if (LAUNCHD_USER_DIR / f"{label}.plist").exists()
    ]


def shared_tunnel_service_state() -> tuple[str, str, int | None]:
    if _is_linux():
        if not (SYSTEMD_USER_DIR / SHARED_SYSTEMD_UNIT).exists():
            return "inactive", "disabled", None
        show = _run(
            [
                "systemctl",
                "--user",
                "show",
                SHARED_SYSTEMD_UNIT,
                "-p",
                "ActiveState",
                "-p",
                "UnitFileState",
                "-p",
                "MainPID",
            ]
        )
        props: dict[str, str] = {}
        for line in show.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props[key] = value.strip()
        raw_pid = props.get("MainPID", "0")
        pid = int(raw_pid) if raw_pid.isdigit() and raw_pid != "0" else None
        return props.get("ActiveState", "unknown"), props.get("UnitFileState", "unknown"), pid
    plist = LAUNCHD_USER_DIR / f"{SHARED_LAUNCHD_LABEL}.plist"
    if not plist.exists():
        return "inactive", "disabled", None
    listed = _run(["launchctl", "list", SHARED_LAUNCHD_LABEL])
    return ("active" if listed.returncode == 0 else "inactive", "enabled", None)


def describe_services() -> list[ServiceInfo]:
    """Project each connector onto the one shared runtime for status UIs."""
    from lemoncrow.gateway.mcp_connectors import connector_slug, load_all_connectors

    connectors = load_all_connectors()
    state, enabled, pid = shared_tunnel_service_state()
    name = SHARED_SYSTEMD_UNIT if _is_linux() else SHARED_LAUNCHD_LABEL
    return [
        ServiceInfo(
            name=name,
            slug=connector_slug(binding.hostname),
            hostname=binding.hostname,
            workspace=binding.workspace,
            state=state,
            enabled=enabled,
            pid=pid,
        )
        for binding in connectors
    ]


def control_shared_tunnel_service(action: str) -> None:
    if _is_linux():
        units = (
            (SHARED_SYSTEMD_UNIT, SHARED_GATEWAY_SYSTEMD_UNIT)
            if action == "stop"
            else (SHARED_GATEWAY_SYSTEMD_UNIT, SHARED_SYSTEMD_UNIT)
        )
        for unit in units:
            result = _run(["systemctl", "--user", action, unit])
            if result.returncode != 0:
                raise ServiceError(f"systemctl --user {action} {unit} failed: {_subprocess_output(result).strip()}")
        return
    labels = (
        (SHARED_LAUNCHD_LABEL, SHARED_GATEWAY_LAUNCHD_LABEL)
        if action == "stop"
        else (SHARED_GATEWAY_LAUNCHD_LABEL, SHARED_LAUNCHD_LABEL)
    )
    for label in labels:
        plist = LAUNCHD_USER_DIR / f"{label}.plist"
        if action in {"stop", "restart"}:
            _run(["launchctl", "unload", str(plist)])
        if action in {"start", "restart"}:
            result = _run(["launchctl", "load", str(plist)])
            if result.returncode != 0:
                raise ServiceError(f"launchctl load failed for {label}: {_subprocess_output(result).strip()}")


def remove_shared_tunnel_service() -> bool:
    removed = False
    for unit in (SHARED_SYSTEMD_UNIT, SHARED_GATEWAY_SYSTEMD_UNIT):
        path = SYSTEMD_USER_DIR / unit
        if path.exists():
            _run(["systemctl", "--user", "disable", "--now", unit])
            path.unlink(missing_ok=True)
            removed = True
    if removed:
        _run(["systemctl", "--user", "daemon-reload"])
    for label in (SHARED_LAUNCHD_LABEL, SHARED_GATEWAY_LAUNCHD_LABEL):
        plist = LAUNCHD_USER_DIR / f"{label}.plist"
        if plist.exists():
            _run(["launchctl", "unload", str(plist)])
            plist.unlink(missing_ok=True)
            removed = True
    return removed


def remove_legacy_connector_services(*, narrate: Callable[[str], None] | None = None) -> list[str]:
    """Stop/delete pre-shared-topology per-host LemonCrow wrapper services."""
    removed: list[str] = []
    if SYSTEMD_USER_DIR.is_dir():
        for path in sorted(SYSTEMD_USER_DIR.glob(_LEGACY_UNIT_GLOB)):
            if path.name in {SHARED_SYSTEMD_UNIT, SHARED_GATEWAY_SYSTEMD_UNIT}:
                continue
            _run(["systemctl", "--user", "disable", "--now", path.name])
            path.unlink(missing_ok=True)
            removed.append(path.name)
        if removed:
            _run(["systemctl", "--user", "daemon-reload"])
    if LAUNCHD_USER_DIR.is_dir():
        for path in sorted(LAUNCHD_USER_DIR.glob(f"{MCP_LABEL}.*.plist")):
            if path.stem in {SHARED_LAUNCHD_LABEL, SHARED_GATEWAY_LAUNCHD_LABEL}:
                continue
            _run(["launchctl", "unload", str(path)])
            path.unlink(missing_ok=True)
            removed.append(path.stem)
    if narrate is not None:
        for name in removed:
            narrate(f"Removed obsolete connector service {name}")
    return removed


def shared_log_command(*, lines: int, follow: bool) -> list[str]:
    if _is_linux():
        command = ["journalctl", "--user", "-u", SHARED_SYSTEMD_UNIT, f"-n{lines}"]
        if follow:
            command.append("-f")
        return command
    from lemoncrow.core.foundation.paths import default_store_root

    path = default_store_root() / "mcp" / "tunnel.log"
    return ["tail", *(["-f"] if follow else []), "-n", str(lines), str(path)]


def management_hints(unit_or_label: str) -> list[str]:
    if unit_or_label.endswith(".service"):
        return [
            f"systemctl --user status {SHARED_GATEWAY_SYSTEMD_UNIT} {SHARED_SYSTEMD_UNIT}",
            f"journalctl --user -u {SHARED_GATEWAY_SYSTEMD_UNIT} -u {SHARED_SYSTEMD_UNIT} -f",
            f"systemctl --user stop {SHARED_SYSTEMD_UNIT} {SHARED_GATEWAY_SYSTEMD_UNIT}",
        ]
    return [
        f"launchctl list {unit_or_label}",
        f"launchctl unload ~/Library/LaunchAgents/{unit_or_label}.plist",
    ]
