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
from lemoncrow.gateway.cli.events import (
    AssistantDelta,
    AssistantMessage,
    LemonCrowEvent,
    MemoryHit,
    PermissionRequested,
    RouteSelected,
    RuntimeErrorEvent,
    ToolFinished,
    ToolOutput,
    ToolRequested,
    ToolStarted,
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
    ) -> None:
        self._root = root or Path.home() / ".lemoncrow"
        self._yolo = yolo
        self._provider_override = provider
        self._override_model = model
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        self._pending_permissions: dict[str, dict[str, Any]] = {}
        self._active_tools: list[str] | None = None
        self._current_mode: str = "code"
        self._mcp_servers: list[MCPServerProcess] = []
        self._mcp_tools: list[MCPTool] = []
        self._mcp_lock = threading.Lock()
        self._mcp_startup_thread: threading.Thread | None = None
        self._background_tasks: list[dict[str, Any]] = []  # {id, name, status, result}
        self._share_tokens: dict[str, str] = {}

    async def start_session(
        self,
        project_root: str | None = None,
        *,
        session_id: str | None = None,
    ) -> str:
        session_id = session_id or uuid.uuid4().hex
        while len(self._sessions) >= _MAX_TRACKED_SESSIONS:
            self._sessions.pop(next(iter(self._sessions)))
        self._sessions[session_id] = []
        if project_root:
            os.environ["CLAUDE_WORKSPACE_ROOT"] = project_root
        self._start_mcp_servers()
        return session_id

    def _start_mcp_servers(self) -> None:
        """Start MCP servers in a background thread to avoid blocking session start."""

        def _start() -> None:
            try:
                from lemoncrow.core.capabilities.mcp_integration.loader import (
                    MCPServerProcess,
                    discover_mcp_configs,
                )

                configs = discover_mcp_configs()
                for cfg in configs:
                    proc = MCPServerProcess(cfg)
                    if proc.start():
                        tools = proc.list_tools()
                        with self._mcp_lock:
                            self._mcp_servers.append(proc)
                            self._mcp_tools.extend(tools)
                        logger.info("Started MCP server %s with %d tools", cfg.name, len(tools))
            except Exception:  # noqa: BLE001
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
    ) -> list[dict[str, Any]]:
        """Move the Anthropic cache boundary to the latest completed message."""
        from lemoncrow.core.capabilities.owned_agent_session.phase_runner import _provider_cache_style

        if _provider_cache_style(self._provider_override or "", model) != "anthropic":
            return messages
        # Shallow-copy the list so the two messages we re-wrap below don't mutate
        # the persisted history, then deep-copy only those messages. A full
        # deepcopy of the whole history here runs on every agent-loop iteration,
        # so its cost scales as iterations x history-size.
        request_messages = list(messages)
        first_index = 0 if request_messages else None
        if (
            first_index is not None
            and request_messages[0].get("role") == "system"
            and isinstance(request_messages[0].get("content"), str)
            and request_messages[0]["content"]
        ):
            # Static breakpoint on the system message: pins the tools+system
            # prefix so every call gets a cache hit there regardless of how far
            # the moving trailing breakpoint has advanced (Anthropic's automatic
            # prefix lookback only scans a bounded number of blocks before each
            # explicit breakpoint).
            first = copy.deepcopy(request_messages[0])
            first["content"] = [
                {
                    "type": "text",
                    "text": first["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            request_messages[0] = first
        # Moving breakpoint on the latest completed message.
        for index in range(len(request_messages) - 1, -1, -1):
            content = request_messages[index].get("content")
            if not isinstance(content, str) or not content:
                continue
            message = copy.deepcopy(request_messages[index])
            message["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            request_messages[index] = message
            break
        return request_messages

    async def _completion_with_backoff(self, request_kwargs: dict[str, Any]) -> Any:
        """Call LiteLLM with bounded exponential backoff for provider throttling."""
        import litellm

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

    async def _execute_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        session_id: str = "",
    ) -> tuple[str, bool]:
        """Execute one built-in or external MCP tool without blocking the event loop."""
        try:
            if tool_name.startswith("mcp__"):
                result_str = await asyncio.to_thread(self._dispatch_mcp_tool, tool_name, tool_args)
                return result_str, not result_str.startswith("Error:")
            result = await asyncio.to_thread(_dispatch_tool, tool_name, tool_args)
            return _render_tool_result(tool_name, result, tool_args, session_id=session_id), True
        except Exception as exc:  # noqa: BLE001 - tool failures are model-visible results
            return f"Error: {exc}", False

    def _dispatch_mcp_tool(self, tool_name: str, tool_args: dict[str, Any]) -> str:
        """Route an ``mcp__<server>__<tool>`` call to the right MCP server."""
        parts = tool_name.split("__", 2)
        if len(parts) != 3:
            return f"Error: malformed MCP tool name '{tool_name}'"
        _, server_name, actual_tool = parts
        for server in self._mcp_servers:
            if server.config.name == server_name:
                return server.call_tool(actual_tool, tool_args)
        return f"Error: MCP server '{server_name}' not found"

    @property
    def session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return a copy of the normalized conversation for persistence or APIs."""
        return list(self._sessions.get(session_id, ()))

    async def handle_user_message(
        self,
        session_id: str,
        text: str,
        *,
        model_override: str | None = None,
        budget_hint: str = "balanced",
    ) -> AsyncIterator[LemonCrowEvent]:
        messages = self._sessions.setdefault(session_id, [])

        # Inject mode prefix in the user message (NOT in the system prompt) so the
        # system prefix stays byte-identical across modes for maximum cache reuse.
        mode_prefix = {
            "code": "[MODE: code]",
            "explore": "[MODE: explore — no edits please]",
            "research": "[MODE: research — no edits]",
            "plan": "[MODE: plan — no edits]",
        }.get(self._current_mode, "")
        prefixed_text = f"{mode_prefix} {text}".strip() if mode_prefix else text
        messages.append({"role": "user", "content": prefixed_text})

        if model_override or self._override_model:
            model = model_override or self._override_model  # type: ignore[assignment]
            yield RouteSelected(
                type="route.selected",
                provider=self._provider_override if not model_override else None,
                model=model,
                reason="api model override" if model_override else "user override (/set-model)",
            )
            async for event in self._agent_loop(session_id, messages, model=model or ""):  # type: ignore[arg-type]
                yield event
            return

        try:
            from lemoncrow.core.capabilities.owned_execution_routing import (
                OwnedRouteRequest,
                select_owned_route,
            )
            from lemoncrow.gateway.cli.commands.run import _resolve_litellm_model

            decision = select_owned_route(
                self._root,
                OwnedRouteRequest(tool_name="tui", task_text=text, mode="auto", budget=budget_hint),  # type: ignore[arg-type]
            )
            model = _resolve_litellm_model(decision.provider, decision.model)
            yield RouteSelected(
                type="route.selected",
                provider=decision.provider,
                model=decision.model,
                reason=decision.reason,
            )
        except Exception:  # noqa: BLE001 - fall back gracefully
            model = os.environ.get("LEMONCROW_LITELLM_MODEL", "gpt-4o-mini")

        async for event in self._agent_loop(session_id, messages, model=model):
            yield event

    async def _agent_loop(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_iterations: int = 100,
    ) -> AsyncIterator[LemonCrowEvent]:
        from lemoncrow.core.capabilities.owned_agent_session.phase_runner import _system_message

        # Stable, immutable generic system prompt shared across ALL modes and turns.
        # The mode is injected as a user-message prefix (see handle_user_message),
        # never here — this keeps the system prefix byte-identical for cache reuse.
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, _system_message(self._provider_override or "", model))

        tools = [
            t for t in _get_litellm_tools() if self._active_tools is None or t["function"]["name"] in self._active_tools
        ]

        # Add MCP tools as litellm-compatible tool defs
        mcp_litellm_tools = [
            {
                "type": "function",
                "function": {
                    "name": f"mcp__{t.server_name}__{t.name}",
                    "description": f"[MCP:{t.server_name}] {t.description}",
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            }
            for t in self._mcp_tools
        ]
        tools = tools + mcp_litellm_tools

        total_input = total_output = total_cache_read = total_cache_write = 0
        tool_call_counts: dict[str, int] = {}  # normalized name+arguments -> count

        for _ in range(max_iterations):
            accumulated_text = ""
            tool_calls_acc: dict[int, dict[str, Any]] = {}
            finish_reason = ""

            try:
                request_kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": self._messages_with_cache_breakpoint(messages, model),
                    "tools": tools,
                    "tool_choice": "auto",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if model.startswith("bedrock/"):
                    bearer_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
                    if bearer_token:
                        os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
                        request_kwargs["api_key"] = bearer_token
                stream = await self._completion_with_backoff(request_kwargs)
            except Exception as exc:  # noqa: BLE001 - fall back gracefully
                err_str = str(exc)
                if "API_KEY_SERVICE_BLOCKED" in err_str or "PERMISSION_DENIED" in err_str or "403" in err_str:
                    from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import (
                        detect_api_key_vendors,
                    )

                    other_vendors = [v for v in detect_api_key_vendors() if "google" not in v.lower()]
                    fallback_model = os.environ.get("LEMONCROW_LITELLM_MODEL", "gpt-4o-mini")
                    if other_vendors and model != fallback_model:
                        yield RuntimeErrorEvent(
                            type="error",
                            message=(
                                f"Provider {model!r} blocked (API_KEY_SERVICE_BLOCKED). "
                                f"Retrying with {fallback_model!r}."
                            ),
                        )
                        async for event in self._agent_loop(
                            session_id,
                            messages,
                            model=fallback_model,
                            max_iterations=max_iterations - 1,
                        ):
                            yield event
                    else:
                        yield RuntimeErrorEvent(type="error", message=f"LLM call failed: {exc}")
                else:
                    yield RuntimeErrorEvent(type="error", message=f"LLM call failed: {exc}")
                return

            async for chunk in _aiter_sync_stream(stream):
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    total_input += int(getattr(usage, "prompt_tokens", 0) or 0)
                    total_output += int(getattr(usage, "completion_tokens", 0) or 0)
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
                    total_cache_read += cached
                    total_cache_write += cache_write
                    total_input -= cached + cache_write
                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue
                delta = choice.delta
                finish_reason = choice.finish_reason or finish_reason

                if delta.content:
                    accumulated_text += delta.content
                    yield AssistantDelta(type="assistant.delta", text=delta.content)

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc.id or "",
                                "type": "function",
                                "function": {
                                    "name": (tc.function.name if tc.function else "") or "",
                                    "arguments": "",
                                },
                            }
                        if tc.id:
                            tool_calls_acc[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_acc[idx]["function"]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_acc[idx]["function"]["arguments"] += tc.function.arguments

            if not tool_calls_acc or finish_reason == "stop":
                if accumulated_text:
                    messages.append({"role": "assistant", "content": accumulated_text})
                    yield AssistantMessage(type="assistant.message", text=accumulated_text)
                break

            tool_calls_list = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
            messages.append(
                {
                    "role": "assistant",
                    "content": accumulated_text or None,
                    "tool_calls": tool_calls_list,
                }
            )

            looping = False
            for tc in tool_calls_list:
                tool_name = tc["function"]["name"]
                fingerprint = f"{tool_name}:{tc['function']['arguments']}"
                tool_call_counts[fingerprint] = tool_call_counts.get(fingerprint, 0) + 1
                if tool_call_counts[fingerprint] > 3:
                    yield RuntimeErrorEvent(
                        type="error",
                        message=(
                            f"⚠ Loop detected: '{tool_name}' called "
                            f"{tool_call_counts[fingerprint]} times with identical arguments. "
                            "Consider interrupting with Ctrl+C."
                        ),
                    )
                    if tool_call_counts[fingerprint] > 6:
                        looping = True
            if looping:
                break

            prepared_calls: list[tuple[str, str, dict[str, Any]]] = []
            for tc in tool_calls_list:
                tool_id = tc["id"]
                tool_name = tc["function"]["name"]
                try:
                    tool_args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    tool_args = {}

                yield ToolRequested(type="tool.requested", id=tool_id, name=tool_name, args=tool_args)

                if not self._yolo and tool_name in ("edit", "bash"):
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
                    if not self._pending_permissions.get(tool_id, {}).get("approved", False):
                        result_str = "[denied by user]"
                        messages.append({"role": "tool", "tool_call_id": tool_id, "content": result_str})
                        yield ToolFinished(
                            type="tool.finished",
                            id=tool_id,
                            name=tool_name,
                            ok=False,
                            result=result_str,
                        )
                        continue
                prepared_calls.append((tool_id, tool_name, tool_args))

            parallel_tools = _PARALLEL_SAFE_TOOLS
            index = 0
            while index < len(prepared_calls):
                tool_id, tool_name, tool_args = prepared_calls[index]
                if tool_name in parallel_tools or tool_name.startswith("mcp__"):
                    end = index + 1
                    while end < len(prepared_calls) and (
                        prepared_calls[end][1] in parallel_tools or prepared_calls[end][1].startswith("mcp__")
                    ):
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
                    output_preview = result_str[:2000] + ("…" if len(result_str) > 2000 else "")
                    yield ToolOutput(type="tool.output", id=batch_id, chunk=output_preview)
                    yield ToolFinished(
                        type="tool.finished",
                        id=batch_id,
                        name=batch_name,
                        ok=ok,
                        result=result_str[:500],
                    )
                    messages.append({"role": "tool", "tool_call_id": batch_id, "content": result_str})

                    if batch_name == "edit" and ok:
                        try:
                            edited_paths = [
                                str(e.get("file_path") or e.get("path") or "").split("#")[0]
                                for e in batch_args.get("edits", [])
                            ]
                            diff_cmd = ["git", "diff", "--no-color"]
                            if edited_paths and all(edited_paths):
                                diff_cmd += ["--", *edited_paths]
                            raw_diff = await asyncio.to_thread(
                                subprocess.check_output,
                                diff_cmd,
                                cwd=os.getcwd(),
                                stderr=subprocess.DEVNULL,
                            )
                            diff = raw_diff.decode(errors="replace")[:5000]
                            if diff.strip():
                                from lemoncrow.gateway.cli.events import PatchProposed

                                yield PatchProposed(
                                    type="patch.proposed",
                                    id=batch_id,
                                    files=[str(e.get("file_path", "?")) for e in batch_args.get("edits", [])],
                                    diff=diff,
                                )
                        except Exception:  # noqa: BLE001 - diff is best-effort
                            pass
                index = end

            # History must stay append-only: with prompt caching, mutating any
            # prior message (e.g. squashing stale tool output) invalidates the
            # cached prefix and re-writes the whole conversation at the cache
            # write rate (~12.5x the read rate) — measured far more expensive
            # than re-reading the verbose output. Dedup stubs handle repeats
            # without touching history.

        total_input = max(0, total_input)
        denom = total_cache_read + total_cache_write + total_input
        if denom > 0:
            from lemoncrow.core.capabilities.savings_summary import estimate_cost_usd
            from lemoncrow.gateway.cli.events import CacheStats

            efficiency = round(total_cache_read / denom * 100, 1)
            cost = estimate_cost_usd(
                model_id=model,
                input_tokens=total_input,
                output_tokens=total_output,
                cache_read_tokens=total_cache_read,
                cache_write_tokens=total_cache_write,
            )
            naive = estimate_cost_usd(
                model_id=model,
                input_tokens=denom,
                output_tokens=total_output,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
            yield CacheStats(
                type="cache.stats",
                session_id=session_id,
                cache_efficiency_pct=efficiency,
                cost_usd=cost,
                savings_usd=max(0.0, naive - cost),
                cache_read_tokens=total_cache_read,
                cache_write_tokens=total_cache_write,
                fresh_tokens=total_input,
            )
            from lemoncrow.core.capabilities.owned_agent_session.stem_prompt import STEM_VERSION
            from lemoncrow.gateway.cli.events import ContextUsageUpdated

            yield ContextUsageUpdated(
                type="context.usage.updated",
                session_id=session_id,
                input_tokens=total_input,
                cache_read_tokens=total_cache_read,
                cache_write_tokens=total_cache_write,
                output_tokens=total_output,
                cache_efficiency_pct=efficiency,
                cost_usd=cost,
                stem_version=STEM_VERSION,
            )

        self._sessions[session_id] = _trim_history(messages)

        # Warm-cache prompt suggestions: when most of the input was served from
        # cache, surface a few low-cost follow-up prompts.
        if total_cache_read > total_input // 2 and total_input > 0:
            last_assistant = next(
                (
                    m["content"]
                    for m in reversed(messages)
                    if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str)
                ),
                "",
            )
            if last_assistant:
                suggestions = []
                lowered = last_assistant.lower()
                if "error" in lowered or "failed" in lowered:
                    suggestions.append("fix the error")
                if "implement" in lowered or "edit" in lowered:
                    suggestions.append("write tests for this")
                suggestions.append("explain how this works")
                from lemoncrow.gateway.cli.events import (
                    PromptSuggestion as PromptSuggestionEvent,
                )

                for s in suggestions[:3]:
                    yield PromptSuggestionEvent(type="prompt.suggestion", text=s)

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
                    from lemoncrow.core.capabilities.owned_agent_session.session import (
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
            except Exception as exc:  # noqa: BLE001 - analytics is best-effort
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
                    except Exception:  # noqa: BLE001 - config is best-effort
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
            except Exception:  # noqa: BLE001 - version is best-effort
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
            except Exception:  # noqa: BLE001 - version is best-effort
                yield AssistantMessage(type="assistant.message", text="LemonCrow (version unknown)")
        elif name == "newtask":
            self._sessions[session_id] = []
            yield AssistantMessage(
                type="assistant.message",
                text="✓ New task started. Conversation cleared.",
            )
        elif name == "checkpoint":
            from lemoncrow.core.capabilities.owned_agent_session.checkpoint import (
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
                from lemoncrow.core.capabilities.owned_agent_session.checkpoint import (
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
                    from lemoncrow.core.capabilities.owned_agent_session.checkpoint import (
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
                except Exception as exc:  # noqa: BLE001 - shell is best-effort
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
            from lemoncrow.core.capabilities.owned_agent_session.phase_runner import (
                _call_llm,
            )

            model = self._override_model or "gpt-4o-mini"
            try:
                content, *_ = _call_llm(ephemeral_messages, model=model, provider="openai")
                yield AssistantMessage(type="assistant.message", text=f"**(btw)** {content}")
            except Exception as exc:  # noqa: BLE001 - ephemeral call is best-effort
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
        except Exception as exc:  # noqa: BLE001 - fall back gracefully
            yield RuntimeErrorEvent(type="error", message=f"Memory search failed: {exc}")

    async def _run_route(self, task: str) -> AsyncIterator[LemonCrowEvent]:
        if not task:
            yield RuntimeErrorEvent(type="error", message="Usage: /route <task description>")
            return
        try:
            from lemoncrow.core.capabilities.owned_execution_routing import (
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
        except Exception as exc:  # noqa: BLE001 - fall back gracefully
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
            from lemoncrow.core.capabilities import context_dedup

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
