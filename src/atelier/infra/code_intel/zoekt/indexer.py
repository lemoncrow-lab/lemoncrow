"""Repository metrics for Zoekt routing decisions."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from time import time

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
}
_SKIP_PARTS = {".git", ".atelier", ".venv", "node_modules", "dist", "build", "__pycache__"}


@dataclass(frozen=True)
class ZoektIndexSnapshot:
    indexed_at: float
    total_lines: int
    path_lines: dict[str, int]


class ZoektIndexer:
    """Keep lightweight line-count metadata for routing decisions."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self._lock = threading.Lock()
        self._snapshot: ZoektIndexSnapshot | None = None

    def ensure_snapshot(self) -> ZoektIndexSnapshot:
        with self._lock:
            if self._snapshot is None:
                self._snapshot = self._build_snapshot()
            return self._snapshot

    def line_count(self, search_path: str | Path | None = None) -> int:
        snapshot = self.ensure_snapshot()
        if search_path is None:
            return snapshot.total_lines
        target = Path(search_path).resolve()
        if target == self.repo_root:
            return snapshot.total_lines
        prefix = self._relative_path(target)
        if prefix is None:
            return snapshot.total_lines
        if target.is_file():
            return snapshot.path_lines.get(prefix, 0)
        return sum(lines for path, lines in snapshot.path_lines.items() if path.startswith(f"{prefix}/"))

    def index_age_seconds(self) -> int:
        snapshot = self.ensure_snapshot()
        return int(max(0, time() - snapshot.indexed_at))

    def _build_snapshot(self) -> ZoektIndexSnapshot:
        path_lines: dict[str, int] = {}
        total_lines = 0
        for path in sorted(self.repo_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.repo_root).as_posix()
            rel_parts = path.relative_to(self.repo_root).parts
            if any(part in _SKIP_PARTS for part in rel_parts):
                continue
            if path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            line_count = len(source.splitlines())
            path_lines[rel] = line_count
            total_lines += line_count
        return ZoektIndexSnapshot(indexed_at=time(), total_lines=total_lines, path_lines=path_lines)

    def _relative_path(self, target: Path) -> str | None:
        try:
            return target.relative_to(self.repo_root).as_posix()
        except ValueError:
            return None


__all__ = ["ZoektIndexSnapshot", "ZoektIndexer"]
