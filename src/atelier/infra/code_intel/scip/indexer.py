"""Discovery helpers for precomputed SCIP artifacts."""

from __future__ import annotations

from pathlib import Path

from atelier.infra.code_intel.scip.binaries import discover_scip_binaries
from atelier.infra.code_intel.scip.external_artifacts import (
    DiscoveredScipArtifact,
    classify_scip_artifact,
    discover_external_scip_artifacts,
)


def default_scip_cache_root(repo_root: Path, repo_id: str) -> Path:
    """Return the repo-local cache directory used for synthetic SCIP artifacts."""

    return repo_root / ".atelier" / "cache" / "scip" / repo_id


class ScipIndexer:
    """Discovers checked-in or repo-local SCIP artifacts without installing tooling."""

    def __init__(self, repo_root: Path, repo_id: str, *, cache_root: Path | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_id = repo_id
        self.cache_root = (cache_root or default_scip_cache_root(self.repo_root, repo_id)).resolve()

    def discover_artifacts(self) -> list[DiscoveredScipArtifact]:
        """Return existing `.scip` artifacts under the allowed repo-local cache roots."""

        roots = [self.cache_root]
        artifacts: list[DiscoveredScipArtifact] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("*.scip")):
                resolved = path.resolve()
                if resolved.name.startswith("external-"):
                    continue
                if resolved not in seen and resolved.is_file():
                    seen.add(resolved)
                    artifacts.append(classify_scip_artifact(resolved))
            for artifact in discover_external_scip_artifacts(root):
                if artifact.path not in seen:
                    seen.add(artifact.path)
                    artifacts.append(artifact)
        return artifacts

    def available_binaries(self) -> dict[str, Path]:
        """Expose local SCIP binaries for future bootstrap paths."""

        return discover_scip_binaries()


__all__ = ["ScipIndexer", "default_scip_cache_root"]
