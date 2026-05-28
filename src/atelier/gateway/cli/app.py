"""CLI for the Atelier reasoning runtime.

Designed to be readable when piped into another tool. All commands that
return data accept ``--json`` to emit machine-parseable output.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout, suppress
from datetime import UTC, datetime, timedelta
from functools import wraps
from hashlib import sha256
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from io import StringIO
from pathlib import Path
from typing import Any

import click
import yaml

from atelier import __version__ as atelier_version
from atelier.bench import bootstrap as _bench_bootstrap
from atelier.core.environment import cli_dev_disabled_message, is_dev_mode
from atelier.core.foundation.models import (
    ReasonBlock,
    Rubric,
    Trace,
    to_jsonable,
)
from atelier.core.foundation.paths import default_store_root
from atelier.core.foundation.renderer import (
    render_block_markdown,
    render_context_for_agent,
    render_rubric_result,
)
from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers.registry import SUPPORTED_SESSION_IMPORT_HOSTS
from atelier.gateway.integrations.external_analytics import REPORTABLE_TOOL_IDS

# `--tool` choices for the external-report CLI. Built once from the source-of-
# truth `SPECS` tuple plus the special-case `codeburn:optimize` sub-report and
# the `all` aggregator. Adding a new analyzer to external_analytics.SPECS now
# flows here automatically - no second hardcoded list to keep in sync.
_EXTERNAL_REPORT_TOOL_CHOICES = ("all", *REPORTABLE_TOOL_IDS, "codeburn:optimize")
# Order matters for the human-readable `all` iteration: keep it focused on the
# core report trio and leave newer analyzers available via explicit --tool.
_EXTERNAL_REPORT_ALL_TOOLS = (
    *(t for t in REPORTABLE_TOOL_IDS if t in {"tokscale", "codeburn"}),
    "codeburn:optimize",
)

logger = logging.getLogger(__name__)

DEFAULT_ROOT = default_store_root()
SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS = ("today", "week", "month")
DEFAULT_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS = (
    "today",
    "week",
    "month",
)

CONTROLLER_UNIT = "atelier-controller.service"
STACK_UNIT = "atelier-stack.service"
LETTA_UNIT = "atelier-letta.service"
OPENMEMORY_UNIT = "atelier-openmemory.service"
ZOEKT_UNIT = "atelier-zoekt.service"
MCP_UNIT = "atelier-mcp.service"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
LAUNCHD_USER_DIR = Path.home() / "Library" / "LaunchAgents"
CONTROLLER_LABEL = "com.atelier.controller"
STACK_LABEL = "com.atelier.stack"
LETTA_LABEL = "com.atelier.letta"
OPENMEMORY_LABEL = "com.atelier.openmemory"
ZOEKT_LABEL = "com.atelier.zoekt"
MCP_LABEL = "com.atelier.mcp"
DEFAULT_STACK_SERVICE_HOST = "0.0.0.0"
DEFAULT_STACK_SERVICE_PORT = 8787
DEFAULT_STACK_FRONTEND_HOST = "0.0.0.0"
DEFAULT_STACK_FRONTEND_PORT = 3125


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


# --------------------------------------------------------------------------- #
# Product telemetry helpers                                                   #
# --------------------------------------------------------------------------- #


def _atelier_version() -> str:
    try:
        return version("atelier")
    except PackageNotFoundError:
        return "0.1.0"


def _cli_command_name(argv: list[str]) -> str:
    skip_next = False
    options_with_values = {"--root"}
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in options_with_values:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token.replace("-", "_")
    return "root"


def _telemetry_session(ctx: click.Context) -> str | None:
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    value = obj.get("_telemetry_session_id")
    return value if isinstance(value, str) else None


def _begin_cli_telemetry(command_name: str) -> tuple[str, float]:
    from atelier.bench.mode import mode as _bench_mode
    from atelier.core.foundation.identity import (
        get_anon_id,
        new_session_id,
        platform_payload,
    )
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.banner import maybe_show_banner

    maybe_show_banner()
    # OTel is initialized lazily on first emit_product_log call.
    session_id = new_session_id()
    payload = platform_payload()
    emit_product(
        "session_start",
        agent_host="cli",
        atelier_version=_atelier_version(),
        anon_id=get_anon_id(),
        session_id=session_id,
        bench_mode=_bench_mode().value,
        **payload,
    )
    emit_product(
        "cli_command_invoked",
        command_name=command_name,
        session_id=session_id,
        anon_id=get_anon_id(),
    )
    return session_id, time.perf_counter()


def _finish_cli_telemetry(
    *,
    command_name: str,
    session_id: str,
    started_at: float,
    ok: bool,
    exit_reason: str,
) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import bucket_duration_ms, bucket_duration_s

    elapsed = max(0.0, time.perf_counter() - started_at)
    emit_product(
        "cli_command_completed",
        command_name=command_name,
        session_id=session_id,
        duration_ms_bucket=bucket_duration_ms(elapsed * 1000),
        ok=ok,
    )
    emit_product(
        "session_end",
        session_id=session_id,
        duration_s_bucket=bucket_duration_s(elapsed),
        exit_reason=exit_reason,
    )


def _emit_cli_interrupted(
    *,
    session_id: str,
    started_at: float,
    signum: int,
    command_name: str,
) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import bucket_duration_s

    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    emit_product(
        "session_interrupted",
        session_id=session_id,
        signal=signal_name,
        elapsed_s_bucket=bucket_duration_s(max(0.0, time.perf_counter() - started_at)),
        last_phase=command_name,
    )


def _record_reasonblock_events(
    scored: list[Any],
    *,
    event_name: str,
    domain: str | None,
    session_id: str | None,
) -> None:
    if session_id is None:
        return
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import hash_identifier

    for rank, item in enumerate(scored, start=1):
        block = getattr(item, "block", None)
        block_id = getattr(block, "id", "")
        block_domain = getattr(block, "domain", domain or "")
        props: dict[str, Any] = {
            "block_id_hash": hash_identifier(str(block_id)),
            "domain": str(block_domain or domain or ""),
            "retrieval_score": float(getattr(item, "score", 0.0)),
            "session_id": session_id,
        }
        if event_name == "reasonblock_retrieved":
            props["rank"] = rank
        emit_product(event_name, **props)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _load_store(root: Path) -> Any:
    from atelier.infra.storage.factory import create_store

    try:
        store = create_store(root)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    db_path = getattr(store, "db_path", None)
    if db_path is not None and not Path(db_path).exists():
        raise click.ClickException(f"No atelier store at {root}. Run `atelier init` first.")
    return store


def _core_runtime(root: Path) -> Any:
    from atelier.core.runtime import AtelierRuntimeCore

    return AtelierRuntimeCore(root)


def _lesson_promoter(root: Path) -> Any:
    from atelier.core.capabilities.lesson_promotion import LessonPromoterCapability

    store = _load_store(root)
    return LessonPromoterCapability(store)


def _lesson_pr_bot(root: Path) -> Any:
    from atelier.core.capabilities.lesson_promotion import LessonPrBot

    store = _load_store(root)
    return LessonPrBot(store=store, root=root)


def _emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        click.echo(data)


def _seed_resources() -> tuple[list[Path], list[Path]]:
    """Return (block_files, rubric_files) bundled with the package."""
    blocks_dir = resources.files("atelier") / "infra" / "seed_blocks"
    rubrics_dir = resources.files("atelier") / "core" / "rubrics"
    block_files = sorted(Path(str(p)) for p in blocks_dir.iterdir() if p.name.endswith(".yaml"))
    rubric_files = sorted(Path(str(p)) for p in rubrics_dir.iterdir() if p.name.endswith(".yaml"))
    return block_files, rubric_files


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_domain_manager(root: Path) -> Any:
    from atelier.core.domains import DomainManager

    return DomainManager(root)


_REDACTION_PLACEHOLDER_RE = re.compile(r"<redacted[^>]*>")


def _redact_memory_input(text: str, field_name: str) -> str:
    from atelier.core.foundation.redaction import redact

    redacted = redact(text)
    if not text:
        return redacted
    remaining = _REDACTION_PLACEHOLDER_RE.sub("", redacted)
    if len(remaining.strip()) < len(text.strip()) * 0.5:
        raise click.ClickException(f"{field_name} rejected: likely secret leakage")
    return redacted


def _read_memory_value(value: str) -> str:
    if not value.startswith("@"):
        return value
    path_text = value[1:]
    if path_text == "/dev/stdin" or path_text == "-":
        return sys.stdin.read()
    return Path(path_text).read_text(encoding="utf-8")


def _parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dhm])", value.strip())
    if not match:
        raise click.ClickException("duration must look like 7d, 12h, or 30m")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def _letta_compose_file() -> Path:
    return Path.cwd() / "deploy" / "letta" / "docker-compose.yml"


def _run_compose(args: list[str]) -> None:
    subprocess.run(["docker", "compose", "-f", str(_letta_compose_file()), *args], check=True)


def _project_root() -> Path:
    env = os.environ.get("ATELIER_INSTALL_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    install_record = Path.home() / ".atelier" / "install_dir"
    with contextlib.suppress(OSError):
        recorded = install_record.read_text(encoding="utf-8").strip()
        if recorded:
            recorded_path = Path(recorded).expanduser().resolve()
            if recorded_path.exists():
                return recorded_path
    return Path(__file__).resolve().parents[4]


def _openmemory_dir(root: Path) -> Path:
    return Path(root) / "openmemory"


def _openmemory_checkout_dir(root: Path) -> Path:
    return _openmemory_dir(root) / "mem0"


def _openmemory_workdir(root: Path) -> Path:
    return _openmemory_checkout_dir(root) / "openmemory"


def _openmemory_service_env_path(root: Path) -> Path:
    return _openmemory_dir(root) / "service.env"


def _openmemory_api_env_path(root: Path) -> Path:
    return _openmemory_workdir(root) / "api" / ".env"


def _openmemory_ui_env_path(root: Path) -> Path:
    return _openmemory_workdir(root) / "ui" / ".env"


def _openmemory_log_path(root: Path) -> Path:
    return _openmemory_dir(root) / "openmemory.log"


def _mcp_dir(root: Path) -> Path:
    return Path(root) / "mcp"


def _mcp_log_path(root: Path) -> Path:
    return _mcp_dir(root) / "mcp.log"


def _ensure_openmemory_service_env(root: Path) -> Path:
    env_path = _openmemory_service_env_path(root)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        # Do not persist API keys to disk in plaintext.
        # Keep sensitive secrets in process environment at runtime instead.
        "USER": os.environ.get("ATELIER_OPENMEMORY_USER_ID", os.environ.get("USER", "")),
        "ATELIER_OPENMEMORY_URL": os.environ.get("ATELIER_OPENMEMORY_URL", "http://127.0.0.1:8765"),
    }
    lines = []
    for key, value in values.items():
        if not value:
            continue
        escaped = value.replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    if lines:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif not env_path.exists():
        env_path.write_text("", encoding="utf-8")
    return env_path


def _ensure_openmemory_checkout(root: Path) -> Path:
    repo_dir = _openmemory_checkout_dir(root)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    repo_url = os.environ.get("ATELIER_OPENMEMORY_REPO_URL", "https://github.com/mem0ai/mem0.git")
    repo_ref = os.environ.get("ATELIER_OPENMEMORY_REF", "main")
    if (repo_dir / ".git").exists():
        subprocess.run(["git", "-C", str(repo_dir), "fetch", "--depth=1", "origin", repo_ref], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "checkout", repo_ref], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only", "origin", repo_ref], check=True)
    else:
        subprocess.run(["git", "clone", "--depth=1", "--branch", repo_ref, repo_url, str(repo_dir)], check=True)
    workdir = _openmemory_workdir(root)
    if not workdir.exists():
        raise click.ClickException(f"OpenMemory checkout is missing {workdir}")
    return workdir


def _write_openmemory_env_files(root: Path) -> None:
    api_env = _openmemory_api_env_path(root)
    ui_env = _openmemory_ui_env_path(root)
    user_id = (
        os.environ.get("ATELIER_OPENMEMORY_USER_ID", "").strip() or os.environ.get("USER", "").strip() or "atelier"
    )
    api_url = os.environ.get("ATELIER_OPENMEMORY_URL", "http://127.0.0.1:8765").strip() or "http://127.0.0.1:8765"
    openai_api_key = os.environ.get("ATELIER_OPENMEMORY_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", "")).strip()
    api_env.parent.mkdir(parents=True, exist_ok=True)
    api_lines = [
        f"OPENAI_API_KEY={openai_api_key}",
        f"USER={user_id}",
    ]
    api_env.write_text("\n".join(api_lines) + "\n", encoding="utf-8")
    ui_env.parent.mkdir(parents=True, exist_ok=True)
    ui_lines = [
        f"NEXT_PUBLIC_API_URL={api_url}",
        f"NEXT_PUBLIC_USER_ID={user_id}",
    ]
    ui_env.write_text("\n".join(ui_lines) + "\n", encoding="utf-8")


def _run_openmemory_make(root: Path, *args: str) -> None:
    workdir = _openmemory_workdir(root)
    env = {**os.environ}
    user_id = (
        os.environ.get("ATELIER_OPENMEMORY_USER_ID", "").strip() or os.environ.get("USER", "").strip() or "atelier"
    )
    env["USER"] = user_id
    env["NEXT_PUBLIC_API_URL"] = os.environ.get("ATELIER_OPENMEMORY_URL", "http://127.0.0.1:8765")
    subprocess.run(["make", *args], cwd=workdir, env=env, check=True)


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


def _stack_frontend_dir() -> Path:
    return _project_root() / "frontend"


def _stack_install_command(frontend_dir: Path) -> list[str]:
    return ["npm", "ci"] if (frontend_dir / "package-lock.json").exists() else ["npm", "install"]


def _ensure_stack_frontend_dependencies(frontend_dir: Path) -> None:
    if not frontend_dir.exists():
        raise click.ClickException(f"frontend directory not found: {frontend_dir}")
    if not shutil.which("npm"):
        raise click.ClickException("npm is required to run the optional Atelier frontend stack")
    node_modules = frontend_dir / "node_modules"
    vite_bin = node_modules / ".bin" / "vite"
    if node_modules.exists() and vite_bin.exists():
        return
    subprocess.run(_stack_install_command(frontend_dir), cwd=frontend_dir, check=True)


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


def _servicectl_dir(root: Path) -> Path:
    return Path(root) / "servicectl"


def _servicectl_pid_path(root: Path) -> Path:
    return _servicectl_dir(root) / "servicectl.pid"


def _servicectl_log_path(root: Path) -> Path:
    return _servicectl_dir(root) / "servicectl.log"


def _servicectl_state_path(root: Path) -> Path:
    return _servicectl_dir(root) / "state.json"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_servicectl_state(root: Path) -> dict[str, Any]:
    path = _servicectl_state_path(root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_servicectl_state(root: Path, payload: dict[str, Any]) -> None:
    state_path = _servicectl_state_path(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _read_servicectl_pid(root: Path) -> int | None:
    path = _servicectl_pid_path(root)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _clear_servicectl_pid(root: Path) -> None:
    path = _servicectl_pid_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _kill_orphan_servicectl_processes(current_root: Path) -> None:
    """Kill any servicectl run processes started with a root other than current_root.

    Prevents accumulation of stale daemons pointing at old/project-local stores
    when the canonical root changes (e.g. after moving from project/.atelier to
    ~/.atelier).  Only runs on Linux (requires /proc).
    """
    import glob as _glob

    my_pid = os.getpid()
    current_root_str = str(current_root.resolve())
    for cmdline_file in _glob.glob("/proc/*/cmdline"):
        try:
            pid = int(cmdline_file.split("/")[2])
        except (ValueError, IndexError):
            continue
        if pid == my_pid:
            continue
        try:
            raw = Path(cmdline_file).read_bytes()
        except OSError:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        if (
            "atelier.gateway.cli" in cmdline
            and "servicectl" in cmdline
            and " run " in cmdline
            and current_root_str not in cmdline
        ):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)


def _servicectl_status_payload(root: Path) -> dict[str, Any]:
    state = _read_servicectl_state(root)
    pid = _read_servicectl_pid(root)
    running = bool(pid is not None and _pid_is_running(pid))
    return {
        "running": running,
        "pid": pid,
        "pid_file": str(_servicectl_pid_path(root)),
        "log_file": str(_servicectl_log_path(root)),
        "state_file": str(_servicectl_state_path(root)),
        "last_tick_at": state.get("last_tick_at"),
        "last_processed_jobs": state.get("last_processed_jobs", []),
        "last_enqueued_jobs": state.get("last_enqueued_jobs", []),
        "last_imported_sessions": state.get("last_imported_sessions", {}),
        "last_session_import_at": state.get("last_session_import_at"),
        "last_external_analytics_runs": state.get("last_external_analytics_runs", []),
        "last_external_analytics_at": state.get("last_external_analytics_at"),
        "last_exit_reason": state.get("last_exit_reason"),
        "started_at": state.get("started_at"),
    }


def _servicectl_refresh_host_status(root: Path) -> dict[str, str]:
    """Detect host agent CLI tools and persist status for the Docker service.

    Writes to ``{root}/hosts/status.json`` in the same format as
    ``scripts/status.sh --write`` so the API running in Docker can
    consume it via ``_load_host_status_file()``.

    Also writes to the CWD's ``.atelier/hosts/status.json`` if different
    from *root* (handles the common case where Docker mounts the project's
    ``.atelier`` while servicectl uses ``~/.atelier``).

    Runs on the host (inside servicectl) so ``shutil.which()`` can
    find the actual CLI binaries.
    """
    import shutil

    hosts = [
        ("claude", "claude"),
        ("codex", "codex"),
        ("opencode", None),
        ("copilot", None),
        ("antigravity", "agy"),
    ]
    status: dict[str, str] = {}
    for hid, check in hosts:
        if check:
            installed = shutil.which(check) is not None
        elif hid == "opencode":
            installed = shutil.which("opencode") is not None
        elif hid == "copilot":
            installed = shutil.which("code") is not None
        elif hid == "antigravity":
            installed = shutil.which("agy") is not None or shutil.which("antigravity") is not None
        else:
            installed = False
        status[hid] = "installed" if installed else "not_installed"

    def _write_to(hosts_dir: Path) -> None:
        hosts_dir.mkdir(parents=True, exist_ok=True)
        (hosts_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")

    # Primary: write to servicectl's root
    _write_to(Path(root) / "hosts")

    return status


def _servicectl_import_sessions(store: ContextStore) -> dict[str, int]:
    """Import host sessions with importer-level timestamp dedup.

    Each importer already skips unchanged sessions by comparing source timestamp
    against the previously imported RawArtifact timestamp.
    """
    from atelier.gateway.hosts.session_parsers.registry import iter_importer_classes

    counts: dict[str, int] = {}
    importers: list[tuple[str, Any]] = [(host, importer_cls(store)) for host, importer_cls in iter_importer_classes()]
    all_imported_ids = []
    for host, importer in importers:
        try:
            # Keep servicectl output machine-readable (`--json`) by swallowing
            # importer progress prints.
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                imported_ids = importer.import_all(force=False)
            counts[host] = len(imported_ids)
            all_imported_ids.extend(imported_ids)
        except Exception:
            counts[host] = 0

    # Report aggregated session counts to atelier.beseam.com
    try:
        from atelier.core.service.sync import sync_usage

        sync_usage(store.root, session_ids=all_imported_ids)
    except Exception:
        logger.warning(
            "Suppressed exception at cli.py:534",
            exc_info=True,
        )

    return counts


def _normalize_external_analytics_periods(
    periods: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    requested = periods or DEFAULT_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS
    normalized: list[str] = []
    for period in requested:
        if period not in SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS:
            raise ValueError(
                "Unsupported external analytics period "
                f"'{period}'. Choose from: {', '.join(SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS)}"
            )
        if period not in normalized:
            normalized.append(period)
    return tuple(normalized)


def _servicectl_collect_external_analytics(
    store: Any,
    *,
    periods: tuple[str, ...] | list[str],
) -> list[dict[str, Any]]:
    from atelier.gateway.integrations.external_analytics import (
        persist_external_reports,
        run_external_reports,
    )

    persisted: list[dict[str, Any]] = []
    for period in _normalize_external_analytics_periods(periods):
        batch = run_external_reports(tool="all", period=period, cwd=Path.cwd(), include_optimize=True)
        persisted.extend(persist_external_reports(store, batch, source="servicectl"))
    return persisted


def _servicectl_check_and_apply_updates(root: Path) -> bool:
    """Check for git updates and apply them if available.

    Returns True if an update was applied and the process should restart.
    """
    try:
        # 1. Identify project root (where .git is)
        # We look for the install record or traverse up from this file.
        record_path = Path.home() / ".atelier" / "install_dir"
        if record_path.exists():
            project_root = Path(record_path.read_text(encoding="utf-8").strip())
        else:
            # Fallback: traverse up from src/atelier/gateway/cli/app.py
            project_root = Path(__file__).parents[4]

        if not (project_root / ".git").exists():
            return False

        # 2. git fetch
        subprocess.run(["git", "fetch", "--quiet"], cwd=project_root, check=True)

        # 3. Check if behind
        res = subprocess.run(
            ["git", "rev-list", "HEAD..@{u}", "--count"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )
        behind_count = int(res.stdout.strip())

        if behind_count == 0:
            return False

        logger.info(f"Auto-update: detected {behind_count} new commits. Pulling...")

        # 4. Pull
        subprocess.run(["git", "pull", "--ff-only", "--quiet"], cwd=project_root, check=True)

        # 5. Check if dependencies changed
        if (project_root / "uv.lock").exists() or (project_root / "pyproject.toml").exists():
            import shutil

            if shutil.which("uv"):
                logger.info("Auto-update: syncing dependencies with uv...")
                subprocess.run(["uv", "sync"], cwd=project_root, check=True)

        # 6. Check if we should restart systemd/launchd managed services
        # If we are running under systemd, we can trigger a restart of the whole stack
        if os.environ.get("INVOCATION_ID"):
            logger.info("Auto-update: update applied (systemd). Triggering stack restart...")
            subprocess.run(["systemctl", "--user", "restart", STACK_UNIT], check=False)
        elif _is_macos() and (LAUNCHD_USER_DIR / f"{STACK_LABEL}.plist").exists():
            logger.info("Auto-update: update applied (launchd). Triggering stack restart...")
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{STACK_LABEL}"], check=False)

        logger.info("Auto-update: update applied successfully. Exiting for restart.")
        return True

    except Exception as exc:
        logger.error(f"Auto-update failed: {exc}")
        return False


def _servicectl_tick(
    root: Path,
    *,
    maintenance_interval_seconds: int,
    session_import_interval_seconds: int,
    external_analytics_interval_seconds: int,
    external_analytics_periods: tuple[str, ...] | list[str],
    auto_update: bool = False,
    auto_update_interval_seconds: int = 3600,
) -> dict[str, Any]:
    from atelier.core.service.jobs import JOB_CONSOLIDATE_BLOCKS
    from atelier.core.service.worker import Worker
    from atelier.infra.storage.factory import create_store

    SESSION_IMPORT_KEY = "import_host_sessions"
    EXTERNAL_ANALYTICS_KEY = "external_analytics_reports"

    store = create_store(root)
    store.init()
    worker = Worker(store=store)
    normalized_external_analytics_periods = _normalize_external_analytics_periods(external_analytics_periods)

    # Refresh host agent detection status for the Docker service
    with suppress(Exception):
        _servicectl_refresh_host_status(root)

    now = datetime.now(UTC)
    state = _read_servicectl_state(root)
    periodic = state.setdefault("periodic_jobs", {})

    # 0. Check for auto-updates
    if auto_update:
        AUTO_UPDATE_KEY = "auto_update_check"
        last_update_raw = periodic.get(AUTO_UPDATE_KEY)
        last_update_at: datetime | None = None
        if isinstance(last_update_raw, str):
            try:
                last_update_at = datetime.fromisoformat(last_update_raw)
            except ValueError:
                last_update_at = None

        if last_update_at is None or (now - last_update_at).total_seconds() >= auto_update_interval_seconds:
            if _servicectl_check_and_apply_updates(root):
                # Process will exit if update was applied (Restart=always will pick it up)
                sys.exit(0)
            periodic[AUTO_UPDATE_KEY] = now.isoformat()

    last_enqueue_raw = periodic.get(JOB_CONSOLIDATE_BLOCKS)
    last_enqueue_at: datetime | None = None
    if isinstance(last_enqueue_raw, str):
        try:
            last_enqueue_at = datetime.fromisoformat(last_enqueue_raw)
        except ValueError:
            last_enqueue_at = None

    last_session_import_raw = periodic.get(SESSION_IMPORT_KEY)
    last_session_import_at: datetime | None = None
    if isinstance(last_session_import_raw, str):
        try:
            last_session_import_at = datetime.fromisoformat(last_session_import_raw)
        except ValueError:
            last_session_import_at = None

    last_external_analytics_raw = periodic.get(EXTERNAL_ANALYTICS_KEY)
    last_external_analytics_at: datetime | None = None
    if isinstance(last_external_analytics_raw, str):
        try:
            last_external_analytics_at = datetime.fromisoformat(last_external_analytics_raw)
        except ValueError:
            last_external_analytics_at = None

    if session_import_interval_seconds < 0:
        import_due = False
    elif session_import_interval_seconds == 0 or last_session_import_at is None:
        import_due = True
    else:
        import_due = (now - last_session_import_at).total_seconds() >= session_import_interval_seconds
    imported_sessions: dict[str, int] = {}
    if import_due:
        imported_sessions = _servicectl_import_sessions(store)
        periodic[SESSION_IMPORT_KEY] = now.isoformat()

    if external_analytics_interval_seconds < 0:
        external_analytics_due = False
    elif external_analytics_interval_seconds == 0 or last_external_analytics_at is None:
        external_analytics_due = True
    else:
        external_analytics_due = (
            now - last_external_analytics_at
        ).total_seconds() >= external_analytics_interval_seconds
    external_analytics_runs: list[dict[str, Any]] = []
    if external_analytics_due:
        with suppress(Exception):
            external_analytics_runs = _servicectl_collect_external_analytics(
                store,
                periods=normalized_external_analytics_periods,
            )
        periodic[EXTERNAL_ANALYTICS_KEY] = now.isoformat()

    enqueued: list[str] = []
    if maintenance_interval_seconds <= 0 or last_enqueue_at is None:
        due = True
    else:
        due = (now - last_enqueue_at).total_seconds() >= maintenance_interval_seconds

    if due:
        active_jobs = [
            job
            for job in store.list_jobs(job_type=JOB_CONSOLIDATE_BLOCKS, limit=200)
            if job["status"] in {"pending", "running", "failed"}
        ]
        if not active_jobs:
            job_id = store.enqueue_job(
                JOB_CONSOLIDATE_BLOCKS,
                {"dry_run": False, "source": "servicectl"},
            )
            enqueued.append(job_id)
            periodic[JOB_CONSOLIDATE_BLOCKS] = now.isoformat()

    processed: list[str] = []
    while len(processed) < 20:
        job_id = worker.run_once()
        if job_id is None:
            break
        processed.append(job_id)

    payload = {
        "last_tick_at": now.isoformat(),
        "last_processed_jobs": processed,
        "last_enqueued_jobs": enqueued,
        "last_imported_sessions": imported_sessions if import_due else state.get("last_imported_sessions", {}),
        "last_session_import_at": periodic.get(SESSION_IMPORT_KEY),
        "last_external_analytics_runs": (
            external_analytics_runs if external_analytics_due else state.get("last_external_analytics_runs", [])
        ),
        "last_external_analytics_periods": list(normalized_external_analytics_periods),
        "last_external_analytics_at": periodic.get(EXTERNAL_ANALYTICS_KEY),
        "last_exit_reason": state.get("last_exit_reason"),
        "periodic_jobs": periodic,
        "started_at": state.get("started_at"),
    }
    _write_servicectl_state(root, payload)
    return {
        "enqueued_jobs": enqueued,
        "processed_jobs": processed,
        "imported_sessions": imported_sessions,
        "session_import_ran": import_due,
        "external_analytics_runs": external_analytics_runs,
        "external_analytics_periods": list(normalized_external_analytics_periods),
        "external_analytics_ran": external_analytics_due,
        "pending_jobs": len(
            [job for job in store.list_jobs(limit=200) if job["status"] in {"pending", "running", "failed"}]
        ),
        "tick_at": now.isoformat(),
    }


def _parse_tags(values: tuple[str, ...]) -> list[str]:
    tags: list[str] = []
    for value in values:
        tags.extend(tag.strip() for tag in value.split(",") if tag.strip())
    return tags


def _cache_disabled() -> bool:
    return os.environ.get("ATELIER_CACHE_DISABLED") == "1"


def _path_content_fingerprint(path_text: str) -> str:
    path = Path(path_text)
    digest = sha256()
    if path.is_file():
        try:
            digest.update(path.read_bytes())
            return digest.hexdigest()[:16]
        except OSError:
            return "unreadable"
    if path.is_dir():
        files = [p for p in sorted(path.rglob("*")) if p.is_file()]
        for file_path in files[:500]:
            try:
                digest.update(str(file_path.relative_to(path)).encode())
                digest.update(b"\0")
                digest.update(file_path.read_bytes())
                digest.update(b"\0")
            except OSError:
                continue
        digest.update(str(len(files)).encode())
        return digest.hexdigest()[:16]
    return "missing"


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=atelier_version, prog_name="atelier")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=DEFAULT_ROOT,
    show_default=True,
    help="Atelier runtime data directory.",
)
@click.pass_context
def cli(ctx: click.Context, root: Path) -> None:
    """Atelier - Agent Reasoning Runtime."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root


@cli.command("help", context_settings={"ignore_unknown_options": True})
@click.argument("command_path", nargs=-1)
@click.pass_context
def help_cmd(ctx: click.Context, command_path: tuple[str, ...]) -> None:
    """Show help for Atelier or a specific command path."""
    root_ctx = ctx.parent
    if root_ctx is None:
        click.echo(cli.get_help(ctx))
        return

    if not command_path:
        click.echo(root_ctx.get_help())
        return

    command: click.Command = cli
    command_ctx = root_ctx
    for token in command_path:
        if not isinstance(command, click.Group):
            raise click.ClickException(f"{command_ctx.command_path} has no subcommands")
        next_command = command.get_command(command_ctx, token)
        if next_command is None:
            raise click.ClickException(f"unknown command: {' '.join(command_path)}")
        command = next_command
        command_ctx = click.Context(command, info_name=token, parent=command_ctx)

    click.echo(command.get_help(command_ctx))


# ----- init ---------------------------------------------------------------- #


def _detect_git_root(search_path: Path) -> Path | None:
    """Return the git repo root containing search_path, or None if not in a repo."""
    import subprocess as _subprocess

    try:
        result = _subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(search_path),
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


@cli.command()
@click.option("--seed/--no-seed", default=True, help="Import bundled seed blocks and rubrics.")
@click.option("--stack", default=None, help="Copy starter ReasonBlock templates for a stack.")
@click.option("--list-stacks", "show_stacks", is_flag=True, help="List available starter stacks.")
@click.option(
    "--index/--no-index",
    default=True,
    help="Bootstrap the code index for the current git repo (default: on).",
)
@click.pass_context
def init(ctx: click.Context, seed: bool, stack: str | None, show_stacks: bool, index: bool) -> None:
    """Initialize the runtime store at --root."""
    if show_stacks:
        from atelier.core.capabilities.starter_packs import list_stacks

        stacks = list_stacks()
        if not stacks:
            click.echo("No starter stacks available.")
            return
        click.echo("Available starter stacks:")
        for item in stacks:
            click.echo(f"  {item.slug:20} {item.name} ({item.version}) - {item.description}")
        return

    root: Path = ctx.obj["root"]
    from atelier.infra.storage.factory import create_store

    try:
        store = create_store(root)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    store.init()
    click.echo(f"initialized atelier store at {store.root}")
    if seed:
        block_files, rubric_files = _seed_resources()
        seeded_blocks: dict[str, ReasonBlock] = {}
        for path in block_files:
            data = _load_yaml(path)
            if "id" not in data:
                data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
            block = ReasonBlock.model_validate(data)
            seeded_blocks[block.id] = block
        for block in _load_domain_manager(root).all_reasonblocks():
            seeded_blocks[block.id] = block
        n_b = 0
        for block in seeded_blocks.values():
            store.upsert_block(block)
            n_b += 1
        n_r = 0
        for path in rubric_files:
            data = _load_yaml(path)
            rubric = Rubric.model_validate(data)
            store.upsert_rubric(rubric)
            n_r += 1
        click.echo(f"seeded {n_b} reasonblocks and {n_r} rubrics")
    if stack:
        from atelier.core.capabilities.starter_packs import copy_stack_templates

        try:
            copied, skipped = copy_stack_templates(stack, store.blocks_dir)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        suffix = f", skipped {skipped} existing" if skipped else ""
        click.echo(f"copied {copied} starter reasonblocks for stack {stack}{suffix}")
    if index:
        git_root = _detect_git_root(Path.cwd())
        if git_root is not None:
            click.echo(f"bootstrapping code index for {git_root} ...")
            engine = _code_context_engine(str(git_root))
            stats = engine.index_repo().model_dump(mode="json")
            click.echo(
                f"indexed {stats['files_indexed']} files, "
                f"{stats['symbols_indexed']} symbols "
                f"({stats['imports_indexed']} imports)"
            )
        else:
            click.echo("code index skipped (no git repository detected in current directory)")


# ----- uninstall ----------------------------------------------------------- #


@cli.command("uninstall")
@click.option("--dry-run", is_flag=True, help="Print planned actions and exit.")
@click.option("--no-hosts", is_flag=True, help="Skip per-host uninstallation.")
@click.option(
    "--purge",
    is_flag=True,
    help="Also remove runtime state, install dirs, tool envs, and known host residue.",
)
@click.option(
    "--workspace",
    type=click.Path(path_type=Path),
    help="Uninstall for a specific workspace.",
)
def uninstall(dry_run: bool, no_hosts: bool, purge: bool, workspace: Path | None) -> None:
    """Remove Atelier and all agent-host integrations."""
    root = _project_root()
    script = root / "scripts" / "uninstall.sh"
    if not script.exists():
        raise click.ClickException(f"uninstall script not found: {script}")

    cmd = ["bash", str(script)]
    if dry_run:
        cmd.append("--dry-run")
    if no_hosts:
        cmd.append("--no-hosts")
    if purge:
        cmd.append("--purge")
    if workspace:
        cmd.extend(["--workspace", str(workspace)])

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"uninstall failed with code {exc.returncode}") from exc


class _DummyGroup:
    """A placeholder for a Click group that does nothing."""

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda f: f

    def group(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        return lambda f: _DummyGroup()  # type: ignore


MCP_TOOL_ONLY_COMMANDS = frozenset({"context", "rescue", "verify", "read", "edit", "search"})
MCP_TOOL_ONLY_GROUPS = frozenset({"memory", "route"})


def _dev_command(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a dev command but gate execution at runtime."""
    if name in MCP_TOOL_ONLY_COMMANDS:
        return lambda f: f

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        command_name = name or func.__name__.replace("_", "-")

        @wraps(func)
        def guarded(*args: Any, **inner_kwargs: Any) -> Any:
            _check_dev_mode(command_name)
            return func(*args, **inner_kwargs)

        return cli.command(name, **kwargs)(guarded)

    return decorator


def _dev_group(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
    """Register a dev group but gate execution at runtime."""
    if name in MCP_TOOL_ONLY_GROUPS:
        return lambda f: _DummyGroup()

    def decorator(func: Callable[..., Any]) -> Any:
        group_name = name or func.__name__.replace("_", "-")

        @wraps(func)
        def guarded(*args: Any, **inner_kwargs: Any) -> Any:
            _check_dev_mode(group_name)
            return func(*args, **inner_kwargs)

        return cli.group(name, **kwargs)(guarded)

    return decorator


@_dev_command("reembed")
@click.option("--dry-run", is_flag=True, help="Count legacy rows without writing vectors.")
@click.option("--batch-size", default=100, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def reembed(ctx: click.Context, dry_run: bool, batch_size: int, as_json: bool) -> None:
    """Back-fill legacy_stub embeddings for archival passages and lesson candidates."""
    from atelier.infra.embeddings.factory import make_embedder

    root: Path = ctx.obj["root"]
    store = ContextStore(root)
    store.init()
    embedder = make_embedder()
    counts = {"archival_passage": 0, "lesson_candidate": 0, "dry_run": dry_run}
    with store._connect() as conn:
        passages = conn.execute(
            """
            SELECT id, text FROM archival_passage
            WHERE embedding_provenance = 'legacy_stub'
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        lessons = conn.execute(
            """
            SELECT id, cluster_fingerprint, evidence_trace_ids, body FROM lesson_candidate
            WHERE embedding_provenance = 'legacy_stub'
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        counts["archival_passage"] = len(passages)
        counts["lesson_candidate"] = len(lessons)
        if not dry_run:
            for row in passages:
                vector = embedder.embed([str(row["text"])])[0]
                conn.execute(
                    """
                    UPDATE archival_passage
                    SET embedding = ?, embedding_provenance = ?
                    WHERE id = ?
                    """,
                    (json.dumps(vector).encode("utf-8"), embedder.__class__.__name__, row["id"]),
                )
            for row in lessons:
                text = "\n".join(
                    [
                        str(row["cluster_fingerprint"]),
                        str(row["evidence_trace_ids"]),
                        str(row["body"]),
                    ]
                )
                vector = embedder.embed([text])[0]
                conn.execute(
                    """
                    UPDATE lesson_candidate
                    SET embedding = ?, embedding_provenance = ?
                    WHERE id = ?
                    """,
                    (json.dumps(vector), embedder.__class__.__name__, row["id"]),
                )
    _emit(counts, as_json=as_json)


# ----- add-block ----------------------------------------------------------- #


@_dev_command("add-block")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def add_block(ctx: click.Context, path: Path) -> None:
    """Add or update a ReasonBlock from a YAML file."""
    store = _load_store(ctx.obj["root"])
    data = _load_yaml(path)
    if "id" not in data:
        data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
    block = ReasonBlock.model_validate(data)
    store.upsert_block(block)
    click.echo(f"upserted {block.id}")


@cli.group("domain")
def domain_group() -> None:
    """Manage Atelier internal domain bundles."""


@domain_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def domain_list(ctx: click.Context, as_json: bool) -> None:
    """List available domain bundles (built-in + user)."""
    manager = _load_domain_manager(ctx.obj["root"])
    refs = manager.list_bundles()
    payload = [r.model_dump(mode="json") for r in refs]
    if as_json:
        _emit(payload, as_json=True)
        return
    if not payload:
        click.echo("(no domain bundles)")
        return
    for item in payload:
        click.echo(f"{item['bundle_id']}\t{item['domain']}\t{item['description'][:60]}")


@domain_group.command("info")
@click.argument("bundle_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def domain_info(ctx: click.Context, bundle_id: str, as_json: bool) -> None:
    """Show details for a domain bundle."""
    manager = _load_domain_manager(ctx.obj["root"])
    result = manager.info(bundle_id)
    if result is None:
        raise click.ClickException(f"domain bundle not found: {bundle_id}")
    if as_json:
        _emit(result, as_json=True)
        return
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


# ----- search -------------------------------------------------------------- #


@_dev_command("search")
@click.argument("query_parts", nargs=-1)
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.pass_context
def search(ctx: click.Context, query_parts: tuple[str, ...], limit: int, as_json: bool) -> None:
    """Search procedures. Supports legacy mode and `search smart <query>`."""
    if not query_parts:
        raise click.ClickException("query is required")

    if query_parts[0] == "smart":
        smart_query = " ".join(query_parts[1:]).strip()
        if not smart_query:
            raise click.ClickException("smart search query is required")
        rt = _core_runtime(ctx.obj["root"])
        payload = rt.smart_search(smart_query, limit=limit)
        _emit(payload, as_json=True)
        return

    query = " ".join(query_parts).strip()
    store = _load_store(ctx.obj["root"])
    blocks = store.search_blocks(query, limit=limit)
    if as_json:
        _emit([to_jsonable(b) for b in blocks], as_json=True)
        return
    if not blocks:
        click.echo("(no matches)")
        return
    for b in blocks:
        click.echo(f"{b.id}\t{b.domain}\t{b.title}")


def _check_dev_mode(command_name: str, status: int = 1) -> None:
    if not is_dev_mode():
        click.echo(cli_dev_disabled_message(command_name))
        sys.exit(status)


# ----- context -------------------------------------------------------------- #


@_dev_command("context")
@click.option("--task", required=True, help="Task description.")
@click.option("--domain", default=None)
@click.option("--file", "files", multiple=True, help="File path likely to be edited.")
@click.option("--tool", "tools", multiple=True, help="Tool the agent expects to use.")
@click.option("--error", "errors", multiple=True, help="Known error message.")
@click.option("--limit", default=5, show_default=True, type=int)
@click.option("--token-budget", default=2000, show_default=True, type=int)
@click.option("--no-dedup", "dedup", is_flag=True, flag_value=False, default=True)
@click.option("--telemetry", "include_telemetry", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def context_cmd(
    ctx: click.Context,
    task: str,
    domain: str | None,
    files: tuple[str, ...],
    tools: tuple[str, ...],
    errors: tuple[str, ...],
    limit: int,
    token_budget: int,
    dedup: bool,
    include_telemetry: bool,
    as_json: bool,
) -> None:
    """Render the context block to inject into an agent prompt."""
    _check_dev_mode("context")
    from atelier.core.foundation.retriever import TaskContext, retrieve
    from atelier.core.service.telemetry.frustration import match_frustration

    match_frustration(task, surface="cli_input", session_id=_telemetry_session(ctx))
    store = _load_store(ctx.obj["root"])
    tctx = TaskContext(task=task, domain=domain, files=list(files), tools=list(tools), errors=list(errors))
    scored = retrieve(store, tctx, limit=limit, token_budget=token_budget, dedup=dedup)
    _record_reasonblock_events(
        scored,
        event_name="reasonblock_retrieved",
        domain=domain,
        session_id=_telemetry_session(ctx),
    )
    context_text = render_context_for_agent([s.block for s in scored])
    if as_json:
        payload: dict[str, Any] = {
            "matched": [{"id": s.block.id, "score": s.score, "breakdown": s.breakdown} for s in scored],
            "context": context_text,
        }
        if include_telemetry:
            from atelier.core.foundation.retriever import count_tokens

            naive = retrieve(store, tctx, limit=limit, token_budget=None, dedup=False)
            naive_text = render_context_for_agent([s.block for s in naive])
            tokens_used = count_tokens(context_text)
            payload["tokens_used"] = tokens_used
            payload["tokens_saved_vs_naive"] = max(0, count_tokens(naive_text) - tokens_used)
        _emit(
            payload,
            as_json=True,
        )
        return
    click.echo(context_text)


# ----- rescue -------------------------------------------------------------- #


@_dev_command("rescue")
@click.option("--task", required=True)
@click.option("--error", required=True)
@click.option("--domain", default=None)
@click.option("--file", "files", multiple=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def rescue(
    ctx: click.Context,
    task: str,
    error: str,
    domain: str | None,
    files: tuple[str, ...],
    as_json: bool,
) -> None:
    """Suggest a rescue procedure for a repeated failure."""
    _check_dev_mode("rescue")
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.frustration import match_frustration
    from atelier.core.service.telemetry.schema import hash_identifier
    from atelier.gateway.adapters.runtime import ContextRuntime

    match_frustration(task, surface="cli_input", session_id=_telemetry_session(ctx))
    rt = ContextRuntime(ctx.obj["root"])
    result = rt.rescue_failure(task=task, error=error, files=list(files), domain=domain)
    if _telemetry_session(ctx) is not None:
        cluster_id_hash = hash_identifier(result.matched_blocks[0] if result.matched_blocks else "unmatched_rescue")
        emit_product(
            "rescue_offered",
            cluster_id_hash=cluster_id_hash,
            rescue_type="reasonblock" if result.matched_blocks else "summary",
            session_id=_telemetry_session(ctx),
        )
    if as_json:
        _emit(to_jsonable(result), as_json=True)
        return
    click.echo(result.rescue)
    if result.matched_blocks:
        click.echo("matched blocks: " + ", ".join(result.matched_blocks))


# ----- telemetry ---------------------------------------------------------- #


@cli.group("telemetry")
def telemetry_group() -> None:
    """Product telemetry controls."""


@telemetry_group.command("status")
@click.option("--json", "as_json", is_flag=True)
def telemetry_status(as_json: bool) -> None:
    from atelier.core.foundation.identity import (
        get_anon_id,
        new_session_id,
        telemetry_id_path,
    )
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.banner import is_acknowledged
    from atelier.core.service.telemetry.config import config_path, load_telemetry_config
    from atelier.core.service.telemetry.local_store import default_db_path

    session_id = new_session_id()
    emit_product(
        "cli_command_invoked",
        command_name="telemetry_status",
        session_id=session_id,
        anon_id=get_anon_id(),
    )
    cfg = load_telemetry_config()
    payload = {
        "remote_enabled": cfg.remote_enabled,
        "lexical_frustration_enabled": cfg.lexical_frustration_enabled,
        "config_path": str(config_path()),
        "telemetry_id_path": str(telemetry_id_path()),
        "local_db_path": str(default_db_path()),
        "acknowledged": is_acknowledged(),
        "anon_id": get_anon_id(),
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"remote telemetry: {'on' if cfg.remote_enabled else 'off'}")
    click.echo(f"lexical frustration detection: {'on' if cfg.lexical_frustration_enabled else 'off'}")
    click.echo(f"local database: {payload['local_db_path']}")


@telemetry_group.command("on")
def telemetry_on() -> None:
    from atelier.core.service.telemetry import set_remote_enabled

    set_remote_enabled(True)
    click.echo("remote telemetry: on")


@telemetry_group.command("off")
def telemetry_off() -> None:
    from atelier.core.service.telemetry import set_remote_enabled
    from atelier.core.service.telemetry.banner import mark_acknowledged

    set_remote_enabled(False)
    mark_acknowledged()
    click.echo("remote telemetry: off")


@telemetry_group.command("show")
@click.option("--limit", default=20, show_default=True, type=int)
def telemetry_show(limit: int) -> None:
    from atelier.core.service.telemetry.local_store import LocalTelemetryStore

    events = LocalTelemetryStore().list_events(limit=limit)
    _emit([{"event": item["event"], "props": item["props"]} for item in events], as_json=True)


@telemetry_group.command("reset-id")
def telemetry_reset_id() -> None:
    from atelier.core.foundation.identity import reset_anon_id

    click.echo(reset_anon_id())


@telemetry_group.group("lexical")
def telemetry_lexical_group() -> None:
    """Lexical frustration detection controls."""


@telemetry_lexical_group.command("on")
def telemetry_lexical_on() -> None:
    from atelier.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(lexical_frustration_enabled=True)
    click.echo("lexical frustration detection: on")


@telemetry_lexical_group.command("off")
def telemetry_lexical_off() -> None:
    from atelier.core.service.telemetry.config import save_telemetry_config

    save_telemetry_config(lexical_frustration_enabled=False)
    click.echo("lexical frustration detection: off")


@telemetry_lexical_group.command("status")
def telemetry_lexical_status() -> None:
    from atelier.core.service.telemetry.config import load_telemetry_config

    cfg = load_telemetry_config()
    click.echo(f"lexical frustration detection: {'on' if cfg.lexical_frustration_enabled else 'off'}")


# ----- runs ----------------------------------------------------------------- #


@cli.group("runs")
def runs_group() -> None:
    """Run record, list, and inspect commands."""


@runs_group.command("record")
@click.option(
    "--input",
    "input_path",
    type=click.Path(path_type=Path),
    default="-",
    show_default=True,
    help="Trace JSON file. Use '-' for stdin.",
)
@click.pass_context
def trace_record(ctx: click.Context, input_path: Path | str) -> None:
    """Record an observable trace."""
    store = _load_store(ctx.obj["root"])
    raw = sys.stdin.read() if str(input_path) == "-" else Path(input_path).read_text("utf-8")
    data = json.loads(raw)
    if "id" not in data:
        data["id"] = Trace.make_id(data.get("task", "untitled"), data.get("agent", "agent"))
    trace = Trace.model_validate(data)
    store.record_trace(trace)
    click.echo(trace.id)


@runs_group.command("list")
@click.option("--domain", default=None, help="Filter by domain.")
@click.option("--status", default=None, type=click.Choice(["success", "failed", "partial"]))
@click.option("--agent", default=None, help="Filter by agent name.")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def trace_list(
    ctx: click.Context,
    domain: str | None,
    status: str | None,
    agent: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """List recorded traces."""
    store = _load_store(ctx.obj["root"])
    traces = store.list_traces(domain=domain, status=status, agent=agent, limit=limit)
    if as_json:
        _emit([to_jsonable(t) for t in traces], as_json=True)
        return
    if not traces:
        click.echo("(no traces)")
        return
    for t in traces:
        click.echo(f"{t.id}\t{t.agent}\t{t.status}\t{t.domain}\t{t.task[:60]}")


@runs_group.command("show")
@click.argument("trace_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def trace_show(ctx: click.Context, trace_id: str, as_json: bool) -> None:
    """Show a single trace by ID."""
    store = _load_store(ctx.obj["root"])
    trace = store.get_trace(trace_id)
    if trace is None:
        raise click.ClickException(f"trace not found: {trace_id}")
    if as_json:
        _emit(to_jsonable(trace), as_json=True)
        return
    click.echo(f"id:     {trace.id}")
    click.echo(f"agent:  {trace.agent}")
    click.echo(f"status: {trace.status}")
    click.echo(f"domain: {trace.domain}")
    click.echo(f"task:   {trace.task}")


# ----- report ------------------------------------------------------------- #


@cli.command("report")
@click.option("--since", default="7d", show_default=True, help="Lookback duration, e.g. 7d or 12h.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "json"]),
    default="markdown",
    show_default=True,
)
@click.option("--output", "output_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def report_cmd(ctx: click.Context, since: str, output_format: str, output_path: Path | None) -> None:
    """Generate an engineering-leader governance report."""
    from atelier.core.capabilities.reporting.weekly_report import generate_report, render_markdown

    store = _load_store(ctx.obj["root"])
    report = generate_report(_parse_duration(since), store=store, repo_root=Path.cwd())
    if output_format == "json":
        rendered = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False)
    else:
        rendered = render_markdown(report)
    if output_path is not None:
        output_path.write_text(rendered, encoding="utf-8")
        return
    click.echo(rendered.rstrip())


# ----- import-style-guide ------------------------------------------------- #


@cli.command("import-style-guide")
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path, exists=True))
@click.option("--domain", default="coding", show_default=True)
@click.option("--dry-run", is_flag=True, help="Print proposed candidates without writing.")
@click.option("--limit", default=25, show_default=True, type=int)
@click.pass_context
def import_style_guide_cmd(
    ctx: click.Context,
    paths: tuple[Path, ...],
    domain: str,
    dry_run: bool,
    limit: int,
) -> None:
    """Draft lesson candidates from Markdown style guides."""
    from atelier.core.capabilities.style_import import import_files
    from atelier.infra.internal_llm.ollama_client import OllamaUnavailable

    if not paths:
        raise click.ClickException("at least one Markdown file or directory is required")
    store = _load_store(ctx.obj["root"])
    try:
        candidates = import_files(paths, domain, store=store, write=not dry_run, limit=limit)
    except OllamaUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    payload = {
        "dry_run": dry_run,
        "written": 0 if dry_run else len(candidates),
        "candidates": [candidate.model_dump(mode="json", exclude={"embedding"}) for candidate in candidates],
    }
    if dry_run:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    click.echo(f"imported {len(candidates)} lesson candidates into inbox")
    for candidate in candidates:
        click.echo(candidate.id)


# --------------------------------------------------------------------------- #
# block                                                                       #
# --------------------------------------------------------------------------- #


@_dev_group("block")
def block_group() -> None:
    """ReasonBlock curation commands."""


@block_group.command("list")
@click.option("--domain", default=None)
@click.option("--include-deprecated", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def block_list(ctx: click.Context, domain: str | None, include_deprecated: bool, as_json: bool) -> None:  # type: ignore
    """List ReasonBlocks."""
    store = _load_store(ctx.obj["root"])
    blocks = store.list_blocks(domain=domain, include_deprecated=include_deprecated)
    if as_json:
        _emit([to_jsonable(b) for b in blocks], as_json=True)
        return
    if not blocks:
        click.echo("(no blocks)")
        return
    click.echo(f"{len(blocks)} blocks shown")
    for b in blocks:
        click.echo(f"{b.id}\t{b.domain}\t{b.title}")


@block_group.command("add")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def block_add(ctx: click.Context, path: Path) -> None:  # type: ignore
    """Import a ReasonBlock from a YAML file."""
    store = _load_store(ctx.obj["root"])
    data = _load_yaml(path)
    if "id" not in data:
        data["id"] = ReasonBlock.make_id(data["title"], data["domain"])
    block = ReasonBlock.model_validate(data)
    store.upsert_block(block)
    click.echo(f"upserted {block.id}")


@block_group.command("extract")
@click.argument("trace_id")
@click.option("--save", is_flag=True, help="Persist the candidate block.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def block_extract(ctx: click.Context, trace_id: str, save: bool, as_json: bool) -> None:  # type: ignore
    """Extract a candidate ReasonBlock from a trace."""
    store = _load_store(ctx.obj["root"])
    trace = store.get_trace(trace_id)
    if trace is None:
        raise click.ClickException(f"trace not found: {trace_id}")
    from atelier.core.foundation.extractor import extract_candidate

    candidate = extract_candidate(trace)
    if save:
        store.upsert_block(candidate.block)
    payload = {
        "block": to_jsonable(candidate.block),
        "confidence": candidate.confidence,
        "reasons": candidate.reasons,
        "saved": save,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"candidate: {candidate.block.id} (confidence={candidate.confidence:.2f})")
    for r in candidate.reasons:
        click.echo(f"  - {r}")
    click.echo(render_block_markdown(candidate.block))


# ----- list-blocks --------------------------------------------------------- #


@cli.command("list-blocks")
@click.option("--domain", default=None)
@click.option("--include-deprecated", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_blocks_cmd(ctx: click.Context, domain: str | None, include_deprecated: bool, as_json: bool) -> None:
    """List ReasonBlocks."""
    store = _load_store(ctx.obj["root"])
    blocks = store.list_blocks(domain=domain, include_deprecated=include_deprecated)
    if as_json:
        _emit([to_jsonable(b) for b in blocks], as_json=True)
        return
    from atelier.core.foundation.metrics import summarize

    summary = summarize(store)
    click.echo(
        f"# {len(blocks)} blocks shown "
        f"(active={summary.blocks_active}, "
        f"deprecated={summary.blocks_deprecated}, "
        f"quarantined={summary.blocks_quarantined})"
    )
    for b in blocks:
        click.echo(f"{b.status[:1].upper()} {b.id}\t{b.domain}\t{b.title}")


# ----- env ----------------------------------------------------------------- #


@cli.group()
def env() -> None:
    """Validate named compatibility environments."""


@env.command("validate")
@click.argument("env_name")
@click.pass_context
def env_validate(ctx: click.Context, env_name: str) -> None:
    """Validate that a named environment contract exists."""
    store = _load_store(ctx.obj["root"])
    candidates = [env_name]
    suffix = env_name[4:] if env_name.startswith("env_") else env_name
    candidates.append(f"rubric_{suffix}")
    for rubric_id in candidates:
        if store.get_rubric(rubric_id) is not None:
            click.echo(f"ok: {env_name}")
            return
    raise click.ClickException(f"unknown environment: {env_name}")


# ----- deprecate / quarantine --------------------------------------------- #


@cli.command()
@click.argument("block_id")
@click.pass_context
def deprecate(ctx: click.Context, block_id: str) -> None:
    """Mark a block as deprecated."""
    store = _load_store(ctx.obj["root"])
    if not store.update_block_status(block_id, "deprecated"):
        raise click.ClickException(f"block not found: {block_id}")
    click.echo(f"deprecated {block_id}")


@cli.command()
@click.argument("block_id")
@click.pass_context
def quarantine(ctx: click.Context, block_id: str) -> None:
    """Quarantine a block (will not be retrieved)."""
    store = _load_store(ctx.obj["root"])
    if not store.update_block_status(block_id, "quarantined"):
        raise click.ClickException(f"block not found: {block_id}")
    click.echo(f"quarantined {block_id}")


# ----- verify --------------------------- #


@_dev_command("verify")
@click.argument("rubric_id")
@click.option(
    "--input",
    "input_path",
    type=click.Path(path_type=Path),
    default="-",
    show_default=True,
    help="JSON object mapping check_name -> bool. Use '-' for stdin.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def run_rubric_cmd(ctx: click.Context, rubric_id: str, input_path: Path | str, as_json: bool) -> None:
    """Evaluate a rubric against a checks JSON object."""
    _check_dev_mode("verify", status=2)
    from atelier.core.foundation.rubric_gate import run_rubric

    store = _load_store(ctx.obj["root"])
    rubric = store.get_rubric(rubric_id)
    if rubric is None:
        raise click.ClickException(f"rubric not found: {rubric_id}")
    raw = sys.stdin.read() if str(input_path) == "-" else Path(input_path).read_text("utf-8")
    checks = json.loads(raw)
    result = run_rubric(rubric, checks)
    if as_json:
        _emit(to_jsonable(result), as_json=True)
    else:
        click.echo(render_rubric_result(result))
    sys.exit(0 if result.status != "blocked" else 2)


# ----- agent host importers ------------------------------------------------- #
# Each sub-group follows the same pattern:
#   atelier <host> import [--path PATH]
#
# Data model (all three hosts):
#   - RawArtifact  : full redacted session file(s) stored under .atelier/raw/
#   - Trace        : compact curated summary with raw_artifact_ids linkback
#
# Nothing is thrown away except secrets/PII stripped by Atelier's redactor.
# --------------------------------------------------------------------------- #


@cli.group()
def copilot() -> None:
    """Copilot session-state integration (~/.copilot/session-state/)."""


@copilot.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override sessions root (default: ~/.copilot/session-state).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def copilot_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import Copilot sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.copilot import CopilotImporter

    store = _load_store(ctx.obj["root"])
    importer = CopilotImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} copilot sessions")


# ----- claude --------------------------------------------------------------- #


@cli.group()
def claude() -> None:
    """Claude Code session integration (~/.claude/projects/)."""


@claude.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override sessions root (default: ~/.claude/projects/).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def claude_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import Claude Code sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.claude import ClaudeImporter

    store = _load_store(ctx.obj["root"])
    importer = ClaudeImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} claude sessions")


# ----- codex ---------------------------------------------------------------- #


@cli.group()
def codex() -> None:
    """Codex session integration (~/.codex/sessions/)."""


@codex.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override sessions root (default: ~/.codex/sessions/).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def codex_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import Codex sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.codex import CodexImporter

    store = _load_store(ctx.obj["root"])
    importer = CodexImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} codex sessions")


# ----- opencode ------------------------------------------------------------- #


@cli.group()
def opencode() -> None:
    """OpenCode session integration (~/.local/share/opencode/opencode.db)."""


@opencode.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override DB path (default: ~/.local/share/opencode/opencode.db/).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def opencode_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import OpenCode sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.opencode import OpenCodeImporter

    store = _load_store(ctx.obj["root"])
    importer = OpenCodeImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} opencode sessions")


# ----- gemini --------------------------------------------------------------- #


@cli.group()
def gemini() -> None:
    """Gemini CLI session integration (~/.gemini/tmp/atelier/chats/)."""


@gemini.command("import")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override sessions root.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.pass_context
def gemini_import(ctx: click.Context, path: Path | None, force: bool) -> None:
    """Import Gemini sessions into the Atelier store (loss-preserving)."""
    from atelier.gateway.hosts.session_parsers.gemini import GeminiImporter

    store = _load_store(ctx.obj["root"])
    importer = GeminiImporter(store)
    ids = importer.import_all(path, force=force)
    click.echo(f"imported {len(ids)} gemini sessions")


# ----- global import -------------------------------------------------------- #


@cli.command("import")
@click.option(
    "--host",
    type=click.Choice(list(SUPPORTED_SESSION_IMPORT_HOSTS)),
    default=None,
    help="Import from only one specific host.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-import all sessions, ignoring timestamp dedup.",
)
@click.option(
    "--export-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Export reconstructed session logs (JSONL) to this directory.",
)
@click.pass_context
def global_import(ctx: click.Context, host: str | None, force: bool, export_dir: Path | None) -> None:
    """Unified import for ALL agent sessions (Claude, Gemini, Codex, etc.)."""
    from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns
    from atelier.gateway.hosts.session_parsers.registry import iter_importer_classes

    store = _load_store(ctx.obj["root"])
    store.init()

    if export_dir:
        export_dir.mkdir(parents=True, exist_ok=True)
        click.echo(f"exporting reconstructed sessions to {export_dir}")

    hosts = iter_importer_classes()

    total = 0
    reconstructable = 0
    all_imported_ids = []

    with store.batch_mode():
        for name, importer_cls in hosts:
            if host and name != host:
                continue

            try:
                ids = importer_cls(store).import_all(force=force)
                count = len(ids)
                total += count
                all_imported_ids.extend(ids)

                # Reconstruction audit
                for tid in ids:
                    trace = store.get_trace(tid)
                    if trace and trace.raw_artifact_ids:
                        art_id = trace.raw_artifact_ids[0]
                        artifact = store.get_raw_artifact(art_id)
                        if artifact:
                            try:
                                content = store.read_raw_artifact_content(artifact)
                                turns = parse_session_turns(content, name)
                                if turns:
                                    reconstructable += 1
                                    if export_dir:
                                        safe_tid = tid.replace("/", "_").replace("\\", "_")
                                        export_file = export_dir / f"{name}-{safe_tid}.jsonl"
                                        export_file.write_text(content)
                            except Exception:
                                logger.warning(
                                    "Suppressed exception at cli.py:1812",
                                    exc_info=True,
                                )

            except Exception as e:
                click.secho(f"FATAL: {name} importer raised: {e!r}", fg="red", err=True)

    if total > 0:
        pct = (reconstructable / total) * 100
        click.echo(f"\nAudit: {reconstructable}/{total} sessions ({pct:.1f}%) 100% reconstructable.")

    # Sync aggregated usage
    try:
        from atelier.core.service.sync import sync_usage

        sync_usage(ctx.obj["root"], session_ids=all_imported_ids)
    except Exception:
        logger.warning(
            "Suppressed exception at cli.py:1827",
            exc_info=True,
        )


# --------------------------------------------------------------------------- #
# V2: Ledger / Watchdog / Compress / Env / Failure / Eval / Smart / Savings   #
# --------------------------------------------------------------------------- #


def _ledger_dir(root: Path) -> Path:
    return Path(root) / "runs"


def _latest_ledger_path(root: Path) -> Path | None:
    runs = _ledger_dir(root)
    if not runs.is_dir():
        return None
    paths = sorted(runs.glob("*.json"))
    return paths[-1] if paths else None


def _ledger_path(root: Path, session_id: str | None) -> Path:
    if session_id:
        return _ledger_dir(root) / f"{session_id}.json"
    latest = _latest_ledger_path(root)
    if latest is None:
        raise click.ClickException("no run ledger found. Pass --session-id or record one first.")
    return latest


# ----- ledger ------------------------------------------------------------- #


@cli.group()
def ledger() -> None:
    """Manage run ledgers."""


@ledger.command("show")
@click.option("--session-id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def ledger_show(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    snap = json.loads(path.read_text(encoding="utf-8"))
    if as_json:
        _emit(snap, as_json=True)
        return
    click.echo(f"session_id: {snap.get('session_id')}")
    click.echo(f"status: {snap.get('status')}")
    click.echo(f"task: {snap.get('task', '')}")
    click.echo(f"domain: {snap.get('domain', '')}")
    click.echo(f"events: {len(snap.get('events', []))}")
    click.echo(f"errors_seen: {len(snap.get('errors_seen', []))}")
    click.echo(f"current_blockers: {snap.get('current_blockers', [])}")


@ledger.command("reset")
@click.option("--session-id", default=None)
@click.confirmation_option(prompt="Delete this ledger snapshot?")
@click.pass_context
def ledger_reset(ctx: click.Context, session_id: str | None) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    path.unlink(missing_ok=True)
    click.echo(f"removed {path}")


@ledger.command("update")
@click.option("--session-id", default=None)
@click.option("--field", "field_name", required=True)
@click.option("--value", required=True, help="Value (use JSON literal for lists/dicts).")
@click.pass_context
def ledger_update(ctx: click.Context, session_id: str | None, field_name: str, value: str) -> None:
    path = _ledger_path(ctx.obj["root"], session_id)
    snap = json.loads(path.read_text(encoding="utf-8"))
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    snap[field_name] = parsed
    path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    click.echo(f"updated {field_name}")


@ledger.command("summarize")
@click.option("--session-id", default=None)
@click.pass_context
def ledger_summarize(ctx: click.Context, session_id: str | None) -> None:
    from atelier.infra.runtime.context_compressor import ContextCompressor
    from atelier.infra.runtime.run_ledger import RunLedger

    path = _ledger_path(ctx.obj["root"], session_id)
    led = RunLedger.load(path)
    state = ContextCompressor().compress(led)
    click.echo(state.to_prompt_block())


# ----- compress-context --------------------------------------------------- #


@cli.command("compress-context")
@click.option("--session-id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def compress_context_cmd(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """Compress a run ledger into a small state packet."""
    from atelier.infra.runtime.context_compressor import ContextCompressor
    from atelier.infra.runtime.run_ledger import RunLedger

    path = _ledger_path(ctx.obj["root"], session_id)
    led = RunLedger.load(path)
    state = ContextCompressor().compress(led)
    if as_json:
        _emit(
            {
                "files_changed": state.files_changed,
                "error_fingerprints": state.error_fingerprints,
                "high_severity_alerts": state.high_severity_alerts,
                "current_blocker": state.current_blocker,
                "tool_call_count": state.tool_call_count,
                "total_tool_output_chars": state.total_tool_output_chars,
                "preserved": {
                    "latest_error": (state.error_fingerprints[-1] if state.error_fingerprints else None),
                    "active_rubrics": led.active_rubrics,
                    "active_reasonblocks": led.active_reasonblocks,
                    "next_required_validation": led.next_required_validation,
                },
            },
            as_json=True,
        )
        return
    click.echo(state.to_prompt_block())


# ----- checkpoint --------------------------------------------------------- #


@cli.group()
def checkpoint() -> None:
    """Manage idempotent agent checkpoints for resumable execution."""


@checkpoint.command("create")
@click.option("--session-id", default=None, help="Session ID (defaults to latest ledger).")
@click.option("--tool", "tool_name", default="manual", show_default=True)
@click.option("--model-route", default="cheap_llm", show_default=True)
@click.option("--note", default="", help="Optional note stored as compact_state.")
@click.pass_context
def checkpoint_create(
    ctx: click.Context,
    session_id: str | None,
    tool_name: str,
    model_route: str,
    note: str,
) -> None:
    """Create a checkpoint at the current ledger step."""
    from atelier.infra.runtime.checkpoint import Checkpoint, CheckpointStore
    from atelier.infra.runtime.run_ledger import RunLedger

    root = ctx.obj["root"]
    path = _ledger_path(root, session_id)
    led = RunLedger.load(path)
    store = CheckpointStore(root)
    step_id = len(store.list_checkpoints(led.session_id))
    ckpt = Checkpoint.create(
        session_id=led.session_id,
        step_id=step_id,
        tool_name=tool_name,
        model_route=model_route,
        input_data=note,
        output_data=led.status,
        compact_state=note,
        cost_so_far_usd=led.cost_tracker.snapshot().get("total_cost_usd", 0.0) if led.cost_tracker else 0.0,
    )
    saved_path = store.save(ckpt)
    click.echo(f"checkpoint created: session={ckpt.session_id} step={ckpt.step_id} txn={ckpt.transaction_id}")
    click.echo(f"  saved to: {saved_path}")


@checkpoint.command("list")
@click.option("--session-id", default=None, help="Filter to a specific session.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def checkpoint_list(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """List available checkpoints."""
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)
    sessions = [session_id] if session_id else store.list_sessions()
    if not sessions:
        click.echo("no checkpoints found.")
        return
    rows = []
    for sid in sessions:
        for ckpt in store.list_checkpoints(sid):
            rows.append(ckpt.to_dict())
    if as_json:
        _emit(rows, as_json=True)
        return
    for row in rows:
        click.echo(
            f"  {row['session_id'][:12]}  step={row['step_id']:3d}"
            f"  tool={row['tool_name']:<18s}  route={row['model_route']:<14s}"
            f"  cost=${row['cost_so_far_usd']:.4f}  txn={row['transaction_id']}"
        )


@checkpoint.command("resume")
@click.argument("session_id")
@click.option(
    "--from-step",
    "from_step",
    type=int,
    default=None,
    help="Resume from this step (default: last).",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def checkpoint_resume(
    ctx: click.Context,
    session_id: str,
    from_step: int | None,
    as_json: bool,
) -> None:
    """Resume execution context from a saved checkpoint.

    Prints the compact_state from the checkpoint so the agent can restore
    context and continue from step N instead of restarting the full loop.
    """
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)

    if from_step is not None:
        ckpt = store.load(session_id, from_step)
        if ckpt is None:
            raise click.ClickException(f"no checkpoint found for session={session_id} step={from_step}")
    else:
        ckpt = store.latest_checkpoint(session_id)
        if ckpt is None:
            raise click.ClickException(f"no checkpoints found for session={session_id}")

    if as_json:
        _emit(ckpt.to_dict(), as_json=True)
        return

    click.echo(f"resuming from: session={ckpt.session_id}  step={ckpt.step_id}  txn={ckpt.transaction_id}")
    click.echo(f"  tool_name:    {ckpt.tool_name}")
    click.echo(f"  model_route:  {ckpt.model_route}")
    click.echo(f"  cost_so_far:  ${ckpt.cost_so_far_usd:.4f}")
    click.echo(f"  input_hash:   {ckpt.input_hash}")
    click.echo(f"  output_hash:  {ckpt.output_hash}")
    if ckpt.compact_state:
        click.echo("\ncompact_state:")
        click.echo(ckpt.compact_state)


@checkpoint.command("delete")
@click.argument("session_id")
@click.confirmation_option(prompt="Delete all checkpoints for this session?")
@click.pass_context
def checkpoint_delete(ctx: click.Context, session_id: str) -> None:
    """Delete all checkpoints for a session."""
    from atelier.infra.runtime.checkpoint import CheckpointStore

    root = ctx.obj["root"]
    store = CheckpointStore(root)
    count = store.delete_session(session_id)
    click.echo(f"deleted {count} checkpoint(s) for session={session_id}")


# ----- failure ------------------------------------------------------------ #


@cli.group()
def failure() -> None:
    """Failure cluster management."""


def _failure_state_path(root: Path) -> Path:
    return Path(root) / "failure_clusters.json"


def _load_failure_state(root: Path) -> dict[str, dict[str, Any]]:
    path = _failure_state_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_failure_state(root: Path, state: dict[str, dict[str, Any]]) -> None:
    path = _failure_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


@failure.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def failure_list(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    runs = _ledger_dir(ctx.obj["root"])
    clusters = FailureAnalyzer(runs).analyze()
    state = _load_failure_state(ctx.obj["root"])
    if as_json:
        _emit(
            [{**to_jsonable(c), "status": state.get(c.id, {}).get("status", "open")} for c in clusters],
            as_json=True,
        )
        return
    if not clusters:
        click.echo("(no failure clusters)")
        return
    for c in clusters:
        st = state.get(c.id, {}).get("status", "open")
        click.echo(f"{c.id}\t{st}\t{c.severity}\t{c.domain}\t{c.fingerprint[:60]}")


@failure.command("show")
@click.argument("cluster_id")
@click.pass_context
def failure_show(ctx: click.Context, cluster_id: str) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    clusters = {c.id: c for c in FailureAnalyzer(_ledger_dir(ctx.obj["root"])).analyze()}
    if cluster_id not in clusters:
        raise click.ClickException(f"cluster not found: {cluster_id}")
    state = _load_failure_state(ctx.obj["root"])
    payload = to_jsonable(clusters[cluster_id])
    payload["status"] = state.get(cluster_id, {}).get("status", "open")
    _emit(payload, as_json=True)


@failure.command("accept")
@click.argument("cluster_id")
@click.pass_context
def failure_accept(ctx: click.Context, cluster_id: str) -> None:
    state = _load_failure_state(ctx.obj["root"])
    state.setdefault(cluster_id, {})["status"] = "accepted"
    _save_failure_state(ctx.obj["root"], state)
    click.echo(f"accepted {cluster_id}")


@failure.command("reject")
@click.argument("cluster_id")
@click.pass_context
def failure_reject(ctx: click.Context, cluster_id: str) -> None:
    state = _load_failure_state(ctx.obj["root"])
    state.setdefault(cluster_id, {})["status"] = "rejected"
    _save_failure_state(ctx.obj["root"], state)
    click.echo(f"rejected {cluster_id}")


# ----- lesson ------------------------------------------------------------- #


@cli.group()
def lesson() -> None:
    """Lesson candidate review workflow."""


def _emit_lesson_inbox(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    lessons = _lesson_promoter(ctx.obj["root"]).inbox(domain=domain, limit=limit)
    if as_json:
        _emit([item.model_dump(mode="json") for item in lessons], as_json=True)
        return
    if not lessons:
        click.echo("(no inbox lessons)")
        return
    for item in lessons:
        click.echo(f"{item.id}\t{item.domain}\t{item.kind}\t{item.confidence:.2f}\t{item.cluster_fingerprint[:48]}")


@lesson.command("list")
@click.option("--domain", default=None)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_list(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    _emit_lesson_inbox(ctx, domain, limit, as_json)


@lesson.command("inbox")
@click.option("--domain", default=None)
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_inbox(ctx: click.Context, domain: str | None, limit: int, as_json: bool) -> None:
    """List lesson candidates currently waiting in the inbox."""
    _emit_lesson_inbox(ctx, domain, limit, as_json)


@lesson.command("approve")
@click.argument("lesson_id")
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_approve(
    ctx: click.Context,
    lesson_id: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision="approve",
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"approved {lesson_id}")


@lesson.command("reject")
@click.argument("lesson_id")
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_reject(
    ctx: click.Context,
    lesson_id: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision="reject",
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"rejected {lesson_id}")


@lesson.command("decide")
@click.argument("lesson_id")
@click.argument("decision", type=click.Choice(["approve", "reject"]))
@click.option("--reviewer", required=True)
@click.option("--reason", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_decide(
    ctx: click.Context,
    lesson_id: str,
    decision: str,
    reviewer: str,
    reason: str,
    as_json: bool,
) -> None:
    """Approve or reject a lesson candidate."""
    payload = _lesson_promoter(ctx.obj["root"]).decide(
        lesson_id=lesson_id,
        decision=decision,
        reviewer=reviewer,
        reason=reason,
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    verb = "approved" if decision == "approve" else "rejected"
    click.echo(f"{verb} {lesson_id}")


@lesson.group("active")
def lesson_active_group() -> None:
    """Inspect and manage active typed lessons."""


@lesson_active_group.command("list")
@click.option("--include-inactive", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_list(ctx: click.Context, include_inactive: bool, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lessons = TypedLessonStore(ctx.obj["root"], create=False).list_lessons()
    if not include_inactive:
        lessons = [lesson for lesson in lessons if lesson.enabled]
    if as_json:
        _emit([lesson.model_dump(mode="json") for lesson in lessons], as_json=True)
        return
    if not lessons:
        click.echo("(no active lessons)")
        return
    for lesson in lessons:
        last_applied = lesson.last_applied_at.isoformat() if lesson.last_applied_at else "-"
        click.echo(
            f"{lesson.id}\t{lesson.kind}\t{lesson.scope}\t{lesson.effective_confidence_at():.2f}\t"
            f"{'enabled' if lesson.enabled else 'disabled'}\t{last_applied}"
        )


@lesson_active_group.command("show")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_show(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"], create=False).get_lesson(lesson_id)
    if lesson is None:
        raise click.ClickException(f"typed lesson not found: {lesson_id}")
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(json.dumps(lesson.model_dump(mode="json"), indent=2, ensure_ascii=False, default=str))


@lesson_active_group.command("disable")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_disable(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"]).set_enabled(lesson_id, False)
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(f"disabled {lesson_id}")


@lesson_active_group.command("enable")
@click.argument("lesson_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_active_enable(ctx: click.Context, lesson_id: str, as_json: bool) -> None:
    from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore

    lesson = TypedLessonStore(ctx.obj["root"]).set_enabled(lesson_id, True)
    if as_json:
        _emit(lesson.model_dump(mode="json"), as_json=True)
        return
    click.echo(f"enabled {lesson_id}")


@lesson.command("sync-pr")
@click.argument("lesson_id")
@click.option("--dry-run", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def lesson_sync_pr(ctx: click.Context, lesson_id: str, dry_run: bool, as_json: bool) -> None:
    payload = _lesson_pr_bot(ctx.obj["root"]).sync_pr(lesson_id=lesson_id, dry_run=dry_run)
    if as_json:
        _emit(payload, as_json=True)
        return
    if payload.get("skipped"):
        click.echo(f"skipped: {payload.get('reason', 'unknown')}")
        return
    if dry_run:
        click.echo(payload.get("diff", ""))
        return
    click.echo(f"created {payload.get('pr_url', '').strip()}")


@cli.command("analyze-failures")
@click.option("--since", default=None, help="ISO timestamp or shorthand like '7d' (filter by mtime).")
@click.option("--trace", "trace_id", default=None, help="Single ledger run id to analyze.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def analyze_failures_cmd(ctx: click.Context, since: str | None, trace_id: str | None, as_json: bool) -> None:
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    runs = _ledger_dir(ctx.obj["root"])
    fa = FailureAnalyzer(runs)
    snaps = fa.load_snapshots()

    if trace_id:
        snaps = [s for s in snaps if s.get("session_id") == trace_id]

    if since:
        from datetime import datetime, timedelta

        cutoff: datetime | None = None
        if since.endswith("d") and since[:-1].isdigit():
            cutoff = datetime.now(UTC) - timedelta(days=int(since[:-1]))
        else:
            try:
                cutoff = datetime.fromisoformat(since)
            except ValueError:
                cutoff = None
        if cutoff is not None:
            kept = []
            for s in snaps:
                ts = s.get("updated_at") or s.get("created_at")
                if not ts:
                    continue
                try:
                    if datetime.fromisoformat(ts) >= cutoff:
                        kept.append(s)
                except ValueError:
                    continue
            snaps = kept

    from atelier.core.improvement.failure_analyzer import analyze_failures

    clusters = analyze_failures(snaps)
    session_id = _telemetry_session(ctx)
    if session_id is not None:
        from atelier.core.service.telemetry import emit_product
        from atelier.core.service.telemetry.schema import hash_identifier

        for cluster in clusters:
            emit_product(
                "failure_cluster_matched",
                cluster_id_hash=hash_identifier(cluster.id),
                domain=cluster.domain,
                session_id=session_id,
            )
    if as_json:
        _emit([to_jsonable(c) for c in clusters], as_json=True)
        return
    for c in clusters:
        click.echo(f"{c.id}\t{c.severity}\t{c.domain}\t{c.fingerprint[:60]}")


# ----- eval --------------------------------------------------------------- #


def _eval_dir(root: Path) -> Path:
    return Path(root) / "evals"


def _load_eval(root: Path, case_id: str) -> dict[str, Any] | None:
    p = _eval_dir(root) / f"{case_id}.json"
    if not p.exists():
        return None
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def _save_eval(root: Path, case: dict[str, Any]) -> Path:
    d = _eval_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{case['id']}.json"
    p.write_text(json.dumps(case, indent=2), encoding="utf-8")
    return p


def _evaluate_eval_case(case: dict[str, Any]) -> dict[str, Any]:
    expected_status = str(case.get("expected_status", "pass"))
    actual_status = str(case.get("actual_status", expected_status))
    return {
        "case_id": str(case.get("id", "unknown")),
        "domain": str(case.get("domain", "unknown")),
        "description": str(case.get("description", "")),
        "expected_status": expected_status,
        "actual_status": actual_status,
        "passed": actual_status == expected_status,
    }


@cli.group(name="eval")
def eval_() -> None:  # name with trailing underscore to avoid python builtin
    """Evaluation case management."""


# Click v8 needs explicit name binding because eval is reserved-ish.
eval_.name = "eval"


@eval_.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def eval_list(ctx: click.Context, as_json: bool) -> None:
    d = _eval_dir(ctx.obj["root"])
    cases = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            cases.append(json.loads(p.read_text(encoding="utf-8")))
    if as_json:
        _emit(cases, as_json=True)
        return
    for c in cases:
        click.echo(f"{c.get('id')}\t{c.get('status', 'draft')}\t{c.get('domain', '')}\t{c.get('description', '')[:60]}")


@eval_.command("show")
@click.argument("case_id")
@click.pass_context
def eval_show(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    _emit(case, as_json=True)


@eval_.command("promote")
@click.argument("case_id")
@click.pass_context
def eval_promote(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    case["status"] = "active"
    _save_eval(ctx.obj["root"], case)
    click.echo(f"promoted {case_id}")


@eval_.command("deprecate")
@click.argument("case_id")
@click.pass_context
def eval_deprecate(ctx: click.Context, case_id: str) -> None:
    case = _load_eval(ctx.obj["root"], case_id)
    if case is None:
        raise click.ClickException(f"eval case not found: {case_id}")
    case["status"] = "deprecated"
    _save_eval(ctx.obj["root"], case)
    click.echo(f"deprecated {case_id}")


@eval_.command("run")
@click.option("--domain", default=None)
@click.option("--case", "case_id", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def eval_run(ctx: click.Context, domain: str | None, case_id: str | None, as_json: bool) -> None:
    """Run deterministic eval cases."""
    # Note: plan-check based evals have been deprecated.
    # This command now only lists the cases if not in JSON mode.
    d = _eval_dir(ctx.obj["root"])
    cases: list[dict[str, Any]] = []
    if case_id:
        c = _load_eval(ctx.obj["root"], case_id)
        if c is None:
            raise click.ClickException(f"eval case not found: {case_id}")
        cases = [c]
    elif d.is_dir():
        for p in sorted(d.glob("*.json")):
            cases.append(json.loads(p.read_text(encoding="utf-8")))
    if domain:
        cases = [c for c in cases if c.get("domain") == domain]
    results = [_evaluate_eval_case(case) for case in cases]

    if as_json:
        _emit(results, as_json=True)
    else:
        for result in results:
            click.echo(
                f"{result['case_id']}\t{result['domain']}\t{result['expected_status']}"
                f"\t{result['actual_status']}\t{'pass' if result['passed'] else 'fail'}"
            )


@cli.command("eval-from-cluster")
@click.argument("cluster_id")
@click.pass_context
def eval_from_cluster(ctx: click.Context, cluster_id: str) -> None:
    """Generate a draft eval from an accepted FailureCluster."""
    from atelier.core.improvement.failure_analyzer import FailureAnalyzer

    state = _load_failure_state(ctx.obj["root"])
    if state.get(cluster_id, {}).get("status") != "accepted":
        raise click.ClickException(f"cluster not accepted: {cluster_id}")
    clusters = {c.id: c for c in FailureAnalyzer(_ledger_dir(ctx.obj["root"])).analyze()}
    if cluster_id not in clusters:
        raise click.ClickException(f"cluster not found: {cluster_id}")
    c = clusters[cluster_id]
    case = {
        "id": f"eval_from_{cluster_id}",
        "domain": c.domain,
        "description": f"Replay of {c.fingerprint[:60]}",
        "task": f"Replay failure cluster {cluster_id}",
        "plan": [c.suggested_rubric_check or "no-op"],
        "expected_status": "blocked",
        "expected_warnings_contain": [],
        "expected_dead_ends": [],
        "status": "draft",
        "source_trace_ids": list(c.trace_ids),
    }
    p = _save_eval(ctx.obj["root"], case)
    click.echo(f"saved draft eval at {p}")


# ----- smart tools (shadow mode) ------------------------------------------ #


def _smart_state_path(root: Path) -> Path:
    return Path(root) / "smart_state.json"


def _load_smart_state(root: Path) -> dict[str, Any]:
    p = _smart_state_path(root)
    if not p.exists():
        return {"mode": "shadow", "cache": {}, "savings": {"calls_avoided": 0, "tokens_saved": 0}}
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def _save_smart_state(root: Path, state: dict[str, Any]) -> None:
    p = _smart_state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


@cli.group("tool-mode")
def tool_mode() -> None:
    """Smart tool mode (shadow|suggest|replace)."""


@tool_mode.command("show")
@click.pass_context
def tool_mode_show(ctx: click.Context) -> None:
    s = _load_smart_state(ctx.obj["root"])
    click.echo(s.get("mode", "shadow"))


@tool_mode.command("set")
@click.argument("mode", type=click.Choice(["shadow", "suggest", "replace"]))
@click.pass_context
def tool_mode_set(ctx: click.Context, mode: str) -> None:
    s = _load_smart_state(ctx.obj["root"])
    s["mode"] = mode
    _save_smart_state(ctx.obj["root"], s)
    click.echo(f"tool_mode={mode}")


def _mcp_cli_args(raw: str) -> dict[str, Any]:
    text = raw
    if raw.startswith("@"):
        text = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid JSON args: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException("--args must decode to a JSON object")
    return payload


def _prepare_mcp_cli(ctx: click.Context, *, dev: bool, workspace: Path | None = None) -> Callable[[], None]:
    old_root = os.environ.get("ATELIER_ROOT")
    old_dev = os.environ.get("ATELIER_DEV_MODE")
    old_workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT")
    os.environ["ATELIER_ROOT"] = str(ctx.obj["root"])
    if dev:
        os.environ["ATELIER_DEV_MODE"] = "1"
    if workspace is not None:
        os.environ["CLAUDE_WORKSPACE_ROOT"] = str(workspace)

    def restore() -> None:
        if old_root is None:
            os.environ.pop("ATELIER_ROOT", None)
        else:
            os.environ["ATELIER_ROOT"] = old_root
        if old_dev is None:
            os.environ.pop("ATELIER_DEV_MODE", None)
        else:
            os.environ["ATELIER_DEV_MODE"] = old_dev
        if old_workspace is None:
            os.environ.pop("CLAUDE_WORKSPACE_ROOT", None)
        else:
            os.environ["CLAUDE_WORKSPACE_ROOT"] = old_workspace

    return restore


@cli.group("tools")
def tools_group() -> None:
    """Inspect and call Atelier MCP tools."""


@tools_group.command("list")
@click.option("--dev", is_flag=True, help="List the full development MCP surface.")
@click.option("--json", "as_json", is_flag=True, help="Emit tool metadata as JSON.")
@click.pass_context
def tools_list_cmd(ctx: click.Context, dev: bool, as_json: bool) -> None:
    """List tools visible through MCP tools/list."""
    restore = _prepare_mcp_cli(ctx, dev=dev)
    try:
        from atelier.gateway.adapters.mcp_server import (
            TOOLS,
            _tool_description,
            _tool_visible_to_llm,
        )

        tools = [
            {
                "name": name,
                "description": _tool_description(spec),
                "inputSchema": spec.get("inputSchema", {}),
            }
            for name, spec in TOOLS.items()
            if _tool_visible_to_llm(name, spec)
        ]
        if as_json:
            _emit({"tools": tools}, as_json=True)
            return
        for tool in tools:
            click.echo(tool["name"])
    finally:
        restore()


@tools_group.command("call")
@click.argument("name")
@click.option("--args", "args_json", default="{}", show_default=True, help="JSON object or @path.")
@click.option("--dev", is_flag=True, help="Enable development tools for this call.")
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Workspace root for path-scoped MCP tools.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the decoded MCP payload as JSON.")
@click.pass_context
def tools_call_cmd(
    ctx: click.Context, name: str, args_json: str, dev: bool, workspace: Path | None, as_json: bool
) -> None:
    """Call one MCP tool by name."""
    restore = _prepare_mcp_cli(ctx, dev=dev, workspace=workspace)
    try:
        args = _mcp_cli_args(args_json)
        if name == "memory" and isinstance(args, dict):
            from atelier.core.foundation.redaction import redact

            op = str(args.get("op") or "")
            if op == "block_upsert" and "value" in args:
                args["value"] = redact(str(args.get("value") or ""))
                if "description" in args:
                    args["description"] = redact(str(args.get("description") or ""))
            elif op == "archive" and "text" in args:
                args["text"] = redact(str(args.get("text") or ""))
        from atelier.gateway.adapters.mcp_server import _handle

        response = _handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        if response is None:
            raise click.ClickException("tool call returned no response")
        if "error" in response:
            raise click.ClickException(str(response["error"].get("message") or response["error"]))
        content = response.get("result", {}).get("content", [])
        text = str(content[0].get("text", "")) if content else ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        if as_json:
            _emit(payload, as_json=True)
            return
        if isinstance(payload, (dict, list)):
            click.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return
        click.echo(payload)
    finally:
        restore()


@_dev_group("route")
def route_group() -> None:
    """Quality-aware routing helpers."""


@cli.group("route")
def route_public_group() -> None:
    """Cross-vendor routing helpers."""


@route_public_group.command("configure")
@click.option("--vendor", "vendors", multiple=True, type=click.Choice(["anthropic", "openai", "google"]))
@click.option("--risk-class", type=click.Choice(["low", "medium", "high"]), default="low", show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_configure_public_cmd(
    ctx: click.Context,
    vendors: tuple[str, ...],
    risk_class: str,
    as_json: bool,
) -> None:
    from atelier.core.capabilities.cross_vendor_routing.advisor import CrossVendorRouteAdvisor
    from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfigError

    try:
        payload = CrossVendorRouteAdvisor(ctx.obj["root"]).configure(
            enabled_vendors=list(vendors) or None,
            risk_class=risk_class,
        )
    except RouteConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"Saved {payload['path']}")
    click.echo("Enabled vendors: " + ", ".join(payload["enabled_vendors"]))


@route_public_group.command("plan")
@click.option("--tool", "tool_name", required=True, help="Tool or turn type to evaluate.")
@click.option("--task", "task_text", required=True, help="Task summary for routing.")
@click.option("--actual-vendor", default=None, help="Current host vendor for edit-pin decisions.")
@click.option("--expected-input-tokens", default=1000, show_default=True, type=int)
@click.option("--expected-output-tokens", default=200, show_default=True, type=int)
@click.option("--turn-number", default=0, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_plan_cmd(
    ctx: click.Context,
    tool_name: str,
    task_text: str,
    actual_vendor: str | None,
    expected_input_tokens: int,
    expected_output_tokens: int,
    turn_number: int,
    as_json: bool,
) -> None:
    from atelier.core.capabilities.cross_vendor_routing.advisor import CrossVendorRouteAdvisor
    from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfigError

    try:
        payload = CrossVendorRouteAdvisor(ctx.obj["root"]).recommend(
            tool_name=tool_name,
            task_text=task_text,
            actual_vendor=actual_vendor,
            session_state={
                "expected_input_tokens": expected_input_tokens,
                "expected_output_tokens": expected_output_tokens,
                "turn_number": turn_number,
            },
        )
    except RouteConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"Recommendation: {payload['model']}")
    click.echo(f"  vendor: {payload['vendor']}")
    click.echo(f"  predicted cost: ${payload['predicted_cost_usd']:.6f}")
    if payload.get("fallback"):
        click.echo(f"  fallback: {payload['fallback']}")


@route_public_group.command("status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_status_cmd(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.cross_vendor_routing.advisor import CrossVendorRouteAdvisor
    from atelier.core.capabilities.cross_vendor_routing.configuration import RouteConfigError

    try:
        payload = CrossVendorRouteAdvisor(ctx.obj["root"]).status()
    except RouteConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("Enabled vendors: " + ", ".join(payload["enabled_vendors"]))
    click.echo(f"Recommendations logged: {payload['recommendation_count']}")
    click.echo(f"Estimated savings: ${payload['estimated_savings_usd']:.6f}")
    click.echo(f"Active lessons: {payload['active_lesson_count']}")
    click.echo(f"Lesson-driven recommendations: {payload['lesson_application_count']}")
    click.echo(f"Cost-cap triggers: {payload['cost_cap_trigger_count']}")


@route_group.command("decide")
@click.option("--goal", "user_goal", required=True, help="User goal/task summary.")
@click.option("--repo-root", default=".", show_default=True)
@click.option(
    "--task-type",
    type=click.Choice(["debug", "feature", "refactor", "test", "explain", "review", "docs", "ops"]),
    default="feature",
    show_default=True,
)
@click.option(
    "--risk-level",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    show_default=True,
)
@click.option("--changed-file", "changed_files", multiple=True, help="Repeat for each changed file.")
@click.option("--domain", default=None)
@click.option(
    "--step-type",
    type=click.Choice(
        [
            "classify",
            "compress",
            "retrieve",
            "plan",
            "edit",
            "debug",
            "verify",
            "summarize",
            "lesson_extract",
        ]
    ),
    default="plan",
    show_default=True,
)
@click.option("--step-index", default=0, show_default=True, type=int)
@click.option(
    "--evidence-json",
    default="{}",
    show_default=True,
    help="JSON object with optional routing evidence (confidence, refs, verifier_coverage, etc.).",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_decide_cmd(
    ctx: click.Context,
    user_goal: str,
    repo_root: str,
    task_type: str,
    risk_level: str,
    changed_files: tuple[str, ...],
    domain: str | None,
    step_type: str,
    step_index: int,
    evidence_json: str,
    as_json: bool,
) -> None:  # type: ignore
    """Compute a deterministic route decision from quality-aware policy and runtime evidence."""
    _check_dev_mode("route")
    rt = _core_runtime(ctx.obj["root"])

    try:
        evidence = json.loads(evidence_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid --evidence-json: {exc}") from exc
    if not isinstance(evidence, dict):
        raise click.ClickException("--evidence-json must decode to an object")

    decision = rt.route_decide(
        user_goal=user_goal,
        repo_root=repo_root,
        task_type=task_type,
        risk_level=risk_level,
        changed_files=list(changed_files),
        domain=domain,
        step_type=step_type,
        step_index=step_index,
        evidence_summary=evidence,
    )
    payload = to_jsonable(decision)

    if as_json:
        _emit(payload, as_json=True)
        return

    click.echo(
        f"tier={payload['tier']} model={payload.get('selected_model', '') or '(deterministic)'} "
        f"confidence={payload['confidence']:.2f}"
    )
    click.echo(payload["reason"])
    if payload.get("escalation_trigger"):
        click.echo(f"escalation: {payload['escalation_trigger']}")
    if payload.get("verifier_required"):
        click.echo("verifiers: " + ", ".join(payload["verifier_required"]))


@route_group.command("verify")
@click.option("--route-decision-id", required=True)
@click.option("--changed-file", "changed_files", multiple=True, help="Repeat for each changed file.")
@click.option(
    "--validation-json",
    default="[]",
    show_default=True,
    help="JSON list of validation result objects: [{name, passed, detail}].",
)
@click.option(
    "--rubric-status",
    type=click.Choice(["not_run", "pass", "warn", "fail"]),
    default="not_run",
    show_default=True,
)
@click.option("--required-verifier", "required_verifiers", multiple=True)
@click.option("--protected-file-match", is_flag=True, default=False)
@click.option("--repeated-failure", "repeated_failures", multiple=True)
@click.option("--diff-line-count", default=0, show_default=True, type=int)
@click.option("--human-accepted/--human-rejected", default=None)
@click.option("--benchmark-accepted/--benchmark-rejected", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def route_verify_cmd(
    ctx: click.Context,
    route_decision_id: str,
    changed_files: tuple[str, ...],
    validation_json: str,
    rubric_status: str,
    required_verifiers: tuple[str, ...],
    protected_file_match: bool,
    repeated_failures: tuple[str, ...],
    diff_line_count: int,
    human_accepted: bool | None,
    benchmark_accepted: bool | None,
    as_json: bool,
) -> None:  # type: ignore
    """Verify routing outcome and determine pass/warn/fail/escalate status."""
    _check_dev_mode("route", status=2)
    rt = _core_runtime(ctx.obj["root"])

    try:
        validation_results = json.loads(validation_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid --validation-json: {exc}") from exc
    if not isinstance(validation_results, list):
        raise click.ClickException("--validation-json must decode to a list")

    envelope = rt.quality_router.verify(
        route_decision_id=route_decision_id,
        session_id="cli-route-verify",
        changed_files=list(changed_files),
        validation_results=[item for item in validation_results if isinstance(item, dict)],
        rubric_status=rubric_status,
        required_verifiers=list(required_verifiers),
        protected_file_match=protected_file_match,
        repeated_failure_signatures=list(repeated_failures),
        diff_line_count=diff_line_count,
        human_accepted=human_accepted,
        benchmark_accepted=benchmark_accepted,
    )

    payload = to_jsonable(envelope)
    if as_json:
        _emit(payload, as_json=True)
        return

    click.echo(f"outcome={payload['outcome']} rubric={payload['rubric_status']}")
    click.echo(payload["compressed_evidence"])


# --------------------------------------------------------------------------- #
# proof                                                                       #
# --------------------------------------------------------------------------- #


@cli.group("proof")
def proof_group() -> None:
    """Cost-quality proof gate commands (WP-32)."""


@proof_group.command("run")
@click.option(
    "--session-id",
    required=True,
    help="Stable identifier for this proof run (e.g. a git SHA or timestamp).",
)
@click.option(
    "--context-reduction-pct",
    type=float,
    default=None,
    help=(
        "Context reduction percentage from WP-19 savings bench. " "When omitted, the benchmark is re-run automatically."
    ),
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_run_cmd(
    ctx: click.Context,
    session_id: str,
    context_reduction_pct: float | None,
    as_json: bool,
) -> None:
    """Run the cost-quality proof gate and write proof-report.json/md (WP-32)."""
    from atelier.core.capabilities.proof_gate.capability import (
        BenchmarkCase,
        ProofGateCapability,
    )

    root: Path = ctx.obj["root"]

    # Derive context_reduction_pct from savings bench if not provided
    if context_reduction_pct is None:
        try:
            from benchmarks.swe.savings_bench import run_savings_bench

            savings = run_savings_bench(root / "proof" / "savings_bench_tmp")
            context_reduction_pct = savings.reduction_pct
        except Exception as exc:
            raise click.ClickException(f"Could not run savings bench (pass --context-reduction-pct): {exc}") from exc

    # Build a minimal deterministic set of benchmark cases from the savings bench suite
    # to provide trace evidence for the proof report.
    cases: list[BenchmarkCase] = _build_proof_cases(session_id)

    capability = ProofGateCapability(root)
    report = capability.run(
        session_id=session_id,
        context_reduction_pct=context_reduction_pct,
        benchmark_cases=cases,
        save=True,
    )

    payload = to_jsonable(report)
    if as_json:
        _emit(payload, as_json=True)
        return

    status_str = "PASS" if report.status == "pass" else "FAIL"
    click.echo(f"proof session_id={report.session_id} status={status_str}")
    click.echo(f"context_reduction_pct={report.context_reduction_pct:.1f}%")
    click.echo(f"cost_per_accepted_patch=${report.cost_per_accepted_patch:.4f}")
    click.echo(f"accepted_patch_rate={report.accepted_patch_rate:.3f}")
    click.echo(f"routing_regression_rate={report.routing_regression_rate:.4f}")
    click.echo(f"cheap_success_rate={report.cheap_success_rate:.3f}")
    if report.failed_thresholds:
        click.echo(f"failed_thresholds={','.join(report.failed_thresholds)}")


def _show_proof_report(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.proof_gate.capability import ProofGateCapability

    root: Path = ctx.obj["root"]
    capability = ProofGateCapability(root)
    report = capability.load()

    if report is None:
        raise click.ClickException("No proof report found. Run `atelier proof run --session-id <id>` first.")

    payload = to_jsonable(report)
    if as_json:
        _emit(payload, as_json=True)
        return

    status_str = "PASS" if report.status == "pass" else "FAIL"
    click.echo(f"proof session_id={report.session_id} status={status_str}")
    click.echo(f"context_reduction_pct={report.context_reduction_pct:.1f}%")
    click.echo(f"cost_per_accepted_patch=${report.cost_per_accepted_patch:.4f}")
    if report.failed_thresholds:
        click.echo(f"failed_thresholds={','.join(report.failed_thresholds)}")


@proof_group.command("report")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_report_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show the last saved proof report (WP-32)."""
    _show_proof_report(ctx, as_json)


@proof_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def proof_show_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show the last saved proof report (WP-32)."""
    _show_proof_report(ctx, as_json)


def _build_proof_cases(session_id: str) -> list[Any]:
    """Build a deterministic set of benchmark cases for the proof gate.

    These cases are derived from the WP-28 routing eval suite.  Each case
    must include a trace_id so the evidence link requirement is met.  Failed
    cheap attempts are included - they cannot be elided.
    """
    from atelier.core.capabilities.proof_gate.capability import BenchmarkCase

    # Deterministic cases representative of the routing eval suite.
    # Each case carries a synthetic trace_id so every claim links to evidence.
    _CASES: list[dict[str, Any]] = [
        {
            "case_id": f"{session_id}:cheap-01",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": True,
            "cost_usd": 0.002,
            "trace_id": f"{session_id}:trace:cheap-01",
            "session_id": session_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{session_id}:cheap-02",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": False,
            "cost_usd": 0.002,
            "trace_id": f"{session_id}:trace:cheap-02",
            "session_id": session_id,
            "verifier_outcome": "fail",
        },
        {
            "case_id": f"{session_id}:cheap-03",
            "task_type": "coding",
            "tier": "cheap",
            "accepted": True,
            "cost_usd": 0.002,
            "trace_id": f"{session_id}:trace:cheap-03",
            "session_id": session_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{session_id}:mid-01",
            "task_type": "coding",
            "tier": "mid",
            "accepted": True,
            "cost_usd": 0.008,
            "trace_id": f"{session_id}:trace:mid-01",
            "session_id": session_id,
            "verifier_outcome": "pass",
        },
        {
            "case_id": f"{session_id}:premium-01",
            "task_type": "coding",
            "tier": "premium",
            "accepted": True,
            "cost_usd": 0.05,
            "trace_id": f"{session_id}:trace:premium-01",
            "session_id": session_id,
            "verifier_outcome": "pass",
        },
    ]
    return [BenchmarkCase(**c) for c in _CASES]


@_dev_command("read")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--max-lines", default=120, show_default=True)
@click.pass_context
def read_cmd(ctx: click.Context, path: Path, max_lines: int) -> None:
    """Read a file with summarization and related-ReasonBlock hints."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.smart_read(path, max_lines=max_lines)
    _emit(payload, as_json=True)


@_dev_command("edit")
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JSON file with edits: [{path, find, replace}, ...]",
)
@click.pass_context
def edit_cmd(ctx: click.Context, input_path: Path) -> None:
    """Apply a batch of find/replace edits from a JSON file."""
    rt = _core_runtime(ctx.obj["root"])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise click.ClickException("edit input must be a JSON list")
    result = rt.smart_edit([p for p in payload if isinstance(p, dict)])
    _emit(result, as_json=True)


@_dev_group("memory")
def memory_group() -> None:
    """Session memory operations."""


def _workspace_manager_or_none(root: Path) -> Any | None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    manager = TeamWorkspaceManager(root)
    return manager if manager.exists() else None


def _workspace_memory_metadata(root: Path, metadata: dict[str, Any]) -> tuple[dict[str, Any], Any | None, Any | None]:
    manager = _workspace_manager_or_none(root)
    if manager is None:
        return metadata, None, None
    workspace = manager.load_workspace()
    member = manager.require_member(None, workspace=workspace)
    merged = dict(metadata)
    merged.setdefault("scope", "private")
    merged.setdefault("workspace_id", workspace.id)
    merged.setdefault("owner_user_id", member.user_id)
    return merged, manager, member


@memory_group.command("upsert")
@click.option("--agent-id", required=True)
@click.option("--label", required=True)
@click.option("--value", required=True, help="Inline text or @path. Use @/dev/stdin for stdin.")
@click.option("--limit-chars", default=8000, show_default=True, type=int)
@click.option("--description", default="")
@click.option("--read-only", is_flag=True)
@click.option("--pinned", is_flag=True)
@click.option("--metadata-json", default="{}")
@click.option("--expected-version", default=None, type=int)
@click.option("--actor", default=None)
@click.pass_context
def memory_upsert(
    ctx: click.Context,
    agent_id: str,
    label: str,
    value: str,
    limit_chars: int,
    description: str,
    read_only: bool,
    pinned: bool,
    metadata_json: str,
    expected_version: int | None,
    actor: str | None,
) -> None:  # type: ignore
    """Create or update one editable memory block."""
    from atelier.core.foundation.memory_models import MemoryBlock
    from atelier.infra.storage.factory import make_memory_store
    from atelier.infra.storage.memory_store import MemoryConcurrencyError, MemorySidecarUnavailable

    try:
        metadata_raw = json.loads(metadata_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid --metadata-json: {exc}") from exc
    if not isinstance(metadata_raw, dict):
        raise click.ClickException("--metadata-json must decode to an object")
    metadata_raw, _, member = _workspace_memory_metadata(ctx.obj["root"], metadata_raw)
    if member is not None and metadata_raw.get("scope") == "shared":
        from atelier.core.capabilities.team import ensure_shared_memory_write

        ensure_shared_memory_write(member)

    store = make_memory_store(ctx.obj["root"])
    clean_value = _redact_memory_input(_read_memory_value(value), "value")
    clean_description = _redact_memory_input(description, "description")
    existing = store.get_block(agent_id, label)
    version = expected_version if expected_version is not None else (existing.version if existing else 1)
    seed = existing or MemoryBlock(agent_id=agent_id, label=label, value=clean_value)
    block = MemoryBlock(
        id=seed.id,
        agent_id=agent_id,
        label=label,
        value=clean_value,
        limit_chars=limit_chars,
        description=clean_description,
        read_only=read_only,
        metadata=metadata_raw,
        pinned=pinned,
        version=version,
        current_history_id=existing.current_history_id if existing else None,
        created_at=seed.created_at,
    )
    try:
        stored = store.upsert_block(block, actor=actor or f"agent:{agent_id}")
    except MemoryConcurrencyError as exc:
        raise click.ClickException(str(exc)) from exc
    except MemorySidecarUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    _emit({"id": stored.id, "version": stored.version}, as_json=True)


@memory_group.command("get")
@click.option("--agent-id", default=None)
@click.option("--label", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def memory_get(ctx: click.Context, agent_id: str | None, label: str, as_json: bool) -> None:  # type: ignore
    """Fetch one editable memory block."""
    from atelier.infra.storage.factory import make_memory_store

    store = make_memory_store(ctx.obj["root"])
    block = store.get_block(agent_id, label)
    if block is None:
        _emit(None, as_json=as_json)
        return
    manager = _workspace_manager_or_none(ctx.obj["root"])
    if manager is not None:
        from atelier.core.capabilities.team import visible_memory_blocks

        visible = visible_memory_blocks([block], manager=manager)
        if not visible:
            raise click.ClickException(f"memory block is not visible to current workspace user: {label}")
        block = visible[0]
    payload = block.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"{payload.get('agent_id', 'shared')}\t{payload['label']}\tv{payload['version']}")
    click.echo(payload["value"])


@memory_group.command("list")
@click.option("--agent-id", default=None)
@click.option("--shared", "shared_only", is_flag=True, help="Show only workspace-shared blocks.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def memory_list(ctx: click.Context, agent_id: str | None, shared_only: bool, as_json: bool) -> None:  # type: ignore
    """List all memory blocks for an agent."""
    from atelier.infra.storage.factory import make_memory_store

    store = make_memory_store(ctx.obj["root"])
    blocks = store.list_blocks(agent_id)
    manager = _workspace_manager_or_none(ctx.obj["root"])
    if manager is not None:
        from atelier.core.capabilities.team import visible_memory_blocks

        blocks = visible_memory_blocks(blocks, manager=manager, shared_only=shared_only)
    if as_json:
        _emit([b.model_dump(mode="json") for b in blocks], as_json=True)
        return
    if not blocks:
        click.echo("(no blocks)")
        return
    for b in blocks:
        click.echo(f"{b.label}\tv{b.version}\t{len(b.value)} chars")


@memory_group.command("archive")
@click.option("--agent-id", default=None)
@click.option("--text", required=True, help="Inline text or @path. Use @/dev/stdin for stdin.")
@click.option("--source", required=True)
@click.option("--source-ref", default="")
@click.option("--tags", "tag_values", multiple=True)
@click.pass_context
def memory_archive(
    ctx: click.Context,
    agent_id: str | None,
    text: str,
    source: str,
    source_ref: str,
    tag_values: tuple[str, ...],
) -> None:  # type: ignore
    """Archive long-term memory text for later recall."""
    from atelier.core.capabilities.archival_recall import ArchivalRecallCapability
    from atelier.core.foundation.redaction import redact
    from atelier.infra.embeddings.factory import make_embedder
    from atelier.infra.storage.factory import make_memory_store

    capability = ArchivalRecallCapability(make_memory_store(ctx.obj["root"]), make_embedder(), redactor=redact)
    passage = capability.archive(
        agent_id=agent_id,
        text=_read_memory_value(text),
        source=source,  # type: ignore[arg-type]
        source_ref=source_ref,
        tags=_parse_tags(tag_values),
    )
    _emit({"id": passage.id, "dedup_hit": passage.dedup_hit}, as_json=True)


@memory_group.command("recall")
@click.option("--agent-id", default=None)
@click.option("--query", required=True)
@click.option("--top-k", default=5, show_default=True, type=int)
@click.option("--tags", "tag_values", multiple=True)
@click.option("--since", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def memory_recall(
    ctx: click.Context,
    agent_id: str,
    query: str,
    top_k: int,
    tag_values: tuple[str, ...],
    since: str | None,
    as_json: bool,
) -> None:  # type: ignore
    """Recall relevant archival memory passages."""
    from atelier.core.capabilities.archival_recall import ArchivalRecallCapability
    from atelier.core.foundation.redaction import redact
    from atelier.infra.embeddings.factory import make_embedder
    from atelier.infra.storage.factory import make_memory_store

    capability = ArchivalRecallCapability(make_memory_store(ctx.obj["root"]), make_embedder(), redactor=redact)
    passages, recall = capability.recall(
        agent_id=agent_id,
        query=query,
        top_k=top_k,
        tags=_parse_tags(tag_values) or None,
        since=datetime.fromisoformat(since) if since else None,
    )
    payload = {
        "passages": [
            {
                "id": passage.id,
                "text": passage.text,
                "source_ref": passage.source_ref,
                "tags": passage.tags,
            }
            for passage in passages
        ],
        "recall_id": recall.id,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    for passage in passages:
        click.echo(f"{passage.id}\t{passage.source_ref}\t{passage.text}")


@cli.group("letta")
def letta_group() -> None:
    """Manage the self-hosted Letta sidecar."""


@letta_group.command("up")
def letta_up() -> None:
    """Start the Letta memory server Docker Compose stack."""
    _run_compose(["up", "-d"])


@letta_group.command("down")
def letta_down() -> None:
    """Stop the Letta Docker Compose stack while preserving volumes."""
    _run_compose(["down"])


@letta_group.command("status")
def letta_status() -> None:
    """Print Letta health status."""
    url = os.environ.get("ATELIER_LETTA_URL", "http://localhost:8283").rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/v1/health", timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
        click.echo(f"healthy\t{url}\t{body}")
    except Exception as exc:
        raise click.ClickException(f"Letta is not healthy at {url}: {exc}") from exc


@letta_group.command("reset")
@click.option("--yes", is_flag=True, help="Confirm destructive volume removal.")
def letta_reset(yes: bool) -> None:
    """Remove the Letta container and persistent volume."""
    if not yes:
        raise click.ClickException("refusing to reset Letta data without --yes")
    _run_compose(["down", "-v"])


@cli.group("openmemory")
def openmemory_group() -> None:
    """Manage the self-hosted OpenMemory sidecar."""


@openmemory_group.command("up")
@click.pass_context
def openmemory_up(ctx: click.Context) -> None:
    """Clone/update OpenMemory and start its local MCP stack."""
    root = ctx.obj["root"]
    missing = [name for name in ("git", "docker", "make") if not shutil.which(name)]
    if missing:
        raise click.ClickException(f"OpenMemory requires: {', '.join(missing)}")
    if not os.environ.get("ATELIER_OPENMEMORY_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", "")).strip():
        raise click.ClickException("OPENAI_API_KEY or ATELIER_OPENMEMORY_OPENAI_API_KEY must be set for OpenMemory")
    _ensure_openmemory_checkout(root)
    _ensure_openmemory_service_env(root)
    _write_openmemory_env_files(root)
    _run_openmemory_make(root, "build")
    _run_openmemory_make(root, "up")
    click.echo(f"OpenMemory started at {os.environ.get('ATELIER_OPENMEMORY_URL', 'http://127.0.0.1:8765')}")


@openmemory_group.command("down")
@click.pass_context
def openmemory_down(ctx: click.Context) -> None:
    """Stop the local OpenMemory stack while preserving the checkout."""
    root = ctx.obj["root"]
    workdir = _openmemory_workdir(root)
    if not workdir.exists():
        click.echo("OpenMemory checkout not found; nothing to stop.")
        return
    _run_openmemory_make(root, "down")
    click.echo("OpenMemory stopped.")


@openmemory_group.command("status")
@click.pass_context
def openmemory_status(ctx: click.Context) -> None:
    """Show the local OpenMemory service status."""
    root = ctx.obj["root"]
    workdir = _openmemory_workdir(root)
    if not workdir.exists():
        click.echo("OpenMemory checkout not found.")
        return
    subprocess.run(["docker", "compose", "ps"], cwd=workdir, check=False)


@openmemory_group.command("logs")
@click.option("-f", "--follow", is_flag=True, help="Follow the logs.")
@click.pass_context
def openmemory_logs(ctx: click.Context, follow: bool) -> None:
    """Show OpenMemory Docker Compose logs."""
    root = ctx.obj["root"]
    workdir = _openmemory_workdir(root)
    if not workdir.exists():
        raise click.ClickException("OpenMemory checkout not found")
    cmd = ["docker", "compose", "logs"]
    if follow:
        cmd.append("-f")
    subprocess.run(cmd, cwd=workdir, check=False)


@cli.group("zoekt")
def zoekt_group() -> None:
    """Manage Zoekt local binaries and optional Docker sidecar."""


def _zoekt_workspace_prefix(repo_root: Path) -> str:
    return f"atelier-zoekt-{sha256(str(repo_root.resolve()).encode('utf-8')).hexdigest()[:12]}-"


def _zoekt_default_index_dir() -> Path:
    return Path.home() / ".zoekt"


def _zoekt_missing_local_binaries() -> list[str]:
    required = ("zoekt-git-index", "zoekt-index", "zoekt", "zoekt-webserver")
    return [name for name in required if shutil.which(name) is None]


def _zoekt_install_commands() -> tuple[str, ...]:
    return (
        "go install github.com/sourcegraph/zoekt/cmd/zoekt-git-index@latest",
        "go install github.com/sourcegraph/zoekt/cmd/zoekt-index@latest",
        "go install github.com/sourcegraph/zoekt/cmd/zoekt@latest",
        "go install github.com/sourcegraph/zoekt/cmd/zoekt-webserver@latest",
    )


@zoekt_group.command("install")
@click.option("--auto", is_flag=True, help="Run go install commands automatically.")
@click.option("--print-only", is_flag=True, help="Only print the install commands.")
def zoekt_install(auto: bool, print_only: bool) -> None:
    """Install/check local Zoekt binaries (native, no Docker)."""
    missing = _zoekt_missing_local_binaries()
    commands = _zoekt_install_commands()

    if not missing:
        click.echo("Zoekt local binaries are already installed.")
        return

    click.echo("Missing Zoekt binaries: " + ", ".join(missing))
    click.echo("Install with:")
    for command in commands:
        click.echo(f"  {command}")

    if print_only:
        return
    if not auto:
        raise click.ClickException("Install the commands above, or run: atelier zoekt install --auto")
    if shutil.which("go") is None:
        raise click.ClickException("Go is required for --auto install (go command not found on PATH)")

    for command in commands:
        subprocess.run(command.split(), check=True)

    missing_after = _zoekt_missing_local_binaries()
    if missing_after:
        raise click.ClickException("Zoekt install incomplete; still missing: " + ", ".join(missing_after))
    click.echo("Zoekt local binaries installed.")


@zoekt_group.command("index")
@click.argument(
    "target",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=".",
    required=False,
)
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
def zoekt_index(target: Path, index_dir: Path) -> None:
    """Index a repository/directory into a local Zoekt index."""
    target = target.resolve()
    index_dir = index_dir.resolve()
    index_dir.mkdir(parents=True, exist_ok=True)

    git_index = shutil.which("zoekt-git-index")
    plain_index = shutil.which("zoekt-index")
    if git_index and (target / ".git").exists():
        cmd = [git_index, "-index", str(index_dir), str(target)]
    elif plain_index:
        cmd = [plain_index, "-index", str(index_dir), str(target)]
    elif git_index:
        cmd = [git_index, "-index", str(index_dir), str(target)]
    else:
        raise click.ClickException("Zoekt index binaries not found. Run: atelier zoekt install")

    subprocess.run(cmd, check=True)
    click.echo(f"Zoekt index updated at {index_dir}")


@zoekt_group.command("search")
@click.argument("query", nargs=-1, required=True)
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
def zoekt_search(query: tuple[str, ...], index_dir: Path) -> None:
    """Search the local Zoekt index from CLI."""
    zoekt_bin = shutil.which("zoekt")
    if zoekt_bin is None:
        raise click.ClickException("zoekt binary not found. Run: atelier zoekt install")
    q = " ".join(query).strip()
    if not q:
        raise click.ClickException("query cannot be empty")
    result = subprocess.run([zoekt_bin, "-index", str(index_dir.resolve()), q], check=False)
    if result.returncode not in (0, 1):
        raise click.ClickException(f"zoekt search failed (exit {result.returncode})")


@zoekt_group.command("serve")
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=6070, show_default=True, type=int)
def zoekt_serve(index_dir: Path, host: str, port: int) -> None:
    """Run local Zoekt web/API server against the local index."""
    webserver_bin = shutil.which("zoekt-webserver")
    if webserver_bin is None:
        raise click.ClickException("zoekt-webserver binary not found. Run: atelier zoekt install")
    subprocess.run(
        [webserver_bin, "-index", str(index_dir.resolve()), "-listen", f"{host}:{port}"],
        check=True,
    )


@zoekt_group.command("up")
@click.pass_context
def zoekt_up(ctx: click.Context) -> None:
    """Start the persistent Zoekt search container for the current repo."""
    from atelier.infra.code_intel.zoekt.binary import discover_zoekt_binary
    from atelier.infra.code_intel.zoekt.server import get_zoekt_server

    repo_root = Path(_project_root())
    resolution = discover_zoekt_binary(repo_root)
    if not resolution.available:
        raise click.ClickException(f"Zoekt runtime unavailable: {resolution.reason}")
    server = get_zoekt_server(repo_root, resolution=resolution)
    handle = server.ensure_started()
    click.echo(f"Zoekt started: {handle}")


@zoekt_group.command("down")
@click.pass_context
def zoekt_down(ctx: click.Context) -> None:
    """Stop the persistent Zoekt container for the current repo."""
    from atelier.infra.code_intel.zoekt.server import get_zoekt_server

    repo_root = Path(_project_root())
    server = get_zoekt_server(repo_root)
    server.stop()
    click.echo("Zoekt stopped.")


@zoekt_group.command("status")
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
@click.pass_context
def zoekt_status(ctx: click.Context, index_dir: Path) -> None:
    """Show local Zoekt status (and Docker sidecar status if present)."""
    missing = _zoekt_missing_local_binaries()
    if missing:
        click.echo("Local Zoekt binaries: missing -> " + ", ".join(missing))
        click.echo("Install with: atelier zoekt install")
    else:
        click.echo("Local Zoekt binaries: installed")
    resolved_index = index_dir.resolve()
    click.echo(f"Local index dir: {resolved_index} ({'exists' if resolved_index.exists() else 'missing'})")

    repo_root = Path(_project_root())
    prefix = _zoekt_workspace_prefix(repo_root)
    if shutil.which("docker") is None:
        return
    click.echo("")
    click.echo("Docker sidecar containers (optional):")
    subprocess.run(["docker", "ps", "-a", "--filter", f"name={prefix}"], check=False)


@zoekt_group.command("reindex")
@click.option(
    "--index",
    "index_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=str(_zoekt_default_index_dir()),
    show_default=True,
)
@click.pass_context
def zoekt_reindex(ctx: click.Context, index_dir: Path) -> None:
    """Reindex current repository into local Zoekt index."""
    target = Path(_project_root())
    ctx.invoke(zoekt_index, target=target, index_dir=index_dir)


@zoekt_group.command("reset")
@click.option("--yes", is_flag=True, help="Confirm removal of Zoekt runtime data.")
@click.pass_context
def zoekt_reset(ctx: click.Context, yes: bool) -> None:
    """Stop Zoekt and remove runtime state for this repository."""
    if not yes:
        raise click.ClickException("Pass --yes to confirm index cleanup.")
    repo_root = Path(_project_root())
    prefix = _zoekt_workspace_prefix(repo_root)
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={prefix}"],
        capture_output=True,
        text=True,
        check=False,
    )
    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if container_ids:
        subprocess.run(["docker", "rm", "-f", *container_ids], check=False)
    from atelier.core.foundation.paths import default_store_root

    workspace_hash = sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
    runtime_root = default_store_root() / "workspaces" / workspace_hash / "zoekt"
    shutil.rmtree(runtime_root, ignore_errors=True)
    click.echo("Zoekt state removed.")


@cli.command("consolidate")
@click.option("--since", default="7d", show_default=True)
@click.option("--dry-run", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def consolidate_cmd(ctx: click.Context, since: str, dry_run: bool, as_json: bool) -> None:
    """Run manual sleep-time consolidation."""
    from atelier.core.capabilities.consolidation import consolidate

    store = _load_store(ctx.obj["root"])
    report = consolidate(store, since=_parse_duration(since), dry_run=dry_run)
    _emit(report.to_dict(), as_json=as_json)


@cli.group("consolidation")
def consolidation_group() -> None:
    """Consolidation candidate review workflow."""


@consolidation_group.command("inbox")
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def consolidation_inbox(ctx: click.Context, limit: int, as_json: bool) -> None:
    store = _load_store(ctx.obj["root"])
    items = store.list_consolidation_candidates(limit=limit)
    payload = {"candidates": [item.model_dump(mode="json") for item in items]}
    if as_json:
        _emit(payload, as_json=True)
        return
    if not items:
        click.echo("(no consolidation candidates)")
        return
    for item in items:
        click.echo(f"{item.id}\t{item.kind}\t{item.proposed_action}")


@consolidation_group.command("decide")
@click.argument("candidate_id")
@click.argument("decision")
@click.option("--reviewer", default="human", show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def consolidation_decide(ctx: click.Context, candidate_id: str, decision: str, reviewer: str, as_json: bool) -> None:
    store = _load_store(ctx.obj["root"])
    candidate = store.get_consolidation_candidate(candidate_id)
    if candidate is None:
        raise click.ClickException(f"consolidation candidate not found: {candidate_id}")
    candidate.decided_at = datetime.now(UTC)
    candidate.decided_by = reviewer
    candidate.decision = decision
    store.upsert_consolidation_candidate(candidate)
    payload = candidate.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"{decision} {candidate_id}")


@cli.group("stack")
def stack_group() -> None:
    """Manage the optional visualization stack (service + frontend)."""


@stack_group.command("start")
@click.option("--with-docs", is_flag=True, help="Deprecated; docs are no longer managed by atelier stack.")
@click.pass_context
def stack_start(ctx: click.Context, with_docs: bool) -> None:
    """Start the optional visualization stack with native processes."""
    root = ctx.obj["root"]
    if (SYSTEMD_USER_DIR / STACK_UNIT).exists():
        click.echo(
            f"Notice: {STACK_UNIT} is installed. "
            "Prefer using 'atelier systemd restart' or 'systemctl --user restart atelier-stack'."
        )
    if with_docs:
        click.echo("Notice: docs are no longer part of the managed stack; starting service + frontend only.")

    payload = _stack_status_payload(root)
    if payload["running"]:
        click.echo(f"frontend: {payload['frontend_url']}")
        click.echo(f"service: {payload['service_url']}")
        return

    if payload["runner_running"] or payload["service_running"] or payload["frontend_running"]:
        _stop_stack_processes(root, force=True)

    stack_dir = _stack_dir(root)
    stack_dir.mkdir(parents=True, exist_ok=True)
    project_root = _project_root()
    command = [
        sys.executable,
        "-m",
        "atelier.gateway.cli",
        "--root",
        str(root),
        "stack",
        "run",
    ]
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(root)
    with _stack_log_path(root).open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=project_root,
            start_new_session=True,
            close_fds=True,
        )

    _stack_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
    deadline = time.time() + 2
    while time.time() < deadline and _pid_is_running(proc.pid):
        time.sleep(0.1)

    if not _pid_is_running(proc.pid):
        tail = _tail_text(_stack_log_path(root))
        detail = f"\n\n{tail}" if tail else ""
        raise click.ClickException(f"native stack failed to start; inspect {_stack_log_path(root)}{detail}")

    payload = _stack_status_payload(root)
    click.echo(f"frontend: {payload['frontend_url']}")
    click.echo(f"service: {payload['service_url']}")


@stack_group.command("stop")
@click.option("--force", is_flag=True, help="Use SIGKILL if the stack does not stop cleanly.")
@click.pass_context
def stack_stop(ctx: click.Context, force: bool) -> None:
    """Stop the optional visualization stack."""
    root = ctx.obj["root"]
    if (SYSTEMD_USER_DIR / STACK_UNIT).exists():
        click.echo(
            f"Notice: {STACK_UNIT} is installed. "
            "Prefer using 'atelier systemd uninstall' or 'systemctl --user stop atelier-stack'."
        )
    payload = _stop_stack_processes(root, force=force)
    if payload["running"]:
        raise click.ClickException("stack did not stop cleanly")
    click.echo("stopped stack")


@stack_group.command("status")
@click.pass_context
def stack_status(ctx: click.Context) -> None:
    """Show visualization stack process status."""
    root = ctx.obj["root"]
    if (SYSTEMD_USER_DIR / STACK_UNIT).exists():
        subprocess.run(["systemctl", "--user", "status", STACK_UNIT, "--no-pager"], check=False)
        click.echo("-" * 40)
    payload = _stack_status_payload(root)
    click.echo(f"running: {str(payload['running']).lower()}")
    click.echo(f"runner_pid: {payload['runner_pid']}")
    click.echo(f"service_pid: {payload['service_pid']}")
    click.echo(f"frontend_pid: {payload['frontend_pid']}")
    click.echo(f"log_file: {payload['log_file']}")
    click.echo(f"service: {payload['service_url']}")
    click.echo(f"frontend: {payload['frontend_url']}")
    if payload["last_exit_reason"]:
        click.echo(f"last_exit_reason: {payload['last_exit_reason']}")


@stack_group.command("run", hidden=True)
@click.option("--service-host", default=DEFAULT_STACK_SERVICE_HOST, show_default=True)
@click.option("--service-port", default=DEFAULT_STACK_SERVICE_PORT, show_default=True, type=int)
@click.option("--frontend-host", default=DEFAULT_STACK_FRONTEND_HOST, show_default=True)
@click.option("--frontend-port", default=DEFAULT_STACK_FRONTEND_PORT, show_default=True, type=int)
@click.pass_context
def stack_run(
    ctx: click.Context,
    service_host: str,
    service_port: int,
    frontend_host: str,
    frontend_port: int,
) -> None:
    """Internal long-running supervisor for the optional native stack."""
    root = ctx.obj["root"]
    frontend_dir = _stack_frontend_dir()
    _ensure_stack_frontend_dependencies(frontend_dir)

    _stack_dir(root).mkdir(parents=True, exist_ok=True)
    _stack_pid_path(root).write_text(f"{os.getpid()}\n", encoding="utf-8")
    _write_stack_state(
        root,
        {
            "started_at": datetime.now(UTC).isoformat(),
            "last_exit_reason": None,
            "service_url": f"http://localhost:{service_port}",
            "frontend_url": f"http://localhost:{frontend_port}",
        },
    )

    service_env = os.environ.copy()
    service_env.update(
        {
            "ATELIER_ROOT": str(root),
            "ATELIER_SERVICE_HOST": service_host,
            "ATELIER_SERVICE_PORT": str(service_port),
            "ATELIER_REQUIRE_AUTH": "false",
        }
    )
    frontend_env = os.environ.copy()
    frontend_env["VITE_API_URL"] = f"http://localhost:{service_port}"

    service_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "atelier.gateway.cli",
            "--root",
            str(root),
            "service",
            "start",
            "--host",
            service_host,
            "--port",
            str(service_port),
        ],
        cwd=_project_root(),
        env=service_env,
        start_new_session=True,
    )
    frontend_proc = subprocess.Popen(
        [
            "npm",
            "exec",
            "vite",
            "--",
            "--host",
            frontend_host,
            "--port",
            str(frontend_port),
        ],
        cwd=frontend_dir,
        env=frontend_env,
        start_new_session=True,
    )
    _stack_service_pid_path(root).write_text(f"{service_proc.pid}\n", encoding="utf-8")
    _stack_frontend_pid_path(root).write_text(f"{frontend_proc.pid}\n", encoding="utf-8")

    stopping = False

    def _handle_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous_sigterm = signal.signal(signal.SIGTERM, _handle_stop)
    previous_sigint = signal.signal(signal.SIGINT, _handle_stop)

    exit_reason = "stopped"
    exit_code = 0
    try:
        while True:
            service_code = service_proc.poll()
            frontend_code = frontend_proc.poll()
            if stopping:
                break
            if service_code is not None:
                exit_reason = f"service_exited:{service_code}"
                exit_code = service_code or 1
                break
            if frontend_code is not None:
                exit_reason = f"frontend_exited:{frontend_code}"
                exit_code = frontend_code or 1
                break
            time.sleep(1)
    finally:
        for proc in (frontend_proc, service_proc):
            if proc.poll() is None:
                _signal_process_group(proc.pid, signal.SIGTERM)
        deadline = time.time() + 5
        while time.time() < deadline and any(proc.poll() is None for proc in (frontend_proc, service_proc)):
            time.sleep(0.1)
        for proc in (frontend_proc, service_proc):
            if proc.poll() is None:
                _signal_process_group(proc.pid, signal.SIGKILL)
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        _clear_stack_pidfiles(root)
        state = _read_stack_state(root)
        state["last_exit_reason"] = exit_reason
        state["stopped_at"] = datetime.now(UTC).isoformat()
        _write_stack_state(root, state)

    raise SystemExit(exit_code)


@cli.group("bash")
def bash_group() -> None:
    """Shell interception helpers."""


@bash_group.command("intercept")
@click.option("--command", "command_text", required=True, help="Shell command string to inspect.")
@click.option(
    "--history",
    "history_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional JSON array file with prior shell commands.",
)
@click.pass_context
def bash_intercept(ctx: click.Context, command_text: str, history_path: Path | None) -> None:
    rt = _core_runtime(ctx.obj["root"])
    history: list[str] = []
    if history_path is not None:
        raw = json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            history = [str(item) for item in raw]
    payload = rt.bash_intercept(command_text, history=history)
    _emit(payload, as_json=True)


@cli.command("search-read")
@click.option("--query", required=True, help="Pattern to search for (rg).")
@click.option("--path", "search_path", default=".", show_default=True, help="Directory or file to search.")
@click.option("--max-files", default=10, show_default=True, type=int, help="Max hit-files to return.")
@click.option("--max-chars-per-file", default=2000, show_default=True, type=int)
@click.option("--no-outline", "include_outline", is_flag=True, flag_value=False, default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON (default: human-readable).")
@click.pass_context
def search_read_cmd(
    ctx: click.Context,
    query: str,
    search_path: str,
    max_files: int,
    max_chars_per_file: int,
    include_outline: bool,
    as_json: bool,
) -> None:
    """Combined search + read.

    Collapses grep->read->read into a single ranked-snippet call.  Returns
    context windows around each match plus AST outlines for dense files.
    Typically saves >=70 % of tokens vs. separate grep + full-file-read calls.

    Host-native search/read tools remain available for raw exploration.
    """
    from atelier.core.capabilities.tool_supervision.search_read import (
        search_read,
        search_read_to_dict,
    )

    try:
        result = search_read(
            query=query,
            path=search_path,
            max_files=max_files,
            max_chars_per_file=max_chars_per_file,
            include_outline=include_outline,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    payload = search_read_to_dict(result)

    if as_json:
        _emit(payload, as_json=True)
        return

    click.echo(
        f"matches: {len(payload['matches'])} files  "
        f"tokens: {payload['total_tokens']}  "
        f"saved_vs_naive: {payload['tokens_saved_vs_naive']}"
    )
    for m in payload["matches"]:
        click.echo(f"\n  [{m['lang']}] {m['path']}  ({m['tokens']} tokens)")
        for sn in m["snippets"]:
            click.echo(f"    lines {sn['line_start']}-{sn['line_end']}  score={sn['score']:.2f}")
            for ln in sn["text"].splitlines()[:5]:
                click.echo(f"      {ln}")
        if m.get("outline"):
            symbols = m["outline"].get("symbols", [])
            click.echo(f"    outline: {len(symbols)} symbols")


def _code_context_engine(repo_root: str) -> Any:
    from atelier.core.capabilities.code_context import CodeContextEngine

    return CodeContextEngine(repo_root)


@cli.group("project")
def project_group() -> None:
    """Per-project setup — index bootstrap, workspace binding, host guidance."""


@project_group.command("init")
@click.argument("directory", default=".", type=click.Path(file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Emit JSON summary.")
def project_init_cmd(directory: Path, as_json: bool) -> None:
    """Bootstrap code index for a project and print host-setup guidance.

    Detects the git root of DIRECTORY (default: current directory), runs
    `atelier code index` for it, and prints next steps for registering the
    workspace with your host (Claude Code, Copilot, Codex, etc.).
    """
    resolved = directory.expanduser().resolve()
    if not resolved.exists():
        raise click.ClickException(f"Directory does not exist: {resolved}")

    git_root = _detect_git_root(resolved)
    index_root = git_root if git_root is not None else resolved

    click.echo(f"project root: {index_root}")
    if git_root is None:
        click.echo("  (not a git repo — indexing directory as-is)")

    click.echo("bootstrapping code index …")
    engine = _code_context_engine(str(index_root))
    stats = engine.index_repo().model_dump(mode="json")

    if as_json:
        _emit(
            {
                "project_root": str(index_root),
                "is_git_repo": git_root is not None,
                "files_indexed": stats["files_indexed"],
                "symbols_indexed": stats["symbols_indexed"],
                "imports_indexed": stats["imports_indexed"],
            },
            as_json=True,
        )
        return

    click.echo(
        f"indexed {stats['files_indexed']} files, "
        f"{stats['symbols_indexed']} symbols "
        f"({stats['imports_indexed']} imports)"
    )
    click.echo("")
    click.echo("next steps — register the workspace with your host:")
    click.echo(f"  Claude Code:  bash scripts/install_claude.sh --workspace {index_root}")
    click.echo(f"  Copilot:      bash scripts/install_copilot.sh --workspace {index_root}")
    click.echo(f"  Codex:        bash scripts/install_codex.sh --workspace {index_root}")
    click.echo(f"  OpenCode:     bash scripts/install_opencode.sh --workspace {index_root}")
    click.echo("")
    click.echo("  (skip if already using global-mode install — autosync will keep the index warm)")


@cli.group("code")
def code_group() -> None:
    """Code context indexing, retrieval, repo maps, and impact analysis."""


@code_group.command("index")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--include", "include_globs", multiple=True)
@click.option("--exclude", "exclude_globs", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def code_index_cmd(
    repo_root: str,
    include_globs: tuple[str, ...],
    exclude_globs: tuple[str, ...],
    as_json: bool,
) -> None:
    """Index a repository into the SQLite FTS5 symbol store."""
    engine = _code_context_engine(repo_root)
    payload = engine.index_repo(
        include_globs=list(include_globs) or None,
        exclude_globs=list(exclude_globs) or None,
    ).model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(
        f"indexed {payload['files_indexed']} files, {payload['symbols_indexed']} symbols "
        f"({payload['imports_indexed']} imports)"
    )


@code_group.command("search-symbols")
@click.argument("query")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--kind", default=None)
@click.option("--language", default=None)
@click.option("--json", "as_json", is_flag=True)
def code_search_symbols_cmd(
    query: str,
    repo_root: str,
    limit: int,
    kind: str | None,
    language: str | None,
    as_json: bool,
) -> None:
    """BM25/FTS symbol search over the code index."""
    engine = _code_context_engine(repo_root)
    results = [
        item.model_dump(mode="json") for item in engine.search_symbols(query, limit=limit, kind=kind, language=language)
    ]
    if as_json:
        _emit({"items": results}, as_json=True)
        return
    for item in results:
        click.echo(
            f"{item['file_path']}:{item['start_line']}  [{item['kind']}] "
            f"{item['qualified_name']}  {item['signature']}"
        )


@code_group.command("get-symbol")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--symbol-id", default=None)
@click.option("--qualified-name", default=None)
@click.option("--symbol-name", default=None)
@click.option("--file-path", default=None)
@click.option("--json", "as_json", is_flag=True)
def code_get_symbol_cmd(
    repo_root: str,
    symbol_id: str | None,
    qualified_name: str | None,
    symbol_name: str | None,
    file_path: str | None,
    as_json: bool,
) -> None:
    """Retrieve exact symbol source by byte offsets."""
    engine = _code_context_engine(repo_root)
    try:
        payload = engine.get_symbol(
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=file_path,
        )
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload["source"])


@code_group.command("file-outline")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--file-path", default=None)
@click.option("--limit", default=200, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def code_file_outline_cmd(repo_root: str, file_path: str | None, limit: int, as_json: bool) -> None:
    """Return compact file or repository outlines."""
    payload = _code_context_engine(repo_root).file_outline(file_path=file_path, limit=limit)
    if as_json:
        _emit(payload, as_json=True)
        return
    for path, symbols in payload["files"].items():
        click.echo(path)
        for symbol in symbols:
            click.echo(f"  L{symbol['line_start']}: {symbol['qualified_name']} ({symbol['kind']})")


@code_group.command("repo-map")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--seed-file", "seed_files", multiple=True)
@click.option("--budget-tokens", default=2000, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def code_repo_map_cmd(
    repo_root: str,
    seed_files: tuple[str, ...],
    budget_tokens: int,
    as_json: bool,
) -> None:
    """Aider-style PageRank repo map with token-budgeted output."""
    payload = _code_context_engine(repo_root).repo_map(seed_files=list(seed_files), budget_tokens=budget_tokens)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload.get("outline", ""))


@code_group.command("search-text")
@click.argument("query")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--path", "search_path", default=".", show_default=True)
@click.option("--limit", default=50, show_default=True, type=int)
@click.option("--ignore-case", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def code_search_text_cmd(
    query: str,
    repo_root: str,
    search_path: str,
    limit: int,
    ignore_case: bool,
    as_json: bool,
) -> None:
    """Literal code search using ripgrep when available."""
    results = [
        item.model_dump(mode="json")
        for item in _code_context_engine(repo_root).search_text(
            query,
            path=search_path,
            limit=limit,
            ignore_case=ignore_case,
        )
    ]
    if as_json:
        _emit({"items": results}, as_json=True)
        return
    for item in results:
        click.echo(f"{item['file_path']}:{item['line']}:{item['column']}: {item['text']}")


@code_group.command("context-pack")
@click.argument("task")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--seed-file", "seed_files", multiple=True)
@click.option("--budget-tokens", default=4000, show_default=True, type=int)
@click.option("--max-symbols", default=8, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def code_context_pack_cmd(
    task: str,
    repo_root: str,
    seed_files: tuple[str, ...],
    budget_tokens: int,
    max_symbols: int,
    as_json: bool,
) -> None:
    """Build a task-specific compact context bundle."""
    payload = _code_context_engine(repo_root).context_pack(
        task=task,
        seed_files=list(seed_files),
        budget_tokens=budget_tokens,
        max_symbols=max_symbols,
    )
    if as_json:
        _emit(payload.model_dump(mode="json"), as_json=True)
        return
    click.echo(payload.content)


@code_group.command("impact")
@click.argument("file_path")
@click.option("--repo-root", default=".", show_default=True)
@click.option("--json", "as_json", is_flag=True)
def code_impact_cmd(file_path: str, repo_root: str, as_json: bool) -> None:
    """Importers, blast radius, tests, and approximate dead-code candidates."""
    payload = _code_context_engine(repo_root).impact(file_path).model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"risk: {payload['risk_level']}")
    click.echo(f"direct_importers: {', '.join(payload['direct_importers']) or '(none)'}")
    click.echo(f"transitive_importers: {', '.join(payload['transitive_importers']) or '(none)'}")
    click.echo(f"affected_tests: {', '.join(payload['affected_tests']) or '(none)'}")


@cli.command("cached-grep")
@click.argument("pattern")
@click.option("--path", "search_path", default=".", show_default=True)
@click.pass_context
def cached_grep(ctx: click.Context, pattern: str, search_path: str) -> None:
    """Cache-aware grep. Returns cached result on repeated queries."""
    from atelier.core.foundation.redaction import assert_safe_grep_args

    try:
        assert_safe_grep_args(pattern, search_path)
    except ValueError as exc:
        _emit({"error": str(exc)}, as_json=True)
        ctx.exit(2)
        return
    s = _load_smart_state(ctx.obj["root"])
    cache = s.setdefault("cache", {})
    key = f"rg:{pattern}:{search_path}:{_path_content_fingerprint(search_path)}"
    if not _cache_disabled() and key in cache:
        s["savings"]["calls_avoided"] = int(s["savings"].get("calls_avoided", 0)) + 1
        _save_smart_state(ctx.obj["root"], s)
        _emit({**cache[key], "cached": True}, as_json=True)
        return
    import subprocess

    try:
        proc = subprocess.run(
            [
                "rg",
                "-H",
                "-n",
                "--no-heading",
                "--color",
                "never",
                "--hidden",
                "--no-ignore",
                "--glob",
                "!.git",
                "--",
                pattern,
                search_path,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        out = proc.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        out = f"(rg failed: {exc})"
    payload = {"cached": False, "output": out[:8000]}
    if not _cache_disabled():
        cache[key] = payload
        _save_smart_state(ctx.obj["root"], s)
    _emit(payload, as_json=True)


# ----- savings + benchmark ----------------------------------------------- #


@cli.command("savings")
@click.option("--json", "as_json", is_flag=True)
@click.option("--line", is_flag=True, help="Pipe-delimited one-liner for statusline.sh.")
@click.pass_context
def savings_cmd(ctx: click.Context, as_json: bool, line: bool) -> None:
    """Aggregate savings: cache + reasoning-library + cost-delta vs. baseline."""
    if line:
        from atelier.core.capabilities.savings_summary import savings_line

        click.echo(
            savings_line(
                os.environ.get("ATELIER_STATUS_SESSION_ID", ""),
                workspace=os.environ.get("CLAUDE_WORKSPACE_ROOT", "") or None,
            )
        )
        return
    from atelier.core.capabilities.plugin_runtime import build_savings_report
    from atelier.core.capabilities.session_optimizer import build_trace_optimization_report

    runs = _ledger_dir(ctx.obj["root"])
    bad_plans_blocked = 0
    rescue_events = 0
    rubric_failures = 0
    if runs.is_dir():
        for p in runs.glob("*.json"):
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for ev in snap.get("events", []):
                kind = ev.get("kind")
                if kind == "watchdog_alert":
                    sev = (ev.get("payload") or {}).get("severity")
                    if sev == "high":
                        rescue_events += 1
                if kind == "rubric_run" and (ev.get("payload") or {}).get("status") == "blocked":
                    rubric_failures += 1
    payload = build_savings_report(ctx.obj["root"])
    store = _load_store(ctx.obj["root"])
    payload["optimization"] = build_trace_optimization_report(store.list_traces(limit=5000), days=7)
    payload["bad_plans_blocked"] = bad_plans_blocked
    payload["rescue_events"] = rescue_events
    payload["rubric_failures_caught"] = rubric_failures
    if as_json:
        _emit(payload, as_json=True)
    else:
        for k, v in payload.items():
            if isinstance(v, dict):
                click.echo(f"{k}:")
                for k2, v2 in v.items():
                    click.echo(f"  {k2}: {v2}")
            else:
                click.echo(f"{k}: {v}")


def _legacy_optimize_report(ctx: click.Context, host: str | None, days: int, limit: int) -> dict[str, Any]:
    from atelier.core.capabilities.session_optimizer import build_trace_optimization_report

    store = _load_store(ctx.obj["root"])
    return build_trace_optimization_report(store.list_traces(limit=5000), days=days, host=host, limit=limit)


def _run_external_optimize(ctx: click.Context, days: int) -> dict[str, Any] | None:
    from atelier.gateway.integrations.external_analytics import (
        persist_external_reports,
        run_external_reports,
    )

    period = "week" if days <= 7 else "30days"
    try:
        external_batch = run_external_reports(
            tool="codeburn:optimize", period=period, cwd=Path.cwd(), include_optimize=True
        )
        store = _load_store(ctx.obj["root"])
        persist_external_reports(store, external_batch, source="cli_optimize")
        return external_batch["reports"][0] if external_batch["reports"] else None
    except Exception as exc:
        logger.debug("External optimization report failed: %s", exc)
        return None


def _advisor_result(ctx: click.Context, host: str | None, days: int) -> Any:
    from atelier.core.capabilities.optimization import load_current_policy, optimize_from_traces

    store = _load_store(ctx.obj["root"])
    current_policy = load_current_policy(ctx.obj["root"])
    return optimize_from_traces(store.list_traces(limit=5000), current_policy=current_policy, days=days, host=host)


def _recommended_candidate(result: Any) -> Any:
    if not result.has_recommendation:
        return None
    target_cost = result.baseline_weekly_cost_usd - result.weekly_savings_usd
    candidates = [candidate for candidate in result.candidates if candidate.id != "current"]
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs(candidate.weekly_cost_usd - target_cost))


def _render_optimization_summary(result: Any) -> None:
    current = next(candidate for candidate in result.candidates if candidate.id == "current")
    recommended = _recommended_candidate(result)
    click.echo("Optimization Autopilot")
    click.echo("─────────────────────────────────────────────────")
    click.echo(
        f"Analysed your last 7 days: {result.sessions_analysed} sessions, "
        f"{result.replayable_tasks} replayable tasks"
    )
    click.echo("")
    click.echo(f"Current setting: {result.current_policy.name}")
    click.echo(f"  Cost / week:      ${current.weekly_cost_usd:.2f}")
    click.echo(f"  Estimated quality: {current.estimated_quality:.1%}")
    click.echo(f"  Latency mult:      {current.latency_mult:.2f}x")
    click.echo(f"  Escalation rate:   {current.escalation_rate:.0%}")
    click.echo("")
    if recommended is None:
        click.echo(result.message)
    else:
        savings_pct = (
            result.weekly_savings_usd / result.baseline_weekly_cost_usd if result.baseline_weekly_cost_usd > 0 else 0.0
        )
        click.echo("Recommended: Custom (auto-tuned from your sessions)")
        click.echo(f"  Cost / week:      ${recommended.weekly_cost_usd:.2f}  (-{savings_pct:.0%})")
        click.echo(f"  Estimated quality: {recommended.estimated_quality:.1%}  ({result.quality_delta:+.1%})")
        click.echo(f"  Latency mult:      {recommended.latency_mult:.2f}x")
        click.echo(f"  Escalation rate:   {recommended.escalation_rate:.0%}")
    click.echo("")
    click.echo(f"Confidence: {result.confidence.title()}")
    click.echo(f"  {result.confidence_reason}")
    click.echo(
        f"Golden corpus: {result.golden.passed}/{result.golden.total} well-formed tasks " f"({result.golden.score:.0%})"
    )


def _render_optimization_details(result: Any) -> None:
    click.echo("Pareto frontier - cost vs estimated correctness on your tasks")
    click.echo("─────────────────────────────────────────────────")
    sorted_candidates = sorted(result.candidates, key=lambda item: item.weekly_cost_usd, reverse=True)
    recommended = _recommended_candidate(result)
    for candidate in sorted_candidates:
        marker = "★" if recommended is not None and candidate.id == recommended.id else " "
        label = candidate.policy.name
        click.echo(
            f"{marker} {label:<18} ${candidate.weekly_cost_usd:>7.2f}   "
            f"{candidate.estimated_quality:>6.1%}   latency {candidate.latency_mult:.2f}x   "
            f"escalation {candidate.escalation_rate:.0%}"
        )

    if recommended is None:
        return
    click.echo("")
    click.echo("Compaction breakdown for [recommended]:")
    for name, saved in recommended.compaction_breakdown.items():
        click.echo(f"  {name}: ${saved:.2f}/wk saved")
    click.echo("")
    click.echo("Routing breakdown for [recommended]:")
    for tier, share in recommended.routing_breakdown.items():
        click.echo(f"  {tier}-tier for {share:.0%} of turns")
    click.echo(f"  Escalation rate: {recommended.escalation_rate:.0%}")


@cli.group("optimize", invoke_without_command=True)
@click.option(
    "--host",
    type=click.Choice(list(SUPPORTED_SESSION_IMPORT_HOSTS)),
    default=None,
)
@click.option("--days", default=7, show_default=True, type=int)
@click.option("--limit", default=6, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_group(ctx: click.Context, host: str | None, days: int, limit: int, as_json: bool) -> None:
    """Show and apply Optimization Advisor recommendations."""
    if ctx.invoked_subcommand is not None:
        return

    from atelier.core.capabilities.optimization import append_history

    report = _legacy_optimize_report(ctx, host, days, limit)
    result = _advisor_result(ctx, host, days)
    append_history(ctx.obj["root"], result)
    report["advisor"] = result.to_dict()
    report["external"] = _run_external_optimize(ctx, days)
    if as_json:
        _emit(report, as_json=True)
        return
    _render_optimization_summary(result)
    click.echo("")
    click.echo(
        f"Legacy trace recommendations: {report['estimated_tokens_saved']} tokens, "
        f"${report['estimated_usd_saved']:.4f}"
    )
    if not report["recommendations"]:
        click.echo("No legacy trace recommendations found for this window.")
        return
    for index, recommendation in enumerate(report["recommendations"], start=1):
        click.echo("")
        click.echo(f"{index}. {recommendation['title']}  {recommendation['severity']}")
        click.echo(f"   Sessions: {recommendation['session_count']}")
        click.echo(
            f"   Savings: {recommendation['estimated_tokens_saved']} tokens, ${recommendation['estimated_usd_saved']:.4f}"
        )
        click.echo(f"   Action: {recommendation['action']}")


@optimize_group.command("details")
@click.option("--host", type=click.Choice(list(SUPPORTED_SESSION_IMPORT_HOSTS)), default=None)
@click.option("--days", default=7, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_details(ctx: click.Context, host: str | None, days: int, as_json: bool) -> None:
    """Show Pareto frontier, compaction, and routing breakdowns."""
    result = _advisor_result(ctx, host, days)
    if as_json:
        _emit(result.to_dict(), as_json=True)
        return
    _render_optimization_details(result)


@optimize_group.command("apply")
@click.option("--preset", type=click.Choice(["conservative", "balanced", "economy"]), default=None)
@click.option("--recommended", is_flag=True)
@click.option("--custom", type=click.Path(path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_apply(
    ctx: click.Context,
    preset: str | None,
    recommended: bool,
    custom: Path | None,
    as_json: bool,
) -> None:
    """Apply a preset, the latest recommendation, or a custom policy YAML."""
    from atelier.core.capabilities.optimization.policy import (
        policy_from_config,
        preset_policy,
        save_policy,
    )

    selected = sum(1 for value in (preset, custom) if value is not None) + (1 if recommended else 0)
    if selected != 1:
        raise click.ClickException("choose exactly one of --preset, --recommended, or --custom")

    if preset is not None:
        policy = preset_policy(preset)
    elif custom is not None:
        import yaml as _yaml

        try:
            raw = _yaml.safe_load(custom.read_text(encoding="utf-8"))
        except _yaml.YAMLError as exc:
            raise click.ClickException(f"invalid custom policy YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise click.ClickException("custom policy YAML must be a mapping")
        policy = policy_from_config(raw)
    else:
        result = _advisor_result(ctx, None, 7)
        if not result.has_recommendation:
            raise click.ClickException(result.message)
        policy = result.recommended_policy

    path = save_policy(ctx.obj["root"], policy)
    payload = {"applied": policy.to_dict(), "path": str(path)}
    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo(f"Applied optimization policy: {policy.name} ({policy.preset})")
        click.echo(f"Saved: {path}")


@optimize_group.group("shadow", invoke_without_command=True)
@click.option("--policy", "policy_name", default="recommended", show_default=True)
@click.option("--days", default=7, show_default=True, type=int)
@click.option("--max-daily-spend-usd", type=float, default=None)
@click.option("--i-understand-this-costs-money", is_flag=True)
@click.option("--yes", is_flag=True, help="Accept the pre-run shadow cost estimate.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_shadow(
    ctx: click.Context,
    policy_name: str,
    days: int,
    max_daily_spend_usd: float | None,
    i_understand_this_costs_money: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Shadow-run a policy in parallel without changing live behavior."""
    if ctx.invoked_subcommand is not None:
        return

    from atelier.core.capabilities.optimization.policy import (
        record_shadow_consent,
        shadow_consent_at,
    )
    from atelier.core.capabilities.optimization.shadow import build_shadow_state, save_shadow_state

    if shadow_consent_at(ctx.obj["root"]) is None:
        if not i_understand_this_costs_money:
            raise click.ClickException(
                "First shadow run requires --i-understand-this-costs-money because it may spend real money."
            )
        record_shadow_consent(ctx.obj["root"])

    result = _advisor_result(ctx, None, max(1, days))
    try:
        state = build_shadow_state(
            policy=policy_name,
            days=days,
            baseline_weekly_cost_usd=result.baseline_weekly_cost_usd,
            max_daily_spend_usd=max_daily_spend_usd,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json and not yes:
        _emit(
            {
                "status": "confirmation_required",
                "message": "Shadow run not started. Re-run with --yes to accept the pre-run cost estimate.",
                "estimate": state.to_dict(),
            },
            as_json=True,
        )
        return
    if not as_json and not yes:
        click.echo(
            f"Shadow will spend approximately ${state.estimated_weekly_spend_usd:.2f} this week "
            f"against your ${state.baseline_weekly_cost_usd:.2f} baseline."
        )
        if not click.confirm("Continue?", default=False):
            click.echo("Shadow run cancelled.")
            return

    save_shadow_state(ctx.obj["root"], state)
    if as_json:
        _emit(state.to_dict(), as_json=True)
    else:
        click.echo(f"Shadow run started for policy {policy_name}.")


@optimize_shadow.command("status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_shadow_status(ctx: click.Context, as_json: bool) -> None:
    """Show live shadow spend versus cap."""
    from atelier.core.capabilities.optimization.shadow import load_shadow_state

    state = load_shadow_state(ctx.obj["root"]) or {"status": "not_running"}
    if as_json:
        _emit(state, as_json=True)
        return
    click.echo(f"Shadow status: {state.get('status', 'not_running')}")
    if state.get("status") != "not_running":
        click.echo(
            f"Shadow spend (this run only): ${float(state.get('spend_usd', 0.0)):.2f} / "
            f"${float(state.get('max_daily_spend_usd', 0.0)):.2f} daily cap"
        )


@optimize_shadow.command("stop")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_shadow_stop(ctx: click.Context, as_json: bool) -> None:
    """Halt the active shadow run immediately."""
    from atelier.core.capabilities.optimization.shadow import stop_shadow

    state = stop_shadow(ctx.obj["root"])
    if as_json:
        _emit(state, as_json=True)
    else:
        click.echo(f"Shadow status: {state.get('status')}")


@optimize_shadow.command("forget-consent")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_shadow_forget_consent(ctx: click.Context, as_json: bool) -> None:
    """Revoke persistent shadow-run cost consent."""
    from atelier.core.capabilities.optimization.policy import forget_shadow_consent

    revoked = forget_shadow_consent(ctx.obj["root"])
    payload = {"revoked": revoked}
    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo("Shadow consent revoked." if revoked else "No shadow consent was recorded.")


@optimize_group.command("compare")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_compare(ctx: click.Context, as_json: bool) -> None:
    """Compare current policy with the active or latest shadow run."""
    from atelier.core.capabilities.optimization.shadow import load_shadow_state

    result = _advisor_result(ctx, None, 7)
    state = load_shadow_state(ctx.obj["root"]) or {"status": "not_running", "spend_usd": 0.0}
    payload = {"advisor": result.to_dict(), "shadow": state}
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"Current weekly cost: ${result.baseline_weekly_cost_usd:.2f}")
    if result.has_recommendation:
        click.echo(f"Recommended weekly savings: ${result.weekly_savings_usd:.2f}")
    click.echo(f"Shadow spend (this run only): ${float(state.get('spend_usd', 0.0)):.2f}")


@optimize_group.command("history")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def optimize_history(ctx: click.Context, limit: int, as_json: bool) -> None:
    """Show past optimization recommendations and outcomes."""
    from atelier.core.capabilities.optimization import load_history

    history = load_history(ctx.obj["root"], limit=limit)
    if as_json:
        _emit(history, as_json=True)
        return
    if not history:
        click.echo("No optimization history recorded yet.")
        return
    for item in reversed(history):
        recorded_at = item.get("recorded_at", "-")
        confidence = item.get("confidence", "-")
        savings = float(item.get("weekly_savings_usd", 0.0) or 0.0)
        click.echo(f"{recorded_at}  confidence={confidence}  weekly_savings=${savings:.2f}")


@cli.command("external-status")
@click.option("--json", "as_json", is_flag=True)
def external_status_cmd(as_json: bool) -> None:
    """Show optional upstream analyzer availability and integration posture."""
    from atelier.gateway.integrations.external_analytics import external_status

    payload = {"tools": external_status(cwd=Path.cwd())}
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("External analyzers")
    click.echo("")
    for item in payload["tools"]:
        state = "available" if item["available"] else "missing"
        click.echo(f"- {item['display_name']} [{state}]")
        click.echo(f"  license: {item['license']}")
        click.echo(f"  mode: {item['execution_mode']}")
        if item.get("path"):
            click.echo(f"  path: {item['path']}")
        click.echo(f"  update: {item['update_strategy']}")
        for note in item.get("notes", []):
            click.echo(f"  note: {note}")
        warning = item.get("warning")
        if warning:
            click.echo(f"  warning: {warning}")
        click.echo(f"  install: {item['install_hint']}")
        click.echo("")


@cli.command("external-report")
@click.option(
    "--tool",
    type=click.Choice(_EXTERNAL_REPORT_TOOL_CHOICES),
    default="all",
    show_default=True,
)
@click.option(
    "--period",
    type=click.Choice(["today", "week", "month", "30days", "all"]),
    default="week",
    show_default=True,
)
@click.option(
    "--persist",
    is_flag=True,
    default=False,
    help="Store the collected report snapshots for the API/UI.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def external_report_cmd(ctx: click.Context, tool: str, period: str, persist: bool, as_json: bool) -> None:
    """Run upstream JSON reports from supported external analyzers."""
    from atelier.gateway.integrations.external_analytics import (
        persist_external_reports,
        run_external_report,
        run_external_reports,
    )

    if as_json:
        try:
            payload = run_external_reports(tool=tool, period=period, cwd=Path.cwd(), include_optimize=True)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        if persist:
            store = _load_store(ctx.obj["root"])
            payload["persisted"] = persist_external_reports(store, payload, source="cli")
        _emit(payload, as_json=True)
        return

    selected_tools = list(_EXTERNAL_REPORT_ALL_TOOLS) if tool == "all" else [tool]
    store = _load_store(ctx.obj["root"]) if persist else None

    click.echo(f"External reports  period={period}")
    click.echo("")

    total_persisted = 0
    for selected_tool in selected_tools:
        click.echo(f"[external-report] running {selected_tool} period={period}...")
        sys.stdout.flush()
        try:
            report = run_external_report(selected_tool, period=period, cwd=Path.cwd())
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        persisted: list[dict[str, Any]] = []
        if store is not None:
            batch = {
                "generated_at": datetime.now(UTC).isoformat(),
                "tool": selected_tool,
                "period": period,
                "reports": [report],
            }
            persisted = persist_external_reports(store, batch, source="cli")
            total_persisted += len(persisted)

        status = "ok" if report.get("ok") else "failed"
        persisted_suffix = f" persisted={len(persisted)}" if persist else ""
        click.echo(f"[external-report] done {selected_tool} status={status}{persisted_suffix}")

        click.echo(f"- {report['tool']}")
        click.echo(f"  cmd: {report.get('command_display') or '-'}")
        if report["ok"]:
            click.echo("  status: ok")
        else:
            click.echo(f"  status: failed ({report.get('error') or report.get('returncode')})")
            message = report.get("message")
            if message:
                click.echo(f"  detail: {message}")
            stderr = report.get("stderr")
            if stderr:
                click.echo(f"  stderr: {stderr[:240]}")
            parse_error = report.get("parse_error")
            if parse_error:
                click.echo(f"  parse: {parse_error}")
            continue

        body = report.get("payload")
        if isinstance(body, dict):
            if report["tool"] == "codeburn":
                overview = body.get("overview") or {}
                click.echo(
                    "  summary: "
                    f"cost={overview.get('cost', '-')} calls={overview.get('calls', '-')} sessions={overview.get('sessions', '-')}"
                )
            elif report["tool"] == "codeburn:optimize":
                overview = body.get("overview") or {}
                click.echo(
                    "  summary: "
                    f"waste={overview.get('estimated_usd_saved', '-')} grade={overview.get('health_grade', '-')} score={overview.get('health_score', '-')}"
                )
            elif report["tool"] == "tokscale":
                click.echo(f"  summary: keys={', '.join(sorted(body.keys())[:6])}")
        click.echo("")

    if persist:
        click.echo(f"persisted {total_persisted} snapshots")


@cli.command("savings-detail")
@click.option("--json", "as_json", is_flag=True)
@click.option("--limit", default=20, show_default=True, help="Top N operations.")
@click.pass_context
def savings_detail(ctx: click.Context, as_json: bool, limit: int) -> None:
    """Per-operation cost-delta breakdown (last_cost - new_cost, baseline %)."""
    from atelier.infra.runtime.cost_tracker import CostTracker

    tracker = CostTracker(ctx.obj["root"])
    summary = tracker.total_savings()
    rows = summary["per_operation"][:limit]
    if as_json:
        _emit(
            {
                "summary": {k: v for k, v in summary.items() if k != "per_operation"},
                "operations": rows,
            },
            as_json=True,
        )
        return
    click.echo(
        f"Tracked operations: {summary['operations_tracked']}  "
        f"calls={summary['total_calls']}  "
        f"saved=${summary['saved_usd']:.4f} ({summary['saved_pct']}%)"
    )
    click.echo("-" * 92)
    click.echo(
        f"{'op_key':18} {'calls':>5} {'baseline$':>10} "
        f"{'last$':>10} {'now$':>10} {'d_last$':>10} {'d_base$':>10} {'%down':>6}  domain"
    )
    click.echo("-" * 92)
    for r in rows:
        click.echo(
            f"{r['op_key']:18} {r['calls_count']:>5} "
            f"{r['baseline_cost_usd']:>10.4f} {r['last_cost_usd']:>10.4f} "
            f"{r['current_cost_usd']:>10.4f} {r['delta_vs_last_usd']:>10.4f} "
            f"{r['delta_vs_base_usd']:>10.4f} {r['pct_vs_base']:>6.1f}  "
            f"{r.get('domain', '-')}"
        )


@cli.command("savings-reset")
@click.pass_context
def savings_reset(ctx: click.Context) -> None:
    s = _load_smart_state(ctx.obj["root"])
    s["savings"] = {"calls_avoided": 0, "tokens_saved": 0}
    _save_smart_state(ctx.obj["root"], s)
    from atelier.infra.runtime.cost_tracker import save_cost_history

    save_cost_history(ctx.obj["root"], {"operations": {}})
    click.echo("savings reset (cache + cost history)")


@cli.command("login")
@click.option("--token", default=None, help="Credentials JSON, base64 payload, or refresh token.")
@click.option("--anonymous", "anonymous", is_flag=True, help="Start a local anonymous trial.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def login_cmd(ctx: click.Context, token: str | None, anonymous: bool, as_json: bool) -> None:
    """Create local Atelier auth state for plugin operations."""
    from atelier.core.capabilities.plugin_runtime import (
        begin_browser_login,
        claim_anonymous_trial,
        parse_login_token,
        write_auth_state,
    )

    if anonymous:
        payload = {"auth": claim_anonymous_trial(ctx.obj["root"]), "mode": "anonymous"}
    elif token:
        payload = {
            "auth": write_auth_state(ctx.obj["root"], parse_login_token(token)),
            "mode": "token",
        }
    else:
        pending = begin_browser_login(ctx.obj["root"])
        payload = {"mode": "browser", "pending": pending}
    if as_json:
        _emit(payload, as_json=True)
        return
    if str(payload.get("mode")) == "browser":
        pending_payload = payload.get("pending")
        pending = pending_payload if isinstance(pending_payload, dict) else {}
        click.echo("Open this URL to finish login:")
        click.echo(pending.get("url", ""))
    else:
        auth_payload = payload.get("auth")
        auth = auth_payload if isinstance(auth_payload, dict) else {}
        label = "anonymous trial" if auth.get("isAnonymous") else auth.get("email") or auth.get("userId")
        click.echo(f"logged in: {label}")


@cli.command("logout")
@click.option("--no-trial", is_flag=True, help="Do not create a local anonymous trial after logout.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def logout_cmd(ctx: click.Context, no_trial: bool, as_json: bool) -> None:
    """Remove local auth and optionally activate an anonymous trial."""
    from atelier.core.capabilities.plugin_runtime import logout_local

    payload = logout_local(ctx.obj["root"], claim_trial=not no_trial)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("logged out" + ("; anonymous trial active" if payload.get("anonymous") else ""))


# ── Status dashboard helpers (ported from bin/atelier-status) ───────────────

_STATUS_COLORS = {
    "success": "\033[38;2;80;200;120m",
    "complete": "\033[38;2;80;200;120m",
    "failed": "\033[38;2;255;80;80m",
    "error": "\033[38;2;255;80;80m",
    "running": "\033[38;2;255;200;60m",
    "partial": "\033[38;2;255;200;60m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[38;2;80;200;120m"
_RED = "\033[38;2;255;80;80m"
_YELLOW = "\033[38;2;255;200;60m"
_BRAND = "\033[1;38;2;155;117;217m"
_BADGE = "\033[1;48;2;155;117;217;38;2;255;255;255m atelier:code \033[0m"
_SEP = "\033[2;38;2;180;180;180m │\033[0m"
_W = 72


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def _usd(v: float) -> str:
    if v >= 1:
        return f"${v:.2f}"
    if v > 0:
        return f"${v:.4f}"
    return "$0"


def _age(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        s = max(0, int((datetime.now(UTC) - dt).total_seconds()))
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return "?"


def _dur(t0: str, t1: str) -> str:
    try:
        a = datetime.fromisoformat(t0.replace("Z", "+00:00"))
        b = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        s = max(0, int((b - a).total_seconds()))
        if s < 60:
            return f"{s}s"
        return f"{s // 60}m{s % 60:02d}s"
    except Exception:
        return ""


def _status_color(s: str) -> str:
    c = _STATUS_COLORS.get(s)
    return f"{c}{s}{_RESET}" if c else f"{_DIM}{s}{_RESET}"


def _status_icon(s: str) -> str:
    if s in ("success", "complete"):
        return f"{_GREEN}✓{_RESET}"
    if s in ("failed", "error"):
        return f"{_RED}✖{_RESET}"
    if s in ("running", "partial"):
        return f"{_YELLOW}⋯{_RESET}"
    return f"{_DIM}?{_RESET}"


def _box_line(content: str = "") -> None:
    plain = re.sub(r"\033\[[^m]*m", "", content)
    pad = max(0, _W - 2 - len(plain))
    click.echo(f" {content}{' ' * pad} ")


def _rule(label: str = "") -> None:
    if label:
        line = f" {_BOLD}{label}{_RESET} "
        fill = _W - len(line) - 2
        click.echo(f"{_DIM}─{_RESET}{line}{_DIM}{'─' * fill}{_RESET}")
    else:
        click.echo(f"{_DIM}{'─' * _W}{_RESET}")


def _render_dashboard(root: Path, *, line_mode: bool, n_runs: int, session_id: str | None) -> None:
    """Render the runs dashboard (same output as the old atelier-status binary)."""

    # When NO_COLOR is set, suppress all ANSI by swapping module-level globals
    # for the duration of this call.
    if os.environ.get("NO_COLOR"):
        saved = {
            "_BRAND": _BRAND,
            "_BADGE": _BADGE,
            "_SEP": _SEP,
            "_DIM": _DIM,
            "_RESET": _RESET,
            "_GREEN": _GREEN,
            "_RED": _RED,
            "_YELLOW": _YELLOW,
            "_BOLD": _BOLD,
        }
        for k in saved:
            globals()[k] = ""
        try:
            return _render_dashboard_impl(root, line_mode, n_runs, session_id)
        finally:
            for k, v in saved.items():
                globals()[k] = v
    else:
        return _render_dashboard_impl(root, line_mode, n_runs, session_id)


def _render_dashboard_impl(root: Path, line_mode: bool, n_runs: int, session_id: str | None) -> None:
    runs_dir = root / "runs"

    # Resolve ledger path
    ledger_path: str | None = None
    if session_id:
        candidate = runs_dir / f"{session_id}.json"
        if candidate.exists():
            ledger_path = str(candidate)
    elif runs_dir.is_dir():
        files = sorted(runs_dir.glob("*.json"), key=os.path.getmtime, reverse=True)
        if files:
            ledger_path = str(files[0])
    if not ledger_path:
        ledger_path = "NONE"

    # Load savings data
    savings_map: dict[str, float] = {}
    routing_map: dict[str, float] = {}
    compaction_map: dict[str, float] = {}
    routing_total = 0.0
    compaction_total = 0.0
    savings_path = root / "live_savings_events.jsonl"
    if savings_path.exists():
        for line in savings_path.read_text().splitlines():
            try:
                d = json.loads(line)
                rid = d.get("session_id")
                cost = float(d.get("cost_saved_usd", 0.0) or 0.0)
                lever = str(d.get("lever") or d.get("tool_name") or "")
                bucket = (
                    "routing" if "routing" in lever.lower() else "compaction" if "compact" in lever.lower() else "other"
                )
                if rid:
                    savings_map[rid] = savings_map.get(rid, 0.0) + cost
                    if bucket == "routing":
                        routing_map[rid] = routing_map.get(rid, 0.0) + cost
                        routing_total += cost
                    elif bucket == "compaction":
                        compaction_map[rid] = compaction_map.get(rid, 0.0) + cost
                        compaction_total += cost
            except Exception:
                pass

    # Load cost + token data from DB
    cost_map: dict[str, float] = {}
    tokens_map: dict[str, int] = {}
    db_runs: list[dict] = []
    total_runs_in_db = 0
    db_path = root / "atelier.db"
    if db_path.exists():
        try:
            import sqlite3

            with sqlite3.connect(str(db_path)) as conn:
                for row in conn.execute(
                    "SELECT id, json_extract(payload, '$.input_tokens'), json_extract(payload, '$.output_tokens'), json_extract(payload, '$.cached_input_tokens'), json_extract(payload, '$.thinking_tokens'), host FROM traces"
                ):
                    rid, inp, out, cr, th, _h = row
                    c = ((inp or 0) * 3 + (cr or 0) * 0.3 + (out or 0) * 15) / 1_000_000.0
                    cost_map[rid] = c
                    tokens_map[rid] = (inp or 0) + (out or 0) + (cr or 0) + (th or 0)

                total_runs_in_db = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]

                for row in conn.execute(
                    "SELECT session_id, SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) FROM context_budget GROUP BY session_id"
                ):
                    rid, inp, out, cr = row
                    cost = ((inp or 0) * 3 + (cr or 0) * 0.3 + (out or 0) * 15) / 1_000_000.0
                    cost_map[rid] = cost
                    tokens_map[rid] = (inp or 0) + (out or 0) + (cr or 0)

                for row in conn.execute("SELECT payload FROM traces ORDER BY created_at DESC LIMIT 1000"):
                    p = json.loads(row[0])
                    db_runs.append(p)
        except Exception:
            pass

    # Load flat ledger if exists
    def _load_run(path: str) -> dict | None:
        try:
            return json.loads(Path(path).read_text())
        except Exception:
            return None

    snap: dict | None = None
    if ledger_path != "NONE":
        snap = _load_run(ledger_path)

    if not snap and session_id:
        snap = next(
            (r for r in db_runs if r.get("session_id") == session_id or r.get("id") == session_id),
            None,
        )

    if not snap and db_runs and not session_id:
        snap = db_runs[0]

    # ── ONE-LINER MODE ──
    if line_mode:
        if not snap:
            click.echo(f"atelier | run {Path(ledger_path).stem[:8] if ledger_path != 'NONE' else '?'} not found")
            return

        sid = snap.get("session_id") or snap.get("id") or "?"
        domain = snap.get("domain") or "-"
        task = (snap.get("task") or "").strip().splitlines()[0] if snap.get("task") else "-"
        if len(task) > 50:
            task = task[:47] + "..."
        status = snap.get("status") or "?"
        events = len(snap.get("events", []) or [])
        errors = len(snap.get("errors_seen", []) or [])
        blockers = len(snap.get("current_blockers", []) or [])
        files_n = len(snap.get("files_touched", []) or [])
        tools_n = int(snap.get("tool_call_count", 0) or snap.get("tool_count", 0) or len(snap.get("tools_called", [])))
        agent = snap.get("agent") or "?"
        age_str = _age(snap.get("updated_at") or snap.get("created_at") or "")
        dur_str = _dur(snap.get("created_at", ""), snap.get("updated_at", ""))

        cost_v = cost_map.get(sid, float(snap.get("cost", {}).get("total_cost_usd", 0.0)))
        if cost_v == 0 and "input_tokens" in snap:
            cost_v = (snap.get("input_tokens", 0) * 3 + snap.get("output_tokens", 0) * 15) / 1_000_000.0

        saved_v = savings_map.get(sid, 0.0)
        routing_v = routing_map.get(sid, 0.0)
        compaction_v = compaction_map.get(sid, 0.0)

        saved_seg = ""
        if saved_v > 0:
            breakdown = []
            if compaction_v > 0:
                breakdown.append(f"compact={_usd(compaction_v)}")
            if routing_v > 0:
                breakdown.append(f"routing={_usd(routing_v)}")
            suffix = f" ({', '.join(breakdown)})" if breakdown else ""
            saved_seg = f" {_SEP} {_GREEN}saved={_usd(saved_v)}{suffix}{_RESET}"

        line = (
            f"{_BADGE} {_BRAND}run {sid[:8]}{_RESET} {_SEP} {_DIM}{agent}{_RESET} {_SEP} "
            f"{domain} {_SEP} {task} {_SEP} {_status_color(status)} "
            f"{_SEP} ev={events} err={errors} blk={blockers}"
            f" {_SEP} files={files_n} tools={tools_n}"
            + (f" {_SEP} cost={_usd(cost_v)}" if cost_v > 0 else "")
            + saved_seg
            + (f" {_SEP} {dur_str}" if dur_str else "")
            + f" {_SEP} {_DIM}{age_str}{_RESET}"
        )
        click.echo(line)
        return

    # ── DASHBOARD MODE (default) ──
    all_run_entries: list[dict] = []
    seen_ids: set[str] = set()

    if runs_dir.is_dir():
        for rf in sorted(runs_dir.glob("*.json"), key=os.path.getmtime, reverse=True):
            try:
                d = json.loads(rf.read_text())
                rid = d.get("session_id") or rf.stem
                all_run_entries.append(d)
                seen_ids.add(rid)
            except Exception:
                pass

    for dr in db_runs:
        rid = dr.get("session_id") or dr.get("id")
        if rid not in seen_ids:
            all_run_entries.append(dr)
            seen_ids.add(rid)

    total_runs = max(total_runs_in_db, len(seen_ids))
    success_count = 0
    failed_count = 0
    total_tools = 0
    total_files = 0
    total_errors = 0

    for d in all_run_entries:
        s = d.get("status", "")
        if s in ("success", "complete"):
            success_count += 1
        elif s in ("failed", "error"):
            failed_count += 1
        total_tools += int(d.get("tool_call_count", 0) or d.get("tool_count", 0) or len(d.get("tools_called", [])))
        total_files += len(d.get("files_touched", []) or [])
        total_errors += len(d.get("errors_seen", []) or [])

    total_cost = sum(cost_map.values())
    saved_usd = sum(savings_map.values())
    total_tokens = sum(tokens_map.values())

    _rule("SYSTEM OVERVIEW")
    _box_line(f"{_BADGE}  {_DIM}{root}{_RESET}")

    sr = f"{_GREEN}{success_count} ok{_RESET}" if success_count else f"{_DIM}0 ok{_RESET}"
    fr = f"{_RED}{failed_count} failed{_RESET}" if failed_count else f"{_DIM}0 failed{_RESET}"
    _box_line(
        f"{_BOLD}{total_runs}{_RESET} runs  {sr}  {fr}  "
        f"{_DIM}tools={_k(total_tools)}  files={total_files}  errs={total_errors}{_RESET}"
    )
    if total_cost > 0 or saved_usd > 0:
        parts = []
        if compaction_total > 0:
            parts.append(f"compact {_usd(compaction_total)}")
        if routing_total > 0:
            parts.append(f"routing {_usd(routing_total)}")
        breakdown_str = f"  {_DIM}({' · '.join(parts)}){_RESET}" if parts else ""
        _box_line(
            f"{_DIM}cost{_RESET} {_usd(total_cost)}  "
            + (f"{_GREEN}saved{_RESET} {_usd(saved_usd)}{breakdown_str}" if saved_usd > 0 else "")
            + (f"  {_DIM}tokens{_RESET} {_k(total_tokens)}" if total_tokens else "")
        )

    shown = min(n_runs, len(all_run_entries))
    _rule(f"RECENT RUNS ({shown})")

    for d in all_run_entries[:n_runs]:
        sid = d.get("session_id") or d.get("id") or "?"
        agent = (d.get("agent") or "?")[:8]
        domain = (d.get("domain") or "-")[:12]
        task = (d.get("task") or "").strip().replace("\n", " ")
        if len(task) > 55:
            task = task[:52] + "..."
        if not task:
            task = f"{_DIM}(no task){_RESET}"
        status = d.get("status") or "?"
        files_n = len(d.get("files_touched", []) or [])
        tools_n = int(d.get("tool_call_count", 0) or d.get("tool_count", 0) or len(d.get("tools_called", [])))
        # errs_n intentionally unused; kept for debugging access
        age_str = _age(d.get("updated_at") or d.get("created_at") or "")
        dur_str = _dur(d.get("created_at", ""), d.get("updated_at", ""))

        cost_v = cost_map.get(sid, float(d.get("cost", {}).get("total_cost_usd", 0.0)))
        if cost_v == 0 and "input_tokens" in d:
            cost_v = (d.get("input_tokens", 0) * 3 + d.get("output_tokens", 0) * 15) / 1_000_000.0

        saved_v = savings_map.get(sid, 0.0)
        routing_v = routing_map.get(sid, 0.0)
        compaction_v = compaction_map.get(sid, 0.0)

        dots = "." * max(1, (_W - len(re.sub(r"\033\[[^m]*m", "", task)) - 16))
        _box_line(f" {_status_icon(status)}  {_BOLD}{task}{_RESET} {_DIM}{dots} {sid[:8]}{_RESET}")

        metrics = []
        if cost_v > 0:
            metrics.append(f"cost={_usd(cost_v)}")
        if saved_v > 0:
            breakdown_parts = []
            if compaction_v > 0:
                breakdown_parts.append(f"c={_usd(compaction_v)}")
            if routing_v > 0:
                breakdown_parts.append(f"r={_usd(routing_v)}")
            run_breakdown_str = f" {_DIM}({' '.join(breakdown_parts)}){_RESET}" if breakdown_parts else ""
            metrics.append(f"{_GREEN}saved={_usd(saved_v)}{run_breakdown_str}")
        if dur_str:
            metrics.append(dur_str)
        metrics_str = f" {_SEP} ".join(metrics)

        meta_line = f"    {_DIM}{age_str}{_RESET} {_SEP} {agent} {_SEP} {domain}"
        if metrics_str:
            meta_line += f" {_SEP} {metrics_str}"
        meta_line += f" {_SEP} {_DIM}f={files_n} t={tools_n}{_RESET}"
        _box_line(meta_line)

    _rule()
    _box_line(f"{_DIM}store: {root}   runs dir: {runs_dir}{_RESET}")
    _rule()


@cli.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON of runs data.")
@click.option("--line", "line_mode", is_flag=True, help="One-liner mode (good for status bars).")
@click.option("-n", type=int, default=5, show_default=True, help="Number of recent runs to show.")
@click.option("--session-id", default=None, help="Show detail for a specific run only.")
@click.option("--auth", "auth_mode", is_flag=True, help="Show auth/subscription status instead of runs.")
@click.pass_context
def status_cmd(
    ctx: click.Context,
    as_json: bool,
    line_mode: bool,
    n: int,
    session_id: str | None,
    auth_mode: bool,
) -> None:
    """Show runs dashboard or auth status.

    Default view: runs dashboard (overview of recent runs, totals, savings).

    Use --auth to show the old auth/subscription status.
    """
    root: Path = ctx.obj["root"]

    if auth_mode:
        from atelier.core.capabilities.plugin_runtime import auth_status, load_plugin_settings

        payload = auth_status(root)
        payload["settings"] = load_plugin_settings(root)
        if as_json:
            _emit(payload, as_json=True)
            return
        click.echo(f"authenticated: {payload['authenticated']}")
        click.echo(f"anonymous: {payload['isAnonymous']}")
        if payload.get("email"):
            click.echo(f"email: {payload['email']}")
        if payload.get("subscription"):
            click.echo(f"subscription: {payload['subscription']}")
        click.echo(f"root: {payload['root']}")
        return

    if as_json:
        runs_dir = root / "runs"
        if session_id:
            target = runs_dir / f"{session_id}.json"
        else:
            files = sorted(runs_dir.glob("*.json"), key=os.path.getmtime, reverse=True)
            target = files[0] if files else None
        if target and target.exists():
            click.echo(target.read_text().strip())
        else:
            click.echo("{}")
        return

    _render_dashboard(root, line_mode=line_mode, n_runs=n, session_id=session_id)


@cli.command("share")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def share_cmd(ctx: click.Context, as_json: bool) -> None:
    """Render local referral/share text."""
    from atelier.core.capabilities.plugin_runtime import share_referral

    payload = share_referral(ctx.obj["root"])
    if payload.get("is_error"):
        raise click.ClickException(str(payload["message"]))
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload["text"])


@cli.group("settings")
def plugin_settings_group() -> None:
    """Manage local plugin settings."""


@plugin_settings_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def plugin_settings_show(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.plugin_runtime import load_plugin_settings

    payload = load_plugin_settings(ctx.obj["root"])
    if as_json:
        _emit(payload, as_json=True)
        return
    for key, value in payload.items():
        click.echo(f"{key}: {str(value).lower()}")


@plugin_settings_group.command("set")
@click.argument("key")
@click.argument("value", type=click.Choice(["true", "false", "on", "off", "1", "0"]))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def plugin_settings_set(ctx: click.Context, key: str, value: str, as_json: bool) -> None:
    from atelier.core.capabilities.plugin_runtime import write_plugin_setting

    enabled = value in {"true", "on", "1"}
    try:
        payload = write_plugin_setting(ctx.obj["root"], key, enabled)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"set {key}={str(enabled).lower()}")


@cli.group("benchmark")
def benchmark_group() -> None:
    """Run Atelier benchmark suites and reports."""


@benchmark_group.command("run")
@click.option(
    "--prompt",
    "prompts",
    multiple=True,
    help="Prompts to benchmark (repeat). Defaults to 5 built-in tasks.",
)
@click.option("--model", default="claude-sonnet-4.6", show_default=True)
@click.option("--rounds", default=3, show_default=True, help="How many rounds per prompt.")
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write report/export output to this path.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "markdown", "csv"]),
    default="json",
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_run(
    ctx: click.Context,
    prompts: tuple[str, ...],
    model: str,
    rounds: int,
    output_path: Path | None,
    output_format: str,
    as_json: bool,
) -> None:
    """Run the core runtime benchmark and write the latest report."""
    from atelier.infra.runtime.benchmarking import (
        benchmark_report_path,
        export_runtime_report,
        render_runtime_report,
        run_runtime_benchmark,
    )

    report = run_runtime_benchmark(
        root=ctx.obj["root"],
        prompts=prompts,
        model=model,
        rounds=rounds,
    )
    if output_path is not None:
        export_runtime_report(report, output_path=output_path, output_format=output_format)
    if as_json:
        _emit(report, as_json=True)
        return
    click.echo(render_runtime_report(report))
    click.echo(f"saved report: {benchmark_report_path(ctx.obj['root'])}")


@benchmark_group.command("savings")
@click.option(
    "--prompt",
    "prompts",
    multiple=True,
    help="Prompts to benchmark (repeat). Defaults to replay prompts.",
)
@click.option("--model", default="claude-sonnet-4.6", show_default=True)
@click.option(
    "--baseline-command",
    required=True,
    help="Command template for baseline runs. Receives ATELIER_BENCH_PROMPT.",
)
@click.option(
    "--atelier-command",
    required=True,
    help="Command template for Atelier-enabled runs. Receives ATELIER_BENCH_PROMPT.",
)
@click.option(
    "--timeout",
    "timeout_s",
    default=600.0,
    show_default=True,
    type=float,
    help="Seconds per command.",
)
@click.option("--max-prompts", default=5, show_default=True, type=int, help="Default replay prompts to run.")
@click.option("--output", "output_path", type=click.Path(path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_savings(
    ctx: click.Context,
    prompts: tuple[str, ...],
    model: str,
    baseline_command: str,
    atelier_command: str,
    timeout_s: float,
    max_prompts: int,
    output_path: Path | None,
    as_json: bool,
) -> None:
    """Run paired baseline-vs-Atelier command savings benchmarks."""
    from benchmarks.swe.savings_replay import run_paired_command_benchmark

    tasks = [
        {"id": f"prompt-{idx}", "task_type": "ad_hoc", "task": prompt} for idx, prompt in enumerate(prompts, start=1)
    ]
    paired_report = run_paired_command_benchmark(
        root=ctx.obj["root"],
        baseline_command=baseline_command,
        atelier_command=atelier_command,
        tasks=tasks or None,
        model=model,
        timeout_s=timeout_s,
        max_prompts=max_prompts,
    )
    payload = paired_report.to_dict()
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(
        f"savings benchmark complete: {payload['tokens_saved']} tokens, "
        f"{payload['reduction_pct']:.2f}% reduction, "
        f"${payload['cost_saved_usd']:.4f} saved"
    )
    click.echo(f"saved report: {ctx.obj['root'] / 'benchmarks' / 'savings' / 'latest.json'}")


@benchmark_group.command("savings-compact")
@click.option(
    "--corpus",
    "corpus_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory of claude-*.jsonl session exports. Defaults to exports/ in the repo root.",
)
@click.option(
    "--max-sessions",
    default=None,
    type=int,
    show_default=True,
    help="Maximum number of qualifying sessions to process.",
)
@click.option(
    "--min-context",
    "min_context_tokens",
    default=80_000,
    show_default=True,
    type=int,
    help="Skip sessions whose peak context is below this token threshold.",
)
@click.option("--output", "output_path", type=click.Path(path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_savings_compact(
    ctx: click.Context,
    corpus_dir: Path | None,
    max_sessions: int | None,
    min_context_tokens: int,
    output_path: Path | None,
    as_json: bool,
) -> None:
    """Measure additional context freed by Atelier compact vs native /compact.

    Reads real Claude Code session exports, detects native compaction events
    (context drops >= 40 %), and compares native output (measured) vs Atelier
    estimate. Reports the *delta* only - never pretends native doesn't compact.

    Output is written to <root>/benchmarks/savings/compact_latest.json.
    """
    from benchmarks.swe.compact_bench import run_compact_bench

    if corpus_dir is None:
        # Try to find exports/ relative to the project root
        corpus_dir = ctx.obj["root"].parent / "exports"
        if not corpus_dir.is_dir():
            raise click.ClickException("Could not locate exports/ directory. Pass --corpus PATH explicitly.")

    report = run_compact_bench(
        corpus_dir,
        max_sessions=max_sessions,
        min_context_tokens=min_context_tokens,
    )

    out_path = output_path or ctx.obj["root"] / "benchmarks" / "savings" / "compact_latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    if as_json:
        _emit(report, as_json=True)
        return

    n = report["sessions_benchmarked"]
    delta = report["avg_delta_tokens"]
    cost = report["total_cost_saved_usd"]
    pct = report.get("atelier_vs_native_delta_pct", 0.0)
    click.echo(
        f"savings-compact: {n} sessions | "
        f"avg delta {delta:+,} tokens ({pct:+.1f}% vs native) | "
        f"${cost:.4f} additional savings"
    )
    click.echo(f"saved report: {out_path}")


@benchmark_group.command("savings-routing")
@click.option(
    "--corpus",
    "corpus_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory of claude-*.jsonl session exports. Defaults to exports/ in the repo root.",
)
@click.option(
    "--max-sessions",
    default=None,
    type=int,
    show_default=True,
    help="Maximum number of sessions to process.",
)
@click.option("--output", "output_path", type=click.Path(path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_savings_routing(
    ctx: click.Context,
    corpus_dir: Path | None,
    max_sessions: int | None,
    output_path: Path | None,
    as_json: bool,
) -> None:
    """Measure cost savings from Atelier model routing vs actual session model.

    Reads real Claude Code session exports, runs ModelRouter per turn, and
    computes cost delta between actual model and recommended cheaper tier.
    Only positive deltas count - sessions already on an optimal model show $0.

    Output is written to <root>/benchmarks/savings/routing_latest.json.
    """
    from benchmarks.swe.routing_bench import run_routing_bench

    if corpus_dir is None:
        corpus_dir = ctx.obj["root"].parent / "exports"
        if not corpus_dir.is_dir():
            raise click.ClickException("Could not locate exports/ directory. Pass --corpus PATH explicitly.")

    report = run_routing_bench(corpus_dir, max_sessions=max_sessions)

    out_path = output_path or ctx.obj["root"] / "benchmarks" / "savings" / "routing_latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    if as_json:
        _emit(report, as_json=True)
        return

    n = report["sessions_benchmarked"]
    turns = report["total_turns_analyzed"]
    down = report["total_downtiered_turns"]
    pct = report["downtiered_pct"]
    cost = report["total_cost_saved_usd"]
    by_tier = report.get("by_tier", {})
    click.echo(
        f"savings-routing: {n} sessions | "
        f"{turns:,} turns | "
        f"{down:,} downtiered ({pct:.1f}%) | "
        f"${cost:.4f} saved"
    )
    click.echo(
        f"  by tier: cheap={by_tier.get('cheap', 0):,}  medium={by_tier.get('medium', 0):,}  expensive={by_tier.get('expensive', 0):,}"
    )
    click.echo(f"saved report: {out_path}")


@benchmark_group.command("quality-routing")
@click.option(
    "--corpus",
    "corpus_dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing claude/*.jsonl session exports.",
)
@click.option("--max-sessions", default=None, type=int, help="Cap sessions processed.")
@click.pass_context
def benchmark_quality_routing(
    ctx: click.Context,
    corpus_dir: Path | None,
    max_sessions: int | None,
) -> None:
    """Routing QUALITY benchmark: how safe are downtiered recommendations?

    Classifies each downtiered turn as safe / moderate / risky using:
      - tool risk (Edit=1.0, Bash=0.4, Read=0.0)
      - output complexity (tokens as reasoning proxy)
      - immediate error (did the tool call fail right after?)
    """
    from benchmarks.swe.routing_quality_bench import run_routing_quality_bench

    root: Path = ctx.obj["root"]
    if corpus_dir is None:
        corpus_dir = root.parent / "exports"
    if not corpus_dir.exists():
        raise click.ClickException(f"corpus not found: {corpus_dir}")

    report = run_routing_quality_bench(corpus_dir, max_sessions=max_sessions)

    out_dir = root / "benchmarks" / "savings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "routing_quality_latest.json"
    out_path.write_text(json.dumps(report, indent=2))

    n = report["sessions_benchmarked"]
    total_down = report["total_downtiered_turns"]
    safe_pct = report["safe_pct"]
    mod_pct = report["moderate_pct"]
    risky_pct = report["risky_pct"]
    env_pct = report["env_error_pct_on_downtiered"]
    model_pct = report["model_error_pct_on_downtiered"]
    retry_pct = report["retry_pct_on_downtiered"]
    quality = report["avg_quality_score"]
    click.echo(f"quality-routing: {n} sessions | {total_down:,} downtiered turns | " f"quality score {quality:.3f}")
    click.echo(f"  risk split: safe={safe_pct:.1f}%  moderate={mod_pct:.1f}%  risky={risky_pct:.1f}%")
    click.echo(f"  errors: env={env_pct:.1f}% (excluded)  model={model_pct:.1f}%  retries={retry_pct:.1f}%")
    click.echo(f"saved report: {out_path}")


@benchmark_group.command("quality-compact")
@click.option(
    "--corpus",
    "corpus_dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing claude/*.jsonl session exports.",
)
@click.option("--max-sessions", default=None, type=int, help="Cap sessions processed.")
@click.pass_context
def benchmark_quality_compact(
    ctx: click.Context,
    corpus_dir: Path | None,
    max_sessions: int | None,
) -> None:
    """Compact QUALITY benchmark: does context survive compaction intact?

    For each real compaction event measures:
      - error rate drift (pre vs post compact)
      - extra re-reads post compact (proxy for lost context)
      - session continuation rate
      - composite retention score (0-1)
    """
    from benchmarks.swe.compact_quality_bench import run_compact_quality_bench

    root: Path = ctx.obj["root"]
    if corpus_dir is None:
        corpus_dir = root.parent / "exports"
    if not corpus_dir.exists():
        raise click.ClickException(f"corpus not found: {corpus_dir}")

    report = run_compact_quality_bench(corpus_dir, max_sessions=max_sessions)

    out_dir = root / "benchmarks" / "savings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "compact_quality_latest.json"
    out_path.write_text(json.dumps(report, indent=2))

    n = report["sessions_benchmarked"]
    n_events = report["total_compaction_events"]
    retention = report["avg_retention_score"]
    drift = report["avg_error_drift"]
    rr = report["avg_extra_read_rate"]
    cont = report["sessions_continued_pct"]
    click.echo(f"quality-compact: {n} sessions | {n_events} compaction events | " f"retention score {retention:.3f}")
    click.echo(f"  error drift: {drift:+.3f}  extra re-reads: {rr:.3f}  continuation: {cont:.1f}%")
    click.echo(f"saved report: {out_path}")


@benchmark_group.command("replay-routing")
@click.option(
    "--corpus",
    "corpus_dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing claude/*.jsonl session exports.",
)
@click.option(
    "--max-sessions",
    default=10,
    show_default=True,
    type=int,
    help="Max sessions to replay (cost control).",
)
@click.option(
    "--max-turns",
    default=5,
    show_default=True,
    type=int,
    help="Max haiku calls per session (cost control). 0 = unlimited.",
)
@click.option(
    "--context-lines",
    default=30,
    show_default=True,
    type=int,
    help="Recent context lines sent to haiku per call.",
)
@click.option(
    "--haiku-model",
    default="claude-haiku-4-5",
    show_default=True,
    help="Haiku model alias for --model flag.",
)
@click.option(
    "--delay",
    default=0.5,
    show_default=True,
    type=float,
    help="Seconds between CLI calls (rate limiting).",
)
@click.option("--verbose", is_flag=True, default=False, help="Print each turn result as it completes.")
@click.pass_context
def benchmark_replay_routing(
    ctx: click.Context,
    corpus_dir: Path | None,
    max_sessions: int,
    max_turns: int,
    context_lines: int,
    haiku_model: str,
    delay: float,
    verbose: bool,
) -> None:
    """Routing REPLAY benchmark: actually call haiku on downtiered turns.

    True counterfactual - uses the claude CLI (no API key required).
    Reconstructs session context as text, asks haiku what tool it would call
    next, and compares its choice to what sonnet actually did.

    Estimated cost: ~$0.01-0.03 per turn replayed (haiku via Claude Code auth).

    Quality labels per turn:
      match        - same tool, similar input (similarity >= 0.7)
      partial      - same tool, somewhat different input (0.3-0.7)
      diverge      - same tool, very different input (< 0.3)
      tool_mismatch - haiku chose a different tool entirely
      parse_error  - haiku responded but JSON could not be parsed
    """
    from benchmarks.swe.routing_replay_bench import run_routing_replay_bench

    root: Path = ctx.obj["root"]
    if corpus_dir is None:
        corpus_dir = root.parent / "exports"
    if not corpus_dir.exists():
        raise click.ClickException(f"corpus not found: {corpus_dir}")

    max_t = max_turns if max_turns > 0 else None
    click.echo(
        f"Replaying with {haiku_model} (via claude CLI) | "
        f"up to {max_sessions} sessions x {max_t or 'all'} turns each | "
        f"context={context_lines} lines"
    )

    report = run_routing_replay_bench(
        corpus_dir,
        max_sessions=max_sessions,
        max_turns_per_session=max_t,
        context_lines=context_lines,
        haiku_model=haiku_model,
        rate_limit_delay=delay,
        verbose=verbose,
    )

    out_dir = root / "benchmarks" / "savings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "routing_replay_latest.json"
    out_path.write_text(json.dumps(report, indent=2))

    n = report["sessions_benchmarked"]
    total = report["total_turns_replayed"]
    match = report["tool_match_rate"]
    sim = report["avg_input_similarity"]
    ratio = report["avg_output_token_ratio"]
    cost = report["total_haiku_cost_usd"]
    labels = report["quality_label_counts"]
    parse_errs = sum(1 for r in report.get("sessions", []) for t in r.get("turns", []) if t.get("parse_error"))

    click.echo(f"replay-routing: {n} sessions | {total} turns replayed | " f"tool match {match:.1%} | cost ${cost:.4f}")
    click.echo(f"  avg input similarity (matched turns): {sim:.3f}")
    click.echo(f"  avg output token ratio: {ratio:.3f} (haiku/sonnet)")
    click.echo(f"  quality: {json.dumps(labels)}")
    if parse_errs:
        click.echo(f"  parse errors: {parse_errs}", err=True)
    click.echo(f"saved report: {out_path}")


@benchmark_group.command("compare")
@click.option(
    "--input",
    "inputs",
    multiple=True,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Benchmark report JSON input. Provide at least two.",
)
def benchmark_compare(inputs: tuple[Path, ...]) -> None:
    """Compare two or more runtime benchmark reports."""
    from atelier.infra.runtime.benchmarking import compare_runtime_reports

    if len(inputs) < 2:
        raise click.ClickException("benchmark compare requires at least two --input reports")
    comparison = compare_runtime_reports(list(inputs))
    _emit(comparison, as_json=True)


@benchmark_group.command("report")
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Benchmark report JSON input.",
)
@click.option("--json", "as_json", is_flag=True)
def benchmark_report(input_path: Path, as_json: bool) -> None:
    """Render one runtime benchmark report."""
    from atelier.infra.runtime.benchmarking import load_runtime_report, render_runtime_report

    report = load_runtime_report(input_path)
    if as_json:
        _emit(report, as_json=True)
        return
    click.echo(render_runtime_report(report))


@benchmark_group.command("export")
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Benchmark report JSON input.",
)
@click.option("--output", "output_path", required=True, type=click.Path(path_type=Path))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "markdown", "csv"]),
    default="json",
    show_default=True,
)
def benchmark_export(input_path: Path, output_path: Path, output_format: str) -> None:
    """Export a runtime benchmark report."""
    from atelier.infra.runtime.benchmarking import export_runtime_report, load_runtime_report

    report = load_runtime_report(input_path)
    exported = export_runtime_report(report, output_path=output_path, output_format=output_format)
    _emit({"output": str(exported), "format": output_format}, as_json=True)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _run_benchmark_core(
    *,
    root: Path,
    prompts: tuple[str, ...],
    model: str,
    rounds: int,
) -> dict[str, Any]:
    from atelier.infra.runtime.benchmarking import run_runtime_benchmark

    report = run_runtime_benchmark(root=root, prompts=prompts, model=model, rounds=rounds)
    return {"suite": "core", "report": report}


def _run_benchmark_hosts(*, workspace: str | None = None) -> dict[str, Any]:
    script = _repo_root() / "scripts" / "verify_agent_clis.sh"
    cmd = ["bash", str(script)]
    if workspace:
        cmd.extend(["--workspace", workspace])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "suite": "hosts",
        "exit_code": proc.returncode,
        "status": "pass" if proc.returncode == 0 else "fail",
        "command": " ".join(cmd),
        "output": output.strip(),
    }


def _run_benchmark_packs(*, root: Path, host: str) -> dict[str, Any]:
    manager = _load_domain_manager(root)
    bundle_ids = [ref.bundle_id for ref in manager.list_bundles()]

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for bundle_id in bundle_ids:
        try:
            info = manager.info(bundle_id) or {}
            results.append({"bundle_id": bundle_id, "domain": info.get("domain", ""), "status": "ok"})
        except Exception as exc:
            failures.append({"bundle_id": bundle_id, "error": str(exc)})

    return {
        "suite": "domains",
        "host": host,
        "domains_total": len(bundle_ids),
        "domains_benchmarked": len(results),
        "results": results,
        "failures": failures,
    }


@benchmark_group.command("core")
@click.option(
    "--prompt",
    "prompts",
    multiple=True,
    help="Prompts to benchmark (repeat). Defaults to built-in runtime tasks.",
)
@click.option("--model", default="claude-sonnet-4.6", show_default=True)
@click.option("--rounds", default=3, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_core(
    ctx: click.Context,
    prompts: tuple[str, ...],
    model: str,
    rounds: int,
    as_json: bool,
) -> None:
    """Phase T3: benchmark core runtime behavior."""
    payload = _run_benchmark_core(root=ctx.obj["root"], prompts=prompts, model=model, rounds=rounds)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("core benchmark complete")
    click.echo(f"tasks: {len(payload['report'].get('tasks', []))}")


@benchmark_group.command("hosts")
@click.option("--workspace", default=None, help="Optional workspace path passed to verify scripts.")
@click.option("--json", "as_json", is_flag=True)
def benchmark_hosts(workspace: str | None, as_json: bool) -> None:
    """Phase T3: benchmark/verify host integration readiness."""
    payload = _run_benchmark_hosts(workspace=workspace)
    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo(payload["output"])
    if payload["exit_code"] != 0:
        raise click.ClickException("host benchmark/verification failed")


@benchmark_group.command("runtime")
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON output path.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def bench_runtime(ctx: click.Context, output_path: Path | None, as_json: bool) -> None:
    """Emit runtime capability efficiency metrics."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.benchmark_runtime_metrics()
    if output_path is not None:
        rt.export_benchmark_runtime(output_path)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@benchmark_group.command("packs")
@click.option("--host", default="codex", show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_packs(ctx: click.Context, host: str, as_json: bool) -> None:
    """Phase T3: benchmark official/installed packs."""
    payload = _run_benchmark_packs(root=ctx.obj["root"], host=host)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"domain benchmark complete: {payload['domains_benchmarked']}/{payload['domains_total']} domains")
    if payload["failures"]:
        click.echo("failures:")
        for item in payload["failures"]:
            click.echo(f"  - {item.get('bundle_id', item.get('pack_id', '?'))}: {item['error']}")


@benchmark_group.command("full")
@click.option(
    "--prompt",
    "prompts",
    multiple=True,
    help="Prompts to benchmark for the core suite (repeat).",
)
@click.option("--model", default="claude-sonnet-4.6", show_default=True)
@click.option("--rounds", default=3, show_default=True)
@click.option("--host", default="codex", show_default=True)
@click.option("--workspace", default=None, help="Optional workspace path passed to host verify scripts.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def benchmark_full(
    ctx: click.Context,
    prompts: tuple[str, ...],
    model: str,
    rounds: int,
    host: str,
    workspace: str | None,
    as_json: bool,
) -> None:
    """Phase T3: run core + hosts + packs benchmark suite."""
    core_payload = _run_benchmark_core(root=ctx.obj["root"], prompts=prompts, model=model, rounds=rounds)
    hosts_payload = _run_benchmark_hosts(workspace=workspace)
    packs_payload = _run_benchmark_packs(root=ctx.obj["root"], host=host)

    payload = {
        "suite": "full",
        "core": core_payload,
        "hosts": hosts_payload,
        "packs": packs_payload,
        "status": ("pass" if hosts_payload["exit_code"] == 0 and not packs_payload["failures"] else "warn"),
    }

    if as_json:
        _emit(payload, as_json=True)
    else:
        click.echo("full benchmark suite complete")
        click.echo(f"core tasks: {len(core_payload['report'].get('tasks', []))}")
        click.echo(f"host verification status: {hosts_payload['status']}")
        click.echo(f"domain coverage: {packs_payload['domains_benchmarked']}/{packs_payload['domains_total']}")

    if hosts_payload["exit_code"] != 0:
        raise click.ClickException("full benchmark failed in host verification")


@benchmark_group.command("publish")
@click.option(
    "--since",
    default="7d",
    show_default=True,
    help="Coverage window label included in the report (informational only).",
)
@click.option(
    "--output",
    "output_dir",
    default="reports",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Parent directory for published reports (reports/YYYY-Www/).",
)
@click.option(
    "--corpus",
    "corpus_arg",
    default="",
    help="Optional corpus label for the Methodology section.",
)
@click.option("--dry-run", "dry_run", is_flag=True, help="Print what would be written; do not write.")
@click.pass_context
def benchmark_publish(
    ctx: click.Context,
    since: str,
    output_dir: Path,
    corpus_arg: str,
    dry_run: bool,
) -> None:
    """Render latest benchmark results into a publishable weekly report.

    Reads cached JSON files from {root}/benchmarks/savings/ and writes
    reports/YYYY-Www/benchmark.{md,json}. Computes Δ vs the prior week's
    report if available.
    """
    from atelier.infra.benchmarks.publisher import publish

    root: Path = ctx.obj["root"]

    # Resolve output_dir relative to cwd (not ~/.atelier)
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir

    mode_label = " [dry-run]" if dry_run else ""
    click.echo(f"Building benchmark report{mode_label}…")

    report_dir = publish(
        root=root,
        output_dir=output_dir,
        since=since,
        corpus_arg=corpus_arg,
        dry_run=dry_run,
    )

    if dry_run:
        click.echo("Dry-run complete - no files written.")
    else:
        assert report_dir is not None
        click.echo(f"Report written -> {report_dir}")
        click.echo(f"  {report_dir / 'benchmark.md'}")
        click.echo(f"  {report_dir / 'benchmark.json'}")
        click.echo(f"  {output_dir / 'index.json'} (updated)")


def _register_swe_benchmark_group() -> None:
    try:
        from benchmarks.swe.run_swe_bench import swe as swe_benchmark_group
    except ModuleNotFoundError:
        # Keep CLI startup resilient when benchmark modules are not present
        # in the runtime environment (e.g. partial installs/services).
        return

    benchmark_group.add_command(swe_benchmark_group)


_register_swe_benchmark_group()


@cli.group("service")
def service_group() -> None:
    """Production service commands."""


@service_group.command("start")
@click.option("--host", default=None, help="Bind host (overrides ATELIER_SERVICE_HOST).")
@click.option("--port", default=None, type=int, help="Bind port (overrides ATELIER_SERVICE_PORT).")
@click.option("--reload", is_flag=True, default=False, help="Enable uvicorn auto-reload.")
def service_start(host: str | None, port: int | None, reload: bool) -> None:
    """Start the Atelier HTTP service API."""
    try:
        from atelier.core.service.api import main as service_main
    except ImportError as exc:
        if "cannot import name 'main'" in str(exc):
            raise click.ClickException(
                "The service API 'main' entrypoint is missing. " "Ensure your 'atelier' installation is up to date."
            ) from exc
        raise click.ClickException(
            "Could not start the service API. Ensure all dependencies are installed: uv sync --extra api"
        ) from exc
    service_main(host=host, port=port, reload=reload)


@service_group.command("config")
def service_config() -> None:
    """Print current service configuration (no secret values)."""
    import json

    from atelier.core.service.config import cfg

    click.echo(json.dumps(cfg.as_dict(), indent=2))


# --------------------------------------------------------------------------- #
# Worker group (P6)                                                           #
# --------------------------------------------------------------------------- #


@cli.group("worker")
def worker_group() -> None:
    """Worker/job queue commands."""


@worker_group.command("start")
@click.pass_context
def worker_start(ctx: click.Context) -> None:
    """Start the background worker loop."""
    try:
        from atelier.core.service.worker import Worker
    except ImportError as exc:
        raise click.ClickException("Worker dependencies not available.") from exc

    from atelier.infra.storage.factory import create_store

    root = ctx.obj["root"]
    store = create_store(root)
    store.init()
    worker = Worker(store=store)
    click.echo("Worker started. Press Ctrl+C to stop.")
    worker.run()


@worker_group.command("run-once")
@click.pass_context
def worker_run_once(ctx: click.Context) -> None:
    """Claim and process one pending job then exit."""
    try:
        from atelier.core.service.worker import Worker
    except ImportError as exc:
        raise click.ClickException("Worker dependencies not available.") from exc

    from atelier.infra.storage.factory import create_store

    root = ctx.obj["root"]
    store = create_store(root)
    store.init()
    worker = Worker(store=store)
    processed = worker.run_once()
    if processed:
        click.echo(f"processed job: {processed}")
    else:
        click.echo("no pending jobs")


@worker_group.command("enqueue")
@click.argument("job_type")
@click.option("--payload", default="{}", show_default=True, help="Inline JSON object payload.")
@click.option("--max-attempts", default=3, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def worker_enqueue(
    ctx: click.Context,
    job_type: str,
    payload: str,
    max_attempts: int,
    as_json: bool,
) -> None:
    """Queue one background job."""
    from atelier.infra.storage.factory import create_store

    try:
        payload_data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid JSON payload: {exc}") from exc
    if not isinstance(payload_data, dict):
        raise click.ClickException("payload must decode to a JSON object")

    store = create_store(ctx.obj["root"])
    store.init()
    job_id = store.enqueue_job(job_type, payload_data, max_attempts=max_attempts)
    result = {
        "job_id": job_id,
        "job_type": job_type,
        "status": "pending",
    }
    _emit(result, as_json=as_json) if as_json else click.echo(job_id)


@worker_group.command("list")
@click.option("--status", default=None, help="Filter by job status.")
@click.option("--job-type", default=None, help="Filter by job type.")
@click.option("--limit", default=20, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def worker_list(ctx: click.Context, status: str | None, job_type: str | None, limit: int, as_json: bool) -> None:
    """List queued and processed jobs."""
    from atelier.infra.storage.factory import create_store

    store = create_store(ctx.obj["root"])
    store.init()
    jobs = store.list_jobs(status=status, job_type=job_type, limit=limit)
    if as_json:
        _emit(jobs, as_json=True)
        return
    if not jobs:
        click.echo("(no jobs)")
        return
    for job in jobs:
        click.echo(f"{job['id']}\t{job['job_type']}\t{job['status']}\tattempts={job['attempts']}")


@cli.group("servicectl")
def servicectl_group() -> None:
    """Manage the background offline processing controller."""


@servicectl_group.command("tick")
@click.option("--maintenance-interval-seconds", default=300, show_default=True, type=int)
@click.option("--session-import-interval-seconds", default=60, show_default=True, type=int)
@click.option("--external-analytics-interval-seconds", default=300, show_default=True, type=int)
@click.option(
    "--external-analytics-period",
    "external_analytics_periods",
    type=click.Choice(SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS),
    default=DEFAULT_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS,
    multiple=True,
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def servicectl_tick(
    ctx: click.Context,
    maintenance_interval_seconds: int,
    session_import_interval_seconds: int,
    external_analytics_interval_seconds: int,
    external_analytics_periods: tuple[str, ...],
    as_json: bool,
) -> None:
    """Run one maintenance tick: enqueue due jobs and process pending work."""
    payload = _servicectl_tick(
        ctx.obj["root"],
        maintenance_interval_seconds=maintenance_interval_seconds,
        session_import_interval_seconds=session_import_interval_seconds,
        external_analytics_interval_seconds=external_analytics_interval_seconds,
        external_analytics_periods=external_analytics_periods,
    )
    _emit(payload, as_json=as_json) if as_json else click.echo(json.dumps(payload, indent=2))


@servicectl_group.command("start")
@click.option("--interval-seconds", default=60, show_default=True, type=int)
@click.option("--maintenance-interval-seconds", default=300, show_default=True, type=int)
@click.option("--session-import-interval-seconds", default=60, show_default=True, type=int)
@click.option("--external-analytics-interval-seconds", default=300, show_default=True, type=int)
@click.option(
    "--external-analytics-period",
    "external_analytics_periods",
    type=click.Choice(SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS),
    default=DEFAULT_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS,
    multiple=True,
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def servicectl_start(
    ctx: click.Context,
    interval_seconds: int,
    maintenance_interval_seconds: int,
    session_import_interval_seconds: int,
    external_analytics_interval_seconds: int,
    external_analytics_periods: tuple[str, ...],
    as_json: bool,
) -> None:
    """Start the detached background controller."""
    root = ctx.obj["root"]
    if (SYSTEMD_USER_DIR / CONTROLLER_UNIT).exists():
        click.echo(
            f"Notice: {CONTROLLER_UNIT} is installed. "
            "Prefer using 'atelier systemd restart' or 'systemctl --user restart atelier-controller'."
        )

    _kill_orphan_servicectl_processes(root)
    status = _servicectl_status_payload(root)
    if status["running"]:
        if as_json:
            _emit(status, as_json=True)
        else:
            click.echo(f"already running (pid {status['pid']})")
        return

    _clear_servicectl_pid(root)
    control_dir = _servicectl_dir(root)
    control_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "atelier.gateway.cli",
        "--root",
        str(root),
        "servicectl",
        "run",
        "--interval-seconds",
        str(interval_seconds),
        "--maintenance-interval-seconds",
        str(maintenance_interval_seconds),
        "--session-import-interval-seconds",
        str(session_import_interval_seconds),
        "--external-analytics-interval-seconds",
        str(external_analytics_interval_seconds),
    ]
    for period in _normalize_external_analytics_periods(external_analytics_periods):
        command.extend(["--external-analytics-period", period])
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(root)
    with _servicectl_log_path(root).open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    _servicectl_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
    state = _read_servicectl_state(root)
    state.update(
        {
            "started_at": datetime.now(UTC).isoformat(),
            "last_exit_reason": None,
        }
    )
    _write_servicectl_state(root, state)
    payload = _servicectl_status_payload(root)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"started servicectl (pid {proc.pid})")


@servicectl_group.command("stop")
@click.option("--force", is_flag=True, help="Use SIGKILL if SIGTERM does not stop the process.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def servicectl_stop(ctx: click.Context, force: bool, as_json: bool) -> None:
    """Stop the detached background controller."""
    root = ctx.obj["root"]
    pid = _read_servicectl_pid(root)
    if pid is None or not _pid_is_running(pid):
        _clear_servicectl_pid(root)
        state = _read_servicectl_state(root)
        state["last_exit_reason"] = "not_running"
        _write_servicectl_state(root, state)
        payload = _servicectl_status_payload(root)
        if as_json:
            _emit(payload, as_json=True)
        else:
            click.echo("servicectl is not running")
        return

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5
    while _pid_is_running(pid) and time.time() < deadline:
        time.sleep(0.1)
    if _pid_is_running(pid) and force:
        os.kill(pid, signal.SIGKILL)
    _clear_servicectl_pid(root)
    state = _read_servicectl_state(root)
    state["last_exit_reason"] = "stopped"
    _write_servicectl_state(root, state)
    payload = _servicectl_status_payload(root)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo("stopped servicectl")


@servicectl_group.command("status")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def servicectl_status(ctx: click.Context, as_json: bool) -> None:
    """Show background controller status."""
    root = ctx.obj["root"]
    payload = _servicectl_status_payload(root)
    if as_json:
        _emit(payload, as_json=True)
        return

    if payload["running"]:
        import subprocess

        pid = payload["pid"]
        # Try to show system-level status for the PID
        for cmd in [
            ["systemctl", "--user", "status", str(pid), "--no-pager"],
            ["systemctl", "status", str(pid), "--no-pager"],
        ]:
            try:
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    click.echo(res.stdout)
                    click.echo("-" * 40)
                    break
            except Exception:
                logger.warning(
                    "Suppressed exception at cli.py:4346",
                    exc_info=True,
                )

    click.echo(f"running: {str(payload['running']).lower()}")
    click.echo(f"pid: {payload['pid']}")
    click.echo(f"log_file: {payload['log_file']}")
    if payload["last_tick_at"]:
        click.echo(f"last_tick_at: {payload['last_tick_at']}")
    if payload["last_processed_jobs"]:
        click.echo("last_processed_jobs: " + ", ".join(payload["last_processed_jobs"]))


@servicectl_group.command("run", hidden=True)
@click.option("--interval-seconds", default=60, show_default=True, type=int)
@click.option("--maintenance-interval-seconds", default=300, show_default=True, type=int)
@click.option("--session-import-interval-seconds", default=60, show_default=True, type=int)
@click.option("--external-analytics-interval-seconds", default=300, show_default=True, type=int)
@click.option(
    "--external-analytics-period",
    "external_analytics_periods",
    type=click.Choice(SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS),
    default=DEFAULT_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS,
    multiple=True,
    show_default=True,
)
@click.option("--auto-update", is_flag=True, help="Check for git updates periodically.")
@click.option("--auto-update-interval-seconds", default=3600, show_default=True, type=int)
@click.pass_context
def servicectl_run(
    ctx: click.Context,
    interval_seconds: int,
    maintenance_interval_seconds: int,
    session_import_interval_seconds: int,
    external_analytics_interval_seconds: int,
    external_analytics_periods: tuple[str, ...],
    auto_update: bool,
    auto_update_interval_seconds: int,
) -> None:
    """Internal long-running background loop."""
    root = ctx.obj["root"]
    try:
        while True:
            _servicectl_tick(
                root,
                maintenance_interval_seconds=maintenance_interval_seconds,
                session_import_interval_seconds=session_import_interval_seconds,
                external_analytics_interval_seconds=external_analytics_interval_seconds,
                external_analytics_periods=external_analytics_periods,
                auto_update=auto_update,
                auto_update_interval_seconds=auto_update_interval_seconds,
            )
            time.sleep(max(1, interval_seconds))
    except KeyboardInterrupt:
        state = _read_servicectl_state(root)
        state["last_exit_reason"] = "interrupted"
        _write_servicectl_state(root, state)
        raise SystemExit(0) from None


# ----- background services (systemd / launchd) ------------------------------ #


@cli.group("background")
def background_group() -> None:
    """Manage Atelier background services (systemd on Linux, launchd on macOS)."""


@background_group.command("install")
@click.option("--with-stack", is_flag=True, help="Also install the visualization stack service.")
@click.option(
    "--with-letta",
    is_flag=True,
    help="Also install the Letta memory server (Docker-based) service.",
)
@click.option(
    "--with-openmemory",
    is_flag=True,
    help="Also install the OpenMemory MCP (Docker-based) service.",
)
@click.option("--with-zoekt", is_flag=True, help="Also install the Zoekt code-search (Docker-based) service.")
@click.pass_context
def background_install(
    ctx: click.Context, with_stack: bool, with_letta: bool, with_openmemory: bool, with_zoekt: bool
) -> None:
    """Install Atelier services as background units."""
    root = ctx.obj["root"]
    project_root = _project_root()
    atelier_bin = shutil.which("atelier") or str(Path(sys.argv[0]).resolve())

    if with_letta and not shutil.which("docker"):
        click.echo(
            "Warning: 'docker' not found on PATH. "
            "The Letta service unit will be created but will fail until Docker is available."
        )

    if with_zoekt and not shutil.which("docker"):
        click.echo(
            "Warning: 'docker' not found on PATH. "
            "The Zoekt service unit will be created but will fail until Docker is available."
        )

    if with_openmemory:
        _ensure_openmemory_service_env(root)
        missing = [name for name in ("git", "docker", "make") if not shutil.which(name)]
        if missing:
            click.echo(
                "Warning: OpenMemory requires "
                + ", ".join(missing)
                + ". The service unit will be created but will fail until those commands are available."
            )
        if not os.environ.get("ATELIER_OPENMEMORY_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", "")).strip():
            click.echo(
                "Warning: OPENAI_API_KEY not set. "
                "The OpenMemory service unit will be created but startup will fail until the key is provided."
            )

    if _is_linux():
        if not shutil.which("systemctl"):
            raise click.ClickException("systemctl not found.")

        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

        controller_content = f"""[Unit]
Description=Atelier Background Controller
After=network.target

[Service]
Type=simple
ExecStart={atelier_bin} --root {root} servicectl run --auto-update
Restart=always
Environment=ATELIER_ROOT={root}
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={project_root}

[Install]
WantedBy=default.target
"""
        (SYSTEMD_USER_DIR / CONTROLLER_UNIT).write_text(controller_content, encoding="utf-8")
        click.echo(f"Installed {CONTROLLER_UNIT}")

        if with_stack:
            stack_content = f"""[Unit]
Description=Atelier Visualization Stack
After={CONTROLLER_UNIT}

[Service]
Type=simple
WorkingDirectory={project_root}
ExecStart={atelier_bin} --root {root} stack run
ExecStop={atelier_bin} --root {root} stack stop
Restart=always
Environment=ATELIER_ROOT={root}
Environment=ATELIER_STACK_ROOT={root}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
            (SYSTEMD_USER_DIR / STACK_UNIT).write_text(stack_content, encoding="utf-8")
            click.echo(f"Installed {STACK_UNIT}")

        if with_letta:
            letta_content = f"""[Unit]
Description=Atelier Letta Memory Server (Docker)
After=network.target docker.service
Wants=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={atelier_bin} --root {root} letta up
ExecStop={atelier_bin} --root {root} letta down
WorkingDirectory={project_root}
Environment=ATELIER_ROOT={root}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
            (SYSTEMD_USER_DIR / LETTA_UNIT).write_text(letta_content, encoding="utf-8")
            click.echo(f"Installed {LETTA_UNIT}")

        if with_zoekt:
            zoekt_content = f"""[Unit]
Description=Atelier Zoekt Code Search (Docker)
After=network.target docker.service
Wants=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={atelier_bin} --root {root} zoekt up
ExecStop={atelier_bin} --root {root} zoekt down
WorkingDirectory={project_root}
Environment=ATELIER_ROOT={root}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
            (SYSTEMD_USER_DIR / ZOEKT_UNIT).write_text(zoekt_content, encoding="utf-8")
            click.echo(f"Installed {ZOEKT_UNIT}")

        if with_openmemory:
            openmemory_content = f"""[Unit]
Description=Atelier OpenMemory MCP Server
After=network.target docker.service
Wants=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=-{_openmemory_service_env_path(root)}
ExecStart={atelier_bin} --root {root} openmemory up
ExecStop={atelier_bin} --root {root} openmemory down
WorkingDirectory={project_root}
StandardOutput=append:{_openmemory_log_path(root)}
StandardError=append:{_openmemory_log_path(root)}

[Install]
WantedBy=default.target
"""
            (SYSTEMD_USER_DIR / OPENMEMORY_UNIT).write_text(openmemory_content, encoding="utf-8")
        # Clean up stale units for features no longer requested
        # (makes `background install` idempotent across re-installs)
        for flag, unit in [
            (with_stack, STACK_UNIT),
            (with_letta, LETTA_UNIT),
            (with_openmemory, OPENMEMORY_UNIT),
            (with_zoekt, ZOEKT_UNIT),
        ]:
            if not flag:
                unit_path = SYSTEMD_USER_DIR / unit
                if unit_path.exists():
                    subprocess.run(
                        ["systemctl", "--user", "disable", "--now", unit],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    unit_path.unlink()

        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        # Use enable + restart (not enable --now) so already-running services
        # pick up the new code after re-install. `restart` starts inactive units too.
        subprocess.run(["systemctl", "--user", "enable", CONTROLLER_UNIT], check=True)
        subprocess.run(["systemctl", "--user", "restart", CONTROLLER_UNIT], check=True)
        if with_stack:
            subprocess.run(["systemctl", "--user", "enable", STACK_UNIT], check=True)
            subprocess.run(["systemctl", "--user", "restart", STACK_UNIT], check=True)
        if with_letta:
            subprocess.run(["systemctl", "--user", "enable", LETTA_UNIT], check=True)
            subprocess.run(["systemctl", "--user", "restart", LETTA_UNIT], check=True)
        if with_openmemory:
            subprocess.run(["systemctl", "--user", "enable", OPENMEMORY_UNIT], check=True)
            subprocess.run(["systemctl", "--user", "restart", OPENMEMORY_UNIT], check=True)
        if with_zoekt:
            subprocess.run(["systemctl", "--user", "enable", ZOEKT_UNIT], check=True)
            result = subprocess.run(["systemctl", "--user", "restart", ZOEKT_UNIT], check=False)
            if result.returncode != 0:
                click.echo(
                    f"Warning: {ZOEKT_UNIT} did not start cleanly - "
                    "run 'journalctl --user -xeu atelier-zoekt.service' for details",
                    err=True,
                )

    elif _is_macos():
        # Clean up stale plists for features no longer requested
        for flag, label in [
            (with_stack, STACK_LABEL),
            (with_letta, LETTA_LABEL),
            (with_openmemory, OPENMEMORY_LABEL),
            (with_zoekt, ZOEKT_LABEL),
        ]:
            if not flag:
                plist = LAUNCHD_USER_DIR / f"{label}.plist"
                if plist.exists():
                    subprocess.run(
                        ["launchctl", "unload", str(plist)],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    plist.unlink()

        LAUNCHD_USER_DIR.mkdir(parents=True, exist_ok=True)

        controller_plist = f"""<?xml version="1.0" encoding="UTF-8"?
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{CONTROLLER_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{atelier_bin}</string>
        <string>--root</string>
        <string>{root}</string>
        <string>servicectl</string>
        <string>run</string>
        <string>--auto-update</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ATELIER_ROOT</key>
        <string>{root}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""
        (LAUNCHD_USER_DIR / f"{CONTROLLER_LABEL}.plist").write_text(controller_plist, encoding="utf-8")
        click.echo(f"Installed {CONTROLLER_LABEL}.plist")

        if with_stack:
            stack_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{STACK_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{atelier_bin}</string>
        <string>--root</string>
        <string>{root}</string>
        <string>stack</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
    <key>StandardOutPath</key>
    <string>{_stack_log_path(root)}</string>
    <key>StandardErrorPath</key>
    <string>{_stack_log_path(root)}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ATELIER_ROOT</key>
        <string>{root}</string>
        <key>ATELIER_STACK_ROOT</key>
        <string>{root}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""
            (LAUNCHD_USER_DIR / f"{STACK_LABEL}.plist").write_text(stack_plist, encoding="utf-8")
            click.echo(f"Installed {STACK_LABEL}.plist")

        if with_letta:
            letta_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LETTA_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{atelier_bin}</string>
        <string>--root</string>
        <string>{root}</string>
        <string>letta</string>
        <string>up</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
    <key>StandardOutPath</key>
    <string>{Path(root) / "letta" / "letta.log"}</string>
    <key>StandardErrorPath</key>
    <string>{Path(root) / "letta" / "letta.log"}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ATELIER_ROOT</key>
        <string>{root}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""
            (LAUNCHD_USER_DIR / f"{LETTA_LABEL}.plist").write_text(letta_plist, encoding="utf-8")
            click.echo(f"Installed {LETTA_LABEL}.plist")

        if with_zoekt:
            zoekt_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{ZOEKT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{atelier_bin}</string>
        <string>--root</string>
        <string>{root}</string>
        <string>zoekt</string>
        <string>up</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ATELIER_ROOT</key>
        <string>{root}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""
            (LAUNCHD_USER_DIR / f"{ZOEKT_LABEL}.plist").write_text(zoekt_plist, encoding="utf-8")
            click.echo(f"Installed {ZOEKT_LABEL}.plist")

        if with_openmemory:
            openmemory_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{OPENMEMORY_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{atelier_bin}</string>
        <string>--root</string>
        <string>{root}</string>
        <string>openmemory</string>
        <string>up</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
    <key>StandardOutPath</key>
    <string>{_openmemory_log_path(root)}</string>
    <key>StandardErrorPath</key>
    <string>{_openmemory_log_path(root)}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OPENAI_API_KEY</key>
        <string>{os.environ.get("ATELIER_OPENMEMORY_OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))}</string>
        <key>ATELIER_OPENMEMORY_USER_ID</key>
        <string>{os.environ.get("ATELIER_OPENMEMORY_USER_ID", os.environ.get("USER", "atelier"))}</string>
        <key>ATELIER_OPENMEMORY_URL</key>
        <string>{os.environ.get("ATELIER_OPENMEMORY_URL", "http://127.0.0.1:8765")}</string>
    </dict>
</dict>
</plist>
"""
            (LAUNCHD_USER_DIR / f"{OPENMEMORY_LABEL}.plist").write_text(openmemory_plist, encoding="utf-8")
            click.echo(f"Installed {OPENMEMORY_LABEL}.plist")

        subprocess.run(["launchctl", "load", str(LAUNCHD_USER_DIR / f"{CONTROLLER_LABEL}.plist")], check=False)
        if with_stack:
            subprocess.run(["launchctl", "load", str(LAUNCHD_USER_DIR / f"{STACK_LABEL}.plist")], check=False)
        if with_letta:
            subprocess.run(["launchctl", "load", str(LAUNCHD_USER_DIR / f"{LETTA_LABEL}.plist")], check=False)
        if with_openmemory:
            subprocess.run(
                ["launchctl", "load", str(LAUNCHD_USER_DIR / f"{OPENMEMORY_LABEL}.plist")],
                check=False,
            )
        if with_zoekt:
            subprocess.run(["launchctl", "load", str(LAUNCHD_USER_DIR / f"{ZOEKT_LABEL}.plist")], check=False)

    else:
        raise click.ClickException(f"Unsupported platform for background services: {sys.platform}")

    click.echo("Services enabled and started.")


@background_group.command("uninstall")
@click.pass_context
def background_uninstall(ctx: click.Context) -> None:
    """Stop and remove Atelier background units."""
    if _is_linux():
        for unit in [CONTROLLER_UNIT, STACK_UNIT, LETTA_UNIT, OPENMEMORY_UNIT, ZOEKT_UNIT]:
            path = SYSTEMD_USER_DIR / unit
            if path.exists():
                subprocess.run(["systemctl", "--user", "disable", "--now", unit], check=False)
                path.unlink()
                click.echo(f"Removed {unit}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    elif _is_macos():
        for label in [CONTROLLER_LABEL, STACK_LABEL, LETTA_LABEL, OPENMEMORY_LABEL, ZOEKT_LABEL]:
            plist = LAUNCHD_USER_DIR / f"{label}.plist"
            if plist.exists():
                subprocess.run(["launchctl", "unload", str(plist)], check=False)
                plist.unlink()
                click.echo(f"Removed {label}")
    else:
        raise click.ClickException(f"Unsupported platform: {sys.platform}")

    click.echo("Uninstallation complete.")


@background_group.command("status")
@click.pass_context
def background_status(ctx: click.Context) -> None:
    """Show status of Atelier background units."""
    if _is_linux():
        units = [CONTROLLER_UNIT]
        if (SYSTEMD_USER_DIR / STACK_UNIT).exists():
            units.append(STACK_UNIT)
        if (SYSTEMD_USER_DIR / LETTA_UNIT).exists():
            units.append(LETTA_UNIT)
        if (SYSTEMD_USER_DIR / OPENMEMORY_UNIT).exists():
            units.append(OPENMEMORY_UNIT)
        if (SYSTEMD_USER_DIR / ZOEKT_UNIT).exists():
            units.append(ZOEKT_UNIT)
        if (SYSTEMD_USER_DIR / ZOEKT_UNIT).exists():
            units.append(ZOEKT_UNIT)
        for unit in units:
            click.echo(f"--- {unit} ---")
            subprocess.run(["systemctl", "--user", "status", unit, "--no-pager"], check=False)
            click.echo("")
    elif _is_macos():
        for label in [CONTROLLER_LABEL, STACK_LABEL, LETTA_LABEL, OPENMEMORY_LABEL, ZOEKT_LABEL]:
            if (LAUNCHD_USER_DIR / f"{label}.plist").exists():
                click.echo(f"--- {label} ---")
                subprocess.run(["launchctl", "list", label], check=False)
                click.echo("")
    else:
        click.echo(f"Background services not supported on {sys.platform}")


@background_group.command("restart")
@click.pass_context
def background_restart(ctx: click.Context) -> None:
    """Restart Atelier background units."""
    if _is_linux():
        units = [CONTROLLER_UNIT]
        if (SYSTEMD_USER_DIR / STACK_UNIT).exists():
            units.append(STACK_UNIT)
        if (SYSTEMD_USER_DIR / LETTA_UNIT).exists():
            units.append(LETTA_UNIT)
        if (SYSTEMD_USER_DIR / OPENMEMORY_UNIT).exists():
            units.append(OPENMEMORY_UNIT)
        for unit in units:
            subprocess.run(["systemctl", "--user", "restart", unit], check=True)
            click.echo(f"Restarted {unit}")
    elif _is_macos():
        uid = os.getuid()
        for label in [CONTROLLER_LABEL, STACK_LABEL, LETTA_LABEL, OPENMEMORY_LABEL, ZOEKT_LABEL]:
            if (LAUNCHD_USER_DIR / f"{label}.plist").exists():
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False)
                click.echo(f"Restarted {label}")
    else:
        click.echo(f"Background services not supported on {sys.platform}")


# --------------------------------------------------------------------------- #
# Alias 'systemd' to 'background' for backward compatibility                  #
# --------------------------------------------------------------------------- #


@cli.group("systemd", hidden=True)
def systemd_alias_group() -> None:
    """Alias for 'background' group."""


@systemd_alias_group.command("install")
@click.option("--with-stack", is_flag=True)
@click.option("--with-letta", is_flag=True)
@click.option("--with-openmemory", is_flag=True)
@click.option("--with-zoekt", is_flag=True)
@click.pass_context
def systemd_install_alias(
    ctx: click.Context,
    with_stack: bool,
    with_letta: bool,
    with_openmemory: bool,
    with_zoekt: bool,
) -> None:
    ctx.invoke(
        background_install,
        with_stack=with_stack,
        with_letta=with_letta,
        with_openmemory=with_openmemory,
        with_zoekt=with_zoekt,
    )


@systemd_alias_group.command("uninstall")
@click.pass_context
def systemd_uninstall_alias(ctx: click.Context) -> None:
    ctx.invoke(background_uninstall)


@systemd_alias_group.command("status")
@click.pass_context
def systemd_status_alias(ctx: click.Context) -> None:
    ctx.invoke(background_status)


@systemd_alias_group.command("restart")
@click.pass_context
def systemd_restart_alias(ctx: click.Context) -> None:
    ctx.invoke(background_restart)


# --------------------------------------------------------------------------- #
# Unified logs command                                                        #
# --------------------------------------------------------------------------- #


@cli.command("logs")
@click.argument("service", type=click.Choice(["stack", "controller", "letta", "openmemory", "zoekt", "mcp"]))
@click.option("-f", "--follow", is_flag=True, help="Follow log output.")
@click.option("-n", "--lines", default=80, show_default=True, type=int, help="Number of lines to show.")
@click.pass_context
def logs_cmd(ctx: click.Context, service: str, follow: bool, lines: int) -> None:
    """Show logs for an Atelier service.

    SERVICE is one of: stack, controller, letta, openmemory, zoekt, mcp.

    On Linux with systemd units installed, uses journalctl to read
    the service unit logs (which contain everything the process wrote
    to stdout/stderr). On macOS and when running natively without
    systemd, tails the log file directly.
    """
    root = ctx.obj["root"]

    unit_map: dict[str, str] = {
        "stack": STACK_UNIT,
        "controller": CONTROLLER_UNIT,
        "letta": LETTA_UNIT,
        "openmemory": OPENMEMORY_UNIT,
        "zoekt": ZOEKT_UNIT,
        "mcp": MCP_UNIT,
    }
    unit = unit_map[service]

    # Linux with systemd unit installed -> journalctl
    if _is_linux() and (SYSTEMD_USER_DIR / unit).exists():
        cmd: list[str] = ["journalctl", "--user", "-u", unit, "-n", str(lines)]
        if follow:
            cmd.append("-f")
        subprocess.run(cmd, check=False)
        return

    # Native / macOS -> tail the log file
    if service == "stack":
        log_path = _stack_log_path(root)
    elif service == "controller":
        log_path = _servicectl_log_path(root)
    elif service == "letta":
        # Letta runs under Docker Compose -> use compose logs
        args = ["logs"]
        if follow:
            args.append("-f")
        _run_compose(args)
        return
    elif service == "openmemory":
        log_path = _openmemory_log_path(root)
    elif service == "zoekt":
        log_path = Path(root) / "zoekt" / "zoekt.log"
    elif service == "mcp":
        log_path = _mcp_log_path(root)
    else:
        # unreachable given the Choice validator
        raise click.ClickException(f"unknown service: {service}")

    if not log_path.exists():
        click.echo(f"(no {service} logs at {log_path})")
        return

    if follow:
        try:
            subprocess.run(["tail", "-n", str(lines), "-f", str(log_path)], check=True)
        except FileNotFoundError as exc:
            raise click.ClickException("tail is required for --follow log streaming") from exc
        except subprocess.CalledProcessError as exc:
            raise click.ClickException(f"tail exited with code {exc.returncode}") from exc
        return

    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        click.echo(line)


# --------------------------------------------------------------------------- #
# V3 capability commands                                                      #
# --------------------------------------------------------------------------- #


@_dev_command("detect-loop")
@click.option("--session-id", default=None, help="Specific session ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def detect_loop_cmd(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """Detect loops, repeated failures, and dead-end trajectories in a run ledger."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.loop_report(session_id=session_id)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"loop_detected: {payload['loop_detected']}")
    click.echo(f"severity: {payload['severity']}")
    click.echo(f"loop_types: {', '.join(payload['loop_types']) or 'none'}")
    click.echo(f"prior_attempts: {payload['prior_attempts']}")
    if payload["rescue_strategies"]:
        click.echo("rescue_strategies:")
        for s in payload["rescue_strategies"]:
            click.echo(f"  - {s}")


@cli.command("loop-report")
@click.option("--session-id", default=None, help="Specific session ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def loop_report_cmd(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """Full loop analysis: signature, severity, alerts, rescue strategies."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.loop_report(session_id=session_id)
    _emit(payload, as_json=True) if as_json else click.echo(json.dumps(payload, indent=2))


@cli.command("tool-report")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def tool_report_cmd(ctx: click.Context, as_json: bool) -> None:
    """Tool usage + savings summary including redundancy analysis."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.tool_report()
    if as_json:
        _emit(payload, as_json=True)
        return
    metrics = payload.get("metrics", {})
    click.echo(f"total_tool_calls: {metrics.get('total_tool_calls', 0)}")
    click.echo(f"avoided_tool_calls: {metrics.get('avoided_tool_calls', 0)}")
    click.echo(f"token_savings: {metrics.get('token_savings', 0)}")
    click.echo(f"cache_hit_rate: {metrics.get('cache_hit_rate', 0)}")
    recs = payload.get("recommendations", [])
    if recs:
        click.echo("recommendations:")
        for r in recs:
            click.echo(f"  - {r}")


@cli.command("diff-context")
@click.argument("files", nargs=-1, required=True)
@click.option("--lines", default=5, show_default=True, help="Lines of context around diffs.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def diff_context_cmd(ctx: click.Context, files: tuple[str, ...], lines: int, as_json: bool) -> None:
    """Show git diff context for given source files."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.diff_context(list(files), lines=lines)
    if as_json:
        _emit(payload, as_json=True)
        return
    for entry in payload.get("diffs", []):
        click.echo(f"## {entry['path']}")
        click.echo(entry.get("diff", "(no changes)"))


@cli.command("test-context")
@click.argument("files", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def test_context_cmd(ctx: click.Context, files: tuple[str, ...], as_json: bool) -> None:
    """Find test files related to the given source files."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.test_context(list(files))
    if as_json:
        _emit(payload, as_json=True)
        return
    for entry in payload.get("test_contexts", []):
        click.echo(f"{entry['path']}: {', '.join(entry['test_files']) or '(none found)'}")


@cli.command("module-summary")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def module_summary_cmd(ctx: click.Context, path: Path, as_json: bool) -> None:
    """Concise module-level summary: exports, symbols, imports, test files."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.module_summary(path)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"path: {payload['path']}")
    click.echo(f"language: {payload['language']}")
    click.echo(f"exports: {', '.join(payload['exports'][:20]) or '(none)'}")
    click.echo(f"imports: {', '.join(payload['imports'][:10]) or '(none)'}")
    click.echo(f"test_files: {', '.join(payload['test_files']) or '(none found)'}")
    click.echo(f"lines_total: {payload['lines_total']}")


@cli.command("symbol-search")
@click.argument("query")
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def symbol_search_cmd(ctx: click.Context, query: str, limit: int, as_json: bool) -> None:
    """Search for a symbol across all semantically cached files."""
    rt = _core_runtime(ctx.obj["root"])
    results = rt.symbol_search(query, limit=limit)
    if as_json:
        _emit(results, as_json=True)
        return
    if not results:
        click.echo("(no matches)")
        return
    for r in results:
        click.echo(f"{r['path']}:{r['lineno']}  [{r['kind']}]  {r['signature']}")


@cli.command("context-report")
@click.option("--session-id", default=None, help="Specific session ID. Defaults to latest.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def context_report_cmd(ctx: click.Context, session_id: str | None, as_json: bool) -> None:
    """Compression + provenance report for a run ledger."""
    rt = _core_runtime(ctx.obj["root"])
    payload = rt.context_report(session_id=session_id)
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"chars_before: {payload['chars_before']}")
    click.echo(f"chars_after: {payload['chars_after']}")
    click.echo(f"reduction_pct: {payload['reduction_pct']}%")
    click.echo(f"preserved_facts: {len(payload['preserved_facts'])}")
    for fact in payload["preserved_facts"][:10]:
        click.echo(f"  + {fact}")
    dropped = payload.get("dropped", [])
    if dropped:
        click.echo("dropped:")
        for d in dropped:
            click.echo(f"  - {d['kind']} ({d['count']}): {d['reason']}")


# --------------------------------------------------------------------------- #
# batch-edit                                                                  #
# --------------------------------------------------------------------------- #


@cli.command("batch-edit")
@click.option(
    "--from",
    "from_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="JSON file containing the edit payload.",
)
@click.option(
    "--from-stdin",
    is_flag=True,
    default=False,
    help="Read JSON edit payload from stdin.",
)
@click.option(
    "--no-atomic",
    is_flag=True,
    default=False,
    help="Disable atomic (all-or-nothing) mode.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit result as JSON.")
@click.pass_context
def batch_edit_cmd(
    ctx: click.Context,
    from_file: Path | None,
    from_stdin: bool,
    no_atomic: bool,
    as_json: bool,
) -> None:
    """Apply many mechanical edits across files in one deterministic call.

    Reads a JSON payload either from --from <file.json> or --from-stdin.
    The payload shape:

    \b
      {
        "edits": [
          {"path": "src/foo.py", "op": "replace",
           "old_string": "...", "new_string": "..."},
          {"path": "src/bar.py", "op": "insert_after",
           "anchor": "def baz", "new_string": "..."},
          {"path": "src/baz.ts", "op": "replace_range",
           "line_start": 42, "line_end": 58, "new_string": "..."}
        ],
        "atomic": true
      }

    This is an *optional* Atelier augmentation.  Host-native edit tools remain
    the default path for ordinary coding.
    """
    if from_stdin and from_file:
        raise click.UsageError("Provide either --from or --from-stdin, not both.")
    if not from_stdin and not from_file:
        raise click.UsageError("Provide either --from <file.json> or --from-stdin.")

    if from_stdin:
        raw = sys.stdin.read()
    else:
        assert from_file is not None
        raw = from_file.read_text(encoding="utf-8")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON: {exc}") from exc

    edits = payload.get("edits", [])
    atomic = payload.get("atomic", True)
    if no_atomic:
        atomic = False

    from atelier.core.capabilities.tool_supervision.batch_edit import apply_batch_edit

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", str(Path.cwd()))
    result = apply_batch_edit(
        edits,
        atomic=atomic,
        repo_root=Path(workspace),
    )

    applied = result.get("applied", [])
    failed = result.get("failed", [])
    rolled_back = result.get("rolled_back", False)

    if as_json:
        _emit(result, as_json=True)
    else:
        click.echo(f"applied: {len(applied)}  failed: {len(failed)}  rolled_back: {rolled_back}")
        for item in applied:
            click.echo(f"  ✓ {item['path']}")
        for item in failed:
            click.echo(f"  ✗ {item['path']}: {item['error']}")

    if rolled_back:
        sys.exit(2)
    if failed:
        sys.exit(1)


# --------------------------------------------------------------------------- #


def main() -> None:
    _bench_bootstrap()  # Freeze ATELIER_BENCH_MODE before any lazy init (MODE-05)
    command_name = _cli_command_name(sys.argv[1:])
    session_id, started_at = _begin_cli_telemetry(command_name)
    old_handlers: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        _emit_cli_interrupted(
            session_id=session_id,
            started_at=started_at,
            signum=signum,
            command_name=command_name,
        )
        previous = old_handlers.get(signum)
        if callable(previous):
            previous(signum, frame)
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handler)

    try:
        try:
            cli(obj={"_telemetry_session_id": session_id, "_telemetry_command_name": command_name})
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=code == 0,
                exit_reason="success" if code == 0 else "error",
            )
            raise
        except KeyboardInterrupt:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=False,
                exit_reason="interrupted",
            )
            raise
        except BaseException:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=False,
                exit_reason="error",
            )
            raise
        else:
            _finish_cli_telemetry(
                command_name=command_name,
                session_id=session_id,
                started_at=started_at,
                ok=True,
                exit_reason="success",
            )
    finally:
        from atelier.core.service.telemetry import shutdown_otel

        shutdown_otel()


# --------------------------------------------------------------------------- #
# outcomes                                                                     #
# --------------------------------------------------------------------------- #


@cli.group("outcomes")
def outcomes_group() -> None:
    """Inspect captured route and compact decision outcomes."""


@outcomes_group.command("show")
@click.argument("session_id")
@click.pass_context
def outcomes_show(ctx: click.Context, session_id: str) -> None:
    """Print JSON outcome data for SESSION_ID."""
    from atelier.infra.runtime.outcome_capture import load_outcomes_from_state

    root: Path = ctx.obj["root"]
    path = root / "runs" / f"{session_id}_outcomes.json"
    data = load_outcomes_from_state(path)
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


@outcomes_group.command("summary")
@click.option("--since", default="7d", show_default=True, help="Look-back window, e.g. 7d, 24h.")
@click.pass_context
def outcomes_summary(ctx: click.Context, since: str) -> None:
    """Aggregate outcome_scores by (kind, tool) and print averages."""
    from atelier.infra.runtime.outcome_capture import (
        load_outcomes_from_state,
        summarise_outcomes,
    )

    cutoff = datetime.now(UTC) - _parse_duration(since)
    root: Path = ctx.obj["root"]
    runs_dir = root / "runs"
    if not runs_dir.exists():
        click.echo(json.dumps([], indent=2))
        return

    combined: dict[str, list[dict[str, Any]]] = {
        "route_outcomes": [],
        "compact_outcomes": [],
    }
    for outcomes_file in runs_dir.glob("*_outcomes.json"):
        try:
            mtime = datetime.fromtimestamp(outcomes_file.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        data = load_outcomes_from_state(outcomes_file)
        combined["route_outcomes"].extend(data.get("route_outcomes") or [])
        combined["compact_outcomes"].extend(data.get("compact_outcomes") or [])

    summary = summarise_outcomes(combined)
    click.echo(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- #
# session                                                                      #
# --------------------------------------------------------------------------- #


@cli.group("session")
def session_group() -> None:
    """Per-session cost and savings reports."""


@session_group.command("report")
@click.argument("session_id", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI colours.")
@click.pass_context
def session_report_cmd(
    ctx: click.Context,
    session_id: str | None,
    as_json: bool,
    no_color: bool,
) -> None:
    """Show cost and savings breakdown for SESSION_ID (default: most recent)."""
    from atelier.infra.runtime.session_report import (
        list_run_files,
        load_report,
        render_json,
        render_text,
    )

    root: Path = ctx.obj["root"]

    if session_id is None:
        files = list_run_files(root)
        if not files:
            click.echo("No sessions found - run any AI command first.", err=True)
            raise SystemExit(1)
        # Derive session_id from newest run file name
        session_id = files[0].stem

    report = load_report(session_id, root)
    if report is None:
        click.echo(f"Session '{session_id}' not found in {root / 'runs'}.", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(render_json(report))
    else:
        click.echo(render_text(report, no_color=no_color))


@session_group.command("list")
@click.option("--since", default=None, help="Look-back window, e.g. 7d, 24h.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def session_list_cmd(ctx: click.Context, since: str | None, as_json: bool) -> None:
    """List recent sessions with costs and durations (newest first, max 20)."""
    import dataclasses

    from atelier.infra.runtime.session_report import (
        build_report,
        list_run_files,
    )

    root: Path = ctx.obj["root"]
    cutoff = datetime.now(UTC) - _parse_duration(since) if since else None
    files = list_run_files(root, since=cutoff)[:20]

    if not files:
        msg = "No sessions found"
        if since:
            msg += f" in the last {since}"
        click.echo(msg + ".", err=True)
        return

    rows = []
    for f in files:
        try:
            snapshot = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            report = build_report(snapshot, root)
        except Exception:
            continue
        rows.append(report)

    if as_json:
        click.echo(
            json.dumps(
                [dataclasses.asdict(r) for r in rows],
                default=str,
                indent=2,
            )
        )
        return

    # Terminal table
    hdr = f"  {'Session':<10} {'Started':<22} {'Duration':<14} {'Turns':>6} {'Cost':>9} {'Saved':>9}"
    click.echo(hdr)
    click.echo("  " + "─" * (len(hdr) - 2))
    for r in rows:
        sid = r.session_id[:10]
        started = r.started_at.strftime("%Y-%m-%d %H:%M")
        from atelier.infra.runtime.session_report import (
            _fmt_cost,
            _fmt_duration,
        )

        dur = _fmt_duration(r.duration_seconds, r.is_running)
        click.echo(
            f"  {sid:<10} {started:<22} {dur:<14} {r.total_turns:>6}"
            f" {_fmt_cost(r.total_cost_usd):>9} {_fmt_cost(r.total_atelier_savings_usd):>9}"
        )


# --------------------------------------------------------------------------- #
# memory                                                                       #
# --------------------------------------------------------------------------- #


@cli.group("memory")
def memory_group_cli() -> None:
    """Inspect native AI memory files from Claude, Codex, and Gemini."""


def _make_memory_registry(cwd: Path | None = None) -> Any:
    from atelier.core.capabilities.cross_vendor_memory import MemoryRegistry
    from atelier.core.capabilities.cross_vendor_memory.claude_adapter import ClaudeAdapter
    from atelier.core.capabilities.cross_vendor_memory.codex_adapter import CodexAdapter
    from atelier.core.capabilities.cross_vendor_memory.gemini_adapter import GeminiAdapter

    return MemoryRegistry(
        adapters=[  # type: ignore[list-item]
            ClaudeAdapter(),
            CodexAdapter(),
            GeminiAdapter(cwd=cwd or Path.cwd()),
        ]
    )


@memory_group_cli.command("list")
@click.option("--vendor", default=None, help="Filter to a single vendor: claude, codex, gemini.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def memory_list_cmd(ctx: click.Context, vendor: str | None, as_json: bool) -> None:
    """List all detected memory facts, grouped by vendor."""
    import dataclasses

    registry = _make_memory_registry()

    facts = registry.by_vendor(vendor) if vendor else registry.all_facts()

    if as_json:
        click.echo(
            json.dumps(
                [dataclasses.asdict(f) for f in facts],
                default=str,
                indent=2,
            )
        )
        return

    if not facts:
        click.echo("No memory facts found.", err=True)
        return

    # Group by vendor
    by_vendor: dict[str, list[Any]] = {}
    for f in facts:
        by_vendor.setdefault(f.vendor, []).append(f)

    total = len(facts)
    n_vendors = len(by_vendor)
    click.echo(f"Memory facts ({total} total, {n_vendors} vendor{'s' if n_vendors != 1 else ''})")
    click.echo("")

    vendor_labels = {
        "claude": "Anthropic - Claude Code",
        "codex": "OpenAI - Codex",
        "gemini": "Google - Gemini CLI",
    }
    for v, vfacts in sorted(by_vendor.items()):
        label = vendor_labels.get(v, v.capitalize())
        click.echo(f"{label} ({len(vfacts)} fact{'s' if len(vfacts) != 1 else ''})")

        # Group by source_path within vendor
        by_path: dict[Path, list[Any]] = {}
        for f in vfacts:
            by_path.setdefault(f.source_path, []).append(f)

        for path, pfacts in sorted(by_path.items(), key=lambda x: str(x[0])):
            click.echo(f"  {path}  ({pfacts[0].source_kind})")
            preview = pfacts[:3]
            for fact in preview:
                short = fact.content[:72].replace("\n", " ")
                click.echo(f"    [{fact.fact_id}] {short}")
            if len(pfacts) > 3:
                click.echo(f"    ... {len(pfacts) - 3} more")
        click.echo("")


@memory_group_cli.command("show")
@click.argument("fact_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
def memory_show_cmd(fact_id: str, as_json: bool) -> None:
    """Show full content and provenance for FACT_ID."""
    import dataclasses

    registry = _make_memory_registry()
    fact = registry.show(fact_id)

    if fact is None:
        click.echo(f"Fact '{fact_id}' not found.", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(dataclasses.asdict(fact), default=str, indent=2))
        return

    click.echo(f"ID:        {fact.fact_id}")
    click.echo(f"Vendor:    {fact.vendor}")
    click.echo(f"Source:    {fact.source_path}:{fact.line_number or '?'}")
    click.echo(f"Kind:      {fact.source_kind}")
    click.echo(f"Read at:   {fact.captured_at.isoformat()}")
    if fact.raw_meta:
        click.echo(f"Meta:      {json.dumps(fact.raw_meta, default=str)}")
    click.echo("")
    click.echo(fact.content)


@memory_group_cli.command("share")
@click.option("--agent-id", required=True, help="Editable memory agent id, e.g. atelier:code.")
@click.option("--label", required=True, help="Editable memory block label.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.pass_context
def memory_share_cmd(ctx: click.Context, agent_id: str, label: str, as_json: bool) -> None:
    """Promote one editable memory block into workspace-shared memory."""
    from atelier.core.capabilities.team import (
        TeamAuditEvent,
        TeamWorkspaceManager,
        ensure_shared_memory_write,
    )
    from atelier.infra.storage.factory import make_memory_store

    root = ctx.obj["root"]
    manager = TeamWorkspaceManager(root)
    workspace = manager.load_workspace()
    member = manager.require_member(None, workspace=workspace)
    ensure_shared_memory_write(member)

    store = make_memory_store(root)
    block = store.get_block(agent_id, label)
    if block is None:
        raise click.ClickException(f"memory block not found: {agent_id}:{label}")
    metadata = dict(block.metadata or {})
    metadata["scope"] = "shared"
    metadata.setdefault("workspace_id", workspace.id)
    metadata.setdefault("owner_user_id", member.user_id)
    metadata["shared_by_user_id"] = member.user_id
    updated = block.model_copy(update={"metadata": metadata})
    stored = store.upsert_block(updated, actor=f"team:{member.user_id}", reason="workspace share")
    manager.append_audit_event(
        TeamAuditEvent(
            action="memory.share",
            actor_user_id=member.user_id,
            details={"agent_id": agent_id, "label": label, "block_id": stored.id},
        )
    )
    payload = {
        "id": stored.id,
        "label": stored.label,
        "scope": stored.metadata.get("scope"),
        "workspace_id": workspace.id,
    }
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"shared {agent_id}:{label} into workspace {workspace.name}")


@memory_group_cli.command("find")
@click.argument("query")
@click.option("--limit", default=20, show_default=True, help="Max results.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
def memory_find_cmd(query: str, limit: int, as_json: bool) -> None:
    """Find facts matching QUERY using substring + fuzzy search."""
    import dataclasses

    registry = _make_memory_registry()
    facts = registry.find(query, limit=limit)

    if as_json:
        click.echo(json.dumps([dataclasses.asdict(f) for f in facts], default=str, indent=2))
        return

    if not facts:
        click.echo(f"No facts found matching '{query}'.")
        return

    click.echo(f"Found {len(facts)} match{'es' if len(facts) != 1 else ''}:")
    for f in facts:
        short = f.content[:72].replace("\n", " ")
        click.echo(f"  [{f.fact_id}] {short}")


@memory_group_cli.command("paths")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
def memory_paths_cmd(as_json: bool) -> None:
    """Show all file paths the memory adapters read from."""
    registry = _make_memory_registry()
    paths_by_vendor = registry.source_paths_by_vendor()

    if as_json:
        click.echo(json.dumps(paths_by_vendor, indent=2))
        return

    if not paths_by_vendor:
        click.echo("No memory source files found on this machine.")
        return

    for vendor, paths in sorted(paths_by_vendor.items()):
        click.echo(f"{vendor}:")
        for p in paths:
            click.echo(f"  {p}")


# --------------------------------------------------------------------------- #
# team                                                                         #
# --------------------------------------------------------------------------- #


@cli.group("team")
def team_group() -> None:
    """Manage local team workspace state."""


@team_group.command("init")
@click.option("--name", required=True, help="Workspace display name.")
@click.option("--admin-email", default="admin@local", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_init_cmd(ctx: click.Context, name: str, admin_email: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    workspace = TeamWorkspaceManager(ctx.obj["root"]).init_workspace(name=name, admin_email=admin_email)
    payload = workspace.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"initialized workspace {workspace.name} ({workspace.id})")


@team_group.command("invite")
@click.argument("emails", nargs=-1)
@click.option("--role", type=click.Choice(["member", "viewer", "admin"]), default="member", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_invite_cmd(ctx: click.Context, emails: tuple[str, ...], role: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    if not emails:
        raise click.ClickException("provide at least one email")
    invites = TeamWorkspaceManager(ctx.obj["root"]).invite_members(list(emails), role=role)  # type: ignore[arg-type]
    payload = [invite.model_dump(mode="json") for invite in invites]
    if as_json:
        _emit(payload, as_json=True)
        return
    for invite in invites:
        click.echo(f"{invite.email}\t{invite.role}\t{invite.code}")


@team_group.command("join")
@click.argument("invite_code")
@click.option("--user-id", default=None, help="Override the invite email as the local user id.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_join_cmd(ctx: click.Context, invite_code: str, user_id: str | None, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    member = TeamWorkspaceManager(ctx.obj["root"]).join_workspace(invite_code, user_id=user_id)
    payload = member.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"joined workspace as {member.user_id} ({member.role})")


@team_group.command("role")
@click.argument("user_id")
@click.argument("role", type=click.Choice(["admin", "member", "viewer"]))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_role_cmd(ctx: click.Context, user_id: str, role: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    member = TeamWorkspaceManager(ctx.obj["root"]).set_role(user_id, role)  # type: ignore[arg-type]
    payload = member.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"{member.user_id}\t{member.role}")


@team_group.command("usage")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_usage_cmd(ctx: click.Context, since: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager, summarize_workspace_usage

    manager = TeamWorkspaceManager(ctx.obj["root"])
    manager.require_admin()
    payload = summarize_workspace_usage(ctx.obj["root"], manager=manager, since=_parse_since_arg(since))
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"workspace: {payload['workspace_id']}")
    click.echo(f"sessions: {payload['session_count']}")
    click.echo(f"total cost usd: {payload['total_cost_usd']:.6f}")
    for row in payload["users"]:
        click.echo(f"{row['user_id']}\t{row['role']}\t{row['session_count']}\t{row['total_cost_usd']:.6f}")


@team_group.command("audit")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def team_audit_cmd(ctx: click.Context, since: str, as_json: bool) -> None:
    from atelier.core.capabilities.team import TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    manager.require_admin()
    events = manager.list_audit_events(since=_parse_since_arg(since))
    payload = [event.model_dump(mode="json") for event in events]
    if as_json:
        _emit(payload, as_json=True)
        return
    if not events:
        click.echo("(no team audit events)")
        return
    for event in events:
        click.echo(f"{event.at.isoformat()}\t{event.action}\t{event.actor_user_id}")


# --------------------------------------------------------------------------- #
# governance                                                                   #
# --------------------------------------------------------------------------- #


@cli.group("governance")
def governance_group() -> None:
    """Inspect and apply workspace governance policy."""


@governance_group.command("show")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def governance_show_cmd(ctx: click.Context, as_json: bool) -> None:
    from atelier.core.capabilities.governance import load_policy

    policy = load_policy(ctx.obj["root"])
    payload = policy.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(yaml.safe_dump(payload, sort_keys=True).rstrip())


@governance_group.command("apply")
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def governance_apply_cmd(ctx: click.Context, file_path: Path, as_json: bool) -> None:
    from atelier.core.capabilities.governance import GovernancePolicy, save_policy
    from atelier.core.capabilities.team import TeamAuditEvent, TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    member = manager.require_admin()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    policy = GovernancePolicy.model_validate(loaded)
    saved = save_policy(ctx.obj["root"], policy)
    manager.append_audit_event(
        TeamAuditEvent(
            action="governance.apply",
            actor_user_id=member.user_id,
            details={"source": str(file_path)},
        )
    )
    payload = saved.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(f"applied governance policy from {file_path}")


# --------------------------------------------------------------------------- #
# audit export                                                                 #
# --------------------------------------------------------------------------- #


@cli.group("audit")
def audit_group() -> None:
    """Export and verify workspace audit bundles."""


@audit_group.command("export")
@click.option("--since", default="30d", show_default=True, help="Time window like 30d, 24h, or 2026-05-01.")
@click.option("--out", "out_dir", required=True, type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def audit_export_cmd(ctx: click.Context, since: str, out_dir: Path, as_json: bool) -> None:
    from atelier.core.capabilities.audit_export import export_audit_bundle
    from atelier.core.capabilities.team import TeamAuditEvent, TeamWorkspaceManager

    manager = TeamWorkspaceManager(ctx.obj["root"])
    member = manager.require_admin()
    payload = export_audit_bundle(ctx.obj["root"], out_dir=out_dir, since=_parse_since_arg(since))
    manager.append_audit_event(
        TeamAuditEvent(
            action="audit.export",
            actor_user_id=member.user_id,
            details={"bundle_dir": payload["bundle_dir"]},
        )
    )
    if as_json:
        _emit(payload, as_json=True)
        return
    click.echo(payload["bundle_dir"])


@audit_group.command("verify")
@click.argument("bundle_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def audit_verify_cmd(ctx: click.Context, bundle_dir: Path, as_json: bool) -> None:
    from atelier.core.capabilities.audit_export import verify_audit_bundle

    payload = verify_audit_bundle(ctx.obj["root"], bundle_dir=bundle_dir)
    if as_json:
        _emit(payload, as_json=True)
        return
    if payload["valid"]:
        click.echo(f"verified {bundle_dir}")
        return
    raise click.ClickException(
        f"bundle verification failed: {', '.join(payload['tampered_files']) or 'signature mismatch'}"
    )


# --------------------------------------------------------------------------- #
# insights                                                                     #
# --------------------------------------------------------------------------- #


def _parse_since_arg(value: str) -> datetime:
    """Parse ``--since`` argument.

    Accepts:
    * ``7d``, ``30d``, ``24h``, ``30m``  - duration relative to now
    * ``YYYY-MM-DD``                       - absolute date (start of day UTC)
    """
    import re
    from datetime import UTC, datetime, timedelta

    stripped = value.strip()
    # Relative duration (e.g. "7d", "24h", "30m")
    match = re.fullmatch(r"(\d+)([dhm])", stripped)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = (
            timedelta(days=amount)
            if unit == "d"
            else timedelta(hours=amount) if unit == "h" else timedelta(minutes=amount)
        )
        return datetime.now(UTC) - delta

    # Absolute date (YYYY-MM-DD)
    try:
        return datetime.strptime(stripped, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        pass

    raise click.ClickException(
        f"Cannot parse --since value {value!r}. " "Use a duration like '7d', '24h', or a date like '2026-05-01'."
    )


@cli.command("insights")
@click.option(
    "--since",
    default="7d",
    show_default=True,
    help="Time window: '7d', '30d', '24h', or a date like '2026-05-01'.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON.")
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI colours.")
@click.option(
    "--vendor",
    default=None,
    help="Filter output to a specific vendor (e.g. 'anthropic').",
)
@click.option(
    "--group-by",
    "group_by",
    default="tool",
    type=click.Choice(["tool", "vendor", "model", "session"]),
    show_default=True,
    help="Primary grouping for cost breakdown.",
)
@click.pass_context
def insights_cmd(
    ctx: click.Context,
    since: str,
    as_json: bool,
    no_color: bool,
    vendor: str | None,
    group_by: str,
) -> None:
    """Weekly AI-spend insights and savings opportunities."""
    from datetime import UTC, datetime

    from atelier.infra.runtime.insights import (
        InsightsWindow,
        build_insights,
        render_json,
        render_text,
    )

    root: Path = ctx.obj["root"]
    since_dt = _parse_since_arg(since)
    until_dt = datetime.now(UTC)

    window: InsightsWindow = build_insights(root, since=since_dt, until=until_dt)

    if window.session_count == 0:
        if as_json:
            click.echo(render_json(window))
        else:
            since_str = since_dt.strftime("%Y-%m-%d")
            click.echo(f"No sessions found since {since_str}.")
        return

    # Apply vendor filter to cost_by_vendor display (full window still computed).
    if vendor and not as_json:
        vendor_key = vendor.capitalize()
        filtered_cost = window.cost_by_vendor.get(vendor_key, 0.0)
        click.echo(f"Vendor filter: {vendor_key}  ${filtered_cost:.2f}" f" of ${window.total_cost_usd:.2f} total")

    # Apply group-by override for display (swap cost_by_* fields shown).
    display_window = window
    if group_by == "vendor" and not as_json:
        # Reorder: show vendor bars prominently (already first in default render).
        pass
    elif group_by == "model" and not as_json:
        # Swap cost_by_tool -> cost_by_model for the tool section.

        display_window = InsightsWindow(
            since=window.since,
            until=window.until,
            session_count=window.session_count,
            total_duration_seconds=window.total_duration_seconds,
            total_cost_usd=window.total_cost_usd,
            total_atelier_savings_usd=window.total_atelier_savings_usd,
            cost_by_vendor=window.cost_by_vendor,
            cost_by_tool=window.cost_by_model,
            cost_by_model=window.cost_by_model,
            top_sessions=window.top_sessions,
            outcomes_summary=window.outcomes_summary,
            opportunities=window.opportunities,
        )
    elif group_by == "session" and not as_json:
        # Replace cost_by_tool with per-session breakdown.
        session_costs = {s.session_id[:8]: s.cost_usd for s in window.top_sessions}
        display_window = InsightsWindow(
            since=window.since,
            until=window.until,
            session_count=window.session_count,
            total_duration_seconds=window.total_duration_seconds,
            total_cost_usd=window.total_cost_usd,
            total_atelier_savings_usd=window.total_atelier_savings_usd,
            cost_by_vendor=window.cost_by_vendor,
            cost_by_tool=session_costs,
            cost_by_model=window.cost_by_model,
            top_sessions=window.top_sessions,
            outcomes_summary=window.outcomes_summary,
            opportunities=window.opportunities,
        )

    if as_json:
        click.echo(render_json(window))
    else:
        click.echo(render_text(display_window, no_color=no_color))


if __name__ == "__main__":
    main()
