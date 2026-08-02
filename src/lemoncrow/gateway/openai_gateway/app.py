"""OpenAI- and Anthropic-compatible gateway over LemonCrow's owned runtime."""

from __future__ import annotations

import ipaddress
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from lemoncrow.gateway.cli.runtime import InteractiveRuntime

from .adapter import run_chat_completion
from .anthropic import (
    AnthropicCountRequest,
    AnthropicMessageRequest,
    count_anthropic_tokens,
    run_anthropic_message,
)
from .responses import run_response
from .schemas import ChatCompletionRequest, ModelListResponse, ModelObject, ResponsesRequest


def _is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _require_auth(request: Request) -> None:
    """Require the managed token; accept both OpenAI and Anthropic headers."""
    token = os.environ.get("LEMONCROW_GATEWAY_TOKEN")
    if not token:
        if _is_loopback(request):
            return
        raise HTTPException(
            status_code=403,
            detail="Gateway requires LEMONCROW_GATEWAY_TOKEN for non-loopback access",
        )

    header = request.headers.get("Authorization", "")
    scheme, _, bearer = header.partition(" ")
    api_key = request.headers.get("x-api-key", "")
    bearer_ok = scheme.lower() == "bearer" and secrets.compare_digest(bearer, token)
    key_ok = bool(api_key) and secrets.compare_digest(api_key, token)
    if not (bearer_ok or key_ok):
        raise HTTPException(status_code=401, detail="Invalid or missing gateway token")


def _float_env(name: str) -> float | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "off", "no"}


def create_app(
    project_root: str | None = None,
    yolo: bool = True,
    model: str | None = None,
    provider: str | None = None,
) -> FastAPI:
    """Build the local gateway used by lc code and standalone clients."""
    resolved_model = model or os.environ.get("LEMONCROW_CODE_MODEL") or None
    resolved_provider = provider or os.environ.get("LEMONCROW_CODE_PROVIDER") or None
    runtime_root = os.environ.get("LEMONCROW_ROOT")
    runtime = InteractiveRuntime(
        root=Path(runtime_root) if runtime_root else None,
        yolo=yolo,
        model=resolved_model,
        provider=resolved_provider,
        budget_hint=os.environ.get("LEMONCROW_CODE_BUDGET", "balanced"),
        cache_policy=os.environ.get("LEMONCROW_CODE_CACHE_POLICY", "auto"),
        max_cost=_float_env("LEMONCROW_CODE_MAX_COST"),
        dynamic_routing=not bool(resolved_model),
        mcp_enabled=_bool_env("LEMONCROW_CODE_MCP", True),
        mcp_schema_mode=os.environ.get("LEMONCROW_CODE_MCP_SCHEMA_MODE", "auto"),
        optimization_mode=os.environ.get("LEMONCROW_OPTIMIZATION_MODE", "shadow"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        warm_session = await runtime.start_session(project_root)
        runtime.drop_session(warm_session)
        yield
        runtime.shutdown()

    app = FastAPI(
        title="LemonCrow LLM Gateway",
        version="1.1.0",
        description="OpenAI and Anthropic wire protocols backed by LemonCrow's owned execution engine.",
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models", dependencies=[Depends(_require_auth)])
    async def list_models() -> ModelListResponse:
        from lemoncrow.core.capabilities.providers.discovery import discover_models

        model_ids = await discover_models()
        return ModelListResponse(data=[ModelObject(id=item) for item in ["lemoncrow", *model_ids]])

    @app.post("/v1/models/refresh", dependencies=[Depends(_require_auth)])
    async def refresh_models() -> ModelListResponse:
        from lemoncrow.core.capabilities.providers.discovery import discover_models, invalidate_cache

        invalidate_cache()
        model_ids = await discover_models()
        return ModelListResponse(data=[ModelObject(id=item) for item in ["lemoncrow", *model_ids]])

    @app.post("/v1/chat/completions", dependencies=[Depends(_require_auth)])
    async def chat_completions(req: ChatCompletionRequest) -> Any:
        return await run_chat_completion(runtime, req)

    @app.post("/v1/responses", dependencies=[Depends(_require_auth)])
    async def responses(req: ResponsesRequest) -> Any:
        return await run_response(runtime, req)

    @app.post("/v1/messages", dependencies=[Depends(_require_auth)])
    async def anthropic_messages(req: AnthropicMessageRequest) -> Any:
        return await run_anthropic_message(runtime, req)

    @app.post("/v1/messages/count_tokens", dependencies=[Depends(_require_auth)])
    async def anthropic_count_tokens(req: AnthropicCountRequest) -> dict[str, int]:
        return count_anthropic_tokens(req)

    from lemoncrow.core.environment import bool_env

    if bool_env("LEMONCROW_MCP_HTTP"):
        from lemoncrow.gateway.adapters.mcp_http import register_mcp_http

        register_mcp_http(app, auth_dependency=_require_auth)

    return app


__all__ = ["create_app"]
