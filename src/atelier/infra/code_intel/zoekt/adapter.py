"""Routing adapter for large-repo text search on the existing search stack."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from atelier.core.capabilities.tool_supervision.search_read import (
    FileMatch,
    SearchReadResult,
    Snippet,
    _count_tokens,
    _detect_lang,
    _file_outline,
)

from .binary import ZoektBinaryResolution, discover_zoekt_binary
from .client import ZoektClient, ZoektFileResult
from .indexer import ZoektIndexer
from .server import ZoektServer, get_zoekt_server, reset_zoekt_servers

_DEFAULT_LOC_THRESHOLD = 500_000
_NOISE_PATH_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        "benchmarks",
        "deleted",
        "dist",
        "docs-archive",
        "exports",
        "fixtures",
        "node_modules",
        "reports",
        "tests",
    }
)
_SOURCE_PATH_PARTS = ("src", "atelier")
_SUPERVISORS: dict[str, ZoektSupervisor] = {}
_SUPERVISORS_LOCK = threading.Lock()


@dataclass(frozen=True)
class ZoektBackendHealth:
    ok: bool
    backend: str
    binary_path: str | None
    index_age_seconds: int | None
    reason: str | None = None


class ZoektSupervisor:
    """Session-scoped lifecycle owner for the search backend."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self._binary_resolution: ZoektBinaryResolution | None = None
        self._client: ZoektClient | None = None
        self._indexer = ZoektIndexer(self.repo_root)
        self._lock = threading.Lock()

    @property
    def server(self) -> ZoektServer:
        return get_zoekt_server(self.repo_root, resolution=self._binary_resolution)

    def threshold_lines(self) -> int:
        raw = os.environ.get("ATELIER_ZOEKT_LOC_THRESHOLD", "").strip()
        if not raw:
            return _DEFAULT_LOC_THRESHOLD
        try:
            return max(1, int(raw))
        except ValueError:
            return _DEFAULT_LOC_THRESHOLD

    def should_route(self, search_path: str | Path) -> bool:
        return self._indexer.line_count(search_path) >= self.threshold_lines()

    def health(self) -> ZoektBackendHealth:
        resolution = discover_zoekt_binary(self.repo_root)
        self._binary_resolution = resolution
        if not resolution.available or resolution.path is None:
            return ZoektBackendHealth(
                ok=False,
                backend="zoekt",
                binary_path=None,
                index_age_seconds=None,
                reason=resolution.reason,
            )
        try:
            self.ensure_started()
            server_health = self.server.health()
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            runtime_ref = resolution.image_ref or (str(resolution.path) if resolution.path is not None else None)
            return ZoektBackendHealth(
                ok=False,
                backend="zoekt",
                binary_path=runtime_ref,
                index_age_seconds=None,
                reason=str(exc),
            )
        return ZoektBackendHealth(
            ok=server_health.ok,
            backend=server_health.backend,
            binary_path=server_health.binary_path,
            index_age_seconds=server_health.index_age_seconds,
        )

    def ensure_started(self) -> ZoektClient:
        with self._lock:
            if self._client is not None:
                return self._client
            resolution = discover_zoekt_binary(self.repo_root)
            if not resolution.available:
                raise RuntimeError(resolution.reason or "zoekt binary unavailable")
            self._binary_resolution = resolution
            server = get_zoekt_server(self.repo_root, resolution=resolution)
            server.ensure_started()
            self._client = ZoektClient(server)
            return self._client

    def search(
        self,
        *,
        query: str,
        search_path: str | Path,
        max_files: int,
        max_chars_per_file: int,
        include_outline: bool,
        result_mode: Literal["compact", "expanded"] = "compact",
        context_lines: int | None = None,
        max_snippets_per_file: int | None = None,
        skip_noise: bool = True,
        prefer_source: bool = True,
    ) -> SearchReadResult:
        client = self.ensure_started()
        rel_glob = _path_to_glob(self.repo_root, Path(search_path).resolve())
        raw_limit = max(max_files * 4, max_files, 20)
        raw_matches = client.search(query, num_matches=raw_limit, file_glob=rel_glob)
        reranked = _rank_zoekt_file_results(
            query,
            raw_matches,
            skip_noise=skip_noise,
            prefer_source=prefer_source,
        )
        if skip_noise and not reranked:
            reranked = _rank_zoekt_file_results(
                query,
                raw_matches,
                skip_noise=False,
                prefer_source=prefer_source,
            )
        selected = reranked[:max_files]
        resolved_context_lines = 0 if context_lines is None and result_mode == "compact" else 2
        if context_lines is not None:
            resolved_context_lines = max(0, context_lines)
        resolved_snippet_cap = max_snippets_per_file
        if resolved_snippet_cap is None:
            resolved_snippet_cap = 1 if result_mode == "compact" else 3
        resolved_snippet_cap = max(1, resolved_snippet_cap)

        file_matches: list[FileMatch] = []
        total_tokens = 0
        naive_tokens = 0
        for _, file_match in selected:
            rel_path = _normalize_zoekt_path(file_match.path)
            abs_path = self.repo_root / rel_path
            lang = _detect_lang(rel_path)
            raw_line_text = "\n".join(match.line_text for match in file_match.matches if match.line_text)
            naive_tokens += _count_tokens(rel_path) + _count_tokens(raw_line_text)
            content = ""
            lines: list[str] = []
            needs_file = resolved_context_lines > 0 or include_outline
            if needs_file:
                try:
                    content = abs_path.read_text(encoding="utf-8", errors="replace")
                    lines = content.splitlines()
                except OSError:
                    lines = []
            snippets: list[Snippet] = []
            total_chars = 0
            seen_lines: set[int] = set()
            for index, raw in enumerate(file_match.matches):
                if len(snippets) >= resolved_snippet_cap or total_chars >= max_chars_per_file:
                    break
                if raw.line_number in seen_lines:
                    continue
                seen_lines.add(raw.line_number)
                if lines and resolved_context_lines > 0:
                    line_start = max(1, raw.line_number - resolved_context_lines)
                    line_end = min(len(lines), raw.line_number + resolved_context_lines)
                    snippet_text = "\n".join(lines[line_start - 1 : line_end])
                else:
                    line_start = raw.line_number
                    line_end = raw.line_number
                    snippet_text = raw.line_text.strip()
                snippet_text = " ".join(snippet_text.split()) if result_mode == "compact" else snippet_text
                remaining = max_chars_per_file - total_chars
                trimmed_text = snippet_text[:remaining]
                if not trimmed_text:
                    continue
                total_chars += len(trimmed_text)
                snippets.append(
                    Snippet(
                        line_start=line_start,
                        line_end=line_end,
                        score=max(0.1, 1.0 - (index * 0.1)),
                        text=trimmed_text,
                        byte_start=raw.byte_start,
                        byte_end=raw.byte_end,
                    )
                )
            outline = None
            if include_outline and len(file_match.matches) > 5 and content:
                outline = _file_outline(str(abs_path), content, lang)
            file_tokens = _count_tokens(rel_path) + sum(_count_tokens(snippet.text) for snippet in snippets)
            if outline is not None:
                file_tokens += _count_tokens(str(outline))
            total_tokens += file_tokens
            file_matches.append(
                FileMatch(
                    path=rel_path,
                    lang=lang,
                    snippets=snippets,
                    outline=outline,
                    tokens=file_tokens,
                )
            )
        health = self.health()
        return SearchReadResult(
            matches=file_matches,
            total_tokens=total_tokens,
            tokens_saved_vs_naive=max(0, naive_tokens - total_tokens),
            cache_hit=False,
            backend="zoekt",
            index_age_seconds=health.index_age_seconds,
        )


def _normalize_zoekt_path(path: str) -> str:
    return Path(path).as_posix().lstrip("/")


def _is_noise_path(path: str) -> bool:
    parts = set(Path(path).parts)
    if parts & _NOISE_PATH_PARTS:
        return True
    return any(part.startswith(".") and part not in {"."} for part in parts)


def _source_path_score(path: str) -> float:
    parts = Path(path).parts
    if len(parts) >= 2 and parts[:2] == _SOURCE_PATH_PARTS:
        return 2.0
    if parts and parts[0] == "src":
        return 1.0
    return 0.0


def _query_score(query: str, path: str, line_texts: list[str]) -> float:
    needle = query.lower().strip()
    if not needle:
        return 0.0
    score = 0.0
    path_lower = path.lower()
    if needle in path_lower:
        score += 2.0
    for text in line_texts[:5]:
        lowered = text.lower()
        if needle in lowered:
            score += 1.0
        if f"def {needle}" in lowered or f"class {needle}" in lowered:
            score += 2.0
    return score


def _rank_zoekt_file_results(
    query: str,
    raw_matches: list[ZoektFileResult],
    *,
    skip_noise: bool,
    prefer_source: bool,
) -> list[tuple[float, ZoektFileResult]]:
    ranked: list[tuple[float, ZoektFileResult]] = []
    for index, file_match in enumerate(raw_matches):
        path = _normalize_zoekt_path(file_match.path)
        if not path:
            continue
        if skip_noise and _is_noise_path(path):
            continue
        matches = file_match.matches
        line_texts = [match.line_text for match in matches]
        score = _query_score(query, path, line_texts)
        if prefer_source:
            score += _source_path_score(path)
        score += min(len(matches), 5) * 0.05
        score -= index * 0.001
        ranked.append((score, file_match))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def get_zoekt_supervisor(repo_root: str | Path) -> ZoektSupervisor:
    root = str(Path(repo_root).resolve())
    with _SUPERVISORS_LOCK:
        supervisor = _SUPERVISORS.get(root)
        if supervisor is None:
            supervisor = ZoektSupervisor(root)
            _SUPERVISORS[root] = supervisor
        return supervisor


def reset_zoekt_supervisors() -> None:
    with _SUPERVISORS_LOCK:
        _SUPERVISORS.clear()
    reset_zoekt_servers()


def _path_to_glob(repo_root: Path, search_path: Path) -> str | None:
    if search_path == repo_root:
        return None
    try:
        rel_path = search_path.relative_to(repo_root).as_posix()
    except ValueError:
        return None
    return rel_path if search_path.is_file() else f"{rel_path}/**"


__all__ = [
    "ZoektBackendHealth",
    "ZoektSupervisor",
    "get_zoekt_supervisor",
    "reset_zoekt_supervisors",
]
