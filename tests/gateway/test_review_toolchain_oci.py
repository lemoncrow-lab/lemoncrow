"""Review runs an environment.toml check's run stage in its pinned [toolchain] image (plan M2).

The unit tests drive the real :class:`~lemoncrow.pro.environment.oci.OciClient` against a fake engine binary that
records its argv. The integration test runs a real engine (``LEMONCROW_OCI_INTEGRATION=1``) with locally present
pinned images.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lemoncrow_client.environment import Service, Toolchain

from lemoncrow.pro.capabilities.review import runner_host
from lemoncrow.pro.capabilities.review.hermetic import (
    FixtureSpec,
    HermeticPolicy,
    ProvisionedDependencies,
    SidecarSpec,
)
from lemoncrow.pro.capabilities.review.runtime_observation import ObservationSpec
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry
from lemoncrow.pro.environment import oci, runners
from lemoncrow.pro.environment.execution import (
    ExecutionResult,
    ExecutorClass,
    StageOutcome,
    cache_namespace,
)
from lemoncrow.pro.environment.runners import CommandRunner
from lemoncrow.pro.environment.services import ServiceSet

IMAGE = "python@sha256:" + "a" * 64
REDIS = "redis@sha256:" + "b" * 64
#: What the engine is given: the short name spelled out (``docker.io/library/python@...``).
REFERENCE = oci.pinned_reference(IMAGE)

_ENGINE = """\
import json, sys, time
from pathlib import Path

here = Path(sys.argv[0]).resolve().parent
argv = sys.argv[1:]
with (here / "engine.log").open("a") as log:
    log.write(json.dumps(argv) + "\\n")
control = json.loads((here / "control.json").read_text())
verb = argv[0] if argv else ""
if verb == "run":
    print(control.get("stdout", "checked"), flush=True)
    if control.get("stderr"):
        print(control["stderr"], file=sys.stderr, flush=True)
    time.sleep(control.get("sleep", 0))
    sys.exit(control.get("exit", 0))
if verb == "create" and control.get("fail_create"):
    print("Error: the service image is not known", file=sys.stderr)
    sys.exit(125)
if verb == "inspect":
    sys.exit(1)  # every container is gone once removed
sys.exit(0)
"""


class _Engine:
    """A fake ``podman``/``docker`` binary: logs each argv, answers from ``control.json``."""

    def __init__(self, root: Path, engine: oci.Engine = "podman") -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.binary = root / engine
        self.binary.write_text(f"#!{sys.executable}\n{_ENGINE}", encoding="utf-8")
        self.binary.chmod(0o755)
        self.runtime = oci.OciRuntime(engine, str(self.binary), True)
        self.control()

    def control(self, **values: Any) -> None:
        (self.root / "control.json").write_text(json.dumps(values), encoding="utf-8")

    @property
    def calls(self) -> list[list[str]]:
        log = self.root / "engine.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def run_argv(self) -> list[str]:
        (found,) = [call for call in self.calls if call[0] == "run"]
        return found


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Engine:
    fake = _Engine(tmp_path / "engine")
    monkeypatch.setattr(oci, "detect_runtime", lambda **_kwargs: fake.runtime)
    _materialize(monkeypatch)
    return fake


def _materialize(monkeypatch: pytest.MonkeyPatch, files: dict[str, str] | None = None) -> None:
    def materialize(*_args: object, target: Path, scope: str = ".", side: str = "new", **_kwargs: object) -> str:
        target.mkdir(parents=True, exist_ok=True)
        if scope not in {"", "."}:
            (target / scope).mkdir(parents=True, exist_ok=True)
        for name, text in (files or {"check.py": "print('ok')\n"}).items():
            (target / name).write_text(text, encoding="utf-8")
        return side

    monkeypatch.setattr(runner_host, "materialize_review_side", materialize)


def _hermetic(policy: HermeticPolicy | None = None) -> Any:
    hermetic = SimpleNamespace(
        policy_error="",
        policy=policy or HermeticPolicy(),
        cache=SimpleNamespace(),
        content_access="ai_denied",
        artifacts=None,
        executor_class=ExecutorClass.MANAGED_SANDBOX,
        namespace=cache_namespace("new", "unknown"),
    )
    return SimpleNamespace(for_side=lambda _side: hermetic)


def _run(
    tmp_path: Path,
    values: dict[str, Any],
    *,
    execution: Any = None,
    review: str = "review",
    command: tuple[str, ...] = ("sh", "-c", "python3 check.py"),
) -> Any:
    context = SimpleNamespace(
        repo_root=tmp_path / "repo",
        store_root=tmp_path / "store",
        source_root=None,
        store=None,
        revision=SimpleNamespace(review_id=review, id="revision"),
        execution=execution,
        runner_host=runner_host,
    )
    runtime = RuntimeConfigEntry(
        id="recipe:unit", runner="command", values={"command": list(command), "timeout": 30, **values}
    )
    surface = ReviewSurface(id="recipe:unit", provider="recipe", kind="check", title="unit", runtime="recipe:unit")
    return CommandRunner().run(cast(Any, context), runtime, surface, side="new")


def _pairs(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == flag]


def _services() -> ServiceSet:
    return ServiceSet((Service("cache", REDIS, "redis-cli ping"),))


def test_the_run_stage_runs_in_the_pinned_image_with_no_network(tmp_path: Path, engine: _Engine) -> None:
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE)})
    assert result.status == "passed", result.summary
    argv = engine.run_argv()
    assert argv[:3] == ["run", "--rm", "--pull=never"]
    # The pinned reference, then the check's own argv, exactly.
    at = argv.index(REFERENCE)
    assert argv[at + 1 :] == ["sh", "-c", "python3 check.py"]
    options = argv[:at]
    assert _pairs(options, "--network") == ["none"]
    assert {"--cap-drop=ALL", "--security-opt=no-new-privileges", "--userns=keep-id"} <= set(options)
    workspace = tmp_path / "store/review/runtime-runs/review/revision/command"
    (mount,) = _pairs(options, "--mount")
    assert mount.startswith(f"type=bind,source={workspace.resolve()}/")
    assert mount.endswith(",target=/workspace,relabel=private")  # under SELinux, for this run alone
    assert "sock" not in mount
    assert _pairs(options, "--workdir") == ["/workspace"]
    assert {"CI=1", "HOME=/tmp"} <= set(_pairs(options, "--env"))
    assert _pairs(options, "--tmpfs") == ["/tmp:rw,exec,nosuid,nodev,mode=1777"]
    (name,) = _pairs(options, "--name")
    assert "dev.lemoncrow.owner=review-run" in _pairs(options, "--label")
    # No network created for a check without services; the container is removed by name afterwards.
    assert not any(call[:2] == ["network", "create"] for call in engine.calls)
    assert engine.calls[-1] == ["rm", "--force", name]

    execution = ExecutionResult.from_wire(result.data["execution"])
    fingerprint = execution.fingerprint
    assert (fingerprint.image_digest, fingerprint.sandbox) == ("sha256:" + "a" * 64, "oci-podman")
    assert fingerprint.toolchain == (("image", REFERENCE),)
    assert execution.verification.image_digest == "sha256:" + "a" * 64
    assert execution.verification.network == "denied"
    assert execution.executor_class is not ExecutorClass.WORKSPACE_TASK
    (run,) = execution.stages
    assert run.network == "denied" and f"ran in {REFERENCE} under rootless podman" in run.detail


def test_services_run_on_an_internal_network_and_everything_is_removed(tmp_path: Path, engine: _Engine) -> None:
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE), "services": _services()})
    assert result.status == "passed", result.summary
    calls = engine.calls
    (create_network,) = [call for call in calls if call[:2] == ["network", "create"]]
    assert "--internal" in create_network
    network = create_network[-1]
    argv = engine.run_argv()
    options = argv[: argv.index(REFERENCE)]
    (name,) = _pairs(options, "--name")
    assert network == f"{name}-net" and _pairs(options, "--network") == [network]
    assert "LC_SERVICE_CACHE_HOST=cache" in _pairs(options, "--env")
    (service,) = [call for call in calls if call[0] == "create"]
    assert _pairs(service, "--network") == [network] and _pairs(service, "--network-alias") == ["cache"]
    assert service[-1] == oci.pinned_reference(REDIS) and "--publish" not in service
    assert ["exec", f"{name}-svc-cache", "sh", "-c", "redis-cli ping"] in calls
    # Order: services up before the run; then the run container, the service, and last the network go.
    assert calls.index(service) < calls.index(argv)
    assert calls[calls.index(argv) + 1 :] == [
        ["rm", "--force", name],
        ["rm", "--force", f"{name}-svc-cache"],
        ["network", "rm", network],
    ]


@pytest.mark.parametrize(
    ("control", "status", "summary"),
    [
        ({"exit": 3}, "failed", "command exited 3"),
        ({"sleep": 30}, "unavailable", "command timed out inside the isolated sandbox"),
        (
            {"exit": 125, "stderr": "Error: invalid mount"},
            "unavailable",
            "the container engine could not run the check (exit 125): Error: invalid mount",
        ),
    ],
)
def test_failure_and_timeout_still_remove_the_container_services_and_network(
    tmp_path: Path, engine: _Engine, control: dict[str, Any], status: str, summary: str
) -> None:
    engine.control(**control)
    values = {"toolchain": Toolchain(IMAGE), "services": _services(), "timeout": 1}
    result = _run(tmp_path, values)
    assert (result.status, result.summary) == (status, summary)
    calls = engine.calls
    argv = engine.run_argv()
    (name,) = _pairs(argv, "--name")
    assert calls[calls.index(argv) + 1 :] == [
        ["rm", "--force", name],
        ["rm", "--force", f"{name}-svc-cache"],
        ["network", "rm", f"{name}-net"],
    ]


def test_a_service_that_cannot_start_is_not_run_and_the_network_goes(tmp_path: Path, engine: _Engine) -> None:
    engine.control(fail_create=True)
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE), "services": _services()})
    assert result.status == "unavailable" and result.summary.startswith("service cache could not start")
    calls = engine.calls
    assert not any(call[0] == "run" for call in calls)
    assert calls[-1][:2] == ["network", "rm"]


def test_dependency_trees_and_fixtures_are_mounted_read_only(
    tmp_path: Path, engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_tree = tmp_path / "deps" / "env"
    env_tree.mkdir(parents=True)
    provisioned = ProvisionedDependencies(
        stages=(StageOutcome("fetch", "passed", network="registry_only"), StageOutcome("build", "passed")),
        mounts=((env_tree, "/lemoncrow-python"),),
        path_prefix=("/lemoncrow-python/venv/bin",),
        env=(("UV_OFFLINE", "1"),),
        lockfile_digest="locks",
    )
    monkeypatch.setattr(runner_host, "provision_dependencies", lambda *_args, **_kwargs: provisioned)
    policy = HermeticPolicy(fixtures=(FixtureSpec("seed"),))
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE), "report": True}, execution=_hermetic(policy))
    assert result.status == "passed", result.summary
    argv = engine.run_argv()
    options = argv[: argv.index(REFERENCE)]
    mounts = _pairs(options, "--mount")
    # The dependency cache other runs mount too is labelled for every container; the run's own files for it alone.
    assert f"type=bind,source={env_tree.resolve()},target=/lemoncrow-python,readonly,relabel=shared" in mounts
    run_dir = tmp_path / "store/review/runtime-runs/review/revision/command"
    (fixture,) = [mount for mount in mounts if "target=/fixtures/seed" in mount]
    assert fixture.endswith("/fixtures/0/seed,target=/fixtures/seed,readonly,relabel=private")
    assert str(run_dir.resolve()) in fixture
    (report,) = [mount for mount in mounts if "target=/tmp/lemoncrow-reports" in mount]
    assert ",readonly" not in report
    workspace = next(mount for mount in mounts if mount.endswith(",target=/workspace,relabel=private"))
    assert ",readonly" not in workspace
    env = _pairs(options, "--env")
    assert "PATH=/lemoncrow-python/venv/bin:" + runners._COMMAND_PATH in env
    assert {"UV_OFFLINE=1", "LEMONCROW_REPORT_DIR=/tmp/lemoncrow-reports"} <= set(env)
    execution = ExecutionResult.from_wire(result.data["execution"])
    assert [stage.stage for stage in execution.stages] == ["fetch", "build", "run"]
    # The dispatcher's class stands; an OCI run never claims another.
    assert execution.executor_class is ExecutorClass.MANAGED_SANDBOX


def _npm_provisioned(tmp_path: Path) -> tuple[Path, Path, ProvisionedDependencies]:
    """A shared built ``node_modules`` and a seeded npm cache, as hermetic fetch/build hand them to the run stage."""

    modules, npm_cache = tmp_path / "deps" / "node_modules", tmp_path / "deps" / "npm-cache"
    (modules / "left-pad").mkdir(parents=True)
    (modules / "left-pad" / "index.js").write_text("module.exports = 'built'", encoding="utf-8")
    (modules / ".bin").mkdir()
    (modules / ".bin" / "left-pad").symlink_to("../left-pad/index.js")
    npm_cache.mkdir(parents=True)
    provisioned = ProvisionedDependencies(
        stages=(StageOutcome("fetch", "passed", network="registry_only"), StageOutcome("build", "passed")),
        mounts=((modules, "/workspace/node_modules"), (npm_cache, "/lemoncrow-npm-cache")),
        path_prefix=("/workspace/node_modules/.bin",),
        env=(
            ("npm_config_offline", "true"),
            ("npm_config_cache", "/lemoncrow-npm-cache"),
            ("npm_config_logs_dir", "/tmp/lemoncrow-npm-logs"),
        ),
        lockfile_digest="locks",
        ecosystems=("npm",),
    )
    return modules, npm_cache, provisioned


def test_node_modules_gets_a_per_run_overlay_under_podman_and_the_npm_cache_stays_read_only(
    tmp_path: Path, engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules, npm_cache, provisioned = _npm_provisioned(tmp_path)
    monkeypatch.setattr(runner_host, "provision_dependencies", lambda *_args, **_kwargs: provisioned)
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE)}, execution=_hermetic())
    assert result.status == "passed", result.summary
    options = engine.run_argv()[: engine.run_argv().index(REFERENCE)]
    # Podman's private overlay: writable in this container, discarded with it, the shared tree untouched.
    assert _pairs(options, "--volume") == [f"{modules}:/workspace/node_modules:O"]
    mounts = _pairs(options, "--mount")
    assert f"type=bind,source={npm_cache},target=/lemoncrow-npm-cache,readonly,relabel=shared" in mounts
    assert not [mount for mount in mounts if "target=/workspace/node_modules" in mount]
    # The overlay comes after /workspace, so it sits on top of the reviewed tree.
    workspace = next(m for m in mounts if m.endswith(",target=/workspace,relabel=private"))
    assert options.index("--volume") > options.index(workspace)


def test_node_modules_is_copied_per_run_under_docker_and_the_copy_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Engine(tmp_path / "engine", "docker")
    monkeypatch.setattr(oci, "detect_runtime", lambda **_kwargs: docker.runtime)
    _materialize(monkeypatch)
    modules, npm_cache, provisioned = _npm_provisioned(tmp_path)
    monkeypatch.setattr(runner_host, "provision_dependencies", lambda *_args, **_kwargs: provisioned)
    seen: dict[str, Any] = {}
    real = runners.run_limited_bounded

    def observe(argv: list[str], **kwargs: Any) -> Any:
        options = argv[: argv.index(REFERENCE)]
        (layer,) = [mount for mount in _pairs(options, "--mount") if "target=/workspace/node_modules" in mount]
        source = Path(layer.split("source=", 1)[1].split(",target=", 1)[0])
        seen.update(
            layer=layer,
            source=source,
            index=(source / "left-pad" / "index.js").read_text(encoding="utf-8"),
            link=os.readlink(source / ".bin" / "left-pad"),
        )
        (source / "left-pad" / "index.js").write_text("rewritten by the check", encoding="utf-8")
        return real(argv, **kwargs)

    monkeypatch.setattr(runners, "run_limited_bounded", observe)
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE)}, execution=_hermetic())
    assert result.status == "passed", result.summary
    assert not seen["layer"].endswith(",readonly") and "--volume" not in docker.run_argv()
    assert seen["source"] != modules and seen["source"].parent.name == "layers"
    # A copy of the built tree, symlinks kept; what the check wrote never reaches the shared tree.
    assert (seen["index"], seen["link"]) == ("module.exports = 'built'", "../left-pad/index.js")
    assert (modules / "left-pad" / "index.js").read_text(encoding="utf-8") == "module.exports = 'built'"
    assert not seen["source"].exists() and not seen["source"].parent.exists()
    mounts = _pairs(docker.run_argv(), "--mount")
    assert f"type=bind,source={npm_cache},target=/lemoncrow-npm-cache,readonly" in mounts


def test_a_mount_path_that_would_add_engine_options_is_refused(tmp_path: Path, engine: _Engine) -> None:
    client = oci.OciClient(engine.runtime)
    for mount in (
        oci.Mount(tmp_path / "a,readonly=false", "/x"),
        oci.Mount(tmp_path / "a", "/x,type=volume"),
        oci.Mount(tmp_path / "a:b", "/x", read_only=False, overlay=True),
        oci.Mount(tmp_path / "a", "relative"),
    ):
        with pytest.raises(oci.OciRefused):
            client.create_args(oci.ContainerSpec(name="n", image=IMAGE, mounts=(mount,)))
    docker = oci.OciClient(oci.OciRuntime("docker", "docker", True))
    with pytest.raises(oci.OciRefused, match="no overlay"):
        docker.create_args(
            oci.ContainerSpec(name="n", image=IMAGE, mounts=(oci.Mount(tmp_path, "/x", read_only=False, overlay=True),))
        )


def test_a_socket_mount_is_refused_before_the_engine_runs_anything(
    tmp_path: Path, engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_like = tmp_path / "deps" / "docker.sock"
    socket_like.mkdir(parents=True)
    provisioned = ProvisionedDependencies(mounts=((socket_like, "/lemoncrow-python"),))
    monkeypatch.setattr(runner_host, "provision_dependencies", lambda *_args, **_kwargs: provisioned)
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE)}, execution=_hermetic())
    assert result.status == "unavailable" and "container engine socket gives root on the host" in result.summary
    assert not any(call[0] == "run" for call in engine.calls)


@pytest.mark.parametrize(
    ("values", "policy", "reason"),
    [
        ({"observe": ObservationSpec()}, HermeticPolicy(), "runtime observation needs the bubblewrap run stage"),
        (
            {},
            HermeticPolicy(sidecars=(SidecarSpec("mock", ("python3", "mock.py"), 8080),)),
            "trusted-base sidecars need the bubblewrap run stage",
        ),
    ],
)
def test_observation_and_sidecars_are_refused_in_the_toolchain_image(
    tmp_path: Path, engine: _Engine, values: dict[str, Any], policy: HermeticPolicy, reason: str
) -> None:
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE), **values}, execution=_hermetic(policy))
    assert result.status == "unavailable" and result.summary.startswith(reason)
    assert "pinned [toolchain] image" in result.summary
    assert engine.calls == []


host_sandbox = pytest.mark.skipif(
    sys.platform != "darwin" and not (shutil.which("bwrap") or shutil.which("bubblewrap")),
    reason="the host run stage needs bubblewrap here",
)


def _host_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    ran: list[list[str]] = []

    def fake(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(runners, "run_limited_bounded", fake)
    return ran


@host_sandbox
def test_without_a_usable_engine_the_host_runtime_runs_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(**_kwargs: Any) -> oci.OciRuntime:
        raise oci.OciUnavailable("no rootless container engine: docker runs rootful")

    monkeypatch.setattr(oci, "detect_runtime", unavailable)
    _materialize(monkeypatch)
    ran = _host_run(monkeypatch)
    result = _run(tmp_path, {"toolchain": Toolchain(IMAGE)})
    assert result.status == "passed"
    note = f"toolchain {IMAGE} not used (no rootless container engine: docker runs rootful); the run stage ran on"
    assert note in result.summary
    execution = ExecutionResult.from_wire(result.data["execution"])
    assert note in execution.stages[-1].detail
    # Honest record: the host sandbox and the host image, never the toolchain's digest.
    assert execution.fingerprint.sandbox in {"bubblewrap", "seatbelt"}
    assert execution.fingerprint.image_digest != "sha256:" + "a" * 64
    assert ran and Path(ran[0][0]).name not in {"podman", "docker"}


@host_sandbox
def test_only_a_trusted_pinned_toolchain_selects_an_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def never(**_kwargs: Any) -> oci.OciRuntime:
        raise AssertionError("no engine is looked for")

    monkeypatch.setattr(oci, "detect_runtime", never)
    _materialize(monkeypatch)
    _host_run(monkeypatch)
    # Unpinned, or a plain string as YAML could give: the host path, with no toolchain note.
    for value in (Toolchain("python:3.12"), IMAGE, {"image": IMAGE}):
        result = _run(tmp_path / str(uuid.uuid4()), {"toolchain": value})
        assert result.status == "passed" and "toolchain" not in result.summary


def test_recipe_runtime_passes_only_a_pinned_toolchain() -> None:
    from lemoncrow.pro.capabilities.review.check_runs import recipe_runtime
    from lemoncrow.pro.capabilities.review.recipes import environment_recipes

    check = '[[checks]]\nid = "py"\npaths = ["."]\nrun = "true"\n'
    (pinned,), _ = environment_recipes(f'version = 1\n[toolchain]\nimage = "{IMAGE}"\n' + check)
    runtime, _surface = recipe_runtime(pinned)
    assert runtime.values["toolchain"] == Toolchain(IMAGE)
    (unpinned,), _ = environment_recipes('version = 1\n[toolchain]\nimage = "python:3.12"\n' + check)
    assert "toolchain" not in recipe_runtime(unpinned)[0].values
    (bare,), _ = environment_recipes("version = 1\n" + check)
    assert "toolchain" not in recipe_runtime(bare)[0].values


# --------------------------------------------------------------------------- #
# A real engine: LEMONCROW_OCI_INTEGRATION=1 and the pinned images present locally
# --------------------------------------------------------------------------- #

PYTHON_IMAGE = "python@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267"
REDIS_IMAGE = "redis@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"

_CHECK = """\
import json, os, socket

result = {"uid": os.getuid(), "cwd": os.getcwd()}
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    result["egress"] = "connected"
except OSError as exc:
    result["egress"] = type(exc).__name__
host = os.environ.get("LC_SERVICE_CACHE_HOST", "")
if host:
    with socket.create_connection((host, 6379), timeout=5) as conn:
        conn.sendall(b"PING\\r\\n")
        result["cache"] = conn.recv(64).decode().strip()
with open("written-by-check.txt", "w") as handle:
    handle.write("ok")
report = os.environ.get("LEMONCROW_REPORT_DIR")
if report:
    with open(os.path.join(report, "report"), "w") as handle:
        handle.write("report from the image")
print(json.dumps(result))
"""


def _real_engine(monkeypatch: pytest.MonkeyPatch) -> oci.OciClient:
    if os.environ.get("LEMONCROW_OCI_INTEGRATION") != "1":
        pytest.skip("set LEMONCROW_OCI_INTEGRATION=1 to run against a real container engine")
    monkeypatch.setenv(oci.ROOTFUL_OVERRIDE_ENV, "1")
    try:
        client = oci.OciClient(oci.detect_runtime())
    except oci.OciUnavailable as exc:
        pytest.skip(str(exc))
    for image in (PYTHON_IMAGE, REDIS_IMAGE):
        done = subprocess.run(
            [client.runtime.binary, "image", "inspect", image], capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            pytest.skip(f"{image} is not present locally")
    return client


@pytest.mark.parametrize("with_services", [False, True], ids=["no-network", "internal-network"])
def test_a_real_engine_runs_the_check_in_its_toolchain_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_services: bool
) -> None:
    client = _real_engine(monkeypatch)
    _materialize(monkeypatch, {"check.py": _CHECK})
    review = f"oci-it-{uuid.uuid4().hex[:12]}"
    values: dict[str, Any] = {"toolchain": Toolchain(PYTHON_IMAGE), "report": True}
    if with_services:
        values["services"] = ServiceSet((Service("cache", REDIS_IMAGE, "redis-cli ping"),))
    try:
        result = _run(tmp_path, values, review=review)
    finally:
        label = ("dev.lemoncrow.review", review)
        leftovers = client.containers(label), client.networks(label)
    assert result.status == "passed", (result.summary, result.output)
    observed = json.loads(result.output.splitlines()[-1])
    assert observed["egress"] != "connected" and observed["cwd"] == "/workspace"
    if with_services:
        assert observed["cache"] == "+PONG"
    else:
        assert "cache" not in observed
    workspace = next((tmp_path / "store/review/runtime-runs").rglob("written-by-check.txt"))
    assert workspace.read_text() == "ok" and workspace.stat().st_uid == os.getuid()
    assert result.data["report"] == "report from the image"
    execution = ExecutionResult.from_wire(result.data["execution"])
    assert execution.fingerprint.image_digest == PYTHON_IMAGE.split("@", 1)[1]
    assert execution.fingerprint.sandbox == f"oci-{client.runtime.engine}"
    assert f"ran in {oci.pinned_reference(PYTHON_IMAGE)} under" in execution.stages[-1].detail
    assert "cleanup failed" not in execution.stages[-1].detail
    # Nothing is left behind: not the check container, not a service, not the network.
    assert leftovers == ([], [])


NODE_IMAGE = "node@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202"


def _npm_tarball(name: str, version: str, index: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for member, data in (
            ("package/package.json", json.dumps({"name": name, "version": version}).encode()),
            ("package/index.js", index.encode()),
        ):
            info = tarfile.TarInfo(member)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_a_real_engine_runs_npm_ci_offline_over_the_shared_node_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _real_engine(monkeypatch)
    if subprocess.run(
        [client.runtime.binary, "image", "inspect", NODE_IMAGE], capture_output=True, check=False
    ).returncode:
        pytest.skip(f"{NODE_IMAGE} is not present locally")
    tarball = _npm_tarball("left-pad", "1.0.0", "module.exports = 'from the seeded cache'")
    digest = hashlib.sha512(tarball).digest()
    integrity = "sha512-" + base64.b64encode(digest).decode()
    lock = {
        "name": "app",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "1.0.0"}},
            "node_modules/left-pad": {
                "version": "1.0.0",
                "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.0.0.tgz",
                "integrity": integrity,
            },
        },
    }
    _materialize(
        monkeypatch,
        {
            "package.json": json.dumps({"name": "app", "version": "1.0.0", "dependencies": {"left-pad": "1.0.0"}}),
            "package-lock.json": json.dumps(lock),
        },
    )
    modules, npm_cache, provisioned = _npm_provisioned(tmp_path)
    hexdigest = digest.hex()
    entry = npm_cache / "_cacache" / "content-v2" / "sha512" / hexdigest[:2] / hexdigest[2:4] / hexdigest[4:]
    entry.parent.mkdir(parents=True)
    entry.write_bytes(tarball)
    monkeypatch.setattr(runner_host, "provision_dependencies", lambda *_args, **_kwargs: provisioned)
    review = f"oci-npm-{uuid.uuid4().hex[:12]}"
    try:
        result = _run(
            tmp_path,
            {"toolchain": Toolchain(NODE_IMAGE)},
            execution=_hermetic(),
            review=review,
            command=("sh", "-c", "npm ci --offline --no-audit --no-fund && node -p \"require('left-pad')\""),
        )
    finally:
        label = ("dev.lemoncrow.review", review)
        leftovers = client.containers(label), client.networks(label)
    # `npm ci` emptied the per-run layer and installed from the read-only seeded cache, with no network.
    assert result.status == "passed", (result.summary, result.output)
    assert result.output.strip().splitlines()[-1] == "from the seeded cache"
    # The shared built tree is as it was built, and the per-run copy is gone.
    assert (modules / "left-pad" / "index.js").read_text(encoding="utf-8") == "module.exports = 'built'"
    assert not list((tmp_path / "store/review/runtime-runs").rglob("layers"))
    assert leftovers == ([], [])
