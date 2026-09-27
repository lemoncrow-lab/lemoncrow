"""Native coding-agent MCP registration.

This is intentionally a *registration* installer, not a LemonCrow runtime
installer.  It asks each supported host CLI to persist one stdio MCP entry
whose command is ``lemoncrow-client mcp``.  LemonCrow never writes the host's
configuration files directly and never installs a daemon, listener, hook, or
background service.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import Final, TextIO

from . import CLIENT_NAME

SUPPORTED_AGENTS: Final[tuple[str, ...]] = ("claude", "codex", "opencode")

_HOST_BINARY: Final[dict[str, str]] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def register_agents(agent: str, *, dry_run: bool, sink: TextIO) -> int:
    """Register the thin MCP client with one host, or every detected host.

    ``auto`` is deliberately detection-only: it never installs a coding agent.
    Every subprocess is foreground and must finish before this command returns.
    """

    if agent == "auto":
        targets = tuple(name for name in SUPPORTED_AGENTS if _which(_HOST_BINARY[name]) is not None)
        if not targets:
            sink.write("no supported coding agent found (claude, codex, opencode)\n")
            return 2
    elif agent in SUPPORTED_AGENTS:
        targets = (agent,)
    else:
        sink.write(f"unsupported agent {agent!r}; choose auto, claude, codex, or opencode\n")
        return 2

    command = _client_mcp_command()
    failed = False
    for target in targets:
        binary = _which(_HOST_BINARY[target])
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


def _client_mcp_command() -> tuple[str, ...]:
    installed = _which(CLIENT_NAME)
    if installed is not None:
        return (installed, "mcp")
    # Source-checkout fallback.  An installed console script normally wins, but
    # keeping this path makes ``python -m lemoncrow_client install`` usable too.
    return (sys.executable, "-m", "lemoncrow_client", "mcp")


def _registration_command(agent: str, binary: str, command: Sequence[str]) -> tuple[str, ...]:
    if agent == "claude":
        return (binary, "mcp", "add", "--scope", "user", "lc", "--", *command)
    if agent == "codex":
        return (binary, "mcp", "add", "lc", "--", *command)
    if agent == "opencode":
        # OpenCode's current CLI updates the named entry in its global config.
        return (binary, "mcp", "add", "lc", "--global", "--", *command)
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
