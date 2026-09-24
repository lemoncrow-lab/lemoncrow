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
from pathlib import Path
from typing import Any

from aiohttp import web
from multidict import CIMultiDict

from lemoncrow.pro.capabilities.review.api import SECURITY_HEADERS, origin_refusal, register_review_api
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.workspace import bundle_dir

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

    def __init__(self, store_root: Path, *, port: int, frontend_dir: Path | None = None) -> None:
        self.store_root = Path(store_root).expanduser().resolve()
        self.port = int(port)
        self.store = ReviewStore(self.store_root)
        configured = None if frontend_dir is None else Path(frontend_dir).expanduser().resolve()
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
        return self.is_ui_path(path) or path.startswith("/assets/") or path.lstrip("/") in _ROOT_PUBLIC_ASSETS

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
            web.get("/favicon.svg", self.root_asset),
            web.get("/lemoncrow-card.png", self.root_asset),
            web.get("/lemoncrow-card.svg", self.root_asset),
            web.get("/site.webmanifest", self.root_asset),
            web.get("/robots.txt", self.root_asset),
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
        raise web.HTTPFound("/home", headers=self._security_headers())

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
        return web.FileResponse(candidate, headers=self._security_headers())

    async def root_asset(self, request: web.Request) -> web.StreamResponse:
        refusal = self._guard_refusal(request)
        if refusal:
            return web.json_response({"detail": refusal}, status=403, headers=self._security_headers())
        if self.bundle is None:
            raise web.HTTPNotFound()
        name = request.path.lstrip("/")
        if name not in _ROOT_PUBLIC_ASSETS:
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
        rows: list[dict[str, Any]] = []
        for session in sessions:
            revision = self.store.latest_revision(session.id)
            annotations = self.store.list_annotations(session.id)
            rows.append(
                {
                    "id": session.id,
                    "ref": f"r/{session.id}",
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


async def _dispatch_asgi(app: Any, request: web.Request) -> web.StreamResponse:
    """Run one ASGI request in-process and stream the result through aiohttp."""

    body = await request.read()
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(message: dict[str, Any]) -> None:
        await messages.put(message)

    raw_headers = [(name.lower(), value) for name, value in request.raw_headers if name.lower() not in _HOP_HEADERS]
    scope: dict[str, Any] = {
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

    task = asyncio.create_task(app(scope, receive, send))
    first = await _next_asgi_message(task, messages)
    if first.get("type") != "http.response.start":
        task.cancel()
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


async def _next_asgi_message(
    task: asyncio.Task[None],
    messages: asyncio.Queue[dict[str, Any]],
) -> dict[str, Any]:
    """Return the next ASGI message without hanging if the inner app dies."""

    if not messages.empty():
        return messages.get_nowait()

    waiting = asyncio.create_task(messages.get())
    done, _pending = await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
    if waiting in done:
        return waiting.result()

    waiting.cancel()
    try:
        await waiting
    except asyncio.CancelledError:
        pass

    # The app finished before producing the response message we were waiting
    # for. Re-raise its exception when there is one; a clean early return is
    # still a broken ASGI response and must not leave the aiohttp request hung.
    exception = task.exception()
    if exception is not None:
        raise exception
    raise RuntimeError("local Review ASGI app ended before completing the response")
