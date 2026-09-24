"""Self-contained runtime plugins for LemonCrow Review.

Surface providers discover *what* is reviewable. Runner plugins own *how* an
immutable review side is executed. A Bruno request, web route, or service can
therefore bind to Docker Compose in one repository and a plain process runner in
another without either surface provider knowing the execution technology.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import yaml

from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, materialize_review_side
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry, SurfaceContext

RUNNER_ENTRYPOINT_GROUP = "lemoncrow.review_runners"
_COMPOSE_NAMES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".jj",
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
    def __init__(self, runners: Iterable[ReviewRunner] = ()) -> None:
        self._runners: dict[str, ReviewRunner] = {}
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

    def run(self, context: SurfaceContext, surface: ReviewSurface, *, side: str) -> SurfaceRunResult:
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
        return runner.run(context, runtime, surface, side=side)


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
        standalone = shutil.which("docker-compose")
        docker = shutil.which("docker")
        if standalone:
            compose_command = [standalone]
            compose_executable = standalone
        elif docker:
            compose_command = [docker, "compose"]
            compose_executable = docker
        else:
            return self._result(surface, runtime, side, "unavailable", "Docker Compose CLI is not installed")

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
                context.repo_root,
                context.revision,
                side=side,
                target=workspace,
                store=context.store,
            )
        except ReviewSnapshotUnavailable as exc:
            return self._result(surface, runtime, side, "unavailable", str(exc))

        command = [*compose_command, "--project-directory", str(workspace)]
        root = workspace.resolve()
        for rel in compose_files:
            candidate = (workspace / rel).resolve()
            if candidate != root and root not in candidate.parents:
                return self._result(surface, runtime, side, "unavailable", "compose path escapes review snapshot")
            if not candidate.is_file():
                return self._result(surface, runtime, side, "unavailable", f"compose file missing: {rel}")
            command.extend(("-f", str(candidate)))
        command.extend(("config", "--services"))

        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
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
        if not bwrap:
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
                context.repo_root,
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

        sandbox_cwd = "/workspace" if root_value in {"", "."} else "/workspace/" + root_value.strip("/")
        sandbox = [
            bwrap,
            "--die-with-parent",
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
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "--setenv",
                "CI",
                "1",
                "--chdir",
                sandbox_cwd,
                "--",
            )
        )
        started = time.monotonic()
        try:
            proc = subprocess.run(
                sandbox + list(command),
                cwd=workspace_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=timeout,
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"command could not run: {exc}",
                self.runner_id,
                runtime.id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        output = proc.stdout.strip()[-8000:]
        status = "passed" if proc.returncode == 0 else "failed"
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            status,
            f"command exited {proc.returncode}",
            self.runner_id,
            runtime.id,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            output=output,
        )


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
server = subprocess.Popen(
    cfg["command"],
    env=server_env,
    stdout=subprocess.DEVNULL,
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
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=2)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=2)
"""


class HttpServiceRunner:
    """Run one revision-pinned HTTP service and exercise an API request surface.

    The server and request client share one isolated bubblewrap network namespace,
    so loopback works while external networking remains unavailable. The service
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
        if not bwrap:
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
                context.repo_root,
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
            "command": list(command),
            "port": port,
            "port_env": port_env,
            "health": health,
            "health_timeout": health_timeout,
            "request_timeout": min(10, timeout),
            "max_body_bytes": 256_000,
            "method": method,
            "path": request_path,
        }
        sandbox = [
            bwrap,
            "--die-with-parent",
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
            proc = subprocess.run(
                sandbox,
                cwd=workspace_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=timeout + 5,
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "unavailable",
                f"HTTP service could not run: {exc}",
                self.runner_id,
                runtime.id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        output = proc.stdout.strip()
        if proc.returncode != 0:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "failed",
                "HTTP service/request execution failed",
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
    return RunnerRegistry(runners)


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
    "load_entrypoint_runners",
]
