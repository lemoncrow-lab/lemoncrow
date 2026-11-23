"""Sustained Runtime Lite search load on disposable Git repositories only.

Usage: PYTHONPATH=client/src python benchmarks/workspace_search_soak.py --seconds 90 --repos 40

Never targets a real repository. Samples resource growth across the same running
MCP server, checks the provenance/coverage contract on every result and reports
latency, throughput, RSS, FDs and errors; no external services or credentials.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import tempfile
import threading
import time
from pathlib import Path

from lemoncrow_client.lite.mcp import LiteMcp

from workspace_scaling import _create_workspace, _fds, _rss_mb


def run(*, seconds: float, repos: int, files: int, clients: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="lc-workspace-soak-") as scratch:
        base = Path(scratch)
        root = _create_workspace(base, repos, files)
        server = LiteMcp(
            environment={
                "LEMONCROW_WORKSPACE_ROOT": str(root),
                "LEMONCROW_HOME": str(base / "state"),
                "LEMONCROW_RUNTIME_TRACE": "off",
                "HOME": str(base),
            }
        )
        counts: dict[str, int] = {"requests": 0, "errors": 0, "partial": 0, "complete": 0}
        latencies: list[float] = []
        errors: list[str] = []
        measurements: list[dict[str, float | int | None]] = []
        lock = threading.Lock()
        deadline = 0.0
        try:
            server.prepare()
            start = time.monotonic()
            deadline = start + seconds
            measurements.append({"elapsed_s": 0, "rss_mb": _rss_mb(), "fds": _fds()})
            members = server._workspace.membership().members  # only disposable fixture members
            paths_by_owner = {int(repo.relpath[-3:]): repo.relpath for repo in members}
            assert len(paths_by_owner) == repos

            def worker(offset: int) -> None:
                index = offset
                while time.monotonic() < deadline:
                    owner = index % repos
                    # Alternate complete/fast and implicit/explicit routing.
                    mode = (index // repos) % 3
                    arguments: dict[str, object] = {
                        "query": f"stress_token_{owner:03}",
                        "coverage": "fast" if mode == 1 else "complete",
                    }
                    if mode == 2:
                        arguments["paths"] = paths_by_owner[owner]
                    began = time.perf_counter()
                    try:
                        result = server.call("code_search", arguments)
                        payload = result.get("structuredContent", {})
                        coverage = payload.get("workspace", {})
                        found = payload.get("files", [])
                        if result.get("isError") or not isinstance(coverage, dict):
                            raise ValueError("error response or absent coverage")
                        if not coverage.get("complete") and not (
                            coverage.get("skipped")
                            or coverage.get("truncated")
                            or coverage.get("building")
                            or coverage.get("failed")
                        ):
                            raise ValueError("partial response missing reason")
                        if mode != 1 and not coverage.get("complete"):
                            raise ValueError("exhaustive/scoped result is incomplete")
                        # Every fixture query has one unique owning repo. In fast
                        # partial mode a missing target must be reported as skipped.
                        target_path = paths_by_owner[owner]
                        if not found:
                            raise ValueError("no results for existing fixture token")
                        if not any(hit.get("repo") == target_path for hit in found):
                            raise ValueError("owner missing from returned results")
                        partial = not bool(coverage.get("complete"))
                    except Exception as error:
                        with lock:
                            counts["errors"] += 1
                            if len(errors) < 10:
                                errors.append(f"{type(error).__name__}: {error}")
                    else:
                        with lock:
                            counts["partial" if partial else "complete"] += 1
                    finally:
                        elapsed = (time.perf_counter() - began) * 1000
                        with lock:
                            counts["requests"] += 1
                            latencies.append(elapsed)
                    index += clients

            with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
                futures = [pool.submit(worker, offset) for offset in range(clients)]
                while time.monotonic() < deadline:
                    time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
                    measurements.append(
                        {
                            "elapsed_s": round(time.monotonic() - start, 1),
                            "rss_mb": _rss_mb(),
                            "fds": _fds(),
                        }
                    )
                for future in futures:
                    future.result(timeout=15)
            elapsed_s = time.monotonic() - start
            # Do not count in-flight SQLite descriptors as leaked handles.
            # A bounded quiet interval also lets completed refreshes release
            # their handles before the final resource observation.
            time.sleep(0.5)
            idle = {"elapsed_s": round(time.monotonic() - start, 1), "rss_mb": _rss_mb(), "fds": _fds()}
            measurements.append(idle)
            ordered = sorted(latencies)
            return {
                "repositories": repos,
                "files_per_repo": files,
                "clients": clients,
                "seconds": round(elapsed_s, 2),
                **counts,
                "throughput_rps": round(counts["requests"] / elapsed_s, 2),
                "latency_p50_ms": round(statistics.median(ordered), 2),
                "latency_p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))], 2),
                "latency_p99_ms": round(ordered[int(0.99 * (len(ordered) - 1))], 2),
                "rss_growth_mb": round(float(idle["rss_mb"]) - float(measurements[0]["rss_mb"]), 2),
                "rss_last_30s_growth_mb": round(
                    float(idle["rss_mb"])
                    - float(
                        next(
                            (sample["rss_mb"] for sample in measurements if sample["elapsed_s"] >= seconds - 30),
                            measurements[0]["rss_mb"],
                        )
                    ),
                    2,
                ),
                "fd_growth": (
                    int(measurements[-1]["fds"]) - int(measurements[0]["fds"])
                    if measurements[-1]["fds"] is not None and measurements[0]["fds"] is not None
                    else None
                ),
                "samples": measurements,
                "error_samples": errors,
            }
        finally:
            server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--repos", type=int, default=40)
    parser.add_argument("--files", type=int, default=24)
    parser.add_argument("--clients", type=int, default=8)
    args = parser.parse_args()
    if not (10 <= args.seconds <= 300 and 2 <= args.repos <= 64 and 2 <= args.files <= 100 and 1 <= args.clients <= 8):
        parser.error("seconds 10-300, repos 2-64, files 2-100, clients 1-8")
    report = run(seconds=args.seconds, repos=args.repos, files=args.files, clients=args.clients)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
