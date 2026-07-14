"""Public contract types for context compression (sleeptime summaries).

``SleeptimeChunk`` (pydantic) and ``SleeptimeUnavailable`` (builtin-exception
subclass) are caller-facing contract, not IP. They live here (open) because
neither can be mypyc-compiled, so the pro sleeptime logic compiles to native
``.so`` while callers import the same names.
"""

from __future__ import annotations

from pydantic import BaseModel


class SleeptimeChunk(BaseModel):
    """A paraphrase of a consecutive group of evicted ledger events."""

    start_event_index: int
    end_event_index: int
    paraphrase: str


class SleeptimeUnavailable(RuntimeError):
    """Raised when neither the internal LLM nor Letta can summarize evicted events."""
