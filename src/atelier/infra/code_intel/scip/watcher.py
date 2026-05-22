"""Bounded refresh detection for repo-local SCIP artifacts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def _artifact_signature(artifact_paths: list[Path]) -> str:
    parts: list[str] = []
    for path in sorted(artifact_paths):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}")
    return "\n".join(parts)


class ScipArtifactWatcher:
    """Tracks artifact refreshes and invalidates cache state through a callback."""

    def __init__(self, *, state_sync: Callable[[str, str], bool], state_key: str = "scip_artifact_signature") -> None:
        self._state_sync = state_sync
        self._state_key = state_key

    def refresh(self, artifact_paths: list[Path]) -> bool:
        return self._state_sync(self._state_key, _artifact_signature(artifact_paths))


__all__ = ["ScipArtifactWatcher"]
