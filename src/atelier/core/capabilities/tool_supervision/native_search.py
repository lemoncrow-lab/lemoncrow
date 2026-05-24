"""Atelier-native combined file search/read capability."""

from __future__ import annotations

import ast
import base64
import json
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

SearchOutputMode = Literal[
    "ranked_file_map",
    "file_paths_with_content",
    "file_paths_only",
    "file_paths_with_match_count",
]

MAX_STRUCTURED_OUTPUT_CHARS = 80_000
DEFAULT_CONTEXT_BUDGET_TOKENS = 6_000
INLINE_CHARS_PER_TOKEN = 2
SKIP_DIRS: frozenset[str] = frozenset(
    {
        # VCS
        ".git",
        # Atelier internals
        ".atelier",
        # Python
        ".venv",
        "venv",
        ".tox",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        # JS/TS
        "node_modules",
        ".next",
        ".nuxt",
        ".turbo",
        ".svelte-kit",
        # Build outputs
        "dist",
        "build",
        "out",
        "target",
        # Coverage
        "coverage",
        ".nyc_output",
    }
)
_SKIP_DIRS = SKIP_DIRS  # backwards-compat alias
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_PDF_SUFFIXES = {".pdf"}
_TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".sql",
    ".sh",
    ".css",
    ".html",
    ".ipynb",
    ".csv",
    ".tsv",
}
_TYPE_ALIASES = {
    "python": ["**/*.py"],
    "py": ["**/*.py"],
    "typescript": ["**/*.ts", "**/*.tsx"],
    "ts": ["**/*.ts", "**/*.tsx"],
    "javascript": ["**/*.js", "**/*.jsx"],
    "js": ["**/*.js", "**/*.jsx"],
    "markdown": ["**/*.md"],
    "md": ["**/*.md"],
    "sql": ["**/*.sql"],
    "json": ["**/*.json"],
    "yaml": ["**/*.yaml", "**/*.yml"],
    "notebook": ["**/*.ipynb"],
    "image": ["**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.webp", "**/*.bmp"],
    "pdf": ["**/*.pdf"],
}


@dataclass(frozen=True)
class PatternSpec:
    pattern: str
    start_line: int | None = None
    end_line: int | None = None
    graph_mode: Literal["imports", "imported_by"] | None = None


@dataclass(frozen=True)
class RankedMatch:
    file: str
    score: float
    match_count: int
    ranges: list[tuple[int, int]]
    symbols: list[str]
    why: str


def _repo_root(repo_root: str | Path | None = None) -> Path:
    return Path(repo_root or Path.cwd()).resolve()


def _safe_resolve(root: Path, raw_path: str | Path) -> Path:
    path = Path(raw_path)
    resolved = path if path.is_absolute() else root / path
    resolved = resolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escape denied: {raw_path}") from exc
    return resolved


def _has_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[]{}")


def _parse_pattern(pattern: str) -> PatternSpec:
    graph_mode: Literal["imports", "imported_by"] | None = None
    if pattern.endswith("#imports"):
        pattern = pattern[: -len("#imports")]
        graph_mode = "imports"
    elif pattern.endswith("#imported-by"):
        pattern = pattern[: -len("#imported-by")]
        graph_mode = "imported_by"

    match = re.search(r"#(\d+)(?:-(\d+))?$", pattern)
    if not match:
        return PatternSpec(pattern=pattern, graph_mode=graph_mode)
    start_line = int(match.group(1))
    end_line = int(match.group(2) or match.group(1))
    return PatternSpec(
        pattern=pattern[: match.start()],
        start_line=start_line,
        end_line=end_line,
        graph_mode=graph_mode,
    )


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _iter_files(
    root: Path, base: Path, patterns: list[PatternSpec], type_alias: str | None
) -> list[tuple[Path, PatternSpec]]:
    expanded = list(patterns)
    if type_alias:
        expanded.extend(PatternSpec(pattern=item) for item in _TYPE_ALIASES.get(type_alias.lower(), []))
    if not expanded and base.is_file():
        expanded.append(PatternSpec(pattern=str(base.relative_to(root))))
    elif not expanded and base.is_dir():
        # No glob/type specified — walk all files under base
        fallback_spec = PatternSpec(pattern=".")
        found: dict[Path, PatternSpec] = {}
        for item in base.rglob("*"):
            if item.is_file() and not any(part in _SKIP_DIRS for part in item.parts):
                found.setdefault(item.resolve(), fallback_spec)
        return sorted(found.items(), key=lambda pair: str(pair[0]))

    candidates: dict[Path, PatternSpec] = {}
    for spec in expanded:
        raw = spec.pattern or "."
        if _has_glob(raw):
            matches = base.glob(raw) if not Path(raw).is_absolute() else Path("/").glob(raw.lstrip("/"))
            for match in matches:
                if match.is_file() and not any(part in _SKIP_DIRS for part in match.parts):
                    candidates.setdefault(match.resolve(), spec)
            continue

        candidate = _safe_resolve(root, raw)
        if candidate.is_file():
            candidates.setdefault(candidate, spec)
            continue
        if candidate.is_dir():
            for item in candidate.rglob("*"):
                if item.is_file() and not any(part in _SKIP_DIRS for part in item.parts):
                    candidates.setdefault(item.resolve(), spec)

    return sorted(candidates.items(), key=lambda pair: str(pair[0]))


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_SUFFIXES or path.suffix.lower() not in _IMAGE_SUFFIXES | _PDF_SUFFIXES


def _line_window(lines: list[str], line_no: int, before: int, after: int) -> tuple[int, int, list[str]]:
    start = max(1, line_no - before)
    end = min(len(lines), line_no + after)
    return start, end, lines[start - 1 : end]


def _truncate_line(line: str, max_line_length: int | None) -> str:
    if not max_line_length or max_line_length <= 0:
        return line
    return line if len(line) <= max_line_length else line[:max_line_length] + "..."


def _spill_dir() -> Path:
    configured = os.environ.get("ATELIER_MCP_SPILL_DIR")
    if configured:
        path = Path(configured).expanduser().resolve()
    else:
        path = Path(tempfile.gettempdir()) / "atelier-mcp-spill"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _spill_response_payload(payload: dict[str, Any]) -> Path:
    spill_path = _spill_dir() / f"search-{int(time.time() * 1000)}.json"
    spill_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return spill_path


def _python_summary(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    rows: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rows.append(f"{type(node).__name__}: {node.name} @ line {node.lineno}")
    return "\n".join(rows)


def _js_summary(source: str) -> str:
    rows: list[str] = []
    pattern = re.compile(
        r"(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^=]*?=>"
    )
    for idx, line in enumerate(source.splitlines(), start=1):
        match = pattern.search(line)
        if match:
            rows.append(f"symbol: {match.group(1) or match.group(2)} @ line {idx}")
    return "\n".join(rows[:200])


def _summarize(path: Path, source: str) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _python_summary(source)
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return _js_summary(source)
    return ""


def _compact_notebook(source: str) -> str:
    try:
        notebook = json.loads(source)
    except Exception:
        return source[:4000]
    rows: list[str] = []
    for idx, cell in enumerate(notebook.get("cells", [])):
        cell_type = cell.get("cell_type", "cell")
        raw_source = cell.get("source", "")
        text = "".join(raw_source) if isinstance(raw_source, list) else str(raw_source)
        rows.append(f"# cell {idx} ({cell_type})")
        rows.append(text[:2000])
        if cell_type == "code" and cell.get("outputs"):
            rows.append("# outputs: cached/read-only; cleared when source changes")
    return "\n".join(rows)


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return f"[PDF text extraction unavailable: install pypdf to read {path.name}]"
    try:
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        return f"[PDF text extraction failed: {exc}]"


def _imports_for(path: Path, source: str) -> list[str]:
    imports: list[str] = []
    if path.suffix.lower() == ".py":
        for match in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", source, flags=re.M):
            imports.append(match.group(1) or match.group(2) or "")
    else:
        for match in re.finditer(
            r"(?:from\s+['\"]([^'\"]+)['\"]|import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)|require\(\s*['\"]([^'\"]+)['\"]\s*\))",
            source,
        ):
            imports.append(next(group for group in match.groups() if group))
    return [item for item in imports if item]


def _imported_by_for(root: Path, target: Path, candidates: list[tuple[Path, PatternSpec]]) -> list[str]:
    rel_target = str(target.relative_to(root)) if target.is_relative_to(root) else str(target)
    stem = target.stem
    module = rel_target.removesuffix(".py").replace("/", ".")
    target_js = rel_target.removesuffix(".js")
    imported_by: list[str] = []
    for candidate, _spec in candidates:
        if candidate == target or not candidate.is_file():
            continue
        try:
            source = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        imports = _imports_for(candidate, source)
        hit = False
        for item in imports:
            if item == module or item.endswith(f".{stem}") or item.endswith(f"/{stem}") or item.endswith(target_js):
                hit = True
                break
        if not hit:
            continue
        rel = str(candidate.relative_to(root)) if candidate.is_relative_to(root) else str(candidate)
        imported_by.append(rel)
    return sorted(imported_by)


def _looks_plain_identifier_query(content_regex: str | None) -> bool:
    if not content_regex:
        return False
    if re.search(r"[.^$*+?{}\[\]|()\\]", content_regex):
        return False
    return bool(re.search(r"[A-Za-z0-9]", content_regex))


def _query_variants(content_regex: str | None) -> list[str]:
    if not _looks_plain_identifier_query(content_regex):
        return [content_regex] if content_regex else []
    assert content_regex is not None
    base = content_regex.strip()
    tokens = re.split(r"[\s_-]+", base)
    cleaned = [tok for tok in tokens if tok]
    if not cleaned:
        return [base]
    snake = "_".join(tok.lower() for tok in cleaned)
    kebab = "-".join(tok.lower() for tok in cleaned)
    camel = cleaned[0].lower() + "".join(tok.capitalize() for tok in cleaned[1:])
    pascal = "".join(tok.capitalize() for tok in cleaned)
    variants = [base, snake, kebab, camel, pascal, base.replace(" ", "_"), base.replace(" ", "-")]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in variants:
        if item and item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _find_symbol_spans(path: Path, source: str) -> list[tuple[int, int, str]]:
    suffix = path.suffix.lower()
    spans: list[tuple[int, int, str]] = []
    if suffix == ".py":
        for match in re.finditer(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)", source, flags=re.M):
            name = match.group(1)
            start = source.count("\n", 0, match.start()) + 1
            spans.append((start, start, name))
    elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
        for match in re.finditer(
            r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)",
            source,
            flags=re.M,
        ):
            name = match.group(1)
            start = source.count("\n", 0, match.start()) + 1
            spans.append((start, start, name))

    if not spans:
        return spans
    lines = source.splitlines()
    finalized: list[tuple[int, int, str]] = []
    starts = [line for line, _end, _name in spans]
    for idx, (start, _end, name) in enumerate(spans):
        next_start = starts[idx + 1] if idx + 1 < len(starts) else len(lines) + 1
        end = max(start, next_start - 1)
        finalized.append((start, end, name))
    return finalized


def _match_line_numbers(
    lines: list[str],
    regex: re.Pattern[str] | None,
    content_regex: str | None,
    *,
    include_all_when_no_regex: bool,
) -> list[int]:
    if regex is not None:
        return [idx for idx, line in enumerate(lines, start=1) if regex.search(line)]
    if include_all_when_no_regex:
        return list(range(1, len(lines) + 1))
    variants = _query_variants(content_regex)
    if not variants:
        return []
    compiled = [re.compile(re.escape(item), re.I) for item in variants]
    out: list[int] = []
    for idx, line in enumerate(lines, start=1):
        if any(pat.search(line) for pat in compiled):
            out.append(idx)
    return out


def _merge_ranges(ranges: list[tuple[int, int]], *, gap: int = 3) -> list[tuple[int, int]]:
    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int]] = [sorted_ranges[0]]
    for start, end in sorted_ranges[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + gap:
            merged[-1] = (prev_start, max(prev_end, end))
            continue
        merged.append((start, end))
    return merged


def _symbol_windows(
    path: Path,
    lines: list[str],
    source: str,
    line_nos: list[int],
    *,
    lines_before: int,
    lines_after: int,
) -> tuple[list[tuple[int, int]], list[str]]:
    symbols = _find_symbol_spans(path, source)
    windows: list[tuple[int, int]] = []
    symbol_hits: list[str] = []
    for line_no in line_nos:
        matched_symbol = None
        for start, end, name in symbols:
            if start <= line_no <= end:
                matched_symbol = (start, end, name)
                break
        if matched_symbol is not None:
            start, end, name = matched_symbol
            windows.append((start, end))
            symbol_hits.append(name)
            continue
        start = max(1, line_no - lines_before)
        end = min(len(lines), line_no + lines_after)
        windows.append((start, end))
    dedup_symbols: list[str] = []
    seen: set[str] = set()
    for name in symbol_hits:
        if name in seen:
            continue
        seen.add(name)
        dedup_symbols.append(name)
    return _merge_ranges(windows), dedup_symbols


def _render_text_result(
    path: Path,
    root: Path,
    spec: PatternSpec,
    regex: re.Pattern[str] | None,
    content_regex: str | None,
    *,
    output_mode: SearchOutputMode,
    lines_before: int,
    lines_after: int,
    max_line_length: int | None,
    lines_per_file: int | None,
    summary: bool | None,
    if_modified_since: datetime | None,
) -> tuple[str | None, int]:
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    unchanged = if_modified_since is not None and mtime <= if_modified_since
    if unchanged and output_mode == "file_paths_with_content":
        return f"{rel} (unchanged)", 0

    source = (
        _extract_pdf(path)
        if path.suffix.lower() in _PDF_SUFFIXES
        else path.read_text(encoding="utf-8", errors="replace")
    )
    if path.suffix.lower() == ".ipynb":
        source = _compact_notebook(source)

    if spec.graph_mode == "imports":
        imports = _imports_for(path, source)
        return f"{rel}\nimports:\n" + "\n".join(f"- {item}" for item in imports), len(imports)
    if spec.graph_mode == "imported_by":
        # Handled in caller where we have full candidate list context.
        return None, 0

    lines = source.splitlines()
    if spec.start_line is not None:
        start = spec.start_line
        end = spec.end_line or start
        selected = lines[start - 1 : end]
        body = "\n".join(_truncate_line(line, max_line_length) for line in selected)
        return f"{rel}#{start}-{end}\n{body}", len(selected)

    include_all = regex is None and content_regex is None
    match_lines = _match_line_numbers(lines, regex, content_regex, include_all_when_no_regex=include_all)
    if include_all and lines_per_file:
        match_lines = match_lines[: max(0, lines_per_file)]

    if output_mode == "file_paths_only":
        return rel if match_lines or not regex else None, len(match_lines)
    if output_mode == "file_paths_with_match_count":
        return f"{rel}\t{len(match_lines)}", len(match_lines)
    if regex and not match_lines:
        return None, 0

    use_summary = bool(summary)
    # When content_regex is provided, the user explicitly wants line matches.
    # Auto-summary would discard them — skip it.
    if (
        summary is None
        and regex is None
        and path.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx"}
        and len(lines) > 500
    ):
        use_summary = True
    if use_summary:
        outline = _summarize(path, source)
        if outline:
            return f"{rel}\n{outline}", len(match_lines)

    rendered: list[str] = [rel]
    emitted = 0
    windows_seen: set[tuple[int, int]] = set()
    for line_no in match_lines:
        if lines_per_file and lines_per_file > 0 and emitted >= lines_per_file:
            break
        start, end, window = _line_window(lines, line_no, lines_before, lines_after)
        if (start, end) in windows_seen:
            continue
        windows_seen.add((start, end))
        rendered.append(f"@@ {start}-{end}")
        rendered.extend(_truncate_line(line, max_line_length) for line in window)
        emitted += len(window)
    return "\n".join(rendered), len(match_lines)


def search_workspace(
    *,
    path: str = ".",
    content_regex: str | None = None,
    file_glob_patterns: list[str] | None = None,
    output_mode: SearchOutputMode = "file_paths_with_content",
    lines_before: int = 0,
    lines_after: int = 0,
    ignore_case: bool = False,
    type: str | None = None,
    file_limit: int | None = None,
    lines_per_file: int | None = 500,
    if_modified_since: str | None = None,
    max_line_length: int | None = 1000,
    multiline: bool = False,
    summary: bool | None = None,
    repo_root: str | Path | None = None,
    cap_chars: int = MAX_STRUCTURED_OUTPUT_CHARS,
    context_budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """Search and read files in one structured response."""
    if not (content_regex or file_glob_patterns or type or Path(path).is_file()):
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Provide content_regex, file_glob_patterns, or type"}],
        }

    root = _repo_root(repo_root)
    base_spec = _parse_pattern(path)
    base = _safe_resolve(root, base_spec.pattern or ".")
    specs = [_parse_pattern(item) for item in (file_glob_patterns or [])]
    if not specs and base.is_file():
        specs = [base_spec]

    flags = re.I if ignore_case else 0
    if multiline:
        flags |= re.S | re.M
    regex = re.compile(content_regex, flags) if content_regex else None
    since = _parse_when(if_modified_since)
    candidates = _iter_files(root, base if base.is_dir() else root, specs, type)
    limit = file_limit or 100
    blocks: list[dict[str, str]] = []
    total_chars = 0
    effective_cap_chars = cap_chars
    if output_mode == "file_paths_with_content":
        effective_cap_chars = min(cap_chars, max(1000, context_budget_tokens) * 4)
    if output_mode == "ranked_file_map":
        ranked: list[RankedMatch] = []
        total_budget = max(1000, context_budget_tokens)
        for candidate, spec in candidates:
            if len(ranked) >= limit:
                break
            if not _is_text_file(candidate) and candidate.suffix.lower() not in _PDF_SUFFIXES:
                continue
            source = (
                _extract_pdf(candidate)
                if candidate.suffix.lower() in _PDF_SUFFIXES
                else candidate.read_text(encoding="utf-8", errors="replace")
            )
            lines = source.splitlines()
            if spec.graph_mode == "imports":
                imports = _imports_for(candidate, source)
                rel = str(candidate.relative_to(root)) if candidate.is_relative_to(root) else str(candidate)
                ranked.append(
                    RankedMatch(
                        file=rel,
                        score=1.0,
                        match_count=len(imports),
                        ranges=[],
                        symbols=[],
                        why="imports graph requested",
                    )
                )
                continue
            if spec.graph_mode == "imported_by":
                imported = _imported_by_for(root, candidate, candidates)
                rel = str(candidate.relative_to(root)) if candidate.is_relative_to(root) else str(candidate)
                ranked.append(
                    RankedMatch(
                        file=rel,
                        score=1.0,
                        match_count=len(imported),
                        ranges=[],
                        symbols=[],
                        why=f"imported by {len(imported)} files",
                    )
                )
                continue

            line_nos = _match_line_numbers(lines, regex, content_regex, include_all_when_no_regex=regex is None)
            if regex and not line_nos:
                continue
            ranges, symbols = _symbol_windows(
                candidate,
                lines,
                source,
                line_nos,
                lines_before=max(0, lines_before),
                lines_after=max(0, lines_after),
            )
            match_count = len(line_nos)
            rel = str(candidate.relative_to(root)) if candidate.is_relative_to(root) else str(candidate)
            score = float(match_count) + (0.15 * len(symbols))
            ranked.append(
                RankedMatch(
                    file=rel,
                    score=score,
                    match_count=match_count,
                    ranges=ranges,
                    symbols=symbols[:6],
                    why="regex matched symbol-aware ranges" if symbols else "regex matched merged line ranges",
                )
            )

        if not ranked:
            payload: dict[str, Any] = {
                "mode": "ranked_file_map",
                "matches": [],
                "next": [],
            }
            if include_metadata:
                payload["_meta"] = {"fileMatchCount": 0, "capChars": cap_chars}
            return payload

        ranked.sort(key=lambda item: (-item.score, item.file))
        top_score = max(item.score for item in ranked) or 1.0
        normalized = [RankedMatch(**{**item.__dict__, "score": round(item.score / top_score, 3)}) for item in ranked]
        selected: list[RankedMatch] = []
        used_tokens = 0
        for idx, item in enumerate(normalized):
            max_ranges = 4 if idx == 0 else 3 if idx == 1 else 2
            reduced_ranges = item.ranges[:max_ranges]
            est = max(30, len(item.file) // 4 + (len(reduced_ranges) * 24) + (len(item.symbols) * 8))
            if selected and used_tokens + est > total_budget:
                break
            used_tokens += est
            selected.append(RankedMatch(**{**item.__dict__, "ranges": reduced_ranges}))

        handles: dict[str, tuple[str, tuple[int, int] | None]] = {}
        matches_payload: list[dict[str, Any]] = []
        next_actions: list[str] = []
        for idx, item in enumerate(selected, start=1):
            range_text = [f"{start}-{end}" for start, end in item.ranges]
            handle = f"m{idx}"
            handles[handle] = (item.file, item.ranges[0] if item.ranges else None)
            matches_payload.append(
                {
                    "handle": handle,
                    "file": item.file,
                    "score": item.score,
                    "match_count": item.match_count,
                    "ranges": range_text,
                    "symbols": item.symbols,
                    "why": item.why,
                }
            )
            if item.ranges:
                start, end = item.ranges[0]
                next_actions.append(f"read {item.file}#{start}-{end}")
            if item.symbols:
                next_actions.append(f"read {item.file}#{item.symbols[0]}")
        payload = {
            "mode": "ranked_file_map",
            "matches": matches_payload,
            "next": next_actions[: min(12, len(next_actions))],
            "context_budget_tokens": total_budget,
            "handles": {
                k: {"file": v[0], "range": f"{v[1][0]}-{v[1][1]}" if v[1] else None} for k, v in handles.items()
            },
        }
        if include_metadata:
            payload["_meta"] = {"fileMatchCount": len(matches_payload), "capChars": cap_chars}
        return payload

    file_match_count = 0
    for candidate, spec in candidates:
        if len(blocks) >= limit:
            break
        suffix = candidate.suffix.lower()
        if suffix in _IMAGE_SUFFIXES and output_mode == "file_paths_with_content" and regex is None:
            data = base64.b64encode(candidate.read_bytes()).decode("ascii")
            mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            blocks.append({"type": "image", "data": data, "mimeType": mime})
            file_match_count += 1
            continue
        if not _is_text_file(candidate) and suffix not in _PDF_SUFFIXES:
            continue
        if spec.graph_mode == "imported_by":
            imported = _imported_by_for(root, candidate, candidates)
            rel = str(candidate.relative_to(root)) if candidate.is_relative_to(root) else str(candidate)
            rendered = f"{rel}\nimported-by:\n" + "\n".join(f"- {item}" for item in imported)
            file_match_count += 1
            remaining = effective_cap_chars - total_chars
            if remaining <= 0:
                blocks.append({"type": "text", "text": "[truncated: structured output cap reached]"})
                break
            text = rendered[:remaining]
            total_chars += len(text)
            blocks.append({"type": "text", "text": text})
            continue
        _file_rendered, _count = _render_text_result(
            candidate,
            root,
            spec,
            regex,
            content_regex,
            output_mode=output_mode,
            lines_before=max(0, lines_before),
            lines_after=max(0, lines_after),
            max_line_length=max_line_length,
            lines_per_file=lines_per_file,
            summary=summary,
            if_modified_since=since,
        )
        if _file_rendered is None:
            continue
        rendered = _file_rendered
        file_match_count += 1
        remaining = effective_cap_chars - total_chars
        if remaining <= 0:
            blocks.append({"type": "text", "text": "[truncated: structured output cap reached]"})
            break
        text = rendered[:remaining]
        total_chars += len(text)
        blocks.append({"type": "text", "text": text})

    response: dict[str, Any] = {"content": blocks}
    if include_metadata:
        response["_meta"] = {"fileMatchCount": file_match_count, "capChars": effective_cap_chars}
    inline_chars_budget = max(1000, context_budget_tokens) * INLINE_CHARS_PER_TOKEN
    if output_mode == "file_paths_with_content" and total_chars > inline_chars_budget:
        spill_payload = {
            "mode": output_mode,
            "content": blocks,
            "_meta": {
                "fileMatchCount": file_match_count,
                "capChars": effective_cap_chars,
                "inlineChars": total_chars,
                "inlineCharsBudget": inline_chars_budget,
            },
        }
        spill_path = _spill_response_payload(spill_payload)
        preview = ""
        if blocks and isinstance(blocks[0], dict) and blocks[0].get("type") == "text":
            preview = str(blocks[0].get("text", ""))[:240]
        response = {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"[large response spilled] payload saved to {spill_path}. "
                        "Use read on this file to inspect full results."
                    ),
                }
            ],
            "artifact": {
                "path": str(spill_path),
                "format": "json",
                "bytes": spill_path.stat().st_size,
                "preview": preview,
            },
        }
        if include_metadata:
            response["_meta"] = {
                "fileMatchCount": file_match_count,
                "capChars": effective_cap_chars,
                "inlineChars": total_chars,
                "inlineCharsBudget": inline_chars_budget,
                "spilled": True,
            }
    return response


__all__ = ["MAX_STRUCTURED_OUTPUT_CHARS", "SearchOutputMode", "search_workspace"]
