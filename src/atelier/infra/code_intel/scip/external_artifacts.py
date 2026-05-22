"""Helpers for repo-local external SCIP artifact discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ScipArtifactOrigin = Literal["internal", "external"]
_EXTERNAL_SCIP_PREFIX = "external-"


@dataclass(frozen=True)
class DiscoveredScipArtifact:
    """A trusted SCIP artifact path plus its routed origin."""

    path: Path
    origin: ScipArtifactOrigin = "internal"


def classify_scip_artifact(path: Path) -> DiscoveredScipArtifact:
    """Classify a discovered SCIP artifact by filename convention."""

    resolved = path.resolve()
    origin: ScipArtifactOrigin = "external" if resolved.name.startswith(_EXTERNAL_SCIP_PREFIX) else "internal"
    return DiscoveredScipArtifact(path=resolved, origin=origin)


def discover_external_scip_artifacts(cache_root: Path) -> list[DiscoveredScipArtifact]:
    """Return repo-local external SCIP artifacts from the existing cache root."""

    root = cache_root.resolve()
    if not root.exists():
        return []
    artifacts: list[DiscoveredScipArtifact] = []
    for path in sorted(root.glob(f"{_EXTERNAL_SCIP_PREFIX}*.scip")):
        resolved = path.resolve()
        if resolved.is_file():
            artifacts.append(DiscoveredScipArtifact(path=resolved, origin="external"))
    return artifacts


__all__ = [
    "DiscoveredScipArtifact",
    "ScipArtifactOrigin",
    "classify_scip_artifact",
    "discover_external_scip_artifacts",
]
