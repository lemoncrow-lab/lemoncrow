"""Persistent symbol index and token-budgeted retrieval for local code."""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import hashlib
import re
import shutil
import sqlite3
import subprocess
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from atelier.core.capabilities.code_context.models import (
    ContextPack,
    ImpactResult,
    IndexStats,
    SymbolRecord,
    TextMatch,
)
from atelier.core.capabilities.repo_map import build_repo_map
from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.core.capabilities.repo_map.graph import iter_source_files
from atelier.core.foundation.paths import default_store_root
from atelier.core.service.telemetry import emit_product_local
from atelier.infra.tree_sitter.tags import detect_language, extract_tags

_MAX_FILE_BYTES = 1_000_000
_FTS_TERM_RE = re.compile(r"[A-Za-z0-9_]+")
_JS_IMPORT_RE = re.compile(
    r"(?:from\s+['\"]([^'\"]+)['\"]|import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)|require\(\s*['\"]([^'\"]+)['\"]\s*\))"
)
_RUST_MOD_RE = re.compile(r"^\s*(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", re.M)
_GO_IMPORT_RE = re.compile(r"^\s*import\s+(?:\((.*?)\)|\"([^\"]+)\")", re.M | re.S)


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
                files_indexed += 1
                symbols_indexed += len(extracted)
                imports_indexed += len(imports)
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
        )

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
        auto_index: bool = True,
    ) -> list[SymbolRecord]:
        """BM25/FTS-ranked symbol search."""
        if auto_index:
            self._ensure_indexed()
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
        normalized_seeds = [self._normalize_file_arg(seed) for seed in seed_files or []]
        repo_map_payload = self.repo_map(seed_files=normalized_seeds, budget_tokens=max(200, budget_tokens // 4))
        symbol_hits = self.search_symbols(task, limit=max_symbols, auto_index=False)
        seed_symbols = self._symbols_for_files(normalized_seeds, limit=max_symbols)
        selected = self._dedupe_symbols([*seed_symbols, *symbol_hits])[:max_symbols]
        neighbors = self._import_neighbors(normalized_seeds)

        lines = ["# Atelier code context", f"task: {task}", ""]
        if repo_map_payload.get("outline"):
            lines.extend(["## repo_map", str(repo_map_payload["outline"]), ""])
        if neighbors:
            lines.extend(["## import_neighbors", *[f"- {item}" for item in neighbors[:20]], ""])

        packed_symbols: list[SymbolRecord] = []
        naive_tokens = 0
        for symbol in selected:
            full_file = self._read_file(symbol.file_path)
            naive_tokens += count_tokens(full_file)
            symbol_payload = self.get_symbol(symbol_id=symbol.symbol_id, auto_index=False)
            block = (
                f"## symbol {symbol.qualified_name} ({symbol.file_path}:{symbol.start_line}-{symbol.end_line})\n"
                f"```{symbol.language}\n{symbol_payload['source']}\n```"
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
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
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
            CREATE INDEX IF NOT EXISTS idx_symbols_repo_file ON symbols(repo_id, file_path);
            CREATE INDEX IF NOT EXISTS idx_symbols_repo_name ON symbols(repo_id, symbol_name);
            CREATE INDEX IF NOT EXISTS idx_imports_target ON imports(repo_id, target_file);
            """)

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
        for search_base in [base, *base.parents[:4]]:
            candidate = search_base / Path(*parts).with_suffix(".py")
            if candidate.is_file():
                return _safe_relpath(self.repo_root, candidate)
            package = search_base / Path(*parts) / "__init__.py"
            if package.is_file():
                return _safe_relpath(self.repo_root, package)
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


__all__ = ["CodeContextEngine"]
