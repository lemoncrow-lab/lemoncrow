"""External code-indexer benchmark in an isolated workspace.

Runs a practical cross-tool search benchmark for the indexers discussed in this
session:
  - Atelier code tool
  - Serena
  - CodeGraph
  - code-index-mcp
  - cocoindex-code (ccc)
  - mcp-vector-search
  - jcodemunch-mcp

Usage:
  uv run python benchmarks/mcp_tools/bench_external_indexers.py --install --iterations 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")


def token_count(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return len(ENC.encode(text))


def run_cmd(cmd: list[str], *, cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


@dataclass
class ToolBenchResult:
    tool: str
    ok: bool
    median_ms: float
    p95_ms: float
    median_tokens: int
    runs: int
    error: str = ""
    sample: str = ""


class SerenaRunner:
    def __init__(self, project_name: str = "atelier", port: int = 8041) -> None:
        self.project_name = project_name
        self.port = port
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["serena", "start-project-server", "--host", "127.0.0.1", "--port", str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/heartbeat", timeout=2) as res:
                    if res.status == 200:
                        return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("Serena project server failed to start")

    def query(self, tool_name: str, params: dict[str, Any]) -> str:
        payload = json.dumps(
            {
                "project_name": self.project_name,
                "tool_name": tool_name,
                "tool_params_json": json.dumps(params),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/query_project",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as res:
            body = res.read()
            assert isinstance(body, bytes)
            return body.decode("utf-8", errors="replace")

    def stop(self) -> None:
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class CodeIndexRunner:
    def __init__(self, repo_root: Path, code_index_repo: Path) -> None:
        self.repo_root = repo_root
        self.code_index_repo = code_index_repo
        self.search_service: Any = None

    def start(self) -> None:
        src_path = self.code_index_repo / "src"
        if not src_path.exists():
            raise RuntimeError(f"code-index-mcp src not found at {src_path}")
        if str(src_path) not in sys.path:
            sys.path.insert(0, str(src_path))

        from code_index_mcp.project_settings import ProjectSettings
        from code_index_mcp.server import CodeIndexerContext, _BootstrapRequestContext, mcp
        from code_index_mcp.services.index_management_service import IndexManagementService
        from code_index_mcp.services.project_management_service import ProjectManagementService
        from code_index_mcp.services.search_service import SearchService
        from mcp.server.fastmcp import Context

        lifespan = CodeIndexerContext(base_path="", settings=ProjectSettings("", skip_load=True))
        ctx = Context(request_context=_BootstrapRequestContext(lifespan), fastmcp=mcp)
        ProjectManagementService(ctx).initialize_project(str(self.repo_root))
        IndexManagementService(ctx).rebuild_deep_index(max_workers=4, timeout=600)
        self.search_service = SearchService(ctx)

    def query(self, pattern: str) -> dict[str, Any]:
        if self.search_service is None:
            raise RuntimeError("code-index-mcp not initialized")
        result = self.search_service.search_code(
            pattern=pattern,
            regex=False,
            file_pattern="*.py",
            max_results=50,
            context_lines=0,
            case_sensitive=False,
        )
        assert isinstance(result, dict)
        return result


def ensure_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)


def install_external_tools(workspace: Path) -> None:
    ensure_workspace(workspace)
    commands = [
        ["npm", "i", "-g", "@colbymchenry/codegraph"],
        ["uv", "tool", "install", "--upgrade", "mcp-vector-search"],
        ["uv", "tool", "install", "--upgrade", "cocoindex-code[full]"],
        [
            "uv",
            "tool",
            "install",
            "--upgrade",
            "https://github.com/jgravelle/jcodemunch-mcp/releases/download/v1.108.22/jcodemunch_mcp-1.108.22-py3-none-any.whl",
        ],
    ]
    for cmd in commands:
        proc = run_cmd(cmd, cwd=workspace, timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(f"install failed: {' '.join(cmd)}\n{proc.stderr[:1200]}")


def bench_atelier(query: str, iterations: int) -> ToolBenchResult:
    from atelier.gateway.adapters.mcp_server import tool_code

    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        resp = tool_code({"op": "search", "query": query, "mode": "lexical", "limit": 20, "budget_tokens": 4000})
        elapsed = (time.perf_counter() - t0) * 1000
        payload = json.dumps(resp, ensure_ascii=False)
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="atelier",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        sample=sample,
    )


def bench_serena(query: str, iterations: int) -> ToolBenchResult:
    runner = SerenaRunner()
    try:
        runner.start()
        times: list[float] = []
        toks: list[int] = []
        sample = ""
        params = {
            "substring_pattern": query,
            "relative_path": "src/atelier",
            "restrict_search_to_code_files": True,
        }
        for _ in range(iterations):
            t0 = time.perf_counter()
            resp = runner.query("search_for_pattern", params)
            elapsed = (time.perf_counter() - t0) * 1000
            times.append(elapsed)
            toks.append(token_count(resp))
            sample = resp[:280]
        return ToolBenchResult(
            tool="serena",
            ok=True,
            median_ms=statistics.median(times),
            p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
            median_tokens=int(statistics.median(toks)),
            runs=iterations,
            sample=sample,
        )
    finally:
        runner.stop()


def bench_codegraph(repo_root: Path, query: str, iterations: int) -> ToolBenchResult:
    init = run_cmd(["codegraph", "init", "-i", str(repo_root)], timeout=1800)
    if init.returncode != 0:
        raise RuntimeError(init.stderr[:1200] or init.stdout[:1200])
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        proc = run_cmd(["codegraph", "query", "-p", str(repo_root), "-l", "20", "-j", query], timeout=300)
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        payload = proc.stdout
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="codegraph",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        sample=sample,
    )


def bench_code_index(repo_root: Path, code_index_repo: Path, query: str, iterations: int) -> ToolBenchResult:
    runner = CodeIndexRunner(repo_root=repo_root, code_index_repo=code_index_repo)
    runner.start()
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        resp = runner.query(query)
        elapsed = (time.perf_counter() - t0) * 1000
        payload = json.dumps(resp, ensure_ascii=False)
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="code-index-mcp",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        sample=sample,
    )


def bench_ccc(repo_root: Path, query: str, iterations: int) -> ToolBenchResult:
    index = run_cmd(["ccc", "index"], cwd=repo_root, timeout=1800)
    if index.returncode != 0:
        raise RuntimeError(index.stderr[:1200] or index.stdout[:1200])
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        proc = run_cmd(["ccc", "search", "--path", "src/**/*.py", "--limit", "20", query], cwd=repo_root, timeout=300)
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        payload = proc.stdout
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="cocoindex-code",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        sample=sample,
    )


def bench_mvs(repo_root: Path, query: str, iterations: int) -> ToolBenchResult:
    init = run_cmd(["mcp-vector-search", "--project-root", str(repo_root), "init"], timeout=600)
    if init.returncode not in (0, 1):
        raise RuntimeError(init.stderr[:1200] or init.stdout[:1200])
    idx = run_cmd(["mcp-vector-search", "--project-root", str(repo_root), "index"], timeout=1800)
    if idx.returncode != 0:
        raise RuntimeError(idx.stderr[:1200] or idx.stdout[:1200])
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        proc = run_cmd(
            ["mcp-vector-search", "--project-root", str(repo_root), "search", "--json", "--limit", "20", query],
            timeout=300,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        payload = proc.stdout
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="mcp-vector-search",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        sample=sample,
    )


def bench_jcodemunch(repo_root: Path, iterations: int) -> ToolBenchResult:
    idx = run_cmd(["jcodemunch-mcp", "index", str(repo_root), "--no-ai-summaries"], timeout=1800)
    if idx.returncode != 0:
        raise RuntimeError(idx.stderr[:1200] or idx.stdout[:1200])
    times: list[float] = []
    toks: list[int] = []
    sample = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        proc = run_cmd(["jcodemunch-mcp", "digest", str(repo_root), "--json"], timeout=300)
        elapsed = (time.perf_counter() - t0) * 1000
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        payload = proc.stdout
        times.append(elapsed)
        toks.append(token_count(payload))
        sample = payload[:280]
    return ToolBenchResult(
        tool="jcodemunch-mcp",
        ok=True,
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(toks)),
        runs=iterations,
        sample=sample,
    )


def run_external_benchmarks(
    *,
    repo_root: Path,
    workspace_root: Path,
    code_index_repo: Path,
    query: str,
    iterations: int,
) -> list[ToolBenchResult]:
    results: list[ToolBenchResult] = []
    benches: list[tuple[str, Callable[[], ToolBenchResult]]] = [
        ("atelier", lambda: bench_atelier(query, iterations)),
        ("serena", lambda: bench_serena(query, iterations)),
        ("codegraph", lambda: bench_codegraph(repo_root, query, iterations)),
        ("code-index-mcp", lambda: bench_code_index(repo_root, code_index_repo, query, iterations)),
        ("cocoindex-code", lambda: bench_ccc(repo_root, query, iterations)),
        ("mcp-vector-search", lambda: bench_mvs(repo_root, query, iterations)),
        ("jcodemunch-mcp", lambda: bench_jcodemunch(repo_root, iterations)),
    ]
    for name, fn in benches:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(
                ToolBenchResult(
                    tool=name,
                    ok=False,
                    median_ms=0.0,
                    p95_ms=0.0,
                    median_tokens=0,
                    runs=0,
                    error=str(exc),
                )
            )
    return results


def render_table(results: list[ToolBenchResult]) -> str:
    lines = [
        "| Tool | Status | Median ms | P95 ms | Median tokens | Runs |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in results:
        status = "ok" if r.ok else "failed"
        lines.append(f"| {r.tool} | {status} | {r.median_ms:.1f} | {r.p95_ms:.1f} | {r.median_tokens} | {r.runs} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="External code-indexer benchmark runner")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--workspace-root", default=str(Path(__file__).resolve().parents[2] / ".bench-work"))
    parser.add_argument(
        "--code-index-repo",
        default=str(Path(__file__).resolve().parents[2] / ".bench-work" / "code-index-mcp"),
    )
    parser.add_argument("--query", default="classify_command")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    code_index_repo = Path(args.code_index_repo).resolve()
    ensure_workspace(workspace_root)

    if args.install:
        install_external_tools(workspace_root)

    results = run_external_benchmarks(
        repo_root=repo_root,
        workspace_root=workspace_root,
        code_index_repo=code_index_repo,
        query=args.query,
        iterations=args.iterations,
    )

    print(render_table(results))
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
