"""environment.toml services, dependencies and previews in Review's runners (plan M2)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lemoncrow_client.environment import Service

from lemoncrow.pro.capabilities.review import hermetic
from lemoncrow.pro.capabilities.review.hermetic import (
    DeclaredDependencies,
    HermeticPolicy,
    HermeticRefused,
    ProvisionedDependencies,
    provision_declared_dependencies,
)
from lemoncrow.pro.capabilities.review.preview_runtime import (
    ENVIRONMENT_PREVIEW_PORT,
    PreviewRecipeError,
    environment_preview_recipe,
    environment_preview_recipes,
)
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry, load_review_surface_config
from lemoncrow.pro.environment.runners import CommandRunner
from lemoncrow.pro.environment.services import (
    ServiceSet,
    ServicesRunner,
    ServiceUnavailable,
    StartedService,
    service_refusal,
    services_env,
)

PINNED = "postgres@sha256:" + "b" * 64


class _Backend:
    """Records every start/probe/stop; ``ready_after`` probes until each service is ready."""

    def __init__(self, ready_after: dict[str, int] | None = None, fail_start: str = "", fail_stop: str = "") -> None:
        self.events: list[str] = []
        self.ready_after = ready_after or {}
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.probes: dict[str, int] = {}

    def start(self, service: Service) -> StartedService:
        self.events.append(f"start {service.name}")
        if service.name == self.fail_start:
            raise OSError("image not found")
        return StartedService(service.name, handle=service.name, env=((f"{service.name.upper()}_HOST", "127.0.0.1"),))

    def probe(self, started: StartedService, service: Service) -> bool:
        self.probes[service.name] = self.probes.get(service.name, 0) + 1
        self.events.append(f"probe {service.name}")
        return self.probes[service.name] > self.ready_after.get(service.name, 0)

    def stop(self, started: StartedService) -> None:
        self.events.append(f"stop {started.name}")
        if started.name == self.fail_stop:
            raise RuntimeError("still running")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _services(*names: str) -> tuple[Service, ...]:
    return tuple(Service(name, PINNED, "true") for name in names)


def _runner(backend: _Backend, clock: _Clock, timeout: float = 5.0) -> ServicesRunner:
    return ServicesRunner(backend, ready_timeout=timeout, interval=1.0, clock=clock, sleep=clock.sleep)


def test_services_start_in_order_wait_until_ready_and_stop_in_reverse() -> None:
    backend, clock = _Backend({"db": 2}), _Clock()
    runner = _runner(backend, clock)
    with runner.up(_services("db", "cache")) as started:
        assert [service.name for service in started] == ["db", "cache"]
        assert services_env(started) == (("DB_HOST", "127.0.0.1"), ("CACHE_HOST", "127.0.0.1"))
        backend.events.append("run")
    assert backend.events == [
        "start db",
        "probe db",
        "probe db",
        "probe db",
        "start cache",
        "probe cache",
        "run",
        "stop cache",
        "stop db",
    ]
    assert runner.stop_errors == ()


def test_a_service_that_is_never_ready_stops_the_ones_already_up() -> None:
    backend, clock = _Backend({"cache": 1000}), _Clock()
    runner = _runner(backend, clock, timeout=3.0)
    with pytest.raises(ServiceUnavailable, match="service cache was not ready within 3 s"):
        with runner.up(_services("db", "cache")):
            raise AssertionError("the block never runs")
    # The deadline holds at its edge: probes at 0, 1, 2 and 3 s, then the refusal.
    assert backend.probes["cache"] == 4
    assert backend.events[-2:] == ["stop cache", "stop db"]


def test_a_failed_start_or_run_still_stops_everything_and_stop_errors_never_mask() -> None:
    backend, clock = _Backend(fail_start="cache"), _Clock()
    with pytest.raises(ServiceUnavailable, match="service cache could not start: image not found"):
        with _runner(backend, clock).up(_services("db", "cache")):
            pass
    assert backend.events == ["start db", "probe db", "start cache", "stop db"]

    backend = _Backend(fail_stop="db")
    runner = _runner(backend, clock)
    with pytest.raises(KeyError):
        with runner.up(_services("db", "cache")):
            raise KeyError("the check failed")
    assert backend.events[-2:] == ["stop cache", "stop db"]
    assert runner.stop_errors == ("db: still running",)


def test_review_refuses_unpinned_services_and_runs_without_a_backend() -> None:
    assert service_refusal((), None) == ""
    unpinned = (Service("db", "postgres:16", "true"),)
    assert "not pinned by digest" in service_refusal(unpinned, _Backend())
    assert "no service backend" in service_refusal(_services("db"), None)
    assert service_refusal(_services("db"), _Backend()) == ""


def _run(runner: CommandRunner, values: dict[str, Any]) -> Any:
    runtime = RuntimeConfigEntry(id="recipe:unit", runner="command", values={"command": ["true"], **values})
    surface = ReviewSurface(id="recipe:unit", provider="recipe", kind="check", title="unit", runtime="recipe:unit")
    return runner.run(cast(Any, SimpleNamespace()), runtime, surface, side="new")


@pytest.mark.skipif(sys.platform != "darwin", reason="elsewhere the bubblewrap check comes first")
def test_a_check_that_needs_services_is_not_run_without_a_backend() -> None:
    result = _run(CommandRunner(), {"services": ServiceSet(_services("db"))})
    assert result.status == "unavailable" and "needs services (db)" in result.summary
    unpinned = ServiceSet((Service("db", "postgres:16", "true"),))
    result = _run(CommandRunner(service_backend=_Backend()), {"services": unpinned})
    assert result.status == "unavailable" and "not pinned by digest" in result.summary
    # Only a trusted ServiceSet counts: a YAML list never names services to start.
    with pytest.raises(AttributeError):
        _run(CommandRunner(), {"services": ["db"]})  # it goes on to materialize, which the stub context lacks


def test_declared_dependencies_provision_exactly_their_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("uv.lock", "web/package-lock.json", "web/yarn.lock", "api/Cargo.lock", "py2/uv.lock"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def fake(project: Path, *, sandbox_project: str, **_kwargs: Any) -> ProvisionedDependencies:
        calls.append((project.relative_to(tmp_path).as_posix(), sandbox_project))
        npm = project.name == "web"
        return ProvisionedDependencies(
            mounts=((project, f"{sandbox_project}/node_modules" if npm else "/lemoncrow-python"),),
            path_prefix=(f"{sandbox_project}/node_modules/.bin",) if npm else ("/lemoncrow-python/venv/bin",),
            env=(("npm_config_offline", "true"),) if npm else (("UV_OFFLINE", "1"),),
            lockfile_digest=project.name,
            ecosystems=("npm",) if npm else ("pypi",),
        )

    monkeypatch.setattr(hermetic, "provision_dependencies", fake)
    policy, cache = HermeticPolicy(), SimpleNamespace()
    done = provision_declared_dependencies(
        tmp_path, DeclaredDependencies(("uv.lock", "web/package-lock.json")), policy=policy, cache=cache  # type: ignore[arg-type]
    )
    # The declared directories only (web/yarn.lock is in web/, so the provisioner still refuses it there).
    assert calls == [(".", "/workspace"), ("web", "/workspace/web")]
    assert done.ecosystems == ("pypi", "npm") and dict(done.env) == {"UV_OFFLINE": "1", "npm_config_offline": "true"}
    assert done.path_prefix == ("/lemoncrow-python/venv/bin", "/workspace/web/node_modules/.bin")
    assert done.lockfile_digest and len(done.mounts) == 2
    refusals = {
        ("api/Cargo.lock",): "api/Cargo.lock (Cargo (Rust)) cannot be provisioned hermetically",
        ("missing/uv.lock",): "missing/uv.lock is declared but this side of the change has no such file",
        ("uv.lock", "py2/uv.lock"): "Python projects in ., py2; a run provisions one",
    }
    (tmp_path / "out").symlink_to(tmp_path.parent)
    refusals[("out/uv.lock",)] = "resolves outside the reviewed tree"
    for lockfiles, message in refusals.items():
        with pytest.raises(HermeticRefused, match=message.replace("(", r"\(").replace(")", r"\)")):
            provision_declared_dependencies(
                tmp_path, DeclaredDependencies(lockfiles), policy=policy, cache=cache  # type: ignore[arg-type]
            )


def test_python_and_npm_trees_run_offline_with_uv_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import hermetic_python

    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        hermetic_python,
        "provision_python_dependencies",
        lambda *_args, **_kwargs: SimpleNamespace(stages=(), root=tmp_path / "env"),
    )
    done = hermetic.provision_dependencies(
        tmp_path, policy=HermeticPolicy(), cache=SimpleNamespace(), sandbox_project="/workspace"  # type: ignore[arg-type]
    )
    env = dict(done.env)
    assert env["UV_PROJECT_ENVIRONMENT"] == env["VIRTUAL_ENV"] == "/lemoncrow-python/venv"
    assert (env["UV_OFFLINE"], env["UV_NO_SYNC"], env["UV_PYTHON_DOWNLOADS"]) == ("1", "1", "never")


PREVIEWS = """\
version = 1

[previews.web]
run = "npm start"
ready = "/health"

[previews.docs]
run = "make serve"
ready = "/"
"""
REVIEW_YAML = """\
version: 1
runtimes:
  - id: api
    runner: http-service
    command: python3 app.py
surfaces:
  - id: home
    provider: web
    runtime: web
"""


def test_environment_previews_come_before_review_yaml_interactive_preview() -> None:
    recipe = environment_preview_recipe(PREVIEWS)
    assert recipe is not None
    assert (recipe.command, recipe.port, recipe.ready_path) == (
        ("sh", "-c", "npm start"),
        ENVIRONMENT_PREVIEW_PORT,
        "/health",
    )
    assert recipe.source == ".lemoncrow/environment.toml previews.web"
    # No previews: review.yaml's interactive_preview applies; an invalid file never falls back.
    assert environment_preview_recipe(None) is None and environment_preview_recipe("version = 1\n") is None
    with pytest.raises(PreviewRecipeError, match="unknown key 'colour'"):
        environment_preview_recipe('version = 1\ncolour = "red"\n')


def test_every_environment_preview_is_a_recipe_selectable_by_name() -> None:
    recipes = environment_preview_recipes(PREVIEWS)
    # Declaration order; the first declared is the default.
    assert list(recipes) == ["web", "docs"]
    assert recipes["docs"].command == ("sh", "-c", "make serve")
    assert recipes["docs"].source == ".lemoncrow/environment.toml previews.docs"
    assert environment_preview_recipe(PREVIEWS) == recipes["web"]
    assert environment_preview_recipe(PREVIEWS, "docs") == recipes["docs"]
    reordered = PREVIEWS.replace("[previews.web]", "[previews.tmp]").replace("[previews.docs]", "[previews.web]")
    assert list(environment_preview_recipes(reordered)) == ["tmp", "web"]
    with pytest.raises(PreviewRecipeError, match=r"no preview named 'admin'; the trusted base declares web, docs$"):
        environment_preview_recipe(PREVIEWS, "admin")
    assert environment_preview_recipes(None) == {} and environment_preview_recipes("version = 1\n") == {}
    # One preview whose ready is not origin-relative refuses the whole block, never a partial list.
    with pytest.raises(PreviewRecipeError, match=r"previews\.docs: ready must be a path on the preview"):
        environment_preview_recipes(PREVIEWS.replace('ready = "/"', 'ready = "https://evil.example/"'))


def test_previews_replace_review_yaml_runtimes_and_runtimes_are_the_fallback(tmp_path: Path) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(REVIEW_YAML.replace("runtime: web", "runtime: api"))
    fallback = load_review_surface_config(tmp_path)
    assert [runtime.id for runtime in fallback.runtimes] == ["api"]
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(REVIEW_YAML)
    (tmp_path / ".lemoncrow" / "environment.toml").write_text(PREVIEWS)
    config = load_review_surface_config(tmp_path)
    assert [runtime.id for runtime in config.runtimes] == ["web", "docs"]
    web = config.runtime("web")
    assert web is not None and web.runner == "http-service"
    assert (web.strings("command"), web.string("health")) == (("sh", "-c", "npm start"), "/health")
    assert [surface.id for surface in config.surfaces] == ["home"]
    # A surface naming a review.yaml runtime the previews replaced is refused, never silently unbound.
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(REVIEW_YAML.replace("runtime: web", "runtime: api"))
    with pytest.raises(ValueError, match="unknown runtime 'api'"):
        load_review_surface_config(tmp_path)
    (tmp_path / ".lemoncrow" / "review.yaml").unlink()
    assert [runtime.id for runtime in load_review_surface_config(tmp_path).runtimes] == ["web", "docs"]


def test_declared_previews_make_the_app_previewable() -> None:
    from lemoncrow.pro.capabilities.review.capability_catalog import detect_capabilities

    files = {".lemoncrow/environment.toml": PREVIEWS}
    detected = detect_capabilities(list(files), files.get)
    assert detected.has("previewable_app")
    assert detected.evidence["previewable_app"] == ".lemoncrow/environment.toml previews"
    invalid = {".lemoncrow/environment.toml": PREVIEWS + 'colour = "red"\n'}
    assert not detect_capabilities(list(invalid), invalid.get).has("previewable_app")
