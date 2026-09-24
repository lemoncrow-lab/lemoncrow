from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.review.review_setup import inspect_review_setup
from lemoncrow.pro.capabilities.review.runtimes import (
    CommandRunner,
    DockerComposeRunner,
    HttpServiceRunner,
    RunnerRegistry,
    SurfaceRunResult,
)
from lemoncrow.pro.capabilities.review.surface_providers import (
    BrunoSurfaceProvider,
    ServiceSurfaceProvider,
    WebSurfaceProvider,
)
from lemoncrow.pro.capabilities.review.surfaces import (
    ReviewSurface,
    SurfaceContext,
    SurfaceRegistry,
    load_review_surface_config,
    write_review_surface_config,
)


def _revision() -> SimpleNamespace:
    return SimpleNamespace(
        id="revision",
        review_id="review",
        revision_number=1,
        range_mode="commit_range",
        base_sha="base",
        head_sha="head",
        tree_fingerprint="tree",
        source_fingerprint="source",
    )


def test_review_config_round_trip_separates_surfaces_and_runtimes(tmp_path: Path) -> None:
    target = write_review_surface_config(
        tmp_path,
        (
            SimpleNamespace(
                to_config=lambda: {
                    "id": "app",
                    "provider": "web",
                    "root": "frontend",
                    "framework": "next",
                    "runtime": "frontend-dev",
                }
            ),
        ),
        runtime_candidates=(
            {
                "id": "frontend-dev",
                "runner": "command",
                "root": "frontend",
                "command": ["npm", "test"],
            },
        ),
    )
    assert target == tmp_path / ".lemoncrow" / "review.yaml"
    config = load_review_surface_config(tmp_path)
    assert config.version == 1
    assert config.surfaces[0].provider == "web"
    assert config.surfaces[0].string("runtime") == "frontend-dev"
    assert config.runtimes[0].runner == "command"
    assert config.runtimes[0].strings("command") == ("npm", "test")

    target.write_text("version: 1\nsurfaces:\n  - id: bad\n    provider: web\n    root: ../outside\n")
    with pytest.raises(ValueError, match="inside the repository"):
        load_review_surface_config(tmp_path)


def test_config_rejects_surface_bound_to_unknown_runtime(tmp_path: Path) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\nsurfaces:\n  - id: api\n    provider: bruno\n    runtime: missing\n"
    )
    with pytest.raises(ValueError, match="unknown runtime"):
        load_review_surface_config(tmp_path)


def test_registry_deduplicates_provider_surface_identity(tmp_path: Path) -> None:
    class Provider:
        provider_id = "fixture"

        def discover(self, _context: SurfaceContext) -> tuple[ReviewSurface, ...]:
            return (
                ReviewSurface(
                    id="one",
                    provider="fixture",
                    kind="fixture",
                    title="One",
                    affected_paths=("src/a.ts",),
                ),
                ReviewSurface(
                    id="one",
                    provider="fixture",
                    kind="fixture",
                    title="One",
                    affected_paths=("src/b.ts",),
                ),
            )

        def setup_candidates(self, _repo_root: Path):
            return ()

    registry = SurfaceRegistry((Provider(),))
    context = SurfaceContext(tmp_path, tmp_path, _revision(), ("x.ts",), load_review_surface_config(tmp_path))
    assert registry.discover(context) == (
        ReviewSurface(
            id="one",
            provider="fixture",
            kind="fixture",
            title="One",
            affected_paths=("src/a.ts", "src/b.ts"),
        ),
    )


def test_setup_detects_surfaces_and_docker_runtime_independently(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(json.dumps({"dependencies": {"next": "16"}}))

    bruno = tmp_path / "backend" / "bruno"
    bruno.mkdir(parents=True)
    (bruno / "bruno.json").write_text('{"version":"1","name":"api","type":"collection"}')
    (bruno / "environments").mkdir()
    (bruno / "environments" / "development.bru").write_text("vars {\n  baseUrl: http://localhost:8000\n}\n")

    (tmp_path / "compose.yml").write_text("services:\n  app:\n    image: fixture\n  api:\n    image: fixture\n")

    surfaces = SurfaceRegistry((WebSurfaceProvider(), ServiceSurfaceProvider(), BrunoSurfaceProvider()))
    runners = RunnerRegistry((DockerComposeRunner(),))
    report = inspect_review_setup(tmp_path, registry=surfaces, runner_registry=runners)
    by_provider = {candidate.provider: candidate for candidate in report.candidates}
    assert by_provider["web"].root == "frontend"
    assert by_provider["web"].details["framework"] == "next"
    assert by_provider["bruno"].details["collection"] == "backend/bruno"
    assert by_provider["bruno"].details["environment"] == "development"

    assert len(report.runtime_candidates) == 1
    runtime = report.runtime_candidates[0]
    assert runtime.runner == "docker-compose"
    assert runtime.details["compose"] == ["compose.yml"]
    assert runtime.details["services"] == ["app", "api"]
    assert runtime.auto_write is True


def test_setup_ignores_generated_and_archived_web_projects(tmp_path: Path) -> None:
    real = tmp_path / "frontend"
    real.mkdir()
    (real / "package.json").write_text(json.dumps({"dependencies": {"next": "16"}}))
    for ignored in ("deleted", "archive", ".next-review"):
        generated = tmp_path / ignored / "nested"
        generated.mkdir(parents=True)
        (generated / "package.json").write_text(json.dumps({"dependencies": {"next": "16"}}))

    candidates = WebSurfaceProvider().setup_candidates(tmp_path)
    assert [candidate.root for candidate in candidates] == ["frontend"]


def test_bruno_provider_only_executes_when_bound_to_runtime(tmp_path: Path) -> None:
    collection = tmp_path / "backend" / "bruno"
    collection.mkdir(parents=True)
    (collection / "bruno.json").write_text('{"version":"1"}')
    (collection / "Get Item.bru").write_text(
        "meta {\n  name: Get Item\n}\nget {\n  url: {{baseUrl}}/items/:id\n  body: none\n}\n"
    )
    (tmp_path / ".lemoncrow").mkdir()
    config_path = tmp_path / ".lemoncrow" / "review.yaml"
    config_path.write_text("version: 1\nsurfaces:\n  - id: api\n    provider: bruno\n    collection: backend/bruno\n")
    context = SurfaceContext(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        revision=_revision(),
        changed_paths=("backend/bruno/Get Item.bru",),
        config=load_review_surface_config(tmp_path),
    )
    surface = BrunoSurfaceProvider().discover(context)[0]
    assert surface.locator == "GET {{baseUrl}}/items/:id"
    assert surface.runtime == ""
    assert surface.capabilities == ("source",)
    assert surface.metadata["runtime_required"] is True

    config_path.write_text(
        "version: 1\n"
        "runtimes:\n  - id: api-runtime\n    runner: command\n    command: [python, -m, fixture]\n"
        "surfaces:\n  - id: api\n    provider: bruno\n    collection: backend/bruno\n    runtime: api-runtime\n"
    )
    context = SurfaceContext(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        revision=_revision(),
        changed_paths=("backend/bruno/Get Item.bru",),
        config=load_review_surface_config(tmp_path),
    )
    surface = BrunoSurfaceProvider().discover(context)[0]
    assert surface.runtime == "api-runtime"
    assert "execute" in surface.capabilities
    assert surface.metadata["runtime_required"] is False


def test_surface_provider_and_runner_are_independent_plugins(tmp_path: Path) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n  - id: runtime-one\n    runner: fixture-runner\n"
        "surfaces:\n  - id: api\n    provider: fixture-provider\n    runtime: runtime-one\n"
    )
    context = SurfaceContext(tmp_path, tmp_path, _revision(), ("x.ts",), load_review_surface_config(tmp_path))

    class Provider:
        provider_id = "fixture-provider"

        def discover(self, _context: SurfaceContext) -> tuple[ReviewSurface, ...]:
            return (
                ReviewSurface(
                    id="api",
                    provider=self.provider_id,
                    kind="api.request",
                    title="API",
                    runtime="runtime-one",
                    capabilities=("source", "execute", "results"),
                ),
            )

        def setup_candidates(self, _repo_root: Path):
            return ()

    class Runner:
        runner_id = "fixture-runner"

        def setup_candidates(self, _repo_root: Path):
            return ()

        def run(
            self, _context: SurfaceContext, runtime: object, surface: ReviewSurface, *, side: str
        ) -> SurfaceRunResult:
            assert runtime.id == "runtime-one"
            return SurfaceRunResult(
                surface_id=surface.id,
                provider=surface.provider,
                side=side,
                status="passed",
                summary="ok",
                runner=self.runner_id,
                runtime="runtime-one",
                exit_code=0,
            )

    surface = SurfaceRegistry((Provider(),)).discover(context)[0]
    result = RunnerRegistry((Runner(),)).run(context, surface, side="new")
    assert result.status == "passed"
    assert result.provider == "fixture-provider"
    assert result.runner == "fixture-runner"
    assert result.runtime == "runtime-one"


def test_docker_compose_runner_validates_frozen_runtime_without_starting_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n"
        "  - id: stack-runtime\n"
        "    runner: docker-compose\n"
        "    compose: [compose.yml]\n"
        "    services: [app, worker]\n"
        "surfaces:\n"
        "  - id: stack\n"
        "    provider: service\n"
        "    runtime: stack-runtime\n"
    )
    context = SurfaceContext(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        revision=_revision(),
        changed_paths=("compose.yml",),
        config=load_review_surface_config(tmp_path),
    )
    surfaces = ServiceSurfaceProvider().discover(context)
    surface = surfaces[0]
    missing_surface = surfaces[1]
    materialized: list[str] = []

    def materialize(
        _store: Path,
        _repo: Path,
        _revision: object,
        *,
        side: str,
        target: Path,
        scope: str = ".",
        store: object | None = None,
    ) -> str:
        assert store is context.store
        materialized.append(side)
        target.mkdir(parents=True, exist_ok=True)
        (target / "compose.yml").write_text("services:\n  app:\n    image: fixture\n")
        return side

    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="app\n")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.runtimes.shutil.which",
        lambda name: "/usr/bin/docker-compose" if name == "docker-compose" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.subprocess.run", run)

    result = RunnerRegistry((DockerComposeRunner(),)).run(context, surface, side="new")
    missing_result = RunnerRegistry((DockerComposeRunner(),)).run(context, missing_surface, side="new")

    assert result.status == "passed"
    assert result.runner == "docker-compose"
    assert result.runtime == "stack-runtime"
    assert result.data["service"] == "app"
    assert result.data["services"] == ("app",)
    assert "service app is valid" in result.summary

    assert missing_result.status == "failed"
    assert missing_result.data["service"] == "worker"
    assert "worker" in missing_result.summary

    assert materialized == ["new", "new"]
    assert len(calls) == 2
    assert all(call[0] == "/usr/bin/docker-compose" for call in calls)
    assert all(call[-2:] == ["config", "--services"] for call in calls)
    assert all("up" not in call for call in calls)


def test_command_runner_refuses_to_execute_without_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n  - id: checks\n    runner: command\n    command: [python, -m, pytest]\n"
        "surfaces:\n  - id: checks\n    provider: service\n    runtime: checks\n    services: [tests]\n"
    )
    context = SurfaceContext(tmp_path, tmp_path / "store", _revision(), ("x.py",), load_review_surface_config(tmp_path))
    surface = ServiceSurfaceProvider().discover(context)[0]
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.shutil.which", lambda _name: None)

    result = RunnerRegistry((CommandRunner(),)).run(context, surface, side="new")

    assert result.status == "unavailable"
    assert result.runner == "command"
    assert "refusing to run reviewed code unsandboxed" in result.summary


def test_command_runner_uses_frozen_snapshot_and_networkless_bubblewrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n"
        "  - id: checks\n"
        "    runner: command\n"
        "    root: backend\n"
        "    command: [python, -m, pytest, -q]\n"
        "    timeout: 45\n"
        "surfaces:\n"
        "  - id: checks\n"
        "    provider: service\n"
        "    runtime: checks\n"
        "    services: [tests]\n"
    )
    context = SurfaceContext(
        tmp_path, tmp_path / "store", _revision(), ("backend/x.py",), load_review_surface_config(tmp_path)
    )
    surface = ServiceSurfaceProvider().discover(context)[0]
    materialized: list[str] = []

    def materialize(
        _store: Path,
        _repo: Path,
        _revision: object,
        *,
        side: str,
        target: Path,
        scope: str = ".",
        store: object | None = None,
    ) -> str:
        assert store is context.store
        materialized.append(side)
        (target / "backend").mkdir(parents=True)
        return side

    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["timeout"] == 45
        return subprocess.CompletedProcess(command, 0, stdout="2 passed\n")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.runtimes.shutil.which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.subprocess.run", run)

    result = RunnerRegistry((CommandRunner(),)).run(context, surface, side="old")

    assert result.status == "passed"
    assert result.runner == "command"
    assert result.runtime == "checks"
    assert result.output == "2 passed"
    assert materialized == ["old"]
    assert calls
    command = calls[0]
    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in command
    assert "--clearenv" in command
    assert ["--ro-bind", "/usr/local", "/usr/local"] == command[
        command.index("/usr/local") - 1 : command.index("/usr/local") + 2
    ]
    assert "/workspace/backend" in command
    assert command[-4:] == ["python", "-m", "pytest", "-q"]


def test_http_service_runner_executes_api_surface_inside_isolated_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n"
        "  - id: api-runtime\n"
        "    runner: http-service\n"
        "    root: backend\n"
        "    command: [python3, app.py]\n"
        "    health: /health\n"
        "    port: 8765\n"
        "surfaces:\n"
        "  - id: api\n"
        "    provider: bruno\n"
        "    collection: bruno\n"
        "    runtime: api-runtime\n"
        "    watch: [backend/**]\n"
    )
    (tmp_path / "bruno").mkdir()
    (tmp_path / "bruno" / "Hello.bru").write_text(
        "meta {\n  name: Hello\n}\nget {\n  url: {{baseUrl}}/api/hello?name=Ada\n}\n"
    )
    context = SurfaceContext(
        tmp_path,
        tmp_path / "store",
        _revision(),
        ("bruno/Hello.bru", "backend/app.py"),
        load_review_surface_config(tmp_path),
    )
    surface = BrunoSurfaceProvider().discover(context)[0]
    assert surface.affected_paths == ("bruno/Hello.bru", "backend/app.py")

    def materialize(
        _store: Path,
        _repo: Path,
        _revision: object,
        *,
        side: str,
        target: Path,
        scope: str = ".",
        store: object | None = None,
    ) -> str:
        assert store is context.store
        (target / "backend").mkdir(parents=True)
        (target / "backend" / "app.py").write_text("print('fixture')\n")
        return side

    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["timeout"] == 35
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status_code":200,"headers":{"Content-Type":"application/json"},"body":{"message":"Hello, Ada"},"truncated":false,"duration_ms":18}\n',
        )

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.runtimes.shutil.which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.subprocess.run", run)

    result = RunnerRegistry((HttpServiceRunner(),)).run(context, surface, side="new")

    assert result.status == "passed"
    assert result.runner == "http-service"
    assert result.runtime == "api-runtime"
    assert result.data["status_code"] == 200
    assert result.data["body"] == {"message": "Hello, Ada"}
    assert result.data["assertions_executed"] is False
    assert calls
    command = calls[0]
    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in command
    assert "--ro-bind" in command
    assert ["--ro-bind", "/usr/local", "/usr/local"] == command[
        command.index("/usr/local") - 1 : command.index("/usr/local") + 2
    ]
    assert "{{baseUrl}}" not in " ".join(command)
    assert "/api/hello?name=Ada" in " ".join(command)
