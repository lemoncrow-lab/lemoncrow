"""Local model-router daemon — proxy the host's main model calls.

Runs a local LiteLLM proxy that reroutes the agent's model calls to
configured providers per a route map (e.g. ``*opus*`` -> ``openai/gpt-5.5``), then
point the host (Claude Code) at it via ANTHROPIC_BASE_URL. LemonCrow already depends
on litellm and has the host_router_bridge decision layer + ANTHROPIC_BASE_URL host
wiring; this adds the runnable daemon + lifecycle the bridge lacked.

The route map lives at ``<root>/router/route.json`` ({pattern: target_model}); an
empty/absent map is anthropic passthrough. The proxy launcher and pid checks are
injectable so the lifecycle is testable without spawning litellm.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PORT = 4000


def router_dir(root: str | Path) -> Path:
    return Path(root) / "router"


def daemon_state_path(root: str | Path) -> Path:
    return router_dir(root) / "daemon.json"


def litellm_config_path(root: str | Path) -> Path:
    return router_dir(root) / "litellm.config.yaml"


def route_map_path(root: str | Path) -> Path:
    return router_dir(root) / "route.json"


def load_route_map(root: str | Path) -> dict[str, str]:
    """User route map {model-name-glob: target litellm model}. Empty if absent."""
    try:
        data = json.loads(route_map_path(root).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


def generate_litellm_config(root: str | Path) -> dict[str, Any]:
    """Build a litellm proxy config: route-map entries + anthropic passthrough."""
    model_list: list[dict[str, Any]] = [
        {"model_name": pattern, "litellm_params": {"model": target}} for pattern, target in load_route_map(root).items()
    ]
    # Everything not explicitly rerouted passes through to Anthropic.
    model_list.append({"model_name": "*", "litellm_params": {"model": "anthropic/*"}})
    return {"model_list": model_list}


def _to_yaml(config: dict[str, Any]) -> str:
    return yaml.safe_dump(config, sort_keys=False)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _spawn_detached(cmd: list[str]) -> int:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


def _host_settings_path(host: str) -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(cfg) / "settings.json"


def _wire_host(host: str, base_url: str) -> str | None:
    """Point the host at the proxy; return the prior ANTHROPIC_BASE_URL (or None)."""
    if host != "claude":
        return None
    path = _host_settings_path(host)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw_env = data.get("env")
    env = raw_env if isinstance(raw_env, dict) else {}
    prev = env.get("ANTHROPIC_BASE_URL")
    env["ANTHROPIC_BASE_URL"] = base_url
    data["env"] = env
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return prev


def _unwire_host(host: str, prev_base_url: str | None) -> None:
    if host != "claude":
        return
    path = _host_settings_path(host)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    raw_env = data.get("env")
    env = raw_env if isinstance(raw_env, dict) else {}
    if prev_base_url is None:
        env.pop("ANTHROPIC_BASE_URL", None)
    else:
        env["ANTHROPIC_BASE_URL"] = prev_base_url
    data["env"] = env
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def status(root: str | Path) -> dict[str, Any]:
    try:
        record = json.loads(daemon_state_path(root).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"running": False}
    pid = int(record.get("pid") or 0)
    alive = _pid_alive(pid)
    return {
        "running": alive,
        "stale": not alive,
        "pid": pid,
        "port": record.get("port"),
        "base_url": record.get("base_url"),
        "host_wired": record.get("host_wired") or "",
    }


def start(
    root: str | Path,
    *,
    port: int = _DEFAULT_PORT,
    wire_host: str | None = None,
    spawn: Callable[[list[str]], int] | None = None,
) -> dict[str, Any]:
    """Generate config, launch the proxy daemon, optionally wire the host."""
    current = status(root)
    if current.get("running"):
        return {**current, "already_running": True}
    router_dir(root).mkdir(parents=True, exist_ok=True)
    cfg_path = litellm_config_path(root)
    cfg_path.write_text(_to_yaml(generate_litellm_config(root)), encoding="utf-8")
    cmd = ["litellm", "--config", str(cfg_path), "--port", str(port)]
    pid = (spawn or _spawn_detached)(cmd)
    base_url = f"http://127.0.0.1:{port}"
    record: dict[str, Any] = {
        "pid": pid,
        "port": port,
        "base_url": base_url,
        "config": str(cfg_path),
        "host_wired": "",
        "prev_base_url": None,
    }
    if wire_host:
        record["prev_base_url"] = _wire_host(wire_host, base_url)
        record["host_wired"] = wire_host
    daemon_state_path(root).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {"running": True, **record}


def stop(root: str | Path) -> dict[str, Any]:
    try:
        record = json.loads(daemon_state_path(root).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stopped": False, "reason": "not running"}
    pid = int(record.get("pid") or 0)
    if _pid_alive(pid):
        _terminate(pid)
    if record.get("host_wired"):
        _unwire_host(str(record["host_wired"]), record.get("prev_base_url"))
    try:
        daemon_state_path(root).unlink()
    except OSError:
        pass
    return {"stopped": True, "pid": pid}


def restart(root: str | Path, *, port: int = _DEFAULT_PORT, wire_host: str | None = None) -> dict[str, Any]:
    stop(root)
    return start(root, port=port, wire_host=wire_host)
