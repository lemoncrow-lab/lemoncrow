"""Deterministic onboarding for Review surfaces and execution runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.review.runtimes import (
    RunnerRegistry,
    RunnerSetupCandidate,
    built_in_runner_registry,
)
from lemoncrow.pro.capabilities.review.surfaces import (
    CONFIG_RELATIVE_PATH,
    ReviewSurfaceConfig,
    SetupCandidate,
    SurfaceRegistry,
    built_in_registry,
    load_review_surface_config,
    write_review_surface_config,
)


@dataclass(frozen=True)
class SetupIssue:
    level: str
    message: str


@dataclass(frozen=True)
class ReviewSetupReport:
    repo_root: Path
    config: ReviewSurfaceConfig
    candidates: tuple[SetupCandidate, ...]
    runtime_candidates: tuple[RunnerSetupCandidate, ...]
    issues: tuple[SetupIssue, ...]

    @property
    def auto_candidates(self) -> tuple[SetupCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.auto_write)

    @property
    def auto_runtime_candidates(self) -> tuple[RunnerSetupCandidate, ...]:
        return tuple(candidate for candidate in self.runtime_candidates if candidate.auto_write)

    def to_payload(self) -> dict[str, Any]:
        return {
            "repo_root": str(self.repo_root),
            "config_path": str(self.repo_root / CONFIG_RELATIVE_PATH),
            "configured": [
                {"id": entry.id, "provider": entry.provider, **dict(entry.values)} for entry in self.config.surfaces
            ],
            "configured_runtimes": [
                {"id": entry.id, "runner": entry.runner, **dict(entry.values)} for entry in self.config.runtimes
            ],
            "detected": [
                {
                    "id": candidate.id,
                    "provider": candidate.provider,
                    "kind": candidate.kind,
                    "root": candidate.root,
                    "confidence": candidate.confidence,
                    "auto_write": candidate.auto_write,
                    **dict(candidate.details),
                }
                for candidate in self.candidates
            ],
            "detected_runtimes": [
                {
                    "id": candidate.id,
                    "runner": candidate.runner,
                    "root": candidate.root,
                    "confidence": candidate.confidence,
                    "auto_write": candidate.auto_write,
                    **dict(candidate.details),
                }
                for candidate in self.runtime_candidates
            ],
            "issues": [{"level": issue.level, "message": issue.message} for issue in self.issues],
        }


def inspect_review_setup(
    repo_root: Path,
    *,
    registry: SurfaceRegistry | None = None,
    runner_registry: RunnerRegistry | None = None,
) -> ReviewSetupReport:
    root = repo_root.expanduser().resolve()
    registry = built_in_registry() if registry is None else registry
    runner_registry = built_in_runner_registry() if runner_registry is None else runner_registry
    config = load_review_surface_config(root)
    candidates = registry.setup_candidates(root)
    runtime_candidates = runner_registry.setup_candidates(root)
    issues: list[SetupIssue] = []

    available_providers = {provider.provider_id for provider in registry.providers()}
    available_runners = {runner.runner_id for runner in runner_registry.runners()}
    for runtime in config.runtimes:
        if runtime.runner not in available_runners:
            issues.append(
                SetupIssue(
                    "error",
                    f"runtime {runtime.id!r} uses unavailable runner {runtime.runner!r}; install its runner plugin or fix the config",
                )
            )
        root_value = runtime.string("root", ".") or "."
        if not (root / root_value).exists():
            issues.append(SetupIssue("error", f"runtime {runtime.id!r} root does not exist: {root_value}"))

    for entry in config.surfaces:
        if entry.provider not in available_providers:
            issues.append(
                SetupIssue(
                    "error",
                    f"surface {entry.id!r} uses unavailable provider {entry.provider!r}; install its provider plugin or fix the config",
                )
            )
        root_value = entry.string("root", ".") or "."
        if not (root / root_value).exists():
            issues.append(SetupIssue("error", f"surface {entry.id!r} root does not exist: {root_value}"))

    configured_providers = {entry.provider for entry in config.surfaces}
    detected_providers = {candidate.provider for candidate in candidates}
    for provider in sorted(detected_providers - configured_providers):
        if provider == "web":
            continue
        surface_matches = [candidate for candidate in candidates if candidate.provider == provider]
        message = (
            "it can be added automatically"
            if any(item.auto_write for item in surface_matches)
            else "review its candidate(s) before adding config"
        )
        issues.append(SetupIssue("info", f"detected {provider}; {message}"))

    configured_runners = {entry.runner for entry in config.runtimes}
    detected_runners = {candidate.runner for candidate in runtime_candidates}
    for runner in sorted(detected_runners - configured_runners):
        runtime_matches = [candidate for candidate in runtime_candidates if candidate.runner == runner]
        message = (
            "it can be added automatically"
            if any(item.auto_write for item in runtime_matches)
            else "review its candidate(s) before adding config"
        )
        issues.append(SetupIssue("info", f"detected runtime runner {runner}; {message}"))
    if not candidates and not runtime_candidates:
        issues.append(
            SetupIssue(
                "info",
                "no built-in surfaces or runtimes detected; add .lemoncrow/review.yaml or install provider/runner plugins",
            )
        )
    return ReviewSetupReport(root, config, candidates, runtime_candidates, tuple(issues))


def _auto_service_surfaces(runtime_candidates: tuple[RunnerSetupCandidate, ...]) -> tuple[SetupCandidate, ...]:
    rows: list[SetupCandidate] = []
    for runtime in runtime_candidates:
        services = runtime.details.get("services")
        if runtime.runner != "docker-compose" or not isinstance(services, list) or not services:
            continue
        rows.append(
            SetupCandidate(
                id=runtime.id.removeprefix("runtime-") or runtime.id,
                provider="service",
                kind="service",
                details={"runtime": runtime.id, "services": services},
            )
        )
    return tuple(rows)


def write_detected_review_config(report: ReviewSetupReport, *, overwrite: bool = False) -> Path:
    runtimes = report.auto_runtime_candidates
    surfaces = report.auto_candidates + _auto_service_surfaces(runtimes)
    if not surfaces and not runtimes:
        raise ValueError("no high-confidence review surfaces or runtimes can be written automatically")
    return write_review_surface_config(
        report.repo_root,
        surfaces,
        runtime_candidates=[candidate.to_config() for candidate in runtimes],
        overwrite=overwrite,
    )


def render_review_setup(report: ReviewSetupReport) -> str:
    lines = ["REVIEW SURFACES", str(report.repo_root), ""]
    if report.config.surfaces or report.config.runtimes:
        lines.append(
            f"Config  {CONFIG_RELATIVE_PATH.as_posix()} · {len(report.config.surfaces)} surface(s) · {len(report.config.runtimes)} runtime(s)"
        )
    else:
        lines.append(f"Config  none ({CONFIG_RELATIVE_PATH.as_posix()} is optional)")

    lines.extend(("", "Detected surfaces"))
    if report.candidates:
        for candidate in report.candidates:
            root = candidate.root if candidate.root not in {"", "."} else "."
            detail = ""
            if candidate.provider == "web":
                detail = f" · {candidate.details.get('framework', '')} · routes auto"
            elif candidate.provider == "bruno":
                detail = f" · {candidate.details.get('collection', '')}"
            flag = "auto" if candidate.auto_write else "manual"
            lines.append(f"  {candidate.provider:<8} {candidate.id:<26} {root}{detail}  [{flag}]")
    else:
        lines.append("  none")

    lines.extend(("", "Detected runtimes"))
    if report.runtime_candidates:
        for runtime_candidate in report.runtime_candidates:
            root = runtime_candidate.root if runtime_candidate.root not in {"", "."} else "."
            detail = ""
            compose = runtime_candidate.details.get("compose")
            if isinstance(compose, list):
                detail = f" · {', '.join(str(item) for item in compose)}"
            flag = "auto" if runtime_candidate.auto_write else "manual"
            lines.append(f"  {runtime_candidate.runner:<16} {runtime_candidate.id:<26} {root}{detail}  [{flag}]")

    if report.issues:
        lines.extend(("", "Notes"))
        for issue in report.issues:
            lines.append(f"  {issue.level}: {issue.message}")
    if (
        not report.config.surfaces
        and not report.config.runtimes
        and (report.auto_candidates or report.auto_runtime_candidates)
    ):
        lines.extend(("", "Write high-confidence config: lc review --setup --write-review-config"))
    lines.extend(
        (
            "",
            "Surface plugins: lemoncrow.review_surface_providers",
            "Runner plugins:  lemoncrow.review_runners",
        )
    )
    return "\n".join(lines)


__all__ = [
    "ReviewSetupReport",
    "SetupIssue",
    "inspect_review_setup",
    "render_review_setup",
    "write_detected_review_config",
]
