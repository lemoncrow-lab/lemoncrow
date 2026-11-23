from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.review_setup import inspect_review_setup
from lemoncrow.pro.capabilities.review.snapshot import materialize_review_side
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, record_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.surface_providers import (
    BrunoSurfaceProvider,
    ServiceSurfaceProvider,
    WebSurfaceProvider,
)
from lemoncrow.pro.capabilities.review.surfaces import (
    ReviewSurface,
    RuntimeConfigEntry,
    SurfaceContext,
    SurfaceRegistry,
    load_review_surface_config,
    write_review_surface_config,
)
from lemoncrow.pro.environment import runners as review_runtimes
from lemoncrow.pro.environment import sandbox_limits
from lemoncrow.pro.environment.runners import (
    CommandRunner,
    DockerComposeRunner,
    HttpServiceRunner,
    RunnerRegistry,
    SurfaceRunResult,
    api_response_diff_score,
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
    for ignored in ("deleted", "archive", ".next-review", ".claude", ".lc-worktrees", ".lemoncrow"):
        generated = tmp_path / ignored / "nested"
        generated.mkdir(parents=True)
        (generated / "package.json").write_text(json.dumps({"dependencies": {"next": "16"}}))

    candidates = WebSurfaceProvider().setup_candidates(tmp_path)
    assert [candidate.root for candidate in candidates] == ["frontend"]


def test_setup_ignores_internal_worktrees_for_runtime_detection(tmp_path: Path) -> None:
    (tmp_path / "compose.yml").write_text("services:\n  app:\n    image: fixture\n")
    for internal in (".lc-worktrees", ".claude/worktrees"):
        hidden = tmp_path / internal / "other"
        hidden.mkdir(parents=True)
        (hidden / "compose.yml").write_text("services:\n  duplicate:\n    image: fixture\n")

    candidates = DockerComposeRunner().setup_candidates(tmp_path)
    assert len(candidates) == 1
    assert candidates[0].details["compose"] == ["compose.yml"]


def test_web_surface_discovery_uses_trusted_source_repo_for_materialized_historical_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Fixture Tester"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "<redacted-email>"], cwd=source, check=True)
    package = source / "frontend" / "package.json"
    app = source / "frontend" / "src" / "App.tsx"
    main = source / "frontend" / "src" / "main.tsx"
    app.parent.mkdir(parents=True)
    package.write_text(json.dumps({"devDependencies": {"vite": "7"}}), encoding="utf-8")
    main.write_text('import "./App";\n', encoding="utf-8")
    app.write_text("export default function App() { return <div>before</div>; }\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=source, check=True, capture_output=True)
    app.write_text("export default function App() { return <div>after</div>; }\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "visual change"], cwd=source, check=True, capture_output=True)

    store = ReviewStore(tmp_path / "store")
    rng = resolve_rev_range(source, base="HEAD~1", head="HEAD")
    session = open_or_create_session(store, source, rng)
    build = build_review_packet_with_blobs(
        source,
        rng,
        store_root=store.root,
        with_impact=False,
        with_provenance=False,
        with_patch_text=True,
        unbounded_patch_text=True,
    )
    revision = record_revision(store, session, source, rng, store_root=store.root, build=build).revision
    materialized = tmp_path / "materialized"
    materialize_review_side(store.root, source, revision, side="new", target=materialized, store=store)
    assert not (materialized / ".git").exists()

    context = SurfaceContext(
        repo_root=materialized,
        store_root=store.root,
        revision=revision,
        changed_paths=("frontend/src/App.tsx",),
        config=load_review_surface_config(materialized),
        store=store,
        dependency_root=source,
    )
    surfaces = WebSurfaceProvider().discover(context)

    assert any(surface.locator == "/" for surface in surfaces)
    assert all(surface.affected_paths == ("frontend/src/App.tsx",) for surface in surfaces)


def test_api_response_diff_score_ignores_latency_and_volatile_headers() -> None:
    old = SurfaceRunResult(
        "api/health.bru",
        "bruno",
        "old",
        "passed",
        "old",
        duration_ms=3,
        data={
            "status_code": 200,
            "headers": {"Date": "yesterday", "Content-Type": "application/json", "X-Request-Id": "old"},
            "body": {"ok": True, "items": [1, 2]},
        },
    )
    new = SurfaceRunResult(
        "api/health.bru",
        "bruno",
        "new",
        "passed",
        "new",
        duration_ms=999,
        data={
            "status_code": 200,
            "headers": {"Date": "today", "Content-Type": "application/json", "X-Request-Id": "new"},
            "body": {"items": [1, 2], "ok": True},
        },
    )

    score = api_response_diff_score(old, new)

    assert score.status_changed is False
    assert score.body_changed_ratio == 0
    assert score.header_changed_ratio == 0


def test_api_response_diff_score_ranks_status_body_and_stable_header_changes() -> None:
    old = SurfaceRunResult(
        "api/health.bru",
        "bruno",
        "old",
        "passed",
        "old",
        data={"status_code": 200, "headers": {"Content-Type": "application/json", "ETag": "a"}, "body": {"ok": True}},
    )
    new = SurfaceRunResult(
        "api/health.bru",
        "bruno",
        "new",
        "failed",
        "new",
        data={"status_code": 503, "headers": {"Content-Type": "application/json", "ETag": "b"}, "body": {"ok": False}},
    )

    score = api_response_diff_score(old, new)

    assert score.status_changed is True
    assert score.old_status_code == 200
    assert score.new_status_code == 503
    assert score.body_changed_ratio > 0
    assert score.header_changed_ratio > 0


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


def test_bruno_provider_hides_endpoints_unrelated_to_the_snapshot(tmp_path: Path) -> None:
    collection = tmp_path / "backend" / "bruno"
    collection.mkdir(parents=True)
    (collection / "bruno.json").write_text('{"version":"1"}')
    (collection / "Changed.bru").write_text("meta {\n  name: Changed\n}\nget {\n  url: {{baseUrl}}/changed\n}\n")
    (collection / "Unrelated.bru").write_text("meta {\n  name: Unrelated\n}\nget {\n  url: {{baseUrl}}/unrelated\n}\n")
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\nsurfaces:\n  - id: api\n    provider: bruno\n    collection: backend/bruno\n"
    )
    context = SurfaceContext(
        repo_root=tmp_path,
        store_root=tmp_path / "store",
        revision=_revision(),
        changed_paths=("backend/bruno/Changed.bru",),
        config=load_review_surface_config(tmp_path),
    )

    surfaces = BrunoSurfaceProvider().discover(context)

    assert [surface.id for surface in surfaces] == ["backend/bruno/Changed.bru"]


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

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runner_host.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.environment.runners.shutil.which",
        lambda name, **_kwargs: {"docker-compose": "/usr/bin/docker-compose", "bwrap": "/usr/bin/bwrap"}.get(name),
    )
    monkeypatch.setattr("lemoncrow.pro.environment.runners.subprocess.run", run)

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
    assert all(call[0] == "/usr/bin/bwrap" and "--unshare-all" in call for call in calls)
    assert all("/usr/bin/docker-compose" in call for call in calls)
    assert all(call[-2:] == ["config", "--services"] for call in calls)
    assert all("up" not in call for call in calls)


def test_docker_compose_validation_refuses_to_run_without_a_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(
        "lemoncrow.pro.environment.runners.shutil.which",
        lambda name, **_kwargs: "/usr/bin/docker-compose" if name == "docker-compose" else None,
    )

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("reviewed compose files must never be parsed unsandboxed")

    monkeypatch.setattr("lemoncrow.pro.environment.runners.subprocess.run", run)
    context = SimpleNamespace(repo_root=tmp_path, store_root=tmp_path / "store", source_root=None, store=None)
    runtime = RuntimeConfigEntry("stack", "docker-compose", {"compose": ["compose.yml"]})
    surface = SimpleNamespace(id="app", provider="service", kind="service", metadata={"service": "app"})

    result = DockerComposeRunner().run(context, runtime, surface, side="new")  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert "bubblewrap" in result.summary


@pytest.mark.skipif(
    not (shutil.which("docker") or shutil.which("docker-compose")) or not shutil.which("bwrap"),
    reason="requires Docker Compose and bubblewrap",
)
def test_docker_compose_validation_cannot_read_host_files_outside_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewed compose file is parsed sandboxed: an ``include`` of a host path fails."""

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "compose.yaml").write_text("services:\n  leaked:\n    image: fixture\n")
    context = SimpleNamespace(
        repo_root=tmp_path / "repo",
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id="review", id="revision"),
    )

    def materialize(*_args: object, target: Path, **_kwargs: object) -> str:
        target.mkdir(parents=True, exist_ok=True)
        (target / "compose.yml").write_text(
            f"include:\n  - path: {outside / 'compose.yaml'}\nservices:\n  app:\n    image: fixture\n"
        )
        (target / "ok.yml").write_text("services:\n  app:\n    image: fixture\n")
        return "identity"

    monkeypatch.setattr(review_runtimes, "materialize_review_side", materialize)
    surface = SimpleNamespace(id="app", provider="service", kind="service", metadata={"service": "app"})

    escaping = RuntimeConfigEntry("stack", "docker-compose", {"compose": ["compose.yml"]})
    result = DockerComposeRunner().run(context, escaping, surface, side="new")  # type: ignore[arg-type]
    assert "leaked" not in result.data.get("services", ())
    assert result.status == "failed", result.summary

    plain = RuntimeConfigEntry("plain", "docker-compose", {"compose": ["ok.yml"]})
    result = DockerComposeRunner().run(context, plain, surface, side="new")  # type: ignore[arg-type]
    assert result.status == "passed", result.output or result.summary


def test_command_runner_refuses_to_execute_without_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n  - id: checks\n    runner: command\n    command: [python, -m, pytest]\n"
        "surfaces:\n  - id: checks\n    provider: service\n    runtime: checks\n    services: [tests]\n"
    )
    context = SurfaceContext(tmp_path, tmp_path / "store", _revision(), ("x.py",), load_review_surface_config(tmp_path))
    surface = ServiceSurfaceProvider().discover(context)[0]
    monkeypatch.setattr("lemoncrow.pro.environment.runners.shutil.which", lambda _name, **_kwargs: None)

    result = RunnerRegistry((CommandRunner(),)).run(context, surface, side="new")

    assert result.status == "unavailable"
    assert result.runner == "command"
    assert "refusing to run reviewed code unsandboxed" in result.summary


@pytest.mark.parametrize(
    ("platform", "returncode", "stderr", "expected"),
    [
        ("linux", 1, "bwrap: execvp nope: No such file or directory\n", "unavailable"),
        ("darwin", 71, "sandbox-exec: execvp() of 'nope' failed: No such file or directory\n", "unavailable"),
        ("linux", 127, "command could not start: [Errno 2] No such file or directory: 'nope'\n", "unavailable"),
        ("linux", 127, "sh: 1: nope: not found\n", "failed"),
        # The command itself ran and failed, even if it printed something similar earlier.
        ("linux", 1, "bwrap: execvp nope: No such file or directory\nthen it ran\nassert failed\n", "failed"),
        ("linux", 2, "bwrap: execvp nope: No such file or directory\n", "failed"),
        ("darwin", 1, "assertion failed\n", "failed"),
    ],
)
def test_a_command_the_sandbox_could_not_start_is_not_a_failed_check(
    platform: str, returncode: int, stderr: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.environment.runners import _sandbox_start_failure

    monkeypatch.setattr("sys.platform", platform)
    assert bool(_sandbox_start_failure(returncode, stderr)) is (expected == "unavailable")


def test_a_missing_service_executable_is_unavailable_and_carries_no_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n  - id: api\n    runner: http-service\n    command: [nope]\n    health: /health\n    port: 8765\n"
        "surfaces:\n  - id: api\n    provider: bruno\n    collection: bruno\n    runtime: api\n"
    )
    (tmp_path / "bruno").mkdir()
    (tmp_path / "bruno" / "Hello.bru").write_text("meta {\n  name: Hello\n}\nget {\n  url: {{baseUrl}}/x\n}\n")
    context = SurfaceContext(
        tmp_path, tmp_path / "store", _revision(), ("bruno/Hello.bru",), load_review_surface_config(tmp_path)
    )
    surface = BrunoSurfaceProvider().discover(context)[0]

    def materialize(*_args: object, target: Path, side: str, **_kwargs: object) -> str:
        target.mkdir(parents=True, exist_ok=True)
        return side

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 127, stdout="service-not-started: No such file or directory\n")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runner_host.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.environment.runners.shutil.which",
        lambda name, **_kwargs: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.environment.runners.subprocess.run", run)

    result = RunnerRegistry((HttpServiceRunner(),)).run(context, surface, side="new")

    assert result.status == "unavailable" and "could not start" in result.summary
    assert not result.duration_ms  # normalized to NOT_RUN, not to an interrupted (UNKNOWN) run


@pytest.mark.skipif(sys.platform != "linux", reason="records the Linux executor probe (kernel, /usr toolchain)")
def test_command_runner_uses_frozen_snapshot_and_networkless_bubblewrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
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

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runner_host.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.environment.runners.shutil.which",
        lambda name, **_kwargs: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.environment.runners.run_limited_bounded", run)

    result = RunnerRegistry((CommandRunner(),)).run(context, surface, side="old")

    assert result.status == "passed"
    assert result.runner == "command"
    assert result.runtime == "checks"
    assert result.output == "2 passed"
    assert materialized == ["old"]
    assert calls
    # The bounded runner adds the host memory/task limiter itself; the runner
    # hands it the bubblewrap command.
    command = calls[0]
    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in command
    assert "--clearenv" in command
    assert ["--ro-bind", "/usr/local", "/usr/local"] == command[
        command.index("/usr/local") - 1 : command.index("/usr/local") + 2
    ]
    assert "/workspace/backend" in command
    assert command[-4:] == ["python", "-m", "pytest", "-q"]
    # Trust T3 item 1: the probe read before dispatch for this command names the executor
    # environment this run recorded (what actually ran), and the local registry carries that probe.
    from lemoncrow.pro.capabilities.review.runner_host import built_in_runner_registry, command_environment
    from lemoncrow.pro.environment.execution import ExecutionResult

    fingerprint = ExecutionResult.from_wire(result.data["execution"]).fingerprint
    recorded = fingerprint.executor_digest
    assert recorded.startswith("envx:sha256:") and command_environment(["python", "-m", "pytest", "-q"]) == recorded
    assert fingerprint.executables[0].name == "python" and fingerprint.executables[0].path.startswith("/usr/")
    assert fingerprint.kernel and fingerprint.version == 2
    assert built_in_runner_registry(include_entrypoints=False).environment_probe is command_environment
    assert RunnerRegistry((CommandRunner(),)).environment_probe is None


def test_http_service_runner_executes_api_surface_inside_isolated_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
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

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runner_host.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.environment.runners.shutil.which",
        lambda name, **_kwargs: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.environment.runners.subprocess.run", run)

    result = RunnerRegistry((HttpServiceRunner(),)).run(context, surface, side="new")

    assert result.status == "passed"
    assert result.runner == "http-service"
    assert result.runtime == "api-runtime"
    assert result.data["status_code"] == 200
    assert result.data["body"] == {"message": "Hello, Ada"}
    assert result.data["assertions_executed"] is False
    assert calls
    limiter = sandbox_limits.limit_prefix()
    assert calls[0][: len(limiter)] == limiter
    command = calls[0][len(limiter) :]
    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in command
    assert "--ro-bind" in command
    assert ["--ro-bind", "/usr/local", "/usr/local"] == command[
        command.index("/usr/local") - 1 : command.index("/usr/local") + 2
    ]
    assert "{{baseUrl}}" not in " ".join(command)
    assert "/api/hello?name=Ada" in " ".join(command)


def test_concurrent_runs_of_one_runtime_side_never_replace_each_others_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Reader runs an API surface and, a moment later, the impact ranking
    # runs the same runtime, surface and side. Each run re-materializes one
    # fixed workspace (rmtree + write): the second deleted the tree the first
    # service was starting from ("service exited before health check").
    monkeypatch.setattr("sys.platform", "linux")
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n"
        "  - id: api-runtime\n"
        "    runner: http-service\n"
        "    root: backend\n"
        "    command: [python3, app.py]\n"
        "surfaces:\n"
        "  - id: api\n"
        "    provider: bruno\n"
        "    collection: bruno\n"
        "    runtime: api-runtime\n"
        "    watch: [backend/**]\n"
    )
    (tmp_path / "bruno").mkdir()
    (tmp_path / "bruno" / "Hello.bru").write_text("meta {\n  name: Hello\n}\nget {\n  url: {{baseUrl}}/api/hello\n}\n")
    context = SurfaceContext(
        tmp_path,
        tmp_path / "store",
        _revision(),
        ("bruno/Hello.bru", "backend/app.py"),
        load_review_surface_config(tmp_path),
    )
    surface = BrunoSurfaceProvider().discover(context)[0]

    def materialize(_store: Path, _repo: Path, _revision: object, *, target: Path, **_kwargs: object) -> str:
        # What the real one does: replace the target tree.
        if target.exists():
            shutil.rmtree(target)
        (target / "backend").mkdir(parents=True)
        (target / "backend" / "app.py").write_text("print('fixture')\n")
        return "new"

    running = 0
    overlapped: list[bool] = []
    lock = threading.Lock()
    second_lock_attempt = threading.Event()
    lock_attempts = [0]
    original_run_file_lock = review_runtimes.RunFileLock

    class ObservedRunFileLock:
        def __init__(self, path: Path) -> None:
            self._inner = original_run_file_lock(path)

        def __enter__(self) -> object:
            with lock:
                lock_attempts[0] += 1
                if lock_attempts[0] == 2:
                    second_lock_attempt.set()
            return self._inner.__enter__()

        def __exit__(self, exc_type: object, exc: object, tb: object) -> object:
            return self._inner.__exit__(exc_type, exc, tb)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal running
        with lock:
            ordinal = len(overlapped)
            running += 1
            overlapped.append(running > 1)
        if ordinal == 0:
            assert second_lock_attempt.wait(timeout=5), "second run never attempted the workspace lock"
        app = Path(str(kwargs["cwd"])) / "backend" / "app.py"
        present = app.is_file()
        with lock:
            running -= 1
        body = '{"status_code":200,"headers":{},"body":"ok","truncated":false,"duration_ms":1}\n'
        return subprocess.CompletedProcess(
            command, 0 if present else 2, stdout=body if present else "server.py missing"
        )

    monkeypatch.setattr(review_runtimes, "RunFileLock", ObservedRunFileLock)
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runner_host.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.environment.runners.shutil.which",
        lambda name, **_kwargs: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.environment.runners.subprocess.run", run)

    registry = RunnerRegistry((HttpServiceRunner(),))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: registry.run(context, surface, side="new"), range(2)))

    assert [result.status for result in results] == ["passed", "passed"]
    assert overlapped == [False, False]


def _bru(name: str, method: str, url: str, pre_request: str = "") -> str:
    script = f"\nscript:pre-request {{\n{pre_request}}}\n" if pre_request else ""
    return f"meta {{\n  name: {name}\n}}\n\n{method} {{\n  url: {url}\n}}\n{script}"


def _api_context(tmp_path: Path, changed: tuple[str, ...]) -> SurfaceContext:
    (tmp_path / ".lemoncrow").mkdir(exist_ok=True)
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n  - id: api-runtime\n    runner: http-service\n    command: [python3, app.py]\n"
        "surfaces:\n  - id: api\n    provider: bruno\n    collection: bruno\n    runtime: api-runtime\n"
    )
    return SurfaceContext(tmp_path, tmp_path / "store", _revision(), changed, load_review_surface_config(tmp_path))


def test_bruno_run_request_steps_become_setup_requests_in_bruno_order(tmp_path: Path) -> None:
    folder = tmp_path / "bruno" / "invoices"
    folder.mkdir(parents=True)
    (folder / "List.bru").write_text(_bru("List", "get", "{{baseUrl}}/api/invoices?tenant=acme"))
    (folder / "Edit.bru").write_text(
        _bru("Edit", "post", "{{baseUrl}}/api/invoices/update?id=1", '  await bru.runRequest("invoices/List");\n')
    )
    (folder / "Export.bru").write_text(_bru("Export", "get", "{{baseUrl}}/api/export?tenant=acme"))
    (folder / "AfterEdit.bru").write_text(
        _bru(
            "After edit",
            "get",
            "{{baseUrl}}/api/invoices?tenant=acme",
            "  // Bruno runs these first, on the same server.\n"
            '  await bru.runRequest("invoices/Edit");\n'
            '  const exported = await bru.runRequest("invoices/Export")\n',
        )
    )
    (folder / "ReadOnly.bru").write_text(
        _bru(
            "Read only", "get", "{{baseUrl}}/api/invoices?tenant=globex", '  await bru.runRequest("invoices/Export");\n'
        )
    )
    context = _api_context(tmp_path, ("bruno/invoices/AfterEdit.bru", "bruno/invoices/ReadOnly.bru"))

    surfaces = {surface.id: surface for surface in BrunoSurfaceProvider().discover(context)}

    after_edit = surfaces["bruno/invoices/AfterEdit.bru"]
    # The nested step (Edit runs List) comes first, exactly as Bruno runs them.
    assert after_edit.metadata["setup"] == [
        {"request": "invoices/List", "method": "GET", "url": "{{baseUrl}}/api/invoices?tenant=acme"},
        {"request": "invoices/Edit", "method": "POST", "url": "{{baseUrl}}/api/invoices/update?id=1"},
        {"request": "invoices/Export", "method": "GET", "url": "{{baseUrl}}/api/export?tenant=acme"},
    ]
    # A run replays the POST, so it is executable but never automatic.
    assert "execute" in after_edit.capabilities and "auto_compare" not in after_edit.capabilities
    read_only = surfaces["bruno/invoices/ReadOnly.bru"]
    assert [row["request"] for row in read_only.metadata["setup"]] == ["invoices/Export"]
    assert "auto_compare" in read_only.capabilities


@pytest.mark.parametrize(
    ("script", "refusal"),
    [
        ('  await bru.runRequest("invoices/Missing");\n', 'bru.runRequest("invoices/Missing") names no request'),
        ('  await bru.runRequest("../outside");\n', 'bru.runRequest("../outside") names no request'),
        ('  req.setHeader("x-tenant", "acme");\n  await bru.runRequest("invoices/Self");\n', "does more than"),
        ('  await bru.runRequest("invoices/Self");\n', "already running"),
    ],
)
def test_bruno_setup_requests_it_cannot_replay_exactly_are_refused_not_dropped(
    tmp_path: Path, script: str, refusal: str
) -> None:
    folder = tmp_path / "bruno" / "invoices"
    folder.mkdir(parents=True)
    (tmp_path / "bruno" / "outside.bru").write_text(_bru("Outside", "get", "{{baseUrl}}/x"))
    (folder / "Self.bru").write_text(_bru("Self", "get", "{{baseUrl}}/api/invoices", script))
    surface = BrunoSurfaceProvider().discover(_api_context(tmp_path, ("bruno/invoices/Self.bru",)))[0]

    assert "setup" not in surface.metadata
    assert refusal in surface.metadata["setup_error"]
    assert "auto_compare" not in surface.capabilities

    runtime = SimpleNamespace(id="api-runtime", strings=lambda _key: ("python3", "app.py"))
    result = HttpServiceRunner().run(SimpleNamespace(), runtime, surface, side="new")
    assert result.status == "unavailable"
    assert result.summary.startswith("pre-request steps cannot run:") and refusal in result.summary


def test_bruno_setup_chain_past_the_cap_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "bruno"
    folder.mkdir()
    from lemoncrow.pro.capabilities.review.surface_providers import MAX_SETUP_REQUESTS

    for index in range(MAX_SETUP_REQUESTS + 2):
        script = f'  await bru.runRequest("Step{index - 1}");\n' if index else ""
        (folder / f"Step{index}.bru").write_text(_bru(f"Step {index}", "get", f"{{{{baseUrl}}}}/s/{index}", script))
    at_cap = f"bruno/Step{MAX_SETUP_REQUESTS}.bru"
    past_cap = f"bruno/Step{MAX_SETUP_REQUESTS + 1}.bru"
    surfaces = {
        surface.id: surface for surface in BrunoSurfaceProvider().discover(_api_context(tmp_path, (at_cap, past_cap)))
    }

    assert len(surfaces[at_cap].metadata["setup"]) == MAX_SETUP_REQUESTS
    assert f"more than {MAX_SETUP_REQUESTS} requests" in surfaces[past_cap].metadata["setup_error"]


def test_http_service_runner_sends_setup_requests_to_the_supervisor_and_keeps_their_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    (tmp_path / "bruno").mkdir()
    (tmp_path / "bruno" / "Seed.bru").write_text(_bru("Seed", "post", "{{baseUrl}}/seed?n=1"))
    (tmp_path / "bruno" / "Read.bru").write_text(
        _bru("Read", "get", "{{baseUrl}}/read", '  await bru.runRequest("Seed");\n')
    )
    context = _api_context(tmp_path, ("bruno/Read.bru",))
    surface = BrunoSurfaceProvider().discover(context)[0]

    def materialize(_store: Path, _repo: Path, _revision: object, *, target: Path, **_kwargs: object) -> str:
        target.mkdir(parents=True, exist_ok=True)
        (target / "app.py").write_text("print('fixture')\n")
        return "new"

    configs: list[dict[str, object]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        configs.append(json.loads(command[-1]))
        stdout = json.dumps(
            {
                "status_code": 200,
                "headers": {},
                "body": {"seeded": 1},
                "truncated": False,
                "duration_ms": 3,
                "setup": [{"method": "POST", "path": "/seed?n=1", "status_code": 201}],
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout + "\n")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runner_host.materialize_review_side", materialize)
    monkeypatch.setattr(
        "lemoncrow.pro.environment.runners.shutil.which",
        lambda name, **_kwargs: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.environment.runners.subprocess.run", run)

    result = RunnerRegistry((HttpServiceRunner(),)).run(context, surface, side="old")

    assert configs[0]["setup"] == [{"method": "POST", "path": "/seed?n=1"}]
    assert configs[0]["path"] == "/read"
    assert result.status == "passed"
    assert result.summary == "GET /read returned HTTP 200"
    assert result.data["setup"] == [{"method": "POST", "path": "/seed?n=1", "status_code": 201}]


def test_http_service_supervisor_runs_setup_requests_on_the_same_service_first(tmp_path: Path) -> None:
    # The supervisor itself, without a sandbox: a stateful service sees the setup requests before the request.
    (tmp_path / "server.py").write_text(
        "import json, os\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "seen = []\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def _reply(self, status):\n"
        "        body = json.dumps({'seen': seen}).encode()\n"
        "        self.send_response(status)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def do_GET(self):\n"
        "        if self.path != '/health':\n"
        "            seen.append('GET ' + self.path)\n"
        "        self._reply(404 if self.path == '/missing' else 200)\n"
        "    def do_POST(self):\n"
        "        seen.append('POST ' + self.path)\n"
        "        self._reply(201)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "HTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()\n"
    )
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = {
        "command": [sys.executable, str(tmp_path / "server.py")],
        "port": port,
        "port_env": "PORT",
        "health": "/health",
        "health_timeout": 10,
        "request_timeout": 5,
        "max_body_bytes": 256_000,
        "method": "GET",
        "path": "/read?x=1",
        "setup": [{"method": "POST", "path": "/seed"}, {"method": "GET", "path": "/missing"}],
    }
    completed = subprocess.run(
        [sys.executable, "-c", review_runtimes._HTTP_SERVICE_SUPERVISOR, json.dumps(config)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["body"] == {"seen": ["POST /seed", "GET /missing", "GET /read?x=1"]}
    assert payload["setup"] == [
        {"method": "POST", "path": "/seed", "status_code": 201},
        {"method": "GET", "path": "/missing", "status_code": 404},
    ]


def test_runner_materializes_a_side_without_frozen_tree_from_the_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    # A Snapshot with no frozen tree (captured in the server, or before its
    # capture finished) is discovered from a materialized side, which is not a
    # git checkout. The runner must build its old side from the checkout, not
    # from that tree ("not a git repository").
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Fixture Tester"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=source, check=True)
    (source / ".lemoncrow").mkdir()
    (source / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n  - id: checks\n    runner: command\n    root: backend\n    command: [python3, check.py]\n"
        "surfaces:\n  - id: checks\n    provider: service\n    runtime: checks\n    services: [tests]\n"
    )
    (source / "backend" / "check.py").write_text("print('before')\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=source, check=True, capture_output=True)
    (source / "backend" / "check.py").write_text("print('after')\n")
    subprocess.run(["git", "commit", "-am", "change"], cwd=source, check=True, capture_output=True)

    store = ReviewStore(tmp_path / "store")
    rng = resolve_rev_range(source, base="HEAD~1", head="HEAD")
    session = open_or_create_session(store, source, rng)
    build = build_review_packet_with_blobs(source, rng, store_root=store.root, with_impact=False, with_provenance=False)
    revision = record_revision(store, session, source, rng, store_root=store.root, build=build).revision
    assert store.read_source_tree_artifact(session.id, revision.id, side="old") is None
    discovered = tmp_path / "materialized-old"
    materialize_review_side(store.root, source, revision, side="old", target=discovered, store=store)
    context = SurfaceContext(
        repo_root=discovered,
        store_root=store.root,
        revision=revision,
        changed_paths=("backend/check.py",),
        config=load_review_surface_config(discovered),
        store=store,
        source_root=source,
    )
    surface = ServiceSurfaceProvider().discover(context)[0]
    ran: list[str] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append((Path(str(kwargs["cwd"])) / "backend" / "check.py").read_text())
        return subprocess.CompletedProcess(command, 0, stdout="before\n")

    monkeypatch.setattr(
        "lemoncrow.pro.environment.runners.shutil.which",
        lambda name, **_kwargs: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr("lemoncrow.pro.environment.runners.run_limited_bounded", run)

    result = RunnerRegistry((CommandRunner(),)).run(context, surface, side="old")

    assert result.status == "passed", result.summary
    assert ran == ["print('before')\n"]
