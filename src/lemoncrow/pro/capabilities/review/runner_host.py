"""Review's host for the environment runners.

The runners in :mod:`lemoncrow.pro.environment.runners` import no product. A Review run's
context (:class:`~lemoncrow.pro.capabilities.review.surfaces.SurfaceContext`) hands them this
module as its ``runner_host``
(:class:`~lemoncrow.pro.environment.runner_host.RunnerHost`): Review's snapshot
materializer, hermetic stages, run evidence and runtime observation, under the names the
runners call. The functions at the bottom are the runners' helpers bound to this host,
with the signatures Review has always called.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.review.artifact_inspection import inspect_container_archive
from lemoncrow.pro.capabilities.review.hermetic import (
    REPRODUCE_COMMAND,
    DeclaredDependencies,
    HermeticPolicy,
    HermeticRefused,
    HermeticUnavailable,
    ProvisionedDependencies,
    RecordingSinks,
    SandboxView,
    build_execution_result,
    executor_image_digest,
    kernel_release,
    lockfile_digest,
    probe_executables,
    provision_declared_dependencies,
    provision_dependencies,
    sinks_available,
    workspace_digest,
)
from lemoncrow.pro.capabilities.review.run_evidence import (
    RunArtifactStore,
    protect_bytes,
    protect_execution_result,
    protect_text,
)
from lemoncrow.pro.capabilities.review.runtime_observation import (
    ObservationSetup,
    ObservationSpec,
    build_observation,
    workspace_change_rows,
)
from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side
from lemoncrow.pro.environment import runners
from lemoncrow.pro.environment.execution import EnvironmentFingerprint
from lemoncrow.pro.environment.runner_host import RunnerHost


def revision_side_identity(revision: Any, side: str) -> object:
    from lemoncrow.pro.capabilities.review.web_preview import revision_side_identity as identity

    return identity(revision, side)


def host() -> RunnerHost:
    """This module, as the runners' host."""

    from lemoncrow.pro.capabilities.review import runner_host

    return runner_host


def command_view(
    *, workspace: Path | None = None, cwd: str = "/workspace", deps: ProvisionedDependencies | None = None
) -> SandboxView:
    view: SandboxView = runners.command_view(host(), workspace=workspace, cwd=cwd, deps=deps)
    return view


def command_identity(
    command: Sequence[str],
    *,
    view: SandboxView | None = None,
    sinks: bool | None = None,
    strace: bool = False,
    image_digest: str = "",
) -> EnvironmentFingerprint:
    return runners.command_identity(host(), command, view=view, sinks=sinks, strace=strace, image_digest=image_digest)


def command_environment(command: Sequence[str], *, image_digest: str = "") -> str:
    return runners.command_environment(host(), command, image_digest=image_digest)


def revision_snapshot_identity(revision: Any, side: str) -> str:
    return runners.revision_snapshot_identity(host(), revision, side)


def built_in_runner_registry(*, include_entrypoints: bool = True) -> runners.RunnerRegistry:
    return runners.built_in_runner_registry(
        environment_probe=command_environment, include_entrypoints=include_entrypoints
    )


__all__ = [
    "REPRODUCE_COMMAND",
    "DeclaredDependencies",
    "HermeticPolicy",
    "HermeticRefused",
    "HermeticUnavailable",
    "ObservationSetup",
    "ObservationSpec",
    "ProvisionedDependencies",
    "RecordingSinks",
    "ReviewSnapshotUnavailable",
    "RunArtifactStore",
    "SandboxView",
    "build_execution_result",
    "build_observation",
    "built_in_runner_registry",
    "command_environment",
    "command_identity",
    "command_view",
    "executor_image_digest",
    "host",
    "inspect_container_archive",
    "kernel_release",
    "lockfile_digest",
    "materialize_review_side",
    "probe_executables",
    "protect_bytes",
    "protect_execution_result",
    "protect_text",
    "provision_declared_dependencies",
    "provision_dependencies",
    "revision_side_identity",
    "revision_snapshot_identity",
    "sinks_available",
    "workspace_change_rows",
    "workspace_digest",
]
