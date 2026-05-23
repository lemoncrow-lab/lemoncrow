"""Typed models for the Atelier code context engine."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CrossLangReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_id: str | None = None
    symbol_name: str
    qualified_name: str | None = None
    language: str
    file_path: str | None = None
    line: int | None = None
    direction: Literal["incoming", "outgoing"]
    provenance: str = "cross_lang"
    edge_kind: str
    confidence: float


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
    documentation: list[str] | None = None  # raw SCIP SymbolInformation.documentation strings
    snippet: str | None = None
    content_hash: str
    score: float | None = None
    provenance: str = "local"
    origin: Literal["internal", "external"] = "internal"
    repo_name: str | None = None
    cross_lang_refs: list[CrossLangReference] | None = None


class IndexStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    repo_root: str
    db_path: str
    files_indexed: int
    symbols_indexed: int
    imports_indexed: int
    index_version: int = 0


class IndexedFileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    language: str
    symbol_count: int = 0
    top_symbols: list[str] | None = None


class RouteRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framework: str
    method: str
    route: str
    file_path: str
    line: int
    language: str
    handler: str | None = None
    router: str | None = None
    provenance: str = "local"


class TextMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    line: int
    column: int
    text: str


class UsageReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    line: int
    column: int
    end_line: int
    end_column: int
    snippet: str | None = None
    caller: str | None = None
    provenance: str = "local"
    edge_kind: str | None = None
    confidence: float | None = None
    repo_name: str | None = None


class ContextPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    budget_tokens: int
    token_count: int
    tokens_saved_vs_full_files: int
    symbols: list[SymbolRecord]
    entry_points: list[dict[str, Any]] = Field(default_factory=list)
    related_symbols: list[dict[str, Any]] = Field(default_factory=list)
    code_blocks: list[dict[str, Any]] = Field(default_factory=list)
    repo_map: str
    import_neighbors: list[str]
    content: str
    telemetry: dict[str, Any]
    cache_hit: bool = False
    tokens_saved: int = 0
    provenance: str = "local"


class ImpactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: dict[str, Any]
    target_type: Literal["file", "symbol"]
    file_path: str
    affected_files: list[dict[str, Any]]
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
    "CrossLangReference",
    "ImpactResult",
    "IndexStats",
    "IndexedFileRecord",
    "RouteRecord",
    "SymbolRecord",
    "TextMatch",
    "UsageReference",
]
