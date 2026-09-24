"""Compatibility adapter from public code-intel tools to the server index.

The public MCP wrappers are intentionally reused by the hosted/loopback server,
but they must never instantiate their historical per-worktree ``CodeContextEngine``.
This adapter implements the small engine surface those server-routed wrappers use
from the revision-bound Layer 1/2/3 index.

It is request-scoped by :class:`IndexSearchDispatcher`; no adapter is cached
across tenants or views.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .analysis import parse_query
from .boundary import addressable_in
from .contracts import AnalysisArtifact, EdgeKind, IndexBackend, ManifestEntry, SymbolDef
from .query import IndexQuery


@dataclass(frozen=True, slots=True)
class ServerSymbolRecord:
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
    documentation: list[str] | None = None
    snippet: str | None = None
    content_hash: str = ""
    score: float | None = None
    provenance: str = "server_index"
    origin: str = "internal"
    repo_name: str | None = None
    cross_lang_refs: list[Any] | None = None
    commit_sha: str | None = None

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "repo_id": self.repo_id,
            "file_path": self.file_path,
            "language": self.language,
            "symbol_name": self.symbol_name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "signature": self.signature,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "parent_symbol": self.parent_symbol,
            "doc_summary": self.doc_summary,
            "documentation": self.documentation,
            "snippet": self.snippet,
            "content_hash": self.content_hash,
            "score": self.score,
            "provenance": self.provenance,
            "origin": self.origin,
            "repo_name": self.repo_name,
            "cross_lang_refs": self.cross_lang_refs,
            "commit_sha": self.commit_sha,
        }


@dataclass(frozen=True, slots=True)
class _TextHit:
    file_path: str
    line: int


@dataclass(frozen=True, slots=True)
class _Row:
    path: str
    entry: ManifestEntry
    artifact: AnalysisArtifact
    data: bytes


class ServerIndexEngineAdapter:
    """The subset of ``CodeContextEngine`` needed by server-routed MCP tools."""

    __slots__ = (
        "_backend",
        "_cap",
        "_org_id",
        "_query",
        "_repo_id",
        "_repo_root",
        "_semantic_ranker",
        "_view_id",
    )

    def __init__(
        self,
        backend: IndexBackend,
        *,
        org_id: str,
        view_id: str,
        content_size_cap: int,
        repo_root: str | Path | None = None,
    ) -> None:
        self._backend = backend
        self._cap = content_size_cap
        self._org_id = org_id
        self._view_id = view_id
        self._query = IndexQuery(backend, content_size_cap=content_size_cap)
        self._repo_root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
        self._repo_id = backend.views.get(org_id, view_id).repo_id
        # Scoped-context line skimming treats a missing semantic ranker as an
        # allowed lexical-only mode. The actual semantic channel is server-side
        # in IndexQuery, not an object hanging off this compatibility adapter.
        self._semantic_ranker = None

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    @property
    def db_path(self) -> Path:
        """Compatibility sentinel for the public renderer; no database exists.

        The renderer only asks whether this path exists to decide if it should
        print a local-index bootstrap hint.  The materialized workspace already
        exists when the server dispatches a tool, so returning it truthfully
        means "the authoritative server index is ready" without manufacturing a
        per-worktree database.
        """
        return self._repo_root

    def index_ready(self) -> bool:
        return True

    def _current_index_version(self) -> int:
        return int(self._backend.views.get(self._org_id, self._view_id).view_revision)

    def tool_status(self, *, auto_index: bool = False, budget_tokens: int = 200, **kwargs: Any) -> dict[str, Any]:
        _ = auto_index, budget_tokens
        return {
            "index_version": self._current_index_version(),
            "status": "ready",
            "backend": "server_index",
        }

    def tool_cache_status(
        self, *, cache_tool: str | None = None, budget_tokens: int = 4000, **kwargs: Any
    ) -> dict[str, Any]:
        _ = budget_tokens
        return {
            "backend": "server_index",
            "cache_tool": cache_tool,
            "index_version": self._current_index_version(),
            "cache_entries": 0,
            "note": "server index lifecycle is managed by the server, not a per-worktree tool cache",
        }

    def tool_cache_invalidate(
        self, *, cache_tool: str | None = None, budget_tokens: int = 4000, **kwargs: Any
    ) -> dict[str, Any]:
        _ = budget_tokens
        return {
            "backend": "server_index",
            "cache_tool": cache_tool,
            "index_version": self._current_index_version(),
            "invalidated": 0,
            "note": "no client/worktree code-index cache exists to invalidate",
        }

    # ------------------------------------------------------------------
    # Layer helpers
    # ------------------------------------------------------------------

    def _rows(self) -> list[_Row]:
        state = self._backend.views.get(self._org_id, self._view_id)
        membership = self._backend.views.membership(self._org_id, self._view_id)
        reachable = addressable_in(
            self._backend.provenance,
            state,
            tuple(sorted({entry.content_digest for entry in membership.values()})),
        )
        rows: list[_Row] = []
        for path, entry in sorted(membership.items()):
            if entry.size > self._cap or entry.content_digest not in reachable:
                continue
            artifact = self._backend.analysis.get(self._org_id, entry.content_digest, entry.parser_profile)
            data = self._backend.content.get(self._org_id, entry.content_digest)
            if artifact is None or data is None:
                continue
            rows.append(_Row(path, entry, artifact, data))
        return rows

    @staticmethod
    def _line_offsets(data: bytes) -> list[int]:
        offsets = [0]
        for index, value in enumerate(data):
            if value == 10:
                offsets.append(index + 1)
        return offsets

    @staticmethod
    def _line_for_offset(offsets: list[int], offset: int) -> int:
        lo, hi = 0, len(offsets)
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if offsets[mid] <= offset:
                lo = mid
            else:
                hi = mid
        return lo + 1

    def _definition_end_line(self, row: _Row, definition: SymbolDef) -> int:
        lines = row.data.decode("utf-8", errors="replace").splitlines()
        if not lines or definition.line < 1 or definition.line > len(lines):
            return definition.line
        if definition.kind == "binding":
            return definition.line
        start = definition.line - 1
        language = row.artifact.language
        if language == "python":
            head = lines[start]
            base_indent = len(head) - len(head.lstrip(" \t"))
            end = start
            for index in range(start + 1, len(lines)):
                text = lines[index]
                stripped = text.strip()
                if not stripped:
                    end = index
                    continue
                indent = len(text) - len(text.lstrip(" \t"))
                if indent <= base_indent and not stripped.startswith(("#", "@")):
                    break
                end = index
            while end > start and not lines[end].strip():
                end -= 1
            return end + 1
        # Braced languages: definitions in the profiles we support begin on a
        # line carrying an opening brace. Track lexical brace balance; this is
        # intentionally structural-but-lightweight because Layer 1 already
        # established that the line is a definition.
        depth = 0
        opened = False
        for index in range(start, len(lines)):
            text = lines[index]
            opens = text.count("{")
            closes = text.count("}")
            if opens:
                opened = True
            depth += opens - closes
            if opened and depth <= 0:
                return index + 1
        return definition.line

    def _record(
        self, row: _Row, definition: SymbolDef, *, score: float | None = None, snippet: str = ""
    ) -> ServerSymbolRecord:
        lines = row.data.decode("utf-8", errors="replace").splitlines()
        offsets = self._line_offsets(row.data)
        end_line = self._definition_end_line(row, definition)
        signature = lines[definition.line - 1].strip() if 0 < definition.line <= len(lines) else definition.name
        parent: str | None = None
        parent_span = 1 << 60
        for candidate in row.artifact.definitions:
            if candidate.kind != "class" or candidate is definition:
                continue
            candidate_end = self._definition_end_line(row, candidate)
            if candidate.line < definition.line <= candidate_end:
                span = candidate_end - candidate.line
                if span < parent_span:
                    parent, parent_span = candidate.name, span
        qualified = f"{parent}.{definition.name}" if parent else definition.name
        effective_kind = "method" if parent and definition.kind == "function" else definition.kind
        start_byte = offsets[definition.line - 1] if definition.line - 1 < len(offsets) else definition.start
        end_byte = offsets[end_line] if end_line < len(offsets) else len(row.data)
        source = row.data[start_byte:end_byte].decode("utf-8", errors="replace").rstrip("\n")
        symbol_id = hashlib.sha256(f"{row.path}\0{qualified}\0{definition.line}".encode()).hexdigest()[:24]
        return ServerSymbolRecord(
            symbol_id=symbol_id,
            repo_id=self._repo_id,
            file_path=row.path,
            language=row.artifact.language,
            symbol_name=definition.name,
            qualified_name=qualified,
            kind=effective_kind,
            signature=signature,
            start_byte=start_byte,
            end_byte=end_byte,
            start_line=definition.line,
            end_line=end_line,
            parent_symbol=parent,
            snippet=snippet or source,
            content_hash=row.entry.content_digest,
            score=score,
        )

    def _all_records(self) -> list[ServerSymbolRecord]:
        return [self._record(row, definition) for row in self._rows() for definition in row.artifact.definitions]

    def _definition_matches(self, name: str) -> list[ServerSymbolRecord]:
        needle = name.strip()
        if not needle:
            return []
        return [
            record
            for record in self._all_records()
            if record.symbol_name == needle or record.qualified_name == needle or record.symbol_id == needle
        ]

    def _row_for_path(self, path: str) -> _Row | None:
        return next((row for row in self._rows() if row.path == path), None)

    def _enclosing(self, row: _Row, line: int) -> ServerSymbolRecord | None:
        candidates: list[ServerSymbolRecord] = []
        for definition in row.artifact.definitions:
            record = self._record(row, definition)
            if record.start_line <= line <= record.end_line:
                candidates.append(record)
        if not candidates:
            return None
        return min(candidates, key=lambda record: (record.end_line - record.start_line, -record.start_line))

    @staticmethod
    def _matches_glob(path: str, file_glob: str | None) -> bool:
        if not file_glob:
            return True
        return fnmatch.fnmatchcase(path, file_glob) or fnmatch.fnmatchcase(path, file_glob.removesuffix("/**") + "/*")

    # ------------------------------------------------------------------
    # Symbol/search compatibility
    # ------------------------------------------------------------------

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: str = "auto",
        kind: str | None = None,
        language: str | None = None,
        snippet: str = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        scope: str = "repo",
        since: str | None = None,
        touched_by: str | None = None,
        auto_index: bool = True,
        provenance_filter: str | None = None,
        _candidate_files: set[str] | None = None,
        **kwargs: Any,
    ) -> list[ServerSymbolRecord]:
        _ = scope, since, touched_by, auto_index, provenance_filter, snippet_lines
        exact = self._definition_matches(query)
        if exact:
            records = exact
        else:
            answer = self._query.search(
                org_id=self._org_id,
                view_id=self._view_id,
                query=query,
                limit=max(limit * 3, 20),
            )
            by_path = {hit.path: hit.score for hit in answer.hits}
            terms = set(parse_query(query).terms)
            records = []
            for row in self._rows():
                if row.path not in by_path:
                    continue
                for definition in row.artifact.definitions:
                    record = self._record(row, definition, score=float(by_path[row.path]))
                    if terms and terms & set(parse_query(f"{record.symbol_name} {record.signature}").terms):
                        record = ServerSymbolRecord(**{**record.model_dump(), "score": float(by_path[row.path]) + 1.0})
                    records.append(record)
            records.sort(key=lambda record: (-(record.score or 0.0), record.file_path, record.start_line))
        filtered = [
            record
            for record in records
            if (not kind or record.kind == kind)
            and (not language or record.language == language)
            and self._matches_glob(record.file_path, file_glob)
            and (_candidate_files is None or record.file_path in _candidate_files)
        ]
        if snippet == "none":
            filtered = [ServerSymbolRecord(**{**record.model_dump(), "snippet": None}) for record in filtered]
        return filtered[: max(1, limit)]

    def get_symbol(self, *, symbol_id: str) -> dict[str, Any]:
        matches = self._definition_matches(symbol_id)
        if not matches:
            raise LookupError(symbol_id)
        return self._symbol_payload(matches[0])

    def tool_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        line: int | None = None,
        budget_tokens: int = 4000,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = budget_tokens
        query = symbol_id or qualified_name or symbol_name or ""
        matches = self._definition_matches(query) if query else self._all_records()
        if file_path:
            matches = [record for record in matches if record.file_path == file_path]
        if line is not None:
            matches = [record for record in matches if record.start_line <= line <= record.end_line]
        if not matches:
            return {"error": "symbol_not_found", "query": query, "provenance": "server_index"}
        if len(matches) > 1 and not symbol_id:
            return {
                "error": "disambiguation_required",
                "matches": [self._symbol_payload(record, include_source=False) for record in matches[:10]],
                "provenance": "server_index",
            }
        return self._symbol_payload(matches[0])

    def _symbol_payload(self, record: ServerSymbolRecord, *, include_source: bool = True) -> dict[str, Any]:
        payload = {
            "symbol_id": record.symbol_id,
            "id": record.symbol_id,
            "file_path": record.file_path,
            "path": record.file_path,
            "language": record.language,
            "symbol_name": record.symbol_name,
            "name": record.symbol_name,
            "qualified_name": record.qualified_name,
            "kind": record.kind,
            "signature": record.signature,
            "start_line": record.start_line,
            "line": record.start_line,
            "end_line": record.end_line,
            "parent_symbol": record.parent_symbol,
            "provenance": "server_index",
            "origin": "internal",
        }
        if include_source:
            payload["source"] = record.snippet or ""
        return payload

    def tool_search(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: str = "auto",
        intent: str = "auto",
        kind: str | None = None,
        language: str | None = None,
        seed_files: list[str] | None = None,
        snippet: str = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        scope: str = "repo",
        budget_tokens: int = 4000,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = seed_files, budget_tokens
        resolved_mode = "semantic" if intent == "semantic" or mode == "semantic" else mode
        records = self.search_symbols(
            query,
            limit=limit,
            mode=resolved_mode,
            kind=kind,
            language=language,
            snippet=snippet,
            snippet_lines=snippet_lines,
            file_glob=file_glob,
            scope=scope,
        )
        items = [record.model_dump() for record in records]
        if not items:
            query_kind = "semantic" if resolved_mode in {"semantic", "hybrid"} else "lexical"
            answer = self._query.run(
                org_id=self._org_id,
                view_id=self._view_id,
                kind=query_kind,
                query=query,
                limit=limit,
            )
            for hit in answer.hits:
                row = self._row_for_path(hit.path)
                if row is None or not self._matches_glob(hit.path, file_glob):
                    continue
                text = row.data.decode("utf-8", errors="replace")
                terms = parse_query(query).terms
                lines = text.splitlines()
                line = next(
                    (i for i, value in enumerate(lines, 1) if any(term in value.lower() for term in terms)),
                    1,
                )
                head = (
                    "\n".join(lines[max(0, line - 1) : line - 1 + max(1, snippet_lines)]) if snippet != "none" else ""
                )
                items.append(
                    {
                        "file_path": hit.path,
                        "language": row.artifact.language,
                        "start_line": line,
                        "end_line": min(len(lines), line + max(0, snippet_lines - 1)),
                        "snippet": head,
                        "score": hit.score,
                        "provenance": "server_index",
                    }
                )
        return {
            "items": items[:limit],
            "mode": resolved_mode if resolved_mode != "auto" else "lexical",
            "provenance": "server_index",
            "cache_hit": False,
            "total_tokens": 0,
            "tokens_saved": 0,
        }

    def search_text(self, query: str, *, limit: int = 8, **kwargs: Any) -> list[_TextHit]:
        answer = self._query.run(
            org_id=self._org_id,
            view_id=self._view_id,
            kind="lexical",
            query=query,
            limit=limit,
        )
        hits: list[_TextHit] = []
        terms = parse_query(query).terms
        for hit in answer.hits:
            row = self._row_for_path(hit.path)
            if row is None:
                continue
            lines = row.data.decode("utf-8", errors="replace").splitlines()
            line = next((i for i, value in enumerate(lines, 1) if any(term in value.lower() for term in terms)), 1)
            hits.append(_TextHit(hit.path, line))
        return hits

    def search_channel_health(self, query: str, mode: str = "auto") -> Any:
        _ = query, mode
        try:
            from lemoncrow.pro.capabilities.code_context.search_verdict import ChannelHealth

            return ChannelHealth(semantic=True, zoekt=None)
        except Exception:
            return SimpleNamespace(semantic=True, zoekt=None)

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    def _target(self, query: str) -> tuple[ServerSymbolRecord | None, list[ServerSymbolRecord]]:
        matches = self._definition_matches(query)
        return (matches[0] if matches else None, matches)

    def _reference_rows(self, name: str) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        code_languages = {"python", "go", "typescript", "javascript", "rust", "c", "cpp"}
        for row in self._rows():
            lines = row.data.decode("utf-8", errors="replace").splitlines()
            import_lines = {item.line for item in row.artifact.imports}
            definition_lines = {item.line for item in row.artifact.definitions if item.name == name}
            seen_lines: set[int] = set()
            for ref in row.artifact.references:
                if ref.name != name or ref.line in import_lines or ref.line in seen_lines:
                    continue
                seen_lines.add(ref.line)
                caller = self._enclosing(row, ref.line)
                text = lines[ref.line - 1] if 0 < ref.line <= len(lines) else ""
                spans = [(match.start(), match.end()) for match in pattern.finditer(text)] or [(0, len(name))]
                for start, end in spans:
                    column = int(start) + 1
                    refs.append(
                        {
                            "path": row.path,
                            "line": ref.line,
                            "column": column,
                            "end_line": ref.line,
                            "end_column": int(end),
                            "caller": caller.qualified_name if caller else None,
                            "is_call": ref.is_call,
                        }
                    )
            # Some parser profiles intentionally keep Layer-1 references sparse
            # (for example a Python call nested inside an f-string). If a code
            # file has no non-import reference for this name, do a bounded
            # identifier scan over source lines. Definitions, imports and
            # comment-only lines are excluded so this does not recreate the old
            # text-match bug where a comment or the definition itself counted as
            # a usage.
            if not seen_lines and row.artifact.language in code_languages:
                for line_no, text in enumerate(lines, 1):
                    if line_no in import_lines or line_no in definition_lines:
                        continue
                    stripped = text.lstrip()
                    if stripped.startswith(("#", "//", "/*", "*")):
                        continue
                    matches = list(pattern.finditer(text))
                    if not matches:
                        continue
                    caller = self._enclosing(row, line_no)
                    for match in matches:
                        tail = text[match.end() :]
                        is_call = bool(re.match(r"\s*\(", tail))
                        if not is_call:
                            continue
                        refs.append(
                            {
                                "path": row.path,
                                "line": line_no,
                                "column": int(match.start()) + 1,
                                "end_line": line_no,
                                "end_column": int(match.end()),
                                "caller": caller.qualified_name if caller else None,
                                "is_call": True,
                            }
                        )
        return refs

    def tool_usages(
        self,
        query: str | None = None,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        group_by: str = "file",
        limit: int = 20,
        **kwargs: Any,
    ) -> dict[str, Any]:
        name = symbol_id or qualified_name or symbol_name or query or ""
        target, matches = self._target(name)
        if target is None:
            return {"error": "symbol_not_found", "query": name, "provenance": "server_index"}
        bare = target.symbol_name
        refs = self._reference_rows(bare)
        if file_path:
            refs = [ref for ref in refs if ref["path"] == file_path]
        truncated = len(refs) > limit
        refs = refs[:limit]
        if group_by == "none":
            references: Any = [{k: v for k, v in ref.items() if k != "is_call"} for ref in refs]
        else:
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for ref in refs:
                grouped[ref["path"]].append(
                    {k: v for k, v in ref.items() if k not in {"path", "is_call"}} | {"path": ref["path"]}
                )
            references = dict(grouped)
        payload: dict[str, Any] = {
            "target": {"id": target.symbol_id, "name": target.symbol_name, "path": target.file_path},
            "references": references,
            "reference_count": len(refs),
            "group_by": group_by,
            "truncated": truncated,
            "provenance": "server_index",
        }
        if len(matches) > 1:
            payload["ambiguity"] = {
                "note": f"merged {len(matches)} matching symbols for usages",
                "merged_target_count": len(matches),
                "matches": [self._symbol_payload(record, include_source=False) for record in matches[:10]],
            }
        return payload

    def tool_callers(
        self,
        query: str | None = None,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        depth: int = 1,
        limit: int = 20,
        **kwargs: Any,
    ) -> dict[str, Any]:
        name = symbol_id or qualified_name or symbol_name or query or ""
        target, _matches = self._target(name)
        if target is None:
            return {"error": "symbol_not_found", "query": name, "provenance": "server_index"}
        callers: dict[str, ServerSymbolRecord] = {}
        for ref in self._reference_rows(target.symbol_name):
            if not ref["is_call"] or not ref.get("caller"):
                continue
            row = self._row_for_path(str(ref["path"]))
            caller = self._enclosing(row, int(ref["line"])) if row else None
            if caller is not None and caller.symbol_id != target.symbol_id:
                callers[caller.symbol_id] = caller
        related = list(callers.values())[:limit]
        return {
            "target": self._symbol_payload(target, include_source=False),
            "direction": "callers",
            "depth": depth,
            "related": [self._symbol_payload(record, include_source=False) for record in related],
            "related_count": len(related),
            "edge_count": len(related),
            "truncated": len(callers) > limit,
            "data_status": "available" if related else "empty",
            "message": None if related else "no related call edges were found",
            "snapshot": None,
            "provenance": "server_index",
        }

    def tool_callees(
        self,
        query: str | None = None,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        depth: int = 1,
        limit: int = 20,
        **kwargs: Any,
    ) -> dict[str, Any]:
        name = symbol_id or qualified_name or symbol_name or query or ""
        target, _matches = self._target(name)
        if target is None:
            return {"error": "symbol_not_found", "query": name, "provenance": "server_index"}
        row = self._row_for_path(target.file_path)
        callees: dict[str, ServerSymbolRecord] = {}
        if row is not None:
            for ref in row.artifact.references:
                if not ref.is_call or not target.start_line <= ref.line <= target.end_line:
                    continue
                for record in self._definition_matches(ref.name):
                    if record.symbol_id != target.symbol_id:
                        callees[record.symbol_id] = record
        related = list(callees.values())[:limit]
        return {
            "target": self._symbol_payload(target, include_source=False),
            "direction": "callees",
            "depth": depth,
            "related": [self._symbol_payload(record, include_source=False) for record in related],
            "related_count": len(related),
            "edge_count": len(related),
            "truncated": len(callees) > limit,
            "data_status": "available" if related else "empty",
            "message": None if related else "no related call edges were found",
            "snapshot": None,
            "provenance": "server_index",
        }

    # ------------------------------------------------------------------
    # Context / graph
    # ------------------------------------------------------------------

    def tool_context(
        self,
        *,
        task: str,
        seed_files: list[str] | None = None,
        budget_tokens: int = 4000,
        max_symbols: int = 4,
        **kwargs: Any,
    ) -> dict[str, Any]:
        records = self.search_symbols(task, limit=max(max_symbols * 3, 8), snippet="head")
        if seed_files:
            seeds = set(seed_files)
            records.sort(key=lambda record: (record.file_path not in seeds, -(record.score or 0.0), record.file_path))
        records = records[:max_symbols]
        return {
            "task": task,
            "entry_points": [self._symbol_payload(record, include_source=False) for record in records],
            "related_symbols": [self._symbol_payload(record, include_source=False) for record in records],
            "code_blocks": [
                {
                    "path": record.file_path,
                    "symbol": record.qualified_name,
                    "content": record.snippet or "",
                    "line": record.start_line,
                    "end_line": record.end_line,
                }
                for record in records
            ],
            "budget_tokens": budget_tokens,
            "token_count": 0,
            "tokens_saved_vs_full_files": 0,
            "provenance": "server_index",
        }

    def call_graph_centrality(self, *, limit: int = 50, use_cache: bool = True) -> dict[str, Any]:
        _ = use_cache
        self._backend.links.refresh(self._org_id, self._view_id)
        pairs: list[tuple[str, str]] = []
        for edge in self._backend.links.edges(self._org_id, self._view_id):
            if edge.kind is not EdgeKind.CALL:
                continue
            row = self._row_for_path(edge.src_path)
            caller = self._enclosing(row, edge.src_line) if row else None
            if caller is None:
                continue
            pairs.append((caller.qualified_name, edge.symbol))
        try:
            from lemoncrow.pro.capabilities.code_context.call_graph_centrality import compute_call_graph_centrality

            result: dict[str, Any] = dict(compute_call_graph_centrality(pairs, limit=limit))
        except Exception:
            nodes = sorted({name for pair in pairs for name in pair})
            result = {"node_count": len(nodes), "edge_count": len(pairs), "ranking": [], "truncated": False}
        result["index_version"] = self._current_index_version()
        result["backend"] = "server_index"
        return result
