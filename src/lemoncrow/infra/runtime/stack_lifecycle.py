"""Optional visualization-stack process lifecycle (Phase 25-03, QBL-CLI-03).

PID files, status payloads, process-group signalling, and stack-stop logic for
the optional native visualization stack. Moved verbatim from
``gateway/cli/app.py``; the thin ``lemon stack`` command callbacks call these.
Domain errors surface as ``click.ClickException`` only where the original code
already did (frontend-dependency preflight).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import click

from lemoncrow.gateway.integrations.openmemory_lifecycle import project_root as _project_root
from lemoncrow.infra.runtime.daemon_units import (
    DEFAULT_STACK_FRONTEND_PORT,
    DEFAULT_STACK_SERVICE_PORT,
)
from lemoncrow.infra.runtime.servicectl_lifecycle import _pid_is_running


def _stack_dir(root: Path) -> Path:
    return Path(root) / "stack"


def _stack_pid_path(root: Path) -> Path:
    return _stack_dir(root) / "stack.pid"


def _stack_service_pid_path(root: Path) -> Path:
    return _stack_dir(root) / "service.pid"


def _stack_frontend_pid_path(root: Path) -> Path:
    return _stack_dir(root) / "frontend.pid"


def _stack_log_path(root: Path) -> Path:
    return _stack_dir(root) / "stack.log"


def _stack_state_path(root: Path) -> Path:
    return _stack_dir(root) / "state.json"


def _read_pidfile(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _clear_pidfile(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _read_stack_state(root: Path) -> dict[str, Any]:
    path = _stack_state_path(root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_stack_state(root: Path, payload: dict[str, Any]) -> None:
    state_path = _stack_state_path(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _resolve_bin_in_common_locations(name: str, common_paths: list[str]) -> str | None:
    # Try common install locations first, then fall back to a PATH search --
    # the child process (e.g. launchd's minimal-PATH spawner) may not inherit
    # the interactive shell's PATH.
    for path in common_paths:
        if os.path.exists(path):
            return path
    return shutil.which(name)


def _get_npm_path() -> str:
    return (
        _resolve_bin_in_common_locations("npm", ["/opt/homebrew/bin/npm", "/usr/local/bin/npm", "/usr/bin/npm"])
        or "npm"  # Will likely fail, but let's keep the signature consistent
    )


def _get_node_dir(npm_path: str | None = None) -> str | None:
    # npm's own shebang is "#!/usr/bin/env node", so even a fully-resolved
    # npm path (see _get_npm_path) still fails to launch under a minimal
    # PATH -- e.g. launchd's default /usr/bin:/bin:/usr/sbin:/sbin, which
    # has neither -- unless node's directory is added to PATH too.
    #
    # Prefer the node installed alongside the resolved npm (matters for
    # version-managed installs like nvm/volta, where node+npm are paired --
    # picking an unrelated node from a hardcoded location could mismatch the
    # version npm was built/tested against).
    if npm_path and npm_path != "npm":
        sibling_node = os.path.join(os.path.dirname(npm_path), "node")
        if os.path.exists(sibling_node):
            return os.path.dirname(sibling_node)
    node_bin = _resolve_bin_in_common_locations(
        "node", ["/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"]
    )
    return os.path.dirname(node_bin) if node_bin else None


def _stack_frontend_dir() -> Path:
    return _project_root() / "frontend"


def _stack_install_command(npm_path: str, frontend_dir: Path) -> list[str]:
    cmd = "ci" if (frontend_dir / "package-lock.json").exists() else "install"
    return [npm_path, cmd]


def _ensure_stack_frontend_dependencies(frontend_dir: Path) -> None:
    if not frontend_dir.exists():
        raise click.ClickException(f"frontend directory not found: {frontend_dir}")
    npm_path = _get_npm_path()
    if npm_path == "npm" and not shutil.which("npm"):
        raise click.ClickException("npm is required to run the optional LemonCrow frontend stack")
    node_modules = frontend_dir / "node_modules"
    vite_bin = node_modules / ".bin" / "vite"
    if node_modules.exists() and vite_bin.exists():
        return
    subprocess.run(_stack_install_command(npm_path, frontend_dir), cwd=frontend_dir, check=True)


def _stack_status_payload(root: Path) -> dict[str, Any]:
    state = _read_stack_state(root)
    runner_pid = _read_pidfile(_stack_pid_path(root))
    service_pid = _read_pidfile(_stack_service_pid_path(root))
    frontend_pid = _read_pidfile(_stack_frontend_pid_path(root))
    runner_running = bool(runner_pid is not None and _pid_is_running(runner_pid))
    service_running = bool(service_pid is not None and _pid_is_running(service_pid))
    frontend_running = bool(frontend_pid is not None and _pid_is_running(frontend_pid))
    return {
        "running": runner_running and service_running and frontend_running,
        "runner_pid": runner_pid,
        "service_pid": service_pid,
        "frontend_pid": frontend_pid,
        "runner_running": runner_running,
        "service_running": service_running,
        "frontend_running": frontend_running,
        "pid_file": str(_stack_pid_path(root)),
        "service_pid_file": str(_stack_service_pid_path(root)),
        "frontend_pid_file": str(_stack_frontend_pid_path(root)),
        "log_file": str(_stack_log_path(root)),
        "state_file": str(_stack_state_path(root)),
        "started_at": state.get("started_at"),
        "service_url": state.get("service_url", f"http://localhost:{DEFAULT_STACK_SERVICE_PORT}"),
        "frontend_url": state.get("frontend_url", f"http://localhost:{DEFAULT_STACK_FRONTEND_PORT}"),
        "last_exit_reason": state.get("last_exit_reason"),
    }


def _tail_text(path: Path, lines: int = 20) -> str:
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def _clear_stack_pidfiles(root: Path) -> None:
    _clear_pidfile(_stack_pid_path(root))
    _clear_pidfile(_stack_service_pid_path(root))
    _clear_pidfile(_stack_frontend_pid_path(root))


def _signal_process_group(pid: int, sig: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    except OSError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)
        return

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _stop_stack_processes(root: Path, *, force: bool) -> dict[str, Any]:
    runner_pid = _read_pidfile(_stack_pid_path(root))
    service_pid = _read_pidfile(_stack_service_pid_path(root))
    frontend_pid = _read_pidfile(_stack_frontend_pid_path(root))

    for pid in (frontend_pid, service_pid, runner_pid):
        if pid is not None and _pid_is_running(pid):
            _signal_process_group(pid, signal.SIGTERM)

    deadline = time.time() + 5
    while time.time() < deadline:
        payload = _stack_status_payload(root)
        if not payload["runner_running"] and not payload["service_running"] and not payload["frontend_running"]:
            break
        time.sleep(0.1)

    if force:
        for pid in (frontend_pid, service_pid, runner_pid):
            if pid is not None and _pid_is_running(pid):
                _signal_process_group(pid, signal.SIGKILL)

    payload = _stack_status_payload(root)
    if not payload["runner_running"]:
        _clear_pidfile(_stack_pid_path(root))
    if not payload["service_running"]:
        _clear_pidfile(_stack_service_pid_path(root))
    if not payload["frontend_running"]:
        _clear_pidfile(_stack_frontend_pid_path(root))
    return _stack_status_payload(root)
