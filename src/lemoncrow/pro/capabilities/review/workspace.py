"""Legacy per-repository Review workspace support, retained for safe cleanup.

The supported Review architecture no longer starts or adopts a listener from
this module. ``serve_workspace`` and ``ensure_workspace`` are hard retirement
guards; registration/probe helpers remain so old installations can be detected
and cleaned up safely during the migration.


``api.py`` owns the routes; this module owns everything that makes them
reachable, and every one of those decisions is a security decision because the
thing behind the routes is a private repository (spec SS8).

The four that matter, and why each is shaped the way it is:

**Bind.** ``127.0.0.1`` only. There is no ``--host`` and the environment
override exists solely so the refusal can be exercised: any other host raises
:class:`WorkspaceBindRefused` and the process exits non-zero. It does not warn
and carry on -- ``core/service/api.py``'s ``main()`` warns, and a warning nobody
reads is how a repository ends up on a LAN. Port 0 is bound and the **socket is
kept**, then handed to ``uvicorn.Server.run(sockets=[sock])``; discovering a
free port and rebinding it later is a race another local process can win.

**Token.** ``secrets.token_urlsafe(32)``, minted once per process, checked with
``compare_digest`` on every route but ``/healthz``. It travels to the browser in
the URL *fragment*, which is never sent to a server, never lands in a proxy log
and never enters ``Referer``; the page strips it with ``history.replaceState``
and keeps it in a module variable. A loopback bind is not an auth boundary --
every local process and every browser tab shares it.

**Registration.** ``0600``, written atomically, owner-guarded, and keyed by the
repository it serves. A registration whose pid is dead reads as absent. Health
means *the token still works*, not that the pid lives: a stale file naming a
token the live process rejects would otherwise be adopted forever.

**Bundle.** The browser assets are served from the built frontend directory and
nothing else. Pointing a static server at a repository turns it into a
file-read primitive, so the mount is confined to the bundle and the SPA
fallback answers exactly one route.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOOPBACK_HOST = "127.0.0.1"
HOST_ENV = "LEMONCROW_REVIEW_HOST"
BUNDLE_ENV = "LEMONCROW_REVIEW_BUNDLE_DIR"
IDLE_TIMEOUT_ENV = "LEMONCROW_REVIEW_WORKSPACE_IDLE_SECONDS"
WORKSPACE_ROUTE = "/review"
REVIEWS_ROUTE = "/reviews"
_REGISTRATION_DIRNAME = "workspaces"
_PREPARATION_DIRNAME = "preparations"
_PREPARATION_STALE_SECONDS = 300.0
_SPAWN_TIMEOUT_SECONDS = 25.0
_DEFAULT_IDLE_TIMEOUT_SECONDS = 4 * 60 * 60.0
_MIN_IDLE_TIMEOUT_SECONDS = 30.0


class WorkspaceBindRefused(RuntimeError):
    """The workspace was asked to bind something other than loopback.

    A distinct type rather than a bare ``ValueError`` so the CLI can turn it
    into a non-zero exit with the host named, and so a test can assert that the
    refusal is a refusal rather than a warning.
    """


@dataclass(frozen=True)
class WorkspaceHandle:
    """A live workspace, as recorded in its registration file."""

    pid: int
    url: str
    token: str
    started_at: float
    repo_root: str = ""
    generation: str = ""

    @property
    def review_url(self) -> str:
        """Where the browser goes, before the review id is appended."""

        return f"{self.url}{WORKSPACE_ROUTE}"


@dataclass(slots=True)
class _WorkspaceActivity:
    """Thread-safe last-request clock used only by the local idle reaper."""

    last_seen: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def touch(self) -> None:
        with self._lock:
            self.last_seen = time.monotonic()

    def idle_for(self) -> float:
        with self._lock:
            return max(0.0, time.monotonic() - self.last_seen)


def bootstrap_url(handle: WorkspaceHandle, review_id: str, *, preparation_id: str = "") -> str:
    """The one URL a human is ever given.

    The token rides in the fragment and the review id rides with it: a fragment
    is never transmitted to the server, so neither the access log, the proxy nor
    ``Referer`` ever sees the credential. The page strips the whole fragment on
    its first render.
    """

    suffix = f"&p={preparation_id}" if preparation_id else ""
    return f"{handle.url}{REVIEWS_ROUTE}/{review_id}#t={handle.token}&r={review_id}{suffix}"


# --------------------------------------------------------------------------- #
# bind
# --------------------------------------------------------------------------- #


def bind_loopback(host: str = LOOPBACK_HOST) -> socket.socket:
    """Bind an ephemeral loopback port and **keep** the socket.

    Refuses any host but ``127.0.0.1``. ``0.0.0.0`` and ``::`` are not
    "permissive" here, they are "publish a private repository on every
    interface", so they raise rather than warn.

    The listening socket is returned still open, and uvicorn is handed it
    directly. The alternative -- bind port 0, read the port, close, rebind --
    is the documented TOCTOU race in ``coding_engine._free_loopback_port`` that
    ``oauth_flow.py`` exists to avoid.
    """

    if host != LOOPBACK_HOST:
        raise WorkspaceBindRefused(
            f"the review workspace binds {LOOPBACK_HOST} only; refusing {host!r}. "
            "It serves a private repository and a loopback bind is the only boundary it has."
        )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK_HOST, 0))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    return sock


def requested_host() -> str:
    """The host the environment asks for; loopback unless overridden."""

    return os.environ.get(HOST_ENV, "") or LOOPBACK_HOST


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


def registration_path(store_root: Path, repo_root: Path) -> Path:
    """Where this repository's workspace registration lives.

    Keyed by a hash of the resolved repository path, mirroring the MCP daemon's
    per-workspace registration. The spec named a single ``workspace.json``; one
    file per store would make a second repository either adopt a workspace bound
    to a different repo_root or silently overwrite its registration, and the
    first of those two serves the wrong repository.
    """

    digest = hashlib.sha256(str(repo_root.expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    return store_root / "review" / _REGISTRATION_DIRNAME / f"workspace-{digest}.json"


def preparation_path(store_root: Path, review_id: str) -> Path:
    digest = hashlib.sha256(review_id.encode("utf-8")).hexdigest()[:20]
    return store_root / "review" / _PREPARATION_DIRNAME / f"prepare-{digest}.json"


def write_review_preparation(
    store_root: Path,
    review_id: str,
    preparation_id: str,
    stage: str,
    *,
    detail: str = "",
) -> None:
    """Publish one invocation's source-capture progress for the waiting Reader."""

    path = preparation_path(store_root, review_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "review_id": review_id,
        "preparation_id": preparation_id,
        "stage": stage,
        "detail": detail,
        "updated_at": time.time(),
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(OSError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, separators=(",", ":")))
    tmp.replace(path)


def read_review_preparation(store_root: Path, review_id: str, preparation_id: str) -> dict[str, Any] | None:
    """Read only the requested invocation; stale or unrelated markers are ignored."""

    path = preparation_path(store_root, review_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("preparation_id") != preparation_id:
        return None
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, (int, float)) or time.time() - float(updated_at) > _PREPARATION_STALE_SECONDS:
        return {
            "review_id": review_id,
            "preparation_id": preparation_id,
            "stage": "failed",
            "detail": "review preparation stopped before the first revision became ready",
            "updated_at": float(updated_at) if isinstance(updated_at, (int, float)) else 0.0,
        }
    return data


def clear_review_preparation(store_root: Path, review_id: str, preparation_id: str) -> None:
    path = preparation_path(store_root, review_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("preparation_id") == preparation_id:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remove_stale_registration(path: Path, observed: str) -> None:
    """Remove stale metadata only if nobody replaced it after our read."""

    try:
        if path.read_text(encoding="utf-8") == observed:
            path.unlink(missing_ok=True)
    except OSError:
        return


def read_registration(store_root: Path, repo_root: Path) -> WorkspaceHandle | None:
    """The recorded workspace for this repository, or ``None``.

    Missing metadata reads as absent. Corrupt metadata and entries naming dead
    pids are removed opportunistically, but only when the file is still exactly
    what this reader observed.
    """

    path = registration_path(store_root, repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        _remove_stale_registration(path, raw)
        return None
    if not isinstance(data, dict):
        _remove_stale_registration(path, raw)
        return None
    pid = data.get("pid")
    url = data.get("url")
    token = data.get("token")
    if not isinstance(pid, int) or not isinstance(url, str) or not isinstance(token, str):
        _remove_stale_registration(path, raw)
        return None
    if not url or not token or not _pid_alive(pid):
        _remove_stale_registration(path, raw)
        return None
    started = data.get("started_at")
    return WorkspaceHandle(
        pid=pid,
        url=url,
        token=token,
        started_at=float(started) if isinstance(started, (int, float)) else 0.0,
        repo_root=str(data.get("repo_root") or ""),
        generation=str(data.get("generation") or ""),
    )


def write_registration(store_root: Path, repo_root: Path, handle: WorkspaceHandle) -> Path:
    """Publish *handle* atomically, owner-only.

    The token is a bearer credential at rest, so the file is ``0600`` under a
    store root that already ignores its own contents. Written tmp-then-replace:
    a torn write would hand the next reader a truncated JSON document and a
    workspace it cannot reach.
    """

    path = registration_path(store_root, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": handle.pid,
        "url": handle.url,
        "token": handle.token,
        "repo_root": handle.repo_root,
        "started_at": handle.started_at,
        "generation": handle.generation,
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    # 0600 at creation, not chmod'ed to 0600 afterwards: the token is in the
    # very first byte written, so a temp file born at the process umask (0644
    # under the usual 022) publishes the bearer credential to every local user
    # for the width of that window -- and the containing directory is
    # traversable. O_EXCL over an unlinked name also refuses a pre-planted temp.
    with contextlib.suppress(OSError):
        tmp.unlink()
    handle_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle_fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2))
    tmp.replace(path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


def remove_registration(store_root: Path, repo_root: Path) -> None:
    """Delete the registration -- but only while it still names this process."""

    path = registration_path(store_root, repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


def _opener() -> Any:
    """A URL opener that ignores ambient proxy environment.

    ``HTTP_PROXY``/``ALL_PROXY`` apply to real host:port URLs, so a proxied
    shell would route this loopback probe through a third party -- carrying the
    bearer token with it.
    """

    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe_healthy(handle: WorkspaceHandle, *, timeout: float = 2.0) -> bool:
    """True when the workspace answers **and** accepts the recorded token.

    Liveness alone is not health. ``/healthz`` is unauthenticated and answers
    200 for any live process on that port, including a workspace that was
    restarted with a fresh token while a stale registration still names the old
    one. Adopting that one means every authenticated call 403s forever, so the
    probe spends a second, authenticated request.
    """

    opener = _opener()
    host = f"{LOOPBACK_HOST}:{handle.url.rsplit(':', 1)[-1]}"
    try:
        request = urllib.request.Request(f"{handle.url}/healthz", headers={"Host": host})
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return False
        authed = urllib.request.Request(
            f"{handle.url}/api/reviews",
            headers={"Authorization": f"Bearer {handle.token}", "Host": host},
        )
        with opener.open(authed, timeout=timeout) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# bundle
# --------------------------------------------------------------------------- #


def _checkout_bundle() -> Path:
    """``frontend/dist`` of the checkout *this module was imported from*.

    This is the bundle built from the same source tree as the API it will talk
    to, so it is the only candidate guaranteed to carry the review route. In an
    installed wheel the path simply does not exist and the caller moves on.
    """

    # <repo>/src/lemoncrow/pro/capabilities/review/workspace.py -> <repo>
    return Path(__file__).resolve().parents[5] / "frontend" / "dist"


def bundle_dir() -> Path | None:
    """The built frontend bundle, or ``None`` when there is not one.

    ``None`` is an expected outcome, not a failure: ``pip install lemoncrow``
    ships no ``frontend/dist`` (the wheel's ``force-include`` carries two data
    files and ``integrations/``), so the CLI must fall back to the static HTML
    report rather than crash-loop -- which is exactly what happened once before.

    Order matters and is not arbitrary. ``frontend_dir`` resolves to the
    installed frontend bundle (``~/.lemoncrow/install/frontend``), which is a released
    dashboard bundle that predates -- and therefore does not contain -- this
    checkout's ``/review`` route. Serving it renders dashboard chrome around an
    empty pane: a blank page that looks like a broken workspace rather than an
    old bundle. The checkout's own ``frontend/dist`` is checked first because it
    is the one built from the same source as the API it is talking to.
    """

    from lemoncrow.infra.runtime.frontend_bundle import frontend_dir

    candidates: list[Path] = []
    override = os.environ.get(BUNDLE_ENV)
    if override:
        candidates.append(Path(override))
    candidates.append(_checkout_bundle())
    frontend = frontend_dir()
    candidates.append(frontend / "dist")
    candidates.append(frontend)
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def workspace_generation() -> str:
    """Fingerprint the review backend and built browser bundle this process serves."""

    digest = hashlib.sha256()
    source_root = Path(__file__).resolve().parents[3]
    review_root = Path(__file__).resolve().parent
    # ``.so``/``.pyd`` as well as ``.py``: in a mypyc release wheel almost every
    # module in this package ships compiled and its ``.py`` is gone, so a
    # ``*.py`` glob fingerprints a handful of survivors and reports the same
    # generation across a patch release that rewrote the whole review backend --
    # which makes an upgraded CLI adopt the pre-upgrade workspace process.
    # rglob, not glob: `review/sources/` holds `local` -- open_or_create_session,
    # record_revision, refresh, range_for_session, the whole reconciliation --
    # and a non-recursive pattern left a patch confined to that module invisible
    # here, so an upgraded CLI adopted the pre-upgrade workspace process.
    source_files = sorted(path for pattern in ("*.py", "*.so", "*.pyd") for path in review_root.rglob(pattern))
    command_root = source_root / "gateway" / "cli" / "commands"
    source_files.append(command_root / "review.py")
    source_files.extend(sorted(command_root.glob("review.*.so")))
    source_files.extend(sorted(command_root.glob("review.*.pyd")))
    for path in source_files:
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"<missing>")

    bundle = bundle_dir()
    if bundle is None:
        digest.update(b"bundle:none")
    else:
        bundle_files = [bundle / "index.html"]
        assets = bundle / "assets"
        if assets.is_dir():
            # Every emitted asset, not a hand-written list of chunk names. This
            # globbed `ReviewWorkspace-*` until that component was split into
            # `ReviewReader`/`ReviewStream`; the pattern then matched nothing,
            # and the digest stopped moving when the reader bundle did, so an
            # upgraded CLI adopted a workspace still serving the old reader.
            # Vite names each chunk after the module it was split from, so any
            # pattern narrower than "all of them" rots at the next rename --
            # and hashing all 13 MB of them costs under 10 ms.
            bundle_files.extend(sorted(assets.rglob("*")))
        for path in bundle_files:
            if not path.is_file():
                continue
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(hashlib.sha256(path.read_bytes()).digest())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()[:20]


# --------------------------------------------------------------------------- #
# the server
# --------------------------------------------------------------------------- #


def _workspace_idle_seconds() -> float:
    """Configured local workspace idle lifetime, bounded away from churn."""

    raw = os.environ.get(IDLE_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_IDLE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_IDLE_TIMEOUT_SECONDS
    return max(_MIN_IDLE_TIMEOUT_SECONDS, value)


def _idle_reaper(
    server: Any,
    activity: _WorkspaceActivity,
    stop: threading.Event,
    *,
    idle_seconds: float,
    check_interval: float | None = None,
) -> None:
    """Ask uvicorn to exit after a local workspace has been unused long enough."""

    interval = check_interval if check_interval is not None else min(30.0, max(1.0, idle_seconds / 8.0))
    while not stop.wait(interval):
        if activity.idle_for() >= idle_seconds:
            server.should_exit = True
            return


def build_app(
    store_root: Path,
    repo_root: Path,
    *,
    token: str,
    port: int,
    touch: Callable[[], None] | None = None,
) -> Any:
    """The FastAPI app this workspace serves: review API plus the bundle.

    Separated from :func:`serve_workspace` so the whole surface -- including
    every refusal -- is reachable from ``TestClient`` without binding a port.
    """

    from fastapi import FastAPI
    from fastapi.responses import FileResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles

    from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    store = ReviewStore(store_root)
    app = FastAPI(title="LemonCrow review workspace", docs_url=None, redoc_url=None, openapi_url=None)
    if touch is not None:

        @app.middleware("http")
        async def _record_activity(request: Any, call_next: Any) -> Any:
            touch()
            return await call_next(request)

    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(token),
        repo_root=repo_root,
        port=port,
    )

    bundle = bundle_dir()
    if bundle is not None:
        index = bundle / "index.html"

        @app.get(WORKSPACE_ROUTE)
        @app.get(REVIEWS_ROUTE)
        @app.get(f"{REVIEWS_ROUTE}/{{review_path:path}}")
        async def _workspace_page(review_path: str = "") -> Any:
            # Review management routes are explicit SPA entry points.  Do not
            # install a global catch-all: an arbitrary typo still stays a 404
            # rather than being disguised as frontend HTML.
            return FileResponse(index, media_type="text/html")

        @app.get("/")
        async def _root() -> Any:
            return RedirectResponse(REVIEWS_ROUTE)

        # Confined to the bundle directory. Anything outside it -- the store,
        # the repository -- is unreachable through this mount by construction.
        app.mount("/", StaticFiles(directory=str(bundle)), name="bundle")
    return app


def serve_workspace(store_root: Path, repo_root: Path) -> int:
    """Retired compatibility entrypoint; Review uses the configured server."""

    del store_root, repo_root
    raise RuntimeError("the per-repository Review workspace server is retired; use LEMONCROW_URL")


def _retire_workspace(store_root: Path, repo_root: Path, handle: WorkspaceHandle) -> None:
    """Retire a stale registered process without deleting a newer registration."""

    if handle.pid > 0 and handle.pid != os.getpid():
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(handle.pid, signal.SIGTERM)
    path = registration_path(store_root, repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("pid") == handle.pid:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def retire_legacy_workspaces(store_root: Path) -> int:
    """Stop healthy legacy per-repository Review listeners and remove stale registrations.

    This is a one-way migration helper for the single-server architecture.
    A PID is signalled only after the registered bearer token successfully
    authenticates against its Review API, which avoids killing an unrelated
    process after PID reuse. Unhealthy/dead registrations are simply removed.
    """

    root = store_root / "review" / _REGISTRATION_DIRNAME
    if not root.is_dir():
        return 0
    retired = 0
    for path in root.glob("workspace-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            continue
        if not isinstance(data, dict):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            continue
        pid = data.get("pid")
        url = data.get("url")
        token = data.get("token")
        if isinstance(pid, int) and isinstance(url, str) and isinstance(token, str) and pid > 0:
            handle = WorkspaceHandle(
                pid=pid,
                url=url,
                token=token,
                started_at=float(data.get("started_at") or 0.0),
                repo_root=str(data.get("repo_root") or ""),
                generation=str(data.get("generation") or ""),
            )
            if _pid_alive(pid) and probe_healthy(handle, timeout=0.25):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(pid, signal.SIGTERM)
                retired += 1
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
    return retired


def ensure_workspace(
    store_root: Path,
    repo_root: Path,
    *,
    timeout: float = _SPAWN_TIMEOUT_SECONDS,
) -> WorkspaceHandle:
    """Retired compatibility helper; never starts or adopts a listener."""

    del store_root, repo_root, timeout
    raise RuntimeError("the per-repository Review workspace server is retired; use LEMONCROW_URL")


__all__ = [
    "BUNDLE_ENV",
    "HOST_ENV",
    "LOOPBACK_HOST",
    "WORKSPACE_ROUTE",
    "WorkspaceBindRefused",
    "WorkspaceHandle",
    "bind_loopback",
    "bootstrap_url",
    "build_app",
    "bundle_dir",
    "ensure_workspace",
    "probe_healthy",
    "read_registration",
    "registration_path",
    "remove_registration",
    "requested_host",
    "retire_legacy_workspaces",
    "serve_workspace",
    "workspace_generation",
    "write_registration",
]
