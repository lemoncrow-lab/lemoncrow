"""Discovery helpers for precomputed SCIP artifacts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from atelier.infra.code_intel.scip.binaries import discover_scip_binaries, scip_binary_spec
from atelier.infra.code_intel.scip.bootstrap import (
    ScipBootstrapResult,
    ensure_scip_binary,
    scip_availability_statuses,
)
from atelier.infra.code_intel.scip.external_artifacts import (
    DiscoveredScipArtifact,
    classify_scip_artifact,
    discover_external_scip_artifacts,
)
from atelier.infra.code_intel.scip.watcher import resolve_git_repo_state


def scip_cache_base_root(repo_root: Path, repo_id: str) -> Path:
    """Return the repo-local base cache directory used for synthetic SCIP artifacts."""

    return repo_root / ".atelier" / "cache" / "scip" / repo_id


def default_scip_cache_root(repo_root: Path, repo_id: str) -> Path:
    """Return the active branch-specific cache directory for synthetic SCIP artifacts."""

    branch_key = resolve_git_repo_state(repo_root).branch_key
    return scip_cache_base_root(repo_root, repo_id) / branch_key


ScipIndexStatus = Literal[
    "indexed",
    "unsupported",
    "missing_binary",
    "bootstrap_unavailable",
    "user_toolchain_required",
    "missing_context",
    "failed",
    "timeout",
    "missing_output",
]


class ScipIndexResult(BaseModel):
    """Result of an explicit lazy SCIP indexing attempt."""

    model_config = ConfigDict(extra="forbid")

    language: str
    status: ScipIndexStatus
    artifact_path: Path | None = None
    command: tuple[str, ...] = ()
    message: str = ""


class ScipIndexer:
    """Discovers checked-in or repo-local SCIP artifacts without installing tooling."""

    def __init__(self, repo_root: Path, repo_id: str, *, cache_root: Path | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_id = repo_id
        self._cache_root_override = cache_root.resolve() if cache_root is not None else None

    @property
    def base_cache_root(self) -> Path:
        return scip_cache_base_root(self.repo_root, self.repo_id).resolve()

    @property
    def cache_root(self) -> Path:
        if self._cache_root_override is not None:
            return self._cache_root_override
        return default_scip_cache_root(self.repo_root, self.repo_id).resolve()

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

    def availability_statuses(self) -> dict[str, ScipBootstrapResult]:
        """Expose SCIP indexer availability and bootstrap hints."""

        return scip_availability_statuses()

    def index_language(self, language: str, *, timeout_seconds: float = 120.0) -> ScipIndexResult:
        """Run one SCIP indexer on demand and write a repo-local artifact."""

        spec = scip_binary_spec(language)
        if spec is None:
            return ScipIndexResult(language=language, status="unsupported", message="unsupported language")
        bootstrap = ensure_scip_binary(language)
        if bootstrap.binary is None:
            if bootstrap.status == "bootstrap_unavailable":
                return ScipIndexResult(language=language, status="bootstrap_unavailable", message=bootstrap.message)
            if bootstrap.status == "user_toolchain_required":
                return ScipIndexResult(
                    language=language,
                    status="user_toolchain_required",
                    message=bootstrap.install_hint,
                )
            return ScipIndexResult(
                language=language,
                status="missing_binary",
                message=bootstrap.message or "SCIP binary not found",
            )
        binary = bootstrap.binary
        missing_context = spec.missing_context_files(self.repo_root)
        if missing_context:
            return ScipIndexResult(
                language=language,
                status="missing_context",
                message=f"missing required context: {', '.join(missing_context)}",
            )

        self.cache_root.mkdir(parents=True, exist_ok=True)
        output_path = self.cache_root / f"{language}.scip"
        expected_output = spec.expected_output_path(output_path, self.repo_root)
        command = tuple(spec.command(binary, output_path, self.repo_root))

        try:
            completed = subprocess.run(
                list(command),
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ScipIndexResult(language=language, status="timeout", command=command, message="indexer timed out")

        if completed.returncode != 0:
            return ScipIndexResult(
                language=language,
                status="failed",
                command=command,
                message=(completed.stderr or completed.stdout).strip(),
            )

        if expected_output != output_path and expected_output.exists():
            if output_path.exists():
                output_path.unlink()
            expected_output.replace(output_path)

        if not output_path.is_file():
            return ScipIndexResult(
                language=language,
                status="missing_output",
                command=command,
                message=f"indexer did not produce {output_path}",
            )

        artifact = classify_scip_artifact(output_path)
        return ScipIndexResult(
            language=language,
            status="indexed",
            artifact_path=artifact.path,
            command=command,
            message="indexed",
        )


__all__ = [
    "ScipIndexResult",
    "ScipIndexStatus",
    "ScipIndexer",
    "default_scip_cache_root",
    "scip_cache_base_root",
]
