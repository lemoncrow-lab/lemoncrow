#!/usr/bin/env python3
"""Measure hosted PostgreSQL query-path and loopback TLS transport overhead.

This complements the frozen CodeBench MRR corpus. It deliberately reports the
remote-authority delta separately from HTTP/TLS so a regression cannot be hidden
inside one end-to-end number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import statistics
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import ClientSession, TCPConnector, web
from lemoncrow_server.postgres_metadata import PostgresMetadataStore, build_postgres_metadata_hybrid
from lemoncrow_server_core.index.analysis import profile_for_path
from lemoncrow_server_core.index.contracts import ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import manifest_root
from lemoncrow_server_core.index.memory import build_memory_backend
from lemoncrow_server_core.index.query import IndexQuery


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


def seed(backend: object, *, org: str, count: int) -> tuple[str, list[str]]:
    repo = backend.repos.canonical_repo_id(org, RepoIdentityClaim("", "", f"latency-{uuid.uuid4().hex}"))
    files = {
        f"pkg/module_{i:04d}.py": (f"def symbol_{i:04d}():\n    value = 'needle_{i:04d}'\n    return value\n").encode()
        for i in range(count)
    }
    entries = tuple(
        ManifestEntry(
            path=path,
            content_digest=sha256_hex(data),
            parser_profile=profile_for_path(path),
            mode=0o100644,
            size=len(data),
        )
        for path, data in sorted(files.items())
    )
    opened = backend.views.open(
        org_id=org,
        repo_id=repo,
        base_revision="latency-base",
        manifest_root=manifest_root(entries),
        chunk_ids=("chunk-0",),
        lease_s=86_400,
        content_subject="bench",
        owner_subject="bench",
    )
    view_id = opened.state.view_id
    backend.views.put_manifest_chunk(org_id=org, view_id=view_id, chunk_id="chunk-0", entries=entries)
    for entry in entries:
        data = files[entry.path]
        backend.content.put(org, entry.content_digest, data)
        backend.provenance.record(org, repo, "bench", (entry.content_digest,))
        backend.analysis.ensure(org, entry.content_digest, entry.parser_profile, data)
    return view_id, [f"symbol_{i:04d}" for i in range(count)]


def query_latency(backend: object, *, org: str, view_id: str, queries: list[str], repetitions: int) -> dict[str, float]:
    engine = IndexQuery(backend, content_size_cap=1_048_576)
    timings: list[float] = []
    selected = queries[:repetitions]
    # Warm caches/pools independently of the measurement.
    for query in selected[: min(20, len(selected))]:
        engine.search(org_id=org, view_id=view_id, query=query, limit=8)
    for query in selected:
        started = time.perf_counter()
        answer = engine.search(org_id=org, view_id=view_id, query=query, limit=8)
        elapsed = (time.perf_counter() - started) * 1000.0
        if not answer.hits:
            raise RuntimeError(f"benchmark query returned no hits: {query}")
        timings.append(elapsed)
    return {
        "mean_ms": round(statistics.fmean(timings), 3),
        "p50_ms": round(percentile(timings, 0.50), 3),
        "p95_ms": round(percentile(timings, 0.95), 3),
        "p99_ms": round(percentile(timings, 0.99), 3),
        "max_ms": round(max(timings), 3),
        "n": len(timings),
    }


async def _transport_sample(*, tls: bool, requests: int) -> dict[str, float]:
    app = web.Application()
    app.router.add_get("/healthz", lambda _request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_context: ssl.SSLContext | None = None
    client_ssl: ssl.SSLContext | bool = False
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if tls:
        temporary = tempfile.TemporaryDirectory(prefix="lc-latency-tls-")
        root = Path(temporary.name)
        key = root / "key.pem"
        cert = root / "cert.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert, key)
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    sockets = site._server.sockets if site._server is not None else []
    port = int(sockets[0].getsockname()[1])
    scheme = "https" if tls else "http"
    timings: list[float] = []
    try:
        async with ClientSession(connector=TCPConnector(ssl=client_ssl)) as session:
            for _ in range(20):
                async with session.get(f"{scheme}://127.0.0.1:{port}/healthz") as response:
                    await response.read()
            for _ in range(requests):
                started = time.perf_counter()
                async with session.get(f"{scheme}://127.0.0.1:{port}/healthz") as response:
                    await response.read()
                timings.append((time.perf_counter() - started) * 1000.0)
    finally:
        await runner.cleanup()
        if temporary is not None:
            temporary.cleanup()
    return {
        "mean_ms": round(statistics.fmean(timings), 3),
        "p95_ms": round(percentile(timings, 0.95), 3),
        "p99_ms": round(percentile(timings, 0.99), 3),
        "n": len(timings),
    }


async def transport_latency(requests: int) -> dict[str, object]:
    plain = await _transport_sample(tls=False, requests=requests)
    tls = await _transport_sample(tls=True, requests=requests)
    return {
        "http": plain,
        "https": tls,
        "warm_tls_p95_overhead_ms": round(float(tls["p95_ms"]) - float(plain["p95_ms"]), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-dsn", required=True)
    parser.add_argument("--files", type=int, default=500)
    parser.add_argument("--queries", type=int, default=200)
    args = parser.parse_args()

    local = build_memory_backend(content_size_cap=1_048_576)
    local_org = "org_latency_local_" + uuid.uuid4().hex[:8]
    local_view, local_queries = seed(local, org=local_org, count=args.files)
    local_result = query_latency(
        local, org=local_org, view_id=local_view, queries=local_queries, repetitions=args.queries
    )

    hot = build_memory_backend(content_size_cap=1_048_576)
    metadata = PostgresMetadataStore(args.postgres_dsn)
    hosted = build_postgres_metadata_hybrid(
        hot,
        metadata,
        content_size_cap=1_048_576,
        inline_rebuild_budget=5_000,
    )
    hosted_org = "org_latency_hosted_" + uuid.uuid4().hex[:8]
    try:
        hosted_view, hosted_queries = seed(hosted, org=hosted_org, count=args.files)
        hosted_result = query_latency(
            hosted,
            org=hosted_org,
            view_id=hosted_view,
            queries=hosted_queries,
            repetitions=args.queries,
        )
    finally:
        hosted.close_backend()
        local.close_backend()

    transport = asyncio.run(transport_latency(args.queries))
    payload = {
        "files": args.files,
        "queries": args.queries,
        "local_hot": local_result,
        "hosted_postgres_authority": hosted_result,
        "postgres_p95_delta_ms": round(float(hosted_result["p95_ms"]) - float(local_result["p95_ms"]), 3),
        "transport": transport,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
