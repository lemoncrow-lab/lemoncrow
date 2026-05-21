"""SemanticFileMemoryCapability — thin orchestrator over all sub-modules."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .indexer import FileIndex
from .models import FileOutline, SemanticSummary
from .python_ast import analyze_python, stub_function_bodies
from .python_ast import outline as python_outline

_logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _tiktoken_encoder() -> Any:
    """Return a tiktoken encoder, or None if tiktoken is unavailable.

    cl100k_base is the encoder OpenAI ships for GPT-4/3.5; it is the closest
    publicly-available proxy for Anthropic's tokenizer (Anthropic does not
    publish theirs). Empirically ~3.3-3.8 chars/token on code, vs the previous
    chars/4 heuristic which undercounted tokens by ~15-20%.
    """
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:
        _logger.warning("tiktoken unavailable, falling back to chars/4 heuristic: %s", exc)
        return None


def _count_tokens(text: str) -> int:
    """Token count via tiktoken cl100k_base, with chars/4 fallback."""
    if not text:
        return 0
    enc = _tiktoken_encoder()
    if enc is None:
        return len(text) // 4
    return len(enc.encode(text, disallowed_special=()))


from .search import SymbolIndex
from .typescript_ast import analyze_typescript
from .typescript_ast import outline as typescript_outline

try:
    from git import Repo
except Exception:  # pragma: no cover - optional dependency fallback
    Repo: Any = None  # type: ignore[no-redef]


class SemanticFileMemoryCapability:
    """
    Semantic file analysis with content-addressed caching.

    Capabilities:
    - Full Python AST extraction (functions, classes, methods, variables,
      decorators, docstrings, complexity, return types)
    - Full TypeScript/JS export/interface/type/enum detection
    - SHA-256 content-addressed cache (reliable across git, Docker, rsync)
    - Cross-file symbol resolution
    - BM25-ranked full-text search over cached summaries
    - Reverse dependency graph for change impact analysis
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._index = FileIndex(self._root)
        self._symbol_index = SymbolIndex(self._index)

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    @staticmethod
    def _language_for(path: Path) -> str:
        suffix = path.suffix.lower()
        return {
            ".py": "python",
            ".pyi": "python",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".sql": "sql",
            ".md": "markdown",
            ".markdown": "markdown",
            # Languages handled by the generic outline fallback (regex-based).
            # Per-language tree-sitter outlines are queued in
            # docs/plans/active/savings-honest-ab/README.md.
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".kt": "kotlin",
            ".kts": "kotlin",
            ".scala": "scala",
            ".rb": "ruby",
            ".cs": "csharp",
            ".cpp": "cpp",
            ".cc": "cpp",
            ".cxx": "cpp",
            ".hpp": "cpp",
            ".hh": "cpp",
            ".c": "c",
            ".h": "c",
            ".swift": "swift",
            ".php": "php",
            ".sh": "shell",
            ".bash": "shell",
            ".zsh": "shell",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".toml": "toml",
            ".json": "json",
        }.get(suffix, "text")

    # ------------------------------------------------------------------
    # Core summarisation
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_loc(source: str, language: str) -> int:
        """Count effective LOC (exclude blank + comment-only lines)."""
        if language == "python":
            return sum(
                1
                for line in source.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )

        if language in {"typescript", "javascript"}:
            count = 0
            in_block_comment = False
            for raw in source.splitlines():
                line = raw.strip()
                if not line:
                    continue

                if in_block_comment:
                    end_idx = line.find("*/")
                    if end_idx < 0:
                        continue
                    line = line[end_idx + 2 :].strip()
                    in_block_comment = False
                    if not line:
                        continue

                if line.startswith("//"):
                    continue

                if line.startswith("/*"):
                    end_idx = line.find("*/", 2)
                    if end_idx < 0:
                        in_block_comment = True
                        continue
                    trailing = line[end_idx + 2 :].strip()
                    if not trailing:
                        continue
                    line = trailing

                if line:
                    count += 1
            return count

        return sum(1 for line in source.splitlines() if line.strip())

    @staticmethod
    def _parse_range_spec(range_spec: str, total_lines: int) -> tuple[int, int]:
        """Parse line range forms like 42-118 or L42-L118."""
        s = range_spec.strip()
        m = re.fullmatch(r"L?(\d+)\s*-\s*L?(\d+)", s, flags=re.IGNORECASE)
        if m is None:
            raise ValueError("range must be in form 'start-end' or 'Lstart-Lend'")
        start = int(m.group(1))
        end = int(m.group(2))
        if start < 1 or end < 1:
            raise ValueError("range lines must be >= 1")
        if start > end:
            raise ValueError("range start must be <= end")
        return min(start, total_lines), min(end, total_lines)

    @staticmethod
    def _token_savings(full_text: str, returned_text: str) -> int:
        full_tokens = max(1, _count_tokens(full_text))
        returned_tokens = max(1, _count_tokens(returned_text))
        return max(0, full_tokens - returned_tokens)

    def _outline_for(
        self, path: Path, source: str, language: str, *, effective_loc: int
    ) -> FileOutline:
        if language == "python":
            base = python_outline(str(path), source)
        else:
            lang = "javascript" if language == "javascript" else "typescript"
            base = typescript_outline(str(path), source, lang=lang)
        return base.model_copy(update={"loc": effective_loc})

    # Generic outline fallback for languages without a dedicated AST builder.
    # Strategy: keep lines that look structural — column-0 declarations,
    # signature-shaped keywords, Markdown headings, import/use/package lines.
    # Imperfect, but vastly smaller than the full source for typical files.
    _GENERIC_LEADING_KEYWORDS: frozenset[str] = frozenset(
        {
            # declarations
            "def",
            "class",
            "func",
            "function",
            "fn",
            "struct",
            "enum",
            "trait",
            "impl",
            "type",
            "interface",
            "record",
            "object",
            "protocol",
            "extension",
            "mod",
            "module",
            "package",
            "namespace",
            "actor",
            # imports / linkage
            "import",
            "from",
            "use",
            "using",
            "require",
            "include",
            "export",
            "extern",
            # visibility-leading (the body keyword follows)
            "pub",
            "public",
            "private",
            "protected",
            "internal",
            "open",
            "sealed",
            "abstract",
            "final",
            "static",
            "async",
            # value-decls (Rust/Go/TS const)
            "const",
            "let",
            "var",
            # SQL DDL
            "create",
            "alter",
            "drop",
        }
    )

    @classmethod
    def _generic_outline_text(cls, source: str, language: str) -> str:
        """Extract a structural skeleton from any language. Returns plain text."""
        if language == "markdown":
            # For Markdown: keep heading lines + the first non-blank line under each.
            kept: list[str] = []
            prev_was_heading = False
            for raw in source.splitlines():
                stripped = raw.strip()
                if stripped.startswith("#"):
                    kept.append(raw.rstrip())
                    prev_was_heading = True
                elif prev_was_heading and stripped:
                    kept.append(raw.rstrip())
                    prev_was_heading = False
            return "\n".join(kept)

        kept_lines: list[str] = []
        for raw in source.splitlines():
            stripped = raw.lstrip()
            if not stripped:
                continue
            # Column-0 declarations (most languages)
            if raw == stripped:
                kept_lines.append(raw.rstrip())
                continue
            # Indented but signature-looking
            first_token = stripped.split(None, 1)[0].rstrip(":(<{").lower()
            if first_token in cls._GENERIC_LEADING_KEYWORDS:
                kept_lines.append(raw.rstrip())
        return "\n".join(kept_lines)

    def smart_read(
        self,
        path: str | Path,
        *,
        range_spec: str | None = None,
        expand: bool = False,
        outline_threshold: int = 200,
    ) -> dict[str, Any]:
        """Read file in full/range/outline mode with token-savings accounting."""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"file not found: {file_path}")

        source = file_path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        language = self._language_for(file_path)
        effective_loc = self._effective_loc(source, language)

        cache_hit = self._index.get(file_path) is not None
        if not cache_hit:
            self.summarize_file(file_path, cache_enabled=True)

        result: dict[str, Any] = {
            "path": str(file_path),
            "language": language,
            "loc": effective_loc,
            "cache_hit": cache_hit,
        }

        if range_spec:
            start, end = self._parse_range_spec(range_spec, len(lines))
            content = "\n".join(lines[start - 1 : end])
            result.update(
                {
                    "mode": "range",
                    "range": f"{start}-{end}",
                    "content": content,
                    "tokens_saved": self._token_savings(source, content),
                }
            )
            return result

        # Per-language AST outline (python / typescript / javascript)
        if (
            not expand
            and effective_loc > outline_threshold
            and language in {"python", "typescript", "javascript"}
        ):
            outline = self._outline_for(
                file_path,
                source,
                language,
                effective_loc=effective_loc,
            )
            outline_json = json.dumps(outline.model_dump(mode="json"), ensure_ascii=False)
            result.update(
                {
                    "mode": "outline",
                    "outline": outline.model_dump(mode="json"),
                    "tokens_saved": self._token_savings(source, outline_json),
                }
            )
            return result

        # Tree-sitter outline for languages with a per-grammar config.
        # Same 25% guard as generic: if the structural extraction doesn't
        # save at least a quarter, fall through to the next stage rather than
        # ship a fake savings event.
        if not expand and effective_loc > outline_threshold:
            from .treesitter_ast import SUPPORTED_LANGUAGES
            from .treesitter_ast import outline_text as ts_outline_text

            if language in SUPPORTED_LANGUAGES:
                ts_text = ts_outline_text(language, source)
                if ts_text and len(ts_text) <= int(len(source) * 0.75):
                    result.update(
                        {
                            "mode": "outline",
                            "outline": {
                                "kind": "treesitter",
                                "language": language,
                                "text": ts_text,
                            },
                            "tokens_saved": self._token_savings(source, ts_text),
                        }
                    )
                    return result

        # Generic regex-based outline fallback for languages without a
        # tree-sitter config or where the tree-sitter outline didn't earn the
        # 25% bar. Same 25% safety guard so we never ship fake savings.
        if not expand and effective_loc > outline_threshold and language != "text":
            outline_text = self._generic_outline_text(source, language)
            if outline_text and len(outline_text) <= int(len(source) * 0.75):
                result.update(
                    {
                        "mode": "outline",
                        "outline": {"kind": "generic", "language": language, "text": outline_text},
                        "tokens_saved": self._token_savings(source, outline_text),
                    }
                )
                return result

        result.update(
            {
                "mode": "full",
                "content": source,
                "tokens_saved": self._token_savings(source, source),
            }
        )
        return result

    def summarize_file(
        self,
        path: str | Path,
        *,
        max_lines: int = 120,
        cache_enabled: bool = True,
    ) -> SemanticSummary:
        """Analyse a file and cache the result (by SHA-256 content hash)."""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"file not found: {file_path}")

        # Serve from cache if unchanged
        if cache_enabled:
            cached = self._index.get(file_path)
            if cached:
                return self._entry_to_summary(cached)

        source = file_path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        language = self._language_for(file_path)

        symbol_details: list[dict[str, Any]] = []
        symbols: list[str] = []
        exports: list[str] = []
        imports_modules: list[str] = []
        dependency_map: list[str] = []
        ast_summary = "ast:unsupported"
        module_docstring = ""
        complexity_score = 0
        git_last_commit = ""
        git_last_author_date = ""

        if language == "python":
            sym_infos, imp_infos, ast_summary, module_docstring, complexity_score = analyze_python(
                source
            )
            symbols = [s.name for s in sym_infos]
            exports = [s.name for s in sym_infos if s.is_export and not s.is_private]
            symbol_details = [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "lineno": s.lineno,
                    "signature": s.signature,
                    "is_private": s.is_private,
                    "docstring": s.docstring,
                    "decorators": s.decorators,
                    "type_hint": s.type_hint,
                    "complexity": s.complexity,
                }
                for s in sym_infos
            ]
            imports_modules = sorted({i.module for i in imp_infos})
            # Resolve local imports to file paths
            base = file_path.parent
            for imp in imp_infos:
                parts = imp.module.split(".")
                for search_base in [base, base.parent]:
                    candidate = search_base / Path(*parts).with_suffix(".py")
                    if candidate.is_file():
                        dependency_map.append(str(candidate))
                        break
            dependency_map = list(dict.fromkeys(dependency_map))[:15]
            summary_str = stub_function_bodies(source, max_body_lines=2)
            if len(summary_str.splitlines()) > max_lines:
                summary_str = "\n".join(summary_str.splitlines()[:max_lines]) + "\n... [truncated]"

        elif language in ("typescript", "javascript"):
            sym_infos_ts, imp_infos_ts, ast_summary = analyze_typescript(source)
            symbols = [s.name for s in sym_infos_ts]
            exports = [s.name for s in sym_infos_ts if s.is_export]
            symbol_details = [
                {"name": s.name, "kind": s.kind, "lineno": s.lineno, "signature": s.signature}
                for s in sym_infos_ts
            ]
            imports_modules = sorted({i.module for i in imp_infos_ts})
            summary_str = "\n".join(lines[:max_lines])
            if len(lines) > max_lines:
                summary_str += "\n... [truncated]"

        else:
            summary_str = "\n".join(lines[:max_lines])
            if len(lines) > max_lines:
                summary_str += "\n... [truncated]"

        # Find linked test files
        test_files = self._find_test_files(file_path)
        git_last_commit, git_last_author_date = self._git_metadata(file_path)

        payload: dict[str, Any] = {
            "path": str(file_path),
            "language": language,
            "summary": summary_str,
            "symbols": symbols,
            "exports": exports,
            "lines_total": len(lines),
            "ast_summary": ast_summary,
            "symbol_details": symbol_details,
            "imports": imports_modules,
            "dependency_map": dependency_map,
            "test_files": test_files,
            "module_docstring": module_docstring,
            "complexity_score": complexity_score,
            "git_last_commit": git_last_commit,
            "git_last_author_date": git_last_author_date,
        }
        if cache_enabled:
            self._index.put(file_path, payload)
            return self._entry_to_summary(self._index.get(file_path) or payload)
        return self._entry_to_summary(payload)

    @staticmethod
    def _entry_to_summary(entry: dict[str, Any]) -> SemanticSummary:
        return SemanticSummary(
            path=str(entry.get("path", "")),
            language=str(entry.get("language", "text")),
            summary=str(entry.get("summary", "")),
            symbols=list(entry.get("symbols", [])),
            exports=list(entry.get("exports", [])),
            lines_total=int(entry.get("lines_total", 0)),
            ast_summary=str(entry.get("ast_summary", "")),
            content_hash=str(entry.get("content_hash", "")),
            symbol_details=list(entry.get("symbol_details", [])),
            imports=list(entry.get("imports", [])),
            dependency_map=list(entry.get("dependency_map", [])),
            test_files=list(entry.get("test_files", [])),
            module_docstring=str(entry.get("module_docstring", "")),
            complexity_score=int(entry.get("complexity_score", 0)),
            git_last_commit=str(entry.get("git_last_commit", "")),
            git_last_author_date=str(entry.get("git_last_author_date", "")),
        )

    def get_cached(self, path: str | Path) -> SemanticSummary | None:
        """Return cached summary if still valid (hash-match), else None."""
        file_path = Path(path)
        if not file_path.is_file():
            return None
        entry = self._index.get(file_path)
        if entry is None:
            return None
        return self._entry_to_summary(entry)

    def module_summary(self, path: str | Path) -> dict[str, Any]:
        """Return a concise dict suitable for CLI display or LLM injection."""
        s = self.get_cached(path) or self.summarize_file(path)
        return {
            "path": s.path,
            "language": s.language,
            "exports": s.exports,
            "symbols": s.symbols[:50],
            "imports": s.imports,
            "dependency_map": s.dependency_map,
            "test_files": s.test_files,
            "lines_total": s.lines_total,
            "ast_summary": s.ast_summary,
            "module_docstring": s.module_docstring,
            "complexity_score": s.complexity_score,
            "content_hash": s.content_hash,
            "git_last_commit": s.git_last_commit,
            "git_last_author_date": s.git_last_author_date,
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def symbol_search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Find symbols matching query across all cached files."""
        return self._symbol_index.resolve_symbol(query)[:limit]

    def semantic_search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """BM25-ranked full-text search over cached file summaries."""
        return self._symbol_index.bm25_search(query, limit=limit)

    def change_impact(self, path: str | Path) -> dict[str, Any]:
        """Estimate blast radius of modifying a file (uses reverse dep graph)."""
        # Ensure file is in cache
        fp = Path(path)
        if fp.is_file() and not self._index.get(fp):
            self.summarize_file(fp)
        return self._symbol_index.change_impact(str(path))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_test_files(self, file_path: Path) -> list[str]:
        stem = file_path.stem
        if stem.startswith("test_") or stem.endswith("_test"):
            return []
        root = file_path.parent
        for _ in range(4):
            tests_dir = root / "tests"
            if tests_dir.is_dir():
                matches = list(tests_dir.rglob(f"test_{stem}.py")) + list(
                    tests_dir.rglob(f"*{stem}*test*.py")
                )
                return [str(p) for p in matches[:5]]
            root = root.parent
        return []

    def _load(self) -> dict[str, Any]:
        """Return raw index state dict (backward-compat with engine.py)."""
        return self._index._load()

    def _git_metadata(self, file_path: Path) -> tuple[str, str]:
        """Return (last_commit_sha, authored_datetime_iso) for a file."""
        if Repo is None:
            return "", ""
        try:
            repo = Repo(file_path, search_parent_directories=True)
            wtd = repo.working_tree_dir
            assert wtd is not None
            rel_path = str(file_path.resolve().relative_to(wtd))
            commits = list(repo.iter_commits(paths=rel_path, max_count=1))
            if not commits:
                return "", ""
            commit = commits[0]
            return str(commit.hexsha), commit.authored_datetime.isoformat()
        except Exception:
            return "", ""
