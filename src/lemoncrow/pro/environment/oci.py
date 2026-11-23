"""Rootless OCI containers: Workspaces task sandboxes, their services, and Review's toolchain runs.

One engine serves both products. :func:`detect_runtime` prefers rootless Podman and falls back to rootless Docker;
a rootful daemon is refused unless ``LEMONCROW_OCI_ALLOW_ROOTFUL=1`` says this is a development machine (Docker
Desktop counts as rootful), and the refusal says so. ``LEMONCROW_OCI_ENGINE=podman|docker`` names one engine.

Rules every caller gets from :class:`OciClient`, not from its own care:

* images run only by digest (``name@sha256:...``); an image that is not pinned is refused, and a pull happens only
  when the pinned image is not present;
* nothing ever mounts the host's container socket: a mount whose source is, holds or sits beside a Docker or
  Podman socket is refused before the engine is asked (:func:`refuse_socket_mounts`);
* every container drops all capabilities but a short list a package install or a service needs, and sets
  ``no-new-privileges``;
* only names and non-secret values travel on the engine's argv; a secret goes in through ``exec`` standard input
  (:meth:`OciClient.write_file`) into a tmpfs, so it is never on disk and never in ``ps``;
* everything is labelled with its owner, so an owner can list and remove what it left behind;
* under SELinux (Fedora, RHEL) Podman relabels every bind mount: for the container's own level, or for every
  container when :attr:`Mount.shared` says other owners read it too. Containers given one
  :attr:`ContainerSpec.selinux_level` read each other's private files; no other container does.

:class:`OciServiceBackend` is the :class:`~lemoncrow.pro.environment.services.ServiceBackend` both products use:
each service runs on the caller's network under its own name, with no published port, so only containers on that
network (the task's sandbox, or one Review run) reach it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final, Literal

from lemoncrow_client.environment import Service, image_digest

from lemoncrow.pro.environment.services import StartedService

Engine = Literal["podman", "docker"]
ENGINE_ENV: Final[str] = "LEMONCROW_OCI_ENGINE"
ROOTFUL_OVERRIDE_ENV: Final[str] = "LEMONCROW_OCI_ALLOW_ROOTFUL"
ENGINES: Final[tuple[Engine, ...]] = ("podman", "docker")
#: Label on everything this module creates; its value names the owner (a Workspaces server, one Review run).
OWNER_LABEL: Final[str] = "dev.lemoncrow.owner"
#: Capabilities a container keeps: enough to install packages, drop to a service user and bind a low port.
#: NET_RAW (raw sockets, ARP spoofing on the task network), MKNOD, SETFCAP and the rest are dropped.
KEPT_CAPABILITIES: Final[tuple[str, ...]] = (
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "SETGID",
    "SETUID",
    "NET_BIND_SERVICE",
)
PROBE_TIMEOUT_SECONDS: Final[float] = 20.0
PULL_TIMEOUT_SECONDS: Final[float] = 900.0
CONTROL_TIMEOUT_SECONDS: Final[float] = 60.0
#: The most output one ``exec`` keeps; the rest of a flood is dropped and the result says so.
MAX_EXEC_OUTPUT_BYTES: Final[int] = 4 * 1024 * 1024
_SOCKET_NAMES: Final[frozenset[str]] = frozenset({"docker.sock", "podman.sock", "containerd.sock"})
#: An SELinux MLS/MCS level such as ``s0:c12,c345``.
_SELINUX_LEVEL: Final[re.Pattern[str]] = re.compile(r"s\d+(?:-s\d+)?(?::c\d+(?:\.c\d+)?(?:,c\d+(?:\.c\d+)?)*)?")


class OciUnavailable(RuntimeError):
    """No usable container engine, or the engine refused an operation; the message is safe to show."""


class OciRefused(OciUnavailable):
    """An operation this module never performs (an unpinned image, a socket mount)."""


@dataclass(frozen=True, slots=True)
class OciRuntime:
    """The engine chosen by :func:`detect_runtime`."""

    engine: Engine
    binary: str
    rootless: bool

    def describe(self) -> str:
        if self.rootless:
            return f"rootless {self.engine}"
        return f"rootful {self.engine} (allowed by {ROOTFUL_OVERRIDE_ENV}=1, for development only)"


Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _probe(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL
    )


def _rootless(engine: Engine, binary: str, runner: Runner) -> bool | None:
    """Whether *engine* runs rootless; ``None`` when it does not answer."""

    try:
        if engine == "podman":
            done = runner([binary, "info", "--format", "{{.Host.Security.Rootless}}"], PROBE_TIMEOUT_SECONDS)
            if done.returncode != 0:
                return None
            return done.stdout.strip().lower() == "true"
        done = runner([binary, "info", "--format", "{{json .SecurityOptions}}"], PROBE_TIMEOUT_SECONDS)
        if done.returncode != 0:
            return None
        options = json.loads(done.stdout.strip() or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return isinstance(options, list) and any("rootless" in str(item) for item in options)


def detect_runtime(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = _probe,
) -> OciRuntime:
    """The engine to use: rootless Podman, else rootless Docker, else (only with the override) a rootful one.

    Raises :class:`OciUnavailable` naming why each engine was not used.
    """

    env = os.environ if environ is None else environ
    forced = env.get(ENGINE_ENV, "").strip().lower()
    if forced and forced not in ENGINES:
        raise OciUnavailable(f"{ENGINE_ENV} must be podman or docker, got {forced!r}")
    order: tuple[Engine, ...] = (forced,) if forced else ENGINES  # type: ignore[assignment]
    allow_rootful = env.get(ROOTFUL_OVERRIDE_ENV, "").strip() == "1"
    reasons: list[str] = []
    rootful: list[OciRuntime] = []
    for engine in order:
        binary = which(engine)
        if not binary:
            reasons.append(f"{engine} is not installed")
            continue
        rootless = _rootless(engine, binary, runner)
        if rootless is None:
            reasons.append(f"{engine} is installed but does not answer")
        elif rootless:
            return OciRuntime(engine, binary, True)
        else:
            rootful.append(OciRuntime(engine, binary, False))
            reasons.append(f"{engine} runs rootful")
    if rootful and allow_rootful:
        return rootful[0]
    hint = (
        f"; a rootful daemon gives its containers root on the host, so it is refused "
        f"(set {ROOTFUL_OVERRIDE_ENV}=1 only on a development machine)"
        if rootful
        else ""
    )
    raise OciUnavailable("no rootless container engine: " + "; ".join(reasons) + hint)


def pinned_reference(image: str) -> str:
    """``registry/repository@sha256:...`` for an image pinned by digest (any tag dropped); refused when not pinned.

    A short name is spelled out as Docker reads it (``redis`` is ``docker.io/library/redis``), so a definition
    names the same image on every engine: Podman would otherwise search its own registries, and with Fedora's
    ``short-name-mode = "enforcing"`` it refuses a short name it has no alias for when no terminal can prompt.
    """

    digest = image_digest(image)
    if not digest:
        raise OciRefused(f"image {image!r} is not pinned by digest; images run only as name@sha256:<digest>")
    name = image[: image.index("@")]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    return f"{_qualified(name)}@{digest}"


def _qualified(name: str) -> str:
    first, slash, rest = name.partition("/")
    if not slash:
        return f"docker.io/library/{name}"
    if first in ("docker.io", "index.docker.io"):
        return f"docker.io/{rest}" if "/" in rest else f"docker.io/library/{rest}"
    if "." in first or ":" in first or first == "localhost":
        return name
    return f"docker.io/{name}"


def _known_sockets(environ: Mapping[str, str]) -> list[Path]:
    found = [Path("/var/run/docker.sock"), Path("/run/docker.sock"), Path("/run/podman/podman.sock")]
    runtime_dir = environ.get("XDG_RUNTIME_DIR", "")
    if runtime_dir:
        found += [Path(runtime_dir) / "docker.sock", Path(runtime_dir) / "podman" / "podman.sock"]
    home = environ.get("HOME", "")
    if home:
        found += [Path(home) / ".docker" / "run" / "docker.sock", Path(home) / ".colima" / "docker.sock"]
    for variable in ("DOCKER_HOST", "CONTAINER_HOST"):
        value = environ.get(variable, "")
        if value.startswith("unix://"):
            found.append(Path(value.removeprefix("unix://")))
    return found


def refuse_socket_mounts(sources: Iterable[Path], *, environ: Mapping[str, str] | None = None) -> None:
    """Refuse any mount that would hand a container the engine's socket.

    A source that is a socket, is named like one, or is a directory holding a known engine socket is refused: any
    of them gives the container root on the host.
    """

    env = os.environ if environ is None else environ
    sockets = []
    for path in _known_sockets(env):
        try:
            sockets.append(path.resolve())
        except OSError:
            sockets.append(path)
    for source in sources:
        resolved = source.resolve()
        try:
            mode = resolved.stat().st_mode
        except OSError:
            mode = 0
        if stat.S_ISSOCK(mode) or resolved.name in _SOCKET_NAMES:
            raise OciRefused(f"refusing to mount {source}: a container engine socket gives root on the host")
        for socket_path in sockets:
            if socket_path == resolved or socket_path.is_relative_to(resolved):
                raise OciRefused(f"refusing to mount {source}: it holds the container engine socket {socket_path}")


@dataclass(frozen=True, slots=True)
class Mount:
    source: Path
    target: str
    read_only: bool = True
    #: A writable layer over a read-only *source*, private to this container and discarded with it (Podman's
    #: ``:O`` overlay). Ask :meth:`OciClient.writable_layer` for one: it copies where the engine has no overlay.
    overlay: bool = False
    #: Containers of other owners read *source* too (a cache, the published store): under SELinux it is labelled
    #: for every container. Otherwise it is labelled for this container's level only (see the module notes).
    shared: bool = False


def _check_mount(mount: Mount) -> None:
    """Refuse a path that would add options to the engine's mount syntax (``,`` for ``--mount``, ``:`` for ``-v``)."""

    for value in (str(mount.source), mount.target):
        if any(char in value for char in ",\n\r\0") or (mount.overlay and ":" in value):
            raise OciRefused(f"refusing to mount {value!r}: the path holds a character the engine reads as syntax")
    if not mount.target.startswith("/"):
        raise OciRefused(f"refusing to mount at {mount.target!r}: the target must be an absolute container path")


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """One container. ``image`` must be pinned; ``env`` holds names and non-secret values only."""

    name: str
    image: str
    command: tuple[str, ...] = ()
    entrypoint: str | None = None
    #: The first network; ``none`` for no network at all.
    network: str = "none"
    #: Names this container answers to on ``network``.
    aliases: tuple[str, ...] = ()
    #: More networks, joined before the container starts: ``(network, aliases)``.
    extra_networks: tuple[tuple[str, tuple[str, ...]], ...] = ()
    mounts: tuple[Mount, ...] = ()
    #: ``path`` or ``path:options`` tmpfs mounts (memory, never disk).
    tmpfs: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    workdir: str = ""
    labels: tuple[tuple[str, str], ...] = ()
    #: Container ports published on the host's loopback only, at ports the engine picks.
    publish: tuple[int, ...] = ()
    #: Run as the user that owns the bind-mounted files on the host (the sandbox), not as the image's user.
    host_user: bool = False
    #: An explicit ``uid:gid`` (a helper that owns no files), instead of ``host_user``.
    user: str = ""
    #: A minimal init as PID 1, so a long-lived container reaps the processes ``exec`` leaves behind.
    init: bool = False
    read_only_rootfs: bool = False
    memory: str = ""
    pids_limit: int = 0
    cpus: str = ""
    #: Podman under SELinux: run at this level (:meth:`OciClient.selinux_level` of another container), so this
    #: container reads the files that one's private mounts were labelled for. Empty: the engine picks a level.
    selinux_level: str = ""


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False
    truncated: bool = False


@dataclass
class _Drain:
    """Keeps the last :data:`MAX_EXEC_OUTPUT_BYTES` of a stream, so a flood never fills memory."""

    limit: int
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    dropped: bool = False

    def feed(self, stream: IO[bytes]) -> None:
        read = stream.read
        while True:
            chunk = read(65536)
            if not chunk:
                return
            self.chunks.append(chunk)
            self.size += len(chunk)
            while self.size > self.limit and len(self.chunks) > 1:
                self.size -= len(self.chunks.pop(0))
                self.dropped = True

    def text(self) -> str:
        data = b"".join(self.chunks)
        if len(data) > self.limit:
            data, self.dropped = data[-self.limit :], True
        return data.decode("utf-8", errors="replace")


class OciClient:
    """The engine's command line, with the rules in the module docstring applied to every call."""

    def __init__(self, runtime: OciRuntime, *, environ: Mapping[str, str] | None = None) -> None:
        self.runtime = runtime
        self._environ = dict(os.environ if environ is None else environ)
        # The engine's own client needs only its configuration, never the caller's secrets.
        keep = ("PATH", "HOME", "XDG_RUNTIME_DIR", "DOCKER_HOST", "DOCKER_CONFIG", "CONTAINER_HOST", "TMPDIR", "USER")
        self._engine_env = {key: self._environ[key] for key in keep if key in self._environ}

    @property
    def engine_env(self) -> dict[str, str]:
        """The environment the engine's CLI runs with: its own configuration only, never the caller's secrets."""

        return dict(self._engine_env)

    # -- plumbing ------------------------------------------------------------ #

    def _cli(
        self, *args: str, timeout: float = CONTROL_TIMEOUT_SECONDS, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            done = subprocess.run(
                [self.runtime.binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                env=self._engine_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise OciUnavailable(f"{self.runtime.engine} {args[0]} did not finish within {timeout:g} s") from exc
        except OSError as exc:
            raise OciUnavailable(f"{self.runtime.engine} could not run: {exc}") from exc
        if check and done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()
            raise OciUnavailable(f"{self.runtime.engine} {args[0]} failed: {detail[-1] if detail else done.returncode}")
        return done

    def _labels(self, labels: Iterable[tuple[str, str]]) -> list[str]:
        return [arg for key, value in labels for arg in ("--label", f"{key}={value}")]

    # -- images ------------------------------------------------------------- #

    def ensure_image(self, image: str) -> str:
        """The pinned reference, pulled only when it is not present yet."""

        reference = pinned_reference(image)
        if self._cli("image", "inspect", reference, check=False).returncode != 0:
            self._cli("pull", "--quiet", reference, timeout=PULL_TIMEOUT_SECONDS)
        return reference

    # -- networks ----------------------------------------------------------- #

    def create_network(self, name: str, *, internal: bool, labels: Iterable[tuple[str, str]] = ()) -> str:
        """A bridge network; ``internal`` has no route out, so nothing on it reaches beyond its members."""

        args = ["network", "create", *self._labels(labels)]
        if internal:
            args.append("--internal")
        self._cli(*args, name)
        return name

    def remove_network(self, name: str) -> None:
        done = self._cli("network", "rm", name, check=False)
        if done.returncode != 0 and "not found" not in (done.stderr + done.stdout).lower():
            if self.network_exists(name):
                raise OciUnavailable(f"{self.runtime.engine} network rm failed: {done.stderr.strip()[-300:]}")

    def network_exists(self, name: str) -> bool:
        return self._cli("network", "inspect", name, check=False).returncode == 0

    def networks(self, label: tuple[str, str]) -> list[str]:
        done = self._cli("network", "ls", "--filter", f"label={label[0]}={label[1]}", "--format", "{{.Name}}")
        return [line.strip() for line in done.stdout.splitlines() if line.strip()]

    # -- containers --------------------------------------------------------- #

    @property
    def supports_overlay(self) -> bool:
        """Whether :class:`Mount` ``overlay`` works here: Podman's ``:O`` volumes; Docker has none."""

        return self.runtime.engine == "podman"

    def writable_layer(self, source: Path, target: str, scratch: Path) -> Mount:
        """*source* at *target*, writable by one container while *source* itself stays as it is.

        Podman lays a private overlay over the read-only source; elsewhere *source* is copied (symlinks kept) into
        *scratch*, a fresh directory the caller owns and removes after the run.
        """

        if self.supports_overlay:
            return Mount(source, target, read_only=False, overlay=True)
        # lc-debt: Docker has no overlay volume, so the shared tree is copied per run (time and disk grow with
        # it); upgrade: a reflink copy (cp --reflink=auto) where the filesystem has one, or Podman.
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, scratch, symlinks=True)
        return Mount(scratch, target, read_only=False)

    def _user_args(self, spec: ContainerSpec) -> list[str]:
        if not spec.host_user:
            return []
        if self.runtime.engine == "podman" and self.runtime.rootless:
            # The container's user is the host user, so the worktree stays the server's own files.
            return ["--userns=keep-id"]
        if self.runtime.rootless:
            # Rootless Docker maps container root to the host user already.
            return []
        return ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []

    def create_args(self, spec: ContainerSpec) -> list[str]:
        """The ``create``/``run`` options for *spec* (shared by :meth:`run` and one-shot ``run --rm`` callers)."""

        refuse_socket_mounts((mount.source for mount in spec.mounts), environ=self._environ)
        if spec.network == "host" or any(network == "host" for network, _ in spec.extra_networks):
            raise OciRefused("refusing the host network: a container joins only its own networks")
        if spec.aliases and spec.network == "none":
            raise OciRefused(f"{spec.name}: network aliases need a network")
        args = [
            "--name",
            spec.name,
            "--network",
            spec.network,
            "--cap-drop=ALL",
            *(f"--cap-add={cap}" for cap in KEPT_CAPABILITIES),
            "--security-opt=no-new-privileges",
            *self._labels(spec.labels),
            *self._user_args(spec),
            *(["--user", spec.user] if spec.user and not spec.host_user else []),
            *(["--init"] if spec.init else []),
        ]
        podman = self.runtime.engine == "podman"
        if spec.selinux_level and podman:
            if not _SELINUX_LEVEL.fullmatch(spec.selinux_level):
                raise OciRefused(f"{spec.name}: {spec.selinux_level!r} is not an SELinux level")
            args.append(f"--security-opt=label=level:{spec.selinux_level}")
        args += [arg for alias in spec.aliases for arg in ("--network-alias", alias)]
        for mount in spec.mounts:
            _check_mount(mount)
            if mount.overlay:
                if not self.supports_overlay:
                    raise OciRefused(f"{self.runtime.engine} has no overlay mounts; use OciClient.writable_layer")
                # Podman mounts the overlay with the container's own label: the source needs no relabel.
                args += ["--volume", f"{mount.source}:{mount.target}:O"]
                continue
            option = f"type=bind,source={mount.source},target={mount.target}"
            if mount.read_only:
                option += ",readonly"
            if podman:
                # A confined container reads only files labelled for it; Podman does nothing here without SELinux.
                # lc-debt: Docker's --mount has no relabel, so rootless Docker on an SELinux host needs its daemon's
                # SELinux support off; upgrade: --volume with :z/:Z when `docker info` lists selinux.
                option += ",relabel=shared" if mount.shared else ",relabel=private"
            args += ["--mount", option]
        args += [arg for item in spec.tmpfs for arg in ("--tmpfs", item)]
        args += [arg for key, value in spec.env for arg in ("--env", f"{key}={value}")]
        args += [arg for port in spec.publish for arg in ("--publish", f"127.0.0.1::{port}")]
        if spec.workdir:
            args += ["--workdir", spec.workdir]
        if spec.read_only_rootfs:
            args.append("--read-only")
        if spec.memory:
            args += ["--memory", spec.memory]
        if spec.pids_limit:
            args += ["--pids-limit", str(spec.pids_limit)]
        if spec.cpus:
            args += ["--cpus", spec.cpus]
        if spec.entrypoint is not None:
            args += ["--entrypoint", spec.entrypoint]
        return args

    def run_attached(self, spec: ContainerSpec) -> subprocess.Popen[bytes]:
        """``run -i --rm`` *spec* with its standard input and output on pipes: a stdio server in a container.

        The container goes when its input closes; a container a killed client left behind under the same name is
        removed first. Standard error is dropped, so nothing the container prints reaches the server's log.
        """

        reference = self.ensure_image(spec.image)
        self.remove(spec.name)
        argv = [self.runtime.binary, "run", "-i", "--rm", "--pull=never", *self.create_args(spec), reference]
        return subprocess.Popen(
            [*argv, *spec.command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.engine_env,
        )

    def run(self, spec: ContainerSpec) -> str:
        """Create, join the extra networks and start *spec*; returns its name. Removed again when a step fails."""

        reference = self.ensure_image(spec.image)
        self._cli("create", *self.create_args(spec), reference, *spec.command)
        try:
            for network, aliases in spec.extra_networks:
                self._cli("network", "connect", *(f"--alias={alias}" for alias in aliases), network, spec.name)
            self._cli("start", spec.name)
        except BaseException:
            self.remove(spec.name)
            raise
        return spec.name

    def state(self, name: str) -> str:
        """``running``, ``paused``, ``exited`` ... or ``""`` when there is no such container."""

        done = self._cli("inspect", "--format", "{{.State.Status}}", name, check=False)
        return done.stdout.strip().lower() if done.returncode == 0 else ""

    def selinux_level(self, name: str) -> str:
        """The SELinux level *name* runs at (``s0:c1,c2``); ``""`` without SELinux, with Docker or when it is gone."""

        if self.runtime.engine != "podman":
            return ""
        done = self._cli("inspect", "--format", "{{.ProcessLabel}}", name, check=False)
        parts = done.stdout.strip().split(":", 3) if done.returncode == 0 else []
        level = parts[3] if len(parts) == 4 else ""
        return level if _SELINUX_LEVEL.fullmatch(level) else ""

    def port(self, name: str, container_port: int) -> int:
        """The loopback host port *container_port* is published on."""

        done = self._cli("port", name, f"{container_port}/tcp")
        for line in done.stdout.splitlines():
            host, _, port = line.strip().rpartition(":")
            if host in {"127.0.0.1", "0.0.0.0", "[::]", "::"} and port.isdigit():
                return int(port)
        raise OciUnavailable(f"{name}: port {container_port} is not published")

    def pause(self, name: str) -> None:
        if self.state(name) == "running":
            self._cli("pause", name)

    def unpause(self, name: str) -> None:
        if self.state(name) == "paused":
            self._cli("unpause", name)

    def remove(self, name: str) -> None:
        """Stop and remove *name*; nothing when it is already gone."""

        # --volumes: a service image's anonymous data volume (postgres, mysql) goes with its container.
        done = self._cli("rm", "--force", "--volumes", name, check=False)
        if done.returncode != 0 and self.state(name):
            raise OciUnavailable(f"{self.runtime.engine} rm failed for {name}: {done.stderr.strip()[-300:]}")

    def containers(self, label: tuple[str, str]) -> list[str]:
        done = self._cli("ps", "--all", "--filter", f"label={label[0]}={label[1]}", "--format", "{{.Names}}")
        return [line.strip() for line in done.stdout.splitlines() if line.strip()]

    def exec(
        self,
        name: str,
        argv: Sequence[str],
        *,
        workdir: str = "",
        env: Iterable[tuple[str, str]] = (),
        stdin: bytes | None = None,
        timeout: float = 120.0,
        max_output: int = MAX_EXEC_OUTPUT_BYTES,
    ) -> ExecResult:
        """Run *argv* in the running container *name*: combined output, bounded, and a host-side deadline.

        ``env`` values travel on the engine's argv: never pass a secret here (use :meth:`write_file`).
        """

        args = [self.runtime.binary, "exec"]
        if stdin is not None:
            args.append("--interactive")
        if workdir:
            args += ["--workdir", workdir]
        args += [arg for key, value in env for arg in ("--env", f"{key}={value}")]
        args += [name, *argv]
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._engine_env,
            )
        except OSError as exc:
            raise OciUnavailable(f"{self.runtime.engine} exec could not start: {exc}") from exc
        drain = _Drain(max(1024, max_output))
        reader = threading.Thread(target=drain.feed, args=(process.stdout,), daemon=True)
        reader.start()
        if stdin is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin)
                process.stdin.close()
            except OSError:
                pass
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        reader.join(timeout=5)
        code = process.returncode if not timed_out else 124
        return ExecResult(code, drain.text(), timed_out=timed_out, truncated=drain.dropped)

    def write_file(
        self,
        name: str,
        path: str,
        payload: bytes,
        *,
        mode: str = "600",
        umask: str = "077",
        timeout: float = 30.0,
    ) -> None:
        """Write *payload* to *path* inside *name* through standard input (never argv, never a host file).

        The path is resolved inside the container, so a symbolic link there can only lead to the container's own
        files; the file is replaced atomically.
        """

        script = (
            'umask "$3"; d=$(dirname "$1"); mkdir -p "$d" && cat > "$1.lc-new" && chmod "$2" "$1.lc-new" '
            '&& mv -f "$1.lc-new" "$1"'
        )
        result = self.exec(name, ["sh", "-c", script, "sh", path, mode, umask], stdin=payload, timeout=timeout)
        if result.exit_code != 0:
            raise OciUnavailable(f"could not write {path} in {name}: {result.output.strip()[-300:]}")

    def read_file(self, name: str, path: str, *, max_bytes: int, timeout: float = 30.0) -> tuple[bytes, str] | None:
        """``(content, octal mode)`` of the regular file *path* inside *name*; ``None`` when there is none.

        Resolved inside the container, like :meth:`write_file`. A file over *max_bytes* raises.
        """

        script = 'test -f "$1" || exit 3; stat -c %a "$1" >&2 && head -c "$2" "$1"'
        try:
            done = subprocess.run(
                [self.runtime.binary, "exec", name, "sh", "-c", script, "sh", path, str(max_bytes + 1)],
                capture_output=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                env=self._engine_env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OciUnavailable(f"could not read {path} in {name}: {type(exc).__name__}") from exc
        if done.returncode == 3:
            return None
        mode = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] if done.stderr else []
        if done.returncode != 0 or not mode or not mode[0].isdigit():
            raise OciUnavailable(f"could not read {path} in {name}")
        if len(done.stdout) > max_bytes:
            raise OciUnavailable(f"{path} is larger than {max_bytes} bytes")
        return done.stdout, mode[0]


def labels_for(owner: str, **extra: str) -> tuple[tuple[str, str], ...]:
    return ((OWNER_LABEL, owner), *((f"dev.lemoncrow.{key}", value) for key, value in sorted(extra.items())))


def service_env(name: str) -> tuple[tuple[str, str], ...]:
    """What a check gets for one running service: its host name on the network."""

    return ((f"LC_SERVICE_{name.upper().replace('-', '_').replace('.', '_')}_HOST", name),)


#: Where a service started with ``capture_sql`` keeps its statement log, inside its own container.
SQL_LOG_PATH: Final[str] = "/tmp/lc-sql.log"
#: The most of one run's statement log that is read back.
MAX_SQL_LOG_BYTES: Final[int] = 2 * 1024 * 1024
_SQL_LOG_FLAGS: Final[Mapping[str, tuple[str, ...]]] = {
    "postgres": (
        "-c",
        "log_statement=all",
        "-c",
        "logging_collector=on",
        "-c",
        "log_directory=/tmp",
        "-c",
        "log_filename=lc-sql.log",
        "-c",
        "log_rotation_size=0",
        "-c",
        "log_file_mode=0644",
        "-c",
        "log_line_prefix=lc-sql ",
    ),
    "mysql": ("--general-log=1", f"--general-log-file={SQL_LOG_PATH}", "--log-output=FILE"),
}


class OciServiceBackend:
    """Runs declared services on one network, each answering to its own name, with no published port.

    With ``capture_sql`` a service that names its SQL engine logs every statement to a file in its own container;
    :meth:`sql_mark` and :meth:`sql_since` read what a run added. The log is outside the code under review, but that
    code holds a database login: one with enough privilege can turn the log off, so an empty log proves nothing.
    """

    def __init__(
        self,
        client: OciClient,
        *,
        network: str,
        prefix: str,
        labels: tuple[tuple[str, str], ...] = (),
        probe_timeout: float = 30.0,
        capture_sql: bool = False,
    ) -> None:
        self._client = client
        self._network = network
        self._prefix = prefix
        self._labels = labels
        self._probe_timeout = probe_timeout
        self._capture_sql = capture_sql

    def container_name(self, service: Service) -> str:
        return f"{self._prefix}-svc-{service.name}"

    def start(self, service: Service) -> StartedService:
        name = self.container_name(service)
        self._client.remove(name)  # a leftover of an earlier, interrupted start
        self._client.run(
            ContainerSpec(
                name=name,
                image=service.image,
                network=self._network,
                aliases=(service.name,),
                labels=self._labels,
                env=service.env,
                command=_SQL_LOG_FLAGS.get(service.sql, ()) if self._capture_sql else (),
            )
        )
        return StartedService(service.name, handle=name, env=service_env(service.name))

    def probe(self, started: StartedService, service: Service) -> bool:
        result = self._client.exec(str(started.handle), ["sh", "-c", service.ready], timeout=self._probe_timeout)
        return result.exit_code == 0

    def stop(self, started: StartedService) -> None:
        self._client.remove(str(started.handle))

    def sql_mark(self, started: StartedService) -> int:
        """The statement log's size now; a run's statements are what follows it. ``-1`` when it cannot be read."""

        try:
            done = self._client.exec(
                str(started.handle), ["sh", "-c", f"wc -c < {SQL_LOG_PATH}"], timeout=self._probe_timeout
            )
        except OciUnavailable:
            return -1
        text = done.output.strip()
        return int(text) if done.exit_code == 0 and text.isdigit() else -1

    def sql_since(self, started: StartedService, mark: int) -> str | None:
        """The statement log after *mark*, bounded; ``None`` when it cannot be read (nothing was observed)."""

        if mark < 0:
            return None
        try:
            done = self._client.exec(
                str(started.handle),
                ["sh", "-c", f"tail -c +{mark + 1} {SQL_LOG_PATH} | head -c {MAX_SQL_LOG_BYTES}"],
                timeout=self._probe_timeout,
                max_output=MAX_SQL_LOG_BYTES,
            )
        except OciUnavailable:
            return None
        return done.output if done.exit_code == 0 and not done.timed_out else None


_PG_STATEMENT: Final[re.Pattern[str]] = re.compile(
    r"^lc-sql (?:LOG|STATEMENT):\s+(?:(?:statement|execute [^:]*):\s)?(.*)$"
)
_PG_OTHER: Final[re.Pattern[str]] = re.compile(r"^lc-sql ")
_MYSQL_ENTRY: Final[re.Pattern[str]] = re.compile(
    r"^(?:\S+(?:[ ]\S+)?)?\s+\d+\s(Query|Execute|[A-Z][a-z]+(?:[ ][A-Z][a-z]+)?)(?:\t(.*))?$"
)
_SERVER_NOISE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:SET\b|SHOW\b|SELECT\s+@@|SELECT\s+(?:version|current_schema|database)\s*\(|DEALLOCATE\b|DISCARD\b)",
    re.IGNORECASE,
)


def parse_sql_log(engine: str, text: str) -> list[str]:
    """The statements in *text*, a server's own log, in order; driver housekeeping is left out."""

    statements: list[str] = []
    current: list[str] | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            statement = "\n".join(current).strip()
            if statement and not _SERVER_NOISE.match(statement):
                statements.append(statement)
        current = None

    for line in text.splitlines():
        if engine == "postgres":
            match = _PG_STATEMENT.match(line)
            if match is not None and " statement: " not in line and " execute " not in line:
                match = None  # a server message (checkpoint, startup), not a statement
            if match is not None:
                close()
                current = [match.group(1)]
            elif _PG_OTHER.match(line):
                close()
            elif current is not None and line.startswith("\t"):
                current.append(line[1:])
        else:
            match = _MYSQL_ENTRY.match(line)
            if match is not None:
                close()
                current = [match.group(2) or ""] if match.group(1) in {"Query", "Execute"} else None
            elif current is not None:
                current.append(line)
    close()
    return statements


def wait_running(client: OciClient, name: str, *, timeout: float = 30.0, interval: float = 0.25) -> None:
    """Wait until *name* is running; it exited or vanished otherwise."""

    deadline = time.monotonic() + timeout
    while True:
        state = client.state(name)
        if state == "running":
            return
        if state in {"", "exited", "dead"} or time.monotonic() >= deadline:
            raise OciUnavailable(f"container {name} is {state or 'gone'}, not running")
        time.sleep(interval)


__all__ = [
    "CONTROL_TIMEOUT_SECONDS",
    "ENGINES",
    "ENGINE_ENV",
    "KEPT_CAPABILITIES",
    "MAX_EXEC_OUTPUT_BYTES",
    "MAX_SQL_LOG_BYTES",
    "OWNER_LABEL",
    "ROOTFUL_OVERRIDE_ENV",
    "SQL_LOG_PATH",
    "ContainerSpec",
    "ExecResult",
    "Mount",
    "OciClient",
    "OciRefused",
    "OciRuntime",
    "OciServiceBackend",
    "OciUnavailable",
    "detect_runtime",
    "labels_for",
    "parse_sql_log",
    "pinned_reference",
    "refuse_socket_mounts",
    "service_env",
    "wait_running",
]
