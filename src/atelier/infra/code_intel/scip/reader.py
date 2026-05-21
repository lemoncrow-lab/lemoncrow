"""Reads fixture-friendly SCIP artifact payloads from trusted roots."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from atelier.core.capabilities.code_context.call_graph import CallGraphNode
from atelier.core.capabilities.code_context.models import SymbolRecord, UsageReference
from atelier.infra.code_intel.scip.external_artifacts import ScipArtifactOrigin

_MAX_SCIP_ARTIFACT_BYTES = 10 * 1024 * 1024
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ScipArtifactError(ValueError):
    """Raised when a `.scip` artifact is malformed or untrusted."""


@dataclass(frozen=True)
class LoadedScipArtifact:
    """Parsed SCIP artifact indexes for fast routed symbol lookups."""

    path: Path
    origin: ScipArtifactOrigin
    index_sha: str
    symbols: tuple[SymbolRecord, ...]
    symbol_payloads: dict[str, dict[str, Any]]
    reference_payloads: dict[str, tuple[UsageReference, ...]]
    caller_payloads: dict[str, tuple[CallGraphNode, ...]]
    callee_payloads: dict[str, tuple[CallGraphNode, ...]]
    callers_available: bool = False
    callees_available: bool = False

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

    def find_references(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[UsageReference] | None:
        matched_symbol: SymbolRecord | None = None
        for symbol in self.symbols:
            if symbol_id and symbol.symbol_id != symbol_id:
                continue
            if qualified_name and symbol.qualified_name != qualified_name:
                continue
            if symbol_name and symbol.symbol_name != symbol_name:
                continue
            if file_path and symbol.file_path != file_path:
                continue
            matched_symbol = symbol
            break
        if matched_symbol is None:
            return None
        payload = self.reference_payloads.get(matched_symbol.symbol_id)
        if payload is None:
            return None
        return list(payload)

    def find_callers(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[CallGraphNode] | None:
        matched_symbol = self._match_symbol(
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            file_path=file_path,
            symbol_name=symbol_name,
        )
        if matched_symbol is None or not self.callers_available:
            return None
        return list(self.caller_payloads.get(matched_symbol.symbol_id, ()))

    def find_callees(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[CallGraphNode] | None:
        matched_symbol = self._match_symbol(
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            file_path=file_path,
            symbol_name=symbol_name,
        )
        if matched_symbol is None or not self.callees_available:
            return None
        return list(self.callee_payloads.get(matched_symbol.symbol_id, ()))

    def _match_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> SymbolRecord | None:
        matched_symbol: SymbolRecord | None = None
        for symbol in self.symbols:
            if symbol_id and symbol.symbol_id != symbol_id:
                continue
            if qualified_name and symbol.qualified_name != qualified_name:
                continue
            if symbol_name and symbol.symbol_name != symbol_name:
                continue
            if file_path and symbol.file_path != file_path:
                continue
            matched_symbol = symbol
            break
        return matched_symbol


class ScipArtifactReader:
    """Parses trusted repo-local `.scip` artifacts into routed symbol indexes."""

    def __init__(self, *, repo_root: Path, allowed_roots: list[Path]) -> None:
        self.repo_root = repo_root.resolve()
        self.allowed_roots = [root.resolve() for root in allowed_roots]

    def load(self, artifact_path: Path, *, origin: ScipArtifactOrigin = "internal") -> LoadedScipArtifact:
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
        index_sha = payload.get("index_sha")
        if not isinstance(index_sha, str) or not _GIT_SHA_RE.fullmatch(index_sha):
            raise ScipArtifactError(f"missing or invalid index_sha in SCIP artifact: {path}")
        symbols_payload = payload.get("symbols")
        if not isinstance(symbols_payload, list):
            raise ScipArtifactError(f"missing symbols in SCIP artifact: {path}")
        references_payload = payload.get("references", {})
        if not isinstance(references_payload, dict):
            raise ScipArtifactError(f"invalid references in SCIP artifact: {path}")
        call_graph_payload = payload.get("call_graph")
        if call_graph_payload is not None and not isinstance(call_graph_payload, dict):
            raise ScipArtifactError(f"invalid call_graph in SCIP artifact: {path}")
        symbols: list[SymbolRecord] = []
        symbol_payloads: dict[str, dict[str, Any]] = {}
        reference_payloads: dict[str, tuple[UsageReference, ...]] = {}
        caller_payloads: dict[str, tuple[CallGraphNode, ...]] = {}
        callee_payloads: dict[str, tuple[CallGraphNode, ...]] = {}
        for raw in symbols_payload:
            if not isinstance(raw, dict):
                raise ScipArtifactError(f"malformed symbol entry in SCIP artifact: {path}")
            raw_payload = dict(raw)
            source = str(raw_payload.pop("source", "") or "")
            raw_payload.setdefault("documentation", raw.get("documentation"))
            raw_payload.setdefault("provenance", "scip")
            raw_payload.setdefault("origin", origin)
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
                "index_sha": index_sha,
                "origin": symbol.origin,
                "source": source or self._source_from_repo(symbol),
            }
        for raw_symbol_id, raw_references in references_payload.items():
            if not isinstance(raw_symbol_id, str) or not isinstance(raw_references, list):
                raise ScipArtifactError(f"invalid reference entry in SCIP artifact: {path}")
            references: list[UsageReference] = []
            for raw_reference in raw_references:
                if not isinstance(raw_reference, dict):
                    raise ScipArtifactError(f"malformed reference entry in SCIP artifact: {path}")
                payload_reference = dict(raw_reference)
                payload_reference.setdefault("provenance", "scip")
                payload_reference.setdefault("end_line", payload_reference.get("line"))
                payload_reference.setdefault("end_column", payload_reference.get("column"))
                try:
                    references.append(UsageReference.model_validate(payload_reference))
                except ValidationError as exc:
                    raise ScipArtifactError(f"invalid reference entry in SCIP artifact: {path}") from exc
            reference_payloads[raw_symbol_id] = tuple(references)
        callers_available = False
        callees_available = False
        if call_graph_payload is not None:
            callers_raw = call_graph_payload.get("callers", {})
            callees_raw = call_graph_payload.get("callees", {})
            if not isinstance(callers_raw, dict) or not isinstance(callees_raw, dict):
                raise ScipArtifactError(f"invalid call_graph entries in SCIP artifact: {path}")
            caller_payloads = self._parse_call_graph_payload(
                callers_raw,
                symbol_payloads=symbol_payloads,
                artifact_path=path,
            )
            callee_payloads = self._parse_call_graph_payload(
                callees_raw,
                symbol_payloads=symbol_payloads,
                artifact_path=path,
            )
            callers_available = True
            callees_available = True
        return LoadedScipArtifact(
            path=path,
            origin=origin,
            index_sha=index_sha,
            symbols=tuple(symbols),
            symbol_payloads=symbol_payloads,
            reference_payloads=reference_payloads,
            caller_payloads=caller_payloads,
            callee_payloads=callee_payloads,
            callers_available=callers_available,
            callees_available=callees_available,
        )

    def _validate_path(self, artifact_path: Path) -> None:
        for root in self.allowed_roots:
            try:
                artifact_path.relative_to(root)
                return
            except ValueError:
                continue
        raise ScipArtifactError(f"untrusted SCIP artifact path: {artifact_path}")

    def _source_from_repo(self, symbol: SymbolRecord) -> str:
        path = self._validate_relative_repo_path(symbol.file_path, label="symbol source")
        data = path.read_bytes()
        return data[symbol.start_byte : symbol.end_byte].decode("utf-8", errors="replace")

    def _parse_call_graph_payload(
        self,
        raw_payload: dict[str, Any],
        *,
        symbol_payloads: dict[str, dict[str, Any]],
        artifact_path: Path,
    ) -> dict[str, tuple[CallGraphNode, ...]]:
        parsed: dict[str, tuple[CallGraphNode, ...]] = {}
        for raw_symbol_id, raw_neighbors in raw_payload.items():
            if not isinstance(raw_symbol_id, str) or not isinstance(raw_neighbors, list):
                raise ScipArtifactError(f"invalid call_graph entry in SCIP artifact: {artifact_path}")
            neighbors: list[CallGraphNode] = []
            for raw_neighbor in raw_neighbors:
                if not isinstance(raw_neighbor, dict):
                    raise ScipArtifactError(f"malformed call_graph entry in SCIP artifact: {artifact_path}")
                payload_neighbor = dict(raw_neighbor)
                payload_neighbor.setdefault("provenance", "scip")
                try:
                    neighbor = CallGraphNode.model_validate(payload_neighbor)
                except ValidationError as exc:
                    raise ScipArtifactError(f"invalid call_graph entry in SCIP artifact: {artifact_path}") from exc
                self._validate_relative_repo_path(neighbor.file_path, label="call graph")
                if neighbor.symbol_id not in symbol_payloads:
                    raise ScipArtifactError(f"unknown call_graph symbol id in SCIP artifact: {artifact_path}")
                neighbors.append(neighbor)
            parsed[raw_symbol_id] = tuple(neighbors)
        return parsed

    def _validate_relative_repo_path(self, raw_path: str, *, label: str) -> Path:
        path = (self.repo_root / raw_path).resolve()
        try:
            path.relative_to(self.repo_root)
        except ValueError as exc:
            raise ScipArtifactError(f"path escape denied for {label}: {raw_path}") from exc
        return path


__all__ = ["LoadedScipArtifact", "ScipArtifactError", "ScipArtifactReader"]
