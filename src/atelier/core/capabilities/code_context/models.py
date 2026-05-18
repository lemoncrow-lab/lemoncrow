"""Typed models for the Atelier code context engine."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class SymbolRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_id: str
    repo_id: str
    file_path: str
    language: str
    symbol_name: str
    qualified_name: str
    kind: str
    signature: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    parent_symbol: str | None = None
    doc_summary: str | None = None
    snippet: str | None = None
    content_hash: str
    score: float | None = None
    provenance: str = "local"


class IndexStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    repo_root: str
    db_path: str
    files_indexed: int
    symbols_indexed: int
    imports_indexed: int
    index_version: int = 0


class TextMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    line: int
    column: int
    text: str


class ContextPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    budget_tokens: int
    token_count: int
    tokens_saved_vs_full_files: int
    symbols: list[SymbolRecord]
    repo_map: str
    import_neighbors: list[str]
    content: str
    telemetry: dict[str, Any]
    cache_hit: bool = False
    tokens_saved: int = 0
    provenance: str = "local"


class ImpactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    direct_importers: list[str]
    transitive_importers: list[str]
    affected_tests: list[str]
    risk_level: Literal["low", "medium", "high", "critical"]
    dead_code_candidates: list[str]
    cache_hit: bool = False
    tokens_saved: int = 0
    provenance: str = "local"


__all__ = [
    "ContextPack",
    "ImpactResult",
    "IndexStats",
    "SymbolRecord",
    "TextMatch",
]
