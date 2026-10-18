"""Session-scoped host launchers for ``lc with <host>``.

The host keeps its own auth, model, settings, prompts and native tools. LemonCrow
adds one MCP server only to the child process. No host configuration is written.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

TRIAL_HOSTS: Final[tuple[str, ...]] = ("claude", "codex", "opencode", "pi")
_HOST_LABELS: Final[dict[str, str]] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "pi": "Pi",
}


class HostTrialError(RuntimeError):
    pass


def _runtime_mcp_invocation(host: str) -> tuple[str, list[str]]:
    """Resolve the active Runtime executable as the local-only Lite MCP child."""

    from .host_install import _client_mcp_command

    pinned = _client_mcp_command(host)
    # Keep the legacy discovery fallback for source/full-engine launchers only.
    if Path(pinned[0]).is_file():
        return pinned[0], list(pinned[1:])
    current = Path(sys.argv[0]).expanduser()
    args = ["runtime", "lite-mcp", "--host", host]
    if current.name in {"lc", "lemoncrow", "lemoncrow-client", "lemoncrow-runtime-client"} and current.is_file():
        return str(current.resolve()), args
    for name in ("lc", "lemoncrow-runtime-client", "lemoncrow-client", "lemoncrow"):
        executable = shutil.which(name)
        if executable:
            return executable, args
    return sys.executable, ["-m", "lemoncrow_client", *args]


def _trial_workspace_root() -> Path:
    """Return the git worktree root required by indexed `lc with` sessions."""
    cwd = Path.cwd().resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostTrialError(
            "`lc with` needs a git repository so indexed code_search is available; run it inside a repository"
        ) from exc
    output = result.stdout if isinstance(result.stdout, str) else ""
    if result.returncode == 0 and output.strip():
        return Path(output.strip()).expanduser().resolve()
    raise HostTrialError(
        "`lc with` needs a git repository so indexed code_search is available; run it inside a repository"
    )


def _mcp_env(host: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    # Consumed only by the dedicated Lite MCP path. No endpoint, token, login,
    # upload, or hosted View setting is forwarded.
    extra_env = dict(extra or {})
    workspace = extra_env.get("LEMONCROW_WORKSPACE_ROOT") or str(_trial_workspace_root())
    env = {
        "LEMONCROW_AGENT": host,
        "LEMONCROW_WORKSPACE_ROOT": workspace,
        "LEMONCROW_RUNTIME_LITE": "1",
    }
    env.update(extra_env)
    return env


def _claude_launch(
    binary: str,
    arguments: Sequence[str],
    *,
    mcp_extra_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    command, mcp_args = _runtime_mcp_invocation("claude")
    config = {
        "mcpServers": {
            "lc": {
                "type": "stdio",
                "command": command,
                "args": mcp_args,
                "env": _mcp_env("claude", mcp_extra_env),
            }
        }
    }
    # No --strict-mcp-config: the user's normal Claude settings and other MCPs
    # stay active. This is an additive process-scoped config layer.
    return [binary, "--mcp-config", json.dumps(config, separators=(",", ":")), *arguments], dict(os.environ)


def _codex_launch(
    binary: str,
    arguments: Sequence[str],
    *,
    mcp_extra_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    command, mcp_args = _runtime_mcp_invocation("codex")
    env_toml = (
        "{" + ",".join(f"{key}={json.dumps(value)}" for key, value in _mcp_env("codex", mcp_extra_env).items()) + "}"
    )
    launch = [
        binary,
        "-c",
        f"mcp_servers.lc.command={json.dumps(command)}",
        "-c",
        "mcp_servers.lc.args=" + json.dumps(mcp_args, separators=(",", ":")),
        "-c",
        f"mcp_servers.lc.env={env_toml}",
        *arguments,
    ]
    return launch, dict(os.environ)


def _runtime_store_root() -> Path:
    configured = os.environ.get("LEMONCROW_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".lemoncrow").resolve()


def _pi_lite_extension_path() -> Path:
    path = Path(__file__).resolve().parent / "assets" / "pi_lite.mjs"
    if not path.is_file():
        raise HostTrialError("Runtime Lite Pi extension is missing from this installation")
    return path


def _pi_launch(
    binary: str,
    arguments: Sequence[str],
    *,
    mcp_extra_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    command, mcp_args = _runtime_mcp_invocation("pi")
    env = dict(os.environ)
    env.update(_mcp_env("pi", mcp_extra_env))
    env["LEMONCROW_PI_LITE_COMMAND"] = command
    env["LEMONCROW_PI_LITE_ARGS"] = json.dumps(mcp_args, separators=(",", ":"))
    # Explicit -e loads only this additional extension; unlike private managed
    # Pi we intentionally do not pass --no-tools/--no-context-files/--no-skills.
    from .pi_integration import enabled, refresh

    if enabled() and not {"--no-extensions", "-ne"}.intersection(arguments):
        try:
            refresh()
        except (OSError, RuntimeError):
            pass  # The installed copy still loads; it is only older than this client.
        return [binary, *arguments], env
    return [binary, "-e", str(_pi_lite_extension_path()), *arguments], env


def _opencode_launch(
    binary: str,
    arguments: Sequence[str],
    *,
    mcp_extra_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    command, mcp_args = _runtime_mcp_invocation("opencode")
    env = dict(os.environ)
    overlay: dict[str, object] = {}
    existing = env.get("OPENCODE_CONFIG_CONTENT", "").strip()
    if existing:
        try:
            parsed = json.loads(existing)
            if isinstance(parsed, dict):
                overlay = parsed
        except (TypeError, ValueError) as exc:
            raise HostTrialError(
                "OPENCODE_CONFIG_CONTENT is not valid JSON; refusing to replace the existing override"
            ) from exc
    mcp = overlay.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
        overlay["mcp"] = mcp
    mcp["lc"] = {
        "type": "local",
        "command": [command, *mcp_args],
        "environment": _mcp_env("opencode", mcp_extra_env),
    }
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(overlay, separators=(",", ":"))
    return [binary, *arguments], env


def build_trial_launch(
    host: str,
    arguments: Sequence[str],
    *,
    mcp_extra_env: Mapping[str, str] | None = None,
    binary_override: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build one host process with an ephemeral LemonCrow MCP config."""
    if host not in TRIAL_HOSTS:
        raise HostTrialError(f"unsupported host {host!r}; choose claude, codex, or opencode")
    binary = binary_override
    if binary is None and host == "pi":
        try:
            from .pi_host import PiHostError, resolve_host_binary

            binary = resolve_host_binary(_runtime_store_root())
        except PiHostError as exc:
            raise HostTrialError(str(exc)) from exc
        if binary is None:
            raise HostTrialError("Pi host is not installed; run `lc rt host install --engine pi`")
    if binary is None:
        binary = shutil.which(host)
    if binary is None:
        raise HostTrialError(f"{_HOST_LABELS[host]} is not installed or not on PATH")
    if host == "claude":
        return _claude_launch(binary, arguments, mcp_extra_env=mcp_extra_env)
    if host == "codex":
        return _codex_launch(binary, arguments, mcp_extra_env=mcp_extra_env)
    if host == "pi":
        return _pi_launch(binary, arguments, mcp_extra_env=mcp_extra_env)
    return _opencode_launch(binary, arguments, mcp_extra_env=mcp_extra_env)


def run_trial_host(
    host: str,
    arguments: Sequence[str],
    *,
    mcp_extra_env: Mapping[str, str] | None = None,
    binary_override: str | None = None,
    workspace_root: Path | None = None,
) -> int:
    workspace = workspace_root.expanduser().resolve() if workspace_root is not None else _trial_workspace_root()
    try:
        from .lite.index import prepare_lite_workspace

        prepare_lite_workspace(workspace)
    except (OSError, RuntimeError) as exc:
        raise HostTrialError(f"could not prepare local Runtime Lite index: {exc}") from exc
    extra = {
        "LEMONCROW_LITE_PREPARED": "1",
        "LEMONCROW_WORKSPACE_ROOT": str(workspace),
        **dict(mcp_extra_env or {}),
    }
    command, env = build_trial_launch(
        host,
        arguments,
        mcp_extra_env=extra,
        binary_override=binary_override,
    )
    try:
        completed = subprocess.run(command, cwd=workspace, env=env, check=False)
    except OSError as exc:
        raise HostTrialError(f"could not launch {host}: {exc}") from exc
    return int(completed.returncode)


__all__ = ["TRIAL_HOSTS", "HostTrialError", "build_trial_launch", "run_trial_host"]
