"""Public contract types for tool supervision.

Exception types are caller-facing contract, not engine IP; defining them in this
open module lets the pro tool-supervision logic compile to native ``.so`` (mypyc
cannot compile classes inheriting builtin exception types) while callers keep
importing the same names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.tool_supervision.fuzzy_match import FuzzyCandidate


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
