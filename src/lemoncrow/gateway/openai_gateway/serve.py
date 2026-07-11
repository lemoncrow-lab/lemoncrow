"""Entry point for ``lemon serve-openai``.

Starts the LemonCrow OpenAI-compatible HTTP server via uvicorn.
"""

from __future__ import annotations


def serve(
    port: int = 8790,
    host: str = "127.0.0.1",
    project_root: str | None = None,
    yolo: bool = True,
    reload: bool = False,
) -> None:
    """Start the OpenAI-compatible gateway server.

    Args:
        port: TCP port to bind (default 8787).
        host: Bind address (default 127.0.0.1 — loopback only; the yolo
            runtime must not be exposed to the network without a token).
        project_root: Working directory passed to the LemonCrow runtime.
        yolo: Auto-approve all tool permission prompts (default True for
            gateway mode — the TUI cannot respond to interactive prompts).
        reload: Enable uvicorn hot-reload (development only).
    """
    import os

    import uvicorn

    from .app import create_app

    app = create_app(project_root=project_root, yolo=yolo)
    # Bound in-flight requests so a burst of clients can't pile unbounded work
    # onto the single-process event loop; excess connections wait rather than
    # exhausting memory. Loopback single-user by default, so the cap is generous
    # and env-overridable.
    max_concurrency = max(1, int(os.environ.get("LEMONCROW_OPENAI_MAX_CONCURRENCY", "64")))
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        reload=reload,
        limit_concurrency=max_concurrency,
        timeout_keep_alive=30,
    )
