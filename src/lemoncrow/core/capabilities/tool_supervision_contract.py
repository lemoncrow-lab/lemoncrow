"""Public contract types for tool supervision.

Exception types and result models are caller-facing contract, not engine IP;
defining them in this open module lets the pro tool-supervision logic compile to
native ``.so`` (mypyc cannot compile builtin-exception subclasses or pydantic
models) while callers keep importing the same names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.tool_supervision.fuzzy_match import FuzzyCandidate

CompactMethod = Literal["passthrough", "deterministic_truncate", "llm_summary"]


class SqlPathError(Exception):
    """Raised when a sqlite DSN resolves outside the repo sandbox."""


class FuzzyAmbiguousMatchError(ValueError):
    """Raised when fuzzy matching finds multiple acceptable candidate ranges."""

    def __init__(self, candidates: list[FuzzyCandidate]) -> None:
        self.candidates = candidates
        ranges = ", ".join(f"{c.start_line}-{c.end_line}" for c in candidates)
        super().__init__(f"fuzzy replace ambiguous candidates at ranges: {ranges}")


class SymbolEditError(ValueError):
    """Structured symbol-edit failure."""

    def __init__(self, error: str, message: str, **payload: Any) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.error, "message": self.message, **self.payload}


class CompactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compacted: str
    original_tokens: int
    compacted_tokens: int
    recovery_hint: str
    method: CompactMethod
    content_type: str


class GateResult(BaseModel):
    """Outcome of the N6 savings gate.

    ``chosen`` is the text that should actually be emitted. ``used_compact`` is
    True only when the compact form cleared the savings threshold.
    """

    model_config = ConfigDict(extra="forbid")

    chosen: str
    used_compact: bool
    original_chars: int
    compact_chars: int
    savings_ratio: float
    threshold: float
