"""Public contract models for semantic file memory.

``SymbolOutline`` / ``FileOutline`` are the caller-facing outline shapes (data
contract, not IP). They live here (open) because pydantic models cannot be
mypyc-compiled, so the pro ``semantic_file_memory`` logic compiles to native
``.so`` while callers import the same types. The sibling dataclasses stay in the
compiled module (dataclasses compile fine).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SymbolOutline(BaseModel):
    """Outline symbol for class/function/method boundaries."""

    name: str
    kind: Literal["class", "function", "method"]
    start_line: int
    end_line: int


class FileOutline(BaseModel):
    """Compact outline returned for large source files."""

    path: str
    lang: Literal["python", "typescript", "tsx", "javascript"]
    loc: int
    symbols: list[SymbolOutline]
    imports: list[str]
    hint: str = "Pass range=L1-L2 or full=true for untransformed body text"
