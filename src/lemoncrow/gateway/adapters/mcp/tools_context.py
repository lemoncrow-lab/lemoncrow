"""Context MCP handler registration and mode orchestration."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContextHandlerHooks:
    code_context_engine: Callable[..., Any]
    scoped_context_capability: Callable[..., Any]
    runtime: Callable[[], Any]
    get_ledger: Callable[[], Any]
    match_mcp_lexical: Callable[[dict[str, Any]], Any]
    bootstrap_context_status: Callable[[Any], dict[str, Any]]
    lemoncrow_root: Callable[[], Any]
    workspace_root: Callable[[], Any]
    advance_monitors: Callable[..., tuple[Any, Any]]
    get_product_session_id: Callable[[], str]
    spawn_worker_if_idle: Callable[[Any], Any]
    debug_enabled: Callable[[], bool]


_HooksFactory = Callable[[], ContextHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_context_handler_hooks(factory: _HooksFactory) -> None:
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> ContextHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("context handler hooks are not configured")
    return factory()


def _code_context_engine(*args: Any, **kwargs: Any) -> Any:
    return _hooks().code_context_engine(*args, **kwargs)


def _scoped_context_capability(*args: Any, **kwargs: Any) -> Any:
    return _hooks().scoped_context_capability(*args, **kwargs)


def _runtime() -> Any:
    return _hooks().runtime()


def _get_ledger() -> Any:
    return _hooks().get_ledger()


def _match_mcp_lexical(payload: dict[str, Any]) -> Any:
    return _hooks().match_mcp_lexical(payload)


def _bootstrap_context_status(root: Any) -> dict[str, Any]:
    return _hooks().bootstrap_context_status(root)


def _lemoncrow_root() -> Any:
    return _hooks().lemoncrow_root()


def _workspace_root() -> Any:
    return _hooks().workspace_root()


def _advance_monitors(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
    return _hooks().advance_monitors(*args, **kwargs)


def _get_product_session_id() -> str:
    return _hooks().get_product_session_id()


def _spawn_worker_if_idle(root: Any) -> Any:
    return _hooks().spawn_worker_if_idle(root)


def _mcp_debug_enabled() -> bool:
    return _hooks().debug_enabled()


@mcp_tool(name="context")
def tool_get_context(
    task: str,
    domain: str | None = None,
    files: list[str] | None = None,
    keywords: list[str] | None = None,
    excluded_paths: list[str] | None = None,
    tools: list[str] | None = None,
    errors: list[str] | None = None,
    max_blocks: int = 5,
    token_budget: int | None = 2000,
    dedup: bool = True,
    agent_id: str | None = None,
    recall: bool = True,
    mode: Literal["procedures", "symbols", "pull"] = "procedures",
) -> dict[str, Any]:
    """Record task context and retrieve relevant Playbooks for the task.

    Call at task start to seed context with prior procedures, repo bootstrap
    knowledge, and per-agent memory. mode="symbols" returns the most relevant
    code symbols/files from the code index instead; mode="pull" returns scoped
    subtask context (files/keywords/excluded_paths scope it).

    Args: task (required) drives ranking; domain narrows retrieval; files boost
    related blocks; tools/errors rank matching procedure and rescue blocks;
    max_blocks (default 5); token_budget (default 2000, None = unlimited);
    dedup; agent_id loads per-agent memory; recall=False skips memory recall.
    """
    if mode == "symbols":
        engine = _code_context_engine(".")
        return cast(
            dict[str, Any],
            engine.tool_context(
                task=task,
                seed_files=files or [],
                budget_tokens=token_budget or 4000,
                max_symbols=max_blocks,
            ),
        )
    if mode == "pull":
        from lemoncrow.core.capabilities import feature_access as _licensing
        from lemoncrow.pro.capabilities.scoped_context import Subtask

        _licensing.require("scoped_context")
        subtask = Subtask(
            description=task,
            affected_paths=files or [],
            keywords=keywords or [],
            excluded_paths=excluded_paths or [],
            budget_tokens=token_budget or 4000,
        )
        return cast(dict[str, Any], _scoped_context_capability(".").pull(subtask).to_dict())
    if mode != "procedures":
        raise ValueError(f"unknown mode: {mode!r}")
    if errors is None:
        errors = []
    if tools is None:
        tools = []
    if keywords is None:
        keywords = []
    if excluded_paths is None:
        excluded_paths = []
    if files is None:
        files = []
    rt = _runtime()
    led = _get_ledger()
    led.task = task
    if domain:
        led.domain = domain
    _match_mcp_lexical({"task": task})

    led.record_tool_call(
        "get_context",
        {
            "task": task,
            "domain": domain,
            "files": files,
            "keywords": keywords,
            "excluded_paths": excluded_paths,
            "tools": tools,
            "errors": errors,
            "max_blocks": max_blocks,
            "token_budget": token_budget,
            "dedup": dedup,
            "agent_id": agent_id,
            "recall": recall,
        },
    )

    bootstrap = _bootstrap_context_status(_lemoncrow_root())
    # Keep workspace resolution consistent between this MCP adapter and the
    # core runtime path resolver so bootstrap status and injected bootstrap
    # context are derived from the same repository.
    workspace_root = str(_workspace_root().resolve())
    previous_workspace_root = os.environ.get("LEMONCROW_WORKSPACE_ROOT")
    os.environ["LEMONCROW_WORKSPACE_ROOT"] = workspace_root

    # Advance trajectory monitors and obtain FSM-derived retrieval hints.
    _monitor_composite, _fsm_skip_etraces = _advance_monitors(_get_product_session_id(), task, led.task or task)

    try:
        payload = rt.get_context(
            task=task,
            domain=domain,
            files=files,
            tools=tools,
            errors=errors,
            max_blocks=max_blocks,
            token_budget=token_budget,
            dedup=dedup,
            agent_id=agent_id,
            recall=recall,
            monitor_composite=_monitor_composite,
            fsm_skip_etraces=_fsm_skip_etraces,
        )
    finally:
        if previous_workspace_root is None:
            os.environ.pop("LEMONCROW_WORKSPACE_ROOT", None)
        else:
            os.environ["LEMONCROW_WORKSPACE_ROOT"] = previous_workspace_root
    result: dict[str, Any] = payload if isinstance(payload, dict) else {"context": payload}
    if bootstrap["status"] != "warm":
        _spawn_worker_if_idle(_lemoncrow_root())
    result["bootstrap"] = bootstrap

    # Wire PrefixCachePlanner: compute the static/dynamic cache split for this
    # turn. This is cache/token-split diagnostics the model does not act on, so
    # it is gated behind the diagnostics opt-in and never emitted in the default
    # model-facing result. Skipping the whole block also avoids the planner cost
    # on the hot path.
    if _mcp_debug_enabled():
        try:
            from lemoncrow.pro.capabilities.prefix_cache.planner import PrefixCachePlanner
            from lemoncrow.pro.capabilities.prompt_compilation.models import (
                BlockKind,
                PromptBlock,
                Stability,
            )

            context_text = result.get("context", "")
            bootstrap_text = (
                result.get("bootstrap", {}).get("context", "") if isinstance(result.get("bootstrap"), dict) else ""
            )
            _recall_count = len(result.get("recalled_passages", []))

            # Build synthetic PromptBlocks from the assembled context pieces
            blocks: list[PromptBlock] = []
            if context_text:
                blocks.append(
                    PromptBlock(
                        id="context",
                        kind=BlockKind.PLAYBOOK,
                        stability=Stability.BRANCH,
                        content=context_text,
                    )
                )
            if bootstrap_text:
                blocks.append(
                    PromptBlock(
                        id="bootstrap",
                        kind=BlockKind.REPO_SUMMARY,
                        stability=Stability.SESSION,
                        content=bootstrap_text,
                    )
                )
            if task:
                blocks.append(
                    PromptBlock(
                        id="task",
                        kind=BlockKind.USER_TASK,
                        stability=Stability.TURN,
                        content=task,
                    )
                )

            if blocks:
                # Compare with prior hash from last llm_call event in ledger
                prior_hash = ""
                call_events = [e for e in led.events if e.payload.get("kind") == "llm_call"]
                if call_events:
                    prior_hash = call_events[-1].payload.get("stable_prefix_hash", "")

                planner = PrefixCachePlanner()
                plan = planner.plan_with_history(blocks, prior_hash or None)
                result["prefix_plan"] = plan.to_dict()
        except Exception:
            logging.exception("Recovered from broad exception handler")
            # Best-effort: never break tool_context due to prefix planning errors.
            _log.debug("prefix-cache planning failed", exc_info=True)

    return result


__all__ = [
    "ContextHandlerHooks",
    "configure_context_handler_hooks",
    "tool_get_context",
]
