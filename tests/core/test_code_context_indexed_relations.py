from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.code_context import CodeContextEngine


def _write_repo(root: Path) -> None:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "worker.py").write_text(
        "def run_command(cmd: str) -> int:\n"
        "    return len(cmd)\n\n"
        "def classify_command(cmd: str) -> str:\n"
        "    run_command(cmd)\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (root / "src" / "pkg" / "server.py").write_text(
        "from pkg.worker import run_command\n\n"
        "def _run_shell_tool(command: str) -> int:\n"
        "    return run_command(command)\n",
        encoding="utf-8",
    )


def test_indexed_usages_callers_and_callees(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    engine = CodeContextEngine(tmp_path)
    engine.tool_index(include_globs=["src/**/*.py"], budget_tokens=2000)

    usages = engine.tool_usages(symbol_name="run_command", limit=20, group_by="none", budget_tokens=2000)
    assert usages["reference_count"] >= 2
    assert "server.py" in str(usages)

    callers = engine.tool_callers(symbol_name="run_command", limit=20, budget_tokens=2000)
    assert callers["data_status"] == "available"
    assert callers["edge_count"] >= 1
    assert "_run_shell_tool" in str(callers) or "classify_command" in str(callers)

    callees = engine.tool_callees(symbol_name="classify_command", limit=20, budget_tokens=2000)
    assert callees["data_status"] == "available"
    assert "run_command" in str(callees)


def test_native_python_pattern_search_without_ast_grep(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.py").write_text(
        "from somewhere import mcp_tool\n\n" "@mcp_tool(name='code')\n" "def tool_code() -> None:\n" "    pass\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path)
    engine.tool_index(include_globs=["src/**/*.py"], budget_tokens=2000)

    result = engine.tool_pattern(pattern="@mcp_tool($$$)", language="python", limit=10, budget_tokens=1000)
    assert result["total_matches"] >= 1
    assert "mcp_tool" in str(result)


def test_native_python_pattern_search_supports_call_def_and_class_shapes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "patterns.py").write_text(
        "from somewhere import benchmark\n\n"
        "@benchmark(tag='x')\n"
        "def run_job(payload: dict[str, object]) -> None:\n"
        "    helper(payload)\n"
        "    client.helper(payload)\n\n"
        "class Worker(BaseWorker):\n"
        "    pass\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path)
    engine.tool_index(include_globs=["src/**/*.py"], budget_tokens=2000)

    decorator_matches = engine.tool_pattern(pattern="@benchmark($$$)", language="python", limit=10, budget_tokens=1000)
    def_matches = engine.tool_pattern(pattern="def run_job(...)", language="python", limit=10, budget_tokens=1000)
    class_matches = engine.tool_pattern(pattern="class Worker(...)", language="python", limit=10, budget_tokens=1000)
    call_matches = engine.tool_pattern(pattern="helper($$$)", language="python", limit=10, budget_tokens=1000)

    assert decorator_matches["total_matches"] == 1
    assert decorator_matches["matches"][0]["captures"] == {"decorator": "benchmark"}

    assert def_matches["total_matches"] == 1
    assert def_matches["matches"][0]["captures"] == {"name": "run_job"}

    assert class_matches["total_matches"] == 1
    assert class_matches["matches"][0]["captures"] == {"name": "Worker"}

    assert call_matches["total_matches"] == 2
    assert [match["line"] for match in call_matches["matches"]] == sorted(
        match["line"] for match in call_matches["matches"]
    )
    assert set(call_matches["matches"][0]) <= {
        "file_path",
        "line",
        "column",
        "end_line",
        "end_column",
        "snippet",
        "captures",
    }


def test_src_layout_import_impact(tmp_path: Path) -> None:
    (tmp_path / "src" / "atelier" / "core").mkdir(parents=True)
    (tmp_path / "src" / "atelier" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "atelier" / "core" / "bash_exec.py").write_text(
        "def run_command(cmd: str) -> int:\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "atelier" / "gateway.py").write_text(
        "from atelier.core.bash_exec import run_command\n\n" "def go() -> int:\n    return run_command('x')\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path)
    engine.tool_index(include_globs=["src/**/*.py"], budget_tokens=2000)
    impact = engine.tool_impact("src/atelier/core/bash_exec.py", budget_tokens=1000)
    assert "gateway.py" in str(impact)
    assert impact["target_type"] == "file"
    assert any(row["file_path"].endswith("gateway.py") for row in impact["affected_files"])


def test_symbol_impact_groups_affected_files(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    engine = CodeContextEngine(tmp_path)
    engine.tool_index(include_globs=["src/**/*.py"], budget_tokens=2000)

    impact = engine.tool_impact(query="run_command", budget_tokens=1000)

    assert impact["target_type"] == "symbol"
    assert impact["target"]["type"] == "symbol"
    assert impact["target"]["match_count"] >= 1
    assert "src/pkg/server.py" in impact["direct_importers"]
    assert any(row["file_path"] == "src/pkg/server.py" for row in impact["affected_files"])
    assert any("reference" in row["reasons"] or "caller" in row["reasons"] for row in impact["affected_files"])
