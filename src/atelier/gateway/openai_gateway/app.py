"""FastAPI application for the Atelier OpenAI-compatible gateway.

Wraps ``InteractiveRuntime`` in a standards-compliant HTTP server:
  - ``POST /v1/chat/completions``  — streaming or buffered completion
  - ``GET  /v1/models``            — list available Atelier models
  - ``GET  /health``               — liveness probe

Usage::

    from atelier.gateway.openai_gateway.app import create_app
    app = create_app(project_root="/path/to/project")
"""

from __future__ import annotations

import ipaddress
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from atelier.gateway.cli.runtime import InteractiveRuntime

from .adapter import run_chat_completion
from .schemas import ChatCompletionRequest, ModelListResponse, ModelObject


def _is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _require_auth(request: Request) -> None:
    """Gate /v1/* (and, when wired, /mcp) routes behind a bearer token.

    When ``ATELIER_GATEWAY_TOKEN`` is set, every request must present a matching
    ``Authorization: Bearer <token>`` header. When unset, only loopback clients
    are allowed so the yolo runtime is never reachable from the network.

    L1 — NOTE: this gateway authenticates with ``ATELIER_GATEWAY_TOKEN``, which is
    DISTINCT from the service API's auth knobs (``ATELIER_REQUIRE_AUTH`` /
    ``ATELIER_API_KEY``). They are separate surfaces; setting one does not affect
    the other, so both must be configured independently when both are exposed.
    """
    token = os.environ.get("ATELIER_GATEWAY_TOKEN")
    if not token:
        if _is_loopback(request):
            return
        raise HTTPException(
            status_code=403,
            detail="Gateway requires ATELIER_GATEWAY_TOKEN for non-loopback access",
        )
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented, token):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def create_app(
    project_root: str | None = None,
    yolo: bool = True,
    model: str | None = None,
    provider: str | None = None,
) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        project_root: Working directory for the Atelier runtime.
            Defaults to the process cwd.
        yolo: Auto-approve edit and shell tools for unattended endpoint use.
        model: Optional LiteLLM model override, such as
            ``bedrock/us.anthropic.claude-sonnet-4-6``.
        provider: Provider label paired with ``model`` for routing telemetry.
    """
    runtime = InteractiveRuntime(yolo=yolo, model=model, provider=provider)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start_session(project_root)  # warm up tools in the configured workspace
        yield
        runtime.shutdown()

    app = FastAPI(
        title="Atelier OpenAI Gateway",
        version="1.0.0",
        description="OpenAI-compatible chat completions endpoint backed by Atelier's execution engine.",
        lifespan=lifespan,
    )

    # Restrict CORS to same-host origins — the gateway runs tools, so a browser
    # on any origin must not be able to drive it.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── /health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ── /v1/models ───────────────────────────────────────────────────────────

    @app.get("/v1/models", dependencies=[Depends(_require_auth)])
    async def list_models() -> ModelListResponse:
        from atelier.core.capabilities.providers.discovery import discover_models

        model_ids = await discover_models()
        return ModelListResponse(data=[ModelObject(id=m) for m in model_ids])

    @app.post("/v1/models/refresh", dependencies=[Depends(_require_auth)])
    async def refresh_models() -> ModelListResponse:
        from atelier.core.capabilities.providers.discovery import discover_models, invalidate_cache

        invalidate_cache()
        model_ids = await discover_models()
        return ModelListResponse(data=[ModelObject(id=m) for m in model_ids])

    # ── /v1/chat/completions ──────────────────────────────────────────

    @app.post("/v1/chat/completions", dependencies=[Depends(_require_auth)])
    async def chat_completions(req: ChatCompletionRequest) -> Any:
        return await run_chat_completion(runtime, req)

    # ── MCP HTTP transport (G17, opt-in) ─────────────────────────────────────
    # Mount the streamable-HTTP/SSE MCP transport + discovery manifest only when
    # explicitly enabled. stdio remains the default; this never auto-starts.
    from atelier.core.environment import bool_env

    if bool_env("ATELIER_MCP_HTTP"):
        from atelier.gateway.adapters.mcp_http import register_mcp_http

        # C1 — gate /mcp with the same auth dependency as /v1/*; the tool surface
        # must not be reachable unauthenticated while the rest is locked down.
        register_mcp_http(app, auth_dependency=_require_auth)

    return app
