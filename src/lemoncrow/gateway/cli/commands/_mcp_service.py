"""Boot-persistent supervision for ``lc mcp serve --persistent``.

``--persistent`` promises a URL that survives restarts. Before this module it
only survived *tunnel* restarts: the server itself was a foreground process
that died with the terminal, so every reboot meant re-running the command by
hand. Here the same invocation registers itself as a user-level service
(systemd on Linux, launchd on macOS) bound to the directory it was started
from, so the machine brings it back after a reboot with the same hostname,
the same OAuth store and the same pairing code.

One unit per hostname (``lemoncrow-mcp-<slug>.service`` /
``com.lemoncrow.mcp.<slug>``) — hostnames already own their tunnel state and
OAuth store, so a second project never disturbs the first one's service.

The registered command carries ``--foreground``, and the unit exports
``LEMONCROW_MCP_SUPERVISED=1``: both keep the supervised process from trying
to re-register (and restart) itself, which would be a self-kill loop.
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
from xml.sax.saxutils import escape as _xml_escape

from lemoncrow.infra.runtime.daemon_units import (
    CONTROLLER_UNIT,
    LAUNCHD_USER_DIR,
    MCP_LABEL,
    SYSTEMD_USER_DIR,
    _is_linux,
    _is_macos,
    _subprocess_output,
    _systemd_user_bus_unavailable,
)

# Set in the unit environment; also the escape hatch for anything else that
# must not recurse into registration (tests, a manually supervised run).
SUPERVISED_ENV = "LEMONCROW_MCP_SUPERVISED"

_UNIT_PREFIX = "lemoncrow-mcp-"


class ServiceError(RuntimeError):
    """Registration failed in a way the operator has to act on."""


def unit_name(slug: str) -> str:
    """systemd unit for one hostname slug: ``lemoncrow-mcp-<slug>.service``."""
    return f"{_UNIT_PREFIX}{slug}.service"


def launchd_label(slug: str) -> str:
    """launchd label for one hostname slug: ``com.lemoncrow.mcp.<slug>``."""
    return f"{MCP_LABEL}.{slug}"


def supervisor_kind() -> str | None:
    """``"systemd"``, ``"launchd"``, or ``None`` when neither is usable.

    ``None`` is not an error: the caller falls back to today's foreground
    behaviour (containers, WSL without a user bus, exotic platforms).

    Registration is refused inside a test process unless explicitly opted in:
    installing (and starting) a real user unit on the developer's machine is
    an irreversible side effect no unit test should ever have — tests that
    want to cover this path set ``LEMONCROW_MCP_ALLOW_SERVICE=1`` and point
    the unit directories at a tmp_path.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("LEMONCROW_MCP_ALLOW_SERVICE", "") != "1":
        return None
    if _is_macos() and shutil.which("launchctl") is not None:
        return "launchd"
    if _is_linux() and shutil.which("systemctl") is not None:
        return "systemd"
    return None


def is_supervised() -> bool:
    """True inside the unit we installed — registration must be skipped."""
    return os.environ.get(SUPERVISED_ENV, "").strip() not in ("", "0", "false", "False")


def installed_units() -> list[str]:
    """Every installed per-hostname systemd unit, sorted (for ``lc background``)."""
    if not SYSTEMD_USER_DIR.is_dir():
        return []
    return sorted(p.name for p in SYSTEMD_USER_DIR.glob(f"{_UNIT_PREFIX}*.service"))


def installed_labels() -> list[str]:
    """Every installed per-hostname launchd label, sorted (for ``lc background``)."""
    if not LAUNCHD_USER_DIR.is_dir():
        return []
    return sorted(p.stem for p in LAUNCHD_USER_DIR.glob(f"{MCP_LABEL}.*.plist"))


def lemoncrow_binary() -> str:
    """Absolute path to the CLI the unit should exec.

    ``shutil.which`` first (the installed entry point, which survives a venv
    being rebuilt), falling back to this process's own argv[0] resolved — a
    unit holding a relative path would fail the moment systemd starts it from
    a different cwd.
    """
    return shutil.which("lemoncrow") or str(Path(sys.argv[0]).resolve())


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def _enable_linger(narrate: Callable[[str], None]) -> None:
    """Ask logind to keep user services running without an active login.

    Without linger a user unit is stopped at logout and only started at the
    next login — which breaks the "same server after a reboot" promise for
    headless/auto-login-less machines. Best effort: on a desktop that already
    lingers, or where polkit refuses without a password, the service still
    works for every logged-in session, so a failure is a note, not an error.
    """
    if shutil.which("loginctl") is None:
        return
    user = getpass.getuser()
    show = _run(["loginctl", "show-user", user, "--property=Linger"])
    if show.returncode == 0 and "Linger=yes" in show.stdout:
        return
    result = _run(["loginctl", "enable-linger", user])
    if result.returncode == 0:
        narrate("Enabled linger — the service also runs while you are logged out.")
    else:
        narrate(
            "Could not enable linger (needs authorization); the service starts at login. "
            f"Run manually: sudo loginctl enable-linger {user}"
        )


def _systemd_unit_content(*, hostname: str, workspace: Path, command: list[str], root: Path) -> str:
    # shlex.join quotes any path with spaces; systemd honours POSIX single
    # quotes in ExecStart, so this is safe for both.
    exec_start = shlex.join(command)
    return f"""[Unit]
Description=LemonCrow MCP server ({hostname})
After=network-online.target {CONTROLLER_UNIT}
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workspace}
ExecStart={exec_start}
Restart=always
RestartSec=5
Environment=LEMONCROW_ROOT={root}
Environment=PYTHONUNBUFFERED=1
Environment={SUPERVISED_ENV}=1
Environment=PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}

[Install]
WantedBy=default.target
"""


def _launchd_plist_content(*, label: str, workspace: Path, command: list[str], root: Path, log_path: Path) -> str:
    args = "\n".join(f"        <string>{_xml_escape(arg)}</string>" for arg in command)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_xml_escape(label)}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{_xml_escape(str(workspace))}</string>
    <key>StandardOutPath</key>
    <string>{_xml_escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(log_path))}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LEMONCROW_ROOT</key>
        <string>{_xml_escape(str(root))}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>{SUPERVISED_ENV}</key>
        <string>1</string>
        <key>PATH</key>
        <string>{_xml_escape(os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))}</string>
    </dict>
</dict>
</plist>
"""


def register_persistent_service(
    *,
    hostname: str,
    slug: str,
    workspace: Path,
    serve_args: list[str],
    root: Path,
    narrate: Callable[[str], None],
) -> str:
    """Install + (re)start the boot-persistent unit for ``hostname``.

    ``serve_args`` is the ``lc``-relative argv tail (``["mcp", "serve", ...]``)
    the unit should run; the caller is responsible for including
    ``--foreground`` so the supervised process serves instead of recursing
    back into registration. Returns the unit name / launchd label so the
    caller can print the management commands. Re-registering is idempotent:
    the unit is rewritten and restarted, which is also how a moved workspace
    or a changed binary path gets picked up.
    """
    kind = supervisor_kind()
    command = [lemoncrow_binary(), *serve_args]
    if kind == "systemd":
        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        unit = unit_name(slug)
        (SYSTEMD_USER_DIR / unit).write_text(
            _systemd_unit_content(hostname=hostname, workspace=workspace, command=command, root=root),
            encoding="utf-8",
        )
        narrate(f"Installed {SYSTEMD_USER_DIR / unit}")
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
        if reload_result.returncode != 0:
            output = _subprocess_output(reload_result)
            if _systemd_user_bus_unavailable(output):
                raise ServiceError(
                    "systemd user bus is unavailable, so the unit was written but not started. "
                    "Re-run this command from a login session, or start the server with --foreground."
                )
            raise ServiceError(
                "systemctl --user daemon-reload failed" + (f": {output.strip()}" if output.strip() else "")
            )
        for args in (["enable", unit], ["restart", unit]):
            result = _run(["systemctl", "--user", *args])
            if result.returncode != 0:
                raise ServiceError(f"systemctl --user {' '.join(args)} failed: {_subprocess_output(result).strip()}")
        _enable_linger(narrate)
        return unit

    if kind == "launchd":
        LAUNCHD_USER_DIR.mkdir(parents=True, exist_ok=True)
        label = launchd_label(slug)
        plist = LAUNCHD_USER_DIR / f"{label}.plist"
        log_path = root / "mcp" / f"{slug}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            _launchd_plist_content(label=label, workspace=workspace, command=command, root=root, log_path=log_path),
            encoding="utf-8",
        )
        narrate(f"Installed {plist}")
        # unload-then-load, so a re-register replaces the running job rather
        # than failing with "service already loaded".
        _run(["launchctl", "unload", str(plist)])
        result = _run(["launchctl", "load", str(plist)])
        if result.returncode != 0:
            raise ServiceError(f"launchctl load failed: {_subprocess_output(result).strip()}")
        return label

    raise ServiceError("no systemd/launchd user session available")


@dataclass(frozen=True)
class ServiceInfo:
    """One installed per-hostname MCP service, as the CLI shows it."""

    name: str  # systemd unit name, or launchd label
    slug: str
    hostname: str
    workspace: str
    state: str  # active / inactive / failed / loaded / unknown
    enabled: str  # enabled / disabled / n-a
    pid: int | None = None  # main process, when running

    @property
    def is_systemd(self) -> bool:
        return self.name.endswith(".service")


def _hostname_from_command(command: list[str]) -> str:
    """Pull ``--hostname X`` back out of a registered ExecStart/ProgramArguments.

    The hostname is the identity of the service (its tunnel, OAuth store and
    pairing code all key off it), and re-deriving it from the recorded command
    keeps the unit file the single source of truth — no parallel registry to
    drift out of sync with what systemd actually runs.
    """
    for index, arg in enumerate(command):
        if arg == "--hostname" and index + 1 < len(command):
            return command[index + 1]
        if arg.startswith("--hostname="):
            return arg.split("=", 1)[1]
    return ""


def _describe_systemd(unit: str) -> ServiceInfo:
    path = SYSTEMD_USER_DIR / unit
    command: list[str] = []
    workspace = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            command = shlex.split(line.split("=", 1)[1])
        elif line.startswith("WorkingDirectory="):
            workspace = line.split("=", 1)[1].strip()
    show = _run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "UnitFileState", "-p", "MainPID"])
    props: dict[str, str] = {}
    for line in show.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value.strip()
    main_pid = props.get("MainPID", "0")
    return ServiceInfo(
        name=unit,
        slug=unit[len(_UNIT_PREFIX) : -len(".service")],
        hostname=_hostname_from_command(command),
        workspace=workspace,
        state=props.get("ActiveState", "unknown"),
        enabled=props.get("UnitFileState", "unknown"),
        pid=int(main_pid) if main_pid.isdigit() and main_pid != "0" else None,
    )


def _describe_launchd(label: str) -> ServiceInfo:
    plist_path = LAUNCHD_USER_DIR / f"{label}.plist"
    try:
        with plist_path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        data = {}
    command = [str(arg) for arg in data.get("ProgramArguments", [])]
    listed = _run(["launchctl", "list", label])
    # `launchctl list <label>` prints a plist-ish block including `"PID" = N;`
    # only while the job is actually running.
    pid: int | None = None
    for line in listed.stdout.splitlines():
        if '"PID"' in line:
            digits = "".join(char for char in line if char.isdigit())
            pid = int(digits) if digits else None
    return ServiceInfo(
        name=label,
        slug=label[len(f"{MCP_LABEL}.") :],
        hostname=_hostname_from_command(command),
        workspace=str(data.get("WorkingDirectory", "")),
        state="active" if listed.returncode == 0 else "inactive",
        enabled="enabled" if plist_path.exists() else "disabled",
        pid=pid,
    )


def describe_services() -> list[ServiceInfo]:
    """Every installed MCP service on this machine, with live run state."""
    return [_describe_systemd(unit) for unit in installed_units()] + [
        _describe_launchd(label) for label in installed_labels()
    ]


def resolve_service(selector: str | None) -> ServiceInfo:
    """Find one service by hostname, slug, or unit/label name.

    ``None`` is allowed only while exactly one service exists — same rule as
    ``--hostname`` on ``serve``: silently picking one of several would act on
    the wrong project.
    """
    services = describe_services()
    if not services:
        raise ServiceError("no MCP services installed — run `lc mcp serve --persistent --hostname <host>` first")
    if selector is None:
        if len(services) > 1:
            raise ServiceError(
                "several MCP services are installed — name one: "
                + ", ".join(service.hostname or service.name for service in services)
            )
        return services[0]
    needle = selector.strip().lower()
    for service in services:
        if needle in {service.hostname.lower(), service.slug.lower(), service.name.lower()}:
            return service
    raise ServiceError(
        f"no MCP service for {selector!r} — installed: "
        + ", ".join(service.hostname or service.name for service in services)
    )


def control_service(service: ServiceInfo, action: str) -> None:
    """``start`` / ``stop`` / ``restart`` one installed service."""
    if service.is_systemd:
        result = _run(["systemctl", "--user", action, service.name])
        if result.returncode != 0:
            raise ServiceError(f"systemctl --user {action} {service.name} failed: {_subprocess_output(result).strip()}")
        return
    plist = LAUNCHD_USER_DIR / f"{service.name}.plist"
    if action in ("stop", "restart"):
        _run(["launchctl", "unload", str(plist)])
    if action in ("start", "restart"):
        result = _run(["launchctl", "load", str(plist)])
        if result.returncode != 0:
            raise ServiceError(f"launchctl load failed: {_subprocess_output(result).strip()}")


def remove_service(service: ServiceInfo) -> None:
    """Stop and delete one service's unit/plist (leaves tunnel + OAuth state)."""
    if service.is_systemd:
        _run(["systemctl", "--user", "disable", "--now", service.name])
        with_path = SYSTEMD_USER_DIR / service.name
        if with_path.exists():
            with_path.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
        return
    plist = LAUNCHD_USER_DIR / f"{service.name}.plist"
    _run(["launchctl", "unload", str(plist)])
    if plist.exists():
        plist.unlink()


def log_command(service: ServiceInfo, *, lines: int, follow: bool) -> list[str]:
    """The command that tails this service's logs (journald, or its log file)."""
    if service.is_systemd:
        cmd = ["journalctl", "--user", "-u", service.name, f"-n{lines}"]
        if follow:
            cmd.append("-f")
        return cmd
    from lemoncrow.core.foundation.paths import default_store_root

    log_path = default_store_root() / "mcp" / f"{service.slug}.log"
    follow_flag = ["-f"] if follow else []
    return ["tail", *follow_flag, "-n", str(lines), str(log_path)]


def management_hints(unit_or_label: str) -> list[str]:
    """Copy-pasteable status/logs/stop commands for the registered service."""
    if unit_or_label.endswith(".service"):
        return [
            f"systemctl --user status {unit_or_label}",
            f"journalctl --user -u {unit_or_label} -f",
            f"systemctl --user stop {unit_or_label}   # disable: systemctl --user disable --now {unit_or_label}",
        ]
    return [
        f"launchctl list {unit_or_label}",
        f"launchctl unload ~/Library/LaunchAgents/{unit_or_label}.plist",
    ]
