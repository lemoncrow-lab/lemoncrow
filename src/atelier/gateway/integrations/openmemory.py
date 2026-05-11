"""OpenMemory bridge with local persistence and optional MCP sync.

Atelier remains the source of truth for reasoning procedures; this bridge only
stores and retrieves memory pointers/context references.

The bridge always works locally via a JSON store under ``ATELIER_ROOT``.
When ``ATELIER_OPENMEMORY_ENABLED=true``, it additionally attempts best-effort
sync calls to an OpenMemory MCP server.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Lazy import: MCP client only loaded if enabled
_mcp_client: Any = None


# --------------------------------------------------------------------------- #
# Config helpers                                                              #
# --------------------------------------------------------------------------- #


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").lower()
    if not val:
        return default
    return val in ("1", "true", "yes")


def is_enabled() -> bool:
    """Return True when ATELIER_OPENMEMORY_ENABLED=true."""
    return _bool_env("ATELIER_OPENMEMORY_ENABLED", False)


def _server_name() -> str:
    return os.environ.get("ATELIER_OPENMEMORY_MCP_SERVER_NAME", "openmemory")


def _timeout() -> float:
    return float(os.environ.get("ATELIER_OPENMEMORY_TIMEOUT", "10.0"))


def _bridge_store_path() -> Path:
    from atelier.core.foundation.paths import default_store_root

    root = Path(os.environ.get("ATELIER_ROOT", str(default_store_root())))
    return root / "openmemory_bridge.json"


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _empty_store() -> dict[str, Any]:
    return {
        "contexts": {},
        "trace_to_context": {},
        "trace_to_memory": {},
        "events": [],
    }


def _load_store() -> dict[str, Any]:
    path = _bridge_store_path()
    if not path.exists():
        return _empty_store()
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_store()
        merged = _empty_store()
        merged.update(data)
        return merged
    except Exception:
        return _empty_store()


def _save_store(data: dict[str, Any]) -> None:
    path = _bridge_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _append_event(store: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
    events = store.setdefault("events", [])
    if not isinstance(events, list):
        store["events"] = []
        events = store["events"]
    events.append({"ts": _utcnow_iso(), "action": action, **payload})
    # Keep file bounded
    if len(events) > 2000:
        del events[:-1000]


# --------------------------------------------------------------------------- #
# MCP Client Management                                                       #
# --------------------------------------------------------------------------- #


def _get_mcp_client() -> Any:
    """Get or create MCP client lazily.

    Returns None if MCP is not available or if the server is unreachable.
    """
    global _mcp_client

    if not is_enabled():
        return None

    if _mcp_client is not None:
        return _mcp_client

    # Try to import and initialize MCP client
    try:
        from atelier.gateway.sdk import MCPClient

        server_name = _server_name()
        from atelier.core.foundation.paths import default_store_root

        root = os.environ.get("ATELIER_ROOT", str(default_store_root()))
        _timeout()

        _mcp_client = MCPClient(root=root)

        logger.debug(f"OpenMemory MCP client initialized for server '{server_name}'")
        return _mcp_client

    except ImportError:
        logger.warning("MCP module not available. OpenMemory integration disabled.")
        return None
    except Exception as e:
        logger.warning(f"Failed to initialize OpenMemory MCP client: {e}")
        return None


# --------------------------------------------------------------------------- #
# Internal response builders                                                  #
# --------------------------------------------------------------------------- #


def _disabled(action: str) -> dict[str, object]:
    return {
        "ok": False,
        "skipped": True,
        "action": action,
        "reason": ("OpenMemory integration is disabled. Set ATELIER_OPENMEMORY_ENABLED=true to enable."),
    }


def _unavailable(action: str, detail: str = "") -> dict[str, object]:
    server = _server_name()
    return {
        "ok": False,
        "skipped": True,
        "action": action,
        "reason": f"OpenMemory MCP server '{server}' is unavailable.",
        "detail": detail or f"Could not reach MCP server '{server}'.",
        "hint": (
            "Ensure the OpenMemory MCP server is running and its name matches " "ATELIER_OPENMEMORY_MCP_SERVER_NAME."
        ),
    }


def _success(action: str, data: dict[str, Any] | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "action": action,
        "data": data or {},
    }


def _try_remote_call(candidates: list[str], args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Try candidate remote tool names; return (ok, payload_or_error)."""
    client = _get_mcp_client()
    if client is None:
        return False, {"reason": "remote-disabled-or-unavailable"}

    transport = getattr(client, "_transport", None)
    for name in candidates:
        try:
            if transport is not None and hasattr(transport, "call_tool"):
                payload = transport.call_tool(name, args)
                if isinstance(payload, dict):
                    return True, payload
            if hasattr(client, "call_tool"):
                payload = client.call_tool(name, args)
                if isinstance(payload, dict):
                    return True, payload
        except Exception as exc:
            logger.debug("OpenMemory remote call failed for %s: %s", name, exc)
            continue
    return False, {"reason": "no-compatible-remote-tool"}


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def list_available_memory_tools() -> list[str]:
    """List available OpenMemory tools from MCP server.

    Returns an empty list when disabled or when the server is unreachable.
    No exceptions are raised; callers should treat the empty list as
    "no memory context available".
    """
    local = [
        "link_trace_to_memory_context",
        "fetch_memory_context_for_task",
        "store_memory_pointer",
        "link_trace_with_memory_context",
    ]
    if not is_enabled():
        return local

    remote = [
        "link_trace_context",
        "fetch_context",
        "store_pointer",
        "link_and_store",
    ]
    return sorted(set(local + remote))


def maybe_link_trace_to_memory_context(
    trace_id: str,
    context_id: str | None = None,
) -> dict[str, object]:
    """Optionally link an Atelier trace-id to an OpenMemory context pointer.

    Only a (trace_id, context_id) association is stored — never the trace
    content itself.  Atelier remains the source of truth for traces.

    Returns a structured response dict; never raises.
    """
    resolved_context = context_id or f"ctx-{trace_id[:12]}"
    store = _load_store()
    trace_map = store.setdefault("trace_to_context", {})
    if isinstance(trace_map, dict):
        trace_map[trace_id] = resolved_context
    contexts = store.setdefault("contexts", {})
    if isinstance(contexts, dict):
        contexts.setdefault(resolved_context, {"created_at": _utcnow_iso(), "notes": []})
    _append_event(
        store,
        "link_trace_to_memory_context",
        {"trace_id": trace_id, "context_id": resolved_context},
    )
    _save_store(store)

    remote_ok, remote_data = _try_remote_call(
        ["link_trace_context", "memory_link_trace_context", "openmemory_link_trace_context"],
        {"trace_id": trace_id, "context_id": resolved_context},
    )
    payload = {
        "trace_id": trace_id,
        "context_id": resolved_context,
        "mode": "local+remote" if remote_ok else "local",
        "remote": remote_data if remote_ok else None,
    }
    logger.info("Linked trace %s to context %s", trace_id, resolved_context)
    return _success("link_trace_to_memory_context", payload)


def maybe_fetch_memory_context_for_task(
    task: str,
    project_id: str | None = None,
) -> dict[str, object]:
    """Optionally fetch user/project memory context from OpenMemory.

    Atelier does NOT depend on the result for plan checking or rubric
    evaluation.  Any context returned here is supplementary to the
    ReasonBlocks already retrieved by the retriever.

    Returns a structured response dict; never raises.
    """
    store = _load_store()
    trace_to_context = store.get("trace_to_context", {})
    trace_to_memory = store.get("trace_to_memory", {})
    contexts = store.get("contexts", {})

    tokens = [tok for tok in task.lower().split() if tok]
    local_hits: list[dict[str, Any]] = []
    if isinstance(trace_to_context, dict):
        for trace_id, ctx_id in trace_to_context.items():
            hay = f"{trace_id} {ctx_id}".lower()
            if not tokens or any(tok in hay for tok in tokens):
                local_hits.append(
                    {
                        "trace_id": trace_id,
                        "context_id": ctx_id,
                        "memory_id": (trace_to_memory.get(trace_id) if isinstance(trace_to_memory, dict) else None),
                        "context": contexts.get(ctx_id, {}) if isinstance(contexts, dict) else {},
                    }
                )

    _append_event(
        store,
        "fetch_memory_context_for_task",
        {"task": task, "project_id": project_id or "", "local_hits": len(local_hits)},
    )
    _save_store(store)

    remote_ok, remote_data = _try_remote_call(
        ["fetch_context", "memory_fetch_context", "openmemory_fetch_context"],
        {"task": task, "project_id": project_id or "", "limit": 10},
    )

    payload = {
        "task": task,
        "project_id": project_id,
        "matches": local_hits[:10],
        "count": len(local_hits),
        "mode": "local+remote" if remote_ok else "local",
        "remote": remote_data if remote_ok else None,
    }
    logger.debug("Fetched memory context for task '%s' (local_hits=%d)", task, len(local_hits))
    return _success("fetch_memory_context_for_task", payload)


def maybe_store_memory_pointer(trace_id: str, memory_id: str) -> dict[str, object]:
    """Optionally record an OpenMemory pointer for a completed trace.

    Only the (trace_id, memory_id) pairing is stored, never trace content.

    Returns a structured response dict; never raises.
    """
    store = _load_store()
    trace_to_memory = store.setdefault("trace_to_memory", {})
    if isinstance(trace_to_memory, dict):
        trace_to_memory[trace_id] = memory_id
    _append_event(
        store,
        "store_memory_pointer",
        {"trace_id": trace_id, "memory_id": memory_id},
    )
    _save_store(store)

    remote_ok, remote_data = _try_remote_call(
        ["store_pointer", "memory_store_pointer", "openmemory_store_pointer"],
        {"trace_id": trace_id, "memory_id": memory_id},
    )
    payload = {
        "trace_id": trace_id,
        "memory_id": memory_id,
        "mode": "local+remote" if remote_ok else "local",
        "remote": remote_data if remote_ok else None,
    }
    logger.info("Stored memory pointer: trace %s -> memory %s", trace_id, memory_id)
    return _success("store_memory_pointer", payload)


# --------------------------------------------------------------------------- #
# Convenience batch functions                                                #
# --------------------------------------------------------------------------- #


def link_trace_with_memory_context(
    trace_id: str,
    memory_id: str,
    context_data: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Link a trace and store it with context in OpenMemory (atomic operation).

    Combines linking and storing in a single call.
    Returns structured response; never raises.
    """
    link_result = maybe_link_trace_to_memory_context(trace_id, context_id=memory_id)
    pointer_result = maybe_store_memory_pointer(trace_id, memory_id)

    store = _load_store()
    contexts = store.setdefault("contexts", {})
    if isinstance(contexts, dict):
        entry = contexts.setdefault(memory_id, {"created_at": _utcnow_iso(), "notes": []})
        if isinstance(entry, dict):
            entry["context"] = context_data or {}
            entry["updated_at"] = _utcnow_iso()
    _append_event(
        store,
        "link_trace_with_memory_context",
        {"trace_id": trace_id, "memory_id": memory_id},
    )
    _save_store(store)

    remote_ok, remote_data = _try_remote_call(
        ["link_and_store", "memory_link_and_store", "openmemory_link_and_store"],
        {
            "trace_id": trace_id,
            "memory_id": memory_id,
            "context": context_data or {},
        },
    )
    payload = {
        "link": link_result,
        "pointer": pointer_result,
        "mode": "local+remote" if remote_ok else "local",
        "remote": remote_data if remote_ok else None,
    }
    return _success("link_trace_with_memory_context", payload)


__all__ = [
    "is_enabled",
    "link_trace_with_memory_context",
    "list_available_memory_tools",
    "maybe_fetch_memory_context_for_task",
    "maybe_link_trace_to_memory_context",
    "maybe_store_memory_pointer",
]
