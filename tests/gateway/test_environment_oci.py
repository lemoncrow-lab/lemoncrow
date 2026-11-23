"""The OCI backend of the environment core, the egress gateway's rules and the services runner (plan M4)."""

from __future__ import annotations

import json
import socket
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from lemoncrow_client.environment import Service

from lemoncrow.pro.environment import egress_gateway, oci
from lemoncrow.pro.environment.services import ServicesRunner, ServiceUnavailable, StartedService

DIGEST = "sha256:" + "c" * 64


def _runner(answers: dict[str, tuple[int, str]]):
    def run(argv: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        code, out = answers[Path(argv[0]).name]
        return subprocess.CompletedProcess(list(argv), code, out, "")

    return run


def _which(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_rootless_podman_is_preferred_and_rootless_docker_is_the_fallback() -> None:
    answers = {"podman": (0, "true\n"), "docker": (0, '["name=rootless"]')}
    found = oci.detect_runtime(environ={}, which=_which("podman", "docker"), runner=_runner(answers))
    assert (found.engine, found.rootless) == ("podman", True)
    found = oci.detect_runtime(environ={}, which=_which("docker"), runner=_runner(answers))
    assert (found.engine, found.rootless) == ("docker", True)


def test_a_rootful_daemon_is_refused_unless_the_development_override_is_set() -> None:
    answers = {"podman": (0, "false"), "docker": (0, '["name=seccomp"]')}
    with pytest.raises(oci.OciUnavailable) as refused:
        oci.detect_runtime(environ={}, which=_which("podman", "docker"), runner=_runner(answers))
    message = str(refused.value)
    assert "podman runs rootful" in message and "docker runs rootful" in message
    assert oci.ROOTFUL_OVERRIDE_ENV in message and "root on the host" in message
    found = oci.detect_runtime(
        environ={oci.ROOTFUL_OVERRIDE_ENV: "1"}, which=_which("podman", "docker"), runner=_runner(answers)
    )
    assert (found.engine, found.rootless) == ("podman", False)
    assert "development only" in found.describe()


def test_a_forced_engine_is_the_only_one_tried() -> None:
    answers = {"podman": (0, "true"), "docker": (0, '["name=rootless"]')}
    found = oci.detect_runtime(
        environ={oci.ENGINE_ENV: "docker"}, which=_which("podman", "docker"), runner=_runner(answers)
    )
    assert found.engine == "docker"
    with pytest.raises(oci.OciUnavailable, match="podman or docker"):
        oci.detect_runtime(environ={oci.ENGINE_ENV: "lxc"}, which=_which(), runner=_runner(answers))
    with pytest.raises(oci.OciUnavailable, match="not installed"):
        oci.detect_runtime(environ={}, which=_which(), runner=_runner(answers))


def test_images_run_by_digest_only() -> None:
    assert oci.pinned_reference(f"python:3.12@{DIGEST}") == f"docker.io/library/python@{DIGEST}"
    assert oci.pinned_reference(f"ghcr.io:443/a/b:tag@{DIGEST}") == f"ghcr.io:443/a/b@{DIGEST}"
    # Spelled out as Docker reads a short name, so Podman never searches its own registries for it.
    for short, full in (
        ("acme/app", "docker.io/acme/app"),
        ("docker.io/redis", "docker.io/library/redis"),
        ("index.docker.io/acme/app", "docker.io/acme/app"),
        ("localhost/app", "localhost/app"),
        ("registry:5000/app", "registry:5000/app"),
    ):
        assert oci.pinned_reference(f"{short}@{DIGEST}") == f"{full}@{DIGEST}"
        assert oci.pinned_reference(oci.pinned_reference(f"{short}@{DIGEST}")) == f"{full}@{DIGEST}"
    for unpinned in ("python:3.12", "python", "python@sha256:short"):
        with pytest.raises(oci.OciRefused):
            oci.pinned_reference(unpinned)


def test_the_engine_socket_is_never_mounted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    listener = socket.socket(socket.AF_UNIX)
    sock_path = Path("/tmp") / f"lc-oci-test-{tmp_path.name[-8:]}.sock"
    sock_path.unlink(missing_ok=True)
    listener.bind(str(sock_path))
    try:
        environ = {"DOCKER_HOST": f"unix://{run_dir}/docker.sock"}
        for source in (sock_path, run_dir, run_dir / "docker.sock", tmp_path / "x" / "podman.sock"):
            with pytest.raises(oci.OciRefused):
                oci.refuse_socket_mounts([source], environ=environ)
        oci.refuse_socket_mounts([tmp_path / "work"], environ=environ)
    finally:
        listener.close()
        sock_path.unlink(missing_ok=True)


def _client(rootless: bool = True, engine: oci.Engine = "podman") -> oci.OciClient:
    return oci.OciClient(oci.OciRuntime(engine, f"/usr/bin/{engine}", rootless), environ={"PATH": "/usr/bin"})


def test_containers_drop_capabilities_and_never_join_the_host_network(tmp_path: Path) -> None:
    spec = oci.ContainerSpec(
        name="t",
        image=f"python@{DIGEST}",
        network="net",
        aliases=("sandbox",),
        mounts=(oci.Mount(tmp_path, "/workspace", read_only=False), oci.Mount(tmp_path / "g", "/g")),
        publish=(9100,),
        host_user=True,
        init=True,
    )
    args = _client().create_args(spec)
    assert "--cap-drop=ALL" in args and "--security-opt=no-new-privileges" in args
    assert "--cap-add=NET_RAW" not in args and "--cap-add=SYS_ADMIN" not in args
    assert "--userns=keep-id" in args and "--init" in args
    assert args[args.index("--publish") + 1] == "127.0.0.1::9100"
    assert f"type=bind,source={tmp_path},target=/workspace,relabel=private" in args
    assert f"type=bind,source={tmp_path / 'g'},target=/g,readonly,relabel=private" in args
    rootful = _client(rootless=False, engine="docker").create_args(spec)
    assert "--user" in rootful and "--userns=keep-id" not in rootful
    assert f"type=bind,source={tmp_path / 'g'},target=/g,readonly" in rootful  # Docker's --mount has no relabel
    assert "--user" not in _client(rootless=True, engine="docker").create_args(spec)
    with pytest.raises(oci.OciRefused, match="host network"):
        _client().create_args(oci.ContainerSpec(name="t", image="x", network="host"))
    with pytest.raises(oci.OciRefused, match="host network"):
        _client().create_args(oci.ContainerSpec(name="t", image="x", network="n", extra_networks=(("host", ()),)))


def test_podman_labels_each_mount_for_its_readers_under_selinux(tmp_path: Path) -> None:
    """A task's own files for its level only; a cache every container reads, for all; never the overlay's source."""

    spec = oci.ContainerSpec(
        name="t",
        image=f"python@{DIGEST}",
        mounts=(
            oci.Mount(tmp_path / "work", "/workspace", read_only=False),
            oci.Mount(tmp_path / "cache", "/deps", shared=True),
            oci.Mount(tmp_path / "tree", "/tree", read_only=False, overlay=True),
        ),
        selinux_level="s0:c12,c345",
    )
    args = _client().create_args(spec)
    assert "--security-opt=label=level:s0:c12,c345" in args
    assert f"type=bind,source={tmp_path / 'work'},target=/workspace,relabel=private" in args
    assert f"type=bind,source={tmp_path / 'cache'},target=/deps,readonly,relabel=shared" in args
    assert f"{tmp_path / 'tree'}:/tree:O" in args
    docker = _client(engine="docker").create_args(
        oci.ContainerSpec(name="t", image=f"python@{DIGEST}", mounts=spec.mounts[:2], selinux_level="s0:c1,c2")
    )
    assert not any("relabel" in arg or "label=level" in arg for arg in docker)
    for bad in ("s0:c1,c2,type=bind", "s0 c1", "level", "s0:c1;c2"):
        with pytest.raises(oci.OciRefused, match="not an SELinux level"):
            _client().create_args(oci.ContainerSpec(name="t", image=f"python@{DIGEST}", selinux_level=bad))


def test_a_containers_selinux_level_is_read_from_its_process_label() -> None:
    client = _client()
    answers = {
        "lc-ws-a": (0, "system_u:system_r:container_t:s0:c209,c393\n"),
        "lc-ws-off": (0, "\n"),  # SELinux disabled
        "lc-ws-gone": (125, ""),
        "lc-ws-odd": (0, "system_u:system_r:container_t:s0:c1,c2,type=x\n"),
    }

    def cli(*args: str, check: bool = True, **_: object) -> subprocess.CompletedProcess[str]:
        code, out = answers[args[-1]]
        return subprocess.CompletedProcess(list(args), code, out, "")

    client._cli = cli  # type: ignore[method-assign]
    assert client.selinux_level("lc-ws-a") == "s0:c209,c393"
    assert [client.selinux_level(name) for name in ("lc-ws-off", "lc-ws-gone", "lc-ws-odd")] == ["", "", ""]
    assert _client(engine="docker").selinux_level("lc-ws-a") == ""


def test_the_engine_client_gets_only_its_own_configuration() -> None:
    client = oci.OciClient(
        oci.OciRuntime("docker", "/usr/bin/docker", True),
        environ={"PATH": "/bin", "DOCKER_HOST": "unix:///x", "LC_WORKSPACES_TOKEN": "secret", "GIT_TOKEN": "t"},
    )
    assert client.engine_env == {"PATH": "/bin", "DOCKER_HOST": "unix:///x"}


# -- egress gateway -------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700::1111", True),
        ("10.0.0.1", False),
        ("172.17.0.1", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fd00::1", False),
        ("::ffff:10.0.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("not-an-address", False),
    ],
)
def test_the_gateway_connects_only_to_public_addresses(address: str, public: bool) -> None:
    assert egress_gateway.public_address(address) is public


def test_the_gateway_admits_granted_hosts_on_web_ports_only() -> None:
    allow, ports = ["registry.npmjs.org"], [80, 443]
    assert egress_gateway.decide("Registry.NPMJS.org.", 443, allow, ports) is None
    assert egress_gateway.decide("evil.example", 443, allow, ports) == "not granted to this task"
    assert egress_gateway.decide("registry.npmjs.org", 22, allow, ports) == "port 22 is not allowed"
    assert "address" in str(egress_gateway.decide("10.0.0.1", 443, allow, ports))
    assert "address" in str(egress_gateway.decide("[::1]", 443, allow, ports))


def test_proxy_request_targets_are_parsed_strictly() -> None:
    assert egress_gateway.parse_target("CONNECT", "pypi.org:443") == ("pypi.org", 443, "")
    assert egress_gateway.parse_target("GET", "http://pypi.org/simple/x") == ("pypi.org", 80, "/simple/x")
    assert egress_gateway.parse_target("GET", "http://[2001:db8::1]:8080") == ("2001:db8::1", 8080, "/")
    for method, target in (("GET", "/relative"), ("GET", "http://u:p@pypi.org/"), ("CONNECT", "pypi.org")):
        with pytest.raises(ValueError):
            egress_gateway.parse_target(method, target)


def test_the_gateway_configuration_holds_names_and_ports_only() -> None:
    config = egress_gateway.gateway_config(["PyPI.org", "pypi.org"], relays=[(9100, "sandbox", 8100)])
    assert config == {
        "allow": ["pypi.org"],
        "ports": [80, 443],
        "proxy_port": 3128,
        "relays": [{"listen": 9100, "host": "sandbox", "port": 8100}],
    }
    json.dumps(config)


# -- services ------------------------------------------------------------------- #


class _Backend:
    def __init__(self, fail_start: str = "", never_ready: str = "") -> None:
        self.events: list[str] = []
        self._fail_start, self._never_ready = fail_start, never_ready

    def start(self, service: Service) -> StartedService:
        if service.name == self._fail_start:
            raise RuntimeError("no image")
        self.events.append(f"start {service.name}")
        return StartedService(service.name, handle=service.name)

    def probe(self, started: StartedService, service: Service) -> bool:
        return service.name != self._never_ready

    def stop(self, started: StartedService) -> None:
        self.events.append(f"stop {started.name}")


SERVICES = (Service("db", f"postgres@{DIGEST}", "true"), Service("cache", f"redis@{DIGEST}", "true"))


def test_start_all_keeps_services_up_until_stop_all_stops_them_in_reverse() -> None:
    backend = _Backend()
    runner = ServicesRunner(backend, sleep=lambda _s: None)
    started = runner.start_all(SERVICES)
    assert [s.name for s in started] == ["db", "cache"] and backend.events == ["start db", "start cache"]
    assert runner.stop_all(started) == ()
    assert backend.events[2:] == ["stop cache", "stop db"]


def test_a_failed_start_stops_what_already_started() -> None:
    backend = _Backend(fail_start="cache")
    with pytest.raises(ServiceUnavailable, match="cache could not start"):
        ServicesRunner(backend, sleep=lambda _s: None).start_all(SERVICES)
    assert backend.events == ["start db", "stop db"]
    clock = iter(range(0, 1000, 100))
    backend = _Backend(never_ready="db")
    with pytest.raises(ServiceUnavailable, match="not ready"):
        ServicesRunner(backend, clock=lambda: float(next(clock)), sleep=lambda _s: None).start_all(SERVICES)
    assert backend.events == ["start db", "stop db"]
