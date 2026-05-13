"""MCP server (stdio JSON-RPC) for the Atelier task runtime.

Implements a minimal subset of the Model Context Protocol sufficient for
Codex / Claude Code to discover and call the runtime tools.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, create_model

from atelier import __version__ as atelier_version
from atelier.core.capabilities.archival_recall import ArchivalRecallCapability
from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability
from atelier.core.environment import (
    is_dev_mode,
    mcp_tool_description,
    mcp_tool_mode,
    mcp_tool_visible_to_llm,
    passive_tool_message,
)
from atelier.core.foundation.memory_models import MemoryBlock
from atelier.core.foundation.models import RawArtifact, Trace, to_jsonable
from atelier.core.foundation.redaction import redact
from atelier.core.foundation.rubric_gate import run_rubric
from atelier.gateway.adapters.runtime import ReasoningRuntime
from atelier.infra.embeddings.factory import make_embedder
from atelier.infra.runtime.realtime_context import RealtimeContextManager
from atelier.infra.runtime.run_ledger import RunLedger
from atelier.infra.storage.factory import make_memory_store
from atelier.infra.storage.memory_store import MemoryConcurrencyError, MemorySidecarUnavailable

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "atelier-task"
SERVER_VERSION = atelier_version


def _check_dev_mode(tool_name: str) -> str | None:
    if not is_dev_mode():
        return passive_tool_message(tool_name)
    return None


# --------------------------------------------------------------------------- #
# Tool Registry Decorator                                                     #
# --------------------------------------------------------------------------- #

TOOLS: dict[str, dict[str, Any]] = {}


def _tool_description(spec: dict[str, Any]) -> str:
    return mcp_tool_description(
        str(spec.get("description", "") or ""),
        is_dev=bool(spec.get("is_dev")),
    )


def _tool_visible_to_llm(tool_name: str, spec: dict[str, Any]) -> bool:
    return mcp_tool_visible_to_llm(tool_name, is_dev=bool(spec.get("is_dev")))


def _tool_mode(spec: dict[str, Any]) -> str:
    return mcp_tool_mode(is_dev=bool(spec.get("is_dev")))


def mcp_tool(
    name: str | None = None, description: str | None = None, is_dev: bool = False
) -> Callable[[Callable[..., Any]], Callable[[dict[str, Any]], Any]]:
    """Decorator to register a tool and auto-derive its MCP schema."""

    def decorator(
        func: Callable[..., Any],
    ) -> Callable[[dict[str, Any]], Any]:
        tool_name = name or func.__name__.removeprefix("tool_")
        # Use the first line of the docstring as the description
        tool_description = description or (func.__doc__ or "").strip().split("\n")[0]

        sig = inspect.signature(func)
        fields = {}
        for param_name, param in sig.parameters.items():
            annotation = param.annotation if param.annotation is not inspect.Parameter.empty else Any
            default = param.default if param.default is not inspect.Parameter.empty else ...
            fields[param_name] = (
                annotation,
                Field(default=default) if default is not ... else Field(...),
            )

        if fields:
            # Convert to format expected by create_model: (type, default/Field)
            field_defs = {k: (v[0], v[1]) for k, v in fields.items()}
            ArgsModel = create_model(f"{func.__name__}_Args", **field_defs)  # type: ignore[call-overload]
            schema = ArgsModel.model_json_schema()
            # Clean up Pydantic-isms for MCP clients
            if "title" in schema:
                del schema["title"]

            @wraps(func)
            def handler_wrapper(args: dict[str, Any]) -> Any:
                validated = ArgsModel.model_validate(args)
                return func(**validated.model_dump())

        else:
            schema = {"type": "object", "properties": {}}

            @wraps(func)
            def handler_wrapper(args: dict[str, Any]) -> Any:
                return func()

        TOOLS[tool_name] = {
            "handler": handler_wrapper,
            "description": tool_description,
            "inputSchema": schema,
            "is_dev": is_dev,
        }
        return handler_wrapper

    return decorator


# --------------------------------------------------------------------------- #
# session_state.json helpers                                                  #
# --------------------------------------------------------------------------- #

_current_ledger: RunLedger | None = None
_realtime_ctx: RealtimeContextManager | None = None
_product_session_id: str | None = None
_product_session_started_at: float | None = None
_last_plan_hash_by_session: dict[str, str] = {}
_last_plan_by_session: dict[str, list[str]] = {}
_last_blocked_plan_hash_by_session: dict[str, str] = {}


def _service_backed_state() -> bool:
    return True


def _detect_agent() -> str:
    """Derive the agent label from the runtime environment.

    Checks, in order:
    1. ATELIER_AGENT env var (explicit override — any host can set this)
    2. CLAUDE_SESSION_ID → "claude"
    3. GEMINI_SESSION_ID or GEMINI_CLI_VERSION → "gemini"
    4. CODEX_SESSION_ID → "codex"
    5. OPENCODE_SESSION_ID → "opencode"
    6. Falls back to "claude" (the MCP wrapper is shipped with the Claude plugin)
    """
    explicit = os.environ.get("ATELIER_AGENT", "").strip()
    if explicit:
        return explicit
    if os.environ.get("CLAUDE_SESSION_ID"):
        return "claude"
    if os.environ.get("GEMINI_SESSION_ID") or os.environ.get("GEMINI_CLI_VERSION"):
        return "gemini"
    if os.environ.get("CODEX_SESSION_ID"):
        return "codex"
    if os.environ.get("OPENCODE_SESSION_ID"):
        return "opencode"
    # Default: the plugin lives in the Claude Code plugin system
    return "claude"


def _get_ledger() -> RunLedger:
    global _current_ledger
    if _current_ledger is None:
        root = _atelier_root()
        _current_ledger = RunLedger(root=root, agent=_detect_agent())
        # Publish session_id AND atelier_root to session_state so PostToolUse hooks
        # can find the right run file regardless of ATELIER_ROOT in their env.
        _write_session_state(
            {
                "active_session_id": _current_ledger.session_id,
                "atelier_root": str(root),
            }
        )
    return _current_ledger


def _get_realtime_context() -> RealtimeContextManager:
    global _realtime_ctx
    if _realtime_ctx is None:
        _realtime_ctx = RealtimeContextManager(_atelier_root())
    return _realtime_ctx


def _get_product_session_id() -> str:
    global _product_session_id
    if _product_session_id is None:
        from atelier.core.foundation.identity import new_session_id

        _product_session_id = new_session_id()
    return _product_session_id


def _emit_mcp_session_start() -> None:
    global _product_session_started_at
    if _product_session_started_at is not None:
        return
    from importlib.metadata import PackageNotFoundError, version

    from atelier.core.foundation.identity import get_anon_id, platform_payload
    from atelier.core.service.telemetry import emit_product

    try:
        service_version = version("atelier")
    except PackageNotFoundError:
        service_version = SERVER_VERSION
    # OTel is initialized lazily on first emit_product_log call.
    _product_session_started_at = time.perf_counter()
    emit_product(
        "session_start",
        agent_host=_detect_agent(),
        atelier_version=service_version,
        anon_id=get_anon_id(),
        session_id=_get_product_session_id(),
        **platform_payload(),
    )


def _emit_mcp_session_end(exit_reason: str = "success") -> None:
    if _product_session_started_at is None:
        return
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import bucket_duration_s

    elapsed = max(0.0, time.perf_counter() - _product_session_started_at)
    emit_product(
        "session_end",
        session_id=_get_product_session_id(),
        duration_s_bucket=bucket_duration_s(elapsed),
        exit_reason=exit_reason,
    )


def _match_mcp_lexical(args: dict[str, Any]) -> None:
    from atelier.core.service.telemetry.frustration import match_frustration

    for key in ("task", "query", "user_goal", "error"):
        value = args.get(key)
        if isinstance(value, str):
            match_frustration(value, surface="mcp_prompt", session_id=_get_product_session_id())


def _emit_reasonblock_retrieved(scored: list[Any], domain: str | None) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import hash_identifier

    for rank, item in enumerate(scored, start=1):
        block = getattr(item, "block", None)
        emit_product(
            "reasonblock_retrieved",
            block_id_hash=hash_identifier(str(getattr(block, "id", ""))),
            domain=str(getattr(block, "domain", domain or "")),
            retrieval_score=float(getattr(item, "score", 0.0)),
            rank=rank,
            session_id=_get_product_session_id(),
        )


_context_budget_recorder: Any = None


def _get_context_budget_recorder() -> Any:
    """Get or create the ContextBudgetRecorder singleton."""
    global _context_budget_recorder
    if _service_backed_state():
        return _NoOpContextBudgetRecorder()
    if _context_budget_recorder is None:
        try:
            from atelier.core.capabilities.telemetry.context_budget import ContextBudgetRecorder
            from atelier.infra.storage.factory import create_store

            store = create_store(_atelier_root())
            store.init()
            _context_budget_recorder = ContextBudgetRecorder(store)
        except Exception:
            # If recording fails, return a no-op recorder
            _context_budget_recorder = _NoOpContextBudgetRecorder()
    return _context_budget_recorder


class _NoOpContextBudgetRecorder:
    """No-op recorder for when context budget recording is not available."""

    def record(self, **kwargs: Any) -> None:
        """No-op record method."""
        pass

    def aggregate_run(self, session_id: str) -> Any:
        """No-op aggregate method."""
        return {}


def _session_state_path() -> Path:
    from atelier.core.foundation.paths import resolve_session_state_path

    return resolve_session_state_path()


def _read_session_state() -> dict[str, Any]:
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        import typing

        return typing.cast(dict[str, Any], json.loads(p.read_text("utf-8")))
    except Exception:
        return {}


def _write_session_state(updates: dict[str, Any]) -> None:
    if _service_backed_state():
        return
    try:
        p = _session_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        state = _read_session_state()
        state.update(updates)

        if updates.get("trace_recorded"):
            session_id = os.environ.get("CLAUDE_SESSION_ID", "")
            if session_id:
                sessions: dict[str, Any] = state.setdefault("sessions", {})
                sessions[session_id] = {"trace_recorded": True}

        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        logger.warning(
            "Suppressed exception at mcp_server.py:381",
            exc_info=True,
        )


# --------------------------------------------------------------------------- #
# Tool implementations                                                        #
# --------------------------------------------------------------------------- #


def _atelier_root() -> Path:
    from atelier.core.foundation.paths import default_store_root

    return Path(os.environ.get("ATELIER_ROOT", str(default_store_root())))


# --------------------------------------------------------------------------- #
# Zero-config background service                                              #
# --------------------------------------------------------------------------- #


def _autostart_servicectl(root: Path) -> None:
    """Start the background servicectl daemon for *root* if not already running.

    Called once per MCP server session from ``_runtime()``.  Fails silently so
    it can never break the MCP request/response loop.
    """
    import subprocess

    try:
        pid_path = root / "servicectl" / "servicectl.pid"
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text("utf-8").strip())
                os.kill(pid, 0)  # raises if process is gone
                return  # daemon is already running
            except (ValueError, ProcessLookupError, OSError):
                pid_path.unlink(missing_ok=True)

        log_path = root / "servicectl" / "servicectl.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "atelier.gateway.adapters.cli",
            "--root",
            str(root),
            "servicectl",
            "run",
            "--interval-seconds",
            "60",
            "--maintenance-interval-seconds",
            "21600",
        ]
        env = os.environ.copy()
        env["ATELIER_ROOT"] = str(root)
        with log_path.open("a", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(
                command,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
    except Exception:
        logger.warning(
            "Suppressed exception at mcp_server.py:446",
            exc_info=True,
        )


def _detect_default_branch(repo: Path) -> str | None:
    """Detect the remote default branch (main/master) for *repo*."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "show", "origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("HEAD branch:"):
                branch = stripped.split(":")[-1].strip()
                if branch:
                    return branch
    except Exception:
        logger.warning(
            "Suppressed exception at mcp_server.py:468",
            exc_info=True,
        )
    # Fallback: try main then master
    for candidate in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{candidate}"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return candidate
        except Exception:
            continue
    return None


_log = logging.getLogger("atelier.mcp")


def _check_auto_update() -> None:
    """Check git remote for a newer version and auto-update if found.

    Compares the version in the remote repo's ``pyproject.toml`` against the
    currently installed version.  If they differ, pulls the repo and runs
    the install script.  Logs errors and emits telemetry on failure but
    never blocks the MCP server.

    Disabled by setting ``ATELIER_NO_AUTO_UPDATE=1`` in the environment.
    """
    import re
    import subprocess

    if os.environ.get("ATELIER_NO_AUTO_UPDATE") == "1":
        _log.info("auto-update disabled via ATELIER_NO_AUTO_UPDATE=1")
        return

    _log.info("checking for auto-update...")

    try:
        # Determine the repo directory
        install_dir = os.environ.get("ATELIER_INSTALL_DIR", "")
        if install_dir:
            repo = Path(install_dir)
            _log.debug("repo from ATELIER_INSTALL_DIR: %s", repo)
        else:
            repo = Path(__file__).resolve().parents[4]
            _log.debug("repo from file path: %s", repo)

        if not (repo / ".git").exists():
            _log.debug("not a git checkout — skipping auto-update")
            return  # Not a git checkout, nothing to auto-update

        # Fetch latest remote info
        _log.info("fetching latest remote refs from origin...")
        result = subprocess.run(
            ["git", "fetch", "--tags", "--prune", "origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            _log.warning("git fetch exited %d: %s", result.returncode, result.stderr.strip())
            return

        default_branch = _detect_default_branch(repo)
        if default_branch is None:
            _log.warning("could not detect default remote branch")
            return

        # Read remote version from pyproject.toml
        result = subprocess.run(
            ["git", "show", f"origin/{default_branch}:pyproject.toml"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            _log.warning(
                "could not read remote pyproject.toml (exit %d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return

        match = re.search(r'^version\s*=\s*"([^"]+)"', result.stdout, re.MULTILINE)
        if not match:
            _log.warning("could not parse version from remote pyproject.toml")
            return

        remote_version = match.group(1)
        _log.info("current=%s  remote=%s", atelier_version, remote_version)

        if remote_version == atelier_version:
            _log.info("already up-to-date")
            return

        # Newer (or different) version detected — pull and reinstall
        _log.info("version changed — pulling %s/%s ...", default_branch, default_branch)
        subprocess.run(
            ["git", "pull", "--ff-only", "origin", default_branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )

        install_script = repo / "scripts" / "install.sh"
        if install_script.exists():
            _log.info("running install script...")
            subprocess.run(
                ["bash", str(install_script), "--local"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
            _log.info("auto-update complete")
        else:
            _log.warning("install script not found at %s", install_script)
    except Exception:
        _log.exception("auto-update failed")
        with contextlib.suppress(Exception):
            from atelier.core.service.telemetry import emit_product

            emit_product(
                "mcp_auto_update_failed",
                current_version=atelier_version,
                session_id=_get_product_session_id(),
            )


def _run_worker_tick_safe(root: Path) -> None:
    """Process up to 20 pending jobs for *root*.  Run in a daemon thread."""
    try:
        from atelier.core.service.worker import Worker
        from atelier.infra.storage.factory import create_store

        store = create_store(root)
        store.init()
        worker = Worker(store=store)
        for _ in range(20):
            if worker.run_once() is None:
                break
    except Exception:
        logger.warning(
            "Suppressed exception at mcp_server.py:618",
            exc_info=True,
        )


_runtime_cache: ReasoningRuntime | None = None


def _runtime() -> ReasoningRuntime:
    global _runtime_cache
    if _runtime_cache is None:
        _runtime_cache = ReasoningRuntime(_atelier_root())
    return _runtime_cache


def _reset_runtime_cache_for_testing() -> None:
    """Reset the runtime singleton for test isolation."""
    global _runtime_cache
    _runtime_cache = None


def _smart_state_path() -> Path:
    return _atelier_root() / "smart_state.json"


def _read_smart_state() -> dict[str, Any]:
    path = _smart_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _live_savings_events_path() -> Path:
    return _atelier_root() / "live_savings_events.jsonl"


def _append_live_savings_event(event: dict[str, Any]) -> None:
    path = _live_savings_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _write_smart_state(state: dict[str, Any]) -> None:
    try:
        path = _smart_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        logger.warning(
            "Suppressed exception at mcp_server.py:669",
            exc_info=True,
        )


def _coerce_saved_tokens(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return int(max(0.0, value))
    if isinstance(value, dict):
        return sum(
            int(max(0.0, float(item_value)))
            for item_value in value.values()
            if isinstance(item_value, (int, float)) and not isinstance(item_value, bool)
        )
    return 0


def _extract_compact_output_tokens_saved(result: dict[str, Any]) -> int:
    return _coerce_saved_tokens(result.get("tokens_saved_vs_naive"))


def _extract_tokens_saved(result: dict[str, Any]) -> int:
    direct = _coerce_saved_tokens(result.get("tokens_saved"))
    if direct > 0:
        return direct
    return _extract_compact_output_tokens_saved(result)


def _record_smart_state_savings(tokens_saved: int, calls_avoided: int) -> None:
    if tokens_saved <= 0 and calls_avoided <= 0:
        return
    state = _read_smart_state()
    savings = state.get("savings")
    if not isinstance(savings, dict):
        savings = {"calls_avoided": 0, "tokens_saved": 0}
    savings["calls_avoided"] = int(savings.get("calls_avoided", 0) or 0) + max(0, calls_avoided)
    savings["tokens_saved"] = int(savings.get("tokens_saved", 0) or 0) + max(0, tokens_saved)
    state["savings"] = savings
    _write_smart_state(state)


_REDACTION_PLACEHOLDER_RE = re.compile(r"<redacted[^>]*>")


def _core_runtime() -> Any:
    return _runtime().core_runtime


def _redact_memory_input(text: str, field_name: str) -> str:
    redacted = redact(text)
    if not text:
        return redacted
    remaining = _REDACTION_PLACEHOLDER_RE.sub("", redacted)
    if len(remaining.strip()) < len(text.strip()) * 0.5:
        raise ValueError(f"{field_name} rejected: likely secret leakage")
    return redacted


def _memory_store() -> Any:
    return make_memory_store(_atelier_root())


def _archival_recall() -> ArchivalRecallCapability:
    return ArchivalRecallCapability(_memory_store(), make_embedder(), redactor=redact)


def _workspace_path(file_path: str) -> Path:
    p = Path(file_path)
    if p.is_absolute():
        return p
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    return Path(workspace) / p


@mcp_tool(name="task", is_dev=True)
def tool_get_reasoning_context(
    task: str,
    domain: str | None = None,
    files: list[str] | None = None,
    tools: list[str] | None = None,
    errors: list[str] | None = None,
    max_blocks: int = 5,
    token_budget: int | None = 2000,
    dedup: bool = True,
    include_telemetry: bool = False,
    include_run_ledger: bool = False,
    agent_id: str | None = None,
    recall: bool = True,
) -> dict[str, Any]:
    """[DEV] Record task context and retrieve relevant ReasonBlocks for the task."""
    if stub := _check_dev_mode("task"):
        return {"context": stub}

    if errors is None:
        errors = []
    if tools is None:
        tools = []
    if files is None:
        files = []
    rt = _runtime()
    led = _get_ledger()
    led.task = task
    if domain:
        led.domain = domain
    _match_mcp_lexical({"task": task})

    led.record_tool_call(
        "get_reasoning_context",
        {
            "task": task,
            "domain": domain,
            "files": files,
            "tools": tools,
            "errors": errors,
            "max_blocks": max_blocks,
            "token_budget": token_budget,
            "dedup": dedup,
            "include_telemetry": include_telemetry,
            "include_run_ledger": include_run_ledger,
            "agent_id": agent_id,
            "recall": recall,
        },
    )

    with contextlib.suppress(Exception):
        scored = rt.core_runtime.reasoning_reuse.retrieve(
            task=task,
            domain=domain,
            files=files,
            tools=tools,
            errors=errors,
            limit=max_blocks,
            token_budget=token_budget,
            dedup=dedup,
        )
        _emit_reasonblock_retrieved(scored, domain)

    payload = rt.get_reasoning_context(
        task=task,
        domain=domain,
        files=files,
        tools=tools,
        errors=errors,
        max_blocks=max_blocks,
        token_budget=token_budget,
        dedup=dedup,
        include_telemetry=include_telemetry,
        agent_id=agent_id,
        recall=recall,
    )
    result: dict[str, Any] = payload if isinstance(payload, dict) else {"context": payload}
    if include_run_ledger:
        result["run_ledger"] = led.snapshot()
    return result


@mcp_tool(name="route", is_dev=True)
def tool_route(
    op: Literal["decide", "verify"],
    user_goal: str = "",
    repo_root: str = ".",
    task_type: Literal["debug", "feature", "refactor", "test", "explain", "review", "docs", "ops"] = "feature",
    risk_level: Literal["low", "medium", "high"] = "medium",
    changed_files: list[str] | None = None,
    domain: str | None = None,
    step_type: Literal[
        "classify",
        "compress",
        "retrieve",
        "plan",
        "edit",
        "debug",
        "verify",
        "summarize",
        "lesson_extract",
    ] = "plan",
    step_index: int = 0,
    evidence_summary: dict[str, Any] | None = None,
    route_decision_id: str | None = None,
    validation_results: list[dict[str, Any]] | None = None,
    rubric_status: Literal["not_run", "pass", "warn", "fail"] = "not_run",
    required_verifiers: list[str] | None = None,
    protected_file_match: bool = False,
    repeated_failure_signatures: list[str] | None = None,
    diff_line_count: int = 0,
    human_accepted: bool | None = None,
    benchmark_accepted: bool | None = None,
) -> dict[str, Any]:
    """[DEV] Route op-dispatch: op=decide computes a route; op=verify checks the outcome."""
    if stub := _check_dev_mode("route"):
        return {
            "id": "dev-mode-stub",
            "task_type": task_type,
            "step_type": step_type,
            "risk_level": risk_level,
            "action": "proceed",
            "rationale": stub,
        }

    rt = _runtime()
    led = _get_ledger()

    if changed_files is None:
        changed_files = []
    if validation_results is None:
        validation_results = []
    if required_verifiers is None:
        required_verifiers = []
    if repeated_failure_signatures is None:
        repeated_failure_signatures = []
    if evidence_summary is None:
        evidence_summary = {}

    if op == "decide":
        led.record_tool_call(
            "route",
            {
                "op": op,
                "task_type": task_type,
                "risk_level": risk_level,
                "changed_files": changed_files,
                "domain": domain,
                "step_type": step_type,
                "step_index": step_index,
            },
        )
        decision = rt.route_decide(
            user_goal=user_goal,
            repo_root=repo_root,
            task_type=task_type,
            risk_level=risk_level,
            changed_files=changed_files,
            domain=domain,
            step_type=step_type,
            step_index=step_index,
            session_id=led.session_id,
            evidence_summary=evidence_summary,
            ledger=led,
        )
        return to_jsonable(decision)

    if route_decision_id is None:
        raise ValueError("route_decision_id is required when op='verify'")
    led.record_tool_call(
        "route",
        {
            "op": op,
            "route_decision_id": route_decision_id,
            "changed_files": changed_files,
            "rubric_status": rubric_status,
            "required_verifiers": required_verifiers,
            "protected_file_match": protected_file_match,
            "repeated_failure_signatures": repeated_failure_signatures,
            "diff_line_count": diff_line_count,
            "human_accepted": human_accepted,
            "benchmark_accepted": benchmark_accepted,
        },
    )
    envelope = rt.core_runtime.quality_router.verify(
        route_decision_id=route_decision_id,
        session_id=led.session_id,
        changed_files=changed_files,
        validation_results=validation_results,
        rubric_status=rubric_status,
        required_verifiers=required_verifiers,
        protected_file_match=protected_file_match,
        repeated_failure_signatures=repeated_failure_signatures,
        diff_line_count=diff_line_count,
        human_accepted=human_accepted,
        benchmark_accepted=benchmark_accepted,
    )
    return to_jsonable(envelope)


@mcp_tool(name="rescue", is_dev=True)
def tool_rescue_failure(
    task: str,
    error: str,
    domain: str | None = None,
    files: list[str] | None = None,
    recent_actions: list[str] | None = None,
) -> dict[str, Any]:
    """[DEV] Suggest a rescue procedure for a repeated failure."""
    if stub := _check_dev_mode("rescue"):
        return {
            "cluster_id": "dev-mode-stub",
            "domain": domain or "unknown",
            "rescue_type": "none",
            "procedure": [],
            "rationale": stub,
        }

    if recent_actions is None:
        recent_actions = []
    if files is None:
        files = []
    rt = _runtime()
    led = _get_ledger()
    _match_mcp_lexical({"task": task, "error": error})
    led.record_tool_call(
        "rescue_failure",
        {
            "task": task,
            "error": error,
            "domain": domain,
            "files": files,
            "recent_actions": recent_actions,
        },
    )

    result = rt.rescue_failure(
        task=task,
        error=error,
        files=files,
        domain=domain,
        recent_actions=recent_actions,
    )
    payload = to_jsonable(result)
    with contextlib.suppress(Exception):
        from atelier.core.service.telemetry import emit_product
        from atelier.core.service.telemetry.schema import hash_identifier

        matched = list(payload.get("matched_blocks", []) or []) if isinstance(payload, dict) else []
        emit_product(
            "rescue_offered",
            cluster_id_hash=hash_identifier(str(matched[0] if matched else "unmatched_rescue")),
            rescue_type="reasonblock" if matched else "summary",
            session_id=_get_product_session_id(),
        )

    # Lemma-style failure incident analysis from prior failed traces.
    with contextlib.suppress(Exception):
        analysis = rt.core_runtime.analyze_failure_for_error(
            task=task,
            error=error,
            domain=domain,
            lookback=200,
        )
        payload["analysis"] = analysis
        incident = analysis.get("incident") if isinstance(analysis, dict) else None
        if isinstance(incident, dict):
            root_cause = incident.get("root_cause_hypothesis", "")
            if isinstance(root_cause, str) and root_cause:
                led.record(
                    "note",
                    "failure_analysis",
                    {
                        "root_cause": root_cause,
                        "fingerprint": incident.get("fingerprint"),
                        "count": incident.get("count"),
                    },
                )

    return payload


@mcp_tool(name="trace")
def tool_record_trace(
    agent: str,
    domain: str,
    task: str,
    status: Literal["success", "failed", "partial"],
    files_touched: list[str] | None = None,
    tools_called: list[Any] | None = None,
    commands_run: list[str] | None = None,
    errors_seen: list[str] | None = None,
    diff_summary: str = "",
    output_summary: str = "",
    validation_results: list[Any] | None = None,
    prompt: str | None = None,
    response: str | None = None,
    bash_outputs: list[Any] | None = None,
    tool_outputs: list[Any] | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    trace_confidence: str | None = None,
    capture_sources: list[str] | None = None,
    missing_surfaces: list[str] | None = None,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record an observable trace from an agent run."""
    from atelier.core.foundation.redaction import redact, redact_list

    if validation_results is None:
        validation_results = []
    if errors_seen is None:
        errors_seen = []
    if commands_run is None:
        commands_run = []
    if tools_called is None:
        tools_called = []
    if files_touched is None:
        files_touched = []
    if bash_outputs is None:
        bash_outputs = []
    if tool_outputs is None:
        tool_outputs = []
    if capture_sources is None:
        capture_sources = []
    if missing_surfaces is None:
        missing_surfaces = []
    if event_payload is None:
        event_payload = {}
    rt = _runtime()
    led = _get_ledger()
    rtc = _get_realtime_context()

    def _redact_json_strings(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [_redact_json_strings(item) for item in value]
        if isinstance(value, dict):
            return {str(key): _redact_json_strings(item) for key, item in value.items()}
        return value

    def _normalize_tool_calls(items: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                normalized.append({"name": redact(item), "args_hash": "", "count": 1})
                continue
            if isinstance(item, dict):
                raw_count = item.get("count") or 1
                with contextlib.suppress(TypeError, ValueError):
                    raw_count = int(raw_count)
                if not isinstance(raw_count, int):
                    raw_count = 1
                tool_call = {
                    "name": redact(str(item.get("name") or item.get("tool") or "unknown")),
                    "args_hash": redact(str(item.get("args_hash") or "")),
                    "count": raw_count,
                }
                if "args" in item:
                    tool_call["args"] = _redact_json_strings(item["args"])
                if isinstance(item.get("result_summary"), str):
                    tool_call["result_summary"] = redact(item["result_summary"])
                normalized.append(tool_call)
                continue
            normalized.append({"name": redact(str(item)), "args_hash": "", "count": 1})
        return normalized

    def _coerce_validation_passed(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"pass", "passed", "success", "successful", "ok", "true"}:
                return True
            if lowered in {"fail", "failed", "failure", "error", "errored", "false"}:
                return False
        return False

    def _normalize_validation_results(items: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("name") or item.get("check") or "validation"
                detail = item.get("detail") or item.get("output") or ""
                passed = item.get("passed")
                if passed is None:
                    passed = item.get("status")
                normalized.append(
                    {
                        "name": redact(str(name)),
                        "passed": _coerce_validation_passed(passed),
                        "detail": redact(str(detail)),
                    }
                )
                continue
            text = redact(str(item))
            lowered = text.lower()
            passed = not any(token in lowered for token in ("fail", "error", "not run"))
            normalized.append({"name": text, "passed": passed, "detail": ""})
        return normalized

    # Derive host label from agent string and environment
    def _derive_host(a: str) -> str:
        al = a.lower()
        if "gemini" in al or os.environ.get("GEMINI_CLI"):
            return "gemini"
        if "copilot" in al or os.environ.get("COPILOT_CLI"):
            return "copilot"
        if "codex" in al or os.environ.get("CODEX_CLI"):
            return "codex"
        if "opencode" in al or os.environ.get("OPENCODE_CLI"):
            return "opencode"
        if "claude" in al or os.environ.get("CLAUDE_CODE"):
            return "claude"

        # Default to the agent name if no known host environment is detected
        return "atelier" if al.startswith("atelier:") else al

    # Validate: full_live requires capture_sources to include hooks/live
    _VALID_CONFIDENCE = {"full_live", "mcp_live", "wrapper_live", "imported", "manual"}
    if trace_confidence is not None and trace_confidence not in _VALID_CONFIDENCE:
        trace_confidence = None
    if trace_confidence == "full_live" and not any(
        s in ("hooks", "live_hooks", "plugin_hooks") for s in capture_sources
    ):
        # Downgrade silently to mcp_live; caller must include hooks in capture_sources
        trace_confidence = "mcp_live"
        if "hooks" not in missing_surfaces:
            missing_surfaces = [*list(missing_surfaces), "hooks"]

    payload = {
        "agent": agent,
        "domain": domain,
        "task": redact(task),
        "status": status,
        "files_touched": redact_list([str(v) for v in files_touched]),
        "tools_called": _normalize_tool_calls(tools_called),
        "commands_run": redact_list([str(v) for v in commands_run]),
        "errors_seen": redact_list([str(v) for v in errors_seen]),
        "diff_summary": redact(diff_summary),
        "output_summary": redact(output_summary),
        "session_id": session_id or run_id or led.session_id,
        "host": _derive_host(agent),
        "trace_confidence": trace_confidence,
        "capture_sources": capture_sources,
        "missing_surfaces": missing_surfaces,
    }
    payload["validation_results"] = _normalize_validation_results(validation_results)

    if prompt:
        rtc.record_prompt_response(redact(prompt), redact(response or ""))
    if bash_outputs:
        for item in bash_outputs:
            if isinstance(item, dict):
                command = str(item.get("command", ""))
                stdout = redact(str(item.get("stdout", "")))
                stderr = redact(str(item.get("stderr", "")))
                ok = bool(item.get("ok", True))
                rtc.record_bash_output(command, stdout=stdout, stderr=stderr, ok=ok)
            else:
                rtc.record_bash_output("bash", stdout=redact(str(item)), ok=True)
    if tool_outputs:
        for item in tool_outputs:
            rtc.record_tool_output("external_tool", {"output": redact(str(item))})

    raw_artifacts: list[str] = []
    if prompt or response or bash_outputs or tool_outputs:
        source_session_id = (
            os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("CODEX_SESSION_ID")
            or os.environ.get("OPENCODE_SESSION_ID")
            or "unknown"
        )
        artifact_content = {
            "prompt": redact(prompt or ""),
            "response": redact(response or ""),
            "bash_outputs": bash_outputs,
            "tool_outputs": tool_outputs,
        }
        redacted_content = json.dumps(artifact_content, ensure_ascii=False, indent=2)
        artifact_id = f"trace-ctx-{Trace.make_id(task, agent)}"
        digest = sha256(redacted_content.encode("utf-8", errors="replace")).hexdigest()
        artifact = RawArtifact(
            id=artifact_id,
            source="mcp",
            source_session_id=source_session_id,
            kind="trace.context.json",
            relative_path=f"{artifact_id}.json",
            content_path=f"raw/mcp/{source_session_id}/{artifact_id}.json",
            sha256_original=digest,
            sha256_redacted=digest,
            byte_count_original=len(redacted_content.encode("utf-8")),
            byte_count_redacted=len(redacted_content.encode("utf-8")),
            redacted=True,
        )
        with contextlib.suppress(Exception):
            rt.store.record_raw_artifact(artifact, redacted_content)
            raw_artifacts.append(artifact_id)

    if raw_artifacts:
        payload["raw_artifact_ids"] = raw_artifacts

    if event_type:
        led.record("note", f"event:{redact(event_type)}", _redact_json_strings(event_payload))

    if "id" not in payload:
        payload["id"] = Trace.make_id(task, agent)

    trace = Trace.model_validate(payload)
    rt.store.record_trace(trace)

    led.close(status=status)
    led.persist()

    _write_session_state({"trace_recorded": True})
    rtc.persist()

    # Emit to Langfuse if configured (fail-open)
    from atelier.gateway.integrations.langfuse import emit_trace as _lf_emit

    _lf_emit(payload)

    # Kick off an immediate background consolidation tick so knowledge blocks
    # are extracted from this trace without waiting for the daemon's next cycle.
    import threading

    threading.Thread(
        target=_run_worker_tick_safe,
        args=(_atelier_root(),),
        daemon=True,
    ).start()

    return {
        "id": trace.id,
        "session_id": led.session_id,
        "event_recorded": bool(event_type),
        "realtime_context": rtc.snapshot(),
    }


@mcp_tool(name="verify", is_dev=True)
def tool_run_rubric_gate(rubric_id: str, checks: dict[str, Any]) -> Any:
    """[DEV] Evaluate agent results against a domain rubric. Returns pass|warn|fail with per-check detail."""
    if stub := _check_dev_mode("verify"):
        return {
            "rubric_id": rubric_id,
            "status": "pass",
            "results": {},
            "summary": stub,
        }

    rt = _runtime()
    led = _get_ledger()
    led.record_tool_call("run_rubric_gate", {"rubric_id": rubric_id, "checks": checks})

    rubric = rt.store.get_rubric(rubric_id)
    if rubric is None:
        raise ValueError(f"rubric not found: {rubric_id}")

    if rubric_id not in led.active_rubrics:
        led.active_rubrics.append(rubric_id)

    result = run_rubric(rubric, checks)
    led.record("rubric_run", f"Rubric {rubric_id} status: {result.status}", to_jsonable(result))
    return to_jsonable(result)


def _compress_context(session_id: str | None = None) -> Any:
    """Compress the current ledger state into a compact prompt block for context continuation."""
    from atelier.infra.runtime.context_compressor import ContextCompressor

    led = _get_ledger()
    rtc = _get_realtime_context()
    state = ContextCompressor().compress(led)
    return {
        "preserved": {
            "latest_error": state.error_fingerprints[-1] if state.error_fingerprints else None,
            "active_rubrics": led.active_rubrics,
            "active_reasonblocks": led.active_reasonblocks,
        },
        "prompt_block": state.to_prompt_block(),
        "realtime": rtc.snapshot(),
    }


def _memory_upsert_block(
    agent_id: str,
    label: str,
    value: str,
    limit_chars: int = 8000,
    description: str = "",
    read_only: bool = False,
    pinned: bool = False,
    metadata: dict[str, Any] | None = None,
    expected_version: int | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Create or update an editable memory block."""
    clean_value = _redact_memory_input(value, "value")
    clean_description = _redact_memory_input(description, "description")
    store = _memory_store()
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
        metadata=metadata or {},
        pinned=pinned,
        version=version,
        current_history_id=existing.current_history_id if existing else None,
        created_at=seed.created_at,
    )
    from atelier.core.capabilities.memory_arbitration import arbitrate

    decision = arbitrate(block, store, make_embedder())
    target = None
    if decision.target_block_id:
        for item in store.list_blocks(agent_id, include_tombstoned=True, limit=500):
            if item.id == decision.target_block_id:
                target = item
                break

    if decision.op == "NOOP" and target is not None:
        stored = target
    elif decision.op == "UPDATE" and target is not None:
        stored = store.upsert_block(
            target.model_copy(update={"value": decision.merged_value or clean_value}),
            actor=actor or f"agent:{agent_id}",
            reason=decision.reason,
        )
    elif decision.op == "DELETE" and target is not None:
        store.tombstone_block(target.id, deprecated_by_block_id=block.id, reason=decision.reason)
        stored = store.upsert_block(block, actor=actor or f"agent:{agent_id}", reason=decision.reason)
    else:
        stored = store.upsert_block(block, actor=actor or f"agent:{agent_id}")
    return {
        "id": stored.id,
        "version": stored.version,
        "arbitration": decision.model_dump(mode="json"),
    }


def _memory_get_block(agent_id: str, label: str) -> dict[str, Any] | None:
    """Fetch one editable memory block by agent and label."""
    block = _memory_store().get_block(agent_id, label)
    return block.model_dump(mode="json") if block is not None else None


def _memory_archive(
    agent_id: str,
    text: str,
    source: str,
    source_ref: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Archive long-term memory text for later recall."""
    passage = _archival_recall().archive(
        agent_id=agent_id,
        text=text,
        source=source,  # type: ignore[arg-type]
        source_ref=source_ref,
        tags=tags or [],
    )
    return {"id": passage.id, "dedup_hit": passage.dedup_hit}


def _memory_recall(
    agent_id: str,
    query: str,
    top_k: int = 5,
    tags: list[str] | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Recall relevant archival memory passages."""
    since_dt = datetime.fromisoformat(since) if since else None
    passages, recall = _archival_recall().recall(
        agent_id=agent_id,
        query=query,
        top_k=top_k,
        tags=tags or None,
        since=since_dt,
    )
    return {
        "passages": [
            {
                "id": passage.id,
                "text": passage.text,
                "source_ref": passage.source_ref,
                "tags": passage.tags,
                "legacy_stub": passage.embedding_provenance == "legacy_stub",
            }
            for passage in passages
        ],
        "recall_id": recall.id,
    }


@mcp_tool(name="memory", is_dev=True)
def tool_memory(
    op: Literal["block_upsert", "block_get", "archive", "recall", "transcript_recall", "summarize"],
    agent_id: str | None = None,
    label: str | None = None,
    value: str | None = None,
    limit_chars: int = 8000,
    description: str = "",
    read_only: bool = False,
    pinned: bool = False,
    metadata: dict[str, Any] | None = None,
    expected_version: int | None = None,
    actor: str | None = None,
    text: str | None = None,
    source: str | None = None,
    source_ref: str = "",
    tags: list[str] | None = None,
    query: str | None = None,
    top_k: int = 5,
    since: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """[DEV] Memory op-dispatch: block_upsert, block_get, archive, recall, transcript_recall, or summarize."""
    if stub := _check_dev_mode("memory"):
        return {"context": stub, "passages": [], "text": stub}

    def require(name: str, current: str | None) -> str:
        if not current:
            raise ValueError(f"{name} is required for memory op={op}")
        return current

    if op == "block_upsert":
        return _memory_upsert_block(
            agent_id=require("agent_id", agent_id),
            label=require("label", label),
            value=require("value", value),
            limit_chars=limit_chars,
            description=description,
            read_only=read_only,
            pinned=pinned,
            metadata=metadata,
            expected_version=expected_version,
            actor=actor,
        )
    if op == "block_get":
        return _memory_get_block(agent_id=require("agent_id", agent_id), label=require("label", label))
    if op == "archive":
        return _memory_archive(
            agent_id=require("agent_id", agent_id),
            text=require("text", text),
            source=require("source", source),
            source_ref=source_ref,
            tags=tags,
        )
    if op == "recall":
        return _memory_recall(
            agent_id=require("agent_id", agent_id),
            query=require("query", query),
            top_k=top_k,
            tags=tags,
            since=since,
        )
    if op == "transcript_recall":
        from atelier.core.capabilities.local_recall import recall_transcripts

        return recall_transcripts(query=require("query", query), top_k=top_k)
    return _memory_summary(require("session_id", session_id))


@mcp_tool(name="read", is_dev=True)
def tool_smart_read(
    path: str | None = None,
    file_path: str | None = None,
    range: str | None = None,
    expand: bool = False,
    max_lines: int | None = None,
) -> dict[str, Any]:
    """Smart file read with outline-first mode for large Python/TypeScript files."""
    target_path = file_path or path
    if not target_path:
        raise ValueError("provide path or file_path")
    if max_lines is not None and range is None and not expand:
        return cast(dict[str, Any], _core_runtime().smart_read(target_path, max_lines=max_lines))

    cap = SemanticFileMemoryCapability(_atelier_root())
    target = _workspace_path(target_path)
    payload = cap.smart_read(target, range_spec=range, expand=expand)
    return {
        "mode": payload["mode"],
        "cache_hit": bool(payload.get("cache_hit", False)),
        "tokens_saved": int(payload.get("tokens_saved", 0)),
        "outline": payload.get("outline"),
        "content": payload.get("content"),
        "path": payload.get("path", str(target)),
        "range": payload.get("range"),
    }


def _collect_touched_paths(edits: list[dict[str, Any]]) -> list[str]:
    """Extract the file paths referenced in a list of edit descriptors."""
    paths: set[str] = set()
    for edit in edits:
        raw = str(edit.get("file_path") or edit.get("path") or "")
        if raw:
            paths.add(raw)
    return sorted(paths)


def _snapshot_paths(paths: list[str]) -> dict[str, str | None]:
    """Read each file's current content into a dict; None if file does not exist."""
    snap: dict[str, str | None] = {}
    for p in paths:
        fp = Path(p)
        try:
            snap[p] = fp.read_text(encoding="utf-8") if fp.exists() else None
        except Exception:
            snap[p] = None
    return snap


def _compute_and_record_diffs(
    snapshots: dict[str, str | None],
    repo_root: str,
) -> None:
    """Compute unified diffs from *snapshots* vs current file content and record them in the ledger."""
    import difflib

    led = _get_ledger()
    for path, old_content in snapshots.items():
        fp = Path(path)
        try:
            new_content = fp.read_text(encoding="utf-8") if fp.exists() else None
        except Exception:
            new_content = None
        if old_content == new_content:
            continue
        if old_content is None and new_content is None:
            continue
        diff_lines = list(
            difflib.unified_diff(
                (old_content or "").splitlines(keepends=True),
                (new_content or "").splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        diff_text = "".join(diff_lines) if diff_lines else ""
        if diff_text:
            led.record_file_event(path=path, event="edit", diff=diff_text)
        else:
            led.record_file_event(path=path, event="edit")


@mcp_tool(name="edit", is_dev=True)
def tool_smart_edit(
    edits: list[dict[str, Any]],
    atomic: bool = True,
) -> dict[str, Any]:
    """Apply many mechanical edits across files in one deterministic call.

    Legacy descriptors with ``op`` are routed through the deterministic batch
    editor. Rich descriptors with ``file_path``, notebook cell operations, or
    overwrite semantics use the native rich editor and write each touched file
    once after sequential in-memory edits.
    """
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())

    # Snapshot file contents before applying edits so we can compute diffs
    paths = _collect_touched_paths(edits)
    snapshots = _snapshot_paths(paths)

    use_legacy_batch = edits and all(
        "op" in edit and "file_path" not in edit and "cell_action" not in edit for edit in edits
    )
    if not use_legacy_batch:
        from atelier.core.capabilities.tool_supervision.rich_edit import apply_rich_edits

        result = apply_rich_edits(edits, atomic=atomic, repo_root=Path(workspace))
        if not result.get("failed") and not result.get("rolled_back"):
            _compute_and_record_diffs(snapshots, workspace)
        return result

    from atelier.core.capabilities.tool_supervision.batch_edit import apply_batch_edit

    result = apply_batch_edit(
        edits,
        atomic=atomic,
        repo_root=Path(workspace),
    )
    if not result.get("failed") and not result.get("rolled_back"):
        _compute_and_record_diffs(snapshots, workspace)
    return result


@mcp_tool(name="sql", is_dev=True)
def tool_sql(
    action: str,
    name: str | list[str] | None = None,
    prefix: str | None = None,
    sql: str | None = None,
    queries: list[dict[str, str]] | None = None,
    schema_name: str | None = None,
    connection_string: str | None = None,
    dialect: str | None = None,
    max_rows: int = 500,
    timeout_ms: int = 30_000,
    auto_limit: bool = True,
    allow_writes: bool = True,
) -> dict[str, Any]:
    """SQL op-dispatch for connect, schema, table, lint, and bounded query batching."""
    from atelier.core.capabilities.tool_supervision.sql_tool import sql_tool

    return sql_tool(
        action=action,
        name=name,
        prefix=prefix,
        sql=sql,
        queries=queries,
        schema_name=schema_name,
        connection_string=connection_string,
        dialect=dialect,
        max_rows=max_rows,
        timeout_ms=timeout_ms,
        auto_limit=auto_limit,
        allow_writes=allow_writes,
        repo_root=os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd()),
    )


def _compact_advise(session_id: str | None = None) -> dict[str, Any]:
    """Advise when to compact and what context to preserve.

    Returns a manifest with:
    - should_compact: bool (true if utilisation >= 60%)
    """
    try:
        led = _get_ledger()
        if session_id:
            led.session_id = session_id

        # Estimate tokens used: token_count from ledger + events
        tokens_used = led.token_count
        # Rough estimation: each event ~50 tokens average
        event_tokens = max(0, len(led.events) * 10)
        tokens_used += event_tokens

        # Claude 3.5 Sonnet context window is 200K
        context_window = 200_000
        utilisation_pct = round(100.0 * tokens_used / context_window, 1)

        # Determine if compaction is advised
        should_compact = utilisation_pct >= 60.0

        # Collect preserve_blocks: top active ReasonBlocks from ledger
        preserve_blocks = list(set(led.active_reasonblocks))[:3]

        # Collect pin_memory: pinned MemoryBlocks for this run's agent
        pin_memory: list[str] = []
        try:
            store = _memory_store()
            agent_id = led.agent or "claude"
            pinned = store.list_pinned_blocks(agent_id=agent_id)
            pin_memory = [b.id for b in pinned][:5]
        except Exception:
            logger.warning(
                "Suppressed exception at mcp_server.py:1773",
                exc_info=True,
            )

        # Collect open_files: last 5 files touched
        open_files = led.files_touched[-5:] if led.files_touched else []

        # Build suggested prompt
        suggested_prompt = (
            f"Compact this conversation. Context utilisation: {utilisation_pct}%. "
            f"Please preserve these ReasonBlocks: {', '.join(preserve_blocks) or '(none yet)'}. "
            f"Recently edited files: {', '.join(open_files) or '(none)'}"
        )

        # Persist manifest to disk
        try:
            root = _atelier_root()
            run_dir = root / "runs" / led.session_id
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = run_dir / "compact_manifest.json"
            manifest = {
                "created_at": datetime.now(UTC).isoformat(),
                "session_id": led.session_id,
                "should_compact": should_compact,
                "utilisation_pct": utilisation_pct,
                "preserve_blocks": preserve_blocks,
                "pin_memory": pin_memory,
                "open_files": open_files,
                "suggested_prompt": suggested_prompt,
            }
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception:
            logger.warning(
                "Suppressed exception at mcp_server.py:1803",
                exc_info=True,
            )

        return {
            "should_compact": should_compact,
            "utilisation_pct": utilisation_pct,
            "preserve_blocks": preserve_blocks,
            "pin_memory": pin_memory,
            "open_files": open_files,
            "suggested_prompt": suggested_prompt,
        }
    except Exception:
        # Fail-open: return conservative defaults
        return {
            "should_compact": False,
            "utilisation_pct": 0.0,
            "preserve_blocks": [],
            "pin_memory": [],
            "open_files": [],
            "suggested_prompt": "Unable to compute compaction advice; proceed with default compaction.",
        }


def _memory_summary(session_id: str) -> dict[str, Any]:
    """Run the sleeptime summarizer for a given run and return a summary.

    Input:
        session_id: The run identifier to summarize.

    Output:
        tokens_pre, tokens_post, summary_md, evicted_event_ids,
        archived_passage_ids, strategy
    """
    try:
        from atelier.core.capabilities.context_compression.capability import (
            ContextCompressionCapability,
        )

        led = _get_ledger()
        if session_id:
            led.session_id = session_id

        cap = ContextCompressionCapability()
        result = cap.compress_with_sleeptime(led)

        summary_lines = [f"## Sleeptime Summary — run `{led.session_id}`", ""]
        summary_lines.append(f"- Tokens before: {result.chars_before // 4}")
        summary_lines.append(f"- Tokens after:  {result.chars_after // 4}")
        summary_lines.append(f"- Reduction:     {result.reduction_pct}%")
        if result.dropped:
            summary_lines.append("")
            summary_lines.append("### Evicted events")
            for d in result.dropped[:10]:
                summary_lines.append(f"- [{d.kind}] {d.summary[:100]}")

        return {
            "tokens_pre": result.chars_before // 4,
            "tokens_post": result.chars_after // 4,
            "summary_md": "\n".join(summary_lines),
            "evicted_event_ids": [d.kind for d in result.dropped],
            "archived_passage_ids": [],
            "strategy": "tfidf",
        }
    except Exception as exc:
        return {"error": str(exc)}


def _code_context_engine(repo_root: str = ".") -> Any:
    from atelier.core.capabilities.code_context import CodeContextEngine

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    root = Path(repo_root)
    resolved = root if root.is_absolute() else Path(workspace) / root
    return CodeContextEngine(resolved)


@mcp_tool(name="atelier_code_index", is_dev=True)
def tool_atelier_code_index(
    repo_root: str = ".",
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
) -> dict[str, Any]:
    """Index a repository/folder into Atelier's SQLite FTS5 symbol store."""
    return cast(
        dict[str, Any],
        _code_context_engine(repo_root)
        .index_repo(include_globs=include_globs, exclude_globs=exclude_globs)
        .model_dump(mode="json"),
    )


@mcp_tool(name="atelier_code_search", is_dev=True)
def tool_atelier_code_search(
    query: str,
    repo_root: str = ".",
    limit: int = 20,
    kind: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """BM25/FTS symbol search over the Atelier code index."""
    results = _code_context_engine(repo_root).search_symbols(
        query,
        limit=limit,
        kind=kind,
        language=language,
    )
    return {"items": [item.model_dump(mode="json") for item in results]}


@mcp_tool(name="atelier_code_symbol", is_dev=True)
def tool_atelier_code_symbol(
    repo_root: str = ".",
    symbol_id: str | None = None,
    qualified_name: str | None = None,
    symbol_name: str | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Retrieve exact symbol source by byte offsets."""
    return cast(
        dict[str, Any],
        _code_context_engine(repo_root).get_symbol(
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=file_path,
        ),
    )


@mcp_tool(name="atelier_code_outline", is_dev=True)
def tool_atelier_code_outline(
    repo_root: str = ".",
    file_path: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Return compact file/repo outline from the code index."""
    return cast(
        dict[str, Any],
        _code_context_engine(repo_root).file_outline(file_path=file_path, limit=limit),
    )


def _run_shell_tool(
    command: str,
    timeout: int = 30,
    cwd: str | None = None,
    max_lines: int = 200,
) -> dict[str, Any]:
    """Execute a shell command and return compact structured output."""
    from atelier.core.capabilities.tool_supervision.bash_exec import run_command

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    result = run_command(
        command,
        cwd=cwd or workspace,
        timeout=timeout,
        max_lines=max_lines,
    )
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "truncated": result.truncated,
        "lines_omitted": result.lines_omitted,
    }


@mcp_tool(name="atelier_code_context", is_dev=True)
def tool_atelier_code_context(
    task: str,
    repo_root: str = ".",
    seed_files: list[str] | None = None,
    budget_tokens: int = 4000,
    max_symbols: int = 8,
) -> dict[str, Any]:
    """Build a task-specific token-budgeted context bundle."""
    return cast(
        dict[str, Any],
        _code_context_engine(repo_root)
        .context_pack(
            task=task,
            seed_files=seed_files,
            budget_tokens=budget_tokens,
            max_symbols=max_symbols,
        )
        .model_dump(mode="json"),
    )


@mcp_tool(name="atelier_code_impact", is_dev=True)
def tool_atelier_code_impact(repo_root: str = ".", file_path: str = "") -> dict[str, Any]:
    """Return importers, blast radius, tests, and approximate dead-code candidates."""
    if not file_path:
        raise ValueError("file_path is required")
    return cast(dict[str, Any], _code_context_engine(repo_root).impact(file_path).model_dump(mode="json"))


@mcp_tool(name="search", is_dev=True)
def tool_smart_search(
    query: str | None = None,
    path: str = ".",
    mode: Literal["chunks", "full", "map"] = "chunks",
    max_files: int = 10,
    max_chars_per_file: int = 2000,
    include_outline: bool = True,
    seed_files: list[str] | None = None,
    budget_tokens: int = 2000,
    content_regex: str | None = None,
    file_glob_patterns: list[str] | None = None,
    output_mode: Literal[
        "file_paths_with_content", "file_paths_only", "file_paths_with_match_count"
    ] = "file_paths_with_content",
    lines_before: int = 0,
    lines_after: int = 0,
    ignore_case: bool = False,
    type: str | None = None,
    file_limit: int | None = None,
    lines_per_file: int | None = 500,
    if_modified_since: str | None = None,
    max_line_length: int | None = 1000,
    multiline: bool = False,
    summary: bool | None = None,
) -> dict[str, Any]:
    """Smart search/read with ranking plus a native glob/regex media-aware mode.

    Pass ``query`` for query-driven search; pass ``seed_files`` with ``mode="map"``
    for repo-map mode.
    """
    if (
        content_regex is not None
        or file_glob_patterns is not None
        or type is not None
        or if_modified_since is not None
        or lines_before
        or lines_after
        or output_mode != "file_paths_with_content"
        or summary is not None
        or multiline
    ):
        from atelier.core.capabilities.tool_supervision.native_search import search_workspace

        return search_workspace(
            path=path,
            content_regex=content_regex or query,
            file_glob_patterns=file_glob_patterns,
            output_mode=output_mode,
            lines_before=lines_before,
            lines_after=lines_after,
            ignore_case=ignore_case,
            type=type,
            file_limit=file_limit or max_files,
            lines_per_file=lines_per_file,
            if_modified_since=if_modified_since,
            max_line_length=max_line_length,
            multiline=multiline,
            summary=summary,
            repo_root=os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd()),
        )

    if query is None:
        raise ValueError("query is required unless native search selectors are provided")
    from atelier.core.capabilities.tool_supervision.smart_search import smart_search

    return smart_search(
        query=query,
        path=path,
        mode=mode,
        max_files=max_files,
        max_chars_per_file=max_chars_per_file,
        include_outline=include_outline,
        seed_files=seed_files,
        budget_tokens=budget_tokens,
    )


def _compact_tool_output(
    content: str,
    content_type: str = "unknown",
    budget_tokens: int = 500,
    recovery_hint: str | None = None,
) -> dict[str, Any]:
    """Compact large tool output with deterministic or Ollama-backed methods."""
    from atelier.core.capabilities.tool_supervision.compact_output import compact

    result = compact(
        content=content,
        content_type=content_type,
        budget_tokens=budget_tokens,
        recovery_hint=recovery_hint,
    )
    return result.model_dump(mode="json")


@mcp_tool(name="compact", is_dev=True)
def tool_compact(
    op: Literal["output", "session", "advise"],
    content: str = "",
    content_type: str = "unknown",
    budget_tokens: int = 500,
    recovery_hint: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """[DEV] Compact op-dispatch: output, session, or advise."""
    if stub := _check_dev_mode("compact"):
        return {"prompt_block": stub, "output": stub, "advice": stub}

    if op == "output":
        return _compact_tool_output(
            content=content,
            content_type=content_type,
            budget_tokens=budget_tokens,
            recovery_hint=recovery_hint,
        )
    if op == "session":
        return cast(dict[str, Any], _compress_context(session_id=session_id))
    return _compact_advise(session_id=session_id)


# --------------------------------------------------------------------------- #
# Remote mode & dispatcher                                                    #
# --------------------------------------------------------------------------- #

# Tools that are routed through the remote HTTP service in MCP remote mode.
_REMOTE_TOOLS = frozenset(
    {
        "task",
        "memory",
        "rescue",
        "trace",
        "verify",
    }
)


@mcp_tool(name="shell", is_dev=True)
def tool_shell(
    command: str,
    timeout: int = 30,
    cwd: str | None = None,
    max_lines: int = 200,
) -> dict[str, Any]:
    """Execute a shell command. Output is ANSI-stripped and line-truncated for token efficiency."""
    return _run_shell_tool(command, timeout=timeout, cwd=cwd, max_lines=max_lines)


_remote_client: Any = None


def _get_remote_client() -> Any:
    global _remote_client
    if _remote_client is None:
        from atelier.gateway.adapters.remote_client import RemoteClient

        _remote_client = RemoteClient()
    return _remote_client


def _dispatch_remote(name: str, args: dict[str, Any]) -> dict[str, Any]:
    client = _get_remote_client()
    import typing

    if name == "task":
        return typing.cast(dict[str, Any], client.get_task_context(args))
    if name == "memory":
        return typing.cast(dict[str, Any], client.memory(args))
    if name == "rescue":
        return typing.cast(dict[str, Any], client.rescue_failure(args))
    if name == "trace":
        return typing.cast(dict[str, Any], client.record_trace(args))
    if name == "verify":
        return typing.cast(dict[str, Any], client.run_rubric_gate(args))
    raise ValueError(f"tool not supported in remote mode: {name}")


# --------------------------------------------------------------------------- #
# MCP Protocol Handling                                                       #
# --------------------------------------------------------------------------- #


def _lever_for_tool(tool_name: str) -> str:
    lowered = tool_name.strip().lower().replace("-", "_").replace(" ", "_")
    if lowered in {"read", "search"} or lowered.endswith("_read") or lowered.endswith("_search"):
        return "search_read"
    if lowered == "edit" or lowered.endswith("_edit"):
        return "batch_edit"
    if lowered == "sql" or lowered.endswith("_sql"):
        return "sql_batch"
    if lowered == "compact" or lowered.endswith("_compact"):
        return "compact_lifecycle"
    if lowered == "memory" or lowered.endswith("_memory"):
        return "scoped_recall"
    if lowered == "task" or lowered.endswith("_task"):
        return "reasonblock_inject"
    return lowered or "unknown"


def _live_savings_cost_usd(model: str, savings: dict[str, Any]) -> float:
    from atelier.core.capabilities.pricing import get_model_pricing

    pricing = get_model_pricing(model)
    return pricing.cost_usd(
        input_tokens=int(savings.get("input_tokens_saved", 0) or 0),
        output_tokens=int(savings.get("output_tokens_saved", 0) or 0),
        cache_read_tokens=int(savings.get("cache_read_tokens_saved", 0) or 0),
    )


def _classify_read_savings(
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    tokens_saved: int,
    default_lever: str,
) -> tuple[str, dict[str, Any]]:
    lowered = tool_name.strip().lower().replace("-", "_").replace(" ", "_")
    if lowered not in {"read", "smart_read"}:
        return default_lever, {}

    mode = str(result.get("mode") or "").strip().lower()
    if mode == "outline" and tokens_saved > 0:
        classified = "structure_map"
    elif mode == "range" and tokens_saved > 0:
        classified = "delta_read"
    else:
        classified = default_lever

    path = result.get("path") or args.get("file_path") or args.get("path")
    metadata: dict[str, Any] = {"read_mode": mode or "full"}
    if isinstance(path, str) and path:
        metadata["path"] = path
    range_spec = result.get("range") or args.get("range")
    if isinstance(range_spec, str) and range_spec:
        metadata["range"] = range_spec
    if "cache_hit" in result:
        metadata["cache_hit"] = bool(result.get("cache_hit"))
    return classified, metadata


def _record_context_budget_for_tool(
    tool_name: str, args: dict[str, Any], led: RunLedger, result: dict[str, Any]
) -> None:
    """Record context budget metrics for a tool execution.

    Args:
        tool_name: The name of the tool being executed.
        led: The RunLedger for the current run.
        result: The result from the tool.
    """
    try:
        recorder = _get_context_budget_recorder()
        from atelier.core.capabilities.plugin_runtime import compute_live_savings, equivalent_calls

        model = str(getattr(led, "model", "") or os.environ.get("ATELIER_MODEL") or "_default")
        equivalent = equivalent_calls(tool_name, args if isinstance(args, dict) else {})
        live_savings = compute_live_savings(equivalent, model=model)
        live_tokens_saved = (
            int(live_savings.get("input_tokens_saved", 0) or 0)
            + int(live_savings.get("output_tokens_saved", 0) or 0)
            + int(live_savings.get("cache_read_tokens_saved", 0) or 0)
            + int(live_savings.get("cache_write_tokens_saved", 0) or 0)
        )
        compact_tool_tokens_saved = _extract_compact_output_tokens_saved(result)
        tool_tokens_saved = _extract_tokens_saved(result)
        # Prefer a tool-reported saved-vs-naive token delta when present.
        # The live estimator already captures avoided round trips and time; adding
        # both together inflates event-level token savings for the same tool run.
        tokens_saved = tool_tokens_saved if tool_tokens_saved > 0 else live_tokens_saved
        calls_avoided = int(live_savings.get("calls_saved", 0) or 0)
        base_lever = _lever_for_tool(tool_name)
        lever, savings_metadata = _classify_read_savings(
            tool_name,
            args if isinstance(args, dict) else {},
            result,
            tokens_saved=tokens_saved,
            default_lever=base_lever,
        )

        # Extract lever_savings from result if present, otherwise use empty dict
        raw_lever_savings = result.get("tokens_saved")
        lever_savings = raw_lever_savings.copy() if isinstance(raw_lever_savings, dict) else {}
        if compact_tool_tokens_saved > 0 and not lever_savings:
            lever_savings[f"compact_tool_output:{lever}"] = compact_tool_tokens_saved
        elif tokens_saved > 0:
            if tool_name and tool_name != lever and tool_name not in lever_savings and lever == base_lever:
                lever_savings[tool_name] = tokens_saved
            lever_savings[lever] = max(int(lever_savings.get(lever, 0) or 0), tokens_saved)
        if tool_name:
            lever_savings.setdefault(f"tool:{tool_name}", 0)

        _record_smart_state_savings(tokens_saved=tokens_saved, calls_avoided=calls_avoided)
        if calls_avoided > 0 or tokens_saved > 0:
            event = {
                "at": datetime.now(UTC).isoformat(),
                "session_id": led.session_id,
                "agent": led.agent or _detect_agent(),
                "tool_name": tool_name,
                "lever": lever,
                "equivalent_baseline_calls": equivalent,
                "calls_saved": calls_avoided,
                "time_saved_ms": int(live_savings.get("time_saved_ms", 0) or 0),
                "input_tokens_saved": int(live_savings.get("input_tokens_saved", 0) or 0),
                "output_tokens_saved": int(live_savings.get("output_tokens_saved", 0) or 0),
                "cache_read_tokens_saved": int(live_savings.get("cache_read_tokens_saved", 0) or 0),
                "cache_write_tokens_saved": int(live_savings.get("cache_write_tokens_saved", 0) or 0),
                "live_tokens_saved": live_tokens_saved,
                "tool_tokens_saved": tool_tokens_saved,
                "tokens_saved": tokens_saved,
                "cost_saved_usd": _live_savings_cost_usd(model, live_savings),
                "model": model,
            }
            if savings_metadata:
                event.update(savings_metadata)
            _append_live_savings_event(event)

        # Record the tool execution metrics
        actual_output_tokens = int(result.get("total_tokens", 0) or 0)
        if actual_output_tokens <= 0:
            actual_output_tokens = max(0, len(json.dumps(result, ensure_ascii=False, default=str)) // 4)

        if compact_tool_tokens_saved > 0 and not isinstance(raw_lever_savings, dict):
            recorder.record_compact_tool_output(
                session_id=led.session_id,
                turn_index=max(0, len(led.events) - 1),
                model=model,
                method=lever,
                tokens_in=actual_output_tokens + compact_tool_tokens_saved,
                tokens_out=actual_output_tokens,
            )
        else:
            recorder.record(
                session_id=led.session_id,
                turn_index=max(0, len(led.events) - 1),
                model=model,
                input_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                output_tokens=actual_output_tokens,
                naive_input_tokens=actual_output_tokens + tokens_saved,
                lever_savings=lever_savings,
                tool_calls=1,
            )
    except Exception:
        # Silently fail if context budget recording is not available
        logger.warning(
            "Suppressed exception at mcp_server.py:2231",
            exc_info=True,
        )


def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    rid = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    if method == "initialize":
        _emit_mcp_session_start()
        return _ok(
            rid,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        tools = [
            {
                "name": n,
                "description": _tool_description(s),
                "inputSchema": s.get("inputSchema", {}),
            }
            for n, s in TOOLS.items()
            if _tool_visible_to_llm(n, s)
        ]
        return _ok(rid, {"tools": tools})

    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            return _err(rid, -32601, f"unknown tool: {name}")

        started_at = time.perf_counter()
        remote_routed = name in _REMOTE_TOOLS
        try:
            if not _service_backed_state():
                _match_mcp_lexical(args if isinstance(args, dict) else {})
            if not _service_backed_state():
                rtc = _get_realtime_context()
                rtc.record_tool_input(name, args)
            if remote_routed:
                result = _dispatch_remote(name, args)
            else:
                handler: Callable[[dict[str, Any]], dict[str, Any]] = spec["handler"]
                result = handler(args)

            if not _service_backed_state():
                led = _get_ledger()
                result_text = json.dumps(result, ensure_ascii=False, default=str)
                compact_text = (
                    result_text if len(result_text) <= 1200 else result_text[:600] + "..." + result_text[-600:]
                )
                led.record(
                    "tool_result",
                    f"{name} result",
                    {
                        "tool": name,
                        "output": compact_text,
                        "output_chars": len(result_text),
                    },
                )
                rtc.record_tool_output(name, result)
                rtc.persist()

                # Record context budget metrics
                _record_context_budget_for_tool(name, args if isinstance(args, dict) else {}, led, result)

            if not _service_backed_state():
                with contextlib.suppress(Exception):
                    from atelier.core.service.telemetry import emit_product
                    from atelier.core.service.telemetry.schema import bucket_duration_ms

                    emit_product(
                        "mcp_tool_called",
                        tool_name=name,
                        session_id=_get_product_session_id(),
                        duration_ms_bucket=bucket_duration_ms((time.perf_counter() - started_at) * 1000),
                        ok=True,
                    )

            return _ok(
                rid,
                {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                    "structuredContent": result,
                },
            )
        except Exception as exc:
            if not _service_backed_state():
                rtc = _get_realtime_context()
                rtc.record_tool_error(name, str(exc))
                rtc.persist()
            if not _service_backed_state():
                with contextlib.suppress(Exception):
                    from atelier.core.service.telemetry import emit_product
                    from atelier.core.service.telemetry.schema import bucket_duration_ms

                    emit_product(
                        "mcp_tool_called",
                        tool_name=name,
                        session_id=_get_product_session_id(),
                        duration_ms_bucket=bucket_duration_ms((time.perf_counter() - started_at) * 1000),
                        ok=False,
                    )
            return _err(rid, _tool_error_code(exc), str(exc))

    return _err(rid, -32601, f"unknown method: {method}")


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_error_code(exc: Exception) -> int:
    if isinstance(exc, MemoryConcurrencyError):
        return 409
    if isinstance(exc, MemorySidecarUnavailable):
        return 503
    return -32000


def serve() -> None:
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                sys.stdout.write(json.dumps(_err(None, -32700, f"parse error: {exc}")) + "\n")
                sys.stdout.flush()
                continue
            resp = _handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        _emit_mcp_session_end()
        from atelier.core.service.telemetry import shutdown_otel

        shutdown_otel()


def main() -> None:
    import threading

    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print(f"atelier-mcp {SERVER_VERSION}")
        return
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 < len(argv):
            os.environ["ATELIER_ROOT"] = argv[i + 1]
    # Kick off background daemons immediately at server start, before any
    # tool call arrives.  Runs in daemon threads so they never block serve().
    if not _service_backed_state():
        threading.Thread(target=_autostart_servicectl, args=(_atelier_root(),), daemon=True).start()
    threading.Thread(target=_check_auto_update, daemon=True).start()
    serve()


if __name__ == "__main__":
    main()
