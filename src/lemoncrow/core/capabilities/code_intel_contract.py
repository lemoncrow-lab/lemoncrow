"""Public data and exception contracts for code-intelligence engines.

Held open because mypyc cannot compile pydantic models or builtin-exception
subclasses. Pro modules re-export these names so existing imports remain stable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

CrossLangEdgeKind = Literal["ffi_ctypes", "ffi_cffi", "subprocess", "dynamic_import"]


class CrossLangEdge(BaseModel):
    """Persisted literal-only static cross-language edge."""

    model_config = ConfigDict(extra="forbid")

    repo_id: str
    src_symbol_id: str
    src_symbol_name: str
    src_qualified_name: str
    src_language: str
    src_file_path: str
    src_line: int
    tgt_symbol_name: str
    tgt_symbol_id: str | None = None
    tgt_language: str
    tgt_file_path: str | None = None
    edge_kind: CrossLangEdgeKind
    confidence: float


class GitHistoryBootstrapError(RuntimeError):
    """The git-history substrate could not load its required backend."""


class SummarizerError(Exception):
    """The commit summarizer failed or returned an unusable response."""


__all__ = [
    "CrossLangEdge",
    "CrossLangEdgeKind",
    "GitHistoryBootstrapError",
    "SummarizerError",
]
