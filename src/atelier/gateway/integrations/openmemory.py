"""OpenMemory MCP-over-HTTP integration helpers.

This module talks to a locally running OpenMemory server instead of persisting
bridge state in Atelier's SQLite database. The bridge helpers remain
best-effort and never raise; the OpenMemory-backed MemoryStore uses the lower
level client directly and converts transport failures into
``MemorySidecarUnavailable``.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from getpass import getuser
from typing import Any

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 4 * 1024 * 1024
_DEFAULT_TIMEOUT = 15
_DEFAULT_PROTOCOL_VERSION = "2025-03-26"
_DEFAULT_TOOLS = [
    "add_memories",
    "search_memory",
    "list_memories",
    "delete_memories",
    "delete_all_memories",
]

_CLIENT: OpenMemoryClient | None = None


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_user_id() -> str:
    configured = os.environ.get("ATELIER_OPENMEMORY_USER_ID", "").strip()
    if configured:
        return configured
    with_user = os.environ.get("USER", "").strip()
    if with_user:
        return with_user
    try:
        return getuser()
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return "atelier"


class OpenMemoryMCPError(RuntimeError):
    """Raised when the OpenMemory MCP sidecar cannot serve a request."""


class OpenMemoryClient:
    """Small stdlib-only MCP HTTP client for the OpenMemory sidecar."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client_name: str | None = None,
        user_id: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("ATELIER_OPENMEMORY_URL", "http://127.0.0.1:8765")).rstrip("/")
        self.client_name = (
            client_name or os.environ.get("ATELIER_OPENMEMORY_CLIENT_NAME", "atelier")
        ).strip() or "atelier"
        self.user_id = (user_id or _default_user_id()).strip() or "atelier"
        self.timeout = max(
            1,
            int(timeout or os.environ.get("ATELIER_OPENMEMORY_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT),
        )
        self.protocol_version = (
            os.environ.get(
                "ATELIER_OPENMEMORY_MCP_PROTOCOL_VERSION",
                _DEFAULT_PROTOCOL_VERSION,
            ).strip()
            or _DEFAULT_PROTOCOL_VERSION
        )
        self._initialized_endpoints: set[str] = set()
        self._request_id = 0

    def list_tools(self) -> list[str]:
        payload = self._rpc("tools/list", {})
        if not isinstance(payload, dict):
            return list(_DEFAULT_TOOLS)
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return list(_DEFAULT_TOOLS)
        names = [str(item.get("name", "")) for item in tools if isinstance(item, dict) and item.get("name")]
        return names or list(_DEFAULT_TOOLS)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if isinstance(payload, dict) and "structuredContent" in payload:
            return payload["structuredContent"]
        return _decode_content_payload(payload)

    def _endpoint_candidates(self) -> list[str]:
        configured = os.environ.get("ATELIER_OPENMEMORY_HTTP_PATH", "").strip()
        if configured:
            return [urllib.parse.urljoin(f"{self.base_url}/", configured.lstrip("/"))]
        quoted_client = urllib.parse.quote(self.client_name, safe="")
        quoted_user = urllib.parse.quote(self.user_id, safe="")
        return [
            f"{self.base_url}/mcp/{quoted_client}/http/{quoted_user}",
            f"{self.base_url}/mcp/{quoted_client}/sse/{quoted_user}/messages/",
            f"{self.base_url}/mcp/{quoted_client}/sse/{quoted_user}/messages",
        ]

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for endpoint in self._endpoint_candidates():
            try:
                self._ensure_initialized(endpoint)
                response = self._post_json(
                    endpoint,
                    {
                        "jsonrpc": "2.0",
                        "id": self._next_id(),
                        "method": method,
                        "params": params,
                    },
                )
                if "error" in response:
                    error = response["error"]
                    raise OpenMemoryMCPError(str(error))
                return response.get("result", {})
            except Exception as exc:
                logging.exception("Recovered from broad exception handler")
                last_error = exc
                logger.debug("OpenMemory MCP request failed for %s via %s: %s", method, endpoint, exc)
        raise OpenMemoryMCPError(f"OpenMemory MCP request failed for {method}: {last_error}")

    def _ensure_initialized(self, endpoint: str) -> None:
        if endpoint in self._initialized_endpoints:
            return
        init_payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "clientInfo": {"name": self.client_name, "version": "1"},
                "capabilities": {},
            },
        }
        response = self._post_json(endpoint, init_payload)
        if "error" in response:
            raise OpenMemoryMCPError(str(response["error"]))
        self._post_json(
            endpoint,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        self._initialized_endpoints.add(endpoint)

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_BODY_BYTES)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(_MAX_BODY_BYTES).decode("utf-8", errors="replace")
            except Exception:
                logging.exception("Recovered from broad exception handler")
                body = ""
            raise OpenMemoryMCPError(f"HTTP {exc.code} from OpenMemory MCP: {body or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise OpenMemoryMCPError(f"OpenMemory MCP unreachable at {endpoint}: {exc.reason}") from exc
        except Exception as exc:
            raise OpenMemoryMCPError(f"OpenMemory MCP request failed: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise OpenMemoryMCPError(f"OpenMemory MCP returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise OpenMemoryMCPError("OpenMemory MCP returned a non-object JSON payload")
        return parsed

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id


def get_client() -> OpenMemoryClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OpenMemoryClient()
    return _CLIENT


def _decode_content_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        if "content" not in payload:
            return payload
        content = payload.get("content")
        if isinstance(content, list):
            if len(content) == 1 and isinstance(content[0], dict):
                text = content[0].get("text")
                if isinstance(text, str):
                    return _maybe_json(text)
            return [_maybe_json(item.get("text", "")) if isinstance(item, dict) else item for item in content]
    return payload


def _maybe_json(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return json.loads(stripped)
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return value


def _success(action: str, data: dict[str, Any] | None = None) -> dict[str, object]:
    return {"ok": True, "action": action, "data": data or {}}


def _unavailable(action: str, detail: str = "") -> dict[str, object]:
    return {
        "ok": False,
        "skipped": True,
        "action": action,
        "reason": "OpenMemory MCP server is unavailable.",
        "detail": detail or "Could not reach OpenMemory MCP server.",
        "data": {},
    }


def _memory_user_id(project_id: str | None = None) -> str:
    return (project_id or _default_user_id()).strip() or "atelier"


def add_memories(
    *,
    messages: list[dict[str, str]],
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    infer: bool = False,
) -> Any:
    args = {
        "messages": messages,
        "user_id": user_id or _default_user_id(),
        "metadata": metadata or {},
        "infer": infer,
    }
    return get_client().call_tool("add_memories", args)


def search_memory(
    *,
    query: str,
    user_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    payload = get_client().call_tool(
        "search_memory",
        {
            "query": query,
            "user_id": user_id or _default_user_id(),
            "limit": limit,
        },
    )
    return _coerce_memory_list(payload)


def list_memories(*, user_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    payload = get_client().call_tool(
        "list_memories",
        {
            "user_id": user_id or _default_user_id(),
            "limit": limit,
        },
    )
    return _coerce_memory_list(payload)


def delete_all_memories(*, user_id: str | None = None) -> Any:
    return get_client().call_tool(
        "delete_all_memories",
        {"user_id": user_id or _default_user_id()},
    )


def _coerce_memory_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "memories", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def list_available_memory_tools() -> list[str]:
    """List OpenMemory tools, falling back to the canonical tool set."""
    try:
        return get_client().list_tools()
    except OpenMemoryMCPError:
        return list(_DEFAULT_TOOLS)


def maybe_link_trace_to_memory_context(
    trace_id: str,
    context_id: str | None = None,
) -> dict[str, object]:
    """Best-effort trace-to-context linkage stored via OpenMemory itself."""
    resolved_context = context_id or f"ctx-{trace_id[:12]}"
    record = {
        "atelier_kind": "trace_context_link",
        "trace_id": trace_id,
        "context_id": resolved_context,
        "recorded_at": _utcnow_iso(),
    }
    try:
        stored = add_memories(
            messages=[{"role": "system", "content": _compact_json(record)}],
            user_id=_default_user_id(),
            metadata={
                "atelier_kind": "trace_context_link",
                "trace_id": trace_id,
                "context_id": resolved_context,
            },
            infer=False,
        )
    except OpenMemoryMCPError as exc:
        return _unavailable("link_trace_to_memory_context", str(exc))
    return _success(
        "link_trace_to_memory_context",
        {
            "trace_id": trace_id,
            "context_id": resolved_context,
            "remote": stored,
        },
    )


def maybe_fetch_memory_context_for_task(
    task: str,
    project_id: str | None = None,
) -> dict[str, object]:
    """Best-effort semantic memory lookup for the current task."""
    user_id = _memory_user_id(project_id)
    try:
        matches = search_memory(query=task, user_id=user_id, limit=10)
    except OpenMemoryMCPError as exc:
        return _unavailable("fetch_memory_context_for_task", str(exc))
    normalized = [_normalize_memory_hit(item) for item in matches]
    return _success(
        "fetch_memory_context_for_task",
        {
            "task": task,
            "project_id": project_id,
            "matches": normalized,
            "count": len(normalized),
        },
    )


def maybe_store_memory_pointer(trace_id: str, memory_id: str) -> dict[str, object]:
    """Best-effort pointer recording for a completed trace."""
    record = {
        "atelier_kind": "trace_memory_pointer",
        "trace_id": trace_id,
        "memory_id": memory_id,
        "recorded_at": _utcnow_iso(),
    }
    try:
        stored = add_memories(
            messages=[{"role": "system", "content": _compact_json(record)}],
            user_id=_default_user_id(),
            metadata={
                "atelier_kind": "trace_memory_pointer",
                "trace_id": trace_id,
                "memory_id": memory_id,
            },
            infer=False,
        )
    except OpenMemoryMCPError as exc:
        return _unavailable("store_memory_pointer", str(exc))
    return _success(
        "store_memory_pointer",
        {
            "trace_id": trace_id,
            "memory_id": memory_id,
            "remote": stored,
        },
    )


def link_trace_with_memory_context(
    trace_id: str,
    memory_id: str,
    context_data: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Store a single trace/context linkage record in OpenMemory."""
    record = {
        "atelier_kind": "trace_context_bundle",
        "trace_id": trace_id,
        "memory_id": memory_id,
        "context": context_data or {},
        "recorded_at": _utcnow_iso(),
    }
    try:
        stored = add_memories(
            messages=[{"role": "system", "content": _compact_json(record)}],
            user_id=_default_user_id(),
            metadata={
                "atelier_kind": "trace_context_bundle",
                "trace_id": trace_id,
                "memory_id": memory_id,
            },
            infer=False,
        )
    except OpenMemoryMCPError as exc:
        return _unavailable("link_trace_with_memory_context", str(exc))
    return _success(
        "link_trace_with_memory_context",
        {
            "trace_id": trace_id,
            "memory_id": memory_id,
            "context": context_data or {},
            "remote": stored,
        },
    )


def _normalize_memory_hit(item: dict[str, Any]) -> dict[str, Any]:
    memory_text = str(item.get("memory", item.get("text", item.get("content", ""))))
    parsed = _maybe_json(memory_text)
    metadata = item.get("metadata")
    return {
        "memory_id": str(item.get("id", item.get("memory_id", ""))),
        "memory": memory_text,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "score": item.get("score"),
        "parsed": parsed if isinstance(parsed, dict) else {},
    }


__all__ = [
    "OpenMemoryClient",
    "OpenMemoryMCPError",
    "add_memories",
    "delete_all_memories",
    "get_client",
    "link_trace_with_memory_context",
    "list_available_memory_tools",
    "list_memories",
    "maybe_fetch_memory_context_for_task",
    "maybe_link_trace_to_memory_context",
    "maybe_store_memory_pointer",
    "search_memory",
]
