"""MCP server (stdio JSON-RPC) for the Atelier reasoning runtime.

Implements a minimal subset of the Model Context Protocol sufficient for
Codex / Claude Code to discover and call the runtime tools.
"""

from __future__ import annotations

import contextlib
import difflib
import inspect
import json
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

from atelier.core.capabilities.archival_recall import ArchivalRecallCapability
from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability
from atelier.core.foundation.memory_models import MemoryBlock
from atelier.core.foundation.models import RawArtifact, Trace, to_jsonable
from atelier.core.foundation.plan_checker import check_plan
from atelier.core.foundation.redaction import redact
from atelier.core.foundation.rubric_gate import run_rubric
from atelier.gateway.adapters.runtime import ReasoningRuntime
from atelier.infra.embeddings.factory import make_embedder
from atelier.infra.runtime.realtime_context import RealtimeContextManager
from atelier.infra.runtime.run_ledger import RunLedger
from atelier.infra.storage.factory import make_memory_store
from atelier.infra.storage.memory_store import MemoryConcurrencyError, MemorySidecarUnavailable

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "atelier-reasoning"
SERVER_VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# Tool Registry Decorator                                                     #
# --------------------------------------------------------------------------- #

TOOLS: dict[str, dict[str, Any]] = {}


def mcp_tool(
    name: str | None = None, description: str | None = None
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
        # Publish run_id AND atelier_root to session_state so PostToolUse hooks
        # can find the right run file regardless of ATELIER_ROOT in their env.
        _write_session_state(
            {
                "active_run_id": _current_ledger.run_id,
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
        from atelier.core.service.telemetry.identity import new_session_id

        _product_session_id = new_session_id()
    return _product_session_id


def _emit_mcp_session_start() -> None:
    global _product_session_started_at
    if _product_session_started_at is not None:
        return
    from importlib.metadata import PackageNotFoundError, version

    from atelier.core.service.telemetry import emit_product, init_product_telemetry
    from atelier.core.service.telemetry.identity import get_anon_id, platform_payload

    try:
        service_version = version("atelier")
    except PackageNotFoundError:
        service_version = SERVER_VERSION
    init_product_telemetry(service_version=service_version)
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


def _plan_hash(plan: list[str]) -> str:
    return sha256(json.dumps(plan, sort_keys=True).encode("utf-8", errors="replace")).hexdigest()


def _observe_plan_change(plan: list[str], domain: str | None) -> None:
    session_id = _get_product_session_id()
    last_plan = _last_plan_by_session.get(session_id)
    plan_hash = _plan_hash(plan)
    if last_plan is not None and _last_plan_hash_by_session.get(session_id) != plan_hash:
        from atelier.core.service.telemetry import emit_product
        from atelier.core.service.telemetry.schema import bucket_edit_distance

        matcher = difflib.SequenceMatcher(a=last_plan, b=plan)
        added = max(0, len(plan) - len(last_plan))
        removed = max(0, len(last_plan) - len(plan))
        emit_product(
            "plan_modified_by_user",
            domain=domain or "",
            edit_distance_bucket=bucket_edit_distance(1.0 - matcher.ratio()),
            steps_added=added,
            steps_removed=removed,
            session_id=session_id,
        )
    _last_plan_hash_by_session[session_id] = plan_hash
    _last_plan_by_session[session_id] = list(plan)


def _observe_plan_result(result: Any, domain: str | None, plan: list[str]) -> None:
    from atelier.core.service.telemetry import emit_product
    from atelier.core.service.telemetry.schema import hash_identifier

    session_id = _get_product_session_id()
    status = getattr(result, "status", "")
    matched_blocks = list(getattr(result, "matched_blocks", []) or [])
    if status == "blocked":
        plan_hash = _plan_hash(plan)
        if _last_blocked_plan_hash_by_session.get(session_id) == plan_hash:
            emit_product(
                "frustration_signal_behavioral",
                signal_type="plan_resubmitted_unchanged",
                session_id=session_id,
            )
        _last_blocked_plan_hash_by_session[session_id] = plan_hash
        emit_product(
            "plan_check_blocked",
            domain=domain or "",
            blocking_rule_id=hash_identifier(str(matched_blocks[0] if matched_blocks else "blocked")),
            severity="high",
            session_id=session_id,
        )
        return

    _last_blocked_plan_hash_by_session.pop(session_id, None)
    emit_product(
        "plan_check_passed",
        domain=domain or "",
        rule_count=len(matched_blocks),
        session_id=session_id,
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

    def aggregate_run(self, run_id: str) -> Any:
        """No-op aggregate method."""
        return {}


def _session_state_path() -> Path:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    return Path(workspace) / ".atelier" / "session_state.json"


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
        pass


# --------------------------------------------------------------------------- #
# Tool implementations                                                        #
# --------------------------------------------------------------------------- #


def _atelier_root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT", str(Path.home() / ".atelier")))


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
        pass


def _extract_tokens_saved(result: dict[str, Any]) -> int:
    total = 0
    for key in ("tokens_saved", "tokens_saved_vs_naive"):
        value = result.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            total += max(0, value)
            continue
        if isinstance(value, float):
            total += int(max(0.0, value))
            continue
        if isinstance(value, dict):
            total += sum(
                int(max(0.0, float(item_value)))
                for item_value in value.values()
                if isinstance(item_value, (int, float)) and not isinstance(item_value, bool)
            )
    return total


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


@mcp_tool(name="reasoning")
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
    include_environment: bool = False,
    agent_id: str | None = None,
    recall: bool = True,
) -> dict[str, Any]:
    """Retrieve relevant ReasonBlocks for a task and render them as injection context."""
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
            "include_environment": include_environment,
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
    result = payload if isinstance(payload, dict) else {"context": payload}
    if include_run_ledger:
        result["run_ledger"] = led.snapshot()
    if include_environment:
        result["environment"] = {
            "agent": led.agent,
            "atelier_root": str(_atelier_root()),
            "workspace_root": os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd()),
            "cwd": os.getcwd(),
            "run_id": led.run_id,
            "domain": led.domain,
            "environment_id": led.environment.id if led.environment else None,
        }
    return result


@mcp_tool(name="lint")
def tool_check_plan(
    task: str,
    plan: list[str],
    domain: str | None = None,
    files: list[str] | None = None,
    tools: list[str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Validate a proposed agent plan against ReasonBlocks. Returns status pass|warn|blocked."""
    if errors is None:
        errors = []
    if tools is None:
        tools = []
    if files is None:
        files = []
    rt = _runtime()
    led = _get_ledger()
    _match_mcp_lexical({"task": task})
    _observe_plan_change(plan, domain)
    led.set_plan(plan)

    led.record_tool_call(
        "check_plan",
        {
            "task": task,
            "plan": plan,
            "domain": domain,
            "files": files,
            "tools": tools,
            "errors": errors,
        },
    )

    result = check_plan(
        rt.store,
        task=task,
        plan=plan,
        domain=domain,
        files=files,
        tools=tools,
        errors=errors,
    )

    if result.matched_blocks:
        for b_id in result.matched_blocks:
            if b_id not in led.active_reasonblocks:
                led.active_reasonblocks.append(b_id)

    if getattr(result, "status", None) in ("ok", "warn", "pass"):
        _write_session_state({"last_plan_check_ok_ts": time.time()})

    _observe_plan_result(result, domain, plan)

    return to_jsonable(result)


@mcp_tool(name="route")
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
    """Route op-dispatch: op=decide computes a route; op=verify checks the outcome."""
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
            run_id=led.run_id,
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
        run_id=led.run_id,
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


@mcp_tool(name="rescue")
def tool_rescue_failure(
    task: str,
    error: str,
    domain: str | None = None,
    files: list[str] | None = None,
    recent_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Suggest a rescue procedure for a repeated failure."""
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

    # Derive host label from agent string
    def _derive_host(a: str) -> str:
        al = a.lower()
        if "gemini" in al:
            return "gemini"
        if "copilot" in al:
            return "copilot"
        if "codex" in al:
            return "codex"
        if "opencode" in al:
            return "opencode"
        if al.startswith("atelier:") or "claude" in al:
            return "claude"
        return al

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
        "run_id": run_id or led.run_id,
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
    return {
        "id": trace.id,
        "run_id": led.run_id,
        "event_recorded": bool(event_type),
        "realtime_context": rtc.snapshot(),
    }


@mcp_tool(name="verify")
def tool_run_rubric_gate(rubric_id: str, checks: dict[str, Any]) -> Any:
    """Evaluate agent results against a domain rubric. Returns pass|warn|fail with per-check detail."""
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


def _compress_context(run_id: str | None = None) -> Any:
    """Compress the current ledger state into a compact prompt block for context continuation."""
    from atelier.infra.runtime.context_compressor import ContextCompressor

    led = _get_ledger()
    rtc = _get_realtime_context()
    state = ContextCompressor().compress(led)
    return {
        "environment_id": state.environment_id,
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


@mcp_tool(name="memory")
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
    run_id: str | None = None,
) -> dict[str, Any] | None:
    """Memory op-dispatch: block_upsert, block_get, archive, recall, transcript_recall, or summarize."""

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
    return _memory_summary(require("run_id", run_id))


@mcp_tool(name="read")
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


@mcp_tool(name="edit")
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


@mcp_tool(name="sql")
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


def _compact_advise(run_id: str | None = None) -> dict[str, Any]:
    """Advise when to compact and what context to preserve.

    Returns a manifest with:
    - should_compact: bool (true if utilisation >= 60%)
    """
    try:
        led = _get_ledger()
        if run_id:
            led.run_id = run_id

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
            pass  # Fail-open

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
            run_dir = root / "runs" / led.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = run_dir / "compact_manifest.json"
            manifest = {
                "created_at": datetime.now(UTC).isoformat(),
                "run_id": led.run_id,
                "should_compact": should_compact,
                "utilisation_pct": utilisation_pct,
                "preserve_blocks": preserve_blocks,
                "pin_memory": pin_memory,
                "open_files": open_files,
                "suggested_prompt": suggested_prompt,
            }
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception:
            pass  # Fail-open: don't block the tool if persistence fails

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


def _memory_summary(run_id: str) -> dict[str, Any]:
    """Run the sleeptime summarizer for a given run and return a summary.

    Input:
        run_id: The run identifier to summarize.

    Output:
        tokens_pre, tokens_post, summary_md, evicted_event_ids,
        archived_passage_ids, strategy
    """
    try:
        from atelier.core.capabilities.context_compression.capability import (
            ContextCompressionCapability,
        )

        led = _get_ledger()
        if run_id:
            led.run_id = run_id

        cap = ContextCompressionCapability()
        result = cap.compress_with_sleeptime(led)

        summary_lines = [f"## Sleeptime Summary — run `{led.run_id}`", ""]
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


@mcp_tool(name="search")
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
    for repo-map mode (replaces the former atelier_repo_map tool).
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


@mcp_tool(name="compact")
def tool_compact(
    op: Literal["output", "session", "advise"],
    content: str = "",
    content_type: str = "unknown",
    budget_tokens: int = 500,
    recovery_hint: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Compact op-dispatch: output, session, or advise."""
    if op == "output":
        return _compact_tool_output(
            content=content,
            content_type=content_type,
            budget_tokens=budget_tokens,
            recovery_hint=recovery_hint,
        )
    if op == "session":
        return cast(dict[str, Any], _compress_context(run_id=run_id))
    return _compact_advise(run_id=run_id)


# --------------------------------------------------------------------------- #
# Remote mode & dispatcher                                                    #
# --------------------------------------------------------------------------- #

# Tools that are routed through the remote HTTP service in MCP remote mode.
_REMOTE_TOOLS = frozenset(
    {
        "reasoning",
        "lint",
        "rescue",
        "trace",
        "verify",
    }
)

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

    if name == "reasoning":
        return typing.cast(dict[str, Any], client.get_reasoning_context(args))
    if name == "lint":
        return typing.cast(dict[str, Any], client.check_plan(args))
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
    if lowered == "reasoning" or lowered.endswith("_reasoning"):
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
        tool_tokens_saved = _extract_tokens_saved(result)
        tokens_saved = live_tokens_saved + tool_tokens_saved
        calls_avoided = int(live_savings.get("calls_saved", 0) or 0)
        lever = _lever_for_tool(tool_name)

        # Extract lever_savings from result if present, otherwise use empty dict
        lever_savings = result.get("tokens_saved", {})
        if not isinstance(lever_savings, dict):
            lever_savings = {tool_name: tokens_saved} if tokens_saved > 0 else {}
        if tokens_saved > 0:
            lever_savings[lever] = int(lever_savings.get(lever, 0) or 0) + tokens_saved

        _record_smart_state_savings(tokens_saved=tokens_saved, calls_avoided=calls_avoided)
        if calls_avoided > 0 or tokens_saved > 0:
            _append_live_savings_event(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "run_id": led.run_id,
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
                    "tool_tokens_saved": tool_tokens_saved,
                    "tokens_saved": tokens_saved,
                    "cost_saved_usd": _live_savings_cost_usd(model, live_savings),
                    "model": model,
                }
            )

        # Record the tool execution metrics
        actual_output_tokens = max(0, len(json.dumps(result, ensure_ascii=False, default=str)) // 4)
        recorder.record(
            run_id=led.run_id,
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
        pass


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
                "description": s.get("description", ""),
                "inputSchema": s.get("inputSchema", {}),
            }
            for n, s in TOOLS.items()
        ]
        return _ok(rid, {"tools": tools})

    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            return _err(rid, -32601, f"unknown tool: {name}")

        mcp_mode = os.environ.get("ATELIER_MCP_MODE", "local")
        started_at = time.perf_counter()
        try:
            _match_mcp_lexical(args if isinstance(args, dict) else {})
            rtc = _get_realtime_context()
            rtc.record_tool_input(name, args)
            if mcp_mode == "remote" and name in _REMOTE_TOOLS:
                result = _dispatch_remote(name, args)
            else:
                handler: Callable[[dict[str, Any]], dict[str, Any]] = spec["handler"]
                result = handler(args)

            led = _get_ledger()
            result_text = json.dumps(result, ensure_ascii=False, default=str)
            compact_text = result_text if len(result_text) <= 1200 else result_text[:600] + "..." + result_text[-600:]
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
            with contextlib.suppress(Exception):
                rtc = _get_realtime_context()
                rtc.record_tool_error(name, str(exc))
                rtc.persist()
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


def main() -> None:
    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print(f"atelier-mcp {SERVER_VERSION}")
        return
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 < len(argv):
            os.environ["ATELIER_ROOT"] = argv[i + 1]
    serve()


if __name__ == "__main__":
    main()
