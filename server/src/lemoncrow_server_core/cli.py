"""Command-line entry point for the public loopback server."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from .bootstrap import build_local_server
from .config import LocalServerConfig
from .limits import Limits
from .state_lock import StateRootBusy, claim_state_root

# Index workers run pure-Python edge and ANN work in threads of this process.
# Each time the event loop blocks on I/O it must win the GIL back from them,
# which takes up to one switch interval: at the default 5 ms, /healthz alone
# took ~22 ms at p50 beside one busy thread; at 1 ms, ~5 ms.
# lc-debt: CPU-bound index builds share the server's GIL; move them to a worker process.
_SWITCH_INTERVAL_S = 0.001

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lemoncrow-server")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--directory", type=Path, required=True)
    up.add_argument("--port", type=int, default=7420)
    up.add_argument("--token-file", type=Path)
    up.add_argument("--local-no-auth", action="store_true")
    up.add_argument("--frontend-dir", type=Path)
    up.add_argument("--allow-local-fs", action="store_true")
    up.add_argument(
        "--ephemeral",
        action="store_true",
        help="use the in-memory index backend; index state is discarded on exit",
    )
    up.add_argument("--check-only", action="store_true")
    up.add_argument("--max-repository-index-bytes", type=int, default=64 * 1024**3)
    up.add_argument("--max-index-wal-bytes", type=int, default=128 * 1024**2)
    up.add_argument("--max-deferred-index-files", type=int, default=1_250_000)

    migrate = sub.add_parser("migrate-index", help="split and compact a shared index with the server stopped")
    migrate.add_argument("--database", type=Path, required=True)
    migrate.add_argument("--max-repository-index-bytes", type=int, default=64 * 1024**3)
    migrate.add_argument("--max-index-wal-bytes", type=int, default=128 * 1024**2)

    gateway = sub.add_parser("mcp-gateway")
    gateway.add_argument("--port", type=int, default=7421)
    gateway.add_argument("--backend-port", type=int, default=7420)
    return parser


async def _serve(config: LocalServerConfig) -> int:
    server, _state = build_local_server(config)
    url = await server.start()
    sys.stdout.write(url + "\n")
    sys.stdout.flush()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await server.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "migrate-index":
        from .index.migration import migrate_index

        try:
            report = migrate_index(
                args.database,
                max_repository_bytes=args.max_repository_index_bytes,
                max_wal_bytes=args.max_index_wal_bytes,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        except Exception as exc:
            sys.stderr.write(f"index migration failed; source retained: {exc}\n")
            return 1
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.command == "mcp-gateway":
        if not (1 <= args.port <= 65_535 and 1 <= args.backend_port <= 65_535):
            sys.stderr.write("mcp gateway ports must be between 1 and 65535\n")
            return 2
        from .local_mcp_gateway import serve_gateway

        try:
            return asyncio.run(serve_gateway(port=args.port, backend_port=args.backend_port))
        except Exception as exc:
            sys.stderr.write(f"local MCP gateway failed: {exc}\n")
            return 1
    if args.command != "up":
        return 2
    config = LocalServerConfig(
        listen_port=args.port,
        state_root=args.directory.expanduser().resolve(),
        token_file=None if args.token_file is None else args.token_file.expanduser().resolve(),
        local_no_auth=bool(args.local_no_auth),
        frontend_dir=None if args.frontend_dir is None else args.frontend_dir.expanduser().resolve(),
        allow_local_fs=bool(args.allow_local_fs),
        ephemeral=bool(args.ephemeral),
        limits=Limits(
            max_repository_index_bytes=args.max_repository_index_bytes,
            max_index_wal_bytes=args.max_index_wal_bytes,
            max_deferred_index_files=args.max_deferred_index_files,
        ),
    )
    if not config.local_no_auth and config.token_file is None:
        sys.stderr.write("local server requires --local-no-auth or --token-file\n")
        return 2
    if args.check_only:
        try:
            server, _state = build_local_server(config)
        except Exception as exc:
            sys.stderr.write(f"local server check failed: {exc}\n")
            return 1
        # No socket is bound during check-only. Close the materializer opened by
        # composition so the state directory is immediately reusable.
        if server.state.workspace is not None:
            server.state.workspace.close()
        server.state.backend.close_backend()
        return 0
    assert config.state_root is not None
    try:
        claim = claim_state_root(config.state_root, port=config.listen_port)
    except StateRootBusy as exc:
        sys.stderr.write(f"local server refused: {exc}\n")
        return 1
    sys.setswitchinterval(_SWITCH_INTERVAL_S)
    try:
        return asyncio.run(_serve(config))
    except Exception as exc:
        sys.stderr.write(f"local server failed: {exc}\n")
        return 1
    finally:
        claim.close()


if __name__ == "__main__":
    raise SystemExit(main())
