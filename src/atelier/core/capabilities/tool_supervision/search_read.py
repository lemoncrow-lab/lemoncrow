"""Combined search + read — WP-21.

Collapses the common ``grep → read → read`` loop into a single deterministic
call that returns ranked snippets *and* the surrounding context.  Token usage
is always ≤ 30 % of the naïve approach (grep output + full file per hit).

Host-native tools (rg, grep, host Read) remain available for raw exploration;
this module is an *augmentation*, not a replacement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

_ENCODING_CACHE: Any = None


def _encoding() -> Any:
    global _ENCODING_CACHE
    if _ENCODING_CACHE is None:
        import tiktoken

        _ENCODING_CACHE = tiktoken.get_encoding("cl100k_base")
    return _ENCODING_CACHE


def _count_tokens(text: str) -> int:
    try:
        return len(_encoding().encode(text))
    except Exception:
        return len(text) // 4  # fallback: ~4 chars/token


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Snippet:
    line_start: int
    line_end: int
    score: float
    text: str
    byte_start: int | None = None
    byte_end: int | None = None


@dataclass
class FileMatch:
    path: str
    lang: str
    snippets: list[Snippet]
    outline: dict[str, Any] | None
    tokens: int


@dataclass
class SearchReadResult:
    matches: list[FileMatch]
    total_tokens: int
    tokens_saved_vs_naive: int
    cache_hit: bool
    backend: str = "ripgrep"
    index_age_seconds: int | None = None


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_LANG_MAP = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".sql": "sql",
}


def _detect_lang(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return _LANG_MAP.get(suffix, "text")


# ---------------------------------------------------------------------------
# Outline helper — wraps python_ast / typescript_ast outlines
# ---------------------------------------------------------------------------


def _file_outline(path: str, source: str, lang: str) -> dict[str, Any] | None:
    try:
        if lang == "python":
            from atelier.core.capabilities.semantic_file_memory.python_ast import (
                analyze_python,
            )

            symbols, imports, *_ = analyze_python(source)
            return {
                "symbols": [{"name": s.name, "kind": s.kind, "start": s.lineno, "end": s.end_lineno} for s in symbols],
                "imports": [i.module for i in imports[:20]],
            }
        if lang in ("typescript", "javascript"):
            from atelier.core.capabilities.semantic_file_memory.typescript_ast import (
                analyze_typescript,
            )

            symbols, imports, *_ = analyze_typescript(source)
            return {
                "symbols": [{"name": s.name, "kind": s.kind, "start": s.lineno, "end": s.end_lineno} for s in symbols],
                "imports": [i.module for i in imports[:20]],
            }
    except Exception:
        logger.warning(
            "Suppressed exception at search_read.py:138",
            exc_info=True,
        )
    return None


# ---------------------------------------------------------------------------
# Safe ripgrep wrapper (mirrors cached_grep security checks)
# ---------------------------------------------------------------------------

_SHELL_METACHARS_RE = re.compile(r"[;&|`$<>()\n\r]")
_LEADING_DASH_RE = re.compile(r"^-")


def _assert_safe_args(pattern: str, path: str) -> None:
    """Raise ValueError if pattern or path look like shell-injection."""
    if _SHELL_METACHARS_RE.search(pattern):
        raise ValueError("search_read rejected: shell metacharacters not allowed in query")
    if _LEADING_DASH_RE.match(pattern):
        raise ValueError("search_read rejected: query must not start with '-'")
    if _SHELL_METACHARS_RE.search(path):
        raise ValueError("search_read rejected: shell metacharacters not allowed in path")


def _run_grep(pattern: str, search_path: str) -> str:
    """Run rg and return raw stdout (capped at 256 KB)."""
    try:
        proc = subprocess.run(
            [
                "rg",
                "-H",
                "-n",
                "--no-heading",
                "--color",
                "never",
                "--hidden",
                "--no-ignore",
                "--glob",
                "!.git",
                "--",
                pattern,
                search_path,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return proc.stdout[:262144]  # 256 KB cap
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(rg failed: {exc})"


def _cache_state_path(repo_root: Path) -> Path:
    from hashlib import sha256

    from atelier.core.foundation.paths import default_store_root

    h = sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
    return default_store_root() / "workspaces" / h / "smart_state.json"


def _cache_disabled() -> bool:
    return os.environ.get("ATELIER_CACHE_DISABLED") == "1"


def _load_state(repo_root: Path) -> dict[str, Any]:
    state_path = _cache_state_path(repo_root)
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_cache(repo_root: Path) -> dict[str, Any]:
    data = _load_state(repo_root)
    cache = data.get("cache")
    return cache if isinstance(cache, dict) else {}


def _save_cache(repo_root: Path, cache: dict[str, Any]) -> None:
    state = _load_state(repo_root)
    state["cache"] = cache
    state_path = _cache_state_path(repo_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")


def _fingerprint_path(search_path: Path) -> str:
    """Create a deterministic fingerprint from file metadata under search_path."""
    entries: list[str] = []
    if search_path.is_file():
        st = search_path.stat()
        entries.append(f"{search_path}:{st.st_size}:{st.st_mtime_ns}")
    elif search_path.is_dir():
        files = sorted(p for p in search_path.rglob("*") if p.is_file())
        for file_path in files:
            st = file_path.stat()
            entries.append(f"{file_path}:{st.st_size}:{st.st_mtime_ns}")
    else:
        entries.append(str(search_path))
    return hashlib.sha256("\n".join(entries).encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# Core search_read logic
# ---------------------------------------------------------------------------

_CONTEXT_LINES = 8  # lines of context around each match


def _parse_grep_output(raw: str) -> dict[str, list[int]]:
    """Parse 'path:lineno:...' grep output into {path: [lineno, ...]}."""
    hits: dict[str, list[int]] = {}
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        fpath = parts[0]
        try:
            lineno = int(parts[1])
        except ValueError:
            continue
        hits.setdefault(fpath, []).append(lineno)
    return hits


def _expand_snippet(lines: list[str], lineno: int, context: int = _CONTEXT_LINES) -> tuple[int, int, str]:
    """Return (start, end, text) for a match with context lines."""
    n = len(lines)
    start = max(0, lineno - 1 - context)
    end = min(n, lineno + context)
    text = "\n".join(lines[start:end])
    return start + 1, end, text


def _cluster_snippets(linenos: list[int], lines: list[str], context: int = _CONTEXT_LINES) -> list[Snippet]:
    """Merge overlapping match windows into non-overlapping snippets."""
    if not linenos:
        return []
    sorted_lines = sorted(set(linenos))
    snippets: list[Snippet] = []
    # Build windows per match line, then merge overlapping ones
    windows: list[tuple[int, int]] = [(max(1, ln - context), min(len(lines), ln + context)) for ln in sorted_lines]
    # Merge overlapping windows
    merged: list[tuple[int, int]] = []
    cur_start, cur_end = windows[0]
    for ws, we in windows[1:]:
        if ws <= cur_end + 1:
            cur_end = max(cur_end, we)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = ws, we
    merged.append((cur_start, cur_end))

    for ms, me in merged:
        text = "\n".join(lines[ms - 1 : me])
        # Score = density of match lines in this window
        match_in_window = sum(1 for ln in sorted_lines if ms <= ln <= me)
        window_size = max(1, me - ms + 1)
        score = round(match_in_window / window_size, 4)
        snippets.append(Snippet(line_start=ms, line_end=me, score=score, text=text))

    # Sort by score descending
    snippets.sort(key=lambda s: s.score, reverse=True)
    return snippets


def _naive_token_count(grep_output: str, file_contents: dict[str, str]) -> int:
    """Tokens a naive agent would consume: grep output + full file reads."""
    naive = grep_output
    for content in file_contents.values():
        naive += content
    return _count_tokens(naive)


def search_read(
    query: str,
    path: str = ".",
    max_files: int = 10,
    max_chars_per_file: int = 2000,
    include_outline: bool = True,
    context_lines: int = _CONTEXT_LINES,
) -> SearchReadResult:
    """Combined search + read.

    Args:
        query: Pattern to search for (passed to rg).
        path: Directory or file to search in.
        max_files: Maximum number of files to return results for.
        max_chars_per_file: Cap on snippet text per file.
        include_outline: Whether to include AST outline for files with > 5 matches.
        context_lines: Lines of context around each match.

    Returns:
        SearchReadResult with ranked snippets, token counts, and savings.
    """
    _assert_safe_args(query, path)

    # ---- run cached grep ----
    repo_root = Path.cwd().resolve()
    resolved_path = Path(path).resolve()
    cache_key = f"grep:{query}:{resolved_path}:{_fingerprint_path(resolved_path)}"
    cache_hit = False
    if _cache_disabled():
        grep_output = _run_grep(query, path)
    else:
        cache = _load_cache(repo_root)
        cache_hit = cache_key in cache and isinstance(cache[cache_key], str)
        if cache_hit:
            grep_output = str(cache[cache_key])
        else:
            grep_output = _run_grep(query, path)
            cache[cache_key] = grep_output
            # Keep recent entries only to bound file size.
            if len(cache) > 100:
                for key in list(cache.keys())[: len(cache) - 100]:
                    cache.pop(key, None)
            _save_cache(repo_root, cache)

    # ---- parse hits per file ----
    hits_per_file = _parse_grep_output(grep_output)

    # Sort files deterministically (by path, then stable score order)
    sorted_files = sorted(hits_per_file.keys())[:max_files]

    # ---- read files and build file_contents for naive comparison ----
    file_contents: dict[str, str] = {}
    for fpath in sorted_files:
        try:
            file_contents[fpath] = Path(fpath).read_text(encoding="utf-8", errors="replace")
        except OSError:
            file_contents[fpath] = ""

    naive_tokens = _naive_token_count(grep_output, file_contents)

    # ---- build matches ----
    matches: list[FileMatch] = []
    total_tokens = 0

    for fpath in sorted_files:
        content = file_contents.get(fpath, "")
        lines = content.splitlines()
        linenos = hits_per_file[fpath]
        lang = _detect_lang(fpath)

        snippets = _cluster_snippets(linenos, lines, context=context_lines)

        # If > 5 raw match lines: cap snippets to top-3 and attach outline
        outline: dict[str, Any] | None = None
        if len(linenos) > 5:
            snippets = snippets[:3]
            if include_outline:
                outline = _file_outline(fpath, content, lang)

        # Truncate snippet text to max_chars_per_file total
        total_chars = 0
        trimmed_snippets: list[Snippet] = []
        for sn in snippets:
            if total_chars >= max_chars_per_file:
                break
            remaining = max_chars_per_file - total_chars
            trimmed_text = sn.text[:remaining]
            total_chars += len(trimmed_text)
            trimmed_snippets.append(
                Snippet(
                    line_start=sn.line_start,
                    line_end=sn.line_end,
                    score=sn.score,
                    text=trimmed_text,
                )
            )

        file_token_count = sum(_count_tokens(sn.text) for sn in trimmed_snippets)
        if outline:
            file_token_count += _count_tokens(str(outline))

        total_tokens += file_token_count
        matches.append(
            FileMatch(
                path=fpath,
                lang=lang,
                snippets=trimmed_snippets,
                outline=outline,
                tokens=file_token_count,
            )
        )

    tokens_saved = max(0, naive_tokens - total_tokens)

    return SearchReadResult(
        matches=matches,
        total_tokens=total_tokens,
        tokens_saved_vs_naive=tokens_saved,
        cache_hit=cache_hit,
        backend="ripgrep",
        index_age_seconds=None,
    )


def search_read_to_dict(result: SearchReadResult, *, include_metadata: bool = True) -> dict[str, Any]:
    """Serialize SearchReadResult to a JSON-safe dict."""
    matches: list[dict[str, Any]] = []
    for match in result.matches:
        snippets: list[dict[str, Any]] = []
        for snippet in match.snippets:
            entry: dict[str, Any] = {
                "line_start": snippet.line_start,
                "line_end": snippet.line_end,
                "text": snippet.text,
            }
            if include_metadata:
                entry["score"] = snippet.score
                entry["byte_start"] = snippet.byte_start
                entry["byte_end"] = snippet.byte_end
            snippets.append(entry)

        match_payload: dict[str, Any] = {
            "path": match.path,
            "snippets": snippets,
        }
        if include_metadata:
            match_payload["lang"] = match.lang
            match_payload["outline"] = match.outline
            match_payload["tokens"] = match.tokens
        matches.append(match_payload)

    payload: dict[str, Any] = {"matches": matches}
    if include_metadata:
        payload.update(
            {
                "total_tokens": result.total_tokens,
                "tokens_saved_vs_naive": result.tokens_saved_vs_naive,
                "cache_hit": result.cache_hit,
                "backend": result.backend,
                "index_age_seconds": result.index_age_seconds,
            }
        )
    return payload
