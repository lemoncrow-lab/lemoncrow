from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_benchmark_module() -> types.ModuleType:
    return _load_module(
        "bench_external_indexers_testmod",
        ROOT / "benchmarks" / "mcp_tools" / "bench_external_indexers.py",
    )


def test_bench_lemoncrow_uses_snapshot_root(monkeypatch, tmp_path: Path) -> None:
    bench = _load_benchmark_module()
    snapshot_root = tmp_path / "lemoncrow-snapshot"
    snapshot_root.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        bench,
        "prepare_repo_snapshot",
        lambda repo_root, workspace_root, name: snapshot_root,
    )

    benchmarks_pkg = types.ModuleType("benchmarks")
    benchmarks_pkg.__path__ = [str(ROOT / "benchmarks")]
    mcp_pkg = types.ModuleType("benchmarks.mcp_tools")
    mcp_pkg.__path__ = [str(ROOT / "benchmarks" / "mcp_tools")]
    env_mod = types.ModuleType("benchmarks.mcp_tools._env")

    def configure_benchmark_runtime(runtime_root: Path, *, workspace_root: Path) -> None:
        captured["runtime_root"] = runtime_root
        captured["workspace_root"] = workspace_root

    def call_code_op(request: dict[str, object]) -> dict[str, object]:
        captured["request"] = request
        return {"items": []}

    env_mod.configure_benchmark_runtime = configure_benchmark_runtime
    env_mod.call_code_op = call_code_op

    monkeypatch.setitem(sys.modules, "benchmarks", benchmarks_pkg)
    monkeypatch.setitem(sys.modules, "benchmarks.mcp_tools", mcp_pkg)
    monkeypatch.setitem(sys.modules, "benchmarks.mcp_tools._env", env_mod)

    result = bench.bench_lemoncrow(ROOT, tmp_path / "workspace", "classify_command", 1)

    assert captured["workspace_root"] == snapshot_root
    assert captured["request"] == {
        "op": "search",
        "repo_root": str(snapshot_root),
        "query": "classify_command",
        "mode": "lexical",
        "limit": 20,
        "budget_tokens": 4000,
    }
    assert '"repo_root": "' + str(snapshot_root) + '"' in result.input


def test_bench_lemoncrow_zoekt_uses_snapshot_root(monkeypatch, tmp_path: Path) -> None:
    bench = _load_benchmark_module()
    snapshot_root = tmp_path / "lemoncrow-zoekt-snapshot"
    snapshot_root.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        bench,
        "prepare_repo_snapshot",
        lambda repo_root, workspace_root, name: snapshot_root,
    )

    benchmarks_pkg = types.ModuleType("benchmarks")
    benchmarks_pkg.__path__ = [str(ROOT / "benchmarks")]
    mcp_pkg = types.ModuleType("benchmarks.mcp_tools")
    mcp_pkg.__path__ = [str(ROOT / "benchmarks" / "mcp_tools")]
    env_mod = types.ModuleType("benchmarks.mcp_tools._env")

    def configure_benchmark_runtime(runtime_root: Path, *, workspace_root: Path) -> None:
        captured["runtime_root"] = runtime_root
        captured["workspace_root"] = workspace_root

    env_mod.configure_benchmark_runtime = configure_benchmark_runtime

    @dataclass
    class FakeZoektResult:
        files: list[str]

    class FakeSupervisor:
        def search(
            self,
            *,
            query: str,
            search_path: Path,
            max_files: int,
            max_chars_per_file: int,
            include_outline: bool,
        ) -> FakeZoektResult:
            captured["search_kwargs"] = {
                "query": query,
                "search_path": search_path,
                "max_files": max_files,
                "max_chars_per_file": max_chars_per_file,
                "include_outline": include_outline,
            }
            return FakeZoektResult(files=[])

    zoekt_mod = types.ModuleType("lemoncrow.infra.code_intel.zoekt.adapter")

    def reset_zoekt_supervisors() -> None:
        captured["reset"] = True

    def get_zoekt_supervisor(repo_root: Path) -> FakeSupervisor:
        captured["supervisor_repo_root"] = repo_root
        return FakeSupervisor()

    zoekt_mod.reset_zoekt_supervisors = reset_zoekt_supervisors
    zoekt_mod.get_zoekt_supervisor = get_zoekt_supervisor

    monkeypatch.setitem(sys.modules, "benchmarks", benchmarks_pkg)
    monkeypatch.setitem(sys.modules, "benchmarks.mcp_tools", mcp_pkg)
    monkeypatch.setitem(sys.modules, "benchmarks.mcp_tools._env", env_mod)
    monkeypatch.setitem(sys.modules, "lemoncrow.infra.code_intel.zoekt.adapter", zoekt_mod)

    result = bench.bench_lemoncrow_zoekt(ROOT, tmp_path / "workspace", "classify_command", 1)

    assert captured["workspace_root"] == snapshot_root
    assert captured["supervisor_repo_root"] == snapshot_root
    assert captured["search_kwargs"] == {
        "query": "classify_command",
        "search_path": snapshot_root,
        "max_files": 20,
        "max_chars_per_file": 600,
        "include_outline": False,
    }
    assert '"search_path": "' + str(snapshot_root) + '"' in result.input
