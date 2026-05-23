"""Persistent symbol index and token-budgeted retrieval for local code."""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from atelier.core.capabilities.code_context.budget import (
    FROZEN_DROP_STAGES,
    PROTECTED_TOP_RANK,
    BudgetPacker,
)
from atelier.core.capabilities.code_context.cache import RetrievalCache
from atelier.core.capabilities.code_context.call_graph import (
    CallGraphDirection,
    CallGraphEdge,
    CallGraphNode,
    CallGraphTraversalResult,
    build_call_graph_payload,
    traverse_call_graph,
)
from atelier.core.capabilities.code_context.embedding import (
    SearchMode,
    SemanticSearchRanker,
    resolve_search_mode,
    semantic_candidate_limit,
)
from atelier.core.capabilities.code_context.intel_store import ProviderHealth, SymbolIntelStore
from atelier.core.capabilities.code_context.models import (
    ContextPack,
    CrossLangReference,
    ImpactResult,
    IndexedFileRecord,
    IndexStats,
    RouteRecord,
    SymbolRecord,
    TextMatch,
    UsageReference,
)
from atelier.core.capabilities.code_context.output_policy import hard_cap_chars, resolve_output_policy
from atelier.core.capabilities.repo_map import build_repo_map
from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.core.capabilities.repo_map.graph import iter_source_files
from atelier.core.foundation.paths import default_store_root
from atelier.core.service.telemetry import emit_product_local
from atelier.infra.code_intel.astgrep import (
    AstGrepAdapter,
    AstGrepToolUnavailable,
    PatternMatch,
    PatternRewriteResult,
    PatternSearchResult,
)
from atelier.infra.code_intel.cross_lang import CrossLangEdge, CrossLangEdgeStore
from atelier.infra.tree_sitter.tags import detect_language, extract_tags

if TYPE_CHECKING:
    from atelier.infra.code_intel.git_history.adapter import DeletedHistorySearchAdapter

_MAX_FILE_BYTES = 1_000_000
_FTS_TERM_RE = re.compile(r"[A-Za-z0-9_]+")
_SINCE_RELATIVE_RE = re.compile(r"^(?P<amount>\d+)(?P<unit>[dwmy])$")
_JS_IMPORT_RE = re.compile(
    r"(?:from\s+['\"]([^'\"]+)['\"]|import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)|require\(\s*['\"]([^'\"]+)['\"]\s*\))"
)
_RUST_MOD_RE = re.compile(r"^\s*(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", re.M)
_GO_IMPORT_RE = re.compile(r"^\s*import\s+(?:\((.*?)\)|\"([^\"]+)\")", re.M | re.S)
_FASTAPI_DECORATOR_RE = re.compile(
    r"^\s*@(?P<router>[A-Za-z_][A-Za-z0-9_]*)\.(?P<verb>get|post|put|patch|delete|options|head|trace|websocket)\(\s*['\"](?P<route>[^'\"]+)['\"]"
)
_FASTAPI_API_ROUTE_RE = re.compile(
    r"^\s*@(?P<router>[A-Za-z_][A-Za-z0-9_]*)\.api_route\(\s*['\"](?P<route>[^'\"]+)['\"](?P<rest>.*)\)\s*$"
)
_FLASK_ROUTE_RE = re.compile(
    r"^\s*@(?P<router>[A-Za-z_][A-Za-z0-9_]*)\.route\(\s*['\"](?P<route>[^'\"]+)['\"](?P<rest>.*)\)\s*$"
)
_FLASK_ADD_URL_RULE_RE = re.compile(
    r"^\s*(?P<router>[A-Za-z_][A-Za-z0-9_]*)\.add_url_rule\(\s*['\"](?P<route>[^'\"]+)['\"](?P<rest>.*)\)\s*$"
)
_DJANGO_PATH_RE = re.compile(
    r"^\s*(?:re_)?path\(\s*['\"](?P<route>[^'\"]+)['\"]\s*,\s*(?P<handler>[A-Za-z_][A-Za-z0-9_\.]*)"
)
_DJANGO_URL_RE = re.compile(
    r"^\s*url\(\s*(?:r)?['\"](?P<route>[^'\"]+)['\"]\s*,\s*(?P<handler>[A-Za-z_][A-Za-z0-9_\.]*)"
)
_EXPRESS_ROUTE_RE = re.compile(
    r"(?P<router>app|router)\.(?P<verb>get|post|put|patch|delete|options|head|all|use)\(\s*[`'\"](?P<route>[^`'\"]+)[`'\"]\s*(?:,\s*(?P<handler>[A-Za-z_$][A-Za-z0-9_$.]*))?"
)
_EXPRESS_ROUTE_CHAIN_RE = re.compile(
    r"(?P<router>app|router)\.route\(\s*[`'\"](?P<route>[^`'\"]+)[`'\"]\s*\)(?P<chain>.+)$"
)
_EXPRESS_CHAIN_METHOD_RE = re.compile(
    r"\.(?P<verb>get|post|put|patch|delete|options|head|all|use)\(\s*(?P<handler>[A-Za-z_$][A-Za-z0-9_$.]*)?"
)
_METHOD_LITERAL_RE = re.compile(r"['\"](?P<method>GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD|TRACE|CONNECT)['\"]", re.I)
_LOCAL_PROVENANCE = "local"
_SEARCH_ESSENTIAL_KEYS = [
    "symbol_id",
    "symbol_name",
    "qualified_name",
    "file_path",
    "kind",
    "signature",
    "start_line",
    "end_line",
    "language",
    "origin",
    "provenance",
]
_SEARCH_OPTIONAL_KEYS = [
    "snippet",
    "doc_summary",
    "score",
    "repo_id",
    "content_hash",
    "parent_symbol",
    "start_byte",
    "end_byte",
]
_DELETED_SEARCH_ESSENTIAL_KEYS = [
    *_SEARCH_ESSENTIAL_KEYS,
    "deleted_at",
    "deleted_at_sha",
    "last_author",
]
_DELETED_SEARCH_OPTIONAL_KEYS = [
    "rename_target",
    "rename_note",
    "last_commit_msg",
    "matched_on",
]
_SEARCH_COMPACT_DEFAULT_KEYS = set([*_SEARCH_ESSENTIAL_KEYS, "score"])
_DELETED_SEARCH_COMPACT_DEFAULT_KEYS = set(
    [*_DELETED_SEARCH_ESSENTIAL_KEYS, "score", "matched_on", "rename_target", "rename_note"]
)
_OUTLINE_ESSENTIAL_KEYS = ["file_path", "name", "qualified_name", "kind", "signature", "line_start", "line_end"]
_FILES_ESSENTIAL_KEYS = ["file_path"]
_FILES_OPTIONAL_KEYS = ["top_symbols", "symbol_count", "language"]
_ROUTES_ESSENTIAL_KEYS = ["framework", "method", "route", "file_path", "line", "provenance"]
_ROUTES_OPTIONAL_KEYS = ["handler", "router", "language"]
_SYMBOL_ESSENTIAL_KEYS = [
    "symbol_id",
    "symbol_name",
    "qualified_name",
    "file_path",
    "kind",
    "signature",
    "start_line",
    "end_line",
    "language",
    "origin",
    "provenance",
]
_SYMBOL_OPTIONAL_KEYS = [
    "source",
    "doc_summary",
    "content_hash",
    "parent_symbol",
    "start_byte",
    "end_byte",
    "repo_id",
    "score",
    "cross_lang_refs",
]
_INDEX_ESSENTIAL_KEYS = [
    "repo_id",
    "repo_root",
    "files_indexed",
    "symbols_indexed",
    "imports_indexed",
    "index_version",
    "provenance",
]
_CONTEXT_ESSENTIAL_KEYS = [
    "task",
    "budget_tokens",
    "token_count",
    "tokens_saved_vs_full_files",
    "symbols",
    "content",
    "provenance",
]
_EXPLORE_ESSENTIAL_KEYS = [
    "query",
    "entry_points",
    "truncated",
    "provenance",
]
_EXPLORE_OPTIONAL_KEYS = ["relationships", "files", "additional_relevant_files"]
_IMPACT_ESSENTIAL_KEYS = ["file_path", "direct_importers", "affected_tests", "risk_level", "provenance"]
_USAGES_ESSENTIAL_KEYS = ["file_path", "line", "column", "end_line", "end_column", "provenance"]
_USAGES_OPTIONAL_KEYS = ["snippet", "caller", "edge_kind", "confidence"]
_PATTERN_ESSENTIAL_KEYS = ["file_path", "line", "column", "end_line", "end_column", "captures"]
_PATTERN_OPTIONAL_KEYS = ["snippet"]
_STATUS_ESSENTIAL_KEYS = [
    "repo_id",
    "repo_root",
    "db_path",
    "index_version",
    "index",
    "cache",
    "providers",
    "provider_freshness",
    "warnings",
    "freshness",
    "autosync",
    "provenance",
]
_CACHE_STATUS_ESSENTIAL_KEYS = [
    "repo_id",
    "index_version",
    "entry_count",
    "entries_by_tool",
    "total_bytes",
    "max_bytes",
]
_CACHE_INVALIDATE_ESSENTIAL_KEYS = ["repo_id", "index_version", "invalidated_entries", "entries_by_tool", "scope"]
_CALL_GRAPH_ESSENTIAL_KEYS = ["target", "direction", "depth", "related", "edges", "data_status", "provenance"]
_CALL_GRAPH_OPTIONAL_KEYS = ["related_count", "edge_count", "truncated", "message", "snapshot"]
_BLAME_ESSENTIAL_KEYS = [
    "symbol_name",
    "qualified_name",
    "file_path",
    "line_start",
    "line_end",
    "freshness",
    "last_author",
    "last_commit_sha",
    "age_days",
    "local_edits",
    "distinct_authors",
    "provenance",
]
_BLAME_OPTIONAL_KEYS = ["index_sha", "head_sha", "last_modified", "last_commit_summary", "hunks", "churn"]
_OVERFLOW_SPILL_MIN_EXCESS_TOKENS = 128
_OVERFLOW_SPILL_MIN_REDUCTION_TOKENS = 256
DeletedHistoryItem = dict[str, Any]
_CACHE_TOOL_ALIASES = {
    "all": None,
    "explore": "code.explore",
    "files": "code.files",
    "status": "code.status",
    "routes": "code.routes",
    "search": "code.search",
    "symbol": "code.symbol",
    "outline": "code.outline",
    "context": "code.context",
    "impact": "code.impact",
    "usages": "code.usages",
    "callers": "code.callers",
    "callees": "code.callees",
    "pattern": "code.pattern",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class _ExtractedSymbol:
    name: str
    qualified_name: str
    kind: str
    signature: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    parent_symbol: str | None = None
    doc_summary: str | None = None


@dataclass(frozen=True)
class _IndexedReference:
    """A local, index-time reference extracted from Python AST."""

    file_path: str
    symbol_name: str
    line: int
    column: int
    end_column: int
    enclosing_symbol_name: str | None
    enclosing_qualified_name: str | None
    snippet: str


@dataclass(frozen=True)
class _IndexedCallEdge:
    """A local, index-time function/method call edge extracted from Python AST."""

    caller_symbol_name: str
    caller_qualified_name: str
    caller_file_path: str
    caller_start_line: int
    caller_end_line: int
    callee_name: str
    call_line: int
    call_column: int
    snippet: str


def _repo_id(repo_root: Path) -> str:
    return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:16]


def _default_db_path(repo_root: Path) -> Path:
    workspace_hash = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
    return default_store_root() / "workspaces" / workspace_hash / "code_context.sqlite"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    total = 0
    for line in text.splitlines(keepends=True):
        total += len(line.encode("utf-8"))
        offsets.append(total)
    if not text.endswith(("\n", "\r")):
        offsets.append(total)
    return offsets


def _byte_to_line(offsets: list[int], byte_offset: int) -> int:
    return max(1, bisect_right(offsets, byte_offset))


def _safe_relpath(repo_root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(repo_root))
    except ValueError:
        return str(resolved)


def _safe_fts_query(query: str) -> str:
    terms = _FTS_TERM_RE.findall(query)
    return " OR ".join(term[:64] for term in terms[:12])


def _parse_since_filter(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError("since must not be empty")
    match = _SINCE_RELATIVE_RE.fullmatch(normalized.lower())
    if match:
        amount = int(match.group("amount"))
        unit = match.group("unit")
        delta = {
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
            "m": timedelta(days=amount * 30),
            "y": timedelta(days=amount * 365),
        }[unit]
        return int((datetime.now(UTC) - delta).timestamp())
    iso_value = normalized.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("since must be an ISO date/datetime or relative duration like 30d") from exc
        parsed = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _normalize_touched_by(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("touched_by must not be empty")
    return normalized


def _row_to_symbol(row: sqlite3.Row) -> SymbolRecord:
    row_keys = set(row.keys())
    return SymbolRecord(
        symbol_id=str(row["symbol_id"]),
        repo_id=str(row["repo_id"]),
        file_path=str(row["file_path"]),
        language=str(row["language"]),
        symbol_name=str(row["symbol_name"]),
        qualified_name=str(row["qualified_name"]),
        kind=str(row["kind"]),
        signature=str(row["signature"]),
        start_byte=int(row["start_byte"]),
        end_byte=int(row["end_byte"]),
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        parent_symbol=cast(str | None, row["parent_symbol"]),
        doc_summary=cast(str | None, row["doc_summary"]),
        content_hash=str(row["content_hash"]),
        score=float(row["score"]) if "score" in row_keys and row["score"] is not None else None,
    )


def _git_repo_class() -> Any:
    try:
        from git import Repo
    except Exception:  # pragma: no cover - optional dependency fallback
        return None
    return Repo


class CodeContextEngine:
    """Local code intelligence using tree-sitter tags, SQLite FTS5, rg, and repo-map ranking."""

    def __init__(self, repo_root: str | Path = ".", *, db_path: str | Path | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.repo_id = _repo_id(self.repo_root)
        self.db_path = Path(db_path).resolve() if db_path is not None else _default_db_path(self.repo_root)
        self._cache = RetrievalCache(self.db_path)
        self._budget = BudgetPacker()
        self._semantic_ranker = SemanticSearchRanker(self.repo_root, store_root=default_store_root())
        self.intel_store = SymbolIntelStore(
            cache=self._cache,
            packer=self._budget,
            local_search=self._search_symbols_local,
            local_get_symbol=self._get_symbol_local,
            local_find_references=self._find_references_local,
            local_find_callers=self._find_callers_local,
            local_find_callees=self._find_callees_local,
        )
        self._deleted_history_search_adapter: DeletedHistorySearchAdapter | None = None
        self._autosync_enabled = os.getenv("ATELIER_CODE_AUTOSYNC", "").strip().lower() in {"1", "true", "yes", "on"}
        self._autosync_debounce_ms = self._parse_autosync_debounce(os.getenv("ATELIER_CODE_AUTOSYNC_DEBOUNCE_MS"))
        self._autosync_state = "idle"
        self._autosync_signature: str | None = None
        self._autosync_last_sync_ms = 0
        self._autosync_last_event_at: str | None = None
        self._autosync_pending_events = 0
        self._autosync_reindex_count = 0
        self._autosync_history: list[dict[str, Any]] = []
        self._register_symbol_intel_providers()

    def index_repo(
        self,
        *,
        include_globs: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        force: bool = True,
    ) -> IndexStats:
        """Build or rebuild the persistent symbol/import index for this repository."""
        del force
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        files = [
            path
            for path in iter_source_files(self.repo_root, include_globs=include_globs)
            if not self._excluded(path, exclude_globs or [])
        ]
        files_indexed = 0
        symbols_indexed = 0
        imports_indexed = 0
        with self._connect() as conn:
            self._init_schema(conn)
            conn.execute("DELETE FROM symbol_fts")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM imports")
            conn.execute('DELETE FROM "references"')
            conn.execute("DELETE FROM call_edges")
            conn.execute("DELETE FROM files")
            for path in files:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size > _MAX_FILE_BYTES:
                    continue
                source_bytes = path.read_bytes()
                source = source_bytes.decode("utf-8", errors="replace")
                language = detect_language(path) or "text"
                rel = _safe_relpath(self.repo_root, path)
                content_hash = _sha256_bytes(source_bytes)
                conn.execute(
                    """
                    INSERT INTO files(repo_id, file_path, language, content_hash, size_bytes, indexed_at)
                    VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    """,
                    (self.repo_id, rel, language, content_hash, stat.st_size),
                )
                extracted = self._extract_symbols(path, rel, language, source, content_hash)
                for symbol in extracted:
                    self._insert_symbol(conn, rel, language, content_hash, symbol)
                imports = self._extract_imports(path, rel, language, source)
                for raw_import, target_file in imports:
                    conn.execute(
                        "INSERT INTO imports(repo_id, source_file, raw_import, target_file) VALUES (?, ?, ?, ?)",
                        (self.repo_id, rel, raw_import, target_file),
                    )
                if language == "python":
                    references, call_edges = self._extract_python_reference_index(rel, source, extracted)
                    for reference in references:
                        conn.execute(
                            """
                            INSERT INTO "references"(
                                repo_id, symbol_name, file_path, line, column, end_column,
                                enclosing_symbol_name, enclosing_qualified_name, snippet
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                self.repo_id,
                                reference.symbol_name,
                                reference.file_path,
                                reference.line,
                                reference.column,
                                reference.end_column,
                                reference.enclosing_symbol_name,
                                reference.enclosing_qualified_name,
                                reference.snippet,
                            ),
                        )
                    for edge in call_edges:
                        conn.execute(
                            """
                            INSERT INTO call_edges(
                                repo_id, caller_symbol_name, caller_qualified_name, caller_file_path,
                                caller_start_line, caller_end_line, callee_name, call_line, call_column, snippet
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                self.repo_id,
                                edge.caller_symbol_name,
                                edge.caller_qualified_name,
                                edge.caller_file_path,
                                edge.caller_start_line,
                                edge.caller_end_line,
                                edge.callee_name,
                                edge.call_line,
                                edge.call_column,
                                edge.snippet,
                            ),
                        )
                files_indexed += 1
                symbols_indexed += len(extracted)
                imports_indexed += len(imports)
            index_version = self._bump_index_version(conn)
        emit_product_local(
            "code_index_completed",
            repo_id=self.repo_id,
            files_indexed=files_indexed,
            symbols_indexed=symbols_indexed,
        )
        return IndexStats(
            repo_id=self.repo_id,
            repo_root=str(self.repo_root),
            db_path=str(self.db_path),
            files_indexed=files_indexed,
            symbols_indexed=symbols_indexed,
            imports_indexed=imports_indexed,
            index_version=index_version,
        )

    def tool_index(
        self,
        *,
        include_globs: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        force: bool = True,
        budget_tokens: int = 4000,
    ) -> dict[str, Any]:
        self._sync_symbol_intel()
        stats = self.index_repo(include_globs=include_globs, exclude_globs=exclude_globs, force=force)
        return self._pack_single_payload(
            stats.model_dump(mode="json"),
            budget_tokens=budget_tokens,
            essential_keys=_INDEX_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=["db_path"],
        )

    def tool_search(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: SearchMode = "auto",
        kind: str | None = None,
        language: str | None = None,
        snippet: Literal["none", "head", "full"] = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        scope: Literal["repo", "external", "deleted"] = "repo",
        since: str | None = None,
        touched_by: str | None = None,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        effective_budget_tokens = self._effective_budget_tokens("search", budget_tokens)
        if auto_index and scope != "deleted":
            self._ensure_indexed()
        self._sync_symbol_intel()
        resolved_mode = resolve_search_mode(query, mode)
        temporal_scope = scope in {"repo", "deleted"}
        parsed_since = _parse_since_filter(since) if temporal_scope else None
        normalized_touched_by = _normalize_touched_by(touched_by) if temporal_scope else None
        cache_args = {
            "query": query,
            "limit": limit,
            "mode": mode,
            "resolved_mode": resolved_mode,
            "kind": kind,
            "language": language,
            "snippet": snippet,
            "snippet_lines": snippet_lines,
            "file_glob": file_glob,
            "scope": scope,
            "since_ts": parsed_since,
            "touched_by": normalized_touched_by,
            "budget_tokens": effective_budget_tokens,
            "semantic_candidate_limit": semantic_candidate_limit(limit),
        }
        hit, cached = self._cache_get("code.search", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)

        if scope == "deleted":
            raw_deleted_items = self.search_symbols(
                query,
                limit=limit,
                mode=resolved_mode,
                kind=kind,
                language=language,
                snippet=snippet,
                snippet_lines=snippet_lines,
                file_glob=file_glob,
                scope="deleted",
                since=since,
                touched_by=touched_by,
                auto_index=False,
            )
            items = [dict(item) for item in raw_deleted_items]
        else:
            raw_items = self.search_symbols(
                query,
                limit=limit,
                mode=resolved_mode,
                kind=kind,
                language=language,
                snippet=snippet,
                snippet_lines=snippet_lines,
                file_glob=file_glob,
                scope=scope,
                since=since,
                touched_by=touched_by,
                auto_index=False,
            )
            items = [item.model_dump(mode="json", exclude_none=True) for item in raw_items]
        if scope == "repo" and (parsed_since is not None or normalized_touched_by is not None):
            changed_files = self._deleted_history_adapter().changed_files(
                since_ts=parsed_since,
                touched_by=normalized_touched_by,
            )
            items = [item for item in items if str(item.get("file_path") or "") in changed_files]
        items = self._dedupe_search_items(items)
        if snippet == "none":
            items = self._compact_search_items(items, scope=scope)
        essential_keys = _DELETED_SEARCH_ESSENTIAL_KEYS if scope == "deleted" else _SEARCH_ESSENTIAL_KEYS
        optional_keys = _DELETED_SEARCH_OPTIONAL_KEYS if scope == "deleted" else _SEARCH_OPTIONAL_KEYS
        payload = self._pack_items_payload(
            items,
            budget_tokens=effective_budget_tokens,
            essential_keys=essential_keys,
            optional_keys_in_drop_order=optional_keys,
            extra_payload={"mode": resolved_mode},
        )
        self._cache_set("code.search", cache_args, payload)
        return payload

    def tool_blame(
        self,
        *,
        query: str | None = None,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        include_churn: bool = True,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        target = self._resolve_symbol_target(
            operation_name="blame",
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=file_path,
            kind=None,
            language=None,
            file_glob=None,
        )
        if target.get("error"):
            return self._pack_single_payload(
                target,
                budget_tokens=budget_tokens,
                essential_keys=["error", "message", "matches", "cache_hit", "provenance"],
                optional_keys_in_drop_order=["provenance_breakdown"],
            )
        head_sha = self._current_head_sha()
        index_sha = str(target.get("index_sha") or head_sha)
        normalized_file_path = str(target["file_path"])
        cache_args = {
            "query": query,
            "symbol_id": symbol_id or target.get("symbol_id"),
            "qualified_name": qualified_name or target.get("qualified_name"),
            "symbol_name": symbol_name or target.get("symbol_name"),
            "file_path": normalized_file_path,
            "include_churn": include_churn,
            "index_sha": index_sha,
            "head_sha": head_sha,
            "budget_tokens": budget_tokens,
        }
        hit, cached = self._cache_get("code.blame", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)
        if index_sha != head_sha:
            payload = self._pack_single_payload(
                {
                    "error": "index_stale",
                    "hint": 'run code op="index" first',
                    "symbol_name": str(target["symbol_name"]),
                    "qualified_name": str(target["qualified_name"]),
                    "file_path": normalized_file_path,
                    "freshness": "stale",
                    "index_sha": index_sha,
                    "head_sha": head_sha,
                    "provenance": "blame",
                },
                budget_tokens=budget_tokens,
                essential_keys=[
                    "error",
                    "hint",
                    "symbol_name",
                    "qualified_name",
                    "file_path",
                    "freshness",
                    "provenance",
                ],
                optional_keys_in_drop_order=["index_sha", "head_sha"],
            )
            self._cache_set("code.blame", cache_args, payload)
            return payload
        from atelier.infra.code_intel.git_history.blame import BlameAnnotator
        from atelier.infra.code_intel.git_history.models import BlameRequest

        annotation = BlameAnnotator(self.repo_root).annotate(
            BlameRequest(
                file_path=normalized_file_path,
                line_start=int(target["start_line"]),
                line_end=int(target["end_line"]),
                index_sha=index_sha,
                head_sha=head_sha,
                include_churn=include_churn,
            )
        )
        latest_commit_ts = max(hunk.commit_time for hunk in annotation.hunks)
        payload_data: dict[str, Any] = {
            "symbol_name": str(target["symbol_name"]),
            "qualified_name": str(target["qualified_name"]),
            "file_path": normalized_file_path,
            "line_start": int(target["start_line"]),
            "line_end": int(target["end_line"]),
            "index_sha": index_sha,
            "head_sha": head_sha,
            "freshness": annotation.freshness,
            "last_modified": datetime.fromtimestamp(latest_commit_ts, tz=UTC).isoformat().replace("+00:00", "Z"),
            "last_author": annotation.last_author,
            "last_commit_sha": annotation.last_commit_sha,
            "last_commit_summary": annotation.last_commit_summary,
            "age_days": annotation.age_days,
            "local_edits": annotation.local_edits,
            "distinct_authors": len({hunk.author_email for hunk in annotation.hunks if hunk.author_email}),
            "hunks": [asdict(hunk) for hunk in annotation.hunks],
            "provenance": "blame",
        }
        if annotation.churn is not None:
            payload_data["churn"] = asdict(annotation.churn)
        payload = self._pack_single_payload(
            payload_data,
            budget_tokens=budget_tokens,
            essential_keys=_BLAME_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=_BLAME_OPTIONAL_KEYS,
        )
        self._cache_set("code.blame", cache_args, payload)
        return payload

    def tool_hover(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        line: int | None = None,
        col: int | None = None,
        budget_tokens: int = 2000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        """Surface type, docstring, and signature for a symbol — no subprocess needed.

        Resolution priority: symbol_id → qualified_name → (file_path, line) → symbol_name.
        Returns {symbol_id, symbol_name, qualified_name, kind, signature, docstring,
                 documentation, file, line, col, source_snippet, provenance, cache_hit}.
        """
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()

        sym: dict[str, Any]

        positional_lookup = (
            file_path is not None and line is not None and not symbol_id and not qualified_name and not symbol_name
        )
        if positional_lookup:
            normalized = self._normalize_file_arg(file_path)  # type: ignore[arg-type]
            with self._connect() as conn:
                self._init_schema(conn)
                row = conn.execute(
                    """
                    SELECT *, NULL AS score FROM symbols
                    WHERE repo_id = ? AND file_path = ? AND start_line <= ? AND end_line >= ?
                    ORDER BY start_line DESC
                    LIMIT 1
                    """,
                    (self.repo_id, normalized, line, line),
                ).fetchone()
            if row is None:
                raise LookupError("no symbol at that position")
            symbol_rec = _row_to_symbol(row)
            path = self.repo_root / symbol_rec.file_path
            source = path.read_bytes()[symbol_rec.start_byte : symbol_rec.end_byte].decode("utf-8", errors="replace")
            sym = {**symbol_rec.model_dump(mode="json"), "source": source}
        else:
            sym = self.get_symbol(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                symbol_name=symbol_name,
                file_path=file_path,
                auto_index=False,
            )

        emit_product_local("code_hover_retrieved", repo_id=self.repo_id, kind=sym["kind"])
        return {
            "symbol_id": sym["symbol_id"],
            "symbol_name": sym["symbol_name"],
            "qualified_name": sym["qualified_name"],
            "kind": sym["kind"],
            "signature": sym["signature"],
            "docstring": sym.get("doc_summary"),
            "documentation": sym.get("documentation"),
            "file": sym["file_path"],
            "line": sym["start_line"],
            "col": None,
            "source_snippet": sym.get("source", "")[:500],
            "provenance": sym.get("provenance", "local"),
            "cache_hit": sym.get("cache_hit", False),
            "tokens_saved": sym.get("tokens_saved", 0),
        }

    def tool_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        effective_budget_tokens = self._effective_budget_tokens("node", budget_tokens)
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        normalized_file_path = self._normalize_file_arg(file_path) if file_path else None
        cache_args = {
            "symbol_id": symbol_id,
            "qualified_name": qualified_name,
            "symbol_name": symbol_name,
            "file_path": normalized_file_path,
            "budget_tokens": effective_budget_tokens,
        }
        hit, cached = self._cache_get("code.symbol", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)

        payload = self._pack_single_payload(
            self._hydrate_symbol_cross_lang(
                self.get_symbol(
                    symbol_id=symbol_id,
                    qualified_name=qualified_name,
                    symbol_name=symbol_name,
                    file_path=normalized_file_path,
                    auto_index=False,
                )
            ),
            budget_tokens=effective_budget_tokens,
            essential_keys=_SYMBOL_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=_SYMBOL_OPTIONAL_KEYS,
        )
        self._cache_set("code.symbol", cache_args, payload)
        return payload

    def _hydrate_symbol_cross_lang(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol_id = str(payload.get("symbol_id") or "")
        symbol_name = str(payload.get("symbol_name") or "")
        if not symbol_id:
            return payload
        refs: list[CrossLangReference] = []
        for edge in self._cross_lang_store().query_by_source_symbol(symbol_id):
            refs.append(self._symbol_cross_lang_ref(edge, direction="outgoing"))
        for edge in self._cross_lang_store().query_by_target_symbol(
            tgt_symbol_id=symbol_id, tgt_symbol_name=symbol_name
        ):
            refs.append(self._symbol_cross_lang_ref(edge, direction="incoming"))
        if not refs:
            return payload
        deduped = list(
            {
                (
                    ref.direction,
                    ref.symbol_id,
                    ref.symbol_name,
                    ref.file_path,
                    ref.line,
                    ref.edge_kind,
                ): ref
                for ref in refs
            }.values()
        )
        allowed = {key: value for key, value in payload.items() if key in SymbolRecord.model_fields}
        validated = SymbolRecord.model_validate(
            {
                **allowed,
                "cross_lang_refs": [ref.model_dump(mode="json", exclude_none=True) for ref in deduped],
            }
        ).model_dump(mode="json", exclude_none=True)
        if "source" in payload:
            validated["source"] = payload["source"]
        return validated

    def _symbol_cross_lang_ref(
        self,
        edge: CrossLangEdge,
        *,
        direction: Literal["incoming", "outgoing"],
    ) -> CrossLangReference:
        if direction == "incoming":
            return CrossLangReference(
                symbol_id=edge.src_symbol_id,
                symbol_name=edge.src_symbol_name,
                qualified_name=edge.src_qualified_name,
                language=edge.src_language,
                file_path=edge.src_file_path,
                line=edge.src_line,
                direction=direction,
                edge_kind=edge.edge_kind,
                confidence=edge.confidence,
            )
        return CrossLangReference(
            symbol_id=edge.tgt_symbol_id,
            symbol_name=edge.tgt_symbol_name,
            qualified_name=None,
            language=edge.tgt_language,
            file_path=edge.tgt_file_path,
            line=edge.src_line,
            direction=direction,
            edge_kind=edge.edge_kind,
            confidence=edge.confidence,
        )

    def _cross_lang_store(self) -> CrossLangEdgeStore:
        return CrossLangEdgeStore(self.connection)

    def tool_outline(
        self,
        *,
        file_path: str | None = None,
        limit: int = 200,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        effective_budget_tokens = self._effective_budget_tokens("outline", budget_tokens)
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        normalized_file_path = self._normalize_file_arg(file_path) if file_path else None
        cache_args = {
            "file_path": normalized_file_path,
            "limit": limit,
            "budget_tokens": effective_budget_tokens,
            "file_mtime_ns": self._file_mtime_ns(normalized_file_path) if normalized_file_path else None,
        }
        hit, cached = self._cache_get("code.outline", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)

        raw = self.file_outline(file_path=normalized_file_path, limit=limit, auto_index=False)
        flat_items = self._flatten_outline(raw["files"])
        full_payload = {
            "repo_id": str(raw["repo_id"]),
            "files": raw["files"],
            "symbol_count": int(raw["symbol_count"]),
            "provenance": _LOCAL_PROVENANCE,
        }
        full_total_tokens = self._compute_total_tokens({**full_payload, "cache_hit": False, "tokens_saved": 0})

        def build_payload(packed_items: list[dict[str, Any]]) -> dict[str, Any]:
            return self._finalize_packed_payload(
                {
                    "repo_id": str(raw["repo_id"]),
                    "files": self._group_outline(packed_items),
                    "symbol_count": len(packed_items),
                    "cache_hit": False,
                    "provenance": _LOCAL_PROVENANCE,
                },
                full_total_tokens=full_total_tokens,
            )

        payload = self._fit_items_to_budget(
            flat_items,
            budget_tokens=effective_budget_tokens,
            essential_keys=_OUTLINE_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=[],
            build_payload=build_payload,
        )
        self._cache_set("code.outline", cache_args, payload)
        return payload

    def tool_files(
        self,
        *,
        path: str | None = None,
        pattern: str | None = None,
        format: Literal["tree", "flat", "grouped"] = "tree",
        include_metadata: bool = True,
        max_depth: int | None = None,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        if max_depth is not None and max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        normalized_path = self._normalize_files_path(path)
        normalized_pattern = (pattern or "").strip() or None
        cache_args = {
            "path": normalized_path,
            "pattern": normalized_pattern,
            "format": format,
            "include_metadata": include_metadata,
            "max_depth": max_depth,
            "budget_tokens": budget_tokens,
        }
        hit, cached = self._cache_get("code.files", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)

        items = self._indexed_file_records(path=normalized_path, pattern=normalized_pattern, max_depth=max_depth)
        essential_keys = list(_FILES_ESSENTIAL_KEYS)
        if format == "grouped":
            essential_keys.append("language")
        optional_keys = _FILES_OPTIONAL_KEYS if include_metadata else []
        full_payload = self._build_files_payload(
            items,
            path=normalized_path,
            pattern=normalized_pattern,
            format=format,
            include_metadata=include_metadata,
            truncated=False,
        )
        full_total_tokens = self._compute_total_tokens({**full_payload, "tokens_saved": 0})

        def build_payload(packed_items: list[dict[str, Any]]) -> dict[str, Any]:
            return self._finalize_packed_payload(
                self._build_files_payload(
                    packed_items,
                    path=normalized_path,
                    pattern=normalized_pattern,
                    format=format,
                    include_metadata=include_metadata,
                    truncated=len(packed_items) < len(items),
                ),
                full_total_tokens=full_total_tokens,
            )

        packed = self._fit_items_to_budget(
            items,
            budget_tokens=budget_tokens,
            essential_keys=essential_keys,
            optional_keys_in_drop_order=optional_keys,
            build_payload=build_payload,
        )
        payload = self._maybe_attach_overflow_metadata(
            packed_payload=packed,
            full_payload=full_payload,
            full_total_tokens=full_total_tokens,
            budget_tokens=budget_tokens,
        )
        self._cache_set("code.files", cache_args, payload)
        return payload

    def tool_explore(
        self,
        query: str,
        *,
        seed_files: list[str] | None = None,
        max_files: int = 8,
        max_symbols: int = 30,
        include_source: bool = True,
        include_relationships: bool = True,
        line_numbers: bool = True,
        depth: int = 1,
        budget_tokens: int = 12000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()

        bounded_max_symbols = max(1, min(max_symbols, 30))
        bounded_max_files = max(1, min(max_files, 8))
        bounded_depth = max(1, depth)
        normalized_seeds = [self._normalize_file_arg(seed) for seed in seed_files or []]
        seed_set = set(normalized_seeds)
        cache_args = {
            "query": query,
            "seed_files": normalized_seeds,
            "max_files": bounded_max_files,
            "max_symbols": bounded_max_symbols,
            "include_source": include_source,
            "include_relationships": include_relationships,
            "line_numbers": line_numbers,
            "depth": bounded_depth,
            "budget_tokens": budget_tokens,
        }
        hit, cached = self._cache_get("code.explore", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)

        raw_symbols = self.search_symbols(
            query,
            limit=bounded_max_symbols,
            snippet="none",
            auto_index=False,
        )
        ranked_symbols = sorted(
            raw_symbols,
            key=lambda record: (
                0 if record.file_path in seed_set else 1,
                -(record.score or 0.0),
                record.file_path,
                record.start_line,
            ),
        )
        selected_symbols = ranked_symbols[:bounded_max_symbols]
        selected_files: list[str] = []
        by_file: dict[str, list[SymbolRecord]] = {}
        for symbol in selected_symbols:
            by_file.setdefault(symbol.file_path, []).append(symbol)
            if symbol.file_path not in selected_files:
                selected_files.append(symbol.file_path)
        selected_files = selected_files[:bounded_max_files]
        trimmed_symbols = [symbol for symbol in selected_symbols if symbol.file_path in set(selected_files)]
        trimmed_by_file: dict[str, list[SymbolRecord]] = {}
        for symbol in trimmed_symbols:
            trimmed_by_file.setdefault(symbol.file_path, []).append(symbol)

        entry_points = [
            {
                "symbol_id": symbol.symbol_id,
                "symbol_name": symbol.symbol_name,
                "qualified_name": symbol.qualified_name,
                "file_path": symbol.file_path,
                "kind": symbol.kind,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "score": symbol.score,
                "provenance": symbol.provenance,
            }
            for symbol in trimmed_symbols
        ]

        files_payload: list[dict[str, Any]] = []
        for file_path in selected_files:
            symbols = trimmed_by_file.get(file_path, [])
            file_entry: dict[str, Any] = {
                "file_path": file_path,
                "language": symbols[0].language if symbols else "unknown",
                "symbols": [
                    {
                        "symbol_id": symbol.symbol_id,
                        "symbol_name": symbol.symbol_name,
                        "qualified_name": symbol.qualified_name,
                        "kind": symbol.kind,
                        "start_line": symbol.start_line,
                        "end_line": symbol.end_line,
                        "provenance": symbol.provenance,
                    }
                    for symbol in symbols
                ],
            }
            if include_source:
                sections = [
                    self._source_section_for_symbol(symbol, line_numbers=line_numbers)
                    for symbol in symbols
                ]
                merged_sections = self._merge_nearby_source_sections(sections)
                file_entry["source_sections"] = merged_sections
            files_payload.append(file_entry)

        relationships: dict[str, list[dict[str, Any]]] = {"callers": [], "callees": [], "usages": []}
        if include_relationships:
            for symbol in trimmed_symbols[:3]:
                callers = self.tool_callers(
                    symbol_id=symbol.symbol_id,
                    depth=bounded_depth,
                    limit=20,
                    budget_tokens=max(600, budget_tokens // 6),
                    auto_index=False,
                )
                if "error" not in callers:
                    relationships["callers"].append(
                        {
                            "symbol_id": symbol.symbol_id,
                            "symbol_name": symbol.symbol_name,
                            "related": callers.get("related", []),
                            "edges": callers.get("edges", []),
                        }
                    )
                callees = self.tool_callees(
                    symbol_id=symbol.symbol_id,
                    depth=bounded_depth,
                    limit=20,
                    budget_tokens=max(600, budget_tokens // 6),
                    auto_index=False,
                )
                if "error" not in callees:
                    relationships["callees"].append(
                        {
                            "symbol_id": symbol.symbol_id,
                            "symbol_name": symbol.symbol_name,
                            "related": callees.get("related", []),
                            "edges": callees.get("edges", []),
                        }
                    )
                references = self.find_references(
                    symbol_id=symbol.symbol_id,
                    group_by="none",
                    snippet_lines=0,
                    limit=20,
                    auto_index=False,
                    budget_tokens=max(600, budget_tokens // 6),
                )
                if "error" not in references:
                    refs_payload = references.get("references", [])
                    if isinstance(refs_payload, list):
                        relationships["usages"].append(
                            {
                                "symbol_id": symbol.symbol_id,
                                "symbol_name": symbol.symbol_name,
                                "references": refs_payload,
                            }
                        )

        additional_relevant_files = [
            symbol.file_path
            for symbol in ranked_symbols
            if symbol.file_path not in set(selected_files)
        ][:20]
        full_payload = {
            "query": query,
            "repo_id": self.repo_id,
            "entry_points": entry_points,
            "files": files_payload,
            "relationships": relationships,
            "additional_relevant_files": additional_relevant_files,
            "truncated": len(selected_symbols) > len(trimmed_symbols),
            "cache_hit": False,
            "provenance": _LOCAL_PROVENANCE,
        }
        packed = self._pack_single_payload(
            full_payload,
            budget_tokens=budget_tokens,
            essential_keys=_EXPLORE_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=_EXPLORE_OPTIONAL_KEYS,
        )
        self._cache_set("code.explore", cache_args, packed)
        return packed

    def tool_routes(
        self,
        *,
        file_glob: str | None = None,
        language: str | None = None,
        limit: int = 200,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        normalized_language = language.lower().strip() if isinstance(language, str) and language.strip() else None
        cache_args = {
            "file_glob": file_glob,
            "language": normalized_language,
            "limit": limit,
            "budget_tokens": budget_tokens,
        }
        hit, cached = self._cache_get("code.routes", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)

        routes, source_truncated = self._indexed_route_records(
            file_glob=file_glob,
            language=normalized_language,
            limit=max(1, limit),
        )
        full_payload = self._build_routes_payload(
            routes,
            file_glob=file_glob,
            language=normalized_language,
            truncated=source_truncated,
        )
        full_total_tokens = self._compute_total_tokens({**full_payload, "tokens_saved": 0})

        def build_payload(packed_items: list[dict[str, Any]]) -> dict[str, Any]:
            return self._finalize_packed_payload(
                self._build_routes_payload(
                    packed_items,
                    file_glob=file_glob,
                    language=normalized_language,
                    truncated=source_truncated or len(packed_items) < len(routes),
                ),
                full_total_tokens=full_total_tokens,
            )

        packed = self._fit_items_to_budget(
            routes,
            budget_tokens=budget_tokens,
            essential_keys=_ROUTES_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=_ROUTES_OPTIONAL_KEYS,
            build_payload=build_payload,
        )
        payload = self._maybe_attach_overflow_metadata(
            packed_payload=packed,
            full_payload=full_payload,
            full_total_tokens=full_total_tokens,
            budget_tokens=budget_tokens,
        )
        self._cache_set("code.routes", cache_args, payload)
        return payload

    def tool_context(
        self,
        *,
        task: str,
        seed_files: list[str] | None = None,
        budget_tokens: int = 4000,
        max_symbols: int = 8,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        effective_budget_tokens = self._effective_budget_tokens("context", budget_tokens)
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        normalized_seeds = [self._normalize_file_arg(seed) for seed in seed_files or []]
        cache_args = {
            "task": task,
            "seed_files": normalized_seeds,
            "budget_tokens": effective_budget_tokens,
            "max_symbols": max_symbols,
        }
        hit, cached = self._cache_get("code.context", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)

        raw = self.context_pack(
            task=task,
            seed_files=normalized_seeds,
            budget_tokens=effective_budget_tokens,
            max_symbols=max_symbols,
            auto_index=False,
        )
        payload = self._pack_single_payload(
            raw.model_dump(mode="json"),
            budget_tokens=effective_budget_tokens,
            essential_keys=_CONTEXT_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=["telemetry", "import_neighbors", "repo_map"],
            base_tokens_saved=raw.tokens_saved_vs_full_files,
        )
        self._cache_set("code.context", cache_args, payload)
        return payload

    def tool_impact(
        self,
        file_path: str,
        *,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        normalized_file_path = self._normalize_file_arg(file_path)
        cache_args = {
            "file_path": normalized_file_path,
            "budget_tokens": budget_tokens,
            "file_mtime_ns": self._file_mtime_ns(normalized_file_path),
        }
        hit, cached = self._cache_get("code.impact", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)

        payload = self._pack_single_payload(
            self.impact(normalized_file_path, auto_index=False).model_dump(mode="json"),
            budget_tokens=budget_tokens,
            essential_keys=_IMPACT_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=["transitive_importers", "dead_code_candidates"],
        )
        self._cache_set("code.impact", cache_args, payload)
        return payload

    def tool_usages(
        self,
        query: str | None = None,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        kind: str | None = None,
        language: str | None = None,
        file_glob: str | None = None,
        group_by: Literal["file", "caller", "none"] = "file",
        snippet_lines: int = 3,
        limit: int = 20,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        effective_budget_tokens = self._effective_budget_tokens("relation", budget_tokens)
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        normalized_file_path = self._normalize_file_arg(file_path) if file_path else None
        cache_args = {
            "query": query,
            "symbol_id": symbol_id,
            "qualified_name": qualified_name,
            "symbol_name": symbol_name,
            "file_path": normalized_file_path,
            "kind": kind,
            "language": language,
            "file_glob": file_glob,
            "group_by": group_by,
            "snippet_lines": snippet_lines,
            "limit": limit,
            "budget_tokens": effective_budget_tokens,
        }
        hit, cached = self._cache_get("code.usages", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)
        payload = self.find_references(
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=normalized_file_path,
            kind=kind,
            language=language,
            file_glob=file_glob,
            group_by=group_by,
            snippet_lines=snippet_lines,
            limit=limit,
            auto_index=False,
            budget_tokens=effective_budget_tokens,
        )
        if "error" not in payload:
            self._cache_set("code.usages", cache_args, payload)
        return payload

    def tool_callers(
        self,
        query: str | None = None,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        kind: str | None = None,
        language: str | None = None,
        depth: int = 1,
        limit: int = 20,
        snapshot: bool = False,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        return self._tool_call_graph(
            "callers",
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=file_path,
            kind=kind,
            language=language,
            depth=depth,
            limit=limit,
            snapshot=snapshot,
            budget_tokens=budget_tokens,
            auto_index=auto_index,
        )

    def tool_callees(
        self,
        query: str | None = None,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        kind: str | None = None,
        language: str | None = None,
        depth: int = 1,
        limit: int = 20,
        snapshot: bool = False,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        return self._tool_call_graph(
            "callees",
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=file_path,
            kind=kind,
            language=language,
            depth=depth,
            limit=limit,
            snapshot=snapshot,
            budget_tokens=budget_tokens,
            auto_index=auto_index,
        )

    def tool_pattern(
        self,
        *,
        pattern: str,
        rewrite: str | None = None,
        language: str | None = None,
        file_glob: str | None = None,
        dry_run: bool = True,
        limit: int = 20,
        budget_tokens: int = 4000,
    ) -> dict[str, Any]:
        adapter = AstGrepAdapter(self.repo_root)
        if rewrite is None:
            native = self._native_python_pattern_search(
                pattern=pattern,
                language=language,
                file_glob=file_glob,
                limit=limit,
            )
            if native is not None:
                payload = self._pack_pattern_matches(native, budget_tokens=budget_tokens)
                self._cache_set(
                    "code.pattern",
                    {"pattern": pattern, "language": language, "file_glob": file_glob, "limit": limit, "budget_tokens": budget_tokens, "native": True},
                    payload,
                )
                return payload
            cache_args = {
                "pattern": pattern,
                "language": language,
                "file_glob": file_glob,
                "limit": limit,
                "budget_tokens": budget_tokens,
            }
            hit, cached = self._cache_get("code.pattern", cache_args)
            if hit and cached is not None:
                return self._mark_cache_hit(cached)
            try:
                result = adapter.search(pattern=pattern, language=language, file_glob=file_glob, limit=limit)
            except AstGrepToolUnavailable as exc:
                native_unavailable = self._native_python_pattern_search(
                    pattern=pattern,
                    language=language,
                    file_glob=file_glob,
                    limit=limit,
                )
                if native_unavailable is not None:
                    return self._pack_pattern_matches(native_unavailable, budget_tokens=budget_tokens)
                return exc.payload
            if len(result.matches) > limit:
                result = PatternSearchResult(
                    matches=result.matches[:limit],
                    truncated=True,
                    total_matches=result.total_matches if result.total_matches is not None else len(result.matches),
                )
            payload = self._pack_pattern_matches(
                result,
                budget_tokens=budget_tokens,
            )
            self._cache_set("code.pattern", cache_args, payload)
            return payload

        try:
            rewrite_result = adapter.rewrite(
                pattern=pattern,
                rewrite=rewrite,
                language=language,
                file_glob=file_glob,
                dry_run=dry_run,
            )
        except AstGrepToolUnavailable as exc:
            return exc.payload
        if not dry_run and rewrite_result.files_changed:
            self._reindex_files(rewrite_result.files_changed)
        return self._pack_pattern_rewrite(rewrite_result, budget_tokens=budget_tokens)

    def _native_python_pattern_search(
        self,
        *,
        pattern: str,
        language: str | None,
        file_glob: str | None,
        limit: int,
    ) -> PatternSearchResult | None:
        """Native Python structural search for common benchmark-critical patterns.

        ast-grep remains the advanced backend, but decorators/calls should not
        fail just because an external binary is unavailable.
        """
        normalized = pattern.strip()
        if language not in {None, "python", "py"}:
            return None
        decorator_name: str | None = None
        call_wildcard = False
        if normalized.startswith("@"):
            decorator_name = normalized[1:].split("(", 1)[0].strip()
        elif normalized in {"$F($$$ARGS)", "$F($$$)", "$F()"}:
            call_wildcard = True
        else:
            return None

        matches: list[PatternMatch] = []
        candidates = [path for path in self._indexed_files() if path.endswith(".py")]
        if file_glob:
            candidates = [path for path in candidates if fnmatch.fnmatch(path, file_glob)]
        for rel in candidates:
            if len(matches) >= limit:
                break
            source = self._read_file(rel)
            lines = source.splitlines()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            if decorator_name is not None:
                for node in ast.walk(tree):
                    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                        continue
                    for decorator in node.decorator_list:
                        name = self._python_call_name(decorator)
                        if name is None and isinstance(decorator, ast.Name):
                            name = decorator.id
                        if name != decorator_name and not (name or "").endswith(f".{decorator_name}"):
                            continue
                        line = int(getattr(decorator, "lineno", getattr(node, "lineno", 1)) or 1)
                        col = int(getattr(decorator, "col_offset", 0) or 0) + 1
                        snippet = lines[line - 1].strip() if 1 <= line <= len(lines) else ""
                        matches.append(
                            PatternMatch(
                                file_path=rel,
                                line=line,
                                column=col,
                                end_line=line,
                                end_column=col + len(snippet),
                                snippet=snippet,
                                captures={"decorator": name or decorator_name},
                            )
                        )
                        if len(matches) >= limit:
                            break
                    if len(matches) >= limit:
                        break
            elif call_wildcard:
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = self._python_call_name(node.func)
                    if not name:
                        continue
                    line = int(getattr(node, "lineno", 1) or 1)
                    col = int(getattr(node, "col_offset", 0) or 0) + 1
                    snippet = lines[line - 1].strip() if 1 <= line <= len(lines) else ""
                    matches.append(
                        PatternMatch(
                            file_path=rel,
                            line=line,
                            column=col,
                            end_line=line,
                            end_column=col + len(snippet),
                            snippet=snippet,
                            captures={"F": name},
                        )
                    )
                    if len(matches) >= limit:
                        break
        return PatternSearchResult(matches=matches, truncated=len(matches) >= limit, total_matches=len(matches))

    def tool_status(
        self,
        *,
        budget_tokens: int = 2000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        cache_args = {
            "budget_tokens": budget_tokens,
            "index_version": self._current_index_version(),
            "autosync_enabled": self._autosync_enabled,
            "autosync_debounce_ms": self._autosync_debounce_ms,
            "head_sha": self._safe_current_head_sha(),
        }
        hit, cached = self._cache_get("code.status", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)
        index_stats = self._index_snapshot()
        cache_stats = self._cache.stats(
            repo_id=self.repo_id,
            index_version=self._current_index_version(),
            tool_name=None,
        )
        stale_after_seconds = 86_400
        files_indexed = int(index_stats.get("files_indexed", 0) or 0)
        index_age_seconds = index_stats.get("index_age_seconds")
        if files_indexed <= 0:
            freshness_status = "empty"
            freshness_reason = "no indexed files"
        elif isinstance(index_age_seconds, int) and index_age_seconds > stale_after_seconds:
            freshness_status = "stale"
            freshness_reason = "index older than stale threshold"
        else:
            freshness_status = "fresh"
            freshness_reason = "index within freshness threshold"

        provider_thresholds = {
            "required_health_status": "ok",
            "require_index_head_match_for_scip": True,
        }
        head_sha = self._safe_current_head_sha()
        warnings: list[dict[str, Any]] = []
        provider_counts = {"ok": 0, "degraded": 0, "unhealthy": 0}
        providers: list[dict[str, Any]] = []
        for provider in self.intel_store.providers:
            provider_name = str(getattr(provider, "name", provider.__class__.__name__.lower()))
            entry: dict[str, Any] = {"name": provider_name}
            try:
                health = provider.health()
            except Exception as exc:
                health = ProviderHealth(status="unhealthy", reason=str(exc))
            if isinstance(health, ProviderHealth):
                entry["status"] = health.status
                entry["ok"] = health.ok
                if health.reason:
                    entry["reason"] = str(health.reason)
            else:
                ok = bool(health)
                entry["status"] = "ok" if ok else "unhealthy"
                entry["ok"] = ok
            provider_status = str(entry.get("status") or "unhealthy")
            if provider_status in provider_counts:
                provider_counts[provider_status] += 1
            if provider_status != "ok":
                warnings.append(
                    {
                        "code": "provider_health_not_ok",
                        "level": "warning",
                        "provider": provider_name,
                        "message": f"provider '{provider_name}' health is {provider_status}",
                    }
                )
            index_sha_fn = getattr(provider, "index_sha", None)
            if callable(index_sha_fn):
                with contextlib.suppress(Exception):
                    index_sha = index_sha_fn()
                    if index_sha:
                        entry["index_sha"] = str(index_sha)
            if provider_name == "scip":
                if head_sha is not None:
                    entry["head_sha"] = head_sha
                index_sha = entry.get("index_sha")
                if isinstance(index_sha, str) and head_sha:
                    if index_sha == head_sha:
                        entry["freshness"] = "fresh"
                    else:
                        entry["freshness"] = "stale"
                        warnings.append(
                            {
                                "code": "provider_index_stale",
                                "level": "warning",
                                "provider": provider_name,
                                "message": "SCIP index SHA does not match HEAD; reindex recommended.",
                            }
                        )
                else:
                    entry["freshness"] = "unknown"
            else:
                entry["freshness"] = "unknown"
            providers.append(entry)

        payload = {
            "repo_id": self.repo_id,
            "repo_root": str(self.repo_root),
            "db_path": str(self.db_path),
            "index_version": self._current_index_version(),
            "index": index_stats,
            "cache": cache_stats,
            "providers": providers,
            "provider_freshness": {
                "thresholds": provider_thresholds,
                "summary": {
                    **provider_counts,
                    "total": sum(provider_counts.values()),
                },
            },
            "warnings": warnings,
            "freshness": {
                "status": freshness_status,
                "reason": freshness_reason,
                "indexed": files_indexed > 0,
                "last_indexed_at": index_stats.get("last_indexed_at"),
                "index_age_seconds": index_age_seconds,
                "stale_after_seconds": stale_after_seconds,
            },
            "autosync": self._autosync_status(),
            "provenance": _LOCAL_PROVENANCE,
        }
        payload = cast(dict[str, Any], self._json_safe(payload))
        packed = self._pack_single_payload(
            payload,
            budget_tokens=budget_tokens,
            essential_keys=_STATUS_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=[],
        )
        self._cache_set("code.status", cache_args, packed)
        return packed

    def tool_cache_status(
        self,
        *,
        cache_tool: str | None = None,
        budget_tokens: int = 4000,
    ) -> dict[str, Any]:
        tool_name = self._normalize_cache_tool(cache_tool)
        payload = {
            "repo_id": self.repo_id,
            "index_version": self._current_index_version(),
            **self._cache.stats(
                repo_id=self.repo_id,
                index_version=self._current_index_version(),
                tool_name=tool_name,
            ),
            "scope": {
                "cache_tool": cache_tool or "all",
                "tool_name": tool_name,
                "frozen_drop_stages": list(FROZEN_DROP_STAGES),
            },
            "provenance": _LOCAL_PROVENANCE,
        }
        return self._pack_single_payload(
            payload,
            budget_tokens=budget_tokens,
            essential_keys=_CACHE_STATUS_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=["last_hit_at", "scope"],
        )

    def tool_cache_invalidate(
        self,
        *,
        cache_tool: str | None = None,
        budget_tokens: int = 4000,
    ) -> dict[str, Any]:
        tool_name = self._normalize_cache_tool(cache_tool)
        index_version = self._current_index_version()
        invalidated = self._cache.invalidate(repo_id=self.repo_id, index_version=index_version, tool_name=tool_name)
        return self._pack_single_payload(
            {
                "repo_id": self.repo_id,
                "index_version": index_version,
                **invalidated,
                "scope": {"cache_tool": cache_tool or "all", "tool_name": tool_name},
                "provenance": _LOCAL_PROVENANCE,
            },
            budget_tokens=budget_tokens,
            essential_keys=_CACHE_INVALIDATE_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=[],
        )

    @overload
    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: SearchMode = "auto",
        kind: str | None = None,
        language: str | None = None,
        snippet: Literal["none", "head", "full"] = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        scope: Literal["repo", "external"] = "repo",
        since: str | None = None,
        touched_by: str | None = None,
        auto_index: bool = True,
    ) -> list[SymbolRecord]: ...

    @overload
    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: SearchMode = "auto",
        kind: str | None = None,
        language: str | None = None,
        snippet: Literal["none", "head", "full"] = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        scope: Literal["deleted"],
        since: str | None = None,
        touched_by: str | None = None,
        auto_index: bool = True,
    ) -> list[DeletedHistoryItem]: ...

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: SearchMode = "auto",
        kind: str | None = None,
        language: str | None = None,
        snippet: Literal["none", "head", "full"] = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        scope: Literal["repo", "external", "deleted"] = "repo",
        since: str | None = None,
        touched_by: str | None = None,
        auto_index: bool = True,
    ) -> list[SymbolRecord] | list[DeletedHistoryItem]:
        """BM25/FTS-ranked symbol search with routed-provider fallback."""
        if auto_index and scope != "deleted":
            self._ensure_indexed()
        if scope == "deleted":
            return self._deleted_history_adapter().search(
                query,
                limit=limit,
                since_ts=_parse_since_filter(since),
                touched_by=_normalize_touched_by(touched_by),
                language=language,
            )
        resolved_mode = resolve_search_mode(query, mode)
        if resolved_mode == "lexical":
            hits = self.intel_store.search_symbols(query, limit=limit, kind=kind, language=language, scope=scope)
        else:
            candidate_limit = semantic_candidate_limit(limit)
            if scope == "external":
                hits = self.intel_store.search_symbols(
                    query,
                    limit=candidate_limit,
                    kind=kind,
                    language=language,
                    scope="external",
                )
            else:
                lexical_hits = self.intel_store.search_symbols(
                    query,
                    limit=candidate_limit,
                    kind=kind,
                    language=language,
                    scope="repo",
                )
                semantic_hits = self._search_symbols_semantic_local(
                    query,
                    limit=candidate_limit,
                    kind=kind,
                    language=language,
                )
                hits = (
                    semantic_hits[:limit]
                    if resolved_mode == "semantic"
                    else self._semantic_ranker.reciprocal_rank_fuse(lexical_hits, semantic_hits, limit=limit)
                )
        if file_glob:
            hits = [hit for hit in hits if fnmatch.fnmatch(hit.file_path, file_glob)]
        return [self._attach_snippet(symbol, snippet=snippet, snippet_lines=snippet_lines) for symbol in hits[:limit]]

    def _search_symbols_local(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
    ) -> list[SymbolRecord]:
        fts_query = _safe_fts_query(query)
        if not fts_query:
            return []
        filters: list[str] = []
        params: list[Any] = [fts_query, self.repo_id]
        if kind:
            filters.append("s.kind = ?")
            params.append(kind)
        if language:
            filters.append("s.language = ?")
            params.append(language)
        where_extra = f" AND {' AND '.join(filters)}" if filters else ""
        params.extend([query.lower(), query.lower(), limit])
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                f"""
                SELECT s.*, 1.0 / (1.0 + abs(bm25(symbol_fts))) AS score
                FROM symbol_fts
                JOIN symbols s ON s.symbol_id = symbol_fts.symbol_id
                WHERE symbol_fts MATCH ? AND s.repo_id = ?{where_extra}
                ORDER BY
                    CASE
                        WHEN lower(s.symbol_name) = ? THEN 0
                        WHEN lower(s.qualified_name) = ? THEN 1
                        ELSE 2
                    END,
                    bm25(symbol_fts), s.file_path, s.start_line
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        emit_product_local(
            "code_context_retrieved",
            repo_id=self.repo_id,
            operation="search",
            result_count=len(rows),
        )
        return [_row_to_symbol(row) for row in rows]

    def _search_symbols_semantic_local(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
    ) -> list[SymbolRecord]:
        candidates = self._semantic_symbol_candidates(limit=limit, kind=kind, language=language)
        return self._semantic_ranker.semantic_search(
            query,
            candidates=candidates,
            limit=limit,
            source_loader=lambda symbol: self._read_file_slice(symbol.file_path, symbol.start_byte, symbol.end_byte),
        )

    def _semantic_symbol_candidates(
        self,
        *,
        limit: int,
        kind: str | None = None,
        language: str | None = None,
    ) -> list[SymbolRecord]:
        filters = ["repo_id = ?"]
        params: list[Any] = [self.repo_id]
        if kind:
            filters.append("kind = ?")
            params.append(kind)
        if language:
            filters.append("language = ?")
            params.append(language)
        params.append(limit)
        where_sql = " AND ".join(filters)
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                f"""
                SELECT *, NULL AS score
                FROM symbols
                WHERE {where_sql}
                ORDER BY file_path, start_line
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_row_to_symbol(row) for row in rows]

    def get_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        """Retrieve exact symbol metadata and source by byte offsets."""
        if auto_index:
            self._ensure_indexed()
        return self.intel_store.get_symbol(
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            file_path=file_path,
            symbol_name=symbol_name,
        )

    def _get_symbol_local(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["repo_id = ?"]
        params: list[Any] = [self.repo_id]
        if symbol_id:
            clauses.append("symbol_id = ?")
            params.append(symbol_id)
        if qualified_name:
            clauses.append("qualified_name = ?")
            params.append(qualified_name)
        if symbol_name:
            clauses.append("symbol_name = ?")
            params.append(symbol_name)
        if file_path:
            clauses.append("file_path = ?")
            params.append(self._normalize_file_arg(file_path))
        if len(clauses) == 1:
            raise ValueError("symbol_id, qualified_name, symbol_name, or file_path is required")
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute(
                f"SELECT *, NULL AS score FROM symbols WHERE {' AND '.join(clauses)} ORDER BY file_path, start_line LIMIT 1",
                tuple(params),
            ).fetchone()
        if row is None:
            raise LookupError("symbol not found")
        symbol = _row_to_symbol(row)
        path = self.repo_root / symbol.file_path
        source = path.read_bytes()[symbol.start_byte : symbol.end_byte].decode("utf-8", errors="replace")
        emit_product_local("code_symbol_retrieved", repo_id=self.repo_id, kind=symbol.kind)
        return {**symbol.model_dump(mode="json"), "source": source}

    def file_outline(
        self,
        *,
        file_path: str | None = None,
        limit: int = 200,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        """Return compact file or repository symbol outlines."""
        if auto_index:
            self._ensure_indexed()
        params: list[Any] = [self.repo_id]
        where = "repo_id = ?"
        if file_path:
            where += " AND file_path = ?"
            params.append(self._normalize_file_arg(file_path))
        params.append(limit)
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                f"""
                SELECT *, NULL AS score FROM symbols
                WHERE {where}
                ORDER BY file_path, start_line
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            record = _row_to_symbol(row)
            grouped.setdefault(record.file_path, []).append(
                {
                    "name": record.symbol_name,
                    "qualified_name": record.qualified_name,
                    "kind": record.kind,
                    "signature": record.signature,
                    "line_start": record.start_line,
                    "line_end": record.end_line,
                }
            )
        return {"repo_id": self.repo_id, "files": grouped, "symbol_count": len(rows)}

    def repo_map(self, *, seed_files: list[str] | None = None, budget_tokens: int = 2000) -> dict[str, Any]:
        """Build an Aider-style PageRank repo map with a token budget."""
        normalized = [self._normalize_file_arg(seed) for seed in seed_files or []]
        result = build_repo_map(self.repo_root, seed_files=normalized, budget_tokens=budget_tokens)
        return result.model_dump(mode="json") | {"mode": "map"}

    def context_pack(
        self,
        *,
        task: str,
        seed_files: list[str] | None = None,
        budget_tokens: int = 4000,
        max_symbols: int = 8,
        auto_index: bool = True,
    ) -> ContextPack:
        """Build a task-specific context bundle from BM25 symbols, repo map, imports, and exact source."""
        if auto_index:
            self._ensure_indexed()
        context_policy = resolve_output_policy("context")
        normalized_seeds = [self._normalize_file_arg(seed) for seed in seed_files or []]
        repo_map_payload = self.repo_map(seed_files=normalized_seeds, budget_tokens=max(200, budget_tokens // 4))
        bounded_max_symbols = max(1, min(max_symbols, context_policy.max_related_symbols))
        symbol_hits = self.search_symbols(task, limit=bounded_max_symbols, auto_index=False)
        seed_symbols = self._symbols_for_files(normalized_seeds, limit=bounded_max_symbols)
        selected = self._dedupe_symbols([*seed_symbols, *symbol_hits])
        selected = [symbol for symbol in selected if symbol.kind not in {"import", "export"}]
        selected = self._cap_symbols_per_file(selected, max_per_file=max(1, context_policy.max_symbols_per_file))
        selected = selected[:bounded_max_symbols]
        neighbors = self._import_neighbors(normalized_seeds)[: context_policy.max_related_symbols]

        lines = ["# Atelier code context", f"task: {task}", ""]
        if repo_map_payload.get("outline"):
            lines.extend(["## repo_map", str(repo_map_payload["outline"]), ""])
        if neighbors and context_policy.include_edges:
            lines.extend(["## import_neighbors", *[f"- {item}" for item in neighbors[:20]], ""])

        packed_symbols: list[SymbolRecord] = []
        naive_tokens = 0
        max_code_blocks = max(1, context_policy.max_code_blocks)
        for symbol in selected[:max_code_blocks]:
            full_file = self._read_file(symbol.file_path)
            naive_tokens += count_tokens(full_file)
            symbol_payload = self.get_symbol(symbol_id=symbol.symbol_id, auto_index=False)
            source_block = hard_cap_chars(str(symbol_payload["source"]), context_policy.max_code_block_chars)
            block = (
                f"## symbol {symbol.qualified_name} ({symbol.file_path}:{symbol.start_line}-{symbol.end_line})\n"
                f"```{symbol.language}\n{source_block}\n```"
            )
            candidate = "\n".join([*lines, block])
            if count_tokens(candidate) > budget_tokens and packed_symbols:
                break
            lines.append(block)
            lines.append("")
            packed_symbols.append(symbol)

        content = "\n".join(lines).strip()
        token_count = count_tokens(content)
        tokens_saved = max(0, naive_tokens - token_count)
        emit_product_local(
            "code_context_retrieved",
            repo_id=self.repo_id,
            operation="context_pack",
            result_count=len(packed_symbols),
        )
        return ContextPack(
            task=task,
            budget_tokens=budget_tokens,
            token_count=token_count,
            tokens_saved_vs_full_files=tokens_saved,
            symbols=packed_symbols,
            repo_map=str(repo_map_payload.get("outline", "")),
            import_neighbors=neighbors,
            content=content,
            telemetry={
                "repo_id": self.repo_id,
                "selected_symbols": len(packed_symbols),
                "token_budget_fit": token_count <= budget_tokens,
            },
        )

    def search_text(
        self,
        query: str,
        *,
        path: str = ".",
        limit: int = 50,
        ignore_case: bool = False,
    ) -> list[TextMatch]:
        """Literal text search using ripgrep when available, with a Python fallback."""
        search_path = self._resolve_inside_repo(path)
        if shutil.which("rg") is not None:
            args = [
                "rg",
                "--fixed-strings",
                "--line-number",
                "--column",
                "--no-heading",
                "--color",
                "never",
                "--max-count",
                str(limit),
            ]
            if ignore_case:
                args.append("--ignore-case")
            args.extend([query, str(search_path)])
            proc = subprocess.run(args, check=False, capture_output=True, text=True)
            if proc.returncode not in {0, 1}:
                raise RuntimeError(proc.stderr.strip() or "ripgrep failed")
            return self._parse_rg_output(proc.stdout, limit=limit)
        return self._python_text_search(query, search_path, limit=limit, ignore_case=ignore_case)

    def find_references(
        self,
        query: str | None = None,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        kind: str | None = None,
        language: str | None = None,
        file_glob: str | None = None,
        group_by: Literal["file", "caller", "none"] = "file",
        snippet_lines: int = 3,
        limit: int = 20,
        auto_index: bool = True,
        budget_tokens: int = 4000,
    ) -> dict[str, Any]:
        effective_budget_tokens = self._effective_budget_tokens("relation", budget_tokens)
        if auto_index:
            self._ensure_indexed()
        target = self._resolve_symbol_target(
            operation_name="usages",
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=file_path,
            kind=kind,
            language=language,
            file_glob=file_glob,
        )
        if target.get("error"):
            return self._pack_single_payload(
                target,
                budget_tokens=effective_budget_tokens,
                essential_keys=["error", "message", "matches", "cache_hit", "provenance"],
                optional_keys_in_drop_order=["provenance_breakdown"],
            )
        references = self.intel_store.find_references(
            symbol_id=str(target["symbol_id"]),
            qualified_name=str(target["qualified_name"]),
            file_path=str(target["file_path"]),
            symbol_name=str(target["symbol_name"]),
        )
        cross_lang_refs = self._cross_lang_usage_references(target)
        ordered_references = [
            *references,
            *sorted(cross_lang_refs, key=lambda item: (item.file_path, item.line, item.column, item.provenance)),
        ]
        items = [self._usage_item(reference, snippet_lines=snippet_lines) for reference in ordered_references]
        if file_glob:
            items = [item for item in items if fnmatch.fnmatch(str(item["file_path"]), file_glob)]
        relation_policy = resolve_output_policy("relation")
        if not relation_policy.include_snippet:
            for item in items:
                item.pop("snippet", None)
        truncated_by_policy = False
        if relation_policy.max_related_symbols > 0 and len(items) > relation_policy.max_related_symbols:
            items = items[: relation_policy.max_related_symbols]
            truncated_by_policy = True
        full_payload = self._build_usages_payload(
            target=target,
            items=items,
            group_by=group_by,
            truncated=truncated_by_policy,
        )
        full_total_tokens = self._compute_total_tokens({**full_payload, "tokens_saved": 0})

        def build_payload(packed_items: list[dict[str, Any]]) -> dict[str, Any]:
            return self._finalize_packed_payload(
                self._build_usages_payload(
                    target=target,
                    items=packed_items,
                    group_by=group_by,
                    truncated=truncated_by_policy or len(packed_items) < len(items),
                ),
                full_total_tokens=full_total_tokens,
            )

        return self._fit_items_to_budget(
            items[:limit],
            budget_tokens=effective_budget_tokens,
            essential_keys=_USAGES_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=_USAGES_OPTIONAL_KEYS,
            build_payload=build_payload,
        )

    def _cross_lang_usage_references(self, target: dict[str, Any]) -> list[UsageReference]:
        symbol_id = str(target.get("symbol_id") or "")
        symbol_name = str(target.get("symbol_name") or "")
        if not symbol_id:
            return []
        refs: list[UsageReference] = []
        for edge in self._cross_lang_store().query_by_target_symbol(
            tgt_symbol_id=symbol_id, tgt_symbol_name=symbol_name
        ):
            refs.append(
                UsageReference(
                    file_path=edge.src_file_path,
                    line=edge.src_line,
                    column=1,
                    end_line=edge.src_line,
                    end_column=1,
                    caller=edge.src_qualified_name,
                    provenance="cross_lang",
                    edge_kind=edge.edge_kind,
                    confidence=edge.confidence,
                )
            )
        return refs

    def _tool_call_graph(
        self,
        direction: CallGraphDirection,
        *,
        query: str | None = None,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        kind: str | None = None,
        language: str | None = None,
        depth: int = 1,
        limit: int = 20,
        snapshot: bool = False,
        budget_tokens: int = 4000,
        auto_index: bool = True,
    ) -> dict[str, Any]:
        effective_budget_tokens = self._effective_budget_tokens("relation", budget_tokens)
        if auto_index:
            self._ensure_indexed()
        self._sync_symbol_intel()
        normalized_file_path = self._normalize_file_arg(file_path) if file_path else None
        bounded_depth = max(1, depth)
        cache_args = {
            "direction": direction,
            "query": query,
            "symbol_id": symbol_id,
            "qualified_name": qualified_name,
            "symbol_name": symbol_name,
            "file_path": normalized_file_path,
            "kind": kind,
            "language": language,
            "depth": bounded_depth,
            "limit": limit,
            "snapshot": snapshot,
            "budget_tokens": effective_budget_tokens,
        }
        hit, cached = self._cache_get(f"code.{direction}", cache_args)
        if hit and cached is not None:
            return self._mark_cache_hit(cached)
        target = self._resolve_symbol_target(
            operation_name=direction,
            query=query,
            symbol_id=symbol_id,
            qualified_name=qualified_name,
            symbol_name=symbol_name,
            file_path=normalized_file_path,
            kind=kind,
            language=language,
            file_glob=None,
        )
        if target.get("error"):
            return self._pack_single_payload(
                target,
                budget_tokens=effective_budget_tokens,
                essential_keys=["error", "message", "matches", "cache_hit", "provenance"],
                optional_keys_in_drop_order=["provenance_breakdown"],
            )
        lookup = self.intel_store.find_callers if direction == "callers" else self.intel_store.find_callees
        traversal = traverse_call_graph(
            target,
            direction=direction,
            depth=bounded_depth,
            limit=limit,
            snapshot=snapshot,
            lookup_neighbors=lambda current_symbol_id: lookup(symbol_id=current_symbol_id),
        )
        if traversal.data_status == "unavailable" and direction == "callers":
            fallback = self._fallback_callers_from_references(
                target=target,
                limit=limit,
            )
            if fallback.data_status == "available":
                traversal = fallback
        payload = build_call_graph_payload(
            target,
            direction=direction,
            depth=bounded_depth,
            result=traversal,
        )
        relation_policy = resolve_output_policy("relation")
        if relation_policy.max_related_symbols > 0:
            max_related = relation_policy.max_related_symbols
            related_before = len(cast(list[dict[str, Any]], payload.get("related", [])))
            edges_before = len(cast(list[dict[str, Any]], payload.get("edges", [])))
            payload["related"] = cast(list[dict[str, Any]], payload.get("related", []))[:max_related]
            payload["edges"] = cast(list[dict[str, Any]], payload.get("edges", []))[:max_related]
            payload["related_count"] = len(cast(list[dict[str, Any]], payload.get("related", [])))
            payload["edge_count"] = len(cast(list[dict[str, Any]], payload.get("edges", [])))
            payload["truncated"] = bool(payload.get("truncated", False)) or related_before > max_related or edges_before > max_related
        if not relation_policy.include_edges:
            payload["edges"] = []
            payload["edge_count"] = 0
        payload["provenance"] = str(target.get("provenance") or _LOCAL_PROVENANCE)
        packed = self._pack_single_payload(
            payload,
            budget_tokens=effective_budget_tokens,
            essential_keys=_CALL_GRAPH_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=_CALL_GRAPH_OPTIONAL_KEYS,
        )
        if "error" not in packed:
            self._cache_set(f"code.{direction}", cache_args, packed)
        return packed

    def impact(self, file_path: str, *, auto_index: bool = True) -> ImpactResult:
        """Approximate import blast radius and dead-code candidates for a file."""
        if auto_index:
            self._ensure_indexed()
        rel = self._normalize_file_arg(file_path)
        direct = self._direct_importers(rel)
        transitive: list[str] = []
        seen = set(direct)
        frontier = set(direct)
        for _ in range(3):
            next_frontier: set[str] = set()
            for item in sorted(frontier):
                for importer in self._direct_importers(item):
                    if importer not in seen:
                        seen.add(importer)
                        next_frontier.add(importer)
                        transitive.append(importer)
            frontier = next_frontier
            if not frontier:
                break
        affected_tests = sorted(item for item in seen if "/test" in item or Path(item).name.startswith("test_"))
        total = len(direct) + len(transitive)
        risk: Literal["low", "medium", "high", "critical"]
        if total == 0:
            risk = "low"
        elif total <= 3:
            risk = "medium"
        elif total <= 10:
            risk = "high"
        else:
            risk = "critical"
        return ImpactResult(
            file_path=rel,
            direct_importers=direct,
            transitive_importers=transitive,
            affected_tests=affected_tests,
            risk_level=risk,
            dead_code_candidates=self._dead_code_candidates(rel),
            provenance=_LOCAL_PROVENANCE,
        )

    def changed_symbols(self, *, base_ref: str = "HEAD") -> list[SymbolRecord]:
        """Return indexed symbols whose files changed relative to a git ref."""
        repo_class = _git_repo_class()
        if repo_class is None:
            return []
        with contextlib.suppress(Exception):
            repo = repo_class(self.repo_root, search_parent_directories=True)
            changed = {item.a_path for item in repo.index.diff(base_ref)} | {
                item.a_path for item in repo.index.diff(None)
            }
            return self._symbols_for_files(sorted(item for item in changed if item), limit=500)
        return []

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = self._connect()
        self._init_schema(conn)
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS engine_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                repo_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                language TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                indexed_at TEXT NOT NULL,
                PRIMARY KEY (repo_id, file_path)
            );
            CREATE TABLE IF NOT EXISTS symbols (
                symbol_id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                language TEXT NOT NULL,
                symbol_name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                signature TEXT NOT NULL,
                start_byte INTEGER NOT NULL,
                end_byte INTEGER NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                parent_symbol TEXT,
                doc_summary TEXT,
                content_hash TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5(
                symbol_id UNINDEXED,
                name,
                qualified_name,
                signature,
                file_path UNINDEXED,
                source
            );
            CREATE TABLE IF NOT EXISTS imports (
                repo_id TEXT NOT NULL,
                source_file TEXT NOT NULL,
                raw_import TEXT NOT NULL,
                target_file TEXT,
                UNIQUE(repo_id, source_file, raw_import, target_file)
            );
            CREATE TABLE IF NOT EXISTS "references" (
                repo_id TEXT NOT NULL,
                symbol_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line INTEGER NOT NULL,
                column INTEGER NOT NULL,
                end_column INTEGER NOT NULL,
                enclosing_symbol_name TEXT,
                enclosing_qualified_name TEXT,
                snippet TEXT NOT NULL,
                UNIQUE(repo_id, symbol_name, file_path, line, column, enclosing_qualified_name)
            );
            CREATE TABLE IF NOT EXISTS call_edges (
                repo_id TEXT NOT NULL,
                caller_symbol_name TEXT NOT NULL,
                caller_qualified_name TEXT NOT NULL,
                caller_file_path TEXT NOT NULL,
                caller_start_line INTEGER NOT NULL,
                caller_end_line INTEGER NOT NULL,
                callee_name TEXT NOT NULL,
                call_line INTEGER NOT NULL,
                call_column INTEGER NOT NULL,
                snippet TEXT NOT NULL,
                UNIQUE(repo_id, caller_qualified_name, caller_file_path, call_line, call_column, callee_name)
            );
            CREATE INDEX IF NOT EXISTS idx_symbols_repo_file ON symbols(repo_id, file_path);
            CREATE INDEX IF NOT EXISTS idx_symbols_repo_name ON symbols(repo_id, symbol_name);
            CREATE INDEX IF NOT EXISTS idx_imports_target ON imports(repo_id, target_file);
            CREATE INDEX IF NOT EXISTS idx_references_name ON "references"(repo_id, symbol_name);
            CREATE INDEX IF NOT EXISTS idx_references_file ON "references"(repo_id, file_path);
            CREATE INDEX IF NOT EXISTS idx_call_edges_callee ON call_edges(repo_id, callee_name);
            CREATE INDEX IF NOT EXISTS idx_call_edges_caller ON call_edges(repo_id, caller_file_path, caller_start_line);
            """)
        conn.execute("INSERT OR IGNORE INTO engine_state(key, value) VALUES ('index_version', '0')")

    def _insert_symbol(
        self,
        conn: sqlite3.Connection,
        rel: str,
        language: str,
        content_hash: str,
        symbol: _ExtractedSymbol,
    ) -> None:
        raw_id = f"{self.repo_id}:{rel}:{symbol.qualified_name}:{symbol.start_byte}:{content_hash}"
        symbol_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
        conn.execute(
            """
            INSERT INTO symbols(
                symbol_id, repo_id, file_path, language, symbol_name, qualified_name, kind,
                signature, start_byte, end_byte, start_line, end_line, parent_symbol,
                doc_summary, content_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol_id,
                self.repo_id,
                rel,
                language,
                symbol.name,
                symbol.qualified_name,
                symbol.kind,
                symbol.signature,
                symbol.start_byte,
                symbol.end_byte,
                symbol.start_line,
                symbol.end_line,
                symbol.parent_symbol,
                symbol.doc_summary,
                content_hash,
            ),
        )
        source = self._read_file_slice(rel, symbol.start_byte, symbol.end_byte)
        conn.execute(
            "INSERT INTO symbol_fts(symbol_id, name, qualified_name, signature, file_path, source) VALUES (?, ?, ?, ?, ?, ?)",
            (symbol_id, symbol.name, symbol.qualified_name, symbol.signature, rel, source[:20_000]),
        )

    def _ensure_indexed(self) -> None:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute("SELECT COUNT(*) AS n FROM files WHERE repo_id = ?", (self.repo_id,)).fetchone()
            count = int(row["n"]) if row is not None else 0
        if count == 0:
            self.index_repo()
            if self._autosync_enabled:
                self._autosync_signature = self._source_tree_signature()
                self._autosync_last_sync_ms = int(time.time() * 1000)
                self._autosync_state = "idle"
                self._autosync_pending_events = 0
                self._record_autosync_event(event="initial_index", reason="empty_index_bootstrap", reindexed=True)
            return
        if self._autosync_enabled:
            self._maybe_autosync_reindex()

    def _excluded(self, path: Path, patterns: list[str]) -> bool:
        rel = _safe_relpath(self.repo_root, path)
        return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)

    def _normalize_file_arg(self, value: str) -> str:
        path = Path(value)
        if path.is_absolute():
            return _safe_relpath(self.repo_root, path)
        return str(path)

    def _resolve_inside_repo(self, value: str) -> Path:
        path = Path(value)
        resolved = path.resolve() if path.is_absolute() else (self.repo_root / path).resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError(f"path escape denied: {value}") from exc
        return resolved

    def _extract_symbols(
        self, path: Path, rel: str, language: str, source: str, content_hash: str
    ) -> list[_ExtractedSymbol]:
        del rel, content_hash
        if language == "python":
            return self._extract_python_symbols(source)
        return self._extract_tag_symbols(path, source, language)

    def _extract_python_symbols(self, source: str) -> list[_ExtractedSymbol]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        offsets = _line_offsets(source)
        lines = source.splitlines()
        symbols: list[_ExtractedSymbol] = []

        def line_text(line_no: int) -> str:
            if 1 <= line_no <= len(lines):
                return lines[line_no - 1].strip()
            return ""

        def add_node(node: ast.AST, name: str, kind: str, parent: str | None) -> None:
            start_line = int(getattr(node, "lineno", 1))
            end_line = int(getattr(node, "end_lineno", start_line))
            col = int(getattr(node, "col_offset", 0))
            end_col = int(getattr(node, "end_col_offset", 0))
            start_byte = offsets[max(0, start_line - 1)] + col
            end_byte = offsets[max(0, end_line - 1)] + end_col if end_col else offsets[min(end_line, len(offsets) - 1)]
            qualified = f"{parent}.{name}" if parent else name
            doc = (
                ast.get_docstring(node)
                if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                else None
            )
            symbols.append(
                _ExtractedSymbol(
                    name=name,
                    qualified_name=qualified,
                    kind=kind,
                    signature=line_text(start_line),
                    start_byte=start_byte,
                    end_byte=max(start_byte, end_byte),
                    start_line=start_line,
                    end_line=end_line,
                    parent_symbol=parent,
                    doc_summary=doc.strip().splitlines()[0][:200] if doc else None,
                )
            )

        def walk_body(body: list[ast.stmt], parent: str | None = None) -> None:
            for node in body:
                if isinstance(node, ast.ClassDef):
                    add_node(node, node.name, "class", parent)
                    walk_body(node.body, node.name if parent is None else f"{parent}.{node.name}")
                elif isinstance(node, ast.AsyncFunctionDef):
                    add_node(node, node.name, "method" if parent else "async_function", parent)
                elif isinstance(node, ast.FunctionDef):
                    add_node(node, node.name, "method" if parent else "function", parent)
                elif parent is None and isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            add_node(node, target.id, "variable", None)
                elif parent is None and isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    add_node(node, node.target.id, "variable", None)

        walk_body(tree.body)
        return sorted(symbols, key=lambda item: (item.start_line, item.qualified_name))

    def _extract_python_reference_index(
        self,
        rel: str,
        source: str,
        symbols: list[_ExtractedSymbol],
    ) -> tuple[list[_IndexedReference], list[_IndexedCallEdge]]:
        """Extract local references and call edges from Python AST during indexing."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return [], []

        lines = source.splitlines()
        references: list[_IndexedReference] = []
        call_edges: list[_IndexedCallEdge] = []
        seen_refs: set[tuple[str, int, int, str | None]] = set()
        seen_edges: set[tuple[str, int, int, str]] = set()

        def snippet_for(line: int) -> str:
            return lines[line - 1].strip() if 1 <= line <= len(lines) else ""

        def containing_symbol(line: int) -> _ExtractedSymbol | None:
            candidates = [
                symbol
                for symbol in symbols
                if symbol.start_line <= line <= symbol.end_line
                and symbol.kind in {"function", "async_function", "method", "class"}
            ]
            if not candidates:
                return None
            return sorted(candidates, key=lambda item: (item.end_line - item.start_line, -item.start_line))[0]

        def add_reference(name: str, node: ast.AST) -> None:
            line = int(getattr(node, "lineno", 0) or 0)
            if line <= 0:
                return
            column = int(getattr(node, "col_offset", 0) or 0) + 1
            end_column = int(getattr(node, "end_col_offset", column + len(name) - 1) or (column + len(name) - 1))
            enclosing = containing_symbol(line)
            key = (name, line, column, enclosing.qualified_name if enclosing else None)
            if key in seen_refs:
                return
            seen_refs.add(key)
            references.append(
                _IndexedReference(
                    file_path=rel,
                    symbol_name=name,
                    line=line,
                    column=column,
                    end_column=max(column, end_column),
                    enclosing_symbol_name=enclosing.name if enclosing else None,
                    enclosing_qualified_name=enclosing.qualified_name if enclosing else None,
                    snippet=snippet_for(line),
                )
            )

        class Visitor(ast.NodeVisitor):
            def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
                if isinstance(node.ctx, ast.Load):
                    add_reference(node.id, node)
                self.generic_visit(node)

            def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
                add_reference(node.attr, node)
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
                callee = CodeContextEngine._python_call_name(node.func)
                caller = containing_symbol(int(getattr(node, "lineno", 0) or 0))
                if callee and caller is not None:
                    line = int(getattr(node, "lineno", caller.start_line) or caller.start_line)
                    column = int(getattr(node, "col_offset", 0) or 0) + 1
                    edge_key = (caller.qualified_name, line, column, callee)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        call_edges.append(
                            _IndexedCallEdge(
                                caller_symbol_name=caller.name,
                                caller_qualified_name=caller.qualified_name,
                                caller_file_path=rel,
                                caller_start_line=caller.start_line,
                                caller_end_line=caller.end_line,
                                callee_name=callee,
                                call_line=line,
                                call_column=column,
                                snippet=snippet_for(line),
                            )
                        )
                self.generic_visit(node)

        Visitor().visit(tree)
        return references, call_edges

    @staticmethod
    def _python_call_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = CodeContextEngine._python_call_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Call):
            return CodeContextEngine._python_call_name(node.func)
        return None

    def _extract_tag_symbols(self, path: Path, source: str, language: str) -> list[_ExtractedSymbol]:
        del language
        try:
            tags = [tag for tag in extract_tags(path) if tag.kind == "definition"]
        except (OSError, SyntaxError):
            return []
        offsets = _line_offsets(source)
        lines = source.splitlines()
        sorted_tags = sorted(tags, key=lambda tag: (tag.line, tag.name))
        symbols: list[_ExtractedSymbol] = []
        for index, tag in enumerate(sorted_tags):
            start_line = max(1, tag.line)
            next_line = sorted_tags[index + 1].line - 1 if index + 1 < len(sorted_tags) else start_line
            end_line = max(start_line, min(next_line, len(lines)))
            start_byte = offsets[start_line - 1] if start_line - 1 < len(offsets) else tag.byte_range[0]
            end_byte = offsets[end_line] if end_line < len(offsets) else tag.byte_range[1]
            signature = lines[start_line - 1].strip() if start_line <= len(lines) else tag.name
            symbols.append(
                _ExtractedSymbol(
                    name=tag.name,
                    qualified_name=tag.name,
                    kind=self._kind_from_signature(signature),
                    signature=signature,
                    start_byte=start_byte,
                    end_byte=max(start_byte, end_byte),
                    start_line=start_line,
                    end_line=end_line,
                )
            )
        return symbols

    def _extract_imports(self, path: Path, rel: str, language: str, source: str) -> list[tuple[str, str | None]]:
        imports: list[tuple[str, str | None]] = []
        if language == "python":
            imports.extend(self._python_imports(path, source))
        elif language in {"typescript", "javascript"}:
            imports.extend(self._javascript_imports(path, source))
        elif language == "rust":
            for match in _RUST_MOD_RE.finditer(source):
                raw = match.group(1)
                imports.append((raw, self._resolve_relative_module(path.parent, raw, [".rs"])))
        elif language == "go":
            for match in _GO_IMPORT_RE.finditer(source):
                raw_block = match.group(1) or match.group(2) or ""
                for raw in re.findall(r"\"([^\"]+)\"", raw_block) or [raw_block]:
                    imports.append((raw, None))
        return sorted(set((raw, target) for raw, target in imports if raw and target != rel))

    def _python_imports(self, path: Path, source: str) -> list[tuple[str, str | None]]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        imports: list[tuple[str, str | None]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append((alias.name, self._resolve_python_module(path.parent, alias.name)))
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append((node.module, self._resolve_python_module(path.parent, node.module)))
        return imports

    def _javascript_imports(self, path: Path, source: str) -> list[tuple[str, str | None]]:
        imports: list[tuple[str, str | None]] = []
        for match in _JS_IMPORT_RE.finditer(source):
            raw = next(group for group in match.groups() if group)
            target = None
            if raw.startswith("."):
                target = self._resolve_relative_module(path.parent, raw, [".ts", ".tsx", ".js", ".jsx"])
            imports.append((raw, target))
        return imports

    def _resolve_python_module(self, base: Path, module: str) -> str | None:
        parts = module.split(".")
        search_bases: list[Path] = []
        for candidate in [base, *base.parents, self.repo_root, self.repo_root / "src"]:
            resolved = candidate.resolve()
            if resolved not in search_bases:
                search_bases.append(resolved)
        for search_base in search_bases:
            candidate = search_base / Path(*parts).with_suffix(".py")
            if candidate.is_file():
                return _safe_relpath(self.repo_root, candidate)
            package = search_base / Path(*parts) / "__init__.py"
            if package.is_file():
                return _safe_relpath(self.repo_root, package)
            # src-layout imports often omit the top-level src directory while
            # file-local parent probing starts below it. Also handle package-root
            # candidates such as atelier.core.foo -> src/atelier/core/foo.py.
            src_candidate = self.repo_root / "src" / Path(*parts).with_suffix(".py")
            if src_candidate.is_file():
                return _safe_relpath(self.repo_root, src_candidate)
            src_package = self.repo_root / "src" / Path(*parts) / "__init__.py"
            if src_package.is_file():
                return _safe_relpath(self.repo_root, src_package)
        return None

    def _resolve_relative_module(self, base: Path, raw: str, suffixes: list[str]) -> str | None:
        candidate_base = (base / raw).resolve()
        candidates: list[Path] = []
        if candidate_base.suffix:
            candidates.append(candidate_base)
        else:
            candidates.extend(candidate_base.with_suffix(suffix) for suffix in suffixes)
            candidates.extend(candidate_base / f"index{suffix}" for suffix in suffixes)
            candidates.extend(candidate_base / f"mod{suffix}" for suffix in suffixes)
        for candidate in candidates:
            if candidate.is_file():
                return _safe_relpath(self.repo_root, candidate)
        return None

    def _kind_from_signature(self, signature: str) -> str:
        stripped = signature.lstrip()
        if stripped.startswith("class "):
            return "class"
        if stripped.startswith(("interface ", "type ")):
            return "type"
        if stripped.startswith(("function ", "func ", "fn ")):
            return "function"
        if stripped.startswith(("struct ", "enum ", "trait ")):
            return "class"
        return "variable"

    def _symbols_for_files(self, file_paths: list[str], *, limit: int) -> list[SymbolRecord]:
        if not file_paths:
            return []
        placeholders = ",".join("?" for _ in file_paths)
        params: list[Any] = [self.repo_id, *file_paths, limit]
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                f"""
                SELECT *, NULL AS score FROM symbols
                WHERE repo_id = ? AND file_path IN ({placeholders})
                ORDER BY file_path, start_line
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_row_to_symbol(row) for row in rows]

    def _dedupe_symbols(self, symbols: list[SymbolRecord]) -> list[SymbolRecord]:
        seen: set[str] = set()
        output: list[SymbolRecord] = []
        for symbol in symbols:
            if symbol.symbol_id in seen:
                continue
            seen.add(symbol.symbol_id)
            output.append(symbol)
        return output

    def _cap_symbols_per_file(self, symbols: list[SymbolRecord], *, max_per_file: int) -> list[SymbolRecord]:
        if max_per_file <= 0:
            return symbols
        counts: dict[str, int] = {}
        output: list[SymbolRecord] = []
        for symbol in symbols:
            seen = counts.get(symbol.file_path, 0)
            if seen >= max_per_file:
                continue
            counts[symbol.file_path] = seen + 1
            output.append(symbol)
        return output

    def _import_neighbors(self, seed_files: list[str]) -> list[str]:
        if not seed_files:
            return []
        placeholders = ",".join("?" for _ in seed_files)
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                f"""
                SELECT DISTINCT COALESCE(target_file, raw_import) AS neighbor
                FROM imports
                WHERE repo_id = ? AND source_file IN ({placeholders})
                UNION
                SELECT DISTINCT source_file AS neighbor
                FROM imports
                WHERE repo_id = ? AND target_file IN ({placeholders})
                ORDER BY neighbor
                """,
                tuple([self.repo_id, *seed_files, self.repo_id, *seed_files]),
            ).fetchall()
        return [str(row["neighbor"]) for row in rows if row["neighbor"]]

    def _direct_importers(self, rel: str) -> list[str]:
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                """
                SELECT DISTINCT source_file FROM imports
                WHERE repo_id = ? AND target_file = ?
                ORDER BY source_file
                """,
                (self.repo_id, rel),
            ).fetchall()
        return [str(row["source_file"]) for row in rows]

    def _dead_code_candidates(self, rel: str) -> list[str]:
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                """
                SELECT symbol_name FROM symbols
                WHERE repo_id = ? AND file_path = ? AND kind IN ('function', 'class', 'async_function')
                ORDER BY start_line
                LIMIT 50
                """,
                (self.repo_id, rel),
            ).fetchall()
        candidates: list[str] = []
        haystack = "\n".join(self._read_file(path) for path in self._indexed_files() if path != rel)
        for row in rows:
            name = str(row["symbol_name"])
            if not name.startswith("_") and re.search(rf"\b{re.escape(name)}\b", haystack) is None:
                candidates.append(name)
        return candidates

    def _indexed_files(self) -> list[str]:
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                "SELECT file_path FROM files WHERE repo_id = ? ORDER BY file_path",
                (self.repo_id,),
            ).fetchall()
        return [str(row["file_path"]) for row in rows]

    def _read_file(self, rel: str) -> str:
        return (self.repo_root / rel).read_text(encoding="utf-8", errors="replace")

    def _read_file_slice(self, rel: str, start_byte: int, end_byte: int) -> str:
        data = (self.repo_root / rel).read_bytes()
        return data[start_byte:end_byte].decode("utf-8", errors="replace")

    def _source_section_for_symbol(
        self,
        symbol: SymbolRecord | dict[str, Any],
        *,
        line_numbers: bool = True,
    ) -> dict[str, Any]:
        payload = symbol.model_dump(mode="json") if isinstance(symbol, SymbolRecord) else symbol
        file_path = str(payload["file_path"])
        start_line = int(payload["start_line"])
        end_line = int(payload["end_line"])
        source = self._read_file_slice(file_path, int(payload["start_byte"]), int(payload["end_byte"]))
        lines = source.splitlines()
        if line_numbers:
            numbered = [f"{start_line + idx}\t{line}" for idx, line in enumerate(lines)]
            content = "\n".join(numbered)
        else:
            content = source
        return {
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "symbol_id": payload["symbol_id"],
            "symbol_name": payload["symbol_name"],
            "qualified_name": payload["qualified_name"],
            "line_numbers": line_numbers,
            "content": content,
        }

    def _merge_nearby_source_sections(
        self,
        sections: list[dict[str, Any]],
        *,
        gap_lines: int = 4,
    ) -> list[dict[str, Any]]:
        if not sections:
            return []
        ordered = sorted(sections, key=lambda item: (str(item["file_path"]), int(item["start_line"]), int(item["end_line"])))
        merged: list[dict[str, Any]] = [dict(ordered[0])]
        for section in ordered[1:]:
            current = merged[-1]
            same_file = str(current["file_path"]) == str(section["file_path"])
            near_or_overlap = int(section["start_line"]) <= int(current["end_line"]) + max(0, gap_lines)
            if same_file and near_or_overlap:
                line_numbers = bool(current.get("line_numbers", True))
                current["start_line"] = min(int(current["start_line"]), int(section["start_line"]))
                current["end_line"] = max(int(current["end_line"]), int(section["end_line"]))
                current["content"] = self._render_source_section(
                    str(current["file_path"]),
                    start_line=int(current["start_line"]),
                    end_line=int(current["end_line"]),
                    line_numbers=line_numbers,
                )
                continue
            merged.append(dict(section))
        for section in merged:
            section.pop("line_numbers", None)
        return merged

    def _render_source_section(
        self,
        file_path: str,
        *,
        start_line: int,
        end_line: int,
        line_numbers: bool,
    ) -> str:
        lines = self._read_file(file_path).splitlines()
        if not lines:
            return ""
        start_idx = max(0, start_line - 1)
        end_idx = min(len(lines), max(start_idx, end_line))
        segment = lines[start_idx:end_idx]
        if line_numbers:
            return "\n".join(f"{start_line + idx}\t{line}" for idx, line in enumerate(segment))
        return "\n".join(segment)

    def _usage_item(self, reference: UsageReference, *, snippet_lines: int) -> dict[str, Any]:
        payload = reference.model_dump(mode="json", exclude_none=True)
        if snippet_lines > 0 and "snippet" not in payload:
            payload["snippet"] = self._reference_snippet(reference.file_path, reference.line, snippet_lines)
        return payload

    def _reference_snippet(self, file_path: str, line: int, snippet_lines: int) -> str:
        lines = self._read_file(file_path).splitlines()
        if not lines:
            return ""
        start = max(0, line - 1)
        end = min(len(lines), start + max(1, snippet_lines))
        return "\n".join(lines[start:end])

    def _build_usages_payload(
        self,
        *,
        target: dict[str, Any],
        items: list[dict[str, Any]],
        group_by: Literal["file", "caller", "none"],
        truncated: bool,
    ) -> dict[str, Any]:
        provenance_breakdown = self._provenance_breakdown(items)
        provenance = self._items_provenance(items) if items else str(target.get("provenance") or _LOCAL_PROVENANCE)
        payload: dict[str, Any] = {
            "target": self._usage_target_summary(target),
            "references": self._group_usages(items, group_by=group_by),
            "reference_count": len(items),
            "group_by": group_by,
            "truncated": truncated,
            "cache_hit": False,
            "provenance": provenance,
            "provenance_breakdown": provenance_breakdown,
        }
        return payload

    def _group_usages(
        self,
        items: list[dict[str, Any]],
        *,
        group_by: Literal["file", "caller", "none"],
    ) -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
        if group_by == "none":
            return items
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            if group_by == "caller":
                key = str(item.get("caller") or item["file_path"])
            else:
                key = str(item["file_path"])
            grouped.setdefault(key, []).append(item)
        return grouped

    def _usage_target_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "symbol_id",
            "symbol_name",
            "qualified_name",
            "file_path",
            "kind",
            "signature",
            "start_line",
            "end_line",
            "language",
            "provenance",
        ]
        return {key: payload[key] for key in keys if key in payload}

    def _resolve_symbol_target(
        self,
        *,
        operation_name: str,
        query: str | None,
        symbol_id: str | None,
        qualified_name: str | None,
        symbol_name: str | None,
        file_path: str | None,
        kind: str | None,
        language: str | None,
        file_glob: str | None,
    ) -> dict[str, Any]:
        if symbol_id or qualified_name or (symbol_name and file_path):
            try:
                return self.get_symbol(
                    symbol_id=symbol_id,
                    qualified_name=qualified_name,
                    symbol_name=symbol_name,
                    file_path=file_path,
                    auto_index=False,
                )
            except LookupError:
                return {
                    "error": "symbol_not_found",
                    "message": "no matching symbol was found",
                    "cache_hit": False,
                    "provenance": _LOCAL_PROVENANCE,
                }
        target_query = query or qualified_name or symbol_name
        if not target_query:
            raise ValueError(f"query, symbol_id, qualified_name, or symbol_name is required for code {operation_name}")
        candidates = self.search_symbols(
            target_query,
            limit=20,
            kind=kind,
            language=language,
            snippet="none",
            file_glob=file_glob,
            auto_index=False,
        )
        exact = [
            candidate
            for candidate in candidates
            if (candidate.qualified_name == target_query or candidate.symbol_name == target_query)
            and (file_path is None or candidate.file_path == file_path)
        ]
        matches = exact or candidates
        deduped = list({candidate.symbol_id: candidate for candidate in matches}.values())
        if not deduped:
            return {
                "error": "symbol_not_found",
                "message": "no matching symbol was found",
                "cache_hit": False,
                "provenance": _LOCAL_PROVENANCE,
            }
        if len(deduped) > 1:
            return {
                "error": "disambiguation_required",
                "message": f"multiple symbols match the {operation_name} query",
                "matches": [
                    {
                        "symbol_id": candidate.symbol_id,
                        "qualified_name": candidate.qualified_name,
                        "symbol_name": candidate.symbol_name,
                        "file_path": candidate.file_path,
                        "start_line": candidate.start_line,
                        "provenance": candidate.provenance,
                    }
                    for candidate in deduped[:10]
                ],
                "cache_hit": False,
                "provenance": _LOCAL_PROVENANCE,
            }
        return self.get_symbol(symbol_id=deduped[0].symbol_id, auto_index=False)

    def _find_references_local(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[UsageReference]:
        try:
            target = self._get_symbol_local(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                file_path=file_path,
                symbol_name=symbol_name,
            )
        except LookupError:
            target = self._get_symbol_local(
                qualified_name=qualified_name,
                file_path=file_path,
                symbol_name=symbol_name,
            )
        target_name = str(target["symbol_name"])
        target_file = str(target["file_path"])
        target_start = int(target["start_line"])
        target_end = int(target["end_line"])
        indexed = self._indexed_references_for_symbol(
            target_name=target_name,
            target_file=target_file,
            target_start=target_start,
            target_end=target_end,
        )
        if indexed:
            return indexed
        results: list[UsageReference] = []
        seen: set[tuple[str, int, int]] = set()
        for rel in self._indexed_files():
            path = self.repo_root / rel
            try:
                tags = extract_tags(path)
            except OSError:
                continue
            lines = self._read_file(rel).splitlines()
            for tag in tags:
                if tag.kind != "reference" or tag.name != target_name:
                    continue
                if rel == target_file and target_start <= tag.line <= target_end:
                    continue
                line_text = lines[tag.line - 1] if 1 <= tag.line <= len(lines) else ""
                column = max(1, line_text.find(target_name) + 1) if line_text else 1
                key = (rel, tag.line, column)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    UsageReference(
                        file_path=rel,
                        line=tag.line,
                        column=column,
                        end_line=tag.line,
                        end_column=column + len(target_name) - 1,
                        snippet=line_text,
                        provenance="treesitter",
                    )
                )
        results.sort(key=lambda item: (item.file_path, item.line, item.column))
        return results

    def _indexed_references_for_symbol(
        self,
        *,
        target_name: str,
        target_file: str,
        target_start: int,
        target_end: int,
    ) -> list[UsageReference]:
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                """
                SELECT file_path, line, column, end_column, enclosing_qualified_name, snippet
                FROM "references"
                WHERE repo_id = ? AND symbol_name = ?
                ORDER BY file_path, line, column
                """,
                (self.repo_id, target_name),
            ).fetchall()
        results: list[UsageReference] = []
        seen: set[tuple[str, int, int]] = set()
        for row in rows:
            file_path = str(row["file_path"])
            line = int(row["line"])
            column = int(row["column"])
            if file_path == target_file and target_start <= line <= target_end:
                continue
            key = (file_path, line, column)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                UsageReference(
                    file_path=file_path,
                    line=line,
                    column=column,
                    end_line=line,
                    end_column=int(row["end_column"]),
                    caller=cast(str | None, row["enclosing_qualified_name"]),
                    snippet=str(row["snippet"]),
                    provenance="local_index",
                )
            )
        return results

    def _find_callers_local(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[CallGraphNode] | None:
        try:
            target = self._get_symbol_local(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                file_path=file_path,
                symbol_name=symbol_name,
            )
        except LookupError:
            return None
        target_name = str(target["symbol_name"])
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                """
                SELECT DISTINCT caller_symbol_name, caller_qualified_name, caller_file_path,
                       caller_start_line, caller_end_line
                FROM call_edges
                WHERE repo_id = ? AND (callee_name = ? OR callee_name LIKE ?)
                ORDER BY caller_file_path, caller_start_line
                """,
                (self.repo_id, target_name, f"%.{target_name}"),
            ).fetchall()
        return [
            self._call_graph_node_from_indexed_row(
                file_path=str(row["caller_file_path"]),
                start_line=int(row["caller_start_line"]),
                end_line=int(row["caller_end_line"]),
                symbol_name=str(row["caller_symbol_name"]),
                qualified_name=str(row["caller_qualified_name"]),
            )
            for row in rows
        ]

    def _fallback_callers_from_references(
        self,
        *,
        target: dict[str, Any],
        limit: int,
    ) -> CallGraphTraversalResult:
        target_symbol_id = str(target["symbol_id"])
        target_file = str(target["file_path"])
        target_start = int(target["start_line"])
        target_end = int(target["end_line"])
        references = self.intel_store.find_references(
            symbol_id=target_symbol_id,
            qualified_name=str(target["qualified_name"]),
            file_path=target_file,
            symbol_name=str(target["symbol_name"]),
        )
        references = sorted(
            [*references, *self._cross_lang_usage_references(target)],
            key=lambda item: (item.file_path, item.line, item.column, item.provenance),
        )
        nodes_by_id: dict[str, CallGraphNode] = {}
        edges: list[CallGraphEdge] = []
        seen_edges: set[tuple[str, str, int]] = set()
        truncated = False
        for reference in references:
            if reference.file_path == target_file and target_start <= reference.line <= target_end:
                continue
            node = self._caller_node_from_reference(reference, target_symbol_id=target_symbol_id)
            if node is None:
                continue
            if node.symbol_id not in nodes_by_id:
                if len(nodes_by_id) >= limit:
                    truncated = True
                    continue
                nodes_by_id[node.symbol_id] = node
            edge_key = (node.symbol_id, target_symbol_id, 1)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append(
                    CallGraphEdge(
                        caller_symbol_id=node.symbol_id,
                        callee_symbol_id=target_symbol_id,
                        depth=1,
                    )
                )
        ordered_nodes = sorted(nodes_by_id.values(), key=lambda item: (item.file_path, item.start_line, item.symbol_id))
        ordered_edges = sorted(edges, key=lambda item: (item.depth, item.caller_symbol_id, item.callee_symbol_id))
        if not ordered_edges:
            return CallGraphTraversalResult(
                nodes=[],
                edges=[],
                truncated=False,
                data_status="unavailable",
                message="routed call edge data is unavailable",
                snapshot=None,
            )
        return CallGraphTraversalResult(
            nodes=ordered_nodes,
            edges=ordered_edges,
            truncated=truncated,
            data_status="available",
            message="fallback caller graph derived from symbol references",
            snapshot=None,
        )

    def _caller_node_from_reference(
        self,
        reference: UsageReference,
        *,
        target_symbol_id: str,
    ) -> CallGraphNode | None:
        normalized_file = self._normalize_file_arg(reference.file_path)
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute(
                """
                SELECT *, NULL AS score FROM symbols
                WHERE repo_id = ? AND file_path = ? AND start_line <= ? AND end_line >= ?
                ORDER BY (end_line - start_line) ASC, start_line DESC
                LIMIT 1
                """,
                (self.repo_id, normalized_file, reference.line, reference.line),
            ).fetchone()
        if row is not None:
            symbol = _row_to_symbol(row)
            if symbol.symbol_id == target_symbol_id:
                return None
            return CallGraphNode(
                symbol_id=symbol.symbol_id,
                symbol_name=symbol.symbol_name,
                qualified_name=symbol.qualified_name,
                file_path=symbol.file_path,
                kind=symbol.kind,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                provenance=reference.provenance or symbol.provenance,
            )
        synthetic_seed = f"{normalized_file}:{reference.line}:{reference.column}:{reference.caller or ''}"
        synthetic_id = f"ref::{hashlib.sha1(synthetic_seed.encode('utf-8')).hexdigest()[:16]}"
        fallback_name = reference.caller or f"{Path(normalized_file).name}:{reference.line}"
        return CallGraphNode(
            symbol_id=synthetic_id,
            symbol_name=fallback_name,
            qualified_name=fallback_name,
            file_path=normalized_file,
            kind="reference",
            start_line=reference.line,
            end_line=reference.end_line,
            provenance=reference.provenance or "treesitter",
        )

    def _find_callees_local(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[CallGraphNode] | None:
        try:
            target = self._get_symbol_local(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                file_path=file_path,
                symbol_name=symbol_name,
            )
        except LookupError:
            return None
        target_file = str(target["file_path"])
        target_start = int(target["start_line"])
        target_end = int(target["end_line"])
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                """
                SELECT DISTINCT callee_name
                FROM call_edges
                WHERE repo_id = ? AND caller_file_path = ?
                  AND caller_start_line = ? AND caller_end_line = ?
                ORDER BY callee_name
                """,
                (self.repo_id, target_file, target_start, target_end),
            ).fetchall()
        nodes: list[CallGraphNode] = []
        seen: set[str] = set()
        for row in rows:
            callee_name = str(row["callee_name"])
            short_name = callee_name.rsplit(".", 1)[-1]
            if short_name in seen:
                continue
            seen.add(short_name)
            try:
                payload = self._get_symbol_local(symbol_name=short_name)
                nodes.append(
                    CallGraphNode(
                        symbol_id=str(payload["symbol_id"]),
                        symbol_name=str(payload["symbol_name"]),
                        qualified_name=str(payload["qualified_name"]),
                        file_path=str(payload["file_path"]),
                        kind=str(payload["kind"]),
                        start_line=int(payload["start_line"]),
                        end_line=int(payload["end_line"]),
                        provenance=str(payload.get("provenance") or "local_index"),
                    )
                )
            except LookupError:
                synthetic_id = f"local-callee::{hashlib.sha1(callee_name.encode('utf-8')).hexdigest()[:16]}"
                nodes.append(
                    CallGraphNode(
                        symbol_id=synthetic_id,
                        symbol_name=short_name,
                        qualified_name=callee_name,
                        file_path=target_file,
                        kind="reference",
                        start_line=target_start,
                        end_line=target_end,
                        provenance="local_index",
                    )
                )
        return nodes

    def _call_graph_node_from_indexed_row(
        self,
        *,
        file_path: str,
        start_line: int,
        end_line: int,
        symbol_name: str,
        qualified_name: str,
    ) -> CallGraphNode:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute(
                    """
                    SELECT *, NULL AS score FROM symbols
                    WHERE repo_id = ? AND file_path = ? AND start_line = ? AND symbol_name = ?
                    LIMIT 1
                    """,
                    (self.repo_id, file_path, start_line, symbol_name),
            ).fetchone()
        if row is not None:
            symbol = _row_to_symbol(row)
            return CallGraphNode(
                    symbol_id=symbol.symbol_id,
                    symbol_name=symbol.symbol_name,
                    qualified_name=symbol.qualified_name,
                    file_path=symbol.file_path,
                    kind=symbol.kind,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    provenance="local_index",
            )
        synthetic_id = f"local-call::{hashlib.sha1(f'{file_path}:{start_line}:{qualified_name}'.encode('utf-8')).hexdigest()[:16]}"
        return CallGraphNode(
            symbol_id=synthetic_id,
            symbol_name=symbol_name,
            qualified_name=qualified_name,
            file_path=file_path,
            kind="function",
            start_line=start_line,
            end_line=end_line,
            provenance="local_index",
        )

    def _parse_rg_output(self, output: str, *, limit: int) -> list[TextMatch]:
        matches: list[TextMatch] = []
        for line in output.splitlines():
            if len(matches) >= limit:
                break
            path_text, sep, rest = line.partition(":")
            if not sep:
                continue
            line_text, sep, rest = rest.partition(":")
            if not sep:
                continue
            column_text, sep, text = rest.partition(":")
            if not sep:
                continue
            with contextlib.suppress(ValueError):
                matches.append(
                    TextMatch(
                        file_path=self._normalize_file_arg(path_text),
                        line=int(line_text),
                        column=int(column_text),
                        text=text,
                    )
                )
        return matches

    def _cache_get(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        return self._cache.get(
            tool_name=tool_name,
            args=args,
            index_version=self._current_index_version(),
            repo_id=self.repo_id,
        )

    def _cache_set(self, tool_name: str, args: dict[str, Any], payload: dict[str, Any]) -> None:
        self._cache.set(
            tool_name=tool_name,
            args=args,
            index_version=self._current_index_version(),
            repo_id=self.repo_id,
            payload=payload,
        )

    def _reindex_files(self, file_paths: list[str]) -> None:
        if not file_paths:
            return
        self.index_repo()

    def _current_index_version(self) -> int:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute("SELECT value FROM engine_state WHERE key = 'index_version'").fetchone()
        return int(row["value"]) if row is not None else 0

    def _index_snapshot(self) -> dict[str, Any]:
        with self._connect() as conn:
            self._init_schema(conn)
            file_count_row = conn.execute("SELECT COUNT(*) AS count FROM files WHERE repo_id = ?", (self.repo_id,)).fetchone()
            symbol_count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM symbols WHERE repo_id = ?",
                (self.repo_id,),
            ).fetchone()
            import_count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM imports WHERE repo_id = ?",
                (self.repo_id,),
            ).fetchone()
            indexed_at_row = conn.execute(
                "SELECT MAX(indexed_at) AS indexed_at FROM files WHERE repo_id = ?",
                (self.repo_id,),
            ).fetchone()
        files_indexed = int(file_count_row["count"]) if file_count_row is not None else 0
        symbols_indexed = int(symbol_count_row["count"]) if symbol_count_row is not None else 0
        imports_indexed = int(import_count_row["count"]) if import_count_row is not None else 0
        last_indexed_at = str(indexed_at_row["indexed_at"]) if indexed_at_row and indexed_at_row["indexed_at"] else None
        index_age_seconds: int | None = None
        if last_indexed_at:
            with contextlib.suppress(ValueError):
                parsed = datetime.fromisoformat(last_indexed_at.replace("Z", "+00:00"))
                index_age_seconds = max(0, int((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()))
        return {
            "files_indexed": files_indexed,
            "symbols_indexed": symbols_indexed,
            "imports_indexed": imports_indexed,
            "last_indexed_at": last_indexed_at,
            "index_age_seconds": index_age_seconds,
        }

    def _bump_index_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM engine_state WHERE key = 'index_version'").fetchone()
        current = int(row["value"]) if row is not None else 0
        next_version = current + 1
        conn.execute(
            """
            INSERT INTO engine_state(key, value)
            VALUES ('index_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(next_version),),
        )
        return next_version

    def _payload_tokens(self, payload: Any) -> int:
        return count_tokens(_canonical_json(payload))

    def _compute_total_tokens(self, payload: dict[str, Any]) -> int:
        total_tokens = 0
        while True:
            candidate = dict(payload)
            candidate["total_tokens"] = total_tokens
            measured = self._payload_tokens(candidate)
            if measured == total_tokens:
                return measured
            total_tokens = measured

    def _dedupe_search_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int, int, str]] = set()
        for item in items:
            key = (
                str(item.get("symbol_id") or ""),
                str(item.get("file_path") or ""),
                int(item.get("start_line") or 0),
                int(item.get("end_line") or 0),
                str(item.get("qualified_name") or item.get("symbol_name") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _compact_search_items(
        self,
        items: list[dict[str, Any]],
        *,
        scope: Literal["repo", "external", "deleted"],
    ) -> list[dict[str, Any]]:
        allowed_keys = _DELETED_SEARCH_COMPACT_DEFAULT_KEYS if scope == "deleted" else _SEARCH_COMPACT_DEFAULT_KEYS
        return [{key: value for key, value in item.items() if key in allowed_keys} for item in items]

    def _effective_budget_tokens(self, operation: str, requested_budget_tokens: int) -> int:
        requested = max(1, int(requested_budget_tokens))
        safety_max = resolve_output_policy(operation).max_total_tokens
        return min(requested, safety_max)

    def _items_provenance(self, items: list[dict[str, Any]]) -> str:
        provenances = [str(item.get("provenance")) for item in items if item.get("provenance")]
        if not provenances:
            return _LOCAL_PROVENANCE
        first = provenances[0]
        if all(provenance == first for provenance in provenances):
            return first
        return _LOCAL_PROVENANCE

    def _provenance_breakdown(self, items: list[dict[str, Any]]) -> dict[str, int]:
        breakdown: dict[str, int] = {}
        for item in items:
            provenance = str(item.get("provenance") or _LOCAL_PROVENANCE)
            breakdown[provenance] = breakdown.get(provenance, 0) + 1
        return breakdown

    def _finalize_packed_payload(
        self,
        payload: dict[str, Any],
        *,
        full_total_tokens: int,
        base_tokens_saved: int = 0,
    ) -> dict[str, Any]:
        finalized = dict(payload)
        tokens_saved = max(0, base_tokens_saved)
        while True:
            finalized["tokens_saved"] = tokens_saved
            total_tokens = self._compute_total_tokens(finalized)
            updated_tokens_saved = max(base_tokens_saved, full_total_tokens - total_tokens)
            if updated_tokens_saved == tokens_saved:
                finalized["total_tokens"] = total_tokens
                return finalized
            tokens_saved = updated_tokens_saved

    def _fit_items_to_budget(
        self,
        items: list[dict[str, Any]],
        *,
        budget_tokens: int,
        essential_keys: list[str],
        optional_keys_in_drop_order: list[str],
        build_payload: Callable[[list[dict[str, Any]]], dict[str, Any]],
        enforce_protected_top_rank: bool = True,
    ) -> dict[str, Any]:
        minimal_items, _, _ = self._budget.pack(
            items,
            0,
            essential_keys=essential_keys,
            optional_keys_in_drop_order=optional_keys_in_drop_order,
        )
        protected_items = minimal_items[: min(PROTECTED_TOP_RANK, len(minimal_items))]
        protected_payload = build_payload(protected_items)
        if enforce_protected_top_rank and protected_items and protected_payload["total_tokens"] > budget_tokens:
            return self._budget_error_payload(
                budget_tokens=budget_tokens,
                minimum_required_tokens=int(protected_payload["total_tokens"]),
                provenance=str(protected_payload.get("provenance") or _LOCAL_PROVENANCE),
            )

        best_payload = build_payload(minimal_items)
        if best_payload["total_tokens"] > budget_tokens:
            for end in range(len(minimal_items) - 1, -1, -1):
                candidate = build_payload(minimal_items[:end])
                if candidate["total_tokens"] <= budget_tokens:
                    return candidate
            return build_payload([])

        low = 0
        high = max(0, budget_tokens)
        while low <= high:
            mid = (low + high) // 2
            packed_items, _, _ = self._budget.pack(
                items,
                mid,
                essential_keys=essential_keys,
                optional_keys_in_drop_order=optional_keys_in_drop_order,
            )
            candidate = build_payload(packed_items)
            if candidate["total_tokens"] <= budget_tokens:
                best_payload = candidate
                low = mid + 1
            else:
                high = mid - 1

        return best_payload

    def _budget_error_payload(
        self,
        *,
        budget_tokens: int,
        minimum_required_tokens: int,
        provenance: str,
    ) -> dict[str, Any]:
        return self._finalize_packed_payload(
            {
                "error": "budget_too_small",
                "message": "budget_tokens cannot fit the protected top-ranked essentials",
                "budget_tokens": budget_tokens,
                "minimum_required_tokens": minimum_required_tokens,
                "cache_hit": False,
                "provenance": provenance,
            },
            full_total_tokens=max(minimum_required_tokens, 0),
        )

    def _pack_items_payload(
        self,
        items: list[dict[str, Any]],
        *,
        budget_tokens: int,
        essential_keys: list[str],
        optional_keys_in_drop_order: list[str],
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        extra = dict(extra_payload or {})
        provenance = self._items_provenance(items)
        provenance_breakdown = self._provenance_breakdown(items)
        full_payload = {
            "items": items,
            "cache_hit": False,
            "provenance": provenance,
            "provenance_breakdown": provenance_breakdown,
            **extra,
        }
        full_total_tokens = self._compute_total_tokens({**full_payload, "tokens_saved": 0})

        def build_payload(packed_items: list[dict[str, Any]]) -> dict[str, Any]:
            return self._finalize_packed_payload(
                {
                    "items": packed_items,
                    "cache_hit": False,
                    "provenance": provenance,
                    "provenance_breakdown": self._provenance_breakdown(packed_items),
                    **extra,
                },
                full_total_tokens=full_total_tokens,
            )

        packed = self._fit_items_to_budget(
            items,
            budget_tokens=budget_tokens,
            essential_keys=essential_keys,
            optional_keys_in_drop_order=optional_keys_in_drop_order,
            build_payload=build_payload,
        )
        return self._maybe_attach_overflow_metadata(
            packed_payload=packed,
            full_payload=full_payload,
            full_total_tokens=full_total_tokens,
            budget_tokens=budget_tokens,
        )

    def _pack_pattern_matches(
        self,
        result: PatternSearchResult,
        *,
        budget_tokens: int,
    ) -> dict[str, Any]:
        items = [match.to_dict() for match in result.matches]
        full_payload = {
            "matches": items,
            "truncated": bool(result.truncated),
            "total_matches": result.total_matches if result.total_matches is not None else len(items),
            "cache_hit": False,
            "provenance": "ast-grep",
            "provenance_breakdown": {"ast-grep": len(items)},
        }
        full_total_tokens = self._compute_total_tokens({**full_payload, "tokens_saved": 0})

        def build_payload(packed_items: list[dict[str, Any]]) -> dict[str, Any]:
            truncated = bool(result.truncated) or len(packed_items) < len(items)
            return self._finalize_packed_payload(
                {
                    "matches": packed_items,
                    "truncated": truncated,
                    "total_matches": result.total_matches if result.total_matches is not None else len(items),
                    "cache_hit": False,
                    "provenance": "ast-grep",
                    "provenance_breakdown": {"ast-grep": len(packed_items)},
                },
                full_total_tokens=full_total_tokens,
            )

        packed = self._fit_items_to_budget(
            items,
            budget_tokens=budget_tokens,
            essential_keys=_PATTERN_ESSENTIAL_KEYS,
            optional_keys_in_drop_order=_PATTERN_OPTIONAL_KEYS,
            build_payload=build_payload,
        )
        return self._maybe_attach_overflow_metadata(
            packed_payload=packed,
            full_payload=full_payload,
            full_total_tokens=full_total_tokens,
            budget_tokens=budget_tokens,
        )

    def _pack_pattern_rewrite(
        self,
        result: PatternRewriteResult,
        *,
        budget_tokens: int,
    ) -> dict[str, Any]:
        return self._pack_single_payload(
            {
                "diff": result.diff,
                "files_changed": result.files_changed,
                "provenance": "ast-grep",
            },
            budget_tokens=budget_tokens,
            essential_keys=["diff", "files_changed", "provenance"],
            optional_keys_in_drop_order=[],
        )

    def _pack_single_payload(
        self,
        payload: dict[str, Any],
        *,
        budget_tokens: int,
        essential_keys: list[str],
        optional_keys_in_drop_order: list[str],
        base_tokens_saved: int = 0,
    ) -> dict[str, Any]:
        full_payload = dict(payload)
        full_payload.setdefault("cache_hit", False)
        full_payload.setdefault("provenance", _LOCAL_PROVENANCE)
        full_total_tokens = self._compute_total_tokens({**full_payload, "tokens_saved": max(0, base_tokens_saved)})
        minimal_payload = {key: full_payload[key] for key in essential_keys if key in full_payload}

        def build_payload(packed_items: list[dict[str, Any]]) -> dict[str, Any]:
            packed_payload = dict(packed_items[0]) if packed_items else dict(minimal_payload)
            packed_payload["cache_hit"] = False
            packed_payload["provenance"] = str(full_payload.get("provenance") or _LOCAL_PROVENANCE)
            return self._finalize_packed_payload(
                packed_payload,
                full_total_tokens=full_total_tokens,
                base_tokens_saved=base_tokens_saved,
            )

        packed = self._fit_items_to_budget(
            [full_payload],
            budget_tokens=budget_tokens,
            essential_keys=[*essential_keys, "cache_hit", "tokens_saved", "provenance"],
            optional_keys_in_drop_order=optional_keys_in_drop_order,
            build_payload=build_payload,
            enforce_protected_top_rank=False,
        )
        return self._maybe_attach_overflow_metadata(
            packed_payload=packed,
            full_payload=full_payload,
            full_total_tokens=full_total_tokens,
            budget_tokens=budget_tokens,
            base_tokens_saved=base_tokens_saved,
        )

    def _maybe_attach_overflow_metadata(
        self,
        *,
        packed_payload: dict[str, Any],
        full_payload: dict[str, Any],
        full_total_tokens: int,
        budget_tokens: int,
        base_tokens_saved: int = 0,
    ) -> dict[str, Any]:
        if full_total_tokens <= budget_tokens:
            return packed_payload
        if "error" in packed_payload:
            return packed_payload
        overflow_tokens = full_total_tokens - budget_tokens
        if overflow_tokens < _OVERFLOW_SPILL_MIN_EXCESS_TOKENS:
            return packed_payload
        packed_total_tokens = int(packed_payload.get("total_tokens", self._compute_total_tokens(packed_payload)))
        reduction_tokens = max(0, full_total_tokens - packed_total_tokens)
        if reduction_tokens < _OVERFLOW_SPILL_MIN_REDUCTION_TOKENS:
            return packed_payload
        overflow_meta = self._write_overflow_artifact(full_payload, full_total_tokens=full_total_tokens)
        with_meta = dict(packed_payload)
        with_meta["overflow"] = overflow_meta
        finalized = self._finalize_packed_payload(
            with_meta,
            full_total_tokens=full_total_tokens,
            base_tokens_saved=max(base_tokens_saved, int(packed_payload.get("tokens_saved", 0) or 0)),
        )
        if finalized.get("total_tokens", 0) > budget_tokens:
            return packed_payload
        return finalized

    def _write_overflow_artifact(self, payload: dict[str, Any], *, full_total_tokens: int) -> dict[str, Any]:
        artifact_root = default_store_root() / "overflow" / "code"
        artifact_root.mkdir(parents=True, exist_ok=True)
        canonical = _canonical_json(payload)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        filename = f"{self.repo_id}-{int(time.time() * 1000)}-{digest}.json"
        artifact_path = artifact_root / filename
        artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        artifact_bytes = artifact_path.stat().st_size
        return {
            "spilled": True,
            "reason": "budget_exceeded",
            "full_total_tokens": full_total_tokens,
            "artifact_path": str(artifact_path),
            "artifact_format": "json",
            "artifact_bytes": artifact_bytes,
        }

    def _mark_cache_hit(self, payload: dict[str, Any]) -> dict[str, Any]:
        cached = cast(dict[str, Any], json.loads(_canonical_json(payload)))
        cached["cache_hit"] = True
        cached["provenance"] = "cached"
        cached["total_tokens"] = self._compute_total_tokens(cached)
        return cached

    def _normalize_cache_tool(self, cache_tool: str | None) -> str | None:
        normalized = (cache_tool or "all").strip().lower()
        if normalized not in _CACHE_TOOL_ALIASES:
            choices = ", ".join(sorted(_CACHE_TOOL_ALIASES))
            raise ValueError(f"cache_tool must be one of: {choices}")
        return _CACHE_TOOL_ALIASES[normalized]

    def _attach_snippet(
        self,
        symbol: SymbolRecord,
        *,
        snippet: Literal["none", "head", "full"],
        snippet_lines: int,
    ) -> SymbolRecord:
        if snippet == "none":
            return symbol.model_copy(update={"snippet": None})
        safe_line_count = max(1, snippet_lines)
        snippet_text = self._read_symbol_snippet(
            symbol.file_path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            mode=snippet,
            snippet_lines=safe_line_count,
        )
        return symbol.model_copy(update={"snippet": snippet_text})

    def _read_symbol_snippet(
        self,
        file_path: str,
        *,
        start_line: int,
        end_line: int,
        mode: Literal["head", "full"],
        snippet_lines: int,
    ) -> str:
        resolved = self._resolve_inside_repo(file_path)
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return ""
        start_index = max(0, start_line - 1)
        max_end_index = min(len(lines), start_index + snippet_lines)
        if mode == "full":
            max_end_index = min(max_end_index, max(start_index + 1, end_line))
        snippet_slice = lines[start_index:max_end_index]
        return "\n".join(snippet_slice)

    def _flatten_outline(self, files: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for file_path in sorted(files):
            for item in files[file_path]:
                items.append({"file_path": file_path, **item})
        return items

    def _group_outline(self, items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            file_path = str(item["file_path"])
            grouped.setdefault(file_path, []).append({k: v for k, v in item.items() if k != "file_path"})
        return grouped

    def _normalize_files_path(self, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = self._normalize_file_arg(value).strip().strip("/")
        if normalized in {"", "."}:
            return None
        return normalized

    def _matches_files_filters(
        self,
        file_path: str,
        *,
        path: str | None,
        pattern: str | None,
        max_depth: int | None,
    ) -> bool:
        if path and file_path != path and not file_path.startswith(f"{path}/"):
            return False
        if pattern and not fnmatch.fnmatch(file_path, pattern):
            return False
        if max_depth is None:
            return True
        if path and file_path == path:
            relative = ""
        elif path and file_path.startswith(f"{path}/"):
            relative = file_path[len(path) + 1 :]
        else:
            relative = file_path
        depth = relative.count("/") if relative else 0
        return depth <= max_depth

    def _indexed_file_records(
        self,
        *,
        path: str | None,
        pattern: str | None,
        max_depth: int | None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._init_schema(conn)
            file_rows = conn.execute(
                """
                SELECT file_path, language
                FROM files
                WHERE repo_id = ?
                ORDER BY file_path
                """,
                (self.repo_id,),
            ).fetchall()
            symbol_count_rows = conn.execute(
                """
                SELECT file_path, COUNT(*) AS symbol_count
                FROM symbols
                WHERE repo_id = ?
                GROUP BY file_path
                """,
                (self.repo_id,),
            ).fetchall()
            top_symbol_rows = conn.execute(
                """
                SELECT file_path, symbol_name
                FROM (
                    SELECT
                        file_path,
                        symbol_name,
                        ROW_NUMBER() OVER (
                            PARTITION BY file_path
                            ORDER BY start_line, symbol_name
                        ) AS row_no
                    FROM symbols
                    WHERE repo_id = ?
                )
                WHERE row_no <= 3
                ORDER BY file_path, row_no
                """,
                (self.repo_id,),
            ).fetchall()
        symbol_counts = {str(row["file_path"]): int(row["symbol_count"]) for row in symbol_count_rows}
        top_symbols: dict[str, list[str]] = {}
        for row in top_symbol_rows:
            file_path = str(row["file_path"])
            top_symbols.setdefault(file_path, []).append(str(row["symbol_name"]))

        records: list[dict[str, Any]] = []
        for row in file_rows:
            file_path = str(row["file_path"])
            if not self._matches_files_filters(file_path, path=path, pattern=pattern, max_depth=max_depth):
                continue
            record = IndexedFileRecord(
                file_path=file_path,
                language=str(row["language"] or "unknown"),
                symbol_count=symbol_counts.get(file_path, 0),
                top_symbols=top_symbols.get(file_path, []),
            )
            records.append(record.model_dump(mode="json", exclude_none=True))
        return records

    def _indexed_route_records(
        self,
        *,
        file_glob: str | None,
        language: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        candidates = self._indexed_file_records(path=None, pattern=file_glob, max_depth=None)
        allowed_languages = {"python", "javascript", "typescript"}
        if language is not None and language not in allowed_languages:
            return [], False
        routes: list[dict[str, Any]] = []
        truncated = False
        for candidate in candidates:
            file_path = str(candidate.get("file_path") or "")
            file_language = str(candidate.get("language") or "unknown").lower()
            if file_language not in allowed_languages:
                continue
            if language is not None and file_language != language:
                continue
            for route in self._extract_routes_from_file(file_path=file_path, language=file_language):
                routes.append(route)
                if len(routes) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        routes.sort(key=lambda item: (str(item.get("file_path")), int(item.get("line", 0)), str(item.get("route"))))
        return routes[:limit], truncated

    def _extract_routes_from_file(
        self,
        *,
        file_path: str,
        language: str,
    ) -> list[dict[str, Any]]:
        with contextlib.suppress(OSError, UnicodeDecodeError):
            source = self._resolve_inside_repo(file_path).read_text(encoding="utf-8", errors="replace")
            lines = source.splitlines()
            records: list[RouteRecord] = []
            if language == "python":
                records.extend(self._extract_python_routes(file_path=file_path, lines=lines))
            elif language in {"javascript", "typescript"}:
                records.extend(self._extract_js_routes(file_path=file_path, lines=lines, language=language))
            return [record.model_dump(mode="json", exclude_none=True) for record in records]
        return []

    def _extract_python_routes(self, *, file_path: str, lines: list[str]) -> list[RouteRecord]:
        records: list[RouteRecord] = []
        for index, line in enumerate(lines, start=1):
            fastapi_match = _FASTAPI_DECORATOR_RE.search(line)
            if fastapi_match:
                method = fastapi_match.group("verb").upper()
                if method == "WEBSOCKET":
                    method = "WS"
                records.append(
                    RouteRecord(
                        framework="fastapi",
                        method=method,
                        route=fastapi_match.group("route"),
                        file_path=file_path,
                        line=index,
                        language="python",
                        handler=self._next_python_def_name(lines, index),
                        router=fastapi_match.group("router"),
                        provenance=_LOCAL_PROVENANCE,
                    )
                )
                continue
            fastapi_api_route_match = _FASTAPI_API_ROUTE_RE.search(line)
            if fastapi_api_route_match:
                route = fastapi_api_route_match.group("route")
                methods = self._parse_methods(fastapi_api_route_match.group("rest"))
                for method in methods:
                    records.append(
                        RouteRecord(
                            framework="fastapi",
                            method=method,
                            route=route,
                            file_path=file_path,
                            line=index,
                            language="python",
                            handler=self._next_python_def_name(lines, index),
                            router=fastapi_api_route_match.group("router"),
                            provenance=_LOCAL_PROVENANCE,
                        )
                    )
                continue
            flask_match = _FLASK_ROUTE_RE.search(line)
            if flask_match:
                route = flask_match.group("route")
                methods = self._parse_methods(flask_match.group("rest"))
                for method in methods:
                    records.append(
                        RouteRecord(
                            framework="flask",
                            method=method,
                            route=route,
                            file_path=file_path,
                            line=index,
                            language="python",
                            handler=self._next_python_def_name(lines, index),
                            router=flask_match.group("router"),
                            provenance=_LOCAL_PROVENANCE,
                        )
                    )
                continue
            flask_rule_match = _FLASK_ADD_URL_RULE_RE.search(line)
            if flask_rule_match:
                route = flask_rule_match.group("route")
                methods = self._parse_methods(flask_rule_match.group("rest"))
                handler = self._parse_python_handler(flask_rule_match.group("rest"))
                for method in methods:
                    records.append(
                        RouteRecord(
                            framework="flask",
                            method=method,
                            route=route,
                            file_path=file_path,
                            line=index,
                            language="python",
                            handler=handler,
                            router=flask_rule_match.group("router"),
                            provenance=_LOCAL_PROVENANCE,
                        )
                    )
                continue
            django_match = _DJANGO_PATH_RE.search(line)
            if django_match:
                records.append(
                    RouteRecord(
                        framework="django",
                        method="ANY",
                        route=django_match.group("route"),
                        file_path=file_path,
                        line=index,
                        language="python",
                        handler=django_match.group("handler"),
                        provenance=_LOCAL_PROVENANCE,
                    )
                )
                continue
            django_url_match = _DJANGO_URL_RE.search(line)
            if django_url_match:
                records.append(
                    RouteRecord(
                        framework="django",
                        method="ANY",
                        route=django_url_match.group("route"),
                        file_path=file_path,
                        line=index,
                        language="python",
                        handler=django_url_match.group("handler"),
                        provenance=_LOCAL_PROVENANCE,
                    )
                )
        return records

    def _extract_js_routes(self, *, file_path: str, lines: list[str], language: str) -> list[RouteRecord]:
        records: list[RouteRecord] = []
        for index, line in enumerate(lines, start=1):
            match = _EXPRESS_ROUTE_RE.search(line)
            if match:
                verb = match.group("verb").upper()
                method = "ANY" if verb in {"ALL", "USE"} else verb
                records.append(
                    RouteRecord(
                        framework="express",
                        method=method,
                        route=match.group("route"),
                        file_path=file_path,
                        line=index,
                        language=language,
                        handler=match.group("handler"),
                        router=match.group("router"),
                        provenance=_LOCAL_PROVENANCE,
                    )
                )
            chain_match = _EXPRESS_ROUTE_CHAIN_RE.search(line)
            if not chain_match:
                continue
            base_route = chain_match.group("route")
            chain = chain_match.group("chain") or ""
            for method_match in _EXPRESS_CHAIN_METHOD_RE.finditer(chain):
                verb = method_match.group("verb").upper()
                method = "ANY" if verb in {"ALL", "USE"} else verb
                records.append(
                    RouteRecord(
                        framework="express",
                        method=method,
                        route=base_route,
                        file_path=file_path,
                        line=index,
                        language=language,
                        handler=method_match.group("handler"),
                        router=chain_match.group("router"),
                        provenance=_LOCAL_PROVENANCE,
                    )
                )
        return records

    def _next_python_def_name(self, lines: list[str], decorator_line: int) -> str | None:
        for line in lines[decorator_line:]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("@"):
                continue
            match = re.match(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", stripped)
            if match:
                return match.group(1)
            break
        return None

    def _parse_methods(self, value: str | None) -> list[str]:
        if not value:
            return ["GET"]
        methods = [match.group("method").upper() for match in _METHOD_LITERAL_RE.finditer(value)]
        return methods or ["GET"]

    def _parse_python_handler(self, value: str | None) -> str | None:
        if not value:
            return None
        named = re.search(r"view_func\s*=\s*([A-Za-z_][A-Za-z0-9_\.]*)", value)
        if named:
            return named.group(1)
        positional = re.search(r",\s*([A-Za-z_][A-Za-z0-9_\.]*)", value)
        if positional:
            return positional.group(1)
        return None

    def _files_flat(
        self,
        items: list[dict[str, Any]],
        *,
        include_metadata: bool,
    ) -> list[dict[str, Any]]:
        flat: list[dict[str, Any]] = []
        for item in items:
            entry: dict[str, Any] = {"file_path": str(item["file_path"])}
            if include_metadata:
                entry["language"] = str(item.get("language") or "unknown")
                entry["symbol_count"] = int(item.get("symbol_count") or 0)
                entry["top_symbols"] = list(item.get("top_symbols") or [])
            flat.append(entry)
        return flat

    def _files_grouped(
        self,
        items: list[dict[str, Any]],
        *,
        include_metadata: bool,
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            language = str(item.get("language") or "unknown")
            entry: dict[str, Any] = {"file_path": str(item["file_path"])}
            if include_metadata:
                entry["language"] = language
                entry["symbol_count"] = int(item.get("symbol_count") or 0)
                entry["top_symbols"] = list(item.get("top_symbols") or [])
            grouped.setdefault(language, []).append(entry)
        return grouped

    def _files_tree(
        self,
        items: list[dict[str, Any]],
        *,
        include_metadata: bool,
    ) -> dict[str, Any]:
        tree: dict[str, Any] = {}
        for item in items:
            parts = str(item["file_path"]).split("/")
            cursor: dict[str, Any] = tree
            for segment in parts[:-1]:
                child = cursor.get(segment)
                if not isinstance(child, dict):
                    child = {}
                    cursor[segment] = child
                cursor = child
            file_name = parts[-1]
            if include_metadata:
                cursor[file_name] = {
                    "language": str(item.get("language") or "unknown"),
                    "symbol_count": int(item.get("symbol_count") or 0),
                }
            else:
                cursor[file_name] = {}
        return tree

    def _format_files_payload(
        self,
        items: list[dict[str, Any]],
        *,
        format: Literal["tree", "flat", "grouped"],
        include_metadata: bool,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        if format == "flat":
            return self._files_flat(items, include_metadata=include_metadata)
        if format == "grouped":
            return self._files_grouped(items, include_metadata=include_metadata)
        return self._files_tree(items, include_metadata=include_metadata)

    def _build_files_payload(
        self,
        items: list[dict[str, Any]],
        *,
        path: str | None,
        pattern: str | None,
        format: Literal["tree", "flat", "grouped"],
        include_metadata: bool,
        truncated: bool,
    ) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "repo_root": str(self.repo_root),
            "path": path,
            "pattern": pattern,
            "format": format,
            "file_count": len(items),
            "files": self._format_files_payload(items, format=format, include_metadata=include_metadata),
            "truncated": truncated,
            "cache_hit": False,
            "provenance": _LOCAL_PROVENANCE,
        }

    def _build_routes_payload(
        self,
        items: list[dict[str, Any]],
        *,
        file_glob: str | None,
        language: str | None,
        truncated: bool,
    ) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "repo_root": str(self.repo_root),
            "file_glob": file_glob,
            "language": language,
            "route_count": len(items),
            "routes": items,
            "truncated": truncated,
            "cache_hit": False,
            "provenance": _LOCAL_PROVENANCE,
        }

    def _file_mtime_ns(self, file_path: str) -> int | None:
        path = self._resolve_inside_repo(file_path)
        with contextlib.suppress(OSError):
            return path.stat().st_mtime_ns
        return None

    def _python_text_search(self, query: str, search_path: Path, *, limit: int, ignore_case: bool) -> list[TextMatch]:
        query_cmp = query.lower() if ignore_case else query
        paths = [search_path] if search_path.is_file() else iter_source_files(search_path)
        matches: list[TextMatch] = []
        for path in paths:
            rel = _safe_relpath(self.repo_root, path)
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                hay = line.lower() if ignore_case else line
                column = hay.find(query_cmp)
                if column >= 0:
                    matches.append(TextMatch(file_path=rel, line=line_no, column=column + 1, text=line))
                    if len(matches) >= limit:
                        return matches
        return matches

    def _register_symbol_intel_providers(self) -> None:
        try:
            from atelier.infra.code_intel.scip import ScipSymbolIntelProvider
        except Exception:
            return
        self.intel_store.register(
            ScipSymbolIntelProvider(
                repo_root=self.repo_root,
                repo_id=self.repo_id,
                state_sync=self._sync_external_artifact_state,
            )
        )

    def _sync_symbol_intel(self) -> None:
        self.intel_store.refresh()

    def _sync_external_artifact_state(self, state_key: str, signature: str) -> bool:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (state_key,)).fetchone()
            previous = str(row["value"]) if row is not None else None
            conn.execute(
                """
                INSERT INTO engine_state(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (state_key, signature),
            )
            if previous is not None and previous != signature:
                self._bump_index_version(conn)
                return True
        return False

    def _parse_autosync_debounce(self, raw_value: str | None) -> int:
        if raw_value is None:
            return 500
        with contextlib.suppress(ValueError):
            return max(50, int(raw_value))
        return 500

    def _autosync_status(self) -> dict[str, Any]:
        return {
            "enabled": self._autosync_enabled,
            "state": self._autosync_state,
            "mode": "incremental" if self._autosync_enabled else "scaffold_only",
            "debounce_ms": self._autosync_debounce_ms,
            "pending_events": self._autosync_pending_events,
            "last_event_at": self._autosync_last_event_at,
            "reindex_count": self._autosync_reindex_count,
            "history": list(self._autosync_history),
        }

    def _source_tree_signature(self) -> str:
        parts: list[str] = []
        for path in iter_source_files(self.repo_root):
            with contextlib.suppress(OSError):
                stat = path.stat()
                rel = _safe_relpath(self.repo_root, path)
                parts.append(f"{rel}|{stat.st_mtime_ns}|{stat.st_size}")
        digest_input = "\n".join(sorted(parts)).encode("utf-8")
        return hashlib.sha256(digest_input).hexdigest()

    def _maybe_autosync_reindex(self) -> None:
        current_signature = self._source_tree_signature()
        if self._autosync_signature is None:
            self._autosync_signature = current_signature
            self._autosync_last_sync_ms = int(time.time() * 1000)
            self._autosync_state = "idle"
            self._record_autosync_event(event="bootstrap", reason="seed_signature", reindexed=False)
            return
        if current_signature == self._autosync_signature:
            self._autosync_state = "idle"
            self._autosync_pending_events = 0
            return
        now_ms = int(time.time() * 1000)
        self._autosync_last_event_at = datetime.now(UTC).isoformat()
        self._autosync_pending_events = max(1, self._autosync_pending_events + 1)
        if now_ms - self._autosync_last_sync_ms < self._autosync_debounce_ms:
            self._autosync_state = "debouncing"
            self._record_autosync_event(event="change_detected", reason="within_debounce_window", reindexed=False)
            return
        self._autosync_state = "syncing"
        self.index_repo()
        self._autosync_signature = self._source_tree_signature()
        self._autosync_last_sync_ms = int(time.time() * 1000)
        self._autosync_pending_events = 0
        self._autosync_state = "idle"
        self._autosync_reindex_count += 1
        self._record_autosync_event(event="reindex", reason="source_signature_changed", reindexed=True)

    def _record_autosync_event(self, *, event: str, reason: str, reindexed: bool) -> None:
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "reason": reason,
            "reindexed": reindexed,
        }
        self._autosync_history.append(entry)
        if len(self._autosync_history) > 20:
            self._autosync_history = self._autosync_history[-20:]

    def _json_safe(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe(item) for item in value]
        return str(value)

    def _current_head_sha(self) -> str:
        from atelier.infra.code_intel.git_history import require_pygit2

        pygit2 = require_pygit2()
        repo = pygit2.Repository(str(self.repo_root))
        return str(repo.revparse_single("HEAD").id)

    def _safe_current_head_sha(self) -> str | None:
        with contextlib.suppress(Exception):
            value = self._current_head_sha()
            if value is None:
                return None
            return str(value)
        return None

    def _deleted_history_adapter(self) -> DeletedHistorySearchAdapter:
        if self._deleted_history_search_adapter is None:
            from atelier.infra.code_intel.git_history.adapter import DeletedHistorySearchAdapter

            self._deleted_history_search_adapter = DeletedHistorySearchAdapter(
                repo_root=self.repo_root,
                repo_id=self.repo_id,
                connection_factory=self.connection,
            )
        return self._deleted_history_search_adapter


__all__ = ["CodeContextEngine"]
