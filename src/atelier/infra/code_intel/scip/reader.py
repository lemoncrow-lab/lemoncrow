"""Reads fixture-friendly SCIP artifact payloads from trusted roots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from atelier.core.capabilities.code_context.models import SymbolRecord

_MAX_SCIP_ARTIFACT_BYTES = 10 * 1024 * 1024


class ScipArtifactError(ValueError):
    """Raised when a `.scip` artifact is malformed or untrusted."""


@dataclass(frozen=True)
class LoadedScipArtifact:
    """Parsed SCIP artifact indexes for fast routed symbol lookups."""

    path: Path
    symbols: tuple[SymbolRecord, ...]
    symbol_payloads: dict[str, dict[str, Any]]

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
    ) -> list[SymbolRecord]:
        query_lower = query.lower()
        ranked: list[tuple[int, str, SymbolRecord]] = []
        for symbol in self.symbols:
            if kind and symbol.kind != kind:
                continue
            if language and symbol.language != language:
                continue
            haystacks = (symbol.symbol_name.lower(), symbol.qualified_name.lower())
            if all(query_lower not in hay for hay in haystacks):
                continue
            rank = 0 if symbol.symbol_name.lower() == query_lower else 1 if symbol.qualified_name.lower() == query_lower else 2
            ranked.append((rank, symbol.file_path, symbol))
        ranked.sort(key=lambda item: (item[0], item[1], item[2].start_line))
        return [item[2] for item in ranked[:limit]]

    def get_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> dict[str, Any] | None:
        for symbol in self.symbols:
            if symbol_id and symbol.symbol_id != symbol_id:
                continue
            if qualified_name and symbol.qualified_name != qualified_name:
                continue
            if symbol_name and symbol.symbol_name != symbol_name:
                continue
            if file_path and symbol.file_path != file_path:
                continue
            return dict(self.symbol_payloads[symbol.symbol_id])
        return None


class ScipArtifactReader:
    """Parses trusted repo-local `.scip` artifacts into routed symbol indexes."""

    def __init__(self, *, repo_root: Path, allowed_roots: list[Path]) -> None:
        self.repo_root = repo_root.resolve()
        self.allowed_roots = [root.resolve() for root in allowed_roots]

    def load(self, artifact_path: Path) -> LoadedScipArtifact:
        path = artifact_path.resolve()
        self._validate_path(path)
        try:
            stat = path.stat()
        except OSError as exc:  # pragma: no cover - filesystem race
            raise ScipArtifactError(f"unable to stat SCIP artifact: {path}") from exc
        if stat.st_size > _MAX_SCIP_ARTIFACT_BYTES:
            raise ScipArtifactError(f"SCIP artifact too large: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScipArtifactError(f"invalid SCIP artifact: {path}") from exc
        if not isinstance(payload, dict):
            raise ScipArtifactError(f"unexpected SCIP payload shape: {path}")
        symbols_payload = payload.get("symbols")
        if not isinstance(symbols_payload, list):
            raise ScipArtifactError(f"missing symbols in SCIP artifact: {path}")
        symbols: list[SymbolRecord] = []
        symbol_payloads: dict[str, dict[str, Any]] = {}
        for raw in symbols_payload:
            if not isinstance(raw, dict):
                raise ScipArtifactError(f"malformed symbol entry in SCIP artifact: {path}")
            raw_payload = dict(raw)
            source = str(raw_payload.pop("source", "") or "")
            raw_payload.setdefault("provenance", "scip")
            raw_payload.setdefault("repo_id", str(payload.get("repo_id") or ""))
            raw_payload.setdefault("content_hash", "")
            try:
                symbol = SymbolRecord.model_validate(raw_payload)
            except ValidationError as exc:
                raise ScipArtifactError(f"invalid symbol entry in SCIP artifact: {path}") from exc
            if not symbol.repo_id:
                raise ScipArtifactError(f"missing repo_id for symbol in SCIP artifact: {path}")
            symbols.append(symbol)
            symbol_payloads[symbol.symbol_id] = {
                **symbol.model_dump(mode="json"),
                "source": source or self._source_from_repo(symbol),
            }
        return LoadedScipArtifact(path=path, symbols=tuple(symbols), symbol_payloads=symbol_payloads)

    def _validate_path(self, artifact_path: Path) -> None:
        for root in self.allowed_roots:
            try:
                artifact_path.relative_to(root)
                return
            except ValueError:
                continue
        raise ScipArtifactError(f"untrusted SCIP artifact path: {artifact_path}")

    def _source_from_repo(self, symbol: SymbolRecord) -> str:
        path = (self.repo_root / symbol.file_path).resolve()
        try:
            path.relative_to(self.repo_root)
        except ValueError as exc:
            raise ScipArtifactError(f"path escape denied for symbol source: {symbol.file_path}") from exc
        data = path.read_bytes()
        return data[symbol.start_byte : symbol.end_byte].decode("utf-8", errors="replace")


__all__ = ["LoadedScipArtifact", "ScipArtifactError", "ScipArtifactReader"]
