"""Interactive runtime: streaming agent loop wiring the LemonCrow core to the CLI."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import subprocess
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.mcp_integration.loader import (
    MCPServerProcess,
    MCPTool,
)
from lemoncrow.core.capabilities.statusline_sidecar import (
    StatusSnapshot,
    configured_status_path,
    read_index_status,
    write_status_snapshot,
)
from lemoncrow.gateway.cli.events import (
    AssistantDelta,
    AssistantMessage,
    AssistantProgress,
    CacheStats,
    ContextUsageUpdated,
    LemonCrowEvent,
    MemoryHit,
    PermissionRequested,
    RouteSelected,
    RuntimeErrorEvent,
    ToolFinished,
    ToolOutput,
    ToolRequested,
    ToolStarted,
    VerificationResult,
)
from lemoncrow.pro.capabilities.optimization.cache_economics import (
    CacheDecision,
    cache_control_for_tier,
    choose_cache_decision,
    load_provider_cache_handle,
    save_provider_cache_handle,
    select_cache_breakpoint,
    should_rewrite_compacted_prefix,
    stable_system_text,
)
from lemoncrow.pro.capabilities.optimization.evidence_reuse import (
    VerificationReceipt as EvidenceVerificationReceipt,
)
from lemoncrow.pro.capabilities.optimization.evidence_reuse import (
    finalize_task_evidence,
    stage_evidence_result,
)
from lemoncrow.pro.capabilities.optimization.routing_calibration import (
    choose_calibrated_route,
    provider_for_model,
)
from lemoncrow.pro.capabilities.optimization.routing_calibration import (
    record_route_outcome as persist_route_outcome,
)
from lemoncrow.pro.capabilities.optimization.runtime_decisions import (
    OptimizationTraceRecorder,
    normalize_optimization_mode,
)
from lemoncrow.pro.capabilities.owned_agent_session.runtime_policy import (
    RuntimeTurnState,
    build_final_receipt,
    choose_mcp_exposure,
    compact_history,
    estimate_context_tokens,
    is_truncation_finish_reason,
    is_verification_command,
    mcp_broker_schema,
    output_governor_system_message,
    output_token_limit,
    reasoning_effort_for,
    recommended_tool_choice,
    should_switch_route,
    task_requests_mutation,
)

logger = logging.getLogger(__name__)


# Upper bound on retained in-process sessions. Entries are never otherwise
# removed, so a long-lived gateway would accumulate one history list per distinct
# session id; evict the oldest well past any realistic concurrent-session count.
_MAX_TRACKED_SESSIONS = 512
# Pending-permission and share-token maps are written but never popped; cap them
# so a long-lived runtime can't accumulate one entry per request/session forever.
_MAX_PENDING_PERMISSIONS = 1024
_MAX_SHARE_TOKENS = 512


def _evict_oldest(store: dict[str, Any], cap: int) -> None:
    """Drop oldest-inserted entries until *store* is within *cap* (FIFO)."""
    while len(store) > cap:
        store.pop(next(iter(store)))


def _history_cap() -> int:
    try:
        return int(os.environ.get("LEMONCROW_MAX_HISTORY_MESSAGES", "2000"))
    except ValueError:
        return 2000


def _trim_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound conversation history so it can't grow unbounded across a long session
    (which also keeps the per-call cache-breakpoint deep-copy cheap).

    Keeps a leading system message plus the newest messages, snapping the
    retained window forward to a ``user`` turn so a tool result is never orphaned
    from its tool call (which the provider API would reject).
    """
    cap = _history_cap()
    if cap <= 0 or len(messages) <= cap:
        return messages
    head = [messages[0]] if messages and messages[0].get("role") == "system" else []
    body = messages[len(head) :]
    budget = max(1, cap - len(head))
    start = max(0, len(body) - budget)
    while start < len(body) and body[start].get("role") != "user":
        start += 1
    if start >= len(body):
        user_idxs = [index for index, message in enumerate(body) if message.get("role") == "user"]
        start = user_idxs[-1] if user_idxs else 0
    return head + body[start:]


class InteractiveRuntime:
    """Own the agent loop, sessions, routing, and tool supervision for the CLI."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        yolo: bool = False,
        model: str | None = None,
        provider: str | None = None,
        budget_hint: str = "balanced",
        cache_policy: str = "auto",
        max_cost: float | None = None,
        dynamic_routing: bool = True,
        mcp_enabled: bool = True,
        mcp_schema_mode: str = "auto",
        optimization_mode: str = "shadow",
    ) -> None:
        self._root = root or Path.home() / ".lemoncrow"
        self._yolo = yolo
        self._provider_override = provider
        self._override_model = model
        self._budget_hint = budget_hint if budget_hint in {"cheap", "balanced", "best"} else "balanced"
        self._cache_policy = cache_policy
        self._max_cost = max_cost
        self._dynamic_routing = dynamic_routing
        self._mcp_enabled = mcp_enabled
        self._mcp_schema_mode = mcp_schema_mode
        self._optimization_mode = normalize_optimization_mode(optimization_mode)
        self._project_root = Path.cwd().resolve()
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._session_costs: dict[str, float] = {}
        # Structured sidebar snapshot for rich frontends; cumulative for the
        # life of this gateway process (the HTTP adapter mints a fresh session
        # id per request, so per-session totals would reset every turn).
        self._status_snapshot = StatusSnapshot()
        self._status_path = configured_status_path()
        self._pending_permissions: dict[str, dict[str, Any]] = {}
        self._active_tools: list[str] | None = None
        self._current_mode: str = "code"
        self._mcp_servers: list[MCPServerProcess] = []
        self._mcp_tools: list[MCPTool] = []
        self._mcp_lock = threading.Lock()
        self._mcp_startup_thread: threading.Thread | None = None
        self._background_tasks: list[dict[str, Any]] = []
        self._share_tokens: dict[str, str] = {}
        self._gemini_cache_names: dict[str, str] = {}
        self._gemini_cache_failures: set[str] = set()

    async def start_session(
        self,
        project_root: str | None = None,
        *,
        session_id: str | None = None,
    ) -> str:
        session_id = session_id or uuid.uuid4().hex
        while len(self._sessions) >= _MAX_TRACKED_SESSIONS:
            oldest = next(iter(self._sessions))
            self._sessions.pop(oldest)
            self._session_costs.pop(oldest, None)
        self._sessions[session_id] = []
        self._session_costs.setdefault(session_id, 0.0)
        if project_root:
            self._project_root = Path(project_root).resolve()
            os.environ["CLAUDE_WORKSPACE_ROOT"] = str(self._project_root)
        self._start_mcp_servers()
        return session_id

    def _start_mcp_servers(self) -> None:
        """Start discovered MCP servers once; schema selection happens per turn."""
        if not self._mcp_enabled:
            return
        workspace_root = self._project_root

        def _start() -> None:
            try:
                from lemoncrow.core.capabilities.mcp_integration.loader import (
                    MCPServerProcess,
                    discover_mcp_configs,
                )

                configs = discover_mcp_configs(workspace_root)
                for cfg in configs:
                    proc = MCPServerProcess(cfg)
                    if proc.start():
                        tools = proc.list_tools()
                        with self._mcp_lock:
                            self._mcp_servers.append(proc)
                            self._mcp_tools.extend(tools)
                        logger.info("Started MCP server %s with %d tools", cfg.name, len(tools))
            except Exception:
                logger.debug("MCP server startup failed (non-blocking)", exc_info=True)

        thread = threading.Thread(target=_start, daemon=True)
        self._mcp_startup_thread = thread
        thread.start()

    def shutdown(self) -> None:
        startup_thread = self._mcp_startup_thread
        if startup_thread is not None:
            startup_thread.join(timeout=5)
            self._mcp_startup_thread = None
        with self._mcp_lock:
            servers = list(self._mcp_servers)
            self._mcp_servers.clear()
            self._mcp_tools.clear()
        for server in servers:
            server.stop()

    def _messages_with_cache_breakpoint(
        self,
        messages: list[dict[str, Any]],
        model: str,
        decision: CacheDecision | None = None,
    ) -> list[dict[str, Any]]:
        """Apply the selected Anthropic tier only to stable and reusable content."""
        decision = decision or choose_cache_decision(
            self._root,
            requested_policy=self._cache_policy,
            provider=self._provider_override or "",
            model=model,
            messages=messages,
            optimization_mode=self._optimization_mode,
        )
        control = cache_control_for_tier(decision.actual_tier)
        if control is None or decision.provider_style != "anthropic":
            return messages
        request_messages = copy.deepcopy(messages)
        if request_messages and request_messages[0].get("role") == "system":
            content = request_messages[0].get("content")
            if isinstance(content, str) and content:
                request_messages[0]["content"] = [{"type": "text", "text": content, "cache_control": dict(control)}]
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        block["cache_control"] = dict(control)
                        break

        breakpoint = decision.breakpoint or select_cache_breakpoint(request_messages)
        if breakpoint is not None:
            content = request_messages[breakpoint.index].get("content")
            if isinstance(content, str) and content:
                request_messages[breakpoint.index]["content"] = [
                    {"type": "text", "text": content, "cache_control": dict(control)}
                ]
        return request_messages

    def _gemini_cached_content(
        self,
        decision: CacheDecision,
        messages: list[dict[str, Any]],
        model: str,
    ) -> str | None:
        if decision.provider_style != "gemini" or not decision.enabled or self._optimization_mode != "enforce":
            return None
        if decision.lane_key in self._gemini_cache_failures:
            return None
        cached = self._gemini_cache_names.get(decision.lane_key) or load_provider_cache_handle(
            self._root,
            prefix_hash=decision.prefix_hash,
            model=model,
        )
        if cached:
            self._gemini_cache_names[decision.lane_key] = cached
            return cached
        system_text = stable_system_text(messages)
        try:
            minimum = max(1, int(os.environ.get("LEMONCROW_GEMINI_CACHE_MIN_TOKENS", "1024")))
        except ValueError:
            minimum = 1024
        if len(system_text) // 4 < minimum:
            return None
        ttl_seconds = 3600 if decision.actual_tier == "1h" else 300
        try:
            from lemoncrow.pro.capabilities.owned_agent_session.gemini_cache import GeminiContextCache

            cache = GeminiContextCache.create(
                model=model,
                system_prompt=system_text,
                ttl=f"{ttl_seconds}s",
            )
        except Exception:
            self._gemini_cache_failures.add(decision.lane_key)
            logger.debug("Gemini context-cache creation failed", exc_info=True)
            return None
        self._gemini_cache_names[decision.lane_key] = cache.name
        save_provider_cache_handle(
            self._root,
            prefix_hash=decision.prefix_hash,
            model=model,
            name=cache.name,
            ttl_seconds=ttl_seconds,
        )
        return cache.name

    async def _completion_with_backoff(self, request_kwargs: dict[str, Any]) -> Any:
        """Call LiteLLM with bounded exponential backoff for provider throttling."""
        import litellm

        from lemoncrow.core.capabilities.providers.zen import apply_zen_transport

        request_kwargs = apply_zen_transport(request_kwargs)
        max_retries = max(0, int(os.environ.get("LEMONCROW_LLM_MAX_RETRIES", "6")))
        base_delay = max(1.0, float(os.environ.get("LEMONCROW_LLM_RETRY_BASE_SECONDS", "8")))
        for attempt in range(max_retries + 1):
            try:
                return await asyncio.to_thread(litellm.completion, **request_kwargs)
            except Exception as exc:
                lowered = str(exc).lower()
                retryable = (
                    getattr(exc, "status_code", None) == 429  # litellm RateLimitError et al.
                    or "ratelimit" in lowered
                    or "rate limit" in lowered
                    or "too many requests" in lowered
                )
                if not retryable or attempt >= max_retries:
                    raise
                await asyncio.sleep(min(120.0, base_delay * (2**attempt)))
        raise RuntimeError("unreachable retry state")

    def _publish_status_snapshot(
        self,
        *,
        model: str,
        input_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        cost_usd: float,
        saved_usd: float,
        cache_efficiency_pct: float,
    ) -> None:
        """Refresh the sidebar snapshot; a frontend without one costs nothing."""
        if self._status_path is None:
            return
        self._status_snapshot.add_turn(
            provider=self._provider_override or "",
            model=model,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            saved_usd=saved_usd,
            cache_efficiency_pct=cache_efficiency_pct,
        )
        self._status_snapshot.index = read_index_status(self._project_root)
        write_status_snapshot(self._status_path, self._status_snapshot)

    async def _execute_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        session_id: str = "",
    ) -> tuple[str, bool]:
        """Execute one built-in, exposed MCP, or fallback-broker call."""
        self._status_snapshot.record_tool_call(tool_name)
        try:
            if tool_name == "mcp_tool":
                result_str = await asyncio.to_thread(self._dispatch_mcp_broker, tool_args)
                return result_str, not result_str.startswith("Error:")
            if tool_name.startswith("mcp__"):
                result_str = await asyncio.to_thread(self._dispatch_mcp_tool, tool_name, tool_args)
                return result_str, not result_str.startswith("Error:")
            result = await asyncio.to_thread(_dispatch_tool, tool_name, tool_args)
            return _render_tool_result(tool_name, result, tool_args, session_id=session_id), True
        except Exception as exc:
            return f"Error: {exc}", False

    def _dispatch_mcp_tool(self, tool_name: str, tool_args: dict[str, Any]) -> str:
        parts = tool_name.split("__", 2)
        if len(parts) != 3:
            return f"Error: malformed MCP tool name '{tool_name}'"
        _, server_name, actual_tool = parts
        with self._mcp_lock:
            servers = list(self._mcp_servers)
        for server in servers:
            if server.config.name == server_name:
                return server.call_tool(actual_tool, tool_args)
        return f"Error: MCP server '{server_name}' not found"

    def _dispatch_mcp_broker(self, args: dict[str, Any]) -> str:
        action = str(args.get("action", "search"))
        with self._mcp_lock:
            tools = list(self._mcp_tools)
        if action == "search":
            query = str(args.get("query", "")).lower().strip()
            matches = [
                {"server": tool.server_name, "tool": tool.name, "description": tool.description[:240]}
                for tool in tools
                if not query or query in f"{tool.server_name} {tool.name} {tool.description}".lower()
            ][:20]
            return json.dumps({"matches": matches}, ensure_ascii=False)
        if action == "call":
            server_name = str(args.get("server", ""))
            tool_name = str(args.get("tool", ""))
            arguments = args.get("arguments", {})
            if not isinstance(arguments, dict):
                return "Error: MCP arguments must be an object"
            return self._dispatch_mcp_tool(f"mcp__{server_name}__{tool_name}", arguments)
        return f"Error: unknown MCP broker action '{action}'"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._sessions.get(session_id, ()))

    def restore_session(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self._sessions[session_id] = list(messages)

    def drop_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._session_costs.pop(session_id, None)

    async def handle_user_message(
        self,
        session_id: str,
        text: str,
        *,
        model_override: str | None = None,
        budget_hint: str | None = None,
        context: str = "",
        max_output_tokens: int | None = None,
        primer_metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[LemonCrowEvent]:
        messages = self._sessions.setdefault(session_id, [])
        budget = budget_hint or self._budget_hint
        mode_prefix = {
            "code": "[MODE: code]",
            "explore": "[MODE: explore — no edits please]",
            "research": "[MODE: research — no edits]",
            "plan": "[MODE: plan — no edits]",
        }.get(self._current_mode, "")
        prefixed_text = f"{mode_prefix} {text}".strip() if mode_prefix else text
        if context:
            prefixed_text = f"{prefixed_text}\n\n{context}"
        messages.append({"role": "user", "content": prefixed_text})

        initial_route_trace: dict[str, Any] | None = None
        if model_override or self._override_model:
            selected_model = model_override or self._override_model or ""
            initial_route_trace = {
                "proposed": {"model": selected_model},
                "actual": {"model": selected_model},
                "reason": "pinned model override",
                "eligible": False,
            }
            yield RouteSelected(
                type="route.selected",
                provider=self._provider_override if not model_override else None,
                model=selected_model,
                reason="api model override" if model_override else "user model override",
            )
            async for event in self._agent_loop(
                session_id,
                messages,
                model=selected_model,
                task_text=text,
                budget_hint=budget,
                primer_supplied=bool(context),
                dynamic_routing=False,
                max_output_tokens=max_output_tokens,
                initial_route_trace=initial_route_trace,
                primer_metadata=primer_metadata,
            ):
                yield event
            return

        try:
            from lemoncrow.gateway.cli.commands.run import _resolve_litellm_model
            from lemoncrow.pro.capabilities.owned_execution_routing import OwnedRouteRequest, select_owned_route

            decision = select_owned_route(
                self._root,
                OwnedRouteRequest(
                    tool_name="edit" if context else "read",
                    task_text=text,
                    mode="explicit" if self._provider_override else "auto",
                    budget=budget,  # type: ignore[arg-type]
                    provider=self._provider_override or "",
                ),
            )
            if self._optimization_mode == "off":
                actual_provider = decision.provider
                actual_model = decision.model
                route_reason = decision.reason
                initial_route_trace = {
                    "proposed": {"provider": actual_provider, "model": actual_model},
                    "actual": {"provider": actual_provider, "model": actual_model},
                    "reason": "optimization mode off; legacy owned route",
                    "eligible": False,
                }
            else:
                calibrated = choose_calibrated_route(
                    self._root,
                    route_decision=decision,
                    phase="execute" if context else "explore",
                    current_model="",
                    context_tokens=0,
                )
                enforce_calibrated = self._optimization_mode == "enforce" and calibrated.eligible
                actual_provider = calibrated.provider if enforce_calibrated else decision.provider
                actual_model = calibrated.model if enforce_calibrated else decision.model
                route_reason = calibrated.reason if enforce_calibrated else decision.reason
                initial_route_trace = {
                    "proposed": calibrated.trace(),
                    "actual": {"provider": actual_provider, "model": actual_model},
                    "reason": calibrated.reason,
                    "eligible": calibrated.eligible,
                }
            selected_model = _resolve_litellm_model(actual_provider, actual_model)
            yield RouteSelected(
                type="route.selected",
                provider=actual_provider,
                model=actual_model,
                reason=route_reason,
            )
        except Exception:
            from lemoncrow.core.capabilities.providers.zen import fallback_model as _fallback_model

            selected_model = _fallback_model()
            initial_route_trace = {
                "proposed": {"model": selected_model},
                "actual": {"model": selected_model},
                "reason": "owned route fallback",
                "eligible": False,
            }

        async for event in self._agent_loop(
            session_id,
            messages,
            model=selected_model,
            task_text=text,
            budget_hint=budget,
            primer_supplied=bool(context),
            dynamic_routing=self._dynamic_routing,
            max_output_tokens=max_output_tokens,
            initial_route_trace=initial_route_trace,
            primer_metadata=primer_metadata,
        ):
            yield event

    async def _agent_loop(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        model: str,
        task_text: str = "",
        budget_hint: str = "balanced",
        primer_supplied: bool = False,
        dynamic_routing: bool = False,
        max_iterations: int = 100,
        max_output_tokens: int | None = None,
        initial_route_trace: dict[str, Any] | None = None,
        primer_metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[LemonCrowEvent]:
        from lemoncrow.pro.capabilities.owned_agent_session.phase_runner import _system_message

        if not messages or messages[0].get("role") != "system":
            system_message = _system_message(self._provider_override or "", model)
            messages.insert(0, output_governor_system_message(system_message, self._optimization_mode))

        startup_thread = self._mcp_startup_thread
        if startup_thread is not None and startup_thread.is_alive():
            await asyncio.to_thread(startup_thread.join, 1.0)

        tools = [
            tool
            for tool in _get_litellm_tools()
            if self._active_tools is None or tool["function"]["name"] in self._active_tools
        ]
        with self._mcp_lock:
            available_mcp_tools = list(self._mcp_tools)
        exposure = choose_mcp_exposure(available_mcp_tools, task_text, self._mcp_schema_mode)
        for tool in exposure.tools:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"mcp__{tool.server_name}__{tool.name}",
                        "description": f"[MCP:{tool.server_name}] {tool.description}",
                        "parameters": tool.input_schema or {"type": "object", "properties": {}},
                    },
                }
            )
        if exposure.focused:
            tools.append(mcp_broker_schema())

        state = RuntimeTurnState(primer_supplied=primer_supplied)
        mutation_requested = task_requests_mutation(task_text)
        routed_phase = state.phase
        current_model = model
        trace = OptimizationTraceRecorder(
            self._root,
            session_id=session_id,
            task_text=task_text,
            mode=self._optimization_mode,
        )

        def record_route_outcome(
            root: Path,
            *,
            provider: str,
            model: str,
            phase: str,
            success: bool,
            cost_usd: float,
        ) -> None:
            if self._optimization_mode != "off":
                persist_route_outcome(
                    root,
                    provider=provider,
                    model=model,
                    phase=phase,
                    success=success,
                    cost_usd=cost_usd,
                )

        initial_route_trace = initial_route_trace or {
            "proposed": {"model": current_model},
            "actual": {"model": current_model},
            "reason": "initial owned route",
            "eligible": False,
        }
        trace.decision(
            "route",
            phase=state.phase,
            proposed=initial_route_trace["proposed"],
            actual=initial_route_trace["actual"],
            reason=str(initial_route_trace["reason"]),
            eligible=bool(initial_route_trace["eligible"]),
        )
        primer_trace = primer_metadata or {
            "optimization_mode": self._optimization_mode,
            "base_primer_skipped": False,
            "evidence_hits": 0,
            "invalidated_evidence": 0,
            "evidence_applied": False,
            "local_retrieval_invoked": False,
            "local_retrieval_cache_hit": False,
            "local_retrieval_packet_ready": False,
            "local_retrieval_applied": False,
            "local_retrieval_model_calls": 0,
            "local_retrieval_turns": 0,
            "local_retrieval_spans": 0,
            "local_retrieval_confidence": 0.0,
            "local_retrieval_reason": "no first-turn primer metadata",
        }
        base_primer_skipped = bool(primer_trace.get("base_primer_skipped"))
        trace.decision(
            "base_primer",
            phase=state.phase,
            proposed={"skip_broad_primer": base_primer_skipped},
            actual={"skip_broad_primer": base_primer_skipped},
            reason=(
                "explicit source path already supplies the deterministic starting target"
                if base_primer_skipped
                else "bounded deterministic workspace primer"
            ),
            eligible=base_primer_skipped,
        )
        evidence_common = {
            "hit_count": int(primer_trace.get("evidence_hits", 0) or 0),
            "invalidated": int(primer_trace.get("invalidated_evidence", 0) or 0),
        }
        trace.decision(
            "evidence_lookup",
            phase=state.phase,
            proposed={**evidence_common, "applied": evidence_common["hit_count"] > 0},
            actual={**evidence_common, "applied": bool(primer_trace.get("evidence_applied"))},
            reason="verified evidence is prompt-visible only in enforce mode",
            eligible=evidence_common["hit_count"] > 0,
        )
        local_common = {
            "invoked": bool(primer_trace.get("local_retrieval_invoked")),
            "cache_hit": bool(primer_trace.get("local_retrieval_cache_hit")),
            "model_calls": int(primer_trace.get("local_retrieval_model_calls", 0) or 0),
            "turns": int(primer_trace.get("local_retrieval_turns", 0) or 0),
            "spans": int(primer_trace.get("local_retrieval_spans", 0) or 0),
            "confidence": float(primer_trace.get("local_retrieval_confidence", 0.0) or 0.0),
        }
        trace.decision(
            "local_retrieval",
            phase=state.phase,
            proposed={**local_common, "applied": bool(primer_trace.get("local_retrieval_packet_ready"))},
            actual={**local_common, "applied": bool(primer_trace.get("local_retrieval_applied"))},
            reason=str(primer_trace.get("local_retrieval_reason", "bounded local evidence packet"))[:256],
            eligible=bool(local_common["invoked"]),
        )
        trace.decision(
            "tool_exposure",
            phase=state.phase,
            proposed={"profile": exposure.reason, "tool_count": len(tools)},
            actual={"profile": exposure.reason, "tool_count": len(tools)},
            reason="deterministic hybrid MCP exposure",
        )
        total_input = total_output = total_cache_read = total_cache_write = 0
        total_cost = total_naive_cost = 0.0
        tool_call_counts: dict[str, int] = {}
        completed = False
        next_output_limit: int | None = None
        truncation_extensions = 0
        evidence_staged = 0
        last_cache_enabled = False
        last_cache_sample_count = 0

        for _iteration in range(max_iterations):
            if dynamic_routing and state.phase != routed_phase:
                routed_phase = state.phase
                try:
                    from lemoncrow.gateway.cli.commands.run import _resolve_litellm_model
                    from lemoncrow.pro.capabilities.owned_execution_routing import OwnedRouteRequest, select_owned_route

                    phase_tool = {
                        "explore": "read",
                        "execute": "edit",
                        "repair": "bash",
                        "finish": "tui",
                    }[state.phase]
                    decision = select_owned_route(
                        self._root,
                        OwnedRouteRequest(
                            tool_name=phase_tool,
                            task_text=f"[phase:{state.phase}] {task_text}",
                            mode="auto",
                            budget=budget_hint,  # type: ignore[arg-type]
                            session_state={
                                "phase": state.phase,
                                "edits": state.edit_count,
                                "failures": state.failure_count,
                            },
                        ),
                    )
                    legacy_candidate = _resolve_litellm_model(decision.provider, decision.model)
                    if self._optimization_mode == "off":
                        actual_provider = decision.provider
                        actual_model = decision.model
                        candidate = legacy_candidate
                        route_reason = decision.reason
                        proposed_route = {"provider": actual_provider, "model": actual_model}
                        route_eligible = False
                    else:
                        calibrated = choose_calibrated_route(
                            self._root,
                            route_decision=decision,
                            phase=state.phase,
                            current_model=current_model,
                            context_tokens=estimate_context_tokens(messages),
                            failure_count=state.failure_count,
                        )
                        enforce_calibrated = self._optimization_mode == "enforce" and calibrated.eligible
                        actual_provider = calibrated.provider if enforce_calibrated else decision.provider
                        actual_model = calibrated.model if enforce_calibrated else decision.model
                        candidate = (
                            _resolve_litellm_model(actual_provider, actual_model)
                            if enforce_calibrated
                            else legacy_candidate
                        )
                        route_reason = calibrated.reason if enforce_calibrated else decision.reason
                        proposed_route = calibrated.trace()
                        route_eligible = calibrated.eligible
                    switch = should_switch_route(messages, current_model, candidate, state.phase)
                    trace.decision(
                        "route",
                        phase=state.phase,
                        proposed=proposed_route,
                        actual={"provider": actual_provider, "model": candidate if switch else current_model},
                        reason=route_reason,
                        eligible=route_eligible,
                    )
                    if switch:
                        current_model = candidate
                        yield RouteSelected(
                            type="route.selected",
                            provider=actual_provider,
                            model=actual_model,
                            reason=f"phase {state.phase}: {route_reason}",
                        )
                except Exception:
                    logger.debug("Phase route selection failed; retaining %s", current_model, exc_info=True)

            iteration_phase = state.phase
            accumulated_text = ""
            tool_calls_acc: dict[int, dict[str, Any]] = {}
            finish_reason = ""
            iter_input = iter_output = iter_cache_read = iter_cache_write = 0
            # Reasoning models spend the same max_tokens budget on hidden
            # reasoning, so a truncation on those needs a bigger allowance than
            # the single doubling that suffices for a plain completion.
            iter_reasoning = 0
            base_output_limit = output_token_limit(budget_hint, state.phase)
            phase_output_limit = next_output_limit or base_output_limit
            next_output_limit = None
            if max_output_tokens is not None and max_output_tokens > 0:
                phase_output_limit = min(phase_output_limit, max_output_tokens)
            proposed_tool_choice = recommended_tool_choice(current_model, state.phase, task_text)
            actual_tool_choice = proposed_tool_choice if self._optimization_mode == "enforce" else "auto"
            trace.decision(
                "tool_choice",
                phase=state.phase,
                proposed={"tool_choice": proposed_tool_choice},
                actual={"tool_choice": actual_tool_choice},
                reason="tool-only execution for explicit mutation work",
                eligible=proposed_tool_choice == "required",
            )
            cache_decision = choose_cache_decision(
                self._root,
                requested_policy=self._cache_policy,
                provider=self._provider_override or "",
                model=current_model,
                messages=messages,
                optimization_mode=self._optimization_mode,
            )
            last_cache_enabled = cache_decision.enabled
            last_cache_sample_count = cache_decision.sample_count
            trace.decision(
                "cache",
                phase=state.phase,
                proposed=cache_decision.trace_proposed(),
                actual=cache_decision.trace_actual(),
                reason=cache_decision.reason,
            )
            request_kwargs: dict[str, Any] = {
                "model": current_model,
                "messages": self._messages_with_cache_breakpoint(messages, current_model, cache_decision),
                "tools": tools,
                "tool_choice": actual_tool_choice,
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": phase_output_limit,
            }
            if cache_decision.provider_style == "openai" and cache_decision.enabled:
                request_kwargs["prompt_cache_key"] = cache_decision.lane_key
            elif cache_decision.provider_style == "gemini" and cache_decision.enabled:
                cached_content = await asyncio.to_thread(
                    self._gemini_cached_content,
                    cache_decision,
                    messages,
                    current_model,
                )
                if cached_content:
                    request_kwargs["extra_body"] = {"cachedContent": cached_content}
            effort = reasoning_effort_for(current_model, budget_hint, state.phase)
            trace.decision(
                "joint_policy",
                phase=state.phase,
                proposed={
                    "model": current_model,
                    "reasoning_effort": effort,
                    "output_limit": phase_output_limit,
                    "tool_choice": proposed_tool_choice,
                    "tool_profile": exposure.reason,
                    "cache_tier": cache_decision.proposed_tier,
                    "cache_lane": cache_decision.lane_key,
                },
                actual={
                    "model": current_model,
                    "reasoning_effort": effort,
                    "output_limit": phase_output_limit,
                    "tool_choice": actual_tool_choice,
                    "tool_profile": exposure.reason,
                    "cache_tier": cache_decision.actual_tier,
                    "cache_lane": cache_decision.lane_key,
                },
                reason="joint phase policy",
            )
            if effort is not None:
                request_kwargs["reasoning_effort"] = effort
            if current_model.startswith("bedrock/"):
                bearer_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
                if bearer_token:
                    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
                    request_kwargs["api_key"] = bearer_token

            try:
                stream = await self._completion_with_backoff(request_kwargs)
            except Exception as exc:
                err_str = str(exc)
                blocked = "API_KEY_SERVICE_BLOCKED" in err_str or "PERMISSION_DENIED" in err_str or "403" in err_str
                from lemoncrow.core.capabilities.providers.zen import fallback_model as _fallback_model

                fallback_model = _fallback_model()
                if blocked and current_model != fallback_model:
                    yield RuntimeErrorEvent(
                        type="error",
                        message=f"Provider {current_model!r} blocked; retrying with {fallback_model!r}.",
                    )
                    async for event in self._agent_loop(
                        session_id,
                        messages,
                        model=fallback_model,
                        task_text=task_text,
                        budget_hint=budget_hint,
                        primer_supplied=primer_supplied,
                        dynamic_routing=False,
                        max_iterations=max_iterations - 1,
                        max_output_tokens=max_output_tokens,
                        primer_metadata=primer_metadata,
                    ):
                        yield event
                else:
                    yield RuntimeErrorEvent(type="error", message=f"LLM call failed: {exc}")
                record_route_outcome(
                    self._root,
                    provider=provider_for_model(current_model),
                    model=current_model,
                    phase=iteration_phase,
                    success=False,
                    cost_usd=0.0,
                )
                trace.finish(accepted=False, error_code="provider_error")
                return

            async for chunk in _aiter_sync_stream(stream):
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    raw_input = int(getattr(usage, "prompt_tokens", 0) or 0)
                    output = int(getattr(usage, "completion_tokens", 0) or 0)
                    details = getattr(usage, "prompt_tokens_details", None)
                    cached = int(
                        getattr(usage, "cache_read_input_tokens", 0)
                        or (getattr(details, "cached_tokens", 0) if details else 0)
                        or 0
                    )
                    cache_write = int(
                        getattr(usage, "cache_creation_input_tokens", 0)
                        or getattr(usage, "cache_write_input_tokens", 0)
                        or (getattr(details, "cache_creation_tokens", 0) if details else 0)
                        or 0
                    )
                    iter_input += max(0, raw_input - cached - cache_write)
                    iter_output += output
                    iter_cache_read += cached
                    iter_cache_write += cache_write
                    completion_details = getattr(usage, "completion_tokens_details", None)
                    if completion_details is not None:
                        iter_reasoning += int(getattr(completion_details, "reasoning_tokens", 0) or 0)

                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue
                delta = choice.delta
                finish_reason = choice.finish_reason or finish_reason
                if delta.content:
                    accumulated_text += delta.content
                    if self._optimization_mode != "enforce":
                        yield AssistantDelta(type="assistant.delta", text=delta.content)
                if delta.tool_calls:
                    for tool_call in delta.tool_calls:
                        index = tool_call.index
                        if index not in tool_calls_acc:
                            tool_calls_acc[index] = {
                                "id": tool_call.id or "",
                                "type": "function",
                                "function": {
                                    "name": (tool_call.function.name if tool_call.function else "") or "",
                                    "arguments": "",
                                },
                            }
                        if tool_call.id:
                            tool_calls_acc[index]["id"] = tool_call.id
                        if tool_call.function:
                            if tool_call.function.name:
                                tool_calls_acc[index]["function"]["name"] = tool_call.function.name
                            if tool_call.function.arguments:
                                tool_calls_acc[index]["function"]["arguments"] += tool_call.function.arguments

            if self._optimization_mode == "enforce" and accumulated_text:
                if tool_calls_acc:
                    yield AssistantProgress(type="assistant.progress", text=accumulated_text)
                else:
                    yield AssistantDelta(type="assistant.delta", text=accumulated_text)

            total_input += iter_input
            total_output += iter_output
            total_cache_read += iter_cache_read
            total_cache_write += iter_cache_write
            try:
                from lemoncrow.core.capabilities.savings_summary import estimate_cost_usd

                iteration_cost = float(
                    estimate_cost_usd(
                        model_id=current_model,
                        input_tokens=iter_input,
                        output_tokens=iter_output,
                        cache_read_tokens=iter_cache_read,
                        cache_write_tokens=iter_cache_write,
                    )
                )
                iteration_naive = float(
                    estimate_cost_usd(
                        model_id=current_model,
                        input_tokens=iter_input + iter_cache_read + iter_cache_write,
                        output_tokens=iter_output,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                    )
                )
            except Exception:
                iteration_cost = iteration_naive = 0.0
            total_cost += iteration_cost
            total_naive_cost += iteration_naive
            trace.record_provider_call(
                phase=state.phase,
                model=current_model,
                finish_reason=finish_reason,
                output_limit=phase_output_limit,
                reasoning_effort=effort,
                fresh_input_tokens=iter_input,
                cache_read_tokens=iter_cache_read,
                cache_write_tokens=iter_cache_write,
                output_tokens=iter_output,
                cost_usd=iteration_cost,
            )

            projected = self._session_costs.get(session_id, 0.0) + total_cost
            if self._max_cost is not None and projected > self._max_cost:
                record_route_outcome(
                    self._root,
                    provider=provider_for_model(current_model),
                    model=current_model,
                    phase=iteration_phase,
                    success=False,
                    cost_usd=iteration_cost,
                )
                if accumulated_text:
                    messages.append({"role": "assistant", "content": accumulated_text})
                    yield AssistantMessage(type="assistant.message", text=accumulated_text)
                yield RuntimeErrorEvent(
                    type="error",
                    message=(
                        f"Cost cap reached: projected session cost ${projected:.4f} exceeds ${self._max_cost:.4f}."
                    ),
                )
                break

            if is_truncation_finish_reason(finish_reason):
                record_route_outcome(
                    self._root,
                    provider=provider_for_model(current_model),
                    model=current_model,
                    phase=iteration_phase,
                    success=False,
                    cost_usd=iteration_cost,
                )
                # A reasoning model reports its hidden tokens in
                # completion_tokens_details; the visible answer only gets what is
                # left, so grow the cap past the reasoning spend instead of
                # doubling a budget reasoning already consumed.
                reasoning_headroom = iter_reasoning * 2 if iter_reasoning else 0
                extended = min(
                    max(phase_output_limit + 256, phase_output_limit * 2, reasoning_headroom),
                    base_output_limit * 8 if iter_reasoning else base_output_limit * 4,
                )
                if max_output_tokens is not None and max_output_tokens > 0:
                    extended = min(extended, max_output_tokens)
                max_extensions = 3 if iter_reasoning else 1
                if truncation_extensions < max_extensions and extended > phase_output_limit:
                    truncation_extensions += 1
                    next_output_limit = extended
                    trace.record_truncation_extension()
                    trace.decision(
                        "output_extension",
                        phase=state.phase,
                        proposed={"max_tokens": extended},
                        actual={"max_tokens": extended},
                        reason="provider returned an explicit truncation finish reason",
                    )
                    if not tool_calls_acc and accumulated_text:
                        messages.append({"role": "assistant", "content": accumulated_text})
                        messages.append(
                            {
                                "role": "user",
                                "content": "[Output was truncated. Continue exactly where it stopped, concisely.]",
                            }
                        )
                    continue
                yield RuntimeErrorEvent(
                    type="error",
                    message="Provider output remained truncated at the configured hard limit.",
                )
                break

            if not tool_calls_acc or finish_reason == "stop":
                record_route_outcome(
                    self._root,
                    provider=provider_for_model(current_model),
                    model=current_model,
                    phase=iteration_phase,
                    success=bool(accumulated_text) and (not mutation_requested or state.ready_for_receipt),
                    cost_usd=iteration_cost,
                )
                if accumulated_text:
                    messages.append({"role": "assistant", "content": accumulated_text})
                    yield AssistantMessage(type="assistant.message", text=accumulated_text)
                    completed = True
                break

            tool_calls_list = [tool_calls_acc[index] for index in sorted(tool_calls_acc)]
            retain_progress = self._optimization_mode != "enforce"
            trace.decision(
                "progress_history",
                phase=state.phase,
                proposed={"retain_progress": False},
                actual={"retain_progress": retain_progress},
                reason="progress remains user-visible but is unnecessary model context",
                eligible=bool(accumulated_text),
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": (accumulated_text or None) if retain_progress else None,
                    "tool_calls": tool_calls_list,
                }
            )

            looping = False
            for tool_call in tool_calls_list:
                tool_name = tool_call["function"]["name"]
                fingerprint = f"{tool_name}:{tool_call['function']['arguments']}"
                tool_call_counts[fingerprint] = tool_call_counts.get(fingerprint, 0) + 1
                if tool_call_counts[fingerprint] > 3:
                    yield RuntimeErrorEvent(
                        type="error",
                        message=f"Loop detected: {tool_name!r} repeated with identical arguments.",
                    )
                    looping = tool_call_counts[fingerprint] > 6
            if looping:
                for tool_call in tool_calls_list:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": "[stopped: identical tool-call loop]",
                        }
                    )
                break

            prepared_calls: list[tuple[str, str, dict[str, Any]]] = []
            iteration_tools_ok = True
            for tool_call in tool_calls_list:
                tool_id = tool_call["id"]
                tool_name = tool_call["function"]["name"]
                try:
                    tool_args = json.loads(tool_call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                yield ToolRequested(type="tool.requested", id=tool_id, name=tool_name, args=tool_args)

                if not self._yolo and tool_name in {"edit", "bash"}:
                    self._pending_permissions[tool_id] = {"approved": None}
                    _evict_oldest(self._pending_permissions, _MAX_PENDING_PERMISSIONS)
                    yield PermissionRequested(
                        type="permission.requested",
                        id=tool_id,
                        action=f"{tool_name}: {json.dumps(tool_args)[:120]}",
                        risk="high" if tool_name == "bash" else "medium",
                    )
                    for _ in range(300):
                        await asyncio.sleep(0.1)
                        if self._pending_permissions.get(tool_id, {}).get("approved") is not None:
                            break
                    approved = self._pending_permissions.pop(tool_id, {}).get("approved", False)
                    if not approved:
                        result_str = "[denied by user]"
                        messages.append({"role": "tool", "tool_call_id": tool_id, "content": result_str})
                        state.record(tool_name, False, tool_args, result_str)
                        trace.record_tool(tool_name, ok=False)
                        iteration_tools_ok = False
                        yield ToolFinished(
                            type="tool.finished",
                            id=tool_id,
                            name=tool_name,
                            ok=False,
                            result=result_str,
                        )
                        continue
                prepared_calls.append((tool_id, tool_name, tool_args))

            index = 0
            while index < len(prepared_calls):
                _, tool_name, _ = prepared_calls[index]
                if tool_name in _PARALLEL_SAFE_TOOLS or tool_name.startswith("mcp__") or tool_name == "mcp_tool":
                    end = index + 1
                    while end < len(prepared_calls):
                        candidate_name = prepared_calls[end][1]
                        if not (
                            candidate_name in _PARALLEL_SAFE_TOOLS
                            or candidate_name.startswith("mcp__")
                            or candidate_name == "mcp_tool"
                        ):
                            break
                        end += 1
                    batch = prepared_calls[index:end]
                else:
                    end = index + 1
                    batch = [prepared_calls[index]]

                for batch_id, batch_name, _batch_args in batch:
                    yield ToolStarted(type="tool.started", id=batch_id, name=batch_name)
                results = await asyncio.gather(
                    *(
                        self._execute_tool_call(batch_name, batch_args, session_id=session_id)
                        for _, batch_name, batch_args in batch
                    )
                )
                for (batch_id, batch_name, batch_args), (result_str, ok) in zip(batch, results, strict=True):
                    preview = result_str[:2000] + ("…" if len(result_str) > 2000 else "")
                    yield ToolOutput(type="tool.output", id=batch_id, chunk=preview)
                    yield ToolFinished(
                        type="tool.finished",
                        id=batch_id,
                        name=batch_name,
                        ok=ok,
                        result=result_str[:500],
                    )
                    messages.append({"role": "tool", "tool_call_id": batch_id, "content": result_str})
                    state.record(batch_name, ok, batch_args, result_str)
                    trace.record_tool(batch_name, ok=ok)
                    iteration_tools_ok = iteration_tools_ok and ok
                    if ok and self._optimization_mode != "off":
                        staged = await asyncio.to_thread(
                            stage_evidence_result,
                            self._root,
                            self._project_root,
                            task=task_text,
                            tool_name=batch_name,
                            args=batch_args,
                            result=result_str,
                        )
                        evidence_staged += int(staged.staged)

                    if batch_name == "bash" and is_verification_command(batch_args):
                        trace.record_verification(ok=ok)
                        yield VerificationResult(
                            type="verification.result",
                            ok=ok,
                            rubric="project command",
                            details=result_str[-1000:],
                        )

                    if batch_name == "edit" and ok:
                        try:
                            edited_paths = [
                                str(edit.get("file_path") or edit.get("path") or "").split("#")[0]
                                for edit in batch_args.get("edits", [])
                            ]
                            diff_cmd = ["git", "diff", "--no-color"]
                            if edited_paths and all(edited_paths):
                                diff_cmd += ["--", *edited_paths]
                            raw_diff = await asyncio.to_thread(
                                subprocess.check_output,
                                diff_cmd,
                                cwd=str(self._project_root),
                                stderr=subprocess.DEVNULL,
                            )
                            diff = raw_diff.decode(errors="replace")[:5000]
                            if diff.strip():
                                from lemoncrow.gateway.cli.events import PatchProposed

                                yield PatchProposed(
                                    type="patch.proposed",
                                    id=batch_id,
                                    files=edited_paths,
                                    diff=diff,
                                )
                        except Exception:
                            pass
                index = end

            record_route_outcome(
                self._root,
                provider=provider_for_model(current_model),
                model=current_model,
                phase=iteration_phase,
                success=iteration_tools_ok,
                cost_usd=iteration_cost,
            )

            if state.ready_for_receipt:
                receipt = build_final_receipt(state)
                enforce_receipt = self._optimization_mode == "enforce"
                trace.decision(
                    "finalization",
                    phase=state.phase,
                    proposed={"strategy": "deterministic_receipt"},
                    actual={"strategy": "deterministic_receipt" if enforce_receipt else "model"},
                    reason="latest mutation generation has a successful verification receipt",
                )
                if enforce_receipt:
                    yield AssistantDelta(type="assistant.delta", text=receipt)
                    messages.append({"role": "assistant", "content": receipt})
                    yield AssistantMessage(type="assistant.message", text=receipt)
                    completed = True
                    break

        evidence_finalized = 0
        evidence_receipt = None
        if completed and self._optimization_mode != "off":
            if state.ready_for_receipt:
                evidence_receipt = EvidenceVerificationReceipt(
                    kind="project_command",
                    command=state.verification_commands[-1],
                    ok=True,
                    output_hash=state.last_verification_output_hash,
                )
            elif state.edit_count == 0 and not mutation_requested:
                evidence_receipt = EvidenceVerificationReceipt(
                    kind="read_only_completion",
                    command="workspace fingerprint revalidation",
                    ok=True,
                )
            if evidence_receipt is not None:
                evidence_finalized = await asyncio.to_thread(
                    finalize_task_evidence,
                    self._root,
                    self._project_root,
                    task=task_text,
                    receipt=evidence_receipt,
                )
        trace.decision(
            "evidence_reuse",
            phase=state.phase,
            proposed={"staged": evidence_staged, "finalized": evidence_finalized},
            actual={"staged": evidence_staged, "finalized": evidence_finalized},
            reason="only source-hashed deterministic evidence can be finalized",
            eligible=evidence_staged > 0,
        )

        total_input = max(0, total_input)
        denominator = total_cache_read + total_cache_write + total_input
        self._session_costs[session_id] = self._session_costs.get(session_id, 0.0) + total_cost
        if denominator > 0:
            efficiency = round(total_cache_read / denominator * 100, 1)
            yield CacheStats(
                type="cache.stats",
                session_id=session_id,
                cache_efficiency_pct=efficiency,
                cost_usd=total_cost,
                savings_usd=max(0.0, total_naive_cost - total_cost),
                cache_read_tokens=total_cache_read,
                cache_write_tokens=total_cache_write,
                fresh_tokens=total_input,
            )
            from lemoncrow.pro.capabilities.owned_agent_session.stem_prompt import STEM_VERSION

            yield ContextUsageUpdated(
                type="context.usage.updated",
                session_id=session_id,
                input_tokens=total_input,
                cache_read_tokens=total_cache_read,
                cache_write_tokens=total_cache_write,
                output_tokens=total_output,
                cache_efficiency_pct=efficiency,
                cost_usd=total_cost,
                stem_version=STEM_VERSION,
            )
            self._publish_status_snapshot(
                model=current_model,
                input_tokens=total_input,
                cache_read_tokens=total_cache_read,
                cache_write_tokens=total_cache_write,
                output_tokens=total_output,
                cost_usd=total_cost,
                saved_usd=max(0.0, total_naive_cost - total_cost),
                cache_efficiency_pct=efficiency,
            )

        if total_cache_read > total_input // 2 and total_input > 0:
            last_assistant = next(
                (
                    message["content"]
                    for message in reversed(messages)
                    if message.get("role") == "assistant" and isinstance(message.get("content"), str)
                ),
                "",
            )
            if last_assistant:
                from lemoncrow.gateway.cli.events import PromptSuggestion as PromptSuggestionEvent

                suggestions = []
                lowered = last_assistant.lower()
                if "error" in lowered or "failed" in lowered:
                    suggestions.append("fix the error")
                if "implement" in lowered or "edit" in lowered:
                    suggestions.append("write tests for this")
                suggestions.append("explain how this works")
                for suggestion in suggestions[:3]:
                    yield PromptSuggestionEvent(type="prompt.suggestion", text=suggestion)

        from lemoncrow.core.capabilities.pricing import get_model_pricing
        from lemoncrow.pro.capabilities.optimization.audit import context_window_for_model

        try:
            priced_window = int(get_model_pricing(current_model).context_window or 0)
        except Exception:
            priced_window = 0
        context_window = priced_window or context_window_for_model(current_model)
        safe_context_limit = max(8_192, int(context_window * 0.80))
        try:
            configured_threshold = int(os.environ.get("LEMONCROW_COMPACT_AT_TOKENS", "120000"))
        except ValueError:
            configured_threshold = 120_000
        try:
            configured_hard_limit = max(
                1,
                int(os.environ.get("LEMONCROW_COMPACT_HARD_TOKENS", "180000")),
            )
        except ValueError:
            configured_hard_limit = 180_000
        hard_limit = min(configured_hard_limit, safe_context_limit)
        compaction_threshold = min(
            configured_threshold if configured_threshold > 0 else safe_context_limit,
            hard_limit,
        )
        compacted_messages, compaction_available = compact_history(
            messages,
            threshold_tokens=compaction_threshold,
        )
        stored_messages = messages
        if compaction_available:
            old_tokens = estimate_context_tokens(messages)
            compacted_tokens = estimate_context_tokens(compacted_messages)
            expected_reads = float(max(1, min(8, last_cache_sample_count + 1)))
            economical = not last_cache_enabled or should_rewrite_compacted_prefix(
                old_tokens=old_tokens,
                compacted_tokens=compacted_tokens,
                expected_future_reads=expected_reads,
            )
            hard_required = old_tokens >= hard_limit
            proposed_compaction = economical or hard_required
            applied_compaction = proposed_compaction if self._optimization_mode == "enforce" else True
            trace.decision(
                "compaction",
                phase=state.phase,
                proposed={
                    "rewrite": proposed_compaction,
                    "old_tokens": old_tokens,
                    "compacted_tokens": compacted_tokens,
                    "expected_future_reads": expected_reads,
                    "hard_required": hard_required,
                },
                actual={"rewrite": applied_compaction},
                reason="cache rewrite must repay its write cost; hard context limit always wins",
                eligible=True,
            )
            if applied_compaction:
                stored_messages = compacted_messages
        self._sessions[session_id] = _trim_history(stored_messages)
        accepted = completed and (state.ready_for_receipt or (state.edit_count == 0 and not mutation_requested))
        trace.finish(accepted=accepted, error_code=None if completed else "incomplete")

    async def handle_slash_command(
        self,
        session_id: str,
        name: str,
        args: list[str],
    ) -> AsyncIterator[LemonCrowEvent]:
        if name == "help":
            yield AssistantMessage(type="assistant.message", text=_HELP_TEXT)
        elif name in ("tools", "tool"):
            tools = _get_litellm_tools()
            lines = [f"**{t['function']['name']}** — {t['function']['description'][:80]}" for t in tools]
            yield AssistantMessage(type="assistant.message", text="\n".join(lines))
        elif name in ("resume", "sessions"):
            # `/resume <id>` loads a specific session; otherwise list available ones.
            if name == "resume" and args and args[0].strip():
                async for ev in self.handle_slash_command(session_id, "session", args):
                    yield ev
                return

            import datetime

            from lemoncrow.core.foundation.paths import default_store_root

            runs_dir = default_store_root() / "runs"
            # Only show actual TUI sessions (not _context_savings files).
            patterns = ["tui-*.jsonl", "lemoncrow-run-*.jsonl"]
            session_files: list[Path] = []
            for pat in patterns:
                session_files.extend(runs_dir.glob(pat))

            # Sort by mtime descending.
            session_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

            if not session_files:
                yield AssistantMessage(
                    type="assistant.message",
                    text=("No saved TUI sessions found.\n\nSessions are saved when you start a task in the TUI."),
                )
                return

            lines = ["**Saved sessions** (use `/resume <id>` to load one):\n"]
            for f in session_files[:20]:
                sid = f.stem
                mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                size_kb = round(f.stat().st_size / 1024, 1)
                lines.append(f"- `{sid}` — {mtime} ({size_kb}KB)")

            yield AssistantMessage(type="assistant.message", text="\n".join(lines))
        elif name == "session":
            target = args[0] if args else ""
            if not target:
                async for ev in self.handle_slash_command(session_id, "sessions", []):
                    yield ev
                return
            # Try in-memory first; otherwise load from the JSONL run ledger.
            if target not in self._sessions:
                try:
                    from lemoncrow.pro.capabilities.owned_agent_session.session import (
                        OwnedAgentSession,
                    )

                    saved = OwnedAgentSession.load(target)
                    self._sessions[target] = list(saved.messages)
                except FileNotFoundError:
                    yield RuntimeErrorEvent(
                        type="error",
                        message=f"Session '{target}' not found in runs/",
                    )
                    return
            # Replace the current session's conversation with the loaded messages.
            loaded_messages = self._sessions.get(target, [])
            self._sessions[session_id] = list(loaded_messages)
            turn_count = len([m for m in loaded_messages if isinstance(m, dict) and m.get("role") == "user"])
            yield AssistantMessage(
                type="assistant.message",
                text=f"\u2713 Loaded session `{target}` ({turn_count} turns). Conversation replaced.",
            )
        elif name == "memory":
            async for event in self._run_memory_search(" ".join(args)):
                yield event
        elif name == "route":
            async for event in self._run_route(" ".join(args)):
                yield event
        elif name == "approve":
            pending = list(self._pending_permissions.keys())
            if pending:
                self._pending_permissions[pending[-1]]["approved"] = True
                yield AssistantMessage(type="assistant.message", text=f"Approved: {pending[-1]}")
            else:
                yield AssistantMessage(type="assistant.message", text="No pending permission requests.")
        elif name == "deny":
            pending = list(self._pending_permissions.keys())
            if pending:
                self._pending_permissions[pending[-1]]["approved"] = False
                yield AssistantMessage(type="assistant.message", text=f"Denied: {pending[-1]}")
            else:
                yield AssistantMessage(type="assistant.message", text="No pending permission requests.")
        elif name == "set-model":
            model = args[0] if args else ""
            if model:
                self._override_model = model
                yield AssistantMessage(
                    type="assistant.message",
                    text=f"Model set to `{model}`. Type a message to start.",
                )
            else:
                yield RuntimeErrorEvent(type="error", message="Usage: /set-model <model>")
        elif name == "model":
            if args and args[0]:
                model_str = args[0]
                self._override_model = model_str
                yield AssistantMessage(
                    type="assistant.message",
                    text=f"Model switched to `{model_str}`. Changes take effect on your next message.",
                )
            else:
                current = self._override_model or "(auto-routed)"
                yield AssistantMessage(
                    type="assistant.message",
                    text=(
                        f"Current model: `{current}`\n\n"
                        "Usage: `/model <model-string>`\n\n"
                        "Examples:\n"
                        "- `/model anthropic/claude-opus-4-8`\n"
                        "- `/model openrouter/anthropic/claude-opus-4-8`\n"
                        "- `/model bedrock/anthropic.claude-sonnet-4-5-v1:0`\n"
                        "- `/model azure/gpt-4o`"
                    ),
                )
        elif name == "context":
            messages = self._sessions.get(session_id, [])
            turns = len(messages) // 2
            total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
            approx_tokens = total_chars // 4
            tool_results = len([m for m in messages if isinstance(m, dict) and m.get("role") == "tool"])
            yield AssistantMessage(
                type="assistant.message",
                text=(
                    "**Context stats**\n\n"
                    f"- Turns: {turns}\n"
                    f"- Messages: {len(messages)}\n"
                    f"- Estimated tokens: ~{approx_tokens:,}\n"
                    f"- Tool results: {tool_results}\n"
                ),
            )
        elif name == "usage":
            messages = self._sessions.get(session_id, [])
            total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
            approx_tokens = total_chars // 4
            user_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
            asst_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]
            tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
            yield AssistantMessage(
                type="assistant.message",
                text=(
                    "**Token Usage**\n\n"
                    "| Category | Count |\n"
                    "|----------|-------|\n"
                    f"| User turns | {len(user_msgs)} |\n"
                    f"| Assistant turns | {len(asst_msgs)} |\n"
                    f"| Tool results | {len(tool_msgs)} |\n"
                    f"| ~Total chars | {total_chars:,} |\n"
                    f"| ~Total tokens | {approx_tokens:,} |\n"
                    f"| Model | `{self._override_model or '(auto)'}` |\n"
                    f"| Mode | `{self._current_mode}` |\n"
                    "\nTo see cost and savings: `/analytics`"
                ),
            )
        elif name == "permissions":
            mode = self._current_mode
            perm_tools = self._active_tools or [
                "read",
                "edit",
                "bash",
                "grep",
            ]
            perm_map = {
                "edit": "ask" if not self._yolo else "allow",
                "bash": "ask" if not self._yolo else "allow",
                "read": "allow",
                "grep": "allow",
            }
            lines = [f"**Permissions** (mode: {mode})\n"]
            for perm_tool in perm_tools:
                perm = perm_map.get(perm_tool, "allow")
                icon = "✓" if perm == "allow" else "?"
                lines.append(f"- `{perm_tool}` {icon} {perm}")
            lines.append(f"\nYOLO mode: {'on' if self._yolo else 'off'}")
            lines.append("Use `--yolo` to skip all approval prompts.")
            yield AssistantMessage(type="assistant.message", text="\n".join(lines))
        elif name == "yolo":
            self._yolo = not self._yolo
            yield AssistantMessage(
                type="assistant.message",
                text=(
                    f"✓ YOLO mode {'enabled' if self._yolo else 'disabled'}. "
                    + ("Tool calls auto-approved." if self._yolo else "Tool calls will ask for approval.")
                ),
            )
        elif name in ("mode", "agents"):
            mode_name = args[0].lower() if args else ""
            tools_by_mode = {
                "code": ["read", "edit", "bash", "grep"],
                "explore": ["read", "grep"],
                "research": ["read", "grep"],
                "plan": ["read", "grep"],
            }
            if mode_name in tools_by_mode:
                self._active_tools = tools_by_mode[mode_name]
                self._current_mode = mode_name
                yield AssistantMessage(
                    type="assistant.message",
                    text=(f"Switched to **{mode_name.upper()}** mode. Tools: {', '.join(self._active_tools)}"),
                )
            else:
                yield AssistantMessage(
                    type="assistant.message",
                    text="Available modes: code, explore, research, plan",
                )
        elif name == "analytics":
            try:
                from lemoncrow.core.capabilities.analytics.store import AnalyticsStore

                store = AnalyticsStore()
                stats = store.summary_stats()
                recent_sessions = store.recent_sessions(5)
                store.close()

                lines = ["**Session Analytics**\n"]
                lines.append("| Metric | Value |")
                lines.append("|--------|-------|")
                lines.append(f"| Total sessions | {stats.get('total_sessions', 0)} |")
                lines.append(f"| Total cost | ${stats.get('total_cost_usd', 0):.4f} |")
                lines.append(f"| Total savings | ${stats.get('total_savings_usd', 0):.4f} |")
                lines.append(f"| Avg cache efficiency | {stats.get('avg_cache_efficiency_pct', 0):.1f}% |")
                lines.append(f"| Total turns | {stats.get('total_turns', 0)} |")
                lines.append("")
                if recent_sessions:
                    lines.append("**Recent sessions:**")
                    for sess in recent_sessions:
                        lines.append(f"- `{sess.session_id}` — {sess.mode} — ${sess.total_cost_usd:.4f}")
                yield AssistantMessage(type="assistant.message", text="\n".join(lines))
            except Exception as exc:
                yield AssistantMessage(type="assistant.message", text=f"Analytics unavailable: {exc}")
        elif name == "mcp":
            import json as _json

            mcp_files = [
                Path.cwd() / ".mcp.json",
                Path.cwd() / ".claude" / "mcp.json",
                Path.home() / ".lemoncrow" / "tui" / ".mcp.json",
                Path.home() / ".claude" / "claude_mcp_settings.json",
            ]
            all_servers: dict[str, dict[str, Any]] = {}
            for mcp_file in mcp_files:
                if mcp_file.exists():
                    try:
                        data = _json.loads(mcp_file.read_text())
                        servers = data.get("mcpServers") or data.get("servers") or {}
                        for name_key, cfg in servers.items():
                            all_servers[name_key] = {"config": cfg, "source": str(mcp_file)}
                    except Exception:
                        pass

            if all_servers:
                lines = [f"**MCP Servers** ({len(all_servers)} configured)\n"]
                for srv_name, info in all_servers.items():
                    cfg = info["config"]
                    cmd = cfg.get("command", "?")
                    cmd_args = " ".join(str(a) for a in cfg.get("args", []))
                    lines.append(f"- **{srv_name}** — `{cmd} {cmd_args}` _(from {info['source']})_")
                lines.append("\nTo use MCP tools in conversations, start the server and reference its tools.")
                yield AssistantMessage(type="assistant.message", text="\n".join(lines))
            else:
                yield AssistantMessage(
                    type="assistant.message",
                    text=(
                        "**No MCP servers configured.**\n\n"
                        "Add servers to one of:\n"
                        "- `.mcp.json` in your project root\n"
                        "- `~/.lemoncrow/tui/.mcp.json` (global)\n\n"
                        "Format:\n```json\n"
                        '{"mcpServers": {"my-server": {"command": "npx", '
                        '"args": ["my-mcp-package"]}}}\n```'
                    ),
                )
        elif name == "compact":
            messages = self._sessions.get(session_id, [])
            msg_count = len(messages)
            summary_lines = [
                "**Conversation compacted**\n",
                f"(Previous: {msg_count} messages)\n",
            ]
            # Preserve the leading system message, then keep a recent tail.
            head = messages[:1] if messages and messages[0].get("role") == "system" else []
            tail = messages[len(head) :]
            recent = tail[-4:] if len(tail) > 4 else tail
            # A tail must not begin with an orphaned tool_result, nor with an
            # assistant message whose tool_calls lost their results to the cut —
            # providers 400 on a leading tool_result without a preceding tool_use.
            while recent and (recent[0].get("role") == "tool" or recent[0].get("tool_calls")):
                recent = recent[1:]
            self._sessions[session_id] = head + list(recent)
            yield AssistantMessage(type="assistant.message", text="\n".join(summary_lines))
        elif name == "cost":
            yield AssistantMessage(
                type="assistant.message",
                text=(
                    "**Session cost**\n\n"
                    f"Model: `{self._override_model or '(auto-routed)'}`\n"
                    f"Mode: `{self._current_mode}`\n\n"
                    "Use `/analytics` for detailed breakdown."
                ),
            )
        elif name == "doctor":
            from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import (
                detect_api_key_vendors,
            )

            vendors = detect_api_key_vendors()
            lines = ["**LemonCrow Health Check**\n"]
            lines.append(f"- API keys: {', '.join(vendors) if vendors else 'none configured ⚠'}")
            try:
                from lemoncrow import __version__

                lines.append(f"- Version: `{__version__}`")
            except Exception:
                lines.append("- Version: unknown")
            import shutil

            tools_status = {
                "git": bool(shutil.which("git")),
                "uv": bool(shutil.which("uv")),
                "cargo": bool(shutil.which("cargo")),
                "mitmdump": bool(shutil.which("mitmdump")),
                "cloudflared": bool(shutil.which("cloudflared")),
            }
            for tool, ok in tools_status.items():
                lines.append(f"- {tool}: {'✓' if ok else '✗ not found'}")
            yield AssistantMessage(type="assistant.message", text="\n".join(lines))
        elif name == "allowed-tools":
            tools = _get_litellm_tools()
            active = self._active_tools
            lines = [f"**Available tools** (mode: {self._current_mode})\n"]
            for t in tools:
                fn = t["function"]
                is_active = active is None or fn["name"] in active
                status = "✓" if is_active else "○ (inactive in this mode)"
                lines.append(f"- `{fn['name']}` {status} — {fn['description'][:60]}")
            yield AssistantMessage(type="assistant.message", text="\n".join(lines))
        elif name == "version":
            try:
                from lemoncrow import __version__

                yield AssistantMessage(type="assistant.message", text=f"LemonCrow `{__version__}`")
            except Exception:
                yield AssistantMessage(type="assistant.message", text="LemonCrow (version unknown)")
        elif name == "newtask":
            self._sessions[session_id] = []
            yield AssistantMessage(
                type="assistant.message",
                text="✓ New task started. Conversation cleared.",
            )
        elif name == "checkpoint":
            from lemoncrow.pro.capabilities.owned_agent_session.checkpoint import (
                save_checkpoint,
            )

            messages = self._sessions.get(session_id, [])
            label = " ".join(args) if args else ""
            cp = save_checkpoint(session_id, messages, label=label)
            yield AssistantMessage(
                type="assistant.message",
                text=(f"✓ Checkpoint saved: `{cp.id}` — {cp.message_count} messages\n\nRestore: `/rewind {cp.id}`"),
            )
        elif name == "rewind":
            cp_id = args[0] if args else ""
            if not cp_id:
                from lemoncrow.pro.capabilities.owned_agent_session.checkpoint import (
                    list_checkpoints,
                )

                cps = list_checkpoints(session_id)
                if cps:
                    lines = ["**Checkpoints:**\n"]
                    for cp in cps:
                        lines.append(f"- `{cp.id}` — {cp.label} ({cp.message_count} messages) — {cp.created_at[:16]}")
                    lines.append("\nRestore: `/rewind <id>`")
                    yield AssistantMessage(type="assistant.message", text="\n".join(lines))
                else:
                    yield AssistantMessage(
                        type="assistant.message",
                        text="No checkpoints. Create one: `/checkpoint [label]`",
                    )
            else:
                try:
                    from lemoncrow.pro.capabilities.owned_agent_session.checkpoint import (
                        load_checkpoint,
                    )

                    messages = load_checkpoint(cp_id, session_id)
                    self._sessions[session_id] = messages
                    yield AssistantMessage(
                        type="assistant.message",
                        text=f"✓ Rewound to checkpoint `{cp_id}` — {len(messages)} messages restored",
                    )
                except FileNotFoundError:
                    yield RuntimeErrorEvent(type="error", message=f"Checkpoint `{cp_id}` not found")
        elif name == "bash":
            cmd = " ".join(args) if args else ""
            if cmd:
                from lemoncrow.gateway.adapters.mcp_server import tool_bash

                try:
                    result = await asyncio.to_thread(tool_bash, {"command": cmd, "timeout": 30})
                    yield AssistantMessage(type="assistant.message", text=f"```\n{result}\n```")
                except Exception as exc:
                    yield RuntimeErrorEvent(type="error", message=f"Shell failed: {exc}")
            else:
                yield RuntimeErrorEvent(type="error", message="Usage: !<command>")
        elif name == "tasks":
            if not self._background_tasks:
                yield AssistantMessage(type="assistant.message", text="No background tasks.")
                return
            lines = ["**Background tasks:**\n"]
            for t in self._background_tasks:
                status_icon = {"running": "⟳", "done": "✓", "failed": "✗"}.get(t["status"], "?")
                lines.append(f"- `{t['id']}` {status_icon} {t['name']}")
            yield AssistantMessage(type="assistant.message", text="\n".join(lines))
        elif name == "background":
            task_id = f"bg-{uuid.uuid4().hex[:6]}"
            self._background_tasks.append(
                {
                    "id": task_id,
                    "name": f"session-{session_id[:8]}",
                    "status": "running",
                }
            )
            yield AssistantMessage(
                type="assistant.message",
                text=f"Session backgrounded as task `{task_id}`. Use `/tasks` to check status.",
            )
        elif name == "plan":
            task = " ".join(args) if args else ""
            if task:
                old_mode = self._current_mode
                old_tools = self._active_tools
                self._current_mode = "explore"
                self._active_tools = ["read", "grep"]
                yield AssistantMessage(
                    type="assistant.message",
                    text=f"**Plan mode** — exploring (read-only):\n\n> {task}",
                )
                async for event in self.handle_user_message(session_id, task):
                    yield event
                self._current_mode = old_mode
                self._active_tools = old_tools
            else:
                yield AssistantMessage(
                    type="assistant.message",
                    text="Usage: `/plan <task description>`\n\nRuns exploration-only (read-only, no edits).",
                )
        elif name == "btw":
            question = " ".join(args) if args else ""
            if not question:
                yield AssistantMessage(
                    type="assistant.message",
                    text="Usage: `/btw <question>`\n\nAsks an ephemeral question without adding to conversation history.",
                )
                return
            ephemeral_messages = [
                {
                    "role": "system",
                    "content": "Answer the following question concisely. This is a side question.",
                },
                {"role": "user", "content": question},
            ]
            from lemoncrow.pro.capabilities.owned_agent_session.phase_runner import (
                _call_llm,
            )

            model = self._override_model or "gpt-4o-mini"
            try:
                content, *_ = _call_llm(ephemeral_messages, model=model, provider="openai")
                yield AssistantMessage(type="assistant.message", text=f"**(btw)** {content}")
            except Exception as exc:
                yield RuntimeErrorEvent(type="error", message=f"/btw failed: {exc}")
        elif name == "auth":
            from lemoncrow.core.capabilities.auth.wizard import (
                PROVIDER_CONFIGS,
                list_provider_models,
                load_saved_credentials,
                save_credentials,
                validate_provider,
            )
            from lemoncrow.gateway.cli.events import ChoiceRequested

            if not args:
                creds = load_saved_credentials()
                configured_keys = set(creds.keys())
                lines = ["**Provider Authentication**\n"]
                lines.append("| Provider | Status | Keys |")
                lines.append("|----------|--------|------|")
                for _pid, cfg in PROVIDER_CONFIGS.items():
                    keys = [f["name"] for f in cfg["fields"]]
                    has_all = all(k in configured_keys or k in os.environ for k in keys)
                    status = "✓ configured" if has_all else "○ not set"
                    lines.append(f"| {cfg['name'][:25]} | {status} | {', '.join(keys[:2])} |")
                lines.append("\nTo configure a provider: `/auth <provider-id>`")
                lines.append("Example: `/auth anthropic`, `/auth openai`, `/auth groq`")
                lines.append(f"Supported: {', '.join(PROVIDER_CONFIGS.keys())}")
                yield AssistantMessage(type="assistant.message", text="\n".join(lines))
                return

            provider_id = args[0].lower()
            cfg = PROVIDER_CONFIGS.get(provider_id)
            if not cfg:
                yield RuntimeErrorEvent(
                    type="error",
                    message=f"Unknown provider: {provider_id!r}. Try: {', '.join(PROVIDER_CONFIGS.keys())}",
                )
                return

            fields_text = "\n".join(f"  • {f['label']}" for f in cfg["fields"])
            yield AssistantMessage(
                type="assistant.message",
                text=(
                    f"**Configuring {cfg['name']}**\n\n"
                    f"Required credentials:\n{fields_text}\n\n"
                    f"Get your credentials at: {cfg['link']}\n\n"
                    f"Enter credentials in order (one per message):"
                ),
            )

            collected: dict[str, str] = {}
            for field_cfg in cfg["fields"]:
                field_name = field_cfg["name"]
                default = field_cfg.get("default", "")
                prompt_text = f"{field_cfg['label']}" + (f" [default: {default}]" if default else "")
                choice_id = f"auth-{field_name}"
                self._pending_permissions[choice_id] = {"approved": None, "response": None}
                _evict_oldest(self._pending_permissions, _MAX_PENDING_PERMISSIONS)
                yield ChoiceRequested(
                    type="choice.requested",
                    id=choice_id,
                    question=prompt_text,
                    choices=[f"Use default ({default})"] if default else [],
                    allow_freeform=True,
                )
                for _ in range(600):
                    await asyncio.sleep(0.1)
                    resp = self._pending_permissions.get(choice_id, {}).get("response")
                    if resp is not None:
                        break
                val = str(self._pending_permissions.get(choice_id, {}).get("response", default) or default)
                if val:
                    collected[field_name] = val

            if collected:
                ok, msg = validate_provider(provider_id, collected)
                if ok:
                    save_credentials(collected)
                    for k, v in collected.items():
                        os.environ[k] = v
                    yield AssistantMessage(
                        type="assistant.message",
                        text=f"{msg}\n\nCredentials saved to `~/.lemoncrow/.env`",
                    )
                    models = list_provider_models(provider_id)
                    if models:
                        yield AssistantMessage(
                            type="assistant.message",
                            text="Available models:\n"
                            + "\n".join(f"- `{m}`" for m in models)
                            + f"\n\nUse: `/model {models[0]}`",
                        )
                else:
                    yield AssistantMessage(
                        type="assistant.message",
                        text=f"{msg}\n\nPlease check your credentials and try again.",
                    )
        elif name == "share":
            import secrets

            token = secrets.token_urlsafe(12)
            self._share_tokens[session_id] = token
            _evict_oldest(self._share_tokens, _MAX_SHARE_TOKENS)

            local_url = f"http://localhost:{os.environ.get('LEMONCROW_WEB_PORT', '7700')}/share/{token}"
            yield AssistantMessage(
                type="assistant.message",
                text=(
                    f"**Session shared (read-only)**\n\n"
                    f"Share this URL with collaborators:\n\n"
                    f"`{local_url}`\n\n"
                    f"If tunnel is active, use the public URL instead:\n"
                    f"`<tunnel_url>/share/{token}`\n\n"
                    f"Collaborators can observe the conversation in real-time but cannot send commands."
                ),
            )
        elif name in ("verify", "diff"):
            yield AssistantMessage(
                type="assistant.message",
                text=f"/{name} not yet wired. Use plain message instead.",
            )
        else:
            yield RuntimeErrorEvent(
                type="error",
                message=f"Unknown command: /{name}. Type /help for commands.",
            )

    async def _run_memory_search(self, query: str) -> AsyncIterator[LemonCrowEvent]:
        if not query:
            yield RuntimeErrorEvent(type="error", message="Usage: /memory <query>")
            return
        try:
            from lemoncrow.gateway.adapters.mcp_server import tool_memory

            result = await asyncio.to_thread(tool_memory, {"op": "recall", "query": query, "top_k": 5})
            yield MemoryHit(type="memory.hit", key=query, summary=str(result)[:2000])
        except Exception as exc:
            yield RuntimeErrorEvent(type="error", message=f"Memory search failed: {exc}")

    async def _run_route(self, task: str) -> AsyncIterator[LemonCrowEvent]:
        if not task:
            yield RuntimeErrorEvent(type="error", message="Usage: /route <task description>")
            return
        try:
            from lemoncrow.pro.capabilities.owned_execution_routing import (
                OwnedRouteRequest,
                select_owned_route,
            )

            decision = select_owned_route(
                self._root,
                OwnedRouteRequest(tool_name="tui", task_text=task, mode="auto", budget="balanced"),
            )
            yield RouteSelected(
                type="route.selected",
                provider=decision.provider,
                model=decision.model,
                reason=decision.reason,
            )
        except Exception as exc:
            yield RuntimeErrorEvent(type="error", message=f"Route selection failed: {exc}")

    async def respond_to_permission(
        self,
        session_id: str,
        permission_id: str,
        approved: bool,
        scope: str = "once",
    ) -> AsyncIterator[LemonCrowEvent]:
        self._pending_permissions[permission_id] = {"approved": approved}
        _evict_oldest(self._pending_permissions, _MAX_PENDING_PERMISSIONS)
        yield AssistantMessage(
            type="assistant.message",
            text=f"Permission {'approved' if approved else 'denied'}: {permission_id}",
        )

    async def interrupt(self, session_id: str) -> None:
        return None


_HELP_TEXT = """
**LemonCrow Interactive CLI**

Commands:
- `/help` — show this help
- `/agents` — switch agent mode (code, explore, research, plan)
- `/exit`, `/quit` — exit
- `/clear` — clear screen
- `/tools` — list available tools
- `/sessions` — list sessions
- `/session <id>` — switch session
- `/memory <query>` — search LemonCrow memory
- `/route <task>` — show routing decision for task
- `/approve` — approve latest permission request
- `/deny` — deny latest permission request

Type any message to start a coding session.
""".strip()
_OWNED_TOOL_NAMES = (
    "read",
    "grep",
    "edit",
    "bash",
)

# Owned tools that are safe to execute concurrently (everything read-only).
_PARALLEL_SAFE_TOOLS = frozenset(_OWNED_TOOL_NAMES) - {"edit", "bash"}


async def _aiter_sync_stream(stream: Any) -> AsyncIterator[Any]:
    """Iterate a synchronous litellm stream without blocking the event loop."""
    sentinel = object()
    iterator = iter(stream)
    while True:
        chunk = await asyncio.to_thread(next, iterator, sentinel)
        if chunk is sentinel:
            return
        yield chunk


# The MCP host surface still exposes these (user-authorized overrides); hiding
# them here is owned-loop policy only. Single source of truth for both
# _get_litellm_tools (schema) and _dispatch_tool (args).
_OWNED_HIDDEN_PARAMS: dict[str, tuple[str, ...]] = {}


def _get_litellm_tools() -> list[dict[str, Any]]:
    """Return canonical MCP tool definitions for the owned coding runtime."""
    from lemoncrow.gateway.adapters.mcp_server import TOOLS

    tools: list[dict[str, Any]] = []
    for name in _OWNED_TOOL_NAMES:
        spec = TOOLS.get(name)
        if spec is None:
            raise RuntimeError(
                f"Owned tool {name!r} is missing from the MCP registry; "
                "update _OWNED_TOOL_NAMES to match the registered tool names."
            )
        parameters = copy.deepcopy(spec.get("inputSchema") or {})
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            for hidden in _OWNED_HIDDEN_PARAMS.get(name, ()):
                properties.pop(hidden, None)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(spec.get("description") or ""),
                    "parameters": parameters,
                },
            }
        )
    return tools


def _dispatch_tool(name: str, args: dict[str, Any]) -> Any:
    """Dispatch through the canonical MCP registry used by plugin integrations."""
    from lemoncrow.gateway.adapters.mcp_server import TOOLS

    spec = TOOLS.get(name)
    if spec is None or name not in _OWNED_TOOL_NAMES:
        raise ValueError(f"Unknown tool: {name!r}")
    hidden = _OWNED_HIDDEN_PARAMS.get(name, ())
    if hidden:
        args = {key: value for key, value in args.items() if key not in hidden}
    handler = spec.get("handler")
    if not callable(handler):
        raise ValueError(f"Tool has no callable handler: {name!r}")
    return handler(args)


# Read-style tools eligible for within-session byte-identical dedup.
_CLI_DEDUP_TOOLS = frozenset({"read", "search", "grep"})


def _render_tool_result(name: str, result: Any, args: dict[str, Any], *, session_id: str = "") -> str:
    """Render a tool result as the compact model-facing text the MCP path emits.

    Falls back to compact JSON (never Python ``repr``) when no renderer applies,
    then applies within-session content dedup for read-style tools so a
    byte-identical re-read costs a short stub instead of the full payload.
    """
    from lemoncrow.gateway.adapters.mcp_server import render_tool_result_text

    text: str | None = None
    with contextlib.suppress(Exception):
        text = render_tool_result_text(name, result)
    if text is None:
        if isinstance(result, str):
            text = result
        else:
            try:
                text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                text = str(result)
    if session_id and name in _CLI_DEDUP_TOOLS and os.environ.get("LEMONCROW_CONTEXT_DEDUP", "1") != "0":
        with contextlib.suppress(Exception):
            from lemoncrow.pro.capabilities import context_dedup

            outcome = context_dedup.registry().stub_for(
                session_id=session_id,
                content=text,
                epoch=context_dedup.current_epoch(),
                force=bool(args.get("force")),
            )
            if outcome is None and name == "read":
                from lemoncrow.gateway.adapters.mcp_server import _read_dedup_resource

                resource = _read_dedup_resource(args)
                if resource:
                    outcome = context_dedup.registry().delta_for(
                        session_id=session_id,
                        resource=resource,
                        content=text,
                        epoch=context_dedup.current_epoch(),
                        force=bool(args.get("force")),
                    )
            if outcome is not None:
                text = outcome[0]
    return text
