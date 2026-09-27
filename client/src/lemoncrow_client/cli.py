"""The thin-client entry points.

``mcp``
    The one MCP stdio process for a coding-agent session.
``install``
    One-time, foreground registration with a supported host's native MCP CLI.
    It installs no LemonCrow service, listener, hook, or background process.
``target``
    Print, or write into ``~/.lemoncrow/env``, the endpoint the client uses.
``audit``
    Print the resolved runtime claims for enterprise review.

There is deliberately no updater, persistent server, tunnel, service unit or
SessionStart helper in this package.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, TextIO

from . import CLIENT_NAME, CLIENT_VERSION
from .auth_cli import auth_main
from .config import DEFAULT_URL, load_config
from .errors import ClientError
from .host_install import SUPPORTED_AGENTS, register_agents
from .mcpserver import serve
from .protocol import CLIENT_CAPABILITIES, PROTOCOL_VERSION
from .review import review_main
from .routing import ROUTES

__all__ = ["main"]

_USAGE: Final[str] = f"""{CLIENT_NAME} {CLIENT_VERSION}

usage:
  {CLIENT_NAME} mcp                 run the one MCP stdio process for the host session
  {CLIENT_NAME} install [OPTIONS]   register MCP with Claude, Codex, and/or OpenCode
  {CLIENT_NAME} auth [COMMAND]      sign in, inspect status, or sign out
  {CLIENT_NAME} review [OPTIONS]    freeze exact Git sides and publish hosted Review
  {CLIENT_NAME} target [URL]        show, or set, the LemonCrow endpoint
  {CLIENT_NAME} audit               print what this client does and does not do
  {CLIENT_NAME} routes              print the tool execution matrix

install options:
  --agent NAME                  auto (default), claude, codex, or opencode
  --dry-run                     print native host commands without changing host config

environment:
  LEMONCROW_URL                 endpoint (default {DEFAULT_URL})
  LEMONCROW_TOKEN               explicit operator-provided bearer override
  LEMONCROW_REFRESH_TOKEN       explicit operator-provided refresh override
  LEMONCROW_HOME                state directory (default ~/.lemoncrow)
  LEMONCROW_LOCAL_FS            offer the same-host read optimization (default off)
"""


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    sink = stdout if stdout is not None else sys.stdout
    if not arguments or arguments[0] in {"-h", "--help", "help"}:
        sink.write(_USAGE)
        return 0
    verb, rest = arguments[0], arguments[1:]
    if verb == "--version":
        sink.write(f"{CLIENT_NAME} {CLIENT_VERSION}\n")
        return 0
    if verb == "mcp":
        return serve()
    if verb == "install":
        return _install(rest, sink)
    if verb == "auth":
        return auth_main(rest, sink=sink)
    if verb == "review":
        return review_main(rest, sink=sink)
    if verb == "target":
        return _target(rest, sink)
    if verb == "audit":
        return _audit(sink)
    if verb == "routes":
        return _routes(sink)
    sink.write(_USAGE)
    return 2


def _install(rest: Sequence[str], sink: TextIO) -> int:
    agent = "auto"
    dry_run = False
    index = 0
    while index < len(rest):
        argument = rest[index]
        if argument == "--dry-run":
            dry_run = True
        elif argument == "--agent":
            index += 1
            if index >= len(rest):
                sink.write("--agent requires a value\n")
                return 2
            agent = rest[index]
        else:
            sink.write(f"unknown install option: {argument}\n")
            return 2
        index += 1
    if agent not in {*SUPPORTED_AGENTS, "auto"}:
        sink.write(f"unsupported agent {agent!r}; choose auto, claude, codex, or opencode\n")
        return 2
    return register_agents(agent, dry_run=dry_run, sink=sink)


def _target(rest: Sequence[str], sink: TextIO) -> int:
    """Show the endpoint, or persist one into ``~/.lemoncrow/env``.

    Writing the endpoint is the only persistent state this client creates, it
    lives under the one directory the audit permits, and it is a single
    ``KEY=value`` line a reviewer can read.
    """
    try:
        config = load_config()
    except ClientError as exc:
        sink.write(f"{exc.message}\n")
        return 2
    if not rest:
        sink.write(f"{config.url}\n")
        return 0
    url = rest[0].strip()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    target = config.state_dir / "env"
    lines = [
        line
        for line in (target.read_text(encoding="utf-8").splitlines() if target.is_file() else [])
        if not line.startswith("LEMONCROW_URL=")
    ]
    lines.append(f"LEMONCROW_URL={url}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    sink.write(f"wrote LEMONCROW_URL={url} to {target}\n")
    return 0


def _audit(sink: TextIO) -> int:
    """Resolve and print the claims an enterprise review checks."""
    try:
        config = load_config()
    except ClientError as exc:
        sink.write(f"configuration error: {exc.message}\n")
        return 2
    payload: dict[str, Any] = {
        "client": {"name": CLIENT_NAME, "version": CLIENT_VERSION, "protocol": PROTOCOL_VERSION},
        "dependencies": _declared_dependencies(),
        "endpoint": config.url,
        "endpoint_origin": "{}://{}:{}".format(*config.endpoint_origin),
        "install_mode": config.install_mode,
        "token_source": config.token_source or ("LEMONCROW_TOKEN" if config.token else "none"),
        "refresh_token_source": config.refresh_token_source or "none",
        "state_dir": str(config.state_dir),
        "worktree": str(config.repo_root),
        "capabilities_offered": sorted(CLIENT_CAPABILITIES),
        "local_fs_offered": config.offer_local_fs,
        "guarantees": {
            "binds_no_port": True,
            "host_registration_uses_native_cli": True,
            "single_lemoncrow_process_per_host_session": True,
            "separate_session_start_process": False,
            "spawns_mcp_child_server": False,
            "spawns_no_background_process": True,
            "installs_no_unit": True,
            "downloads_nothing": True,
            "self_update": False,
            "product_telemetry": False,
            "writes_outside_repo_and_state_dir": False,
        },
        "tools": {
            "client": sorted(name for name, route in ROUTES.items() if route.local),
            "server": sorted(name for name, route in ROUTES.items() if route.remote),
            "sync": sorted(name for name, route in ROUTES.items() if not route.local and not route.remote),
        },
    }
    sink.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def _routes(sink: TextIO) -> int:
    width = max(len(name) for name in ROUTES)
    for name in sorted(ROUTES):
        route = ROUTES[name]
        sink.write(f"{name:<{width}}  {route.site.value:<26}  {route.reason}\n")
    return 0


def _declared_dependencies() -> list[str]:
    """The installed distribution's ``Requires-Dist``. Expected: none."""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - stdlib since 3.8
        return []
    try:
        requires = metadata.requires("lemoncrow-client")
    except metadata.PackageNotFoundError:
        # Running from a source checkout rather than an installed wheel.
        return _dependencies_from_pyproject()
    return [entry for entry in (requires or []) if "extra ==" not in entry]


def _dependencies_from_pyproject() -> list[str]:
    candidate = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not candidate.is_file():
        return []
    import tomllib

    with candidate.open("rb") as handle:
        parsed = tomllib.load(handle)
    raw = parsed.get("project", {}).get("dependencies", [])
    return [str(entry) for entry in raw]
