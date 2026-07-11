"""Repository metrics for Zoekt routing decisions."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time
from typing import Any

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


@dataclass(frozen=True)
class ZoektIndexSnapshot:
    indexed_at: float
    total_lines: int
    path_lines: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZoektIndexSnapshot:
        return cls(
            indexed_at=float(data["indexed_at"]),
            total_lines=int(data["total_lines"]),
            path_lines={str(k): int(v) for k, v in data["path_lines"].items()},
        )


class ZoektIndexer:
    """Keep lightweight line-count metadata for routing decisions."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self._lock = threading.Lock()
        self._snapshot: ZoektIndexSnapshot | None = None
        if self._snapshot is None:
            t = threading.Thread(
                target=self.ensure_snapshot,
                daemon=True,
                name="lemoncrow-zoekt-snapshot",
            )
            t.start()

    def ensure_snapshot(self) -> ZoektIndexSnapshot:
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            loaded = self._load_snapshot_from_disk()
            if loaded is not None:
                self._snapshot = loaded
                return self._snapshot
            self._snapshot = self._build_snapshot()
            self._save_snapshot_to_disk(self._snapshot)
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

    def _snapshot_cache_path(self) -> Path:
        return self.repo_root / ".git" / "lemoncrow" / "zoekt_snapshot.json"

    def _current_head(self) -> str | None:
        head_file = self.repo_root / ".git" / "HEAD"
        try:
            ref = head_file.read_text(encoding="utf-8").strip()
            if ref.startswith("ref: "):
                ref_path = self.repo_root / ".git" / ref[5:]
                return ref_path.read_text(encoding="utf-8").strip()
            return ref
        except OSError:
            return None

    def _load_snapshot_from_disk(self) -> ZoektIndexSnapshot | None:
        cache_path = self._snapshot_cache_path()
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            stored_head = data.get("head")
            current_head = self._current_head()
            if current_head is None or stored_head != current_head:
                return None
            return ZoektIndexSnapshot.from_dict(data["snapshot"])
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save_snapshot_to_disk(self, snapshot: ZoektIndexSnapshot) -> None:
        cache_path = self._snapshot_cache_path()
        current_head = self._current_head()
        payload = json.dumps({"head": current_head, "snapshot": snapshot.to_dict()})
        tmp = cache_path.with_suffix(".tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(cache_path)
        except OSError:
            pass

    def _build_snapshot(self) -> ZoektIndexSnapshot:
        path_lines: dict[str, int] = {}
        total_lines = 0
        for rel_str in self._git_tracked_text_files():
            path = self.repo_root / rel_str
            if not path.is_file():
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            line_count = len(source.splitlines())
            path_lines[rel_str] = line_count
            total_lines += line_count
        return ZoektIndexSnapshot(indexed_at=time(), total_lines=total_lines, path_lines=path_lines)

    def _git_tracked_text_files(self) -> list[str]:
        """Return repo-relative paths of git-tracked text files, respecting .gitignore.

        Always uses ``git ls-files`` so that .gitignore rules (including nested
        ones) are applied uniformly.  Returns an empty list when git is
        unavailable rather than falling back to an unfiltered filesystem walk.
        """
        import subprocess as _subprocess

        try:
            result = _subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                text=False,
                check=False,
                timeout=30.0,
            )
        except OSError:
            return []
        if result.returncode != 0:
            return []
        entries = [e.decode("utf-8", errors="replace") for e in result.stdout.split(b"\x00") if e]
        return sorted(e for e in entries if Path(e).suffix.lower() in _TEXT_SUFFIXES)

    def _relative_path(self, target: Path) -> str | None:
        try:
            return target.relative_to(self.repo_root).as_posix()
        except ValueError:
            return None


__all__ = ["ZoektIndexSnapshot", "ZoektIndexer"]
