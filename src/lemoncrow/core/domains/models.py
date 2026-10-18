"""Domain Bundle models.

Lightweight replacement for PackManifest — no semver, no dependencies,
no publishing, no signing. Just the data assets that matter.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DomainBundle(BaseModel):
    """Manifest for an internal domain bundle.

    A domain bundle is a directory containing curated reasoning assets
    (playbooks, rubrics, environments, evals, benchmarks) for a specific
    engineering domain. Bundles are internal only — no versioning, no registry,
    no external distribution.
    """

    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    domain: str
    description: str
    author: str = "Beseam"

    playbooks: list[str] = Field(default_factory=list)
    rubrics: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    evals: list[str] = Field(default_factory=list)
    benchmarks: list[str] = Field(default_factory=list)

    @property
    def asset_files(self) -> list[str]:
        """All asset file paths declared by this bundle."""
        return self.playbooks + self.rubrics + self.environments + self.evals + self.benchmarks


class DomainBundleRef(BaseModel):
    """Lightweight reference to a loaded domain bundle (for listings)."""

    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    domain: str
    description: str
    path: str
    playbooks_count: int = 0
    rubrics_count: int = 0
    environments_count: int = 0
    evals_count: int = 0
    benchmarks_count: int = 0

    @classmethod
    def from_bundle(cls, bundle: DomainBundle, path: Path) -> DomainBundleRef:
        return cls(
            bundle_id=bundle.bundle_id,
            domain=bundle.domain,
            description=bundle.description,
            path=str(path),
            playbooks_count=len(bundle.playbooks),
            rubrics_count=len(bundle.rubrics),
            environments_count=len(bundle.environments),
            evals_count=len(bundle.evals),
            benchmarks_count=len(bundle.benchmarks),
        )


def bundle_manifest_path(bundle_path: Path) -> Path:
    return bundle_path / "bundle.yaml"
