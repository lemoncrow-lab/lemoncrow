"""3-way code benchmark: Atelier vs Serena vs CodeGraph."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")


def token_count(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return len(ENC.encode(text))


@dataclass(frozen=True)
class Case:
    name: str
    atelier_args: dict[str, Any]
    serena_tool: str
    serena_params: dict[str, Any]
    code_index_kind: str
    code_index_params: dict[str, Any]
    expect_text: str


CASES: list[Case] = [
    Case(
        name="symbol",
        atelier_args={"op": "symbol", "symbol_name": "classify_command", "budget_tokens": 4000},
        serena_tool="find_symbol",
        serena_params={
            "name_path_pattern": "classify_command",
            "substring_matching": True,
            "max_matches": 20,
            "include_body": False,
            "depth": 0,
            "relative_path": "src/atelier/core/capabilities/tool_supervision",
        },
        code_index_kind="summary",
        code_index_params={"file_path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py"},
        expect_text="classify_command",
    ),
    Case(
        name="usages",
        atelier_args={"op": "usages", "symbol_name": "run_command", "budget_tokens": 6000},
        serena_tool="find_referencing_symbols",
        serena_params={
            "name_path": "run_command",
            "relative_path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
        },
        code_index_kind="search",
        code_index_params={
            "pattern": r"run_command\s*\(",
            "regex": True,
            "file_pattern": "*.py",
            "max_results": 100,
        },
        expect_text="run_command",
    ),
    Case(
        name="outline",
        atelier_args={
            "op": "outline",
            "path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py",
            "budget_tokens": 4000,
        },
        serena_tool="get_symbols_overview",
        serena_params={"relative_path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py", "depth": 0},
        code_index_kind="summary",
        code_index_params={"file_path": "src/atelier/core/capabilities/tool_supervision/bash_exec.py"},
        expect_text="_strip_ansi",
    ),
    Case(
        name="search",
        atelier_args={
            "op": "search",
            "query": "classify_command",
            "mode": "lexical",
            "limit": 20,
            "budget_tokens": 4000,
        },
        serena_tool="search_for_pattern",
        serena_params={
            "substring_pattern": "classify_command",
            "relative_path": "src/atelier",
            "restrict_search_to_code_files": True,
        },
        code_index_kind="search",
        code_index_params={"pattern": "classify_command", "file_pattern": "*.py", "max_results": 50},
        expect_text="classify_command",
    ),
]


class SerenaClient:
    def __init__(self, port: int, project_name: str) -> None:
        self._port = port
        self._project_name = project_name
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["serena", "start-project-server", "--host", "127.0.0.1", "--port", str(self._port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._wait_heartbeat()

    def _wait_heartbeat(self) -> None:
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/heartbeat", timeout=2) as res:
                    if res.status == 200:
                        return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("Serena project server failed to start")

    def query(self, tool_name: str, tool_params: dict[str, Any]) -> str:
        payload = json.dumps(
            {
                "project_name": self._project_name,
                "tool_name": tool_name,
                "tool_params_json": json.dumps(tool_params),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/query_project",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as res:
            body = res.read()
            assert isinstance(body, bytes)
            return body.decode("utf-8", errors="replace")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class CodeIndexClient:
    def __init__(self, repo_root: Path, code_index_repo: Path) -> None:
        self._repo_root = repo_root
        self._code_index_repo = code_index_repo
        self._ctx: Any | None = None
        self._search_service: Any | None = None
        self._code_intel_service: Any | None = None

    def start(self) -> None:
        src_path = self._code_index_repo / "src"
        if not src_path.exists():
            raise RuntimeError(f"code-index-mcp src not found at {src_path}")
        if str(src_path) not in sys.path:
            sys.path.insert(0, str(src_path))

        from code_index_mcp.project_settings import ProjectSettings
        from code_index_mcp.server import CodeIndexerContext, _BootstrapRequestContext, mcp
        from code_index_mcp.services.code_intelligence_service import CodeIntelligenceService
        from code_index_mcp.services.index_management_service import IndexManagementService
        from code_index_mcp.services.project_management_service import ProjectManagementService
        from code_index_mcp.services.search_service import SearchService
        from mcp.server.fastmcp import Context

        lifespan = CodeIndexerContext(base_path="", settings=ProjectSettings("", skip_load=True))
        self._ctx = Context(request_context=_BootstrapRequestContext(lifespan), fastmcp=mcp)
        ProjectManagementService(self._ctx).initialize_project(str(self._repo_root))
        IndexManagementService(self._ctx).rebuild_deep_index(max_workers=4, timeout=600)
        self._search_service = SearchService(self._ctx)
        self._code_intel_service = CodeIntelligenceService(self._ctx)

    def run(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        if kind == "search":
            assert self._search_service is not None
            result = self._search_service.search_code(
                pattern=params["pattern"],
                regex=params.get("regex", False),
                file_pattern=params.get("file_pattern"),
                max_results=params.get("max_results", 10),
                context_lines=params.get("context_lines", 0),
                case_sensitive=params.get("case_sensitive", True),
            )
            assert isinstance(result, dict)
            return result
        if kind == "summary":
            assert self._code_intel_service is not None
            result = self._code_intel_service.analyze_file(params["file_path"])
            assert isinstance(result, dict)
            return result
        raise ValueError(f"unknown code-index kind: {kind}")


@dataclass
class Stats:
    median_ms: float
    p95_ms: float
    median_tokens: int
    failures: int


def summarize(times: list[float], tokens: list[int], failures: int) -> Stats:
    return Stats(
        median_ms=statistics.median(times),
        p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
        median_tokens=int(statistics.median(tokens)),
        failures=failures,
    )


def render_cell(stats: Stats) -> str:
    suffix = f" !{stats.failures}" if stats.failures else ""
    return f"{stats.median_tokens}t/{stats.median_ms:.1f}ms p95={stats.p95_ms:.1f}{suffix}"


def main() -> None:
    parser = argparse.ArgumentParser(description="3-way code benchmark (Atelier / Serena / CodeGraph)")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--serena-port", type=int, default=8041)
    parser.add_argument("--serena-project", default="atelier")
    parser.add_argument(
        "--codegraph-repo",
        "--code-index-repo",
        dest="codegraph_repo",
        default="/tmp/code-index-mcp-bench",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    if not Path(args.codegraph_repo).exists():
        raise SystemExit(
            f"Missing CodeGraph repo at {args.codegraph_repo}. Clone code-index-mcp (CodeGraph backend) first."
        )

    from atelier.gateway.adapters.mcp_server import tool_code

    serena = SerenaClient(port=args.serena_port, project_name=args.serena_project)
    code_index = CodeIndexClient(repo_root=repo_root, code_index_repo=Path(args.codegraph_repo))
    serena.start()
    code_index.start()

    try:
        print("| Case | Atelier | Serena | CodeGraph |")
        print("|---|---:|---:|---:|")
        for case in CASES:
            a_times: list[float] = []
            s_times: list[float] = []
            c_times: list[float] = []
            a_toks: list[int] = []
            s_toks: list[int] = []
            c_toks: list[int] = []
            a_fail = s_fail = c_fail = 0

            for _ in range(args.iterations):
                t0 = time.perf_counter()
                a_resp = tool_code(case.atelier_args)
                a_times.append((time.perf_counter() - t0) * 1000)
                a_toks.append(token_count(a_resp))
                if case.expect_text not in str(a_resp):
                    a_fail += 1

                t0 = time.perf_counter()
                s_resp = serena.query(case.serena_tool, case.serena_params)
                s_times.append((time.perf_counter() - t0) * 1000)
                s_toks.append(token_count(s_resp))
                if case.expect_text not in s_resp:
                    s_fail += 1

                t0 = time.perf_counter()
                c_resp = code_index.run(case.code_index_kind, case.code_index_params)
                c_times.append((time.perf_counter() - t0) * 1000)
                c_toks.append(token_count(c_resp))
                if case.expect_text not in str(c_resp):
                    c_fail += 1

            a_stats = summarize(a_times, a_toks, a_fail)
            s_stats = summarize(s_times, s_toks, s_fail)
            c_stats = summarize(c_times, c_toks, c_fail)
            print(f"| {case.name} | {render_cell(a_stats)} | {render_cell(s_stats)} | {render_cell(c_stats)} |")
    finally:
        serena.stop()


if __name__ == "__main__":
    main()
