"""The review workspace process: bind, mint, register, serve.

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
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOOPBACK_HOST = "127.0.0.1"
HOST_ENV = "LEMONCROW_REVIEW_HOST"
BUNDLE_ENV = "LEMONCROW_REVIEW_BUNDLE_DIR"
WORKSPACE_ROUTE = "/review"
_REGISTRATION_DIRNAME = "workspaces"
_SPAWN_TIMEOUT_SECONDS = 25.0


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


def bootstrap_url(handle: WorkspaceHandle, review_id: str) -> str:
    """The one URL a human is ever given.

    The token rides in the fragment and the review id rides with it: a fragment
    is never transmitted to the server, so neither the access log, the proxy nor
    ``Referer`` ever sees the credential. The page strips the whole fragment on
    its first render.
    """

    return f"{handle.review_url}#t={handle.token}&r={review_id}"


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


def read_registration(store_root: Path, repo_root: Path) -> WorkspaceHandle | None:
    """The recorded workspace for this repository, or ``None``.

    ``None`` for a missing file, a corrupt one, or one naming a dead pid. A
    crash without cleanup must read as "there is no workspace", so the caller
    spawns instead of dialling a port nobody is listening on.
    """

    path = registration_path(store_root, repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    url = data.get("url")
    token = data.get("token")
    if not isinstance(pid, int) or not isinstance(url, str) or not isinstance(token, str):
        return None
    if not url or not token or not _pid_alive(pid):
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

    Order matters and is not arbitrary. ``_stack_frontend_dir`` resolves to the
    *installed* stack (``~/.lemoncrow/install/frontend``), which is a released
    dashboard bundle that predates -- and therefore does not contain -- this
    checkout's ``/review`` route. Serving it renders dashboard chrome around an
    empty pane: a blank page that looks like a broken workspace rather than an
    old bundle. The checkout's own ``frontend/dist`` is checked first because it
    is the one built from the same source as the API it is talking to.
    """

    from lemoncrow.infra.runtime.stack_lifecycle import _stack_frontend_dir

    candidates: list[Path] = []
    override = os.environ.get(BUNDLE_ENV)
    if override:
        candidates.append(Path(override))
    candidates.append(_checkout_bundle())
    frontend = _stack_frontend_dir()
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


def build_app(store_root: Path, repo_root: Path, *, token: str, port: int) -> Any:
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
        async def _workspace_page() -> Any:
            # The SPA entry point, answered explicitly. A catch-all fallback
            # would make every unmatched path return HTML, which hides typos and
            # widens what this process is willing to serve.
            return FileResponse(index, media_type="text/html")

        @app.get("/")
        async def _root() -> Any:
            return RedirectResponse(WORKSPACE_ROUTE)

        # Confined to the bundle directory. Anything outside it -- the store,
        # the repository -- is unreachable through this mount by construction.
        app.mount("/", StaticFiles(directory=str(bundle)), name="bundle")
    return app


def serve_workspace(store_root: Path, repo_root: Path) -> int:
    """Run the workspace in this process until it is stopped."""

    import uvicorn

    host = requested_host()
    sock = bind_loopback(host)  # raises WorkspaceBindRefused for anything else
    port = int(sock.getsockname()[1])
    token = secrets.token_urlsafe(32)
    resolved_repo = repo_root.expanduser().resolve()
    handle = WorkspaceHandle(
        pid=os.getpid(),
        url=f"http://{LOOPBACK_HOST}:{port}",
        token=token,
        started_at=time.time(),
        repo_root=str(resolved_repo),
        generation=workspace_generation(),
    )
    app = build_app(store_root, resolved_repo, token=token, port=port)
    write_registration(store_root, resolved_repo, handle)
    config = uvicorn.Config(app, log_level="warning", timeout_keep_alive=30)
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        remove_registration(store_root, resolved_repo)
        with contextlib.suppress(OSError):
            sock.close()
    return 0


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


def ensure_workspace(
    store_root: Path,
    repo_root: Path,
    *,
    timeout: float = _SPAWN_TIMEOUT_SECONDS,
) -> WorkspaceHandle:
    """Adopt the current-generation workspace, or replace/start one.

    A working token proves liveness and authorization, but not that the process
    serves the review code currently on disk. A generation mismatch therefore
    retires the old process before spawning its replacement.
    """

    resolved_repo = repo_root.expanduser().resolve()
    expected_generation = workspace_generation()
    existing = read_registration(store_root, resolved_repo)
    if existing is not None and probe_healthy(existing):
        if existing.generation == expected_generation:
            return existing
        _retire_workspace(store_root, resolved_repo, existing)

    command = [
        sys.executable,
        # Keep the repository as cwd for git resolution, but never make it a
        # Python import root. Safe-path mode blocks a repo-root module/package
        # from shadowing LemonCrow or any lazily imported dependency.
        "-P",
        "-m",
        "lemoncrow.gateway.cli",
        "--root",
        str(store_root),
        "review",
        "--repo-root",
        str(resolved_repo),
        "--serve-workspace",
    ]
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(store_root)
    log_path = store_root / "review" / "workspace.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
            cwd=str(resolved_repo),
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handle = read_registration(store_root, resolved_repo)
        if handle is not None and handle.generation == expected_generation and probe_healthy(handle, timeout=1.0):
            return handle
        time.sleep(0.2)
    raise RuntimeError(f"the review workspace did not become healthy within {timeout:.0f}s; see {log_path}")


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
    "serve_workspace",
    "workspace_generation",
    "write_registration",
]
