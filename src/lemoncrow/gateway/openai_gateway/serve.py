"""Standalone local gateway process used by lc code."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def serve(
    port: int = 8790,
    host: str = "127.0.0.1",
    project_root: str | None = None,
    yolo: bool = True,
    reload: bool = False,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    import os

    import uvicorn

    from .app import create_app

    app = create_app(
        project_root=project_root,
        yolo=yolo,
        model=model,
        provider=provider,
    )
    max_concurrency = max(
        1,
        int(os.environ.get("LEMONCROW_OPENAI_MAX_CONCURRENCY", "64")),
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        reload=reload,
        limit_concurrency=max_concurrency,
        timeout_keep_alive=30,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the LemonCrow local LLM gateway")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--no-yolo", action="store_true")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    args = parser.parse_args(argv)
    serve(
        port=args.port,
        host=args.host,
        project_root=args.project_root,
        yolo=not args.no_yolo,
        reload=args.reload,
        model=args.model,
        provider=args.provider,
    )


if __name__ == "__main__":
    main()
