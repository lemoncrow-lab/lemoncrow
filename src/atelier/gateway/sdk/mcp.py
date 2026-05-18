"""MCP-backed SDK client.

This client supports the MCP-standard task tools directly. For richer read
operations like listing ReasonBlocks it falls back to a local store at
``root`` so external hosts can embed Atelier without shelling out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from atelier.core.foundation.memory_models import MemoryBlock
from atelier.core.foundation.models import (
    RescueResult,
    RubricResult,
    TraceStatus,
    ValidationResult,
)
from atelier.gateway.adapters import mcp_server
from atelier.gateway.sdk.client import (
    ContextResult,
    MCPToolTransport,
    MemoryArchiveResult,
    MemoryRecallResult,
    MemoryUpsertBlockResult,
    TraceRecordResult,
)
from atelier.gateway.sdk.local import LocalClient


class _LoopbackTransport(MCPToolTransport):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tools = {
            "context": mcp_server.tool_get_context,
            "rescue": mcp_server.tool_rescue_failure,
            "trace": mcp_server.tool_record_trace,
            "record": mcp_server.tool_record_trace,
            "verify": mcp_server.tool_run_rubric_gate,
            "route": mcp_server.tool_route,
            "memory": mcp_server.tool_memory,
            "read": mcp_server.tool_smart_read,
            "search": mcp_server.tool_smart_search,
            "edit": mcp_server.tool_smart_edit,
            "compact": mcp_server.tool_compact,
            "code": mcp_server.tool_code,
        }
        return cast(dict[str, Any], tools[name](arguments))


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

    def memory_upsert_block(
        self,
        *,
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
    ) -> MemoryUpsertBlockResult:
        payload = self._transport.call_tool(
            "memory",
            {
                "op": "block_upsert",
                "agent_id": agent_id,
                "label": label,
                "value": value,
                "limit_chars": limit_chars,
                "description": description,
                "read_only": read_only,
                "pinned": pinned,
                "metadata": metadata or {},
                "expected_version": expected_version,
                "actor": actor,
            },
        )
        return MemoryUpsertBlockResult.model_validate(payload)

    def memory_get_block(self, *, agent_id: str, label: str) -> MemoryBlock | None:
        payload = self._transport.call_tool(
            "memory",
            {"op": "block_get", "agent_id": agent_id, "label": label},
        )
        return MemoryBlock.model_validate(payload) if payload is not None else None

    def memory_archive(
        self,
        *,
        agent_id: str,
        text: str,
        source: str,
        source_ref: str = "",
        tags: list[str] | None = None,
    ) -> MemoryArchiveResult:
        payload = self._transport.call_tool(
            "memory",
            {
                "op": "archive",
                "agent_id": agent_id,
                "text": text,
                "source": source,
                "source_ref": source_ref,
                "tags": tags or [],
            },
        )
        return MemoryArchiveResult.model_validate(payload)

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

    def memory_summary(self, *, session_id: str) -> dict[str, Any]:
        return self._transport.call_tool("memory", {"op": "summarize", "session_id": session_id})

    def route(self, *, op: str, **kwargs: Any) -> dict[str, Any]:
        return self._transport.call_tool("route", {"op": op, **kwargs})

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
    ) -> TraceRecordResult:
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
                "validation_results": [result.model_dump(mode="json") for result in (validation_results or [])],
            },
        )
        payload = {"id": str(payload.get("id") or payload.get("session_id") or "")}
        return TraceRecordResult.model_validate(payload)
