"""Corpus-driven external benchmark matrix across comparable tool surfaces.

Creates a 1000-case workload from the live repository and runs it across the
external tools on comparable operation families:
  - exact symbol lookup
  - exact identifier search
  - substring search
  - file outline
  - no-hit search
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import select
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from atelier.gateway.cli.progress import ProgressReporter

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.bench_external_indexers import (
    SerenaRunner,
    cache_lock,
    default_benchmark_root,
    ensure_code_index_checkout,
    ensure_code_index_runtime,
    external_workspace_root,
    install_external_tools,
    prepare_cached_repo_snapshot,
    prepare_repo_snapshot,
    repo_cache_key,
    run_cmd,
    token_count,
)
from benchmarks.mcp_tools.external_matrix_cases import (
    DEFAULT_CASE_QUOTAS,
    ExternalBenchCase,
    load_case_manifest,
    write_case_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

SURFACE_AUDIT: dict[str, list[dict[str, str | bool]]] = {
    "atelier": [
        {"surface": "symbol", "family": "exact_symbol", "benchmarked": True},
        {"surface": "search:exact", "family": "exact_search", "benchmarked": True},
        {"surface": "search:substring", "family": "substring_search", "benchmarked": True},
        {"surface": "outline", "family": "file_outline", "benchmarked": True},
        {"surface": "search:nohit", "family": "nohit_search", "benchmarked": True},
        {"surface": "usages", "family": "graph", "benchmarked": False},
        {"surface": "callers", "family": "graph", "benchmarked": False},
        {"surface": "callees", "family": "graph", "benchmarked": False},
        {"surface": "impact", "family": "graph", "benchmarked": False},
    ],
    "atelier-zoekt": [
        {"surface": "search:exact", "family": "exact_search", "benchmarked": True},
        {"surface": "search:substring", "family": "substring_search", "benchmarked": True},
        {"surface": "search:nohit", "family": "nohit_search", "benchmarked": True},
    ],
    "zoekt": [
        {"surface": "search:exact", "family": "exact_search", "benchmarked": True},
        {"surface": "search:substring", "family": "substring_search", "benchmarked": True},
        {"surface": "search:nohit", "family": "nohit_search", "benchmarked": True},
    ],
    "atelier-serena": [
        {"surface": "search_for_pattern:exact:compact", "family": "exact_search", "benchmarked": True},
        {
            "surface": "search_for_pattern:substring:compact",
            "family": "substring_search",
            "benchmarked": True,
        },
        {"surface": "search_for_pattern:nohit:compact", "family": "nohit_search", "benchmarked": True},
    ],
    "serena": [
        {"surface": "find_symbol", "family": "exact_symbol", "benchmarked": True},
        {"surface": "search_for_pattern:exact", "family": "exact_search", "benchmarked": True},
        {
            "surface": "search_for_pattern:substring",
            "family": "substring_search",
            "benchmarked": True,
        },
        {"surface": "get_symbols_overview", "family": "file_outline", "benchmarked": True},
        {"surface": "search_for_pattern:nohit", "family": "nohit_search", "benchmarked": True},
        {"surface": "find_referencing_symbols", "family": "graph", "benchmarked": False},
    ],
    "atelier-codegraph": [
        {"surface": "query:search:compact", "family": "exact_search", "benchmarked": True},
        {"surface": "query:substring:compact", "family": "substring_search", "benchmarked": True},
        {"surface": "query:nohit:compact", "family": "nohit_search", "benchmarked": True},
    ],
    "codegraph": [
        {"surface": "query:exact", "family": "exact_symbol", "benchmarked": True},
        {"surface": "query:search", "family": "exact_search", "benchmarked": True},
        {"surface": "query:substring", "family": "substring_search", "benchmarked": True},
        {"surface": "query:nohit", "family": "nohit_search", "benchmarked": True},
        {"surface": "callers", "family": "graph", "benchmarked": False},
        {"surface": "callees", "family": "graph", "benchmarked": False},
        {"surface": "impact", "family": "graph", "benchmarked": False},
    ],
    "code-index-mcp": [
        {"surface": "search_code:exact", "family": "exact_search", "benchmarked": True},
        {"surface": "search_code:substring", "family": "substring_search", "benchmarked": True},
        {"surface": "analyze_file", "family": "file_outline", "benchmarked": True},
        {"surface": "search_code:nohit", "family": "nohit_search", "benchmarked": True},
    ],
    "cocoindex-code": [
        {"surface": "search:exact", "family": "exact_search", "benchmarked": True},
        {"surface": "search:substring", "family": "substring_search", "benchmarked": True},
        {"surface": "search:nohit", "family": "nohit_search", "benchmarked": True},
    ],
    "jcodemunch-mcp": [
        {"surface": "search_symbols", "family": "exact_symbol", "benchmarked": True},
        {"surface": "search_text:exact", "family": "exact_search", "benchmarked": True},
        {"surface": "search_text:substring", "family": "substring_search", "benchmarked": True},
        {"surface": "get_file_outline", "family": "file_outline", "benchmarked": True},
        {"surface": "search_text:nohit", "family": "nohit_search", "benchmarked": True},
        {"surface": "find_references", "family": "graph", "benchmarked": False},
        {"surface": "get_call_hierarchy", "family": "graph", "benchmarked": False},
        {"surface": "get_blast_radius", "family": "graph", "benchmarked": False},
    ],
}

TOOL_SUPPORT: dict[str, set[str]] = {
    tool: {cast(str, row["family"]) for row in rows if bool(row["benchmarked"])} for tool, rows in SURFACE_AUDIT.items()
}

CACHE_SCHEMA = "provider-cache-v1"
DEFAULT_PROVIDER_TOOLS = (
    "atelier",
    "atelier-zoekt",
    "zoekt",
    "atelier-serena",
    "serena",
    "atelier-codegraph",
    "codegraph",
    "code-index-mcp",
    "jcodemunch-mcp",
)


def _provider_cache_marker(snapshot_root: Path, tool_name: str) -> Path:
    return snapshot_root / f".atelier-{tool_name}-cache.json"


def _provider_cache_ready(snapshot_root: Path, tool_name: str, cache_key: str) -> bool:
    marker_path = _provider_cache_marker(snapshot_root, tool_name)
    if not marker_path.is_file():
        return False
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("cache_schema") == CACHE_SCHEMA
        and payload.get("cache_key") == cache_key
    )


def _write_provider_cache_marker(snapshot_root: Path, tool_name: str, cache_key: str) -> None:
    _provider_cache_marker(snapshot_root, tool_name).write_text(
        json.dumps(
            {"cache_schema": CACHE_SCHEMA, "cache_key": cache_key, "tool": tool_name},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _prepare_provider_snapshot(
    repo_root: Path,
    workspace_root: Path,
    *,
    tool_name: str,
    cache_root: Path | None,
    cache_key: str,
) -> Path:
    tool_workspace = external_workspace_root(workspace_root)
    if cache_root is None:
        return prepare_repo_snapshot(repo_root, tool_workspace, f"{tool_name}-matrix")
    return prepare_cached_repo_snapshot(
        repo_root,
        cache_root / "snapshots" / tool_name,
        name=f"{tool_name}-matrix",
        cache_key=cache_key,
    )


@dataclass
class CaseBenchResult:
    case_id: str
    family: str
    tool: str
    status: str
    correctness: float
    median_ms: float
    p95_ms: float
    median_tokens: int
    runs: int
    query: str
    input: str = ""
    output: str = ""
    error: str = ""


class ProviderShardStatusReporter(ProgressReporter):
    """Child-process progress reporter that writes provider status JSON."""

    def __init__(self, shard_name: str, total: int, status_file: Path) -> None:
        super().__init__("providers", total=total, heartbeat_seconds=0)
        self.shard_name = shard_name
        self.status_file = status_file

    def _emit(self, title: str) -> None:
        self._last_title = title
        payload = {
            "shard": self.shard_name,
            "status": "complete" if self.total and self.done >= self.total else "running",
            "title": title,
            "current": self.current,
            "done": self.done,
            "total": self.total or 0,
            "updated_at": time.time(),
        }
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.status_file.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.status_file)


class _RunnerBase:
    tool_name: str
    supported_families: set[str]

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        raise NotImplementedError


class AtelierRunner(_RunnerBase):
    tool_name = "atelier"
    supported_families = TOOL_SUPPORT[tool_name]

    def __init__(self, repo_root: Path, workspace_root: Path, *, cache_root: Path | None, cache_key: str) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        self.cache_root = cache_root
        self.cache_key = cache_key
        self.snapshot_root: Path | None = None
        self.tool_code: Any | None = None

    def start(self) -> None:
        if str(self.repo_root) not in sys.path:
            sys.path.insert(0, str(self.repo_root))
        tool_workspace = external_workspace_root(self.workspace_root)
        self.snapshot_root = _prepare_provider_snapshot(
            self.repo_root,
            self.workspace_root,
            tool_name=self.tool_name,
            cache_root=self.cache_root,
            cache_key=self.cache_key,
        )
        runtime_root = Path(tempfile.mkdtemp(prefix="atelier-matrix-root-", dir=tool_workspace))
        configure_benchmark_runtime(runtime_root, workspace_root=self.snapshot_root)
        from atelier.gateway.adapters.mcp_server import tool_code

        self.tool_code = tool_code

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        assert self.snapshot_root is not None and self.tool_code is not None
        if case.family == "exact_symbol":
            request = {
                "op": "symbol",
                "repo_root": str(self.snapshot_root),
                "symbol_name": case.symbol_name,
                "budget_tokens": 4000,
            }
        elif case.family in {"exact_search", "substring_search", "nohit_search"}:
            intent = "text" if case.family == "substring_search" else "symbol"
            request = {
                "op": "search",
                "repo_root": str(self.snapshot_root),
                "query": case.query,
                "mode": "lexical",
                "intent": intent,
                "limit": 20,
                "file_glob": "src/atelier/**/*.py",
                "budget_tokens": 4000,
            }
        elif case.family == "file_outline":
            request = {
                "op": "outline",
                "repo_root": str(self.snapshot_root),
                "path": case.path,
                "budget_tokens": 4000,
            }
        else:
            raise ValueError(f"unsupported family for {self.tool_name}: {case.family}")
        response = self.tool_code(request)
        return json.dumps(request, ensure_ascii=False), json.dumps(response, ensure_ascii=False)


class ZoektRunner(_RunnerBase):
    tool_name = "zoekt"
    supported_families = TOOL_SUPPORT[tool_name]

    def __init__(self, repo_root: Path, workspace_root: Path, *, cache_root: Path | None, cache_key: str) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        self.cache_root = cache_root
        self.cache_key = cache_key
        self.snapshot_root: Path | None = None
        self.supervisor: Any | None = None

    def start(self) -> None:
        if str(self.repo_root) not in sys.path:
            sys.path.insert(0, str(self.repo_root))
        tool_workspace = external_workspace_root(self.workspace_root)
        self.snapshot_root = _prepare_provider_snapshot(
            self.repo_root,
            self.workspace_root,
            tool_name=self.tool_name,
            cache_root=self.cache_root,
            cache_key=self.cache_key,
        )
        runtime_root = Path(tempfile.mkdtemp(prefix=f"{self.tool_name}-matrix-root-", dir=tool_workspace))
        configure_benchmark_runtime(runtime_root, workspace_root=self.snapshot_root)
        from atelier.infra.code_intel.zoekt.adapter import (
            get_zoekt_supervisor,
            reset_zoekt_supervisors,
        )

        reset_zoekt_supervisors()
        self.supervisor = get_zoekt_supervisor(self.snapshot_root)

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        assert self.snapshot_root is not None and self.supervisor is not None
        search_path = self.snapshot_root / "src" / "atelier"
        request = {
            "query": case.query,
            "search_path": str(search_path),
            "max_files": 20,
            "max_chars_per_file": 600,
            "include_outline": False,
            "result_mode": "expanded",
            "context_lines": 2,
            "max_snippets_per_file": 3,
            "skip_noise": False,
            "prefer_source": False,
        }
        result = self.supervisor.search(
            query=case.query,
            search_path=search_path,
            max_files=request["max_files"],
            max_chars_per_file=request["max_chars_per_file"],
            include_outline=request["include_outline"],
            result_mode="expanded",
            context_lines=2,
            max_snippets_per_file=3,
            skip_noise=False,
            prefer_source=False,
        )
        return json.dumps(request, ensure_ascii=False), json.dumps(asdict(result), ensure_ascii=False)


class AtelierZoektRunner(ZoektRunner):
    tool_name = "atelier-zoekt"
    supported_families = TOOL_SUPPORT[tool_name]

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        assert self.snapshot_root is not None and self.supervisor is not None
        search_path = self.snapshot_root / "src" / "atelier"
        request = {
            "query": case.query,
            "search_path": str(search_path),
            "max_files": 8,
            "max_chars_per_file": 160,
            "include_outline": False,
            "result_mode": "compact",
            "context_lines": 0,
            "max_snippets_per_file": 1,
            "skip_noise": True,
            "prefer_source": True,
        }
        result = self.supervisor.search(
            query=case.query,
            search_path=search_path,
            max_files=request["max_files"],
            max_chars_per_file=request["max_chars_per_file"],
            include_outline=request["include_outline"],
            result_mode="compact",
            context_lines=0,
            max_snippets_per_file=1,
            skip_noise=True,
            prefer_source=True,
        )
        return json.dumps(request, ensure_ascii=False), json.dumps(asdict(result), ensure_ascii=False)


class SerenaMatrixRunner(_RunnerBase):
    tool_name = "serena"
    supported_families = TOOL_SUPPORT[tool_name]

    def __init__(self, repo_root: Path, workspace_root: Path, *, cache_root: Path | None, cache_key: str) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        self.cache_root = cache_root
        self.cache_key = cache_key
        self.runner: SerenaRunner | None = None

    def start(self) -> None:
        tool_workspace = external_workspace_root(self.workspace_root)
        snapshot_root = _prepare_provider_snapshot(
            self.repo_root,
            self.workspace_root,
            tool_name=self.tool_name,
            cache_root=self.cache_root,
            cache_key=self.cache_key,
        )
        self.runner = SerenaRunner(project_root=snapshot_root, home_dir=tool_workspace / "serena-home")
        self.runner.bootstrap()
        self.runner.start()

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.stop()

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        assert self.runner is not None
        if case.family == "exact_symbol":
            tool_name = "find_symbol"
            params = {
                "name_path_pattern": case.symbol_name,
                "substring_matching": True,
                "max_matches": 20,
                "include_body": False,
                "depth": 0,
                "relative_path": "src/atelier",
            }
        elif case.family in {"exact_search", "substring_search", "nohit_search"}:
            tool_name = "search_for_pattern"
            params = {
                "substring_pattern": case.query,
                "relative_path": "src/atelier",
                "restrict_search_to_code_files": True,
            }
        elif case.family == "file_outline":
            tool_name = "get_symbols_overview"
            params = {"relative_path": case.path, "depth": 0}
        else:
            raise ValueError(f"unsupported family for {self.tool_name}: {case.family}")
        response = self.runner.query(tool_name, params)
        return json.dumps({"tool_name": tool_name, "params": params}, ensure_ascii=False), response


class AtelierSerenaMatrixRunner(SerenaMatrixRunner):
    tool_name = "atelier-serena"
    supported_families = TOOL_SUPPORT[tool_name]

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        request, output = super().run_case(case)
        return request, _compact_provider_payload(self.tool_name, case, output)


class CodeGraphRunner(_RunnerBase):
    tool_name = "codegraph"
    supported_families = TOOL_SUPPORT[tool_name]

    def __init__(self, repo_root: Path, workspace_root: Path, *, cache_root: Path | None, cache_key: str) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        self.cache_root = cache_root
        self.cache_key = cache_key
        self.snapshot_root: Path | None = None

    def start(self) -> None:
        self.snapshot_root = _prepare_provider_snapshot(
            self.repo_root,
            self.workspace_root,
            tool_name=self.tool_name,
            cache_root=self.cache_root,
            cache_key=self.cache_key,
        )
        assert self.snapshot_root is not None
        if not _provider_cache_ready(self.snapshot_root, self.tool_name, self.cache_key):
            lock_root = self.cache_root or self.snapshot_root.parent
            with cache_lock(lock_root / f"{self.tool_name}-{self.cache_key}.lock"):
                if not _provider_cache_ready(self.snapshot_root, self.tool_name, self.cache_key):
                    init = run_cmd(["codegraph", "init", "-i", str(self.snapshot_root)], timeout=1800)
                    if init.returncode != 0:
                        raise RuntimeError(init.stderr[:1200] or init.stdout[:1200])
                    _write_provider_cache_marker(self.snapshot_root, self.tool_name, self.cache_key)

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        assert self.snapshot_root is not None
        command = [
            "codegraph",
            "query",
            "-p",
            str(self.snapshot_root),
            "-l",
            "20",
            "-j",
            case.query,
        ]
        proc = run_cmd(command, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        return json.dumps({"command": command}, ensure_ascii=False), proc.stdout


class AtelierCodeGraphRunner(CodeGraphRunner):
    tool_name = "atelier-codegraph"
    supported_families = TOOL_SUPPORT[tool_name]

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        request, output = super().run_case(case)
        return request, _compact_provider_payload(self.tool_name, case, output)


class CodeIndexMatrixRunner(_RunnerBase):
    tool_name = "code-index-mcp"
    supported_families = TOOL_SUPPORT[tool_name]

    def __init__(
        self,
        repo_root: Path,
        workspace_root: Path,
        code_index_repo: Path,
        *,
        cache_root: Path | None,
        cache_key: str,
    ) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        self.code_index_repo = code_index_repo
        self.cache_root = cache_root
        self.cache_key = cache_key
        self.python_bin: Path | None = None
        self.snapshot_root: Path | None = None

    def start(self) -> None:
        self.snapshot_root = _prepare_provider_snapshot(
            self.repo_root,
            self.workspace_root,
            tool_name=self.tool_name,
            cache_root=self.cache_root,
            cache_key=self.cache_key,
        )
        self.code_index_repo = ensure_code_index_checkout(self.code_index_repo)
        self.python_bin = ensure_code_index_runtime(self.code_index_repo)
        assert self.snapshot_root is not None
        if not _provider_cache_ready(self.snapshot_root, self.tool_name, self.cache_key):
            lock_root = self.cache_root or self.snapshot_root.parent
            with cache_lock(lock_root / f"{self.tool_name}-{self.cache_key}.lock"):
                if not _provider_cache_ready(self.snapshot_root, self.tool_name, self.cache_key):
                    warm = self._run_script(
                        rebuild=True,
                        payload={
                            "kind": "search",
                            "pattern": "classify_command",
                            "file_pattern": "*.py",
                        },
                        timeout=1800,
                    )
                    _ = warm
                    _write_provider_cache_marker(self.snapshot_root, self.tool_name, self.cache_key)

    def _script(self, *, rebuild: bool) -> str:
        rebuild_block = (
            """
IndexManagementService(ctx).rebuild_deep_index(max_workers=4, timeout=600)
"""
            if rebuild
            else ""
        )
        return f"""
import json
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
code_index_repo = Path(sys.argv[2]).resolve()
payload = json.loads(sys.argv[3])
sys.path.insert(0, str(code_index_repo / "src"))

from code_index_mcp.project_settings import ProjectSettings
from code_index_mcp.server import CodeIndexerContext, _BootstrapRequestContext, mcp
from code_index_mcp.services.code_intelligence_service import CodeIntelligenceService
from code_index_mcp.services.index_management_service import IndexManagementService
from code_index_mcp.services.project_management_service import ProjectManagementService
from code_index_mcp.services.search_service import SearchService
from mcp.server.fastmcp import Context

lifespan = CodeIndexerContext(base_path="", settings=ProjectSettings("", skip_load=True))
ctx = Context(request_context=_BootstrapRequestContext(lifespan), fastmcp=mcp)
ProjectManagementService(ctx).initialize_project(str(repo_root))
{rebuild_block}

if payload["kind"] == "search":
    result = SearchService(ctx).search_code(
        pattern=payload["pattern"],
        regex=payload.get("regex", False),
        file_pattern=payload.get("file_pattern"),
        max_results=payload.get("max_results", 20),
        context_lines=payload.get("context_lines", 0),
        case_sensitive=payload.get("case_sensitive", False),
    )
else:
    result = CodeIntelligenceService(ctx).analyze_file(payload["file_path"])

print(json.dumps(result, ensure_ascii=False))
"""

    def _run_script(
        self,
        *,
        rebuild: bool,
        payload: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        assert self.python_bin is not None and self.snapshot_root is not None
        proc = run_cmd(
            [
                str(self.python_bin),
                "-c",
                self._script(rebuild=rebuild),
                str(self.snapshot_root),
                str(self.code_index_repo),
                json.dumps(payload, ensure_ascii=False),
            ],
            cwd=self.code_index_repo,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        result = json.loads(proc.stdout)
        assert isinstance(result, dict)
        return result

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        if case.family == "file_outline":
            payload: dict[str, object] = {"kind": "summary", "file_path": case.path}
        else:
            payload = {
                "kind": "search",
                "pattern": case.query,
                "regex": False,
                "file_pattern": "*.py",
                "max_results": 20,
                "context_lines": 0,
                "case_sensitive": False,
            }
        result = self._run_script(rebuild=False, payload=payload, timeout=300)
        return json.dumps(payload, ensure_ascii=False), json.dumps(result, ensure_ascii=False)


class CocoindexRunner(_RunnerBase):
    tool_name = "cocoindex-code"
    supported_families = TOOL_SUPPORT[tool_name]

    def __init__(self, repo_root: Path, workspace_root: Path, *, cache_root: Path | None, cache_key: str) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        self.cache_root = cache_root
        self.cache_key = cache_key
        self.snapshot_root: Path | None = None

    def start(self) -> None:
        self.snapshot_root = _prepare_provider_snapshot(
            self.repo_root,
            self.workspace_root,
            tool_name=self.tool_name,
            cache_root=self.cache_root,
            cache_key=self.cache_key,
        )
        assert self.snapshot_root is not None
        if not _provider_cache_ready(self.snapshot_root, self.tool_name, self.cache_key):
            lock_root = self.cache_root or self.snapshot_root.parent
            with cache_lock(lock_root / f"{self.tool_name}-{self.cache_key}.lock"):
                if not _provider_cache_ready(self.snapshot_root, self.tool_name, self.cache_key):
                    run_cmd(["ccc", "daemon", "stop"], cwd=self.snapshot_root, timeout=60)
                    init = run_cmd(["ccc", "init", "--force"], cwd=self.snapshot_root, timeout=300)
                    if init.returncode != 0:
                        raise RuntimeError(init.stderr[:1200] or init.stdout[:1200])
                    index = run_cmd(["ccc", "index"], cwd=self.snapshot_root, timeout=1800)
                    if index.returncode != 0:
                        raise RuntimeError(index.stderr[:1200] or index.stdout[:1200])
                    _write_provider_cache_marker(self.snapshot_root, self.tool_name, self.cache_key)

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        assert self.snapshot_root is not None
        command = ["ccc", "search", "--path", "src/**/*.py", "--limit", "20", case.query]
        proc = run_cmd(command, cwd=self.snapshot_root, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:1200] or proc.stdout[:1200])
        return json.dumps({"command": command}, ensure_ascii=False), proc.stdout

    def stop(self) -> None:
        if self.snapshot_root is None:
            return
        run_cmd(["ccc", "daemon", "stop"], cwd=self.snapshot_root, timeout=60)


class _JsonRpcLineClient:
    def __init__(self, command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.proc: subprocess.Popen[str] | None = None
        self._next_id = 1

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.command,
            cwd=str(self.cwd) if self.cwd else None,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "atelier-bench", "version": "1"},
                "capabilities": {},
            },
        )
        self.notify("notifications/initialized", {})

    def _read_message(self, *, timeout: float) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.25)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                break
            return cast(dict[str, Any], json.loads(line))
        stderr = ""
        if self.proc is not None and self.proc.stderr is not None:
            try:
                stderr = self.proc.stderr.read(2000)
            except Exception:
                stderr = ""
        raise TimeoutError(f"timed out waiting for JSON-RPC response: {stderr[:400]}")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False) + "\n"
        )
        self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any], *, timeout: float = 60) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdin is not None
        request_id = self._next_id
        self._next_id += 1
        self.proc.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                ensure_ascii=False,
            )
            + "\n"
        )
        self.proc.stdin.flush()
        while True:
            message = self._read_message(timeout=timeout)
            if message.get("id") != request_id:
                continue
            return message

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class JCodeMunchRunner(_RunnerBase):
    tool_name = "jcodemunch-mcp"
    supported_families = TOOL_SUPPORT[tool_name]

    def __init__(self, repo_root: Path, workspace_root: Path, *, cache_root: Path | None, cache_key: str) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        self.cache_root = cache_root
        self.cache_key = cache_key
        self.snapshot_root: Path | None = None
        self.client: _JsonRpcLineClient | None = None
        self.repo_id: str | None = None

    def start(self) -> None:
        self.snapshot_root = _prepare_provider_snapshot(
            self.repo_root,
            self.workspace_root,
            tool_name=self.tool_name,
            cache_root=self.cache_root,
            cache_key=self.cache_key,
        )
        assert self.snapshot_root is not None
        if not _provider_cache_ready(self.snapshot_root, self.tool_name, self.cache_key):
            lock_root = self.cache_root or self.snapshot_root.parent
            with cache_lock(lock_root / f"{self.tool_name}-{self.cache_key}.lock"):
                if not _provider_cache_ready(self.snapshot_root, self.tool_name, self.cache_key):
                    idx = run_cmd(
                        ["jcodemunch-mcp", "index", str(self.snapshot_root), "--no-ai-summaries"],
                        timeout=1800,
                    )
                    if idx.returncode != 0:
                        raise RuntimeError(idx.stderr[:1200] or idx.stdout[:1200])
                    _write_provider_cache_marker(self.snapshot_root, self.tool_name, self.cache_key)
        self.client = _JsonRpcLineClient(["jcodemunch-mcp", "serve"])
        self.client.start()
        repo_result = self._tool_call("resolve_repo", {"path": str(self.snapshot_root)})
        repo_payload = self._content_text_json(repo_result)
        self.repo_id = str(repo_payload["repo"])

    def stop(self) -> None:
        if self.client is not None:
            self.client.stop()

    def _tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self.client is not None
        response = self.client.call("tools/call", {"name": name, "arguments": arguments}, timeout=300)
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"unexpected jcodemunch response: {response}")
        if result.get("isError"):
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result

    def _content_text_json(self, result: dict[str, Any]) -> dict[str, Any]:
        content = result.get("content")
        if not isinstance(content, list) or not content:
            raise RuntimeError(f"missing content in jcodemunch result: {result}")
        entry = content[0]
        if not isinstance(entry, dict):
            raise RuntimeError(f"unexpected content entry: {entry}")
        text = entry.get("text")
        if not isinstance(text, str):
            raise RuntimeError(f"missing text content: {entry}")
        payload = json.loads(text)
        assert isinstance(payload, dict)
        return payload

    def run_case(self, case: ExternalBenchCase) -> tuple[str, str]:
        assert self.repo_id is not None
        if case.family == "exact_symbol":
            arguments = {
                "repo": self.repo_id,
                "query": case.symbol_name,
                "language": "python",
                "max_results": 10,
                "detail_level": "compact",
                "fuzzy": False,
            }
            result = self._tool_call("search_symbols", arguments)
        elif case.family in {"exact_search", "substring_search", "nohit_search"}:
            arguments = {
                "repo": self.repo_id,
                "query": case.query,
                "file_pattern": "src/atelier/**/*.py",
                "max_results": 20,
                "context_lines": 0,
            }
            result = self._tool_call("search_text", arguments)
        elif case.family == "file_outline":
            arguments = {"repo": self.repo_id, "file_path": case.path}
            result = self._tool_call("get_file_outline", arguments)
        else:
            raise ValueError(f"unsupported family for {self.tool_name}: {case.family}")
        return json.dumps(arguments, ensure_ascii=False), json.dumps(result, ensure_ascii=False)


def write_surface_audit(path: Path) -> None:
    payload = [{"tool": tool, **row} for tool, rows in SURFACE_AUDIT.items() for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".csv":
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["tool", "surface", "family", "benchmarked"],
            )
            writer.writeheader()
            writer.writerows(payload)
    else:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _balanced_case_subset(cases: list[ExternalBenchCase], max_cases: int | None) -> list[ExternalBenchCase]:
    if max_cases is None or max_cases >= len(cases):
        return cases
    buckets: dict[str, list[ExternalBenchCase]] = defaultdict(list)
    for case in cases:
        buckets[case.family].append(case)
    ordered_families = sorted(buckets)
    selected: list[ExternalBenchCase] = []
    while len(selected) < max_cases:
        advanced = False
        for family in ordered_families:
            if not buckets[family]:
                continue
            selected.append(buckets[family].pop(0))
            advanced = True
            if len(selected) == max_cases:
                break
        if not advanced:
            break
    return selected


def _payload_contains_all(payload: str, values: Iterable[str]) -> bool:
    lowered = payload.lower()
    return all(value.lower() in lowered for value in values)


def _payload_looks_empty(payload: str) -> bool:
    compact = payload.replace(" ", "").replace("\n", "")
    empty_markers = [
        '"items":[]',
        '"results":[]',
        '"matches":[]',
        '"content":[]',
        '"symbols":[]',
        '"hits":[]',
        '"found":false',
        "Noresultsfound",
        "0matches",
    ]
    return any(marker.lower() in compact.lower() for marker in empty_markers)


def _compact_provider_payload(tool: str, case: ExternalBenchCase, output: str) -> str:
    """Compact provider-native output into an Atelier-style benchmark payload."""
    if case.family == "nohit_search":
        return output[:1200]
    query = case.query.lower()
    selected: list[tuple[float, object]] = []

    def _interesting_text(value: str) -> bool:
        lowered = value.lower()
        return query in lowered or "src/atelier" in lowered

    def _append(item: object, *, path: str = "", text: str = "") -> None:
        score = _compact_item_score(query, path, text)
        selected.append((score, item))

    def _walk_path_mapping(value: dict[str, object]) -> bool:
        handled = False
        for key, item in value.items():
            if "/" not in key:
                continue
            handled = True
            snippets: list[str] = []
            if isinstance(item, list):
                for entry in item:
                    compact = " ".join(str(entry).split())
                    if compact and _interesting_text(f"{key} {compact}"):
                        snippets.append(compact[:240])
                    if len(snippets) >= 3:
                        break
            else:
                compact = " ".join(str(item).split())
                if compact:
                    snippets.append(compact[:240])
            if snippets:
                _append({"path": key, "snippets": snippets}, path=key, text=" ".join(snippets))
        return handled

    def _walk(value: object) -> None:
        if len(selected) >= 80:
            return
        if isinstance(value, dict):
            if _walk_path_mapping(value):
                return
            blob = json.dumps(value, ensure_ascii=False)
            if _interesting_text(blob):
                compact = _shrink_mapping(value)
                _append(compact, text=json.dumps(compact, ensure_ascii=False))
                return
            for child in value.values():
                _walk(child)
            return
        if isinstance(value, list):
            for child in value:
                _walk(child)
                if len(selected) >= 80:
                    break
            return
        if isinstance(value, str) and _interesting_text(value):
            compact = " ".join(value.split())[:240]
            _append(compact, text=compact)

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        _walk(parsed)
    else:
        for line in output.splitlines():
            compact = " ".join(line.split())
            if compact and _interesting_text(compact):
                _append(compact[:240], text=compact)
            if len(selected) >= 80:
                break
        if not selected:
            for line in output.splitlines()[:20]:
                compact = " ".join(line.split())[:240]
                if compact:
                    _append(compact, text=compact)
    selected_items = [item for _, item in sorted(selected, key=lambda pair: pair[0], reverse=True)[:40]]

    return json.dumps(
        {
            "provider": tool,
            "query": case.query,
            "view": "atelier-compact",
            "items": selected_items,
        },
        ensure_ascii=False,
    )


def _compact_item_score(query: str, path: str, text: str) -> float:
    score = 0.0
    haystack = f"{path} {text}".lower()
    if query and query in haystack:
        score += 2.0
    if path.startswith("src/atelier/"):
        score += 3.0
    elif path.startswith("src/"):
        score += 1.0
    if "def " in haystack and query in haystack:
        score += 2.0
    if "class " in haystack and query in haystack:
        score += 2.0
    return score


def _shrink_mapping(value: dict[str, object]) -> dict[str, object]:
    keep = {
        "file",
        "file_path",
        "filename",
        "kind",
        "line",
        "line_number",
        "line_start",
        "line_end",
        "name",
        "path",
        "qualified_name",
        "signature",
        "symbol",
        "text",
    }
    compact: dict[str, object] = {}
    for key, item in value.items():
        if key.lower() not in keep:
            continue
        if isinstance(item, str):
            compact[key] = " ".join(item.split())[:240]
        else:
            compact[key] = item
    return compact or {"text": json.dumps(value, ensure_ascii=False)[:300]}


def score_case(case: ExternalBenchCase, output: str) -> float:
    if case.family == "nohit_search":
        return 1.0 if _payload_looks_empty(output) else 0.0
    if case.family == "file_outline":
        expected_names = case.expected_names[:2]
        return 1.0 if _payload_contains_all(output, expected_names) else 0.0
    if case.family == "substring_search":
        has_expected_path = _payload_contains_all(output, case.expected_paths[:1])
        has_query_or_name = _payload_contains_all(output, [case.query]) or _payload_contains_all(
            output, case.expected_names[:1]
        )
        return 1.0 if has_expected_path and has_query_or_name else 0.0
    expected = [*case.expected_paths[:1], *case.expected_names[:1]]
    return 1.0 if _payload_contains_all(output, expected) else 0.0


def _runner_specs(
    repo_root: Path,
    workspace_root: Path,
    code_index_repo: Path,
    cache_root: Path | None,
    cache_key: str,
) -> list[tuple[str, _RunnerBase]]:
    return [
        (
            "atelier",
            AtelierRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
        (
            "atelier-zoekt",
            AtelierZoektRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
        (
            "zoekt",
            ZoektRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
        (
            "atelier-serena",
            AtelierSerenaMatrixRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
        (
            "serena",
            SerenaMatrixRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
        (
            "atelier-codegraph",
            AtelierCodeGraphRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
        (
            "codegraph",
            CodeGraphRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
        (
            "code-index-mcp",
            CodeIndexMatrixRunner(
                repo_root,
                workspace_root,
                code_index_repo,
                cache_root=cache_root,
                cache_key=cache_key,
            ),
        ),
        (
            "cocoindex-code",
            CocoindexRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
        (
            "jcodemunch-mcp",
            JCodeMunchRunner(repo_root, workspace_root, cache_root=cache_root, cache_key=cache_key),
        ),
    ]


def run_case_matrix(
    *,
    repo_root: Path,
    workspace_root: Path,
    code_index_repo: Path,
    cache_root: Path | None,
    cases: list[ExternalBenchCase],
    iterations: int,
    selected_tools: set[str],
    progress: ProgressReporter | None = None,
) -> list[CaseBenchResult]:
    results: list[CaseBenchResult] = []
    cache_key = repo_cache_key(repo_root)
    runner_specs = [
        (tool_name, runner)
        for tool_name, runner in _runner_specs(repo_root, workspace_root, code_index_repo, cache_root, cache_key)
        if tool_name in selected_tools
    ]
    units_per_case = max(iterations, 1)
    reporter = progress or ProgressReporter("providers", total=len(runner_specs) * len(cases) * units_per_case)
    reporter.start("starting provider benchmark", current=f"{len(runner_specs)} tools")
    for tool_name, runner in runner_specs:
        try:
            reporter.phase("starting provider", current=tool_name)
            runner.start()
        except Exception as exc:
            for case in cases:
                results.append(
                    CaseBenchResult(
                        case_id=case.case_id,
                        family=case.family,
                        tool=tool_name,
                        status="startup_failed",
                        correctness=0.0,
                        median_ms=0.0,
                        p95_ms=0.0,
                        median_tokens=0,
                        runs=0,
                        query=case.query,
                        error=str(exc),
                    )
                )
                reporter.step(
                    "provider startup failed",
                    current=f"{tool_name} {case.case_id}",
                    advance=units_per_case,
                )
            continue
        try:
            for case in cases:
                if case.family not in runner.supported_families:
                    results.append(
                        CaseBenchResult(
                            case_id=case.case_id,
                            family=case.family,
                            tool=tool_name,
                            status="unsupported",
                            correctness=0.0,
                            median_ms=0.0,
                            p95_ms=0.0,
                            median_tokens=0,
                            runs=0,
                            query=case.query,
                        )
                    )
                    reporter.step(
                        "skipping unsupported case",
                        current=f"{tool_name} {case.case_id}",
                        advance=units_per_case,
                    )
                    continue
                try:
                    times: list[float] = []
                    tokens: list[int] = []
                    last_input = ""
                    last_output = ""
                    scores: list[float] = []
                    for iteration in range(iterations):
                        reporter.phase(
                            "running provider case",
                            current=(f"{tool_name} {case.family}/{case.case_id} " f"iter {iteration + 1}/{iterations}"),
                        )
                        t0 = time.perf_counter()
                        last_input, last_output = runner.run_case(case)
                        times.append((time.perf_counter() - t0) * 1000)
                        tokens.append(token_count(last_output))
                        scores.append(score_case(case, last_output))
                        reporter.step(
                            "running provider case",
                            current=(f"{tool_name} {case.family}/{case.case_id} " f"iter {iteration + 1}/{iterations}"),
                        )
                    results.append(
                        CaseBenchResult(
                            case_id=case.case_id,
                            family=case.family,
                            tool=tool_name,
                            status="ok",
                            correctness=statistics.mean(scores),
                            median_ms=statistics.median(times),
                            p95_ms=sorted(times)[int(0.95 * (len(times) - 1))],
                            median_tokens=int(statistics.median(tokens)),
                            runs=iterations,
                            query=case.query,
                            input=last_input,
                            output=last_output,
                        )
                    )
                except Exception as exc:
                    remaining_units = units_per_case - len(times)
                    results.append(
                        CaseBenchResult(
                            case_id=case.case_id,
                            family=case.family,
                            tool=tool_name,
                            status="failed",
                            correctness=0.0,
                            median_ms=0.0,
                            p95_ms=0.0,
                            median_tokens=0,
                            runs=0,
                            query=case.query,
                            error=str(exc),
                        )
                    )
                    if remaining_units > 0:
                        reporter.step(
                            "provider case failed",
                            current=f"{tool_name} {case.family}/{case.case_id}",
                            advance=remaining_units,
                        )
        finally:
            reporter.phase("stopping provider", current=tool_name)
            runner.stop()
    reporter.finish("provider benchmark complete")
    return results


def _atelier_better_pct(
    *,
    atelier_value: float,
    provider_value: float,
    higher_is_better: bool,
) -> str:
    if provider_value == 0:
        if atelier_value == provider_value:
            return "+0.0%"
        atelier_is_better = atelier_value > provider_value if higher_is_better else atelier_value < provider_value
        return "+inf%" if atelier_is_better else "-inf%"
    if higher_is_better:
        pct = ((atelier_value - provider_value) / abs(provider_value)) * 100
    else:
        pct = ((provider_value - atelier_value) / abs(provider_value)) * 100
    return f"{pct:+.1f}%"


def _comparison_label(atelier_score: float, provider_score: float) -> str:
    if atelier_score > provider_score:
        return "atelier better"
    if atelier_score < provider_score:
        return "atelier worse"
    return "equal"


def _add_atelier_comparisons(summary: list[dict[str, object]]) -> None:
    baselines = {row["family"]: row for row in summary if row["tool"] == "atelier"}
    for row in summary:
        baseline = baselines.get(row["family"])
        if baseline is None or int(cast(int, row["ok_cases"])) == 0:
            row["atelier_score_result"] = "n/a"
            row["atelier_score_vs_provider_pct"] = "n/a"
            row["atelier_latency_vs_provider_pct"] = "n/a"
            row["atelier_tokens_vs_provider_pct"] = "n/a"
            continue
        if row["tool"] == "atelier":
            row["atelier_score_result"] = "baseline"
            row["atelier_score_vs_provider_pct"] = "+0.0%"
            row["atelier_latency_vs_provider_pct"] = "+0.0%"
            row["atelier_tokens_vs_provider_pct"] = "+0.0%"
            continue
        atelier_score = float(cast(float, baseline["avg_correctness"]))
        provider_score = float(cast(float, row["avg_correctness"]))
        atelier_ms = float(cast(float, baseline["median_ms"]))
        provider_ms = float(cast(float, row["median_ms"]))
        atelier_tokens = float(cast(int, baseline["median_tokens"]))
        provider_tokens = float(cast(int, row["median_tokens"]))
        row["atelier_score_result"] = _comparison_label(atelier_score, provider_score)
        row["atelier_score_vs_provider_pct"] = _atelier_better_pct(
            atelier_value=atelier_score,
            provider_value=provider_score,
            higher_is_better=True,
        )
        row["atelier_latency_vs_provider_pct"] = _atelier_better_pct(
            atelier_value=atelier_ms,
            provider_value=provider_ms,
            higher_is_better=False,
        )
        row["atelier_tokens_vs_provider_pct"] = _atelier_better_pct(
            atelier_value=atelier_tokens,
            provider_value=provider_tokens,
            higher_is_better=False,
        )


def summarize_results(results: list[CaseBenchResult]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[CaseBenchResult]] = defaultdict(list)
    for result in results:
        grouped[(result.tool, result.family)].append(result)
    summary: list[dict[str, object]] = []
    for (tool, family), rows in sorted(grouped.items()):
        ok_rows = [row for row in rows if row.status == "ok"]
        unsupported = sum(1 for row in rows if row.status == "unsupported")
        failed = sum(1 for row in rows if row.status not in {"ok", "unsupported"})
        summary.append(
            {
                "tool": tool,
                "family": family,
                "cases": len(rows),
                "ok_cases": len(ok_rows),
                "unsupported_cases": unsupported,
                "failed_cases": failed,
                "avg_correctness": round(
                    statistics.mean(row.correctness for row in ok_rows) if ok_rows else 0.0,
                    4,
                ),
                "median_ms": round(
                    statistics.median(row.median_ms for row in ok_rows) if ok_rows else 0.0,
                    2,
                ),
                "median_tokens": int(statistics.median(row.median_tokens for row in ok_rows) if ok_rows else 0),
            }
        )
    _add_atelier_comparisons(summary)
    return summary


def render_summary_table(summary: list[dict[str, object]]) -> str:
    lines = [
        "| Tool | Family | Cases | OK | Unsupported | Failed | Avg correctness | Median ms | Median tokens | Atelier score | Atelier score % | Atelier latency % | Atelier tokens % |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|",
    ]
    for row in summary:
        lines.append(
            f"| {row['tool']} | {row['family']} | {row['cases']} | {row['ok_cases']} | "
            f"{row['unsupported_cases']} | {row['failed_cases']} | "
            f"{float(cast(float, row['avg_correctness'])):.2f} | "
            f"{float(cast(float, row['median_ms'])):.1f} | {int(cast(int, row['median_tokens']))} | "
            f"{row['atelier_score_result']} | {row['atelier_score_vs_provider_pct']} | "
            f"{row['atelier_latency_vs_provider_pct']} | {row['atelier_tokens_vs_provider_pct']} |"
        )
    return "\n".join(lines)


def write_case_csv(results: list[CaseBenchResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "case_id",
                "family",
                "tool",
                "status",
                "correctness",
                "median_ms",
                "p95_ms",
                "median_tokens",
                "runs",
                "query",
                "error",
                "input",
                "output",
            ],
        )
        writer.writeheader()
        for row in results:
            writer.writerow(asdict(row))


def parse_args() -> argparse.Namespace:
    return argparse.ArgumentParser(description=__doc__).parse_args()


def write_summary_csv(summary: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tool",
                "family",
                "cases",
                "ok_cases",
                "unsupported_cases",
                "failed_cases",
                "avg_correctness",
                "median_ms",
                "median_tokens",
                "atelier_score_result",
                "atelier_score_vs_provider_pct",
                "atelier_latency_vs_provider_pct",
                "atelier_tokens_vs_provider_pct",
            ],
        )
        writer.writeheader()
        for row in summary:
            writer.writerow(row)


def _to_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _read_provider_status_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return None


def _render_provider_progress(
    status_files: dict[str, Path],
    *,
    completed_shards: int,
    total_shards: int,
    total_cases: int,
) -> str:
    statuses: list[dict[str, Any]] = []
    for tool_name, status_file in sorted(status_files.items()):
        status = _read_provider_status_file(status_file)
        if status is None:
            continue
        status["tool"] = tool_name
        statuses.append(status)
    if not statuses:
        return ""
    completed_cases = sum(_to_int(status.get("done")) for status in statuses)
    parts = [f"shards {completed_shards}/{total_shards} | " f"cases {completed_cases}/{total_cases}"]
    for status in statuses:
        current = str(status.get("current") or "").strip()
        parts.append(
            f"{status['tool']} {_to_int(status.get('done'))}/"
            f"{_to_int(status.get('total'))} {status.get('status', 'running')}"
            + (f" current {current}" if current else "")
        )
    return " ; ".join(parts)


def _run_parallel_tool_matrix(
    *,
    repo_root: Path,
    workspace_root: Path,
    cache_root: Path | None,
    manifest_path: Path,
    audit_path: Path,
    code_index_repo: Path,
    iterations: int,
    max_cases: int | None,
    selected_tools: set[str],
    selected_families: set[str],
    jobs: int,
    total_units: int,
) -> list[CaseBenchResult]:
    shard_root = workspace_root / "provider-shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    log_root = shard_root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    tool_names = sorted(selected_tools)
    commands: list[tuple[str, list[str], Path, Path, Path]] = []
    status_files: dict[str, Path] = {}
    for tool_name in tool_names:
        tool_workspace_root = shard_root / tool_name
        tool_workspace_root.mkdir(parents=True, exist_ok=True)
        tool_json_out = tool_workspace_root / "results.json"
        tool_log = log_root / f"{tool_name}.log"
        status_file = shard_root / f"{tool_name}.status.json"
        status_files[tool_name] = status_file
        tool_cmd = [
            sys.executable,
            "-m",
            "benchmarks.mcp_tools.bench_external_matrix",
            "--repo-root",
            str(repo_root),
            "--workspace-root",
            str(tool_workspace_root),
            "--cache-root",
            str(cache_root.resolve()) if cache_root is not None else "",
            "--manifest-path",
            str(manifest_path),
            "--audit-path",
            str(audit_path),
            "--json-out",
            str(tool_json_out),
            "--csv-out",
            str(tool_workspace_root / "results.csv"),
            "--iterations",
            str(iterations),
            "--tools",
            tool_name,
            "--families",
            ",".join(sorted(selected_families)),
            "--jobs",
            "1",
            "--code-index-repo",
            str(code_index_repo),
            "--progress-file",
            str(status_file),
            "--shard-name",
            tool_name,
        ]
        if max_cases is not None:
            tool_cmd.extend(["--max-cases", str(max_cases)])
        commands.append((tool_name, tool_cmd, tool_json_out, tool_log, status_file))

    progress = ProgressReporter("providers", total=len(commands))
    progress.start("starting parallel provider benchmark", current=f"{len(commands)} tools x {jobs} jobs")

    def _run_child(tool_name: str, command: list[str], json_path: Path, log_path: Path) -> tuple[str, Path]:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        log_path.write_text(
            "\n".join(
                [
                    "$ " + " ".join(command),
                    "",
                    "[stdout]",
                    completed.stdout,
                    "",
                    "[stderr]",
                    completed.stderr,
                ]
            ),
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Provider shard {tool_name} failed with exit code {completed.returncode}\n"
                f"Log: {log_path}\n"
                f"STDOUT:\n{completed.stdout[-4000:]}\nSTDERR:\n{completed.stderr[-4000:]}"
            )
        if not json_path.is_file():
            raise RuntimeError(f"Provider shard {tool_name} did not produce {json_path}")
        return tool_name, json_path

    results: list[CaseBenchResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(commands))) as executor:
        futures = {
            executor.submit(_run_child, tool_name, command, json_path, log_path): tool_name
            for tool_name, command, json_path, log_path, _status_file in commands
        }
        completed_jsons: list[Path] = []
        last_snapshot = ""
        pending = set(futures)
        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                timeout=1.0,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            snapshot = _render_provider_progress(
                status_files,
                completed_shards=len(completed_jsons),
                total_shards=len(commands),
                total_cases=total_units,
            )
            if snapshot and snapshot != last_snapshot:
                progress.phase("running provider shards", current=snapshot)
                last_snapshot = snapshot
            for future in done:
                tool_name, json_path = future.result()
                completed_jsons.append(json_path)
                progress.step("provider shard complete", current=tool_name)
        for json_path in completed_jsons:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            for item in payload.get("results", []):
                results.append(CaseBenchResult(**item))
    progress.finish("parallel provider benchmark complete")
    results.sort(key=lambda item: (item.tool, item.family, item.case_id))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    repo_root_default = Path.cwd()
    workspace_root_default = default_benchmark_root(repo_root_default)
    parser.add_argument("--repo-root", type=Path, default=repo_root_default)
    parser.add_argument("--workspace-root", type=Path, default=workspace_root_default)
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument(
        "--code-index-repo",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=workspace_root_default / "external_matrix_cases.json",
    )
    parser.add_argument(
        "--audit-path",
        type=Path,
        default=workspace_root_default / "external_tool_surfaces.json",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=workspace_root_default / "bench_external_matrix.latest.json",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=workspace_root_default / "bench_external_matrix.latest.csv",
    )
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--max-cases", type=int, default=100)
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--tools", default=",".join(DEFAULT_PROVIDER_TOOLS))
    parser.add_argument(
        "--families",
        default=",".join(DEFAULT_CASE_QUOTAS),
    )
    parser.add_argument("--progress-file", default="")
    parser.add_argument("--shard-name", default="")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--write-manifest-only", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    workspace_root = args.workspace_root.resolve()
    cache_root = args.cache_root.resolve() if args.cache_root is not None else workspace_root / "_cache"
    manifest_path = args.manifest_path.resolve()
    audit_path = args.audit_path.resolve()
    json_out = args.json_out.resolve()
    csv_out = args.csv_out.resolve()
    code_index_repo = (
        args.code_index_repo.resolve() if args.code_index_repo is not None else cache_root / "code-index-mcp"
    )

    if args.install:
        install_external_tools(external_workspace_root(workspace_root))

    cases = (
        write_case_manifest(manifest_path, repo_root)
        if not manifest_path.exists()
        else load_case_manifest(manifest_path)
    )
    write_surface_audit(audit_path)
    if args.write_manifest_only:
        print(f"Wrote {len(cases)} cases to {manifest_path}")
        print(f"Wrote tool surface audit to {audit_path}")
        return

    selected_families = {family.strip() for family in str(args.families).split(",") if family.strip()}
    max_cases = args.max_cases if args.max_cases > 0 else None
    filtered_cases = [case for case in cases if case.family in selected_families]
    selected_cases = _balanced_case_subset(filtered_cases, max_cases)
    selected_tools = {tool.strip() for tool in str(args.tools).split(",") if tool.strip()}
    resolved_jobs = args.jobs
    if resolved_jobs <= 0:
        detected = max(os.cpu_count() or 1, 1)
        resolved_jobs = max(1, min(len(selected_tools), 32, detected))
    if resolved_jobs > 1 and len(selected_tools) > 1:
        results = _run_parallel_tool_matrix(
            repo_root=repo_root,
            workspace_root=workspace_root,
            cache_root=cache_root,
            manifest_path=manifest_path,
            audit_path=audit_path,
            code_index_repo=code_index_repo,
            iterations=args.iterations,
            max_cases=max_cases,
            selected_tools=selected_tools,
            selected_families=selected_families,
            jobs=resolved_jobs,
            total_units=len(selected_tools) * len(selected_cases) * max(args.iterations, 1),
        )
    else:
        progress: ProgressReporter | None = None
        if args.progress_file:
            progress = ProviderShardStatusReporter(
                str(args.shard_name or ",".join(sorted(selected_tools))),
                total=len(selected_tools) * len(selected_cases) * max(args.iterations, 1),
                status_file=Path(str(args.progress_file)).expanduser().resolve(),
            )
        results = run_case_matrix(
            repo_root=repo_root,
            workspace_root=workspace_root,
            code_index_repo=code_index_repo,
            cache_root=cache_root,
            cases=selected_cases,
            iterations=args.iterations,
            selected_tools=selected_tools,
            progress=progress,
        )
    summary = summarize_results(results)
    payload = {
        "repo_root": str(repo_root),
        "workspace_root": str(workspace_root),
        "cache_root": str(cache_root),
        "manifest_path": str(manifest_path),
        "audit_path": str(audit_path),
        "iterations": args.iterations,
        "jobs": resolved_jobs,
        "selected_cases": len(selected_cases),
        "selected_tools": sorted(selected_tools),
        "requested_tools": sorted(selected_tools),
        "executed_tools": sorted({result.tool for result in results}),
        "missing_tools": sorted(selected_tools - {result.tool for result in results}),
        "results": [asdict(result) for result in results],
        "summary": summary,
    }
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_case_csv(results, csv_out)
    write_summary_csv(summary, csv_out.with_name("summary.csv"))
    print(render_summary_table(summary))


if __name__ == "__main__":
    main()
