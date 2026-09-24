"""Transport-independent tool runtime error types and classification."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from lemoncrow.infra.storage.memory_store import MemoryConcurrencyError, MemorySidecarUnavailable


class ToolArgumentError(ValueError):
    """Malformed tool arguments before or during handler shape validation."""


class ToolProtocolError(RuntimeError):
    """A tool-call failure that belongs to the request/protocol boundary."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ToolFailureDisposition:
    """Transport-neutral classification of one tool-call exception."""

    message: str
    protocol_code: int | None = None

    @property
    def is_execution_error(self) -> bool:
        return self.protocol_code is None


def classify_tool_exception(exc: Exception) -> ToolFailureDisposition:
    """Classify an exception without choosing JSON-RPC, HTTP, or server framing."""
    message = str(exc) or type(exc).__name__
    if isinstance(exc, (ToolArgumentError, ValidationError)):
        return ToolFailureDisposition(message=message, protocol_code=-32602)
    if isinstance(exc, MemoryConcurrencyError):
        return ToolFailureDisposition(message=message, protocol_code=409)
    if isinstance(exc, MemorySidecarUnavailable):
        return ToolFailureDisposition(message=message, protocol_code=503)
    return ToolFailureDisposition(message=message)


def execution_error_payload(exc: Exception) -> dict[str, object]:
    """Return the host-facing content payload for an ordinary execution failure."""
    disposition = classify_tool_exception(exc)
    if not disposition.is_execution_error:
        raise ValueError("protocol failure does not have an execution-error payload")
    return {
        "content": [{"type": "text", "text": disposition.message}],
        "isError": True,
    }


__all__ = [
    "ToolArgumentError",
    "ToolFailureDisposition",
    "ToolProtocolError",
    "classify_tool_exception",
    "execution_error_payload",
]
