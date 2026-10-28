"""Self-contained runtime plugins for LemonCrow Review.

Surface providers discover *what* is reviewable. Runner plugins own *how* an
immutable review side is executed. A Bruno request, web route, or service can
therefore bind to Docker Compose in one repository and a plain process runner in
another without either surface provider knowing the execution technology.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import posixpath
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

import yaml

from lemoncrow.core.foundation.run_file_io import RunFileLock
from lemoncrow.pro.capabilities.review.execution import (
    EXECUTOR_FINGERPRINT_VERSION,
    REPORT_DIR,
    REPORT_ENV,
    RUN_ON_BY_EXECUTOR_CLASS,
    EnvironmentFingerprint,
    ExecutionRecipe,
    ExecutionSubject,
    ExecutorClass,
    StageOutcome,
    cache_namespace,
)
from lemoncrow.pro.capabilities.review.hermetic import (
    REPRODUCE_COMMAND,
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
    provision_dependencies,
    sinks_available,
    workspace_digest,
)
from lemoncrow.pro.capabilities.review.host_tools import host_executable
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
from lemoncrow.pro.capabilities.review.sandbox_limits import SandboxCapacityBusy, limit_note, run_limited
from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry, SurfaceContext

RUNNER_ENTRYPOINT_GROUP = "lemoncrow.review_runners"
_COMPOSE_NAMES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".jj",
        ".claude",
        ".lc-worktrees",
        ".lemoncrow",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        ".turbo",
        "coverage",
        "deleted",
        "archive",
        "archives",
    }
)


@dataclass(frozen=True)
class SurfaceRunResult:
    surface_id: str
    provider: str
    side: str
    status: str
    summary: str
    runner: str = ""
    runtime: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    output: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "provider": self.provider,
            "side": self.side,
            "status": self.status,
            "summary": self.summary,
            "runner": self.runner,
            "runtime": self.runtime,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class ApiResponseDiffScore:
    """Transparent old/new response delta used to rank affected read endpoints."""

    old_status_code: int
    new_status_code: int
    status_changed: bool
    body_changed_ratio: float
    header_changed_ratio: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "old_status_code": self.old_status_code,
            "new_status_code": self.new_status_code,
            "status_changed": self.status_changed,
            "body_changed_ratio": self.body_changed_ratio,
            "header_changed_ratio": self.header_changed_ratio,
        }


_VOLATILE_HTTP_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "date",
        "keep-alive",
        "server",
        "traceparent",
        "tracestate",
        "transfer-encoding",
        "x-request-id",
    }
)


def _response_diff_units(body: Any) -> list[str]:
    """Canonical, bounded-size units for a deterministic response-body delta."""

    if isinstance(body, (dict, list)):
        text = json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False, separators=(",", ": "))
    elif body is None:
        text = ""
    elif isinstance(body, str):
        text = body.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    # Compact JSON and one-line text would otherwise become one all-or-nothing
    # unit. Fixed chunks make the magnitude useful while keeping work linear-ish
    # over the runner's already bounded 256 KiB response body.
    return [text[index : index + 64] for index in range(0, len(text), 64)] or [""]


def _mapping_delta(old: Mapping[str, Any], new: Mapping[str, Any]) -> float:
    keys = set(old) | set(new)
    if not keys:
        return 0.0
    changed = sum(1 for key in keys if old.get(key) != new.get(key))
    return changed / len(keys)


def api_response_diff_score(old: SurfaceRunResult, new: SurfaceRunResult) -> ApiResponseDiffScore:
    """Compare two executed API sides without using latency or volatile headers."""

    try:
        old_status = int(old.data["status_code"])
        new_status = int(new.data["status_code"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("API response diff requires status codes from both Snapshot sides") from exc

    old_units = _response_diff_units(old.data.get("body"))
    new_units = _response_diff_units(new.data.get("body"))
    body_ratio = 1.0 - difflib.SequenceMatcher(None, old_units, new_units, autojunk=False).ratio()

    def stable_headers(result: SurfaceRunResult) -> dict[str, str]:
        raw = result.data.get("headers")
        if not isinstance(raw, Mapping):
            return {}
        return {
            str(key).lower(): str(value) for key, value in raw.items() if str(key).lower() not in _VOLATILE_HTTP_HEADERS
        }

    return ApiResponseDiffScore(
        old_status_code=old_status,
        new_status_code=new_status,
        status_changed=old_status != new_status,
        body_changed_ratio=max(0.0, min(1.0, body_ratio)),
        header_changed_ratio=_mapping_delta(stable_headers(old), stable_headers(new)),
    )


@dataclass(frozen=True)
class RunnerSetupCandidate:
    id: str
    runner: str
    root: str = "."
    confidence: float = 1.0
    auto_write: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_config(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "runner": self.runner}
        if self.root not in {"", "."}:
            payload["root"] = self.root
        payload.update(dict(self.details))
        return payload


@runtime_checkable
class ReviewRunner(Protocol):
    """One execution technology, independent of review surface semantics."""

    runner_id: str

    def run(
        self,
        context: SurfaceContext,
        runtime: RuntimeConfigEntry,
        surface: ReviewSurface,
        *,
        side: str,
    ) -> SurfaceRunResult: ...

    def setup_candidates(self, repo_root: Path) -> tuple[RunnerSetupCandidate, ...]: ...


class RunnerRegistry:
    def __init__(
        self,
        runners: Iterable[ReviewRunner] = (),
        *,
        executor_class: ExecutorClass = ExecutorClass.AUTHOR_MACHINE,
        run_on: str = "",
        environment_probe: Callable[[Sequence[str]], str] | None = None,
    ) -> None:
        self._runners: dict[str, ReviewRunner] = {}
        # The executor environment (``EnvironmentFingerprint.executor_digest``) a
        # run of a recipe's command here will record, known before dispatch: it
        # takes the command, since what runs decides the environment. ``None`` when it is
        # known only after a run (hosted workers report theirs with the result),
        # so nothing is carried forward from an earlier Snapshot.
        self.environment_probe = environment_probe
        # Who executes for this registry, set by the trusted dispatcher that
        # built it (local server: the author's machine; hosted worker plane:
        # customer worker or LemonCrow-managed sandbox).
        self.executor_class = executor_class
        # The run plan's executor locus this registry executes for; a run-plan
        # entry runs here only when it is planned on this locus.
        self.run_on = run_on or RUN_ON_BY_EXECUTOR_CLASS[executor_class]
        if self.run_on not in RUN_ON_BY_EXECUTOR_CLASS.values():
            raise ValueError(f"unknown run plan executor locus: {self.run_on!r}")
        for runner in runners:
            self.register(runner)

    def register(self, runner: ReviewRunner) -> None:
        runner_id = str(runner.runner_id).strip()
        if not runner_id:
            raise ValueError("review runner_id must be non-empty")
        if runner_id in self._runners:
            raise ValueError(f"duplicate review runner: {runner_id}")
        self._runners[runner_id] = runner

    def runners(self) -> tuple[ReviewRunner, ...]:
        return tuple(self._runners[key] for key in sorted(self._runners))

    def get(self, runner_id: str) -> ReviewRunner | None:
        return self._runners.get(runner_id)

    def setup_candidates(self, repo_root: Path) -> tuple[RunnerSetupCandidate, ...]:
        rows: list[RunnerSetupCandidate] = []
        seen: set[tuple[str, str]] = set()
        for runner in self.runners():
            for candidate in runner.setup_candidates(repo_root):
                identity = (candidate.runner, candidate.id)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(candidate)
        return tuple(rows)

    def run(
        self,
        context: SurfaceContext,
        surface: ReviewSurface,
        *,
        side: str,
        cleanup_workspace: bool = False,
    ) -> SurfaceRunResult:
        if side not in {"old", "new"}:
            raise ValueError("review surface side must be old or new")
        if not surface.runtime:
            raise ValueError(f"review surface has no runtime: {surface.provider}:{surface.id}")
        runtime = context.config.runtime(surface.runtime)
        if runtime is None:
            raise ValueError(f"review surface references unknown runtime: {surface.runtime}")
        runner = self.get(runtime.runner)
        if runner is None:
            raise ValueError(f"review runtime {runtime.id!r} uses unavailable runner {runtime.runner!r}")
        if "execute" not in surface.capabilities:
            raise ValueError(f"review surface is not executable: {surface.provider}:{surface.id}")
        # A runner re-materializes its Snapshot side into one fixed workspace per
        # runtime and side (rmtree + write), so two runs of the same runtime and
        # side at once -- the Reader's run and the impact ranking's -- deleted
        # the tree the other was executing from. One run at a time per workspace.
        digest = hashlib.sha256(f"{runtime.id}\0{side}".encode()).hexdigest()[:16]
        lock = (
            context.store_root
            / "review"
            / "runtime-runs"
            / context.revision.review_id
            / context.revision.id
            / runner.runner_id
            / digest
        )
        with RunFileLock(lock):
            try:
                return runner.run(context, runtime, surface, side=side)
            finally:
                if cleanup_workspace:
                    base = (
                        context.store_root
                        / "review"
                        / "runtime-runs"
                        / context.revision.review_id
                        / context.revision.id
                        / runner.runner_id
                    )
                    workspace_digests = {
                        digest,
                        hashlib.sha256(f"{runtime.id}\0{surface.id}\0{side}".encode()).hexdigest()[:16],
                    }
                    for workspace_digest in workspace_digests:
                        workspace = base / workspace_digest / "workspace"
                        if workspace.exists():
                            shutil.rmtree(workspace, ignore_errors=True)


def _walk_named(repo_root: Path, names: set[str], *, max_depth: int = 4) -> tuple[Path, ...]:
    root = repo_root.resolve()
    found: list[Path] = []
    pending = [(root, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and entry.name in _IGNORED_DIRS:
                continue
            if entry.is_file() and entry.name in names:
                found.append(entry)
            elif entry.is_dir() and depth < max_depth:
                pending.append((entry, depth + 1))
    return tuple(sorted(found))


def _relative(repo_root: Path, path: Path) -> str:
    value = path.resolve().relative_to(repo_root.resolve()).as_posix()
    return value or "."


class DockerComposeRunner:
    runner_id = "docker-compose"

    def setup_candidates(self, repo_root: Path) -> tuple[RunnerSetupCandidate, ...]:
        paths = list(_walk_named(repo_root, set(_COMPOSE_NAMES)))
        root = repo_root.resolve()
        for directory in (root, root / "deployments", root / "infra"):
            if not directory.is_dir():
                continue
            try:
                for item in directory.iterdir():
                    lowered = item.name.lower()
                    if (
                        item.is_file()
                        and (lowered.startswith("compose.") or lowered.startswith("docker-compose."))
                        and item.suffix in {".yml", ".yaml"}
                    ):
                        paths.append(item)
            except OSError:
                continue
        candidates: list[RunnerSetupCandidate] = []
        for path in sorted({item.resolve() for item in paths}):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                raw = None
            services_node = raw.get("services", {}) if isinstance(raw, dict) else {}
            services = tuple(str(name) for name in services_node) if isinstance(services_node, dict) else ()
            rel = _relative(repo_root, path)
            name = PurePosixPath(rel).stem.replace(".", "-")
            candidates.append(
                RunnerSetupCandidate(
                    id="runtime-" + name,
                    runner=self.runner_id,
                    confidence=0.95,
                    auto_write=(
                        path.parent.resolve() == root
                        and (
                            path.name in _COMPOSE_NAMES or path.name.startswith(("compose.dev.", "docker-compose.dev."))
                        )
                    ),
                    details={"compose": [rel], "services": list(services[:12])},
                )
            )
        return tuple(candidates)

    def run(
        self,
        context: SurfaceContext,
        runtime: RuntimeConfigEntry,
        surface: ReviewSurface,
        *,
        side: str,
    ) -> SurfaceRunResult:
        """Validate an exact Compose revision without starting containers."""

        compose_files = runtime.strings("compose") or runtime.strings("files")
        if not compose_files:
            return self._result(surface, runtime, side, "unavailable", "no compose files configured")
        standalone = host_executable("docker-compose")
        docker = host_executable("docker")
        if standalone:
            compose_command = [standalone]
            compose_executable = standalone
        elif docker:
            compose_command = [docker, "compose"]
            compose_executable = docker
        else:
            return self._result(surface, runtime, side, "unavailable", "Docker Compose CLI is not installed")
        bwrap = shutil.which("bwrap") or shutil.which("bubblewrap")
        if not bwrap and sys.platform != "darwin":
            return self._result(
                surface,
                runtime,
                side,
                "unavailable",
                "Compose validation requires bubblewrap; refusing to parse reviewed compose files unsandboxed",
            )

        digest = hashlib.sha256(f"{runtime.id}\0{side}".encode()).hexdigest()[:16]
        workspace = (
            context.store_root
            / "review"
            / "runtime-runs"
            / context.revision.review_id
            / context.revision.id
            / self.runner_id
            / digest
            / "workspace"
        )
        try:
            materialize_review_side(
                context.store_root,
                context.source_root or context.repo_root,
                context.revision,
                side=side,
                target=workspace,
                store=context.store,
            )
        except ReviewSnapshotUnavailable as exc:
            return self._result(surface, runtime, side, "unavailable", str(exc))

        root = workspace.resolve()
        # Paths as the sandboxed Compose CLI sees them: bubblewrap mounts the
        # snapshot at /workspace, Seatbelt grants it at its host path.
        inside = root if sys.platform == "darwin" else PurePosixPath("/workspace")
        command = [*compose_command, "--project-directory", str(inside)]
        for rel in compose_files:
            candidate = (workspace / rel).resolve()
            if candidate != root and root not in candidate.parents:
                return self._result(surface, runtime, side, "unavailable", "compose path escapes review snapshot")
            if not candidate.is_file():
                return self._result(surface, runtime, side, "unavailable", f"compose file missing: {rel}")
            command.extend(("-f", str(inside / candidate.relative_to(root).as_posix())))
        command.extend(("config", "--services"))
        try:
            sandboxed = _compose_sandbox(command, workspace=root, executable=compose_executable, bwrap=bwrap)
        except (RuntimeError, ValueError) as exc:
            return self._result(surface, runtime, side, "unavailable", str(exc))

        started = time.monotonic()
        try:
            proc = subprocess.run(
                sandboxed,
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=30,
                env={
                    "PATH": str(Path(compose_executable).parent) + ":/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp",
                    "CI": "1",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._result(
                surface,
                runtime,
                side,
                "unavailable",
                f"Compose validation could not run: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        output = proc.stdout.strip()[-4000:]
        if proc.returncode != 0:
            return self._result(
                surface,
                runtime,
                side,
                "failed",
                "Compose configuration is invalid for this revision",
                exit_code=proc.returncode,
                duration_ms=duration_ms,
                output=output,
            )
        services = tuple(line.strip() for line in output.splitlines() if line.strip())
        service_name = str(surface.metadata.get("service", "")) if surface.kind == "service" else ""
        if service_name and service_name not in services:
            return self._result(
                surface,
                runtime,
                side,
                "failed",
                f"Compose service {service_name!r} is not present in this revision",
                exit_code=0,
                duration_ms=duration_ms,
                output=output,
                data={"service": service_name, "services": services, "compose": compose_files},
            )
        summary = (
            f"Compose service {service_name} is valid"
            if service_name
            else f"Compose configuration valid · {len(services)} service(s)"
        )
        return self._result(
            surface,
            runtime,
            side,
            "passed",
            summary,
            exit_code=0,
            duration_ms=duration_ms,
            data={"service": service_name, "services": services, "compose": compose_files},
        )

    def _result(
        self,
        surface: ReviewSurface,
        runtime: RuntimeConfigEntry,
        side: str,
        status: str,
        summary: str,
        *,
        exit_code: int | None = None,
        duration_ms: int = 0,
        output: str = "",
        data: Mapping[str, Any] | None = None,
    ) -> SurfaceRunResult:
        return SurfaceRunResult(
            surface_id=surface.id,
            provider=surface.provider,
            side=side,
            status=status,
            summary=summary,
            runner=self.runner_id,
            runtime=runtime.id,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output=output,
            data={} if data is None else data,
        )


_MAX_REPORT_BYTES = 4 * 1024 * 1024
_MAX_CHANGED_FILES = 64
_MAX_CHANGED_FILE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class WorkspaceOverlay:
    """Files of another Snapshot side written over a materialized workspace.

    Only a trusted dispatcher constructs one (a configured runtime's YAML
    values can never produce this type); ``source`` is the snapshot identity
    the files were read from and is recorded on the execution subject.
    """

    source: str
    files: tuple[tuple[str, str | None], ...]
    """``(repo-relative path, text)``; ``None`` deletes the path."""

    def digests(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (path, "deleted" if text is None else "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest())
            for path, text in self.files
        )


def apply_workspace_overlay(workspace: Path, overlay: WorkspaceOverlay) -> str:
    """Write *overlay* into *workspace*; returns a refusal reason or ``""``."""

    root = workspace.resolve()
    for path, text in overlay.files:
        rel = PurePosixPath(path)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            return f"overlay path escapes the workspace: {path}"
        target = (root / rel.as_posix()).resolve()
        if root not in target.parents:
            return f"overlay path escapes the workspace: {path}"
        if text is None:
            if target.is_file() and not target.is_symlink():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        target.write_text(text, encoding="utf-8")
    return ""


def workspace_file_hashes(workspace: Path) -> dict[str, str]:
    """``path -> sha256`` of every regular file (``.git`` excluded), for change capture."""

    root = workspace.resolve()
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        try:
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def collect_workspace_changes(workspace: Path, before: Mapping[str, str]) -> dict[str, Any]:
    """Text files the run changed, bounded; never applied anywhere by the runner."""

    after = workspace_file_hashes(workspace)
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    rows: list[dict[str, Any]] = []
    omitted: list[str] = []
    for path in changed:
        if len(rows) >= _MAX_CHANGED_FILES:
            omitted.append(path)
            continue
        if path not in after:
            rows.append({"path": path, "status": "deleted", "text": None, "before_sha256": before[path]})
            continue
        try:
            payload = (workspace / path).read_bytes()
            text = payload.decode("utf-8") if len(payload) <= _MAX_CHANGED_FILE_BYTES else None
        except (OSError, UnicodeError):
            text = None
        if text is None:
            omitted.append(path)
            continue
        rows.append(
            {
                "path": path,
                "status": "added" if path not in before else "modified",
                "text": text,
                "before_sha256": before.get(path, ""),
            }
        )
    return {"files": rows, "omitted": omitted[:256], "count": len(changed)}


def read_report(directory: Path, *, limit: int = _MAX_REPORT_BYTES) -> tuple[str, bool]:
    """The runner-written report (bounded), and whether it was truncated.

    Reviewed code wrote this directory: neither it nor ``report`` may be a
    symlink (opened without following links, relative to the directory
    descriptor), and only a regular file is read, so a planted link can never
    make the host read a file outside the sandbox.
    """

    import stat

    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return "", False
    try:
        try:
            fd = os.open("report", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except OSError:
            return "", False
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return "", False
            payload = handle.read(limit + 1)
    finally:
        os.close(directory_fd)
    return payload[:limit].decode("utf-8", errors="replace"), len(payload) > limit


#: The host runtime a plain ``command`` run mounts read-only at the same paths
#: (the optional ones only when the host has them), and the sandbox's PATH.
_COMMAND_MOUNTS: Final[tuple[str, ...]] = ("/usr", "/bin", "/lib")
_COMMAND_OPTIONAL_MOUNTS: Final[tuple[str, ...]] = ("/usr/local", "/lib64")
_COMMAND_PATH: Final[str] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _command_sandbox() -> str:
    return "seatbelt" if sys.platform == "darwin" else "bubblewrap"


def _command_runtime_mounts() -> tuple[str, ...]:
    return (*_COMMAND_MOUNTS, *(mount for mount in _COMMAND_OPTIONAL_MOUNTS if Path(mount).exists()))


def _compose_sandbox(command: Sequence[str], *, workspace: Path, executable: str, bwrap: str | None) -> list[str]:
    """Wrap ``docker compose config`` so a reviewed compose file is parsed offline.

    The head's compose files and ``.env`` choose ``include``/``extends``/
    ``env_file`` targets: with no network and only the snapshot and the host
    runtime visible, they cannot fetch remote resources or read host files.
    """

    if sys.platform == "darwin":
        from lemoncrow.pro.capabilities.review.macos_sandbox import sandbox_command

        return sandbox_command(command, workspace=workspace, cwd=workspace, executables=(executable,))
    assert bwrap is not None
    mounts = _command_runtime_mounts()
    sandbox = [bwrap, "--die-with-parent", "--new-session", "--unshare-all", "--proc", "/proc", "--dev", "/dev"]
    for mount in mounts:
        sandbox.extend(("--ro-bind", mount, mount))
    resolved = str(Path(executable).resolve())
    if not any(resolved == mount or resolved.startswith(mount + "/") for mount in mounts):
        # The host's own Compose binary (not a reviewed path): only that file is exposed.
        sandbox.extend(("--ro-bind", resolved, executable))
    sandbox.extend(
        (
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(workspace),
            "/workspace",
            "--clearenv",
            "--setenv",
            "PATH",
            _COMMAND_PATH,
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "CI",
            "1",
            "--chdir",
            "/workspace",
        )
    )
    return [*sandbox, *command]


def command_view(
    *,
    workspace: Path | None = None,
    cwd: str = "/workspace",
    deps: ProvisionedDependencies | None = None,
) -> SandboxView:
    """The filesystem and PATH a plain :class:`CommandRunner` command sees in its sandbox.

    Without ``workspace``/``deps`` (a probe before dispatch) a command that lives
    in the reviewed workspace or a provisioned dependency tree does not resolve,
    so its environment is unknown and nothing carries.
    """

    if sys.platform == "darwin":
        mounts: list[tuple[str, str]] = [("/", "/")]
        if workspace is not None:
            mounts.append((str(workspace), str(workspace)))
        return SandboxView(path=_COMMAND_PATH, cwd=cwd, mounts=tuple(mounts), probed=frozenset({"/"}))
    runtime = _command_runtime_mounts()
    mounts = [(mount, mount) for mount in runtime]
    if workspace is not None:
        mounts.append(("/workspace", str(workspace)))
    path = _COMMAND_PATH
    if deps is not None:
        mounts.extend((mount, str(host)) for host, mount in deps.mounts)
        if deps.path_prefix:
            path = ":".join((*deps.path_prefix, _COMMAND_PATH))
    return SandboxView(path=path, cwd=cwd, mounts=tuple(mounts), probed=frozenset(runtime))


def command_identity(
    command: Sequence[str],
    *,
    view: SandboxView | None = None,
    sinks: bool | None = None,
    strace: bool = False,
    image_digest: str = "",
) -> EnvironmentFingerprint:
    """The executor environment a plain :class:`CommandRunner` run of ``command`` uses.

    It resolves what actually runs (the command, its ``#!`` interpreters, the
    recording-sink supervisor and the observer) through the sandbox's PATH and
    mounts, and records each one's resolved path, version output and content
    digest, with the host image and kernel release. Every probe re-reads a
    binary whose on-disk identity changed, so an upgrade under a running server
    is seen at once.
    """

    argv = list(command)
    if sys.platform == "darwin" and argv and argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    names = argv[:1]
    if sinks is None:
        # Before dispatch: the run wraps the command in the sink supervisor exactly when it can.
        sinks = sys.platform != "darwin" and sinks_available()
    if sinks:
        names.append("python3")
    if strace:
        names.append("strace")
    executables = probe_executables(names, view or command_view())
    tools: dict[str, str] = {}
    for item in executables:
        tools[posixpath.basename(item.name)[:64]] = item.version[:128]
    return EnvironmentFingerprint(
        image_digest=image_digest or executor_image_digest(),
        toolchain=tuple(sorted(tools.items())),
        sandbox=_command_sandbox(),
        version=EXECUTOR_FINGERPRINT_VERSION,
        kernel=kernel_release(),
        executables=executables,
    )


def command_environment(command: Sequence[str], *, image_digest: str = "") -> str:
    """The executor environment a plain :class:`CommandRunner` run of ``command`` records, before dispatch.

    It runs the same probe as the run (:func:`command_identity`), so an equal
    digest means the run would record the same executor environment; ``""``
    when any component is unknown.
    """

    return command_identity(command, image_digest=image_digest).executor_digest


class CommandRunner:
    """Explicit bounded command runner for tests/checks, not long-lived servers.

    The command runs inside an immutable-revision workspace in a bubblewrap
    namespace with networking disabled. This is suitable for `python ...`,
    `npm test`, `make verify`, etc. Long-lived servers use a service runner that
    owns start/health/endpoint/stop as one lifecycle.
    """

    runner_id = "command"

    def setup_candidates(self, _repo_root: Path) -> tuple[RunnerSetupCandidate, ...]:
        return ()

    def run(
        self,
        context: SurfaceContext,
        runtime: RuntimeConfigEntry,
        surface: ReviewSurface,
        *,
        side: str,
    ) -> SurfaceRunResult:
        command = runtime.strings("command")
        if not command:
            return SurfaceRunResult(
                surface.id, surface.provider, side, "unavailable", "runtime has no command", self.runner_id, runtime.id
            )
        bwrap = shutil.which("bwrap") or shutil.which("bubblewrap")
        if not bwrap and sys.platform != "darwin":
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "command runtime requires bubblewrap; refusing to run reviewed code unsandboxed",
                self.runner_id,
                runtime.id,
            )
        timeout_raw = runtime.values.get("timeout", 60)
        try:
            timeout = max(1, min(int(timeout_raw), 900))
        except (TypeError, ValueError):
            timeout = 60
        digest = hashlib.sha256(f"{runtime.id}\0{side}".encode()).hexdigest()[:16]
        workspace = (
            context.store_root
            / "review"
            / "runtime-runs"
            / context.revision.review_id
            / context.revision.id
            / self.runner_id
            / digest
            / "workspace"
        )
        root_value = runtime.string("root", ".") or "."
        try:
            materialize_review_side(
                context.store_root,
                context.source_root or context.repo_root,
                context.revision,
                side=side,
                target=workspace,
                scope=root_value,
                store=context.store,
            )
        except ReviewSnapshotUnavailable as exc:
            return SurfaceRunResult(
                surface.id, surface.provider, side, "unavailable", str(exc), self.runner_id, runtime.id
            )
        cwd = (workspace / root_value).resolve()
        workspace_root = workspace.resolve()
        if cwd != workspace_root and workspace_root not in cwd.parents:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "runtime root escapes review snapshot",
                self.runner_id,
                runtime.id,
            )
        if not cwd.is_dir():
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"runtime root missing: {root_value}",
                self.runner_id,
                runtime.id,
            )

        hermetic = context.execution.for_side(side) if context.execution is not None else None
        if hermetic is not None and hermetic.policy_error:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"trusted-base execution policy is invalid: {hermetic.policy_error}",
                self.runner_id,
                runtime.id,
            )
        policy = hermetic.policy if hermetic is not None else HermeticPolicy()
        sinks: RecordingSinks | None = None
        # A trusted dispatcher may overlay another side's files (test-adequacy
        # probe: head tests on base source); YAML runtime values never can.
        overlay = runtime.values.get("overlay")
        overlay = overlay if isinstance(overlay, WorkspaceOverlay) and overlay.files else None
        if overlay is not None:
            problem = apply_workspace_overlay(workspace_root, overlay)
            if problem:
                return SurfaceRunResult(
                    surface.id, surface.provider, side, "unavailable", problem, self.runner_id, runtime.id
                )
        # A fresh report directory per run: a stale or pre-placed report never counts.
        reports_dir: Path | None = None
        if runtime.values.get("report") is True and sys.platform != "darwin":
            reports_dir = workspace.parent / "reports"
            shutil.rmtree(reports_dir, ignore_errors=True)
            reports_dir.mkdir(parents=True)
        collect_changes = runtime.values.get("collect_changes") is True
        # Runtime side-effect observation (R7): a trusted-only spec, never YAML.
        observe = runtime.values.get("observe")
        observe = observe if isinstance(observe, ObservationSpec) else None
        observer: ObservationSetup | None = None
        raw_observation: dict[str, Any] | None = None
        if observe is not None and sys.platform == "darwin":
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "runtime observation needs the bubblewrap run stage (Linux)",
                self.runner_id,
                runtime.id,
            )
        # Fingerprint the exact tree before reviewed code can modify it.
        tree_digest = workspace_digest(workspace_root)
        before_hashes = workspace_file_hashes(workspace_root) if collect_changes or observe is not None else {}
        locks = lockfile_digest(cwd)
        sandbox_cwd = "/workspace" if root_value in {"", "."} else "/workspace/" + root_value.strip("/")
        # Fetch + build stages: the reviewed lockfiles' dependencies, provisioned hermetically.
        deps = ProvisionedDependencies()
        if hermetic is not None:
            try:
                deps = provision_dependencies(cwd, policy=policy, cache=hermetic.cache, sandbox_project=sandbox_cwd)
            except (HermeticRefused, HermeticUnavailable) as exc:
                verb = "refused" if isinstance(exc, HermeticRefused) else "is unavailable"
                return SurfaceRunResult(
                    surface.id,
                    surface.provider,
                    side,
                    "unavailable",
                    f"hermetic dependency provisioning {verb}: {exc}"[:1000],
                    self.runner_id,
                    runtime.id,
                )
            if deps.mounts and sys.platform == "darwin":
                return SurfaceRunResult(
                    surface.id,
                    surface.provider,
                    side,
                    "unavailable",
                    "hermetic dependencies need the bubblewrap run stage (Linux)",
                    self.runner_id,
                    runtime.id,
                )

        if sys.platform == "darwin":
            from lemoncrow.pro.capabilities.review.macos_sandbox import sandbox_command

            try:
                sandbox = sandbox_command(
                    [sys.executable, *command[1:]] if command[0] in {"python", "python3"} else command,
                    workspace=workspace_root,
                    cwd=cwd,
                    executables=(sys.executable, "node"),
                    repo_executables=(command[0],),
                    writable_workspace=True,
                )
            except RuntimeError as exc:
                return SurfaceRunResult(
                    surface.id, surface.provider, side, "unavailable", str(exc), self.runner_id, runtime.id
                )
        else:
            assert bwrap is not None
            sandbox = [
                bwrap,
                "--die-with-parent",
                # Every namespace (IPC, UTS, cgroup, user too): reviewed code never
                # shares host SysV IPC such as X11 MIT-SHM segments.
                "--unshare-all",
                "--unshare-net",
                "--unshare-pid",
                "--new-session",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
            ]
            for mount in _command_runtime_mounts():
                sandbox.extend(("--ro-bind", mount, mount))
            for etc_path in ("/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf", "/etc/resolv.conf"):
                if Path(etc_path).is_file():
                    sandbox.extend(("--ro-bind", etc_path, etc_path))
            # Run stage: recording sinks inside the private network namespace
            # capture outbound side effects; real egress stays denied.
            if sinks_available():
                sinks = RecordingSinks.create(workspace.parent / "sinks", sidecars=policy.sidecars)
            elif policy.sidecars:
                return SurfaceRunResult(
                    surface.id,
                    surface.provider,
                    side,
                    "unavailable",
                    "trusted-base sidecars need python3 in the sandbox runtime",
                    self.runner_id,
                    runtime.id,
                )
            fixture_problem = _mount_trusted_fixtures(context, policy, workspace.parent / "fixtures", sandbox)
            if fixture_problem:
                if sinks is not None:
                    sinks.cleanup()
                return SurfaceRunResult(
                    surface.id, surface.provider, side, "unavailable", fixture_problem, self.runner_id, runtime.id
                )
            sandbox.extend(
                (
                    "--bind",
                    str(workspace_root),
                    "/workspace",
                    "--tmpfs",
                    "/tmp",
                    "--dir",
                    "/home",
                    "--clearenv",
                    "--setenv",
                    "HOME",
                    "/tmp",
                    "--setenv",
                    "PATH",
                    _COMMAND_PATH,
                    "--setenv",
                    "CI",
                    "1",
                )
            )
            if reports_dir is not None:
                sandbox.extend(("--bind", str(reports_dir), REPORT_DIR, "--setenv", REPORT_ENV, REPORT_DIR))
            # Sink endpoints are set after --clearenv, which would otherwise wipe them.
            if sinks is not None:
                sandbox.extend(sinks.bwrap_args())
            # Provisioned dependency trees, read-only, first on PATH (also after --clearenv).
            sandbox.extend(deps.bwrap_args(base_path=_COMMAND_PATH))
            if observe is not None:
                observer = ObservationSetup.create(workspace.parent / "observe", observe, sandbox_root="/workspace")
                sandbox.extend(observer.bwrap_args(pythonpath=dict(deps.env).get("PYTHONPATH", "")))
            sandbox.extend(("--chdir", sandbox_cwd, "--"))
            observed = observer.wrap(command) if observer is not None else list(command)
            sandbox += sinks.wrap(observed) if sinks is not None else observed
        # What actually runs, probed before it runs: the record states this environment.
        identity = command_identity(
            command,
            view=command_view(
                workspace=workspace_root,
                cwd=sandbox_cwd if sys.platform != "darwin" else str(cwd),
                deps=deps,
            ),
            sinks=sinks is not None,
            strace=observer is not None and observer.tier == "strace",
        )
        started = time.monotonic()
        try:
            try:
                # Same host capacity gate and memory ceiling as preview builds.
                proc = run_limited(
                    sandbox,
                    timeout=timeout,
                    cwd=workspace_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    env={"PATH": _COMMAND_PATH, "HOME": "/tmp"},
                )
            except (OSError, subprocess.TimeoutExpired, SandboxCapacityBusy) as exc:
                return SurfaceRunResult(
                    surface.id,
                    surface.provider,
                    side,
                    "unavailable",
                    str(exc) if isinstance(exc, SandboxCapacityBusy) else f"command could not run: {exc}",
                    self.runner_id,
                    runtime.id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            # Redact before the output is returned, persisted or shown.
            output, _redactions = protect_text(proc.stdout.strip()[-8000:], policy.redaction)
            status = "passed" if proc.returncode == 0 else "failed"
            note = limit_note(proc.returncode)
            content_access = hermetic.content_access if hermetic is not None else "ai_denied"
            captures: tuple[Any, ...] = ()
            refs: tuple[Any, ...] = ()
            artifacts = (
                context.store.run_artifacts(retention_seconds=policy.retention_seconds)
                if context.store is not None
                else (
                    hermetic.artifacts
                    if hermetic is not None
                    else RunArtifactStore.for_store(context.store_root, retention_seconds=policy.retention_seconds)
                )
            )
            if sinks is not None:
                captures, refs = sinks.collect(
                    review_id=context.revision.review_id,
                    policy=policy.redaction,
                    content_access=content_access,
                    artifacts=artifacts,
                )
            output_refs = list(refs)
            # stderr is merged into stdout by the current sandbox invocation.
            # Preserve the bounded, redacted combined log without pretending
            # to have independently captured either stream.
            if artifacts is not None and output:
                protected_log, _ = protect_bytes(output.encode("utf-8"), policy.redaction)
                output_refs.append(
                    artifacts.put(
                        context.revision.review_id,
                        protected_log,
                        role="log",
                        media_type="text/plain; charset=utf-8",
                        owner=f"review:{context.revision.id}",
                    )
                )
            if observer is not None:
                raw_observation = observer.collect(command)
        finally:
            if sinks is not None:
                sinks.cleanup()
            if observer is not None:
                observer.cleanup()
        extra: dict[str, Any] = {}
        if reports_dir is not None:
            report_text, truncated = read_report(reports_dir)
            shutil.rmtree(reports_dir, ignore_errors=True)
            # Redacted like output: a report is reviewed code's own text.
            protected_report = protect_text(report_text, policy.redaction)[0] if report_text else ""
            extra["report"] = protected_report
            extra["report_truncated"] = truncated
            if artifacts is not None and protected_report:
                protected_bytes, _ = protect_bytes(protected_report.encode("utf-8"), policy.redaction)
                output_refs.append(
                    artifacts.put(
                        context.revision.review_id,
                        protected_bytes,
                        role="report",
                        media_type="text/plain; charset=utf-8",
                        owner=f"review:{context.revision.id}",
                    )
                )
        if collect_changes:
            extra["changes"] = collect_workspace_changes(workspace_root, before_hashes)
        if observer is not None:
            # Bounded and redacted here, before the observation is returned or stored.
            extra["observation"] = build_observation(
                side=side,
                raw=raw_observation,
                sink_captures=captures,
                workspace_changes=workspace_change_rows(before_hashes, workspace_file_hashes(workspace_root)),
                output=output,
                sandbox_root="/workspace",
                policy=policy.redaction,
            ).to_wire()
        execution = build_execution_result(
            subject=ExecutionSubject(
                review_id=context.revision.review_id,
                revision_id=context.revision.id,
                side=side,
                snapshot_identity=revision_snapshot_identity(context.revision, side),
                scope=root_value,
                workspace_digest=tree_digest,
                overlay_source=overlay.source if overlay is not None else "",
                overlay=overlay.digests() if overlay is not None else (),
            ),
            # The dispatcher re-stamps this; a runner alone never claims a trusted class.
            executor_class=hermetic.executor_class if hermetic is not None else ExecutorClass.AUTHOR_MACHINE,
            policy=policy,
            recipe=ExecutionRecipe(
                command=tuple(command),
                cwd=root_value,
                env=(
                    ("CI", "1"),
                    *(((REPORT_ENV, REPORT_DIR),) if reports_dir is not None else ()),
                    # The observer (and its tier) is part of the recipe, hence of the fingerprint.
                    *(sorted(observer.environment().items()) if observer is not None else ()),
                ),
                stages=("fetch", "build", "run") if deps.stages else ("run",),
                sinks=sinks is not None,
                policy=policy.to_wire(),
            ),
            stages=(
                *deps.stages,
                StageOutcome(
                    "run",
                    "passed" if status == "passed" else "failed",
                    network="denied",
                    detail=f"command exited {proc.returncode}" + (f" ({note})" if note else ""),
                    exit_code=proc.returncode,
                    duration_ms=duration_ms,
                    output=output[-2000:],
                ),
            ),
            namespace=hermetic.namespace if hermetic is not None else cache_namespace(side, "unknown"),
            content_access=content_access,
            environment=identity,
            captures=captures,
            artifacts=output_refs,
            sinks_active=sinks is not None,
            sandbox=_command_sandbox(),
            lockfile_digest=deps.lockfile_digest or locks,
            reproduce=REPRODUCE_COMMAND,
        )
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            status,
            f"command exited {proc.returncode}" + (f" ({note})" if note else ""),
            self.runner_id,
            runtime.id,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            output=output,
            data={"execution": protect_execution_result(execution, policy.redaction).to_wire(), **extra},
        )


def revision_snapshot_identity(revision: Any, side: str) -> str:
    from lemoncrow.pro.capabilities.review.web_preview import revision_side_identity

    try:
        return str(revision_side_identity(revision, side))
    except (AttributeError, TypeError):
        return ""


def _mount_trusted_fixtures(context: SurfaceContext, policy: HermeticPolicy, root: Path, sandbox: list[str]) -> str:
    """Mount trusted-base seed fixtures read-only at ``/fixtures/<path>``.

    Fixture directories are materialized from the trusted base (old) side only,
    never from the reviewed head. Returns a problem string, or ``""``.
    """

    if not policy.fixtures:
        return ""
    sandbox.extend(("--dir", "/fixtures"))
    for index, fixture in enumerate(policy.fixtures):
        target = root / str(index)
        try:
            materialize_review_side(
                context.store_root,
                context.source_root or context.repo_root,
                context.revision,
                side="old",
                target=target,
                scope=fixture.path,
                store=context.store,
            )
        except (ReviewSnapshotUnavailable, KeyError, OSError, RuntimeError, ValueError) as exc:
            return f"trusted-base fixture {fixture.path} is unavailable: {exc}"
        source = (target / fixture.path).resolve()
        if not source.is_dir() or (source != target.resolve() and target.resolve() not in source.parents):
            return f"trusted-base fixture {fixture.path} is not a directory on the trusted base"
        mount = "/fixtures/" + fixture.path.strip("/")
        sandbox.extend(("--ro-bind", str(source), mount))
    return ""


_HTTP_SERVICE_SUPERVISOR = r"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

cfg = json.loads(sys.argv[1])
server_env = os.environ.copy()
server_env[cfg["port_env"]] = str(cfg["port"])
server_env["PYTHONDONTWRITEBYTECODE"] = "1"
import tempfile
log = tempfile.TemporaryFile(mode="w+b")
server = subprocess.Popen(
    cfg["command"],
    env=server_env,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
started = time.monotonic()
try:
    deadline = started + cfg["health_timeout"]
    health_url = f'http://127.0.0.1:{cfg["port"]}{cfg["health"]}'
    while True:
        if server.poll() is not None:
            raise RuntimeError(f"service exited before health check with code {server.returncode}")
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as response:
                if 200 <= response.status < 500:
                    break
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"service did not become healthy at {cfg['health']}")
        time.sleep(0.1)

    request_url = f'http://127.0.0.1:{cfg["port"]}{cfg["path"]}'
    request = urllib.request.Request(request_url, method=cfg["method"])
    status_code = 0
    headers = {}
    body = ""
    try:
        with urllib.request.urlopen(request, timeout=cfg["request_timeout"]) as response:
            status_code = response.status
            headers = dict(response.headers.items())
            body = response.read(cfg["max_body_bytes"] + 1).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        headers = dict(exc.headers.items()) if exc.headers else {}
        body = exc.read(cfg["max_body_bytes"] + 1).decode("utf-8", errors="replace")
    truncated = len(body.encode("utf-8")) > cfg["max_body_bytes"]
    if truncated:
        body = body.encode("utf-8")[:cfg["max_body_bytes"]].decode("utf-8", errors="replace")
    parsed = None
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        pass
    print(json.dumps({
        "status_code": status_code,
        "headers": headers,
        "body": parsed if parsed is not None else body,
        "truncated": truncated,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }, separators=(",", ":")))
except Exception:
    log.seek(max(0, log.seek(0, 2) - 8000))
    sys.stderr.write(log.read().decode("utf-8", errors="replace"))
    raise
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=2)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=2)
    log.close()
"""


_MACOS_SERVICE_PORTS: set[int] = set()
_MACOS_SERVICE_PORT_LOCK = threading.Lock()


@contextmanager
def _macos_service_port() -> Iterator[socket.socket]:
    """Lease a free port for the whole service run, including capacity waits."""
    reservation = socket.socket()
    port = None
    try:
        while True:
            reservation.bind(("127.0.0.1", 0))
            candidate = reservation.getsockname()[1]
            with _MACOS_SERVICE_PORT_LOCK:
                if candidate not in _MACOS_SERVICE_PORTS:
                    _MACOS_SERVICE_PORTS.add(candidate)
                    port = candidate
                    break
            reservation.close()
            reservation = socket.socket()
        yield reservation
    finally:
        reservation.close()
        if port is not None:
            with _MACOS_SERVICE_PORT_LOCK:
                _MACOS_SERVICE_PORTS.discard(port)


class HttpServiceRunner:
    """Run one revision-pinned HTTP service and exercise an API request surface.

    Linux uses an isolated bubblewrap network namespace; macOS uses Seatbelt
    restricted to the assigned loopback port. External networking is denied. The service
    is always torn down before the runner returns.
    """

    runner_id = "http-service"

    def setup_candidates(self, _repo_root: Path) -> tuple[RunnerSetupCandidate, ...]:
        return ()

    @staticmethod
    def _request_path(surface: ReviewSurface) -> str | None:
        raw = str(surface.metadata.get("url") or "").strip()
        if not raw:
            return None
        if raw.startswith("{{baseUrl}}"):
            suffix = raw[len("{{baseUrl}}") :]
            return suffix if suffix.startswith("/") else "/" + suffix
        if raw.startswith("/"):
            return raw
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            return None
        path = parsed.path or "/"
        return path + (("?" + parsed.query) if parsed.query else "")

    def run(
        self,
        context: SurfaceContext,
        runtime: RuntimeConfigEntry,
        surface: ReviewSurface,
        *,
        side: str,
    ) -> SurfaceRunResult:
        if sys.platform != "darwin":
            return self._run(context, runtime, surface, side=side)
        # Seatbelt shares the host network. Before/after and separate API
        # surfaces must each get a fresh port, even when config declares one.
        try:
            with _macos_service_port() as reservation:
                return self._run(context, runtime, surface, side=side, reservation=reservation)
        except OSError as exc:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"HTTP service port could not be allocated: {exc}",
                self.runner_id,
                runtime.id,
            )

    def _run(
        self,
        context: SurfaceContext,
        runtime: RuntimeConfigEntry,
        surface: ReviewSurface,
        *,
        side: str,
        reservation: socket.socket | None = None,
    ) -> SurfaceRunResult:
        if surface.kind != "api.request":
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "http-service currently executes API request surfaces",
                self.runner_id,
                runtime.id,
            )
        command = runtime.strings("command")
        if not command:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "http-service runtime has no command",
                self.runner_id,
                runtime.id,
            )
        request_path = self._request_path(surface)
        if request_path is None:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "API request target must be relative or loopback-bound",
                self.runner_id,
                runtime.id,
            )
        method = str(surface.metadata.get("method") or "GET").upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"unsupported HTTP method: {method}",
                self.runner_id,
                runtime.id,
            )
        bwrap = shutil.which("bwrap") or shutil.which("bubblewrap")
        if not bwrap and sys.platform != "darwin":
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "http-service requires bubblewrap; refusing to run reviewed code unsandboxed",
                self.runner_id,
                runtime.id,
            )

        try:
            port = max(1024, min(int(runtime.values.get("port", 8765)), 65535))
        except (TypeError, ValueError):
            port = 8765
        if reservation is not None:
            port = reservation.getsockname()[1]
        try:
            timeout = max(2, min(int(runtime.values.get("timeout", 30)), 180))
        except (TypeError, ValueError):
            timeout = 30
        try:
            health_timeout = max(1, min(int(runtime.values.get("health_timeout", 10)), timeout))
        except (TypeError, ValueError):
            health_timeout = min(10, timeout)
        health = runtime.string("health", "/health") or "/health"
        if not health.startswith("/"):
            health = "/" + health
        port_env = runtime.string("port_env", "PORT") or "PORT"
        root_value = runtime.string("root", ".") or "."

        digest = hashlib.sha256(f"{runtime.id}\0{surface.id}\0{side}".encode()).hexdigest()[:16]
        workspace = (
            context.store_root
            / "review"
            / "runtime-runs"
            / context.revision.review_id
            / context.revision.id
            / self.runner_id
            / digest
            / "workspace"
        )
        try:
            materialize_review_side(
                context.store_root,
                context.source_root or context.repo_root,
                context.revision,
                side=side,
                target=workspace,
                scope=root_value,
                store=context.store,
            )
        except ReviewSnapshotUnavailable as exc:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                str(exc),
                self.runner_id,
                runtime.id,
            )
        workspace_root = workspace.resolve()
        cwd = (workspace / root_value).resolve()
        if cwd != workspace_root and workspace_root not in cwd.parents:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                "runtime root escapes review snapshot",
                self.runner_id,
                runtime.id,
            )
        if not cwd.is_dir():
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"runtime root missing: {root_value}",
                self.runner_id,
                runtime.id,
            )

        sandbox_cwd = "/workspace" if root_value in {"", "."} else "/workspace/" + root_value.strip("/")
        config = {
            "command": (
                [sys.executable, *command[1:]]
                if sys.platform == "darwin" and command[0] in {"python", "python3"}
                else list(command)
            ),
            "port": port,
            "port_env": port_env,
            "health": health,
            "health_timeout": health_timeout,
            "request_timeout": min(10, timeout),
            "max_body_bytes": 256_000,
            "method": method,
            "path": request_path,
        }
        if sys.platform == "darwin":
            from lemoncrow.pro.capabilities.review.macos_sandbox import sandbox_command

            try:
                sandbox = sandbox_command(
                    [sys.executable, "-c", _HTTP_SERVICE_SUPERVISOR, json.dumps(config, separators=(",", ":"))],
                    workspace=workspace_root,
                    cwd=cwd,
                    executables=(sys.executable, "node"),
                    repo_executables=(command[0],),
                    read_roots=(Path(sys.base_prefix),),
                    loopback_port=port,
                )
            except RuntimeError as exc:
                return SurfaceRunResult(
                    surface.id,
                    surface.provider,
                    side,
                    "unavailable",
                    str(exc),
                    self.runner_id,
                    runtime.id,
                )
        else:
            assert bwrap is not None
            sandbox = [
                bwrap,
                "--die-with-parent",
                "--unshare-all",
                "--unshare-net",
                "--unshare-pid",
                "--new-session",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--ro-bind",
                "/usr",
                "/usr",
                "--ro-bind",
                "/bin",
                "/bin",
                "--ro-bind",
                "/lib",
                "/lib",
            ]
            if Path("/usr/local").exists():
                sandbox.extend(("--ro-bind", "/usr/local", "/usr/local"))
            if Path("/lib64").exists():
                sandbox.extend(("--ro-bind", "/lib64", "/lib64"))
            for etc_path in ("/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf", "/etc/resolv.conf"):
                if Path(etc_path).is_file():
                    sandbox.extend(("--ro-bind", etc_path, etc_path))
            sandbox.extend(
                (
                    "--ro-bind",
                    str(workspace_root),
                    "/workspace",
                    "--tmpfs",
                    "/tmp",
                    "--dir",
                    "/home",
                    "--clearenv",
                    "--setenv",
                    "HOME",
                    "/tmp",
                    "--setenv",
                    "PATH",
                    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "--setenv",
                    "CI",
                    "1",
                    "--chdir",
                    sandbox_cwd,
                    "--",
                    "python3",
                    "-c",
                    _HTTP_SERVICE_SUPERVISOR,
                    json.dumps(config, separators=(",", ":")),
                )
            )
        started = time.monotonic()
        try:
            # Generic service commands cannot inherit a bound listener. Keep
            # the in-process lease until teardown, and release the socket only
            # once the sandbox and its single-port policy are ready.
            # lc-debt: external processes can claim this port during handoff;
            # socket activation is the upgrade path for runners supporting it.
            if reservation is not None:
                reservation.close()
            # Same host capacity gate and memory ceiling as preview builds.
            proc = run_limited(
                sandbox,
                timeout=timeout + 5,
                cwd=workspace_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp"},
            )
        except (OSError, subprocess.TimeoutExpired, SandboxCapacityBusy) as exc:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                str(exc) if isinstance(exc, SandboxCapacityBusy) else f"HTTP service could not run: {exc}",
                self.runner_id,
                runtime.id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        output = proc.stdout.strip()
        if proc.returncode != 0:
            note = limit_note(proc.returncode)
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "failed",
                "HTTP service/request execution failed" + (f" ({note})" if note else ""),
                self.runner_id,
                runtime.id,
                exit_code=proc.returncode,
                duration_ms=duration_ms,
                output=output[-8000:],
            )
        try:
            payload = json.loads(output.splitlines()[-1])
        except (IndexError, TypeError, ValueError):
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "failed",
                "HTTP service returned an unreadable result",
                self.runner_id,
                runtime.id,
                exit_code=proc.returncode,
                duration_ms=duration_ms,
                output=output[-8000:],
            )
        status_code = int(payload.get("status_code") or 0)
        status_name = "passed" if 200 <= status_code < 400 else "failed"
        data = {
            "status_code": status_code,
            "headers": payload.get("headers") or {},
            "body": payload.get("body"),
            "truncated": bool(payload.get("truncated")),
            "assertions": [],
            "assertions_executed": False,
        }
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            status_name,
            f"{method} {request_path} returned HTTP {status_code}",
            self.runner_id,
            runtime.id,
            exit_code=0,
            duration_ms=int(payload.get("duration_ms") or duration_ms),
            data=data,
        )


def load_entrypoint_runners() -> tuple[ReviewRunner, ...]:
    found: list[ReviewRunner] = []
    try:
        points = metadata.entry_points(group=RUNNER_ENTRYPOINT_GROUP)
    except TypeError:  # pragma: no cover
        points = metadata.entry_points().select(group=RUNNER_ENTRYPOINT_GROUP)
    for point in points:
        candidate = point.load()
        runner = candidate() if isinstance(candidate, type) else candidate
        if not isinstance(runner, ReviewRunner):
            raise TypeError(f"entry point {point.name!r} does not implement ReviewRunner")
        found.append(runner)
    return tuple(found)


def built_in_runner_registry(*, include_entrypoints: bool = True) -> RunnerRegistry:
    runners: list[ReviewRunner] = [DockerComposeRunner(), CommandRunner(), HttpServiceRunner()]
    if include_entrypoints:
        runners.extend(load_entrypoint_runners())
    return RunnerRegistry(runners, environment_probe=command_environment)


__all__ = [
    "RUNNER_ENTRYPOINT_GROUP",
    "CommandRunner",
    "DockerComposeRunner",
    "HttpServiceRunner",
    "ReviewRunner",
    "RunnerRegistry",
    "RunnerSetupCandidate",
    "SurfaceRunResult",
    "built_in_runner_registry",
    "command_environment",
    "command_identity",
    "command_view",
    "load_entrypoint_runners",
]
