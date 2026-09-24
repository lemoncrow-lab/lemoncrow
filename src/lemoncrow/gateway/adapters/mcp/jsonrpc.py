"""Small JSON-RPC 2.0 envelope helpers shared by MCP transports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def ok(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success envelope."""
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error envelope."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


__all__ = ["error", "ok"]
