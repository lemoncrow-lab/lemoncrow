"""Hermetic Python provisioning: uv.lock and hash-pinned requirements, wheels only (Phase 9R item 3).

The package index is a real HTTP server on loopback, allowlisted as a
``private_network`` pypi registry. Build/run tests execute in bubblewrap and
skip with an explicit reason when the host cannot create namespaces.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.hermetic import (
    HermeticPolicy,
    HermeticRefused,
    RegistryFetcher,
    hermetic_context,
    load_hermetic_policy,
    provision_dependencies,
    unsupported_lockfiles,
)
from lemoncrow.pro.capabilities.review.hermetic_python import (
    PythonTarget,
    plan_requirements,
    plan_uv_lock,
    probe_sandbox_python,
    provision_python_dependencies,
)
from lemoncrow.pro.capabilities.review.surface_providers import ServiceSurfaceProvider
from lemoncrow.pro.capabilities.review.surfaces import SurfaceContext, load_review_surface_config
from lemoncrow.pro.environment.dependency_cache import DependencyCache
from lemoncrow.pro.environment.execution import ExecutionResult
from lemoncrow.pro.environment.runners import CommandRunner, RunnerRegistry
from tests.gateway.test_review_hermetic import _Server, needs_bwrap


@pytest.fixture
def index() -> Any:
    server = _Server()
    yield server
    server.close()


def _execution_yaml(server: _Server, **extra: Any) -> str:
    rule = {"host": "localhost", "scheme": "http", "port": server.port, "private_network": True, "ecosystem": "pypi"}
    rule.update(extra.pop("rule", {}))
    return yaml.safe_dump({"execution": {"registries": [rule], **extra}})


def _policy(server: _Server, **extra: Any) -> HermeticPolicy:
    return load_hermetic_policy(_execution_yaml(server, **extra))


def _wheel(
    name: str,
    version: str,
    files: dict[str, str],
    *,
    entry_points: str = "",
    tag: str = "py3-none-any",
    dist_name: str = "",
) -> tuple[str, bytes]:
    stem = (dist_name or name).replace("-", "_")
    dist_info = f"{stem}-{version}.dist-info"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, body in files.items():
            archive.writestr(path, body)
        archive.writestr(
            f"{dist_info}/METADATA", f"Metadata-Version: 2.1\nName: {dist_name or name}\nVersion: {version}\n"
        )
        archive.writestr(f"{dist_info}/WHEEL", f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n")
        if entry_points:
            archive.writestr(f"{dist_info}/entry_points.txt", entry_points)
        archive.writestr(f"{dist_info}/RECORD", "")
    return f"{name.replace('-', '_')}-{version}-{tag}.whl", buffer.getvalue()


def _target(executable: str = "/usr/bin/python3") -> PythonTarget:
    return PythonTarget(
        executable=executable,
        version=(3, 12),
        implementation="cpython",
        gil_disabled=False,
        environment=(
            ("implementation_name", "cpython"),
            ("os_name", "posix"),
            ("platform_machine", "x86_64"),
            ("platform_system", "Linux"),
            ("python_full_version", "3.12.4"),
            ("python_version", "3.12"),
            ("sys_platform", "linux"),
        ),
    )


def _lock(server: _Server, packages: list[str], *, root_deps: str, requires: str = ">=3.8") -> str:
    root = (
        '[[package]]\nname = "app"\nversion = "0.1.0"\nsource = { editable = "." }\n'
        f'dependencies = [{root_deps}]\n\n[package.dev-dependencies]\ndev = [{{ name = "demo-lib" }}]\n'
    )
    return f'version = 1\nrequires-python = "{requires}"\n\n' + root + "\n" + "\n".join(packages)


def _registry_package(
    server: _Server, name: str, version: str, wheels: list[tuple[str, str]], sdist: bool = False
) -> str:
    entries = ", ".join(f'{{ url = "{url}", hash = "sha256:{sha}" }}' for url, sha in wheels)
    text = f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "{server.url("/simple")}" }}\n'
    if sdist:
        text += f'sdist = {{ url = "{server.url("/packages/x.tar.gz")}", hash = "sha256:{"0" * 64}" }}\n'
    return text + f"wheels = [{entries}]\n"


def _demo(server: _Server, **wheel_options: Any) -> tuple[str, str]:
    filename, payload = _wheel(
        "demo-lib",
        "1.2.0",
        {"demo_lib/__init__.py": "VALUE = 42\n\ndef main():\n    print('hello from demo')\n"},
        entry_points="[console_scripts]\ndemo-hello = demo_lib:main\n",
        **wheel_options,
    )
    server.files[f"/packages/{filename}"] = payload
    return server.url(f"/packages/{filename}"), hashlib.sha256(payload).hexdigest()


def _project(root: Path, lock: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["demo-lib"]\n', encoding="utf-8")
    (root / "uv.lock").write_text(lock, encoding="utf-8")
    (root / "src" / "app").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app" / "__init__.py").write_text("from demo_lib import VALUE\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# uv.lock plan (fetch stage input): allowlist, hashes, markers, wheels only
# --------------------------------------------------------------------------- #


def test_uv_lock_plan_walks_root_dependencies_and_dev_groups_and_skips_inapplicable_markers(
    tmp_path: Path, index: _Server
) -> None:
    url, sha = _demo(index)
    lock = _lock(
        index,
        [
            _registry_package(index, "demo-lib", "1.2.0", [(url, sha)]),
            _registry_package(index, "winonly", "1.0", [], sdist=True),
        ],
        root_deps='{ name = "demo-lib" }, { name = "winonly", marker = "sys_platform == \'win32\'" }',
    )
    project = _project(tmp_path / "app", lock)

    plan = plan_uv_lock(project, _policy(index), _target(), sandbox_project="/workspace")

    assert [(wheel.name, wheel.version, wheel.sha256) for wheel in plan.wheels] == [("demo-lib", "1.2.0", sha)]
    # The editable root goes on sys.path through its src/ layout, at its in-sandbox path.
    assert plan.project_paths == ("/workspace/src",)
    assert plan.digest == plan_uv_lock(project, _policy(index), _target(), sandbox_project="/workspace").digest
    assert (
        plan.digest
        != plan_uv_lock(project, _policy(index), _target("/usr/local/bin/python3"), sandbox_project="/workspace").digest
    )


def test_uv_lock_plan_refuses_unallowlisted_unhashed_or_incompatible_wheels(tmp_path: Path, index: _Server) -> None:
    for case_index, (mutate, message) in enumerate(
        [
            (
                lambda url, sha: (url.replace("localhost", "evil.localhost"), sha),
                "outside the allowlisted pypi registries",
            ),
            (lambda url, sha: (url.replace("http://", "https://"), sha), "outside the allowlisted pypi registries"),
            (lambda url, sha: (url, "md5:" + sha[:32]), "without a sha256 hash"),
            (lambda url, sha: (url.replace("py3-none-any", "cp27-cp27m-win32"), sha), "no wheel compatible"),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        url, sha = mutate(*_demo(index))
        entry = f"""{{ url = "{url}", hash = "{(sha if ':' in sha else 'sha256:' + sha)}" }}"""
        package = f"""[[package]]\nname = "demo-lib"\nversion = "1.2.0"\nsource = {{ registry = "{index.url('/simple')}" }}\nwheels = [{entry}]\n"""
        project = _project(case_tmp_path / "app", _lock(index, [package], root_deps='{ name = "demo-lib" }'))
        with pytest.raises(HermeticRefused, match=message):
            plan_uv_lock(project, _policy(index), _target(), sandbox_project="/workspace")


def test_uv_lock_plan_refuses_sdist_builds_foreign_sources_and_the_wrong_interpreter(
    tmp_path: Path, index: _Server
) -> None:
    sdist_only = _registry_package(index, "demo-lib", "1.2.0", [], sdist=True)
    project = _project(tmp_path / "sdist", _lock(index, [sdist_only], root_deps='{ name = "demo-lib" }'))
    with pytest.raises(HermeticRefused, match="building it from an sdist is not supported hermetically"):
        plan_uv_lock(project, _policy(index), _target(), sandbox_project="/workspace")

    git = '[[package]]\nname = "demo-lib"\nversion = "1.2.0"\nsource = { git = "https://github.com/x/y?rev=1" }\n'
    project = _project(tmp_path / "git", _lock(index, [git], root_deps='{ name = "demo-lib" }'))
    with pytest.raises(HermeticRefused, match="comes from a git source"):
        plan_uv_lock(project, _policy(index), _target(), sandbox_project="/workspace")

    url, sha = _demo(index)
    package = _registry_package(index, "demo-lib", "1.2.0", [(url, sha)])
    project = _project(tmp_path / "py", _lock(index, [package], root_deps='{ name = "demo-lib" }', requires=">=3.13"))
    with pytest.raises(HermeticRefused, match=r"the sandbox interpreter is 3\.12\.4"):
        plan_uv_lock(project, _policy(index), _target(), sandbox_project="/workspace")


def test_uv_lock_plan_binds_pinned_names_to_their_registry(tmp_path: Path, index: _Server) -> None:
    url, sha = _demo(index)
    package = _registry_package(index, "demo-lib", "1.2.0", [(url, sha)])
    project = _project(tmp_path / "app", _lock(index, [package], root_deps='{ name = "demo-lib" }'))
    corp = {"host": "pypi.corp.example", "ecosystem": "pypi", "packages": ["Demo_Lib"]}
    rule = {"host": "localhost", "scheme": "http", "port": index.port, "private_network": True, "ecosystem": "pypi"}
    policy = load_hermetic_policy(yaml.safe_dump({"execution": {"registries": [rule, corp]}}))
    with pytest.raises(HermeticRefused, match="dependency confusion"):
        plan_uv_lock(project, policy, _target(), sandbox_project="/workspace")


# --------------------------------------------------------------------------- #
# requirements*.txt with --require-hashes semantics
# --------------------------------------------------------------------------- #


def _simple_page(index: _Server, url: str, sha: str, *, json_api: bool) -> None:
    filename = url.rsplit("/", 1)[1]
    if json_api:
        body = {"name": "demo-lib", "files": [{"filename": filename, "url": url, "hashes": {"sha256": sha}}]}
        index.files["/simple/demo-lib/"] = json.dumps(body).encode()
    else:
        index.files["/simple/demo-lib/"] = (
            f'<html><body><a href="{url}#sha256={sha}">{filename}</a></body></html>'.encode()
        )


@pytest.mark.parametrize("json_api", [False, True])
def test_hashed_requirements_resolve_to_the_hash_matching_wheel(tmp_path: Path, index: _Server, json_api: bool) -> None:
    url, sha = _demo(index)
    _simple_page(index, url, sha, json_api=json_api)
    project = tmp_path / "app"
    project.mkdir()
    (project / "requirements.txt").write_text(
        f"--index-url {index.url('/simple/')}\n-r requirements-dev.txt\n"
        f"demo-lib==1.2.0 \\\n    --hash=sha256:{sha}\n    # via app\n"
        f"winonly==1.0 ; sys_platform == 'win32' --hash=sha256:{'1' * 64}\n",
        encoding="utf-8",
    )
    (project / "requirements-dev.txt").write_text(f"Demo_Lib==1.2.0 --hash=sha256:{sha}\n", encoding="utf-8")

    plan = plan_requirements(project, _policy(index), _target(), RegistryFetcher(_policy(index)))

    assert [(wheel.name, wheel.sha256) for wheel in plan.wheels] == [("demo-lib", sha)]
    assert plan.source == "requirements"


def test_requirements_without_require_hashes_semantics_are_refused(tmp_path: Path, index: _Server) -> None:
    for case_index, (line, message) in enumerate(
        [
            ("demo-lib==1.2.0", "no sha256 --hash"),
            ("demo-lib>=1.0 --hash=sha256:{sha}", "must be pinned with =="),
            ("-e ./vendor/demo", "option -e is not supported"),
            ("--find-links https://evil.example/", "option --find-links is not supported"),
            ("--index-url https://pypi.attacker.example/simple/", "not an allowlisted pypi registry"),
            ("demo-lib @ https://evil.example/demo.whl --hash=sha256:{sha}", "direct URL requirement"),
            ("--no-binary :all:", "needs sdist builds"),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        url, sha = _demo(index)
        _simple_page(index, url, sha, json_api=False)
        project = case_tmp_path / "app"
        project.mkdir()
        (project / "requirements.txt").write_text(line.format(sha=sha) + "\n", encoding="utf-8")
        with pytest.raises(HermeticRefused, match=message):
            plan_requirements(project, _policy(index), _target(), RegistryFetcher(_policy(index)))


def test_requirements_with_only_an_sdist_for_the_pinned_hash_are_refused(tmp_path: Path, index: _Server) -> None:
    payload = b"sdist bytes"
    sha = hashlib.sha256(payload).hexdigest()
    index.files["/packages/demo_lib-1.2.0.tar.gz"] = payload
    _simple_page(index, index.url("/packages/demo_lib-1.2.0.tar.gz"), sha, json_api=False)
    project = tmp_path / "app"
    project.mkdir()
    (project / "requirements.txt").write_text(
        f"-i {index.url('/simple/')}\ndemo-lib==1.2.0 --hash=sha256:{sha}\n", encoding="utf-8"
    )
    policy = _policy(index)
    with pytest.raises(HermeticRefused, match="only an sdist matches the pinned hashes"):
        plan_requirements(project, policy, _target(), RegistryFetcher(policy))


# --------------------------------------------------------------------------- #
# Ecosystem dispatch: explicit refusal, never a fallback
# --------------------------------------------------------------------------- #


def test_unsupported_or_unlocked_ecosystems_are_refused_with_a_visible_reason(tmp_path: Path) -> None:
    for case_index, (files, message) in enumerate(
        [
            ({"Cargo.lock": ""}, "Cargo\\.lock \\(Cargo \\(Rust\\)\\) cannot be provisioned hermetically"),
            ({"go.sum": ""}, "go\\.sum \\(Go modules\\)"),
            ({"poetry.lock": ""}, "poetry\\.lock \\(Poetry\\)"),
            ({"package.json": '{"dependencies": {"a": "1"}}'}, "no package-lock.json"),
            ({"pyproject.toml": '[project]\nname = "x"\ndependencies = ["a"]\n'}, "no uv\\.lock or hash-pinned"),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        for name, body in files.items():
            (case_tmp_path / name).write_text(body, encoding="utf-8")
        cache = DependencyCache.for_store(case_tmp_path / "store", "head_untrusted")
        with pytest.raises(HermeticRefused, match=message):
            provision_dependencies(case_tmp_path, policy=HermeticPolicy(), cache=cache, sandbox_project="/workspace")


def test_projects_without_declared_dependencies_provision_nothing(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "tooling", "scripts": {}}', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert unsupported_lockfiles(tmp_path) == ()
    cache = DependencyCache.for_store(tmp_path / "store", "head_untrusted")
    deps = provision_dependencies(tmp_path, policy=HermeticPolicy(), cache=cache, sandbox_project="/workspace")
    assert deps.stages == () and deps.mounts == () and deps.bwrap_args() == []


# --------------------------------------------------------------------------- #
# Offline build + run stage (bubblewrap)
# --------------------------------------------------------------------------- #


@needs_bwrap
def test_wheel_whose_metadata_names_another_package_is_refused_in_the_offline_build(
    tmp_path: Path, index: _Server
) -> None:
    url, sha = _demo(index, dist_name="other-lib")
    package = _registry_package(index, "demo-lib", "1.2.0", [(url, sha)])
    project = _project(tmp_path / "app", _lock(index, [package], root_deps='{ name = "demo-lib" }'))
    cache = DependencyCache.for_store(tmp_path / "store", "head_untrusted")
    with pytest.raises(HermeticRefused, match="lookalike package"):
        provision_python_dependencies(
            project, policy=_policy(index), cache=cache, sandbox_project="/workspace", target=probe_sandbox_python()
        )


_CHECK = """
import json, os, socket, subprocess, sys
import app, demo_lib
hello = subprocess.run(["demo-hello"], capture_output=True, text=True).stdout.strip()
try:
    socket.create_connection(("1.1.1.1", 443), 3).close()
    egress = "connected"
except OSError as exc:
    egress = type(exc).__name__
print(json.dumps({
    "value": app.VALUE, "hello": hello, "prefix": sys.prefix, "egress": egress,
    "env_writable": os.access(os.path.dirname(demo_lib.__file__), os.W_OK),
}))
"""


@needs_bwrap
def test_command_runner_runs_against_a_hermetic_uv_lock_environment(
    tmp_path: Path, index: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, sha = _demo(index)
    lock = _lock(
        index, [_registry_package(index, "demo-lib", "1.2.0", [(url, sha)])], root_deps='{ name = "demo-lib" }'
    )
    trusted = tmp_path / "trusted"
    (trusted / ".lemoncrow").mkdir(parents=True)
    (trusted / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\nruntimes:\n  - id: checks\n    runner: command\n    command: [python3, check.py]\n    timeout: 120\n"
        "surfaces:\n  - id: checks\n    provider: service\n    runtime: checks\n    services: [tests]\n",
        encoding="utf-8",
    )
    revision = SimpleNamespace(
        id="revision",
        review_id="review",
        revision_number=1,
        range_mode="commit_range",
        base_sha="base",
        head_sha="head",
        tree_fingerprint="tree",
        source_fingerprint="source",
    )

    def materialize(_store: Path, _repo: Path, _revision: object, *, side: str, target: Path, **_kw: object) -> str:
        _project(target, lock)
        (target / "check.py").write_text(_CHECK, encoding="utf-8")
        return side

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runner_host.materialize_review_side", materialize)
    store = tmp_path / "store"

    def run() -> Any:
        context = SurfaceContext(
            trusted,
            store,
            revision,  # type: ignore[arg-type]
            ("check.py",),
            load_review_surface_config(trusted),
            execution=hermetic_context(
                trusted_review_config=_execution_yaml(index), store_root=store, review_id="review", side="new"
            ),
        )
        surface = ServiceSurfaceProvider().discover(context)[0]
        return RunnerRegistry((CommandRunner(),)).run(context, surface, side="new")

    result = run()
    assert result.status == "passed", result.output
    observed = json.loads(result.output.splitlines()[-1])
    assert observed["value"] == 42 and observed["hello"] == "hello from demo"
    assert observed["prefix"] == "/lemoncrow-python/venv"
    assert observed["env_writable"] is False and observed["egress"] != "connected"
    execution = ExecutionResult.from_wire(result.data["execution"])
    assert [(stage.stage, stage.status, stage.network) for stage in execution.stages] == [
        ("fetch", "passed", "registry_only"),
        ("build", "passed", "denied"),
        ("run", "passed", "denied"),
    ]
    assert "wheels only" in execution.stages[0].detail
    assert execution.recipe.stages == ("fetch", "build", "run")
    assert execution.fingerprint.lockfile_digest
    requests = list(index.requests)

    # Same lock again: the environment is a cache hit and nothing is fetched.
    again = run()
    assert again.status == "passed", again.output
    stages = ExecutionResult.from_wire(again.data["execution"]).stages
    assert (stages[0].stage, stages[0].status, stages[0].cache) == ("fetch", "skipped", "hit")
    assert index.requests == requests


@needs_bwrap
def test_command_runner_refuses_an_unsupported_lockfile_as_not_run(
    tmp_path: Path, index: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "trusted"
    (trusted / ".lemoncrow").mkdir(parents=True)
    (trusted / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\nruntimes:\n  - id: checks\n    runner: command\n    command: [cargo, test]\n"
        "surfaces:\n  - id: checks\n    provider: service\n    runtime: checks\n    services: [tests]\n",
        encoding="utf-8",
    )
    revision = SimpleNamespace(
        id="revision", review_id="review", revision_number=1, range_mode="commit_range", base_sha="b", head_sha="h"
    )

    def materialize(_store: Path, _repo: Path, _revision: object, *, side: str, target: Path, **_kw: object) -> str:
        target.mkdir(parents=True, exist_ok=True)
        (target / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
        return side

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runner_host.materialize_review_side", materialize)
    context = SurfaceContext(
        trusted,
        tmp_path / "store",
        revision,  # type: ignore[arg-type]
        ("src/main.rs",),
        load_review_surface_config(trusted),
        execution=hermetic_context(
            trusted_review_config=_execution_yaml(index), store_root=tmp_path / "store", review_id="review", side="new"
        ),
    )
    surface = ServiceSurfaceProvider().discover(context)[0]
    result = RunnerRegistry((CommandRunner(),)).run(context, surface, side="new")
    assert result.status == "unavailable"
    assert "Cargo.lock (Cargo (Rust)) cannot be provisioned hermetically" in result.summary
