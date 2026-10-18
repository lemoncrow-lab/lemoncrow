"""Local Review UI/API mounted inside the one LemonCrow loopback server.

The public Review implementation is FastAPI-based while the thin-client server
is aiohttp. Local mode must not grow a second listener just to host Review, so
this module adapts the ASGI Review app in-process behind aiohttp routes.

Security posture:
- enabled only by explicit ServerConfig.local_review_root;
- configuration is refused off loopback;
- HTML/assets are public on the loopback listener, but remain guarded against
  Host/Origin rebinding;
- the machine-authenticated CLI arms a short browser pairing window; the
  browser claims a Review-only capability bound to the exact loopback origin;
  /api requests cross either that capability gate or the machine bearer gate;
- a Review API app is scoped to the exact repo_root recorded on the durable
  ReviewSession, preserving old per-repository confinement without one process
  per repository.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from aiohttp import web
from multidict import CIMultiDict

from lemoncrow.pro.capabilities.review.api import SECURITY_HEADERS, origin_refusal, register_review_api
from lemoncrow.pro.capabilities.review.hosted_attachment import attached_identity
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.workspace import bundle_dir

from .read_budget import ReadBudget, read_budget

__all__ = ["LOCAL_REVIEW_APP_KEY", "LocalReviewSurface"]

_ROOT_PUBLIC_ASSETS = frozenset(
    {
        "favicon.svg",
        "lemoncrow-card.png",
        "lemoncrow-card.svg",
        "site.webmanifest",
        "robots.txt",
    }
)

#: Vite copies ``frontend/public/**`` into the bundle root verbatim, so the SPA
#: references them as root-relative URLs. Names outside this list are never
#: served: the bundle root may also hold whatever else a build dropped there,
#: and only an explicit allowlist keeps that from becoming a file server.
#: ``logos/hosts/*`` is the host-mark set the console renders per host row; a
#: host with no entry under it falls back to the generic ◌ tile.
_ROOT_PUBLIC_PREFIXES: tuple[str, ...] = ("logos/",)

#: Vite content-hashes every /assets file name, so changed bytes are a new URL.
#: no-store made every load re-download the bundle and re-compile it (V8 only
#: reuses compiled code for HTTP-cached scripts); index.html stays no-store and
#: names the current set.
_IMMUTABLE_ASSET_CACHE = "public, max-age=31536000, immutable"

_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailers",
        b"transfer-encoding",
        b"upgrade",
    }
)


class LocalReviewSurface:
    """One local Review surface for every repository known to one server."""

    def __init__(
        self, store_root: Path, *, port: int, frontend_dir: Path | None = None, landing: str = "/home"
    ) -> None:
        self.store_root = Path(store_root).expanduser().resolve()
        self.port = int(port)
        #: Where ``/`` lands: the Dashboard home, or Reviews when only Review runs.
        self.landing = landing
        self.store = ReviewStore(self.store_root)
        configured = None if frontend_dir is None else Path(frontend_dir).expanduser().resolve()
        self.bundle = configured if configured is not None and (configured / "index.html").is_file() else bundle_dir()
        self.bundle = configured if configured is not None and (configured / "index.html").is_file() else bundle_dir()
        self._apps: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        return self.bundle is not None

    def is_console_path(self, path: str) -> bool:
        if path == "/reviews":
            return True
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in (
                "/home",
                "/runs",
                "/code",
                "/knowledge",
                "/usage",
                "/settings",
                "/system",
            )
        )

    def is_ui_path(self, path: str) -> bool:
        return (
            path == "/"
            or path == "/review"
            or self.is_console_path(path)
            or path.startswith("/reviews/")
            or path.startswith("/r/")
            or path.startswith("/rr/")
        )

    def is_public_path(self, path: str) -> bool:
        return self.is_ui_path(path) or path.startswith("/assets/") or self.is_public_root_asset(path)

    @staticmethod
    def is_public_root_asset(path: str) -> bool:
        name = path.lstrip("/")
        return name in _ROOT_PUBLIC_ASSETS or name.startswith(_ROOT_PUBLIC_PREFIXES)

    def public_asset_route_defs(self) -> tuple[web.RouteDef, ...]:
        """Bundle-root files the SPA reads as root-relative URLs.

        Both the local server and the standalone gateway app wire routes from
        here: the allowlist is long enough that a second hand-maintained copy is
        how an allowlisted asset ends up served by one app and 404ing on the
        other.
        """

        return (
            web.get("/favicon.svg", self.root_asset),
            web.get("/lemoncrow-card.png", self.root_asset),
            web.get("/lemoncrow-card.svg", self.root_asset),
            web.get("/site.webmanifest", self.root_asset),
            web.get("/robots.txt", self.root_asset),
            *(web.get(f"/{prefix}{{tail:.*}}", self.root_asset) for prefix in _ROOT_PUBLIC_PREFIXES),
        )

    def route_defs(self) -> tuple[web.RouteDef, ...]:
        if not self.available:
            return ()
        return (
            web.get("/", self.home),
            web.get("/review", self.ui),
            web.get("/home", self.ui),
            web.get("/runs", self.ui),
            web.get("/runs/{tail:.*}", self.ui),
            web.get("/code", self.ui),
            web.get("/knowledge", self.ui),
            web.get("/knowledge/{tail:.*}", self.ui),
            web.get("/usage", self.ui),
            web.get("/reviews", self.ui),
            web.get("/settings", self.ui),
            web.get("/settings/{tail:.*}", self.ui),
            web.get("/system", self.ui),
            web.get("/system/{tail:.*}", self.ui),
            web.get("/reviews/{tail:.*}", self.ui),
            web.get("/r/{tail:.*}", self.ui),
            web.get("/rr/{tail:.*}", self.ui),
            web.get("/assets/{tail:.*}", self.asset),
            *self.public_asset_route_defs(),
            web.get("/api/reviews", self.api_reviews),
            web.route("*", "/api/{tail:.*}", self.api),
        )

    def _guard_refusal(self, request: web.Request) -> str:
        expected_port = self.port or request.url.port or (443 if request.scheme == "https" else 80)
        return origin_refusal(
            host=request.headers.get("host", ""),
            origin=request.headers.get("origin", ""),
            referer=request.headers.get("referer", ""),
            port=expected_port,
        )

    @staticmethod
    def _security_headers() -> dict[str, str]:
        return dict(SECURITY_HEADERS)

    async def home(self, request: web.Request) -> web.StreamResponse:
        refusal = self._guard_refusal(request)
        if refusal:
            return web.json_response({"detail": refusal}, status=403, headers=self._security_headers())
        raise web.HTTPFound(self.landing, headers=self._security_headers())

    async def ui(self, request: web.Request) -> web.StreamResponse:
        refusal = self._guard_refusal(request)
        if refusal:
            return web.json_response({"detail": refusal}, status=403, headers=self._security_headers())
        if self.bundle is None:
            raise web.HTTPNotFound()
        return web.FileResponse(self.bundle / "index.html", headers=self._security_headers())

    async def asset(self, request: web.Request) -> web.StreamResponse:
        refusal = self._guard_refusal(request)
        if refusal:
            return web.json_response({"detail": refusal}, status=403, headers=self._security_headers())
        if self.bundle is None:
            raise web.HTTPNotFound()
        root = (self.bundle / "assets").resolve()
        candidate = (root / request.match_info.get("tail", "")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise web.HTTPNotFound() from exc
        if not candidate.is_file():
            raise web.HTTPNotFound()
        headers = self._security_headers()
        headers["Cache-Control"] = _IMMUTABLE_ASSET_CACHE
        return web.FileResponse(candidate, headers=headers)

    async def root_asset(self, request: web.Request) -> web.StreamResponse:
        refusal = self._guard_refusal(request)
        if refusal:
            return web.json_response({"detail": refusal}, status=403, headers=self._security_headers())
        if self.bundle is None:
            raise web.HTTPNotFound()
        name = request.path.lstrip("/")
        if not self.is_public_root_asset(name):
            raise web.HTTPNotFound()
        candidate = (self.bundle / name).resolve()
        try:
            candidate.relative_to(self.bundle.resolve())
        except ValueError as exc:
            raise web.HTTPNotFound() from exc
        if not candidate.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(candidate, headers=self._security_headers())

    async def api_reviews(self, request: web.Request) -> web.StreamResponse:
        refusal = self._guard_refusal(request)
        if refusal:
            return web.json_response({"detail": refusal}, status=403, headers=self._security_headers())
        status_filter = request.query.get("status_filter", "open")
        if status_filter not in {"open", "finished", "archived", "all"}:
            return web.json_response(
                {"detail": "status_filter must be open, finished, archived, or all"},
                status=422,
                headers=self._security_headers(),
            )
        sessions = self.store.list_sessions(status="" if status_filter == "all" else status_filter, limit=500)
        # One identity (G5-SYNC, RC5): an attached Review is named by its hosted Change.
        attachments = self.store.hosted_attachments() if self.store.hosted_attachment_supported() else {}
        rows: list[dict[str, Any]] = []
        for session in sessions:
            revision = self.store.latest_revision(session.id)
            annotations = self.store.list_annotations(session.id)
            attachment = attachments.get(session.id)
            identity = (
                {}
                if attachment is None
                else attached_identity(
                    local_review_id=session.id,
                    origin=attachment.origin,
                    hosted_review_id=attachment.hosted_review_id,
                )
            )
            rows.append(
                {
                    "id": identity.get("id", session.id),
                    "ref": identity.get("ref", f"r/{session.id}"),
                    **({"hosted": identity["hosted"]} if identity else {}),
                    "title": session.title,
                    "subject_type": session.subject_type,
                    "range_mode": session.range_mode,
                    "source_ref": session.source_ref,
                    "status": session.status,
                    "actor_type": session.actor_type,
                    "reviewer_id": session.reviewer_id,
                    "repo_root": session.repo_root,
                    "updated_at": session.updated_at,
                    "revision_number": 0 if revision is None else revision.revision_number,
                    "revision_id": "" if revision is None else revision.id,
                    "comment_count": len(annotations),
                    "open_comment_count": sum(
                        1 for item in annotations if item.state in {"open", "orphaned"} and not item.parent_id
                    ),
                }
            )
        return web.json_response(
            {"reviews": rows, "repo_root": "", "status_filter": status_filter},
            headers=self._security_headers(),
        )

    async def api(self, request: web.Request) -> web.StreamResponse:
        refusal = self._guard_refusal(request)
        if refusal:
            return web.json_response({"detail": refusal}, status=403, headers=self._security_headers())
        repo = self._repository_for_request(request)
        if repo is None:
            raise web.HTTPNotFound()
        listener_port = request.url.port or (443 if request.scheme == "https" else 80)
        return await _dispatch_asgi(self._app_for(repo, port=listener_port), request)

    def _repository_for_request(self, request: web.Request) -> Path | None:
        path = request.path
        parts = [part for part in path.split("/") if part]
        review_id = ""
        if path == "/api/compare":
            scope = request.query.get("scope", "")
            if scope:
                review_id = scope
            else:
                for source_ref in (request.query.get("from_ref", ""), request.query.get("to_ref", "")):
                    if source_ref.startswith("rr/"):
                        revision = self.store.resolve_revision(source_ref)
                        if revision is not None:
                            review_id = revision.review_id
                            break
                    elif source_ref.startswith("r/"):
                        session = self.store.resolve_session(source_ref)
                        if session is not None:
                            review_id = session.id
                            break
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "reviews":
            review_id = parts[2]
        elif len(parts) >= 3 and parts[0] == "api" and parts[1] == "annotations":
            annotation = self.store.get_annotation(parts[2])
            review_id = "" if annotation is None else annotation.review_id
        elif len(parts) >= 3 and parts[0] == "api" and parts[1] == "evidence":
            evidence = self.store.get_evidence(parts[2])
            review_id = "" if evidence is None else evidence.review_id
        if not review_id:
            return None
        try:
            session = self.store.resolve_session(review_id)
        except LookupError:
            return None
        if session is None:
            return None
        root = Path(session.repo_root).expanduser().resolve()
        return root if root.is_dir() else None

    def _app_for(self, repo_root: Path, *, port: int) -> Any:
        key = f"{repo_root}@{port}"
        app = self._apps.get(key)
        if app is not None:
            return app

        from fastapi import FastAPI

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        async def _already_authenticated() -> None:
            return None

        register_review_api(
            app,
            self.store,
            auth_dependency=_already_authenticated,
            repo_root=repo_root,
            port=port,
        )
        self._apps[key] = app
        return app


LOCAL_REVIEW_APP_KEY: web.AppKey[LocalReviewSurface] = web.AppKey("local_review_surface", LocalReviewSurface)


# The Reader API has ~40 `async def` handlers that do blocking SQLite, git and CPU
# work. On the server's one event loop, a single slow request (a comment save, a
# mark, a map) stalled every other request of every open Reader. Each request that
# does not stream therefore runs its whole ASGI call on its own thread and loop.
# `/ask` and Review SSE keep the shared loop: streaming calls cannot be buffered.
# Four threads: the work is CPU-bound under one GIL, so more threads add no
# throughput, only peak memory (each holds a multi-MB Snapshot projection).
_ISOLATED_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lc-reader-asgi")


def _runs_isolated(path: str) -> bool:
    return "/ask" not in path and not path.endswith(("/source-state/events", "/live-state/events"))


class _IsolatedCall:
    """One ASGI call on its own thread and event loop, cancellable from outside."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[None] | None = None
        self.cancel_requested = False
        self.lock = threading.Lock()

    def cancel(self) -> None:
        with self.lock:
            self.cancel_requested = True
            if self.loop is not None and self.task is not None and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.task.cancel)


def _run_asgi_isolated(
    app: Any, scope: dict[str, Any], body: bytes, budget: ReadBudget, call: _IsolatedCall
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    status = 500
    headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()  # no disconnect signal here; cancellation ends the call
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status = int(message.get("status", 500))
            headers = list(message.get("headers", ()))
        elif message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body", b"")))

    loop = asyncio.new_event_loop()
    try:
        with read_budget(budget):
            task = loop.create_task(app(scope, receive, send))
            with call.lock:
                call.loop, call.task = loop, task
                if call.cancel_requested:
                    task.cancel()
            try:
                loop.run_until_complete(task)
            finally:
                # Whatever the app left running (middleware receive tasks) ends with it.
                leftovers = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for leftover in leftovers:
                    leftover.cancel()
                if leftovers:
                    loop.run_until_complete(asyncio.gather(*leftovers, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()
    return status, headers, b"".join(chunks)


async def run_asgi_get_isolated(app: Any, path: str) -> web.Response:
    """One in-process GET on the Reader pool, buffered like a dispatched request."""

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1")],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
        "state": {},
    }
    loop = asyncio.get_running_loop()
    status, raw_headers, payload = await loop.run_in_executor(
        _ISOLATED_POOL, _run_asgi_isolated, app, scope, b"", ReadBudget(), _IsolatedCall()
    )
    return web.Response(status=status, headers=_response_headers(raw_headers), body=payload)


def _response_headers(raw_headers: list[tuple[bytes, bytes]]) -> CIMultiDict[str]:
    out = CIMultiDict[str]()
    for name, value in raw_headers:
        if name.lower() in _HOP_HEADERS or name.lower() == b"content-length":
            continue
        out.add(name.decode("latin-1"), value.decode("latin-1"))
    return out


async def _dispatch_isolated(app: Any, request: web.Request, body: bytes) -> web.StreamResponse:
    budget = ReadBudget()
    call = _IsolatedCall()
    scope = _asgi_scope(request)
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    future = loop.run_in_executor(
        _ISOLATED_POOL, lambda: context.run(_run_asgi_isolated, app, scope, body, budget, call)
    )

    async def watch_disconnect() -> None:
        while not future.done():
            transport = request.transport
            if transport is None or transport.is_closing():
                budget.cancelled.set()
                call.cancel()
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(watch_disconnect())
    try:
        status, raw_headers, payload = await asyncio.shield(future)
    except asyncio.CancelledError:
        budget.cancelled.set()
        call.cancel()
        # The app winds down on its own thread; wait for it so nothing leaks.
        await asyncio.gather(future, return_exceptions=True)
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    return web.Response(status=status, headers=_response_headers(raw_headers), body=payload)


def _asgi_scope(request: web.Request) -> dict[str, Any]:
    raw_headers = [(name.lower(), value) for name, value in request.raw_headers if name.lower() not in _HOP_HEADERS]
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": f"{request.version.major}.{request.version.minor}",
        "method": request.method,
        "scheme": request.scheme,
        "path": request.path,
        "raw_path": request.path.encode(),
        "query_string": request.query_string.encode("ascii", errors="surrogateescape"),
        "root_path": "",
        "headers": raw_headers,
        "client": request.transport.get_extra_info("peername") if request.transport is not None else None,
        "server": (request.host.split(":", 1)[0], request.url.port or (443 if request.scheme == "https" else 80)),
        "state": {},
    }


async def _dispatch_asgi(app: Any, request: web.Request) -> web.StreamResponse:
    """Run one ASGI request in-process and stream the result through aiohttp."""

    body = await request.read()
    if _runs_isolated(request.path):
        return await _dispatch_isolated(app, request, body)
    received = False
    disconnected = asyncio.Event()
    budget = ReadBudget()

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)

    async def send(message: dict[str, Any]) -> None:
        await messages.put(message)

    scope = _asgi_scope(request)

    with read_budget(budget):
        task = asyncio.create_task(app(scope, receive, send))

    async def watch_disconnect() -> None:
        while not task.done():
            transport = request.transport
            if transport is None or transport.is_closing():
                disconnected.set()
                budget.cancelled.set()
                task.cancel()
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(watch_disconnect())
    try:
        first = await _next_asgi_message(task, messages)
        if first.get("type") != "http.response.start":
            raise RuntimeError("local Review ASGI app emitted a body before response start")

        headers = CIMultiDict[str]()
        for name, value in first.get("headers", ()):
            if name.lower() in _HOP_HEADERS or name.lower() == b"content-length":
                continue
            headers.add(name.decode("latin-1"), value.decode("latin-1"))
        response = web.StreamResponse(status=int(first.get("status", 500)), headers=headers)
        await response.prepare(request)

        while True:
            message = await _next_asgi_message(task, messages)
            if message.get("type") != "http.response.body":
                continue
            chunk = message.get("body", b"")
            if chunk:
                await response.write(chunk)
            if not message.get("more_body", False):
                break
        await response.write_eof()
        await task
        return response
    finally:
        disconnected.set()
        budget.cancelled.set()
        for pending in (task, watcher):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, watcher, return_exceptions=True)


async def _next_asgi_message(
    task: asyncio.Task[None],
    messages: asyncio.Queue[dict[str, Any]],
) -> dict[str, Any]:
    """Return the next ASGI message without hanging if the inner app dies."""

    if not messages.empty():
        return messages.get_nowait()

    waiting = asyncio.create_task(messages.get())
    try:
        done, _pending = await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
        if waiting in done:
            return waiting.result()

        # A clean early return is also a broken ASGI response.
        exception = task.exception()
        if exception is not None:
            raise exception
        raise RuntimeError("local Review ASGI app ended before completing the response")
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
