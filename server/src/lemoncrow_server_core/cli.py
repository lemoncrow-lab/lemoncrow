"""Command-line entry point for the public loopback server."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from .bootstrap import build_local_server
from .config import LOCAL_PRODUCTS, LocalServerConfig, canonical_products
from .limits import Limits
from .state_lock import StateRootBusy, claim_state_root

# Index workers run pure-Python edge and ANN work in threads of this process.
# Each time the event loop blocks on I/O it must win the GIL back from them,
# which takes up to one switch interval: at the default 5 ms, /healthz alone
# took ~22 ms at p50 beside one busy thread; at 1 ms, ~5 ms.
# lc-debt: CPU-bound index builds share the server's GIL; move them to a worker process.
_SWITCH_INTERVAL_S = 0.001

__all__ = ["main", "review_main"]

_REVIEW_ONLY: frozenset[str] = frozenset({"review"})


def _parser(*, review_only: bool = False) -> argparse.ArgumentParser:
    """The local server CLI; ``review_only`` is the Review artifact's narrower surface."""
    parser = argparse.ArgumentParser(prog="lemoncrow-review-server" if review_only else "lemoncrow-server")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--directory", type=Path, required=True)
    up.add_argument("--port", type=int, default=7420)
    up.add_argument("--token-file", type=Path)
    up.add_argument("--local-no-auth", action="store_true")
    up.add_argument("--frontend-dir", type=Path)
    up.add_argument("--allow-local-fs", action="store_true")
    up.add_argument(
        "--product",
        action="append",
        # `runtime` is the pre-Runtime-Lite name for the Dashboard.
        choices=("review",) if review_only else ("review", "dashboard", "runtime"),
        dest="products",
        help=(
            "Review is the only product this server carries"
            if review_only
            else "enable one product; repeat to enable both (default: both, or LEMONCROW_LOCAL_SERVER_PRODUCTS)"
        ),
    )
    up.add_argument(
        "--ephemeral",
        action="store_true",
        help="use the in-memory index backend; index state is discarded on exit",
    )
    up.add_argument("--check-only", action="store_true")
    up.add_argument("--max-repository-index-bytes", type=int, default=64 * 1024**3)
    up.add_argument("--max-index-wal-bytes", type=int, default=128 * 1024**2)
    up.add_argument("--max-deferred-index-files", type=int, default=1_250_000)
    up.add_argument(
        "--in-process-services",
        action="store_true",
        help="host Review and Dashboard in this process instead of isolated workers",
    )

    service = sub.add_parser("service", help="one isolated service; started by `up`, not by hand")
    service.add_argument("--name", choices=("review",) if review_only else ("review", "dashboard"), required=True)
    service.add_argument("--state-root", type=Path, required=True)
    service.add_argument("--review-root", type=Path)
    service.add_argument("--public-port", type=int, default=0)
    service.add_argument("--max-request-bytes", type=int, default=16 * 1024**2)
    service.add_argument("--maintenance-interval-s", type=float, default=300.0)
    service.add_argument("--app", default="", help="module:factory for the dashboard's ASGI app")

    if not review_only:
        # A Review artifact never had the shared pre-repository index this migrates.
        migrate = sub.add_parser("migrate-index", help="split and compact a shared index with the server stopped")
        migrate.add_argument("--database", type=Path, required=True)
        migrate.add_argument("--max-repository-index-bytes", type=int, default=64 * 1024**3)
        migrate.add_argument("--max-index-wal-bytes", type=int, default=128 * 1024**2)
    return parser


def _selected_products(cli_products: list[str] | None, *, review_only: bool = False) -> frozenset[str]:
    allowed = _REVIEW_ONLY if review_only else LOCAL_PRODUCTS
    if cli_products:
        return canonical_products(cli_products)
    raw = os.environ.get("LEMONCROW_LOCAL_SERVER_PRODUCTS", "").strip()
    if not raw:
        return allowed
    products = canonical_products(part.strip().lower() for part in raw.split(",") if part.strip())
    if not products:
        raise ValueError("local server requires at least one product: review or dashboard")
    unknown = products - allowed
    if unknown and review_only:
        raise ValueError(f"this Review server carries only review, not: {', '.join(sorted(unknown))}")
    if unknown:
        raise ValueError(f"unknown local server product(s): {', '.join(sorted(unknown))}")
    return products


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


def review_main(argv: Sequence[str] | None = None) -> int:
    """``lemoncrow-review-server``: the Review artifact's entry point (Review only)."""
    return main(argv, review_only=True)


def main(argv: Sequence[str] | None = None, *, review_only: bool = False) -> int:
    args = _parser(review_only=review_only).parse_args(argv)
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
    if args.command == "service":
        from .service_host import serve_service

        # The same reason as `up`, in every isolated service process (the Review one serves the Reader): its
        # per-file reads win the GIL back from enrichment requests at every SQLite or file call, and at the
        # default 5 ms a 10k Review's one-file patch took 80-340 ms beside the open's enrichment.
        sys.setswitchinterval(_SWITCH_INTERVAL_S)
        state_root = args.state_root.expanduser().resolve()
        try:
            return asyncio.run(
                serve_service(
                    args.name,
                    state_root=state_root,
                    review_root=(
                        state_root / "reviews" if args.review_root is None else args.review_root.expanduser().resolve()
                    ),
                    public_port=args.public_port,
                    max_request_bytes=args.max_request_bytes,
                    maintenance_interval_s=args.maintenance_interval_s,
                    app_reference=args.app,
                )
            )
        except Exception as exc:
            sys.stderr.write(f"{args.name} service failed: {exc}\n")
            return 1
    if args.command != "up":
        return 2
    try:
        products = _selected_products(args.products, review_only=review_only)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    config = LocalServerConfig(
        listen_port=args.port,
        state_root=args.directory.expanduser().resolve(),
        token_file=None if args.token_file is None else args.token_file.expanduser().resolve(),
        local_no_auth=bool(args.local_no_auth),
        frontend_dir=None if args.frontend_dir is None else args.frontend_dir.expanduser().resolve(),
        allow_local_fs=bool(args.allow_local_fs),
        ephemeral=bool(args.ephemeral),
        isolate_services=not args.in_process_services,
        products=products,
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
