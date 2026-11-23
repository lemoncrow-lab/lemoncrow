"""Opt-in, isolated Runtime Lite multi-repository scaling benchmark.

The benchmark creates disposable Git repositories under TemporaryDirectory and
never points LiteMcp at the user's real projects. It prints JSON lines, one
per measurement. Example:

    PYTHONPATH=client/src python benchmarks/workspace_scaling.py --repos 25 50 64 80
    PYTHONPATH=client/src python benchmarks/workspace_scaling.py --repos 64 --files 250

This is an in-process benchmark, not a hosted/network load test.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import resource
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from lemoncrow_client.lite.mcp import LiteMcp
from lemoncrow_client.workspace_graph import build_graph
from lemoncrow_client.workspace_members import MAX_REPOSITORIES, discover_members


def _git(directory: Path, *args: str) -> None:
    result = subprocess.run(
        [
            "git",
            "-c",
            "gc.auto=0",
            "-c",
            "user.name=Workspace Benchmark",
            "-c",
            "user.email=bench@example.invalid",
            *args,
        ],
        cwd=directory,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {args} failed: {result.stderr.decode('utf-8', 'replace')[:200]}")


def _create_workspace(folder: Path, repositories: int, files: int) -> Path:
    root = folder / "workspace"
    root.mkdir()
    for i in range(repositories):
        # Mix depths 1-3 without creating nested Git repositories.
        relpath = (
            f"group{i // 16}/area{i // 8}/repo{i:03}"
            if i % 3 == 0
            else (f"group{i // 16}/repo{i:03}" if i % 3 == 1 else f"repo{i:03}")
        )
        checkout = root / relpath
        (checkout / "src").mkdir(parents=True)
        _git(checkout, "init", "-q", "-b", "main")
        (checkout / "pyproject.toml").write_text(
            f'[project]\nname = "stress-repo-{i:03}"\nversion = "0.1"\n', encoding="utf-8"
        )
        for number in range(files):
            (checkout / "src" / f"file_{number:04}.py").write_text(
                f"stress_token_{i:03} = {number}\ndef benchmark_{i}_{number}():\n    return stress_token_{i:03}\n",
                encoding="utf-8",
            )
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-qm", "isolated stress fixture")
    return root


def _rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _fds() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
        "max_ms": round(max(values), 2),
    }


def _emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def _benchmark(repositories: int, files: int, requests: int, skip_index: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="lemoncrow-workspace-bench-") as directory:
        base = Path(directory)
        started = time.perf_counter()
        root = _create_workspace(base, repositories, files)
        _emit(
            "fixture", repositories=repositories, files_per_repo=files, seconds=round(time.perf_counter() - started, 2)
        )
        started = time.perf_counter()
        membership = discover_members(root)
        duration = (time.perf_counter() - started) * 1000
        assert len(membership.members) == min(repositories, MAX_REPOSITORIES)
        assert membership.truncated == (repositories > MAX_REPOSITORIES)
        _emit(
            "discovery",
            repositories=repositories,
            elapsed_ms=round(duration, 2),
            discovered=len(membership.members),
            truncated=membership.truncated,
        )

        for state in ("cold", "warm"):
            started = time.perf_counter()
            graph = build_graph(root)
            _emit(
                "graph",
                repositories=repositories,
                state=state,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                files_scanned=sum(member.files for member in graph.members),
                incomplete=graph.truncated,
                errors=sum(bool(member.error) for member in graph.members),
            )
            assert graph.truncated == membership.truncated
            assert not any(member.error for member in graph.members)

        if skip_index:
            return
        environment = {
            "LEMONCROW_WORKSPACE_ROOT": str(root),
            "LEMONCROW_HOME": str(base / "state"),
            "LEMONCROW_RUNTIME_TRACE": "off",
            "HOME": str(base),
        }
        server = LiteMcp(environment=environment)
        try:
            started = time.perf_counter()
            prepared = server.prepare()
            _emit(
                "index",
                repositories=repositories,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                prepared=len(prepared["repositories"]),
                rss_mb=_rss_mb(),
            )
            expected_complete = not membership.truncated

            def search(query: str, path: str | None = None, coverage_mode: str = "complete") -> float:
                args = {"query": query, "coverage": coverage_mode}
                if path:
                    args["paths"] = path
                started_at = time.perf_counter()
                answer = server.call("code_search", args)
                elapsed_ms = (time.perf_counter() - started_at) * 1000
                assert answer.get("isError") is False
                coverage = answer.get("structuredContent", {}).get("workspace", {})
                expected = expected_complete and (
                    coverage_mode != "fast" or path is not None or len(membership.members) <= 8
                )
                assert coverage.get("complete") is expected, coverage
                if coverage_mode == "fast" and not expected:
                    assert coverage.get("skipped"), coverage
                return elapsed_ms

            for mode, path, coverage_mode in (
                ("global", None, "complete"),
                ("global-fast", None, "fast"),
                ("scoped", membership.members[0].relpath, "complete"),
            ):
                samples = [search("stress_token_000", path, coverage_mode) for _ in range(10)]
                _emit("search", repositories=repositories, mode=mode, **_summary(samples))

            for clients in (1, 4, 8):
                started = time.perf_counter()
                with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
                    times = list(pool.map(lambda i: search(f"stress_token_{i % 8:03}"), range(requests)))
                elapsed = time.perf_counter() - started
                _emit(
                    "load",
                    repositories=repositories,
                    requests=requests,
                    concurrent_clients=clients,
                    throughput_rps=round(requests / elapsed, 2),
                    rss_mb=_rss_mb(),
                    open_fds=_fds(),
                    **_summary(times),
                )
        finally:
            server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", nargs="+", type=int, default=[25, 50, 64, 80])
    parser.add_argument("--files", type=int, default=24, help="source files per repository")
    parser.add_argument("--requests", type=int, default=48, help="searches at each concurrency level")
    parser.add_argument("--skip-index", action="store_true", help="only test discovery and graph scans")
    args = parser.parse_args()
    if not args.repos or any(n < 1 or n > 150 for n in args.repos) or args.files < 1 or args.files > 1000:
        parser.error("repos must be 1-150; files must be 1-1000")
    if args.requests < 8 or args.requests > 500:
        parser.error("requests must be 8-500")
    for count in args.repos:
        _benchmark(count, args.files, args.requests, args.skip_index)


if __name__ == "__main__":
    main()
