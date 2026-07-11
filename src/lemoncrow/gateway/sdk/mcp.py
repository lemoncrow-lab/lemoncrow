"""MCP-backed SDK client.

This client supports the MCP-standard task tools directly. For richer read
operations like listing Playbooks it falls back to a local store at
``root`` so external hosts can embed LemonCrow without shelling out.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, cast

from lemoncrow.core.foundation.models import (
    RescueResult,
    RubricResult,
    TraceLearning,
    TraceStatus,
    ValidationResult,
)
from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.sdk.client import (
    ContextResult,
    MCPToolTransport,
    MemoryRecallResult,
    TraceRecordResult,
)
from lemoncrow.gateway.sdk.local import LocalClient
from lemoncrow.gateway.trace_payloads import serialize_trace_learnings, serialize_validation_results


class _LoopbackTransport(MCPToolTransport):
    # Aliases the in-process SDK accepts in addition to canonical tool names.
    # ``record`` is a historical synonym for the ``trace`` recorder.
    _ALIASES: ClassVar[dict[str, str]] = {"record": "trace"}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Expose the full registered MCP tool surface to in-process/SDK callers
        # so loopback parity tracks the stdio/HTTP transports automatically as
        # tools are added (workflow, codemod, rename-via-codemod, shell, grep,
        # graph, scan, ...). Each entry is the same dict-accepting handler the
        # MCP dispatcher invokes, so behavior is identical across transports.
        canonical = self._ALIASES.get(name, name)
        spec = mcp_server.TOOLS.get(canonical)
        if spec is None:
            raise KeyError(name)
        handler = cast(Callable[[dict[str, Any]], Any], spec["handler"])
        return cast(dict[str, Any], handler(arguments))


class MCPClient(LocalClient):
    def __init__(self, *, root: str | Path | None = None, transport: MCPToolTransport | None = None) -> None:
        self._transport = transport or _LoopbackTransport()
        super().__init__(root=root)

    @property
    def transport(self) -> MCPToolTransport:
        return self._transport

    def get_context(
        self,
        *,
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
    ) -> ContextResult:
        payload = self.transport.call_tool(
            "context",
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
        return ContextResult.model_validate(payload)

    def memory_recall(
        self,
        *,
        agent_id: str,
        query: str,
        top_k: int = 5,
        tags: list[str] | None = None,
        since: str | None = None,
    ) -> MemoryRecallResult:
        payload = self._transport.call_tool(
            "memory",
            {
                "op": "recall",
                "agent_id": agent_id,
                "query": query,
                "top_k": top_k,
                "tags": tags or [],
                "since": since,
            },
        )
        return MemoryRecallResult.model_validate(payload)

    def compact(self, *, op: str, **kwargs: Any) -> dict[str, Any]:
        return self._transport.call_tool("compact", {"op": op, **kwargs})

    def smart_search(self, *, query: str, **kwargs: Any) -> dict[str, Any]:
        return self._transport.call_tool("search", {"query": query, **kwargs})

    def smart_edit(self, *, edits: list[dict[str, Any]], atomic: bool = True) -> dict[str, Any]:
        return self._transport.call_tool("edit", {"edits": edits, "atomic": atomic})

    def repo_map(self, *, seed_files: list[str], **kwargs: Any) -> dict[str, Any]:
        return self._transport.call_tool("search", {"seed_files": seed_files, "mode": "map", **kwargs})

    def rescue_failure(
        self,
        *,
        task: str,
        error: str,
        domain: str | None = None,
        files: list[str] | None = None,
        recent_actions: list[str] | None = None,
    ) -> RescueResult:
        payload = self._transport.call_tool(
            "rescue",
            {
                "task": task,
                "error": error,
                "domain": domain,
                "files": files or [],
                "recent_actions": recent_actions or [],
            },
        )
        payload = {
            "rescue": str(payload.get("rescue") or ""),
            "matched_blocks": list(payload.get("matched_blocks") or []),
        }
        return RescueResult.model_validate(payload)

    def run_rubric_gate(self, *, rubric_id: str, checks: dict[str, bool | None]) -> RubricResult:
        payload = self._transport.call_tool(
            "verify",
            {"rubric_id": rubric_id, "checks": checks},
        )
        return RubricResult.model_validate(payload)

    def record_trace(
        self,
        *,
        agent: str,
        domain: str,
        task: str,
        status: TraceStatus,
        files_touched: list[str | dict[str, Any]] | None = None,
        commands_run: list[str | dict[str, Any]] | None = None,
        tools_called: list[dict[str, Any]] | None = None,
        errors_seen: list[str] | None = None,
        diff_summary: str = "",
        output_summary: str = "",
        validation_results: list[ValidationResult] | None = None,
        learnings: list[str | dict[str, Any] | TraceLearning] | None = None,
    ) -> TraceRecordResult:
        serialized_learnings = serialize_trace_learnings(learnings)
        payload = self._transport.call_tool(
            "trace",
            {
                "agent": agent,
                "domain": domain,
                "task": task,
                "status": status,
                "files_touched": files_touched or [],
                "commands_run": commands_run or [],
                "tools_called": tools_called or [],
                "errors_seen": errors_seen or [],
                "diff_summary": diff_summary,
                "output_summary": output_summary,
                "validation_results": serialize_validation_results(validation_results),
                "learnings": serialized_learnings,
            },
        )
        payload = {"id": str(payload.get("id") or payload.get("trace_id") or payload.get("session_id") or "")}
        return TraceRecordResult.model_validate(payload)
