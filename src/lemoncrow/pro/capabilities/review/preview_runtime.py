"""Run one interactive preview side in a network-less sandbox (Phase 9R item 16).

A side runs inside bubblewrap with **every namespace unshared** (no network:
only the sandbox's own loopback exists), a cleared environment, the host
runtime mounted read-only and nothing else of the host -- no home directory,
SSH or cloud credentials, Docker socket or ambient environment. It cannot
reach the host's loopback, link-local/private ranges, cloud metadata
endpoints or another preview: each side has its own network namespace and
its own socket directory.

The only way in is a Unix socket in a per-side directory bind-mounted into
the sandbox. A small trusted bridge inside the sandbox either serves a static
build from that socket (``static`` mode) or relays it to the app's loopback
port (``command`` mode). In ``command`` mode the app runs under the R4
recording sinks, so outbound HTTP/SMTP/queue/object-store attempts are
captured instead of delivered and real egress stays denied.

The same launcher serves the author machine (:class:`SandboxedPreviewDriver`)
and the hosted worker plane.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import os
import queue
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

import yaml

from lemoncrow.pro.capabilities.review.hermetic import (
    ProvisionedDependencies,
    RecordingSinks,
    offline_bwrap_prefix,
    read_sink_rows,
    runtime_image_digest,
    sinks_available,
    toolchain_versions,
)
from lemoncrow.pro.capabilities.review.preview_sessions import MAX_TTL_SECONDS, MIN_SESSION_SECONDS
from lemoncrow.pro.environment.execution import EnvironmentFingerprint, ExecutorClass, canonical_digest
from lemoncrow.pro.environment.preview_proxy import (
    PreviewResponseTooLarge,
    PreviewUpstreamError,
    PreviewUpstreamTimeout,
    ProxyRequest,
    ProxyResponse,
    safe_relative_target,
)

PreviewMode = Literal["command", "static"]
SOCKET_MOUNT: Final[str] = "/lemoncrow-preview"
SITE_MOUNT: Final[str] = "/lemoncrow-preview-site"
WORKSPACE_MOUNT: Final[str] = "/workspace"
SOCKET_NAME: Final[str] = "upstream.sock"
RELAY_UNAVAILABLE_HEADER: Final[str] = "X-LemonCrow-Preview-Relay"
DEFAULT_START_SECONDS: Final[int] = 90
MAX_START_SECONDS: Final[int] = 300
BUILD_TIMEOUT_SECONDS: Final[int] = 300
_MAX_ARGS: Final[int] = 64
_MAX_ARG_CHARS: Final[int] = 4096
_LOG_TAIL: Final[int] = 2000
_HOST_PATH: Final[str] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# In-sandbox bridge. Static mode serves the read-only build from the socket;
# command mode starts the app (under the recording-sink supervisor) and relays
# each socket connection to the app's port on the sandbox's own loopback.
_BRIDGE: Final[str] = r"""
import functools, http.server, os, posixpath, socket, socketserver, subprocess, sys, threading
threading.stack_size(256 * 1024)
SOCK = os.environ.pop("LEMONCROW_PREVIEW_SOCKET")
MODE = os.environ.pop("LEMONCROW_PREVIEW_MODE")
PORT = int(os.environ.pop("LEMONCROW_PREVIEW_PORT", "0") or 0)
SITE = os.environ.pop("LEMONCROW_PREVIEW_SITE", "")
SPA = os.environ.pop("LEMONCROW_PREVIEW_SPA", "") == "1"
class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
if MODE == "static":
    class Static(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        def address_string(self):
            return "preview"
        def log_message(self, *args):
            pass
        def send_head(self):
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if SPA and not os.path.exists(self.translate_path(path)) and not posixpath.splitext(path)[1]:
                self.path = "/index.html"
            return super().send_head()
    Server(SOCK, functools.partial(Static, directory=SITE)).serve_forever()
else:
    argv = sys.argv[sys.argv.index("--") + 1:]
    child = subprocess.Popen(argv)
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                upstream = socket.create_connection(("127.0.0.1", PORT), 5)
            except OSError:
                self.request.sendall(b"HTTP/1.0 502 Bad Gateway\r\nX-LemonCrow-Preview-Relay: unavailable\r\nContent-Length: 0\r\n\r\n")
                return
            back = threading.Thread(target=pump, args=(upstream, self.request), daemon=True)
            back.start()
            pump(self.request, upstream)
            back.join(60)
            upstream.close()
    threading.Thread(target=Server(SOCK, Relay).serve_forever, daemon=True).start()
    sys.exit(child.wait())
"""


class PreviewRecipeError(ValueError):
    """The trusted-base ``interactive_preview`` block is invalid."""


class PreviewStartFailed(RuntimeError):
    """A side could not be started; the message is safe to show (already redacted)."""


def _command(raw: object, *, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            raise PreviewRecipeError(f"interactive_preview.{field} cannot be parsed: {exc}") from exc
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        parts = [str(item) for item in raw]
    else:
        raise PreviewRecipeError(f"interactive_preview.{field} must be a string or a list")
    if len(parts) > _MAX_ARGS or any(not item or len(item) > _MAX_ARG_CHARS or "\0" in item for item in parts):
        raise PreviewRecipeError(f"interactive_preview.{field} has an empty, oversized or invalid argument")
    return tuple(parts)


def _root(raw: object) -> str:
    value = str(raw or ".").strip() or "."
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in path.parts) or "\\" in value:
        raise PreviewRecipeError("interactive_preview.root must be a relative path inside the repository")
    return path.as_posix()


@dataclass(frozen=True, slots=True)
class PreviewRecipe:
    """How one side runs, from trusted-base configuration (or inferred static build)."""

    mode: PreviewMode
    command: tuple[str, ...] = ()
    port: int = 0
    root: str = "."
    build: tuple[str, ...] = ()
    framework: str = ""
    ready_path: str = "/"
    start_seconds: int = DEFAULT_START_SECONDS
    ttl_cap_seconds: int = MAX_TTL_SECONDS
    source: str = "trusted_base"

    def to_wire(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "command": list(self.command),
            "port": self.port,
            "root": self.root,
            "build": list(self.build),
            "framework": self.framework,
            "ready_path": self.ready_path,
            "start_seconds": self.start_seconds,
            "ttl_cap_seconds": self.ttl_cap_seconds,
            "source": self.source,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> PreviewRecipe:
        mode = str(raw.get("mode") or "")
        if mode not in {"command", "static"}:
            raise PreviewRecipeError(f"unknown preview mode {mode!r}")
        return cls(
            mode=cast(PreviewMode, mode),
            command=_command(raw.get("command") or None, field="command"),
            port=int(raw.get("port") or 0),
            root=_root(raw.get("root")),
            build=_command(raw.get("build") or None, field="build"),
            framework=str(raw.get("framework") or "")[:32],
            ready_path=safe_relative_target(str(raw.get("ready_path") or "/")) or "/",
            start_seconds=max(5, min(int(raw.get("start_seconds") or DEFAULT_START_SECONDS), MAX_START_SECONDS)),
            ttl_cap_seconds=max(
                MIN_SESSION_SECONDS, min(int(raw.get("ttl_cap_seconds") or MAX_TTL_SECONDS), MAX_TTL_SECONDS)
            ),
            source=str(raw.get("source") or "trusted_base")[:64],
        )

    @property
    def digest(self) -> str:
        """Recipe identity; equal on both sides because it comes from the trusted base."""

        return canonical_digest(self.to_wire())


def load_preview_recipe(trusted_review_config: str | None) -> PreviewRecipe | None:
    """The trusted base's ``interactive_preview`` recipe, ``None`` when it declares none.

    Only trusted-base text may be passed: a head-side recipe never runs.
    """

    if not trusted_review_config or "interactive_preview" not in trusted_review_config:
        return None
    try:
        decoded = yaml.safe_load(trusted_review_config)
    except yaml.YAMLError as exc:
        raise PreviewRecipeError(f"trusted-base review config is not valid YAML: {exc}") from exc
    block = decoded.get("interactive_preview") if isinstance(decoded, Mapping) else None
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise PreviewRecipeError("interactive_preview must be a mapping")
    command = _command(block.get("command"), field="command")
    if not command:
        raise PreviewRecipeError("interactive_preview.command is required")
    try:
        port = int(block.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise PreviewRecipeError("interactive_preview.port must be an integer") from exc
    if port < 1024 or port > 65535:
        raise PreviewRecipeError("interactive_preview.port must be between 1024 and 65535")
    ready = safe_relative_target(str(block.get("ready_path") or "/"))
    if ready is None:
        raise PreviewRecipeError("interactive_preview.ready_path must be an origin-relative path")
    try:
        ttl_minutes = float(block.get("ttl_minutes") or MAX_TTL_SECONDS / 60)
        start = int(block.get("start_seconds") or DEFAULT_START_SECONDS)
    except (TypeError, ValueError) as exc:
        raise PreviewRecipeError("interactive_preview.ttl_minutes/start_seconds must be numbers") from exc
    return PreviewRecipe(
        mode="command",
        command=command,
        port=port,
        root=_root(block.get("root")),
        build=_command(block.get("build"), field="build"),
        ready_path=ready,
        start_seconds=max(5, min(start, MAX_START_SECONDS)),
        ttl_cap_seconds=max(MIN_SESSION_SECONDS, min(int(ttl_minutes * 60), MAX_TTL_SECONDS)),
    )


#: The port an environment.toml preview is given as ``$PORT`` (each preview has its own network namespace).
ENVIRONMENT_PREVIEW_PORT: Final[int] = 3000


def environment_preview_recipes(trusted_environment: str | None) -> dict[str, PreviewRecipe]:
    """Every ``[previews.<name>]`` of the trusted base's ``environment.toml``, as recipes in declaration order.

    ``run`` serves on ``$PORT`` from the repository root; ``ready`` is the path that answers
    once it is up. Empty when the file is absent or declares no previews; when it declares
    any, review.yaml ``interactive_preview`` does not apply. An invalid file, or any preview
    whose ``ready`` is not origin-relative, raises :class:`PreviewRecipeError` rather than
    falling back.
    """

    from lemoncrow_client.environment import ENVIRONMENT_FILE, DefinitionError, parse_environment

    if not trusted_environment:
        return {}
    try:
        definition = parse_environment(trusted_environment, source=ENVIRONMENT_FILE)
    except DefinitionError as exc:
        raise PreviewRecipeError(str(exc)) from exc
    recipes: dict[str, PreviewRecipe] = {}
    for preview in definition.previews:
        ready = safe_relative_target(preview.ready)
        if ready is None:
            raise PreviewRecipeError(
                f"{ENVIRONMENT_FILE}: previews.{preview.name}.ready must be an origin-relative path"
            )
        recipes[preview.name] = PreviewRecipe(
            mode="command",
            command=("sh", "-c", preview.run),
            port=ENVIRONMENT_PREVIEW_PORT,
            ready_path=ready,
            source=f"{ENVIRONMENT_FILE} previews.{preview.name}",
        )
    return recipes


def environment_preview_recipe(trusted_environment: str | None, name: str | None = None) -> PreviewRecipe | None:
    """One environment.toml preview: *name*, or the first declared; ``None`` when it declares none.

    An unknown *name* raises :class:`PreviewRecipeError`.
    """

    recipes = environment_preview_recipes(trusted_environment)
    if not recipes:
        return None
    if name is None:
        return next(iter(recipes.values()))
    if name not in recipes:
        raise PreviewRecipeError(f"no preview named {name!r}; the trusted base declares {', '.join(recipes)}")
    return recipes[name]


def static_recipe(framework: str, root: str = ".") -> PreviewRecipe:
    """A static build of a Next.js/Astro/Vite project, served read-only (local only)."""

    return PreviewRecipe(mode="static", root=_root(root), framework=framework, source="inferred_static_build")


# --------------------------------------------------------------------------- #
# Sides
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PreparedSide:
    """One materialized side, ready to run.

    ``tree`` is the static build output (static) or a disposable materialized
    workspace (command). ``owned`` trees are deleted at teardown.
    """

    side: str
    tree: Path
    lockfile_digest: str = ""
    dependencies: ProvisionedDependencies | None = None
    owned: bool = False
    #: Trusted hermetic spec (``{policy, namespace}``) for a driver that provisions remotely.
    hermetic: Mapping[str, Any] | None = None


@dataclass
class SidecarProcess:
    side: str
    process: subprocess.Popen[bytes]
    directory: Path
    socket_path: Path
    log_path: Path
    sinks: RecordingSinks | None
    tree: Path
    owned_tree: bool
    fingerprint: EnvironmentFingerprint
    started_at: float = field(default_factory=time.time)


def side_fingerprint(recipe: PreviewRecipe, prepared: PreparedSide) -> EnvironmentFingerprint:
    return EnvironmentFingerprint(
        image_digest=runtime_image_digest(),
        toolchain=toolchain_versions(("python3", "node")),
        lockfile_digest=prepared.lockfile_digest,
        recipe_digest=recipe.digest,
        sandbox="bubblewrap",
    )


def _sandbox_command(
    recipe: PreviewRecipe,
    prepared: PreparedSide,
    socket_dir: Path,
    sinks: RecordingSinks | None,
    argv: Sequence[str],
) -> list[str]:
    command = [*offline_bwrap_prefix(unshare_all=True), "--bind", str(socket_dir), SOCKET_MOUNT]
    env: dict[str, str] = {"LEMONCROW_PREVIEW_SOCKET": f"{SOCKET_MOUNT}/{SOCKET_NAME}"}
    if recipe.mode == "static":
        command.extend(("--ro-bind", str(prepared.tree), SITE_MOUNT))
        env.update(
            {
                "LEMONCROW_PREVIEW_MODE": "static",
                "LEMONCROW_PREVIEW_SITE": SITE_MOUNT,
                "LEMONCROW_PREVIEW_SPA": "1" if recipe.framework == "vite" else "0",
            }
        )
    else:
        workdir = WORKSPACE_MOUNT if recipe.root in {"", "."} else f"{WORKSPACE_MOUNT}/{recipe.root}"
        command.extend(("--bind", str(prepared.tree), WORKSPACE_MOUNT, "--chdir", workdir))
        if prepared.dependencies is not None:
            command.extend(prepared.dependencies.bwrap_args())
        if sinks is not None:
            command.extend(sinks.bwrap_args())
        env.update(
            {
                "LEMONCROW_PREVIEW_MODE": "command",
                "LEMONCROW_PREVIEW_PORT": str(recipe.port),
                "PORT": str(recipe.port),
                "HOST": "127.0.0.1",
            }
        )
    for key, value in sorted(env.items()):
        command.extend(("--setenv", key, value))
    return [*command, "--", *argv]


def _log_tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_TAIL))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _run_build(recipe: PreviewRecipe, prepared: PreparedSide, work: Path) -> None:
    """Run the trusted-base build command offline inside the same sandbox shape."""

    workdir = WORKSPACE_MOUNT if recipe.root in {"", "."} else f"{WORKSPACE_MOUNT}/{recipe.root}"
    command = [
        *offline_bwrap_prefix(unshare_all=True),
        "--bind",
        str(prepared.tree),
        WORKSPACE_MOUNT,
        "--chdir",
        workdir,
        *(prepared.dependencies.bwrap_args() if prepared.dependencies is not None else ()),
        "--",
        *recipe.build,
    ]
    log = work / "build.log"
    with log.open("wb") as output:
        try:
            proc = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=BUILD_TIMEOUT_SECONDS,
                check=False,
                env={"PATH": _HOST_PATH},
            )
        except subprocess.TimeoutExpired as exc:
            raise PreviewStartFailed(f"{prepared.side} build timed out after {BUILD_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        raise PreviewStartFailed(f"{prepared.side} build exited {proc.returncode}: {_log_tail(log)[-600:]}")


class _Spawner:
    """Fork long-lived sandboxes from one long-lived thread.

    bubblewrap's ``--die-with-parent`` is ``PR_SET_PDEATHSIG``, which fires when
    the *thread* that forked bwrap exits. Request handlers run on pool threads
    that come and go, so a side forked there would be killed under the reviewer.
    One daemon thread forks every side: sides live while the server process
    lives and still die with it.
    """

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[tuple[Callable[[], Any], concurrent.futures.Future[Any]]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _run(self) -> None:
        while True:
            work, future = self._queue.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(work())
            except BaseException as exc:
                future.set_exception(exc)

    def call(self, work: Callable[[], Any]) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="lc-preview-spawner", daemon=True)
                self._thread.start()
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._queue.put((work, future))
        return future.result()


_SPAWNER: Final[_Spawner] = _Spawner()


def launch_side(recipe: PreviewRecipe, prepared: PreparedSide, work: Path, *, sinks: bool = True) -> SidecarProcess:
    """Start one side in its own sandbox; returns once the process is spawned (not ready)."""

    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o700)
    socket_dir = work / "sock"
    socket_dir.mkdir(mode=0o700, exist_ok=True)
    if recipe.mode == "command" and recipe.build:
        _run_build(recipe, prepared, work)
    recording = RecordingSinks.create(work) if recipe.mode == "command" and sinks and sinks_available() else None
    bridge = ["python3", "-I", "-S", "-c", _BRIDGE]
    if recipe.mode == "command":
        bridge = [*bridge, "--", *(RecordingSinks.wrap(recipe.command) if recording is not None else recipe.command)]
    command = _sandbox_command(recipe, prepared, socket_dir, recording, bridge)
    log_path = work / "app.log"
    with log_path.open("wb") as output:
        process: subprocess.Popen[bytes] = _SPAWNER.call(
            lambda: subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                # bubblewrap clears the sandbox environment itself; nothing of the
                # host environment is handed even to bubblewrap.
                env={"PATH": _HOST_PATH},
            )
        )
    return SidecarProcess(
        side=prepared.side,
        process=process,
        directory=work,
        socket_path=socket_dir / SOCKET_NAME,
        log_path=log_path,
        sinks=recording,
        tree=prepared.tree,
        owned_tree=prepared.owned,
        fingerprint=side_fingerprint(recipe, prepared),
    )


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def forward_unix(socket_path: Path, request: ProxyRequest, *, max_bytes: int, timeout: float) -> ProxyResponse:
    """One HTTP exchange with a side over its socket, bounded in bytes and total time."""

    deadline = time.monotonic() + timeout
    conn = _UnixHTTPConnection(str(socket_path), timeout)
    try:
        conn.putrequest(request.method, request.target, skip_host=True, skip_accept_encoding=True)
        for name, value in request.headers:
            conn.putheader(name, value)
        conn.endheaders(request.body or None)
        response = conn.getresponse()
        chunks: list[bytes] = []
        total = 0
        while True:
            if time.monotonic() > deadline:
                raise PreviewUpstreamTimeout("preview response exceeded the time limit")
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PreviewResponseTooLarge("preview response exceeded the size limit")
            chunks.append(chunk)
        return ProxyResponse(int(response.status), tuple(response.getheaders()), b"".join(chunks))
    except TimeoutError as exc:
        raise PreviewUpstreamTimeout("preview app timed out") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise PreviewUpstreamError(f"preview app is unreachable: {type(exc).__name__}") from exc
    finally:
        conn.close()


def wait_ready(handle: SidecarProcess, recipe: PreviewRecipe, *, clock: Any = time.monotonic) -> None:
    """Wait until the side answers on its socket, or fail with the (redacted) log tail."""

    from lemoncrow.pro.capabilities.review.run_evidence import protect_text

    deadline = clock() + recipe.start_seconds
    probe = ProxyRequest("GET", recipe.ready_path, (("Host", "localhost"), ("Connection", "close")))
    while clock() < deadline:
        if handle.process.poll() is not None:
            tail = protect_text(_log_tail(handle.log_path))[0]
            raise PreviewStartFailed(f"{handle.side} exited {handle.process.returncode} while starting: {tail[-600:]}")
        if handle.socket_path.exists():
            try:
                answer = forward_unix(handle.socket_path, probe, max_bytes=1024 * 1024, timeout=5.0)
            except PreviewUpstreamError:
                answer = None
            if answer is not None and not answer.header(RELAY_UNAVAILABLE_HEADER):
                return
        time.sleep(0.1)
    raise PreviewStartFailed(f"{handle.side} did not answer within {recipe.start_seconds}s")


def stop_side(handle: SidecarProcess) -> list[dict[str, Any]]:
    """Tear one side down (process group, sockets, owned tree); return raw sink rows."""

    process = handle.process
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
    rows: list[dict[str, Any]] = []
    if handle.sinks is not None:
        rows = read_sink_rows(handle.sinks.directory)
        handle.sinks.cleanup()
    shutil.rmtree(handle.directory, ignore_errors=True)
    if handle.owned_tree:
        shutil.rmtree(handle.tree, ignore_errors=True)
    return rows


# --------------------------------------------------------------------------- #
# Driver seam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PreviewStartSpec:
    session_id: str
    review_id: str
    recipe: PreviewRecipe
    sides: tuple[PreparedSide, ...]
    deadline: float


@dataclass
class RunningPreview:
    """A started session as the driver knows it."""

    session_id: str
    fingerprints: dict[str, dict[str, Any]]
    handles: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)


class SandboxedPreviewDriver:
    """Author-machine driver: bubblewrap sides on this host (``author_machine``)."""

    def __init__(
        self,
        work_root: Path,
        *,
        run_on: str = "local",
        executor_class: ExecutorClass = ExecutorClass.AUTHOR_MACHINE,
    ) -> None:
        self.work_root = work_root
        self.run_on = run_on
        self.executor_class = executor_class

    def unavailable(self) -> str:
        if not (shutil.which("bwrap") or shutil.which("bubblewrap")):
            return "interactive previews need bubblewrap on this machine; refusing to run unsandboxed"
        return ""

    def start(self, spec: PreviewStartSpec) -> RunningPreview:
        problem = self.unavailable()
        if problem:
            raise PreviewStartFailed(problem)
        root = self.work_root / spec.session_id
        handles: dict[str, SidecarProcess] = {}
        try:
            for prepared in spec.sides:
                handles[prepared.side] = launch_side(spec.recipe, prepared, root / prepared.side)
            for handle in handles.values():
                wait_ready(handle, spec.recipe)
        except BaseException:
            for handle in handles.values():
                stop_side(handle)
            shutil.rmtree(root, ignore_errors=True)
            raise
        return RunningPreview(
            session_id=spec.session_id,
            fingerprints={side: handle.fingerprint.to_wire() for side, handle in handles.items()},
            handles=dict(handles),
        )

    def forward(
        self, running: RunningPreview, side: str, request: ProxyRequest, *, max_bytes: int, timeout: float
    ) -> ProxyResponse:
        handle = running.handles.get(side)
        if not isinstance(handle, SidecarProcess) or handle.process.poll() is not None:
            raise PreviewUpstreamError("preview side is not running")
        return forward_unix(handle.socket_path, request, max_bytes=max_bytes, timeout=timeout)

    def stop(self, running: RunningPreview) -> dict[str, list[dict[str, Any]]]:
        rows: dict[str, list[dict[str, Any]]] = {}
        for side, handle in list(running.handles.items()):
            if isinstance(handle, SidecarProcess):
                rows[side] = stop_side(handle)
        running.handles.clear()
        shutil.rmtree(self.work_root / running.session_id, ignore_errors=True)
        return rows


def lockfile_text_digest(texts: Mapping[str, str | None]) -> str:
    """Digest of the lockfiles/manifests present on one side (by name), ``""`` when none."""

    present = {name: text for name, text in sorted(texts.items()) if text is not None}
    if not present:
        return ""
    return hashlib.sha256(json.dumps(present, sort_keys=True).encode("utf-8")).hexdigest()


__all__ = [
    "BUILD_TIMEOUT_SECONDS",
    "ENVIRONMENT_PREVIEW_PORT",
    "RELAY_UNAVAILABLE_HEADER",
    "PreparedSide",
    "PreviewRecipe",
    "PreviewRecipeError",
    "PreviewStartFailed",
    "PreviewStartSpec",
    "RunningPreview",
    "SandboxedPreviewDriver",
    "SidecarProcess",
    "environment_preview_recipe",
    "environment_preview_recipes",
    "forward_unix",
    "launch_side",
    "load_preview_recipe",
    "lockfile_text_digest",
    "side_fingerprint",
    "static_recipe",
    "stop_side",
    "wait_ready",
]
