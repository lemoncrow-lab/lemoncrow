"""Per-tool exact input/output token ledger.

Atelier estimates aggregate savings but historically had no per-tool exact
in/out token ledger. This module records, per MCP tool invocation, the input
token count (request args) and the output token count (rendered response text),
accumulated per tool name and persisted as a JSON sidecar alongside the other
savings accounting.

Measurement primitive: the EXISTING local token counter in
``atelier.core.capabilities.prompt_compilation.tokens.estimate_tokens``
(cl100k_base via tiktoken, char/4 fallback). No network API is called — the
local tokenizer is the deterministic measurement.

Additive only: this never touches tool outputs. It is read back through the
savings summary surface (``load_tool_token_ledger``).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atelier.core.capabilities.prompt_compilation.tokens import estimate_tokens

_log = logging.getLogger(__name__)

# Sidecar file name under the atelier root. One JSON document keyed by tool name.
TOOL_TOKEN_LEDGER_FILENAME = "tool_token_ledger.json"

# Serializes the load->record->write of the shared sidecar. record_tool_tokens
# runs per tool-call from the MCP dispatcher's default 16-worker thread pool, so
# an unguarded read-modify-write loses concurrent updates (undercount). Mirrors
# the _STATE_LOCK pattern around _record_smart_state_savings in mcp_server.py.
# NOTE: this lock only protects threads within ONE process. Sharing a single
# atelier_root across multiple processes would additionally need an OS-level
# file lock (e.g. flock) around the same load->record->write critical section.
_LEDGER_LOCK = threading.Lock()


@dataclass
class ToolTokenCounts:
    """Accumulated exact token counts for a single tool name."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))

    def to_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolTokenCounts:
        return cls(
            calls=max(0, int(data.get("calls") or 0)),
            input_tokens=max(0, int(data.get("input_tokens") or 0)),
            output_tokens=max(0, int(data.get("output_tokens") or 0)),
        )


@dataclass
class ToolTokenLedger:
    """In-memory per-tool token ledger with JSON sidecar persistence."""

    per_tool: dict[str, ToolTokenCounts] = field(default_factory=dict)

    def record(self, tool_name: str, *, input_tokens: int, output_tokens: int) -> None:
        if not tool_name:
            return
        self.per_tool.setdefault(tool_name, ToolTokenCounts()).record(input_tokens, output_tokens)

    def total_input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.per_tool.values())

    def total_output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.per_tool.values())

    def total_calls(self) -> int:
        return sum(c.calls for c in self.per_tool.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_tool": {name: counts.to_dict() for name, counts in sorted(self.per_tool.items())},
            "totals": {
                "calls": self.total_calls(),
                "input_tokens": self.total_input_tokens(),
                "output_tokens": self.total_output_tokens(),
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolTokenLedger:
        per_tool_raw = data.get("per_tool")
        per_tool: dict[str, ToolTokenCounts] = {}
        if isinstance(per_tool_raw, dict):
            for name, raw in per_tool_raw.items():
                if isinstance(name, str) and isinstance(raw, dict):
                    per_tool[name] = ToolTokenCounts.from_dict(raw)
        return cls(per_tool=per_tool)


def _ledger_path(atelier_root: str | Path) -> Path:
    return Path(atelier_root) / TOOL_TOKEN_LEDGER_FILENAME


def count_payload_tokens(payload: Any) -> int:
    """Exact token count of a tool input/output payload via the local counter.

    Strings are measured as-is; anything else is rendered to compact JSON first
    (matching how the MCP server serialises non-string results before sending
    them to the model).
    """
    if payload is None:
        return 0
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(payload)
    return estimate_tokens(text)


def load_tool_token_ledger(atelier_root: str | Path) -> ToolTokenLedger:
    """Load the persisted per-tool token ledger (empty when absent/corrupt)."""
    path = _ledger_path(atelier_root)
    if not path.exists():
        return ToolTokenLedger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.exception("Recovered from broad exception handler")
        return ToolTokenLedger()
    if not isinstance(data, dict):
        return ToolTokenLedger()
    return ToolTokenLedger.from_dict(data)


def record_tool_tokens(
    atelier_root: str | Path,
    tool_name: str,
    *,
    input_payload: Any,
    output_payload: Any,
) -> ToolTokenLedger:
    """Record one tool invocation's exact in/out tokens and persist.

    Reads the existing sidecar, accumulates this call's counts, and writes the
    sidecar back. Returns the updated in-memory ledger. Best-effort: a failed
    read/write never raises (the caller is on the hot tool-dispatch path).
    """
    if not tool_name:
        return ToolTokenLedger()
    # Hold the lock across the whole load->record->write so two worker threads
    # cannot both read the same baseline and clobber each other's increment.
    with _LEDGER_LOCK:
        ledger = load_tool_token_ledger(atelier_root)
        ledger.record(
            tool_name,
            input_tokens=count_payload_tokens(input_payload),
            output_tokens=count_payload_tokens(output_payload),
        )
        path = _ledger_path(atelier_root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(ledger.to_dict(), ensure_ascii=False), encoding="utf-8")
        except OSError:
            logging.exception("Recovered from broad exception handler")
            _log.debug("tool token ledger write failed", exc_info=True)
    return ledger


__all__ = [
    "TOOL_TOKEN_LEDGER_FILENAME",
    "ToolTokenCounts",
    "ToolTokenLedger",
    "count_payload_tokens",
    "load_tool_token_ledger",
    "record_tool_tokens",
]
