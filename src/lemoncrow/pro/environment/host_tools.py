"""Find locally installed tools when a GUI server has a minimal PATH."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _host_tool_directories() -> list[Path]:
    return [
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path.home() / ".local/bin",
        Path.home() / ".lemoncrow/bin",
        Path.home() / ".opencode/bin",
        Path.home() / ".bun/bin",
        Path.home() / ".npm-global/bin",
        Path.home() / ".docker/bin",
        Path("/Applications/Docker.app/Contents/Resources/bin"),
    ]


def host_tool_environment() -> dict[str, str]:
    """Preserve agent settings and make env-based runtime shebangs discoverable."""
    environment = os.environ.copy()
    if sys.platform == "darwin":
        environment["PATH"] = os.pathsep.join(
            dict.fromkeys(
                [
                    *environment.get("PATH", os.defpath).split(os.pathsep),
                    *(str(directory) for directory in _host_tool_directories()),
                ]
            )
        )
    return environment


def host_executable(name: str) -> str | None:
    found = shutil.which(name)
    if found or sys.platform != "darwin":
        return found
    directories = _host_tool_directories()
    if name == "docker-compose":
        # Invoke the plugin directly: the cleared runner HOME deliberately
        # excludes ~/.docker/config.json and its cliPluginsExtraDirs setting.
        directories.extend(
            (
                Path("/Applications/Docker.app/Contents/Resources/cli-plugins"),
                Path.home() / ".docker/cli-plugins",
                Path("/opt/homebrew/lib/docker/cli-plugins"),
                Path("/usr/local/lib/docker/cli-plugins"),
            )
        )
    for directory in directories:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
