"""Native coding-agent MCP registration.

This is intentionally a *registration* installer, not a LemonCrow runtime
installer. It asks each supported host CLI to persist one stdio MCP entry:
Local Lite by default (``runtime lite-mcp``), or the hosted thin client only
after explicit ``--hosted`` selection. LemonCrow never writes the host's
configuration files during registration and never installs a daemon, listener,
hook, or background service. Release upgrades separately migrate existing plain
LC invocations with host_repair, preserving their settings.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, TextIO

from . import CLIENT_NAME

SUPPORTED_AGENTS: Final[tuple[str, ...]] = ("claude", "codex", "opencode")

_HOST_BINARY: Final[dict[str, str]] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def _host_executable(host: str) -> str | None:
    """Resolve the normal CLI, including side-by-side OpenCode V2 betas."""
    return _which(_HOST_BINARY[host]) or (_which("opencode2") if host == "opencode" else None)


def register_agents(
    agent: str,
    *,
    dry_run: bool,
    sink: TextIO,
    hosted: bool = False,
) -> int:
    """Register Runtime with one host, or every detected host.

    Local Lite is the persistence default. hosted=True is an explicit opt-in
    to the hosted/thin-client MCP path. auto remains detection-only.
    """

    if agent == "auto":
        targets = tuple(name for name in SUPPORTED_AGENTS if _host_executable(name) is not None)
        if not targets:
            sink.write("no supported coding agent found (claude, codex, opencode)\n")
            return 2
    elif agent in SUPPORTED_AGENTS:
        targets = (agent,)
    else:
        sink.write(f"unsupported agent {agent!r}; choose auto, claude, codex, or opencode\n")
        return 2

    failed = False
    for target in targets:
        command = _client_mcp_command(target, hosted=hosted)
        binary = _host_executable(target)
        if binary is None:
            sink.write(f"{target}: host CLI not found on PATH\n")
            failed = True
            continue
        argv = _registration_command(target, binary, command)
        sink.write(f"{target}: {shlex.join(argv)}\n")
        if dry_run:
            continue
        if target == "claude":
            _run((binary, "mcp", "remove", "--scope", "user", "lc"), check=False)
        elif target == "codex":
            _run((binary, "mcp", "remove", "lc"), check=False)
        try:
            completed = _run(argv, check=True)
        except subprocess.CalledProcessError as exc:
            failed = True
            detail = (exc.stderr or exc.stdout or "host MCP registration failed").strip()
            sink.write(f"{target}: registration failed: {detail}\n")
            continue
        detail = (completed.stdout or "").strip()
        if detail:
            sink.write(f"{target}: {detail}\n")
        sink.write(f"{target}: registered lc -> {shlex.join(command)}\n")
    return 1 if failed else 0


def _status_mode(completed: subprocess.CompletedProcess[str], *, enabled: bool) -> str:
    if not enabled:
        return "unknown"
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    if "lite-mcp" in output:
        return "local_lite"
    if re.search(r"(?:^|[\s\"'/])mcp(?:[\s\"'/]|$)", output):
        return "hosted"
    return "unknown"


def runtime_status(agent: str, *, sink: TextIO, as_json: bool = False) -> int:
    """Report whether Runtime is registered with one host or all supported hosts."""
    targets = SUPPORTED_AGENTS if agent == "auto" else ((agent,) if agent in SUPPORTED_AGENTS else ())
    if not targets:
        sink.write(f"unsupported agent {agent!r}; choose auto, claude, codex, or opencode\n")
        return 2
    present = 0
    rows: list[dict[str, object]] = []
    for target in targets:
        binary = _host_executable(target)
        if binary is None:
            rows.append(
                {
                    "host": target,
                    "installed": False,
                    "enabled": False,
                    "mode": "unknown",
                }
            )
            if not as_json:
                sink.write(f"{target}: host not installed\n")
            continue
        present += 1
        completed = _run(_status_command(target, binary), check=False)
        enabled = _status_is_configured(target, completed)
        mode = _status_mode(completed, enabled=enabled)
        rows.append(
            {
                "host": target,
                "installed": True,
                "enabled": enabled,
                "mode": mode,
            }
        )
        if not as_json:
            if not enabled:
                state = "not enabled"
            else:
                label = {
                    "local_lite": "Lite",
                    "hosted": "Hosted",
                    "unknown": "Unknown mode",
                }[mode]
                state = f"enabled · {label}"
            sink.write(f"{target}: {state}\n")
    if as_json:
        sink.write(json.dumps({"integrations": rows}, sort_keys=True) + "\n")
    return 1 if agent != "auto" and present == 0 else 0


def unregister_agents(agent: str, *, dry_run: bool, sink: TextIO) -> int:
    """Remove the persisted Runtime MCP entry using the host native CLI."""
    if agent == "auto":
        targets = tuple(name for name in SUPPORTED_AGENTS if _host_executable(name) is not None)
        if not targets:
            sink.write("no supported coding agent found (claude, codex, opencode)\n")
            return 2
    elif agent in SUPPORTED_AGENTS:
        targets = (agent,)
    else:
        sink.write(f"unsupported agent {agent!r}; choose auto, claude, codex, or opencode\n")
        return 2

    failed = False
    for target in targets:
        binary = _host_executable(target)
        if binary is None:
            sink.write(f"{target}: host CLI not found on PATH\n")
            failed = True
            continue
        argv = _unregistration_command(target, binary)
        if argv is None:
            sink.write(
                "opencode: native CLI has no MCP remove command; use the full LemonCrow Workspaces uninstaller "
                "or remove the lc MCP entry in OpenCode settings\n"
            )
            failed = True
            continue
        sink.write(f"{target}: {shlex.join(argv)}\n")
        if dry_run:
            continue
        completed = _run(argv, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "host MCP removal failed").strip()
            sink.write(f"{target}: removal failed: {detail}\n")
            failed = True
            continue
        sink.write(f"{target}: Workspaces disabled\n")
    return 1 if failed else 0


def _status_command(agent: str, binary: str) -> tuple[str, ...]:
    if agent == "claude":
        return (binary, "mcp", "get", "lc")
    if agent == "codex":
        return (binary, "mcp", "get", "lc", "--json")
    if agent == "opencode":
        return (binary, "mcp", "list")
    raise ValueError(agent)


def _status_is_configured(agent: str, completed: subprocess.CompletedProcess[str]) -> bool:
    if completed.returncode != 0:
        return False
    if agent != "opencode":
        return True
    import re

    output = f"{completed.stdout}\n{completed.stderr}"
    return re.search(r"(?im)(?:^|\s)lc(?:\s|$)", output) is not None


def _unregistration_command(agent: str, binary: str, *, name: str = "lc") -> tuple[str, ...] | None:
    if agent == "claude":
        return (binary, "mcp", "remove", "--scope", "user", name)
    if agent == "codex":
        return (binary, "mcp", "remove", name)
    if agent == "opencode":
        return None
    raise ValueError(agent)


def _client_launcher() -> tuple[str, ...]:
    """The argv prefix a host runs to start this client (``lc task mcp`` registers through it too)."""
    # Pin the interpreter-owned Runtime first: a Review-owned lc can shadow it
    # on PATH, and sys.argv[0] may be python/-c during Pi extension refresh.
    sibling = Path(sys.executable).parent / "lemoncrow-runtime-client"
    if sibling.is_file():
        return (str(sibling.absolute()),)
    from .cli import _external_runtime_executable

    runtime = _external_runtime_executable()
    if runtime is not None:
        return (str(runtime.absolute()),)
    current = Path(sys.argv[0]).expanduser()
    if current.name in {CLIENT_NAME, "lemoncrow-runtime-client"} and current.is_file():
        return (str(current.resolve()),)
    installed = _which(CLIENT_NAME)
    if installed is not None:
        return (installed,)
    # Source-checkout fallback. An installed console script normally wins.
    return (sys.executable, "-m", "lemoncrow_client")


def _client_mcp_command(host: str, *, hosted: bool = False) -> tuple[str, ...]:
    action = "mcp" if hosted else "lite-mcp"
    return (*_client_launcher(), "runtime", action, "--host", host)


def _registration_command(
    agent: str, binary: str, command: Sequence[str], *, name: str = "lc", env: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The host CLI's ``mcp add``; ``env`` goes in its environment flags, right before ``--``, which also ends
    Claude's variadic ``-e``."""

    flag = "-e" if agent == "claude" else "--env"
    environment = tuple(part for key, value in (env or {}).items() for part in (flag, f"{key}={value}"))
    if agent == "claude":
        return (binary, "mcp", "add", "--scope", "user", name, *environment, "--", *command)
    if agent == "codex":
        return (binary, "mcp", "add", name, *environment, "--", *command)
    if agent == "opencode":
        # OpenCode's current CLI updates the named entry in its global config.
        return (binary, "mcp", "add", name, "--global", *environment, "--", *command)
    raise ValueError(agent)


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(argv: Sequence[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )
