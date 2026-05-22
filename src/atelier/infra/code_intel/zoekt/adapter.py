"""Routing adapter for large-repo text search on the existing search stack."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from atelier.core.capabilities.tool_supervision.search_read import (
    FileMatch,
    SearchReadResult,
    Snippet,
    _count_tokens,
    _detect_lang,
    _file_outline,
)

from .binary import ZoektBinaryResolution, discover_zoekt_binary
from .client import ZoektClient
from .indexer import ZoektIndexer
from .server import ZoektServer, get_zoekt_server, reset_zoekt_servers

_DEFAULT_LOC_THRESHOLD = 500_000
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
    ) -> SearchReadResult:
        client = self.ensure_started()
        rel_glob = _path_to_glob(self.repo_root, Path(search_path).resolve())
        raw_matches = client.search(query, num_matches=max_files, file_glob=rel_glob)
        file_matches: list[FileMatch] = []
        total_tokens = 0
        for file_match in raw_matches:
            abs_path = self.repo_root / file_match.path
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = content.splitlines()
            lang = _detect_lang(file_match.path)
            snippets: list[Snippet] = []
            total_chars = 0
            for index, raw in enumerate(file_match.matches):
                if total_chars >= max_chars_per_file:
                    break
                line_start = max(1, raw.line_number - 2)
                line_end = min(len(lines), raw.line_number + 2) if lines else raw.line_number
                snippet_text = "\n".join(lines[line_start - 1 : line_end])
                remaining = max_chars_per_file - total_chars
                trimmed_text = snippet_text[:remaining]
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
            outline = (
                _file_outline(str(abs_path), content, lang) if include_outline and len(file_match.matches) > 5 else None
            )
            file_tokens = sum(_count_tokens(snippet.text) for snippet in snippets)
            if outline is not None:
                file_tokens += _count_tokens(str(outline))
            total_tokens += file_tokens
            file_matches.append(
                FileMatch(
                    path=str(abs_path),
                    lang=lang,
                    snippets=snippets[:3] if len(file_match.matches) > 5 else snippets,
                    outline=outline,
                    tokens=file_tokens,
                )
            )
        health = self.health()
        naive_tokens = sum(
            _count_tokens(match.path) + _count_tokens("\n".join(snippet.text for snippet in match.snippets))
            for match in file_matches
        )
        return SearchReadResult(
            matches=file_matches,
            total_tokens=total_tokens,
            tokens_saved_vs_naive=max(0, naive_tokens - total_tokens),
            cache_hit=False,
            backend="zoekt",
            index_age_seconds=health.index_age_seconds,
        )


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
