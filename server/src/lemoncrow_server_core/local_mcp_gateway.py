"""Standalone persistent-MCP gateway for local LemonCrow.

The gateway is deliberately a separate process from the indexed server. It
terminates OAuth + streamable HTTP MCP, then delegates every tool through
``lemoncrow_client.McpServer``. Client-routed tools therefore execute here,
while indexed/intelligence tools go to the backend server.

Keeping this process outside ``lemoncrow-local-server.service`` is load-bearing:
a backend restart may briefly make server-routed tools unavailable, but it must
not tear down the MCP transport or client-side tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from aiohttp import web

from .local_remote_mcp import LocalRemoteMcpSurface

__all__ = ["build_gateway_app", "serve_gateway"]


def build_gateway_app(*, backend_port: int = 7420) -> tuple[LocalRemoteMcpSurface, web.Application]:
    surface = LocalRemoteMcpSurface(backend_port=backend_port)
    app = web.Application(client_max_size=8 * 1024 * 1024)

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "role": "mcp_gateway",
                "backend": f"http://127.0.0.1:{backend_port}",
            }
        )

    app.add_routes([web.get("/healthz", healthz), *surface.route_defs()])

    async def close_surface(_app: web.Application) -> None:
        surface.close()

    app.on_shutdown.append(close_surface)
    return surface, app


async def serve_gateway(*, port: int, backend_port: int) -> int:
    _surface, app = build_gateway_app(backend_port=backend_port)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    sys.stdout.write(f"http://127.0.0.1:{port}\n")
    sys.stdout.flush()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
    return 0
