from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InternalLLMChatResult:
    content: str
    parsed_json: dict[str, Any] | None = None
    model: str = ""
    request_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    cache_capability: str = "none"
    request_metadata: dict[str, Any] | None = None


__all__ = ["InternalLLMChatResult"]
