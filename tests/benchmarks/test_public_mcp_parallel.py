from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _ensure_benchmarks_package() -> None:
    import benchmarks

    benchmark_paths = list(getattr(benchmarks, "__path__", []))
    root_path = str(ROOT / "benchmarks")
    src_path = str(ROOT / "src" / "benchmarks")
    for path in (root_path, src_path):
        if path not in benchmark_paths:
            benchmark_paths.append(path)
    benchmarks.__path__ = benchmark_paths

    mcp_pkg = sys.modules.get("benchmarks.mcp_tools")
    if mcp_pkg is None:
        mcp_pkg = types.ModuleType("benchmarks.mcp_tools")
        sys.modules["benchmarks.mcp_tools"] = mcp_pkg
    mcp_paths = list(getattr(mcp_pkg, "__path__", []))
    root_mcp_path = str(ROOT / "benchmarks" / "mcp_tools")
    if root_mcp_path not in mcp_paths:
        mcp_paths.append(root_mcp_path)
    mcp_pkg.__path__ = mcp_paths


def _load(module_name: str) -> ModuleType:
    _ensure_benchmarks_package()
    # Clear dependency modules so source edits take effect on reload
    for cached in list(sys.modules):
        if cached.startswith("benchmarks.mcp_tools."):
            sys.modules.pop(cached, None)
    return importlib.import_module(module_name)


EXPORTER = _load("benchmarks.mcp_tools.export_public_mcp_csv")


def test_plan_suite_shards_covers_each_suite_once() -> None:
    shards = EXPORTER._plan_suite_shards(None, jobs=3)

    flattened = [name for shard in shards for name in shard]
    expected = [name for name, _size, _runner in EXPORTER._suite_specs()]

    assert sorted(flattened) == sorted(expected)
    assert len(flattened) == len(set(flattened))


def test_plan_suite_shards_rejects_unknown_suite() -> None:
    with pytest.raises(ValueError, match="Unknown MCP suite"):
        EXPORTER._plan_suite_shards(["unknown-suite"], jobs=2)


def test_resolve_jobs_uses_full_cpu_up_to_suite_count(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = [(f"suite-{index}", 1, lambda _root, _progress: []) for index in range(12)]
    monkeypatch.setattr(EXPORTER, "_select_suite_specs", lambda _suite_names: specs)
    monkeypatch.setattr(EXPORTER.os, "cpu_count", lambda: 64)

    assert EXPORTER._resolve_jobs(0, None) == 12


def test_render_shard_progress_is_single_status_line(tmp_path: Path) -> None:
    status_file = tmp_path / "shard-1.status.json"
    status_file.write_text(
        (
            '{"current":"search search/example","done":3,"shard":"shard-1",'
            '"status":"running","title":"running","total":5,"updated_at":1}'
        ),
        encoding="utf-8",
    )

    status = EXPORTER._render_shard_progress(
        {1: ["search"]},
        {1: status_file},
        completed_shards=0,
        total_shards=1,
        total_cases=5,
    )

    assert "\n" not in status
    assert "cases 3/5" in status
    assert "shard-1 [search] 3/5 running" in status


def test_select_suite_specs_expands_code_alias() -> None:
    specs = EXPORTER._select_suite_specs(["code"])
    names = [name for name, _size, _runner in specs]

    # "symbols" was merged into "search" — not a separate suite name
    assert "search" in names
    assert "node" in names
    assert "callers" in names
    assert "code" not in names


def test_summarize_rows_adds_total_row() -> None:
    summary = EXPORTER._summarize_rows(
        [
            {
                "tool": "search",
                "passed": True,
                "baseline_tokens": 100,
                "tokens_saved": 25,
                "effective_tokens": 75,
                "savings_pct": 25.0,
            },
            {
                "tool": "search",
                "passed": False,
                "baseline_tokens": 120,
                "tokens_saved": 20,
                "effective_tokens": 100,
                "savings_pct": 16.67,
            },
        ]
    )

    assert summary[0]["tool"] == "search"
    assert summary[0]["cases"] == 2
    assert summary[-1]["tool"] == "TOTAL"
    assert summary[-1]["passed"] == 1


def test_repo_workspace_root_uses_cached_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    snapshot_root = tmp_path / "snapshot"
    calls: list[tuple[Path, Path, str, str]] = []

    monkeypatch.setattr(EXPORTER, "_REPO_SNAPSHOT_ROOT", None)
    monkeypatch.setattr(EXPORTER, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(EXPORTER, "default_benchmark_root", lambda root: root.parent / "benchmarks")
    monkeypatch.setattr(EXPORTER, "repo_cache_key", lambda root: "abc123")

    def fake_prepare(repo: Path, cache_root: Path, *, name: str, cache_key: str) -> Path:
        calls.append((repo, cache_root, name, cache_key))
        return snapshot_root

    monkeypatch.setattr(EXPORTER, "prepare_cached_repo_snapshot", fake_prepare)

    assert EXPORTER._repo_workspace_root() == snapshot_root
    assert EXPORTER._repo_workspace_root() == snapshot_root
    assert calls == [
        (
            repo_root,
            repo_root.parent / "benchmarks" / "mcp-cache" / "snapshots",
            "public-mcp-repo",
            "abc123",
        )
    ]


def test_flatten_reports_adds_case_input_and_stable_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _load("benchmarks.mcp_tools.harness")
    monkeypatch.setattr(EXPORTER, "_repo_root", lambda: tmp_path / "repo")

    case = harness.BenchCase(
        op="bash",
        label="shell/example",
        args={"command": f"cat {tmp_path}/repo/src/example.py"},
        baseline_tokens=100,
    )
    result = harness.CaseResult(
        case=case,
        response={"ok": True},
        lemoncrow_tokens=25,
        baseline_tokens=100,
        quality_score=1.0,
        input_file_tokens=0,
        baseline_commands=[f"cat {tmp_path}/example.py"],
        spill_probe_tokens=0,
        spill_probe_hits=0,
        elapsed_ms=12.5,
        passed=True,
    )

    rows = EXPORTER._flatten_reports([harness.ToolReport(tool_name="bash", results=[result])])

    assert rows[0]["case_input"].startswith("cat ")
    assert "$REPO_ROOT" in rows[0]["stable_args_json"]
    assert "$TMP" in rows[0]["baseline_commands_json"]
