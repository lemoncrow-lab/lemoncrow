"""WS4 G3 -- file-level graph analytics (blast_radius/dead_code/cycles/coupling).

Exercises the new GraphAnalytics over a real on-disk fixture so that
summarize_file produces genuine dependency maps (resolved local imports).
"""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability
from lemoncrow.core.capabilities.semantic_file_memory.graph_analytics import GraphAnalytics


def _build_capability(repo: Path, cache_root: Path) -> SemanticFileMemoryCapability:
    """Write a small import graph and fold every file into the index.

    Graph (forward imports):
        base.py      -> (nothing)
        middle.py    -> base.py
        top.py       -> middle.py, base.py
        orphan.py    -> (nothing, nobody imports it)   [dead code candidate]
        cyclic_a.py  -> cyclic_b.py
        cyclic_b.py  -> cyclic_a.py                     [2-cycle]
        test_top.py  -> top.py                          [test, never dead]
    """
    src = repo / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "base.py").write_text(
        "def base_fn(x: int) -> int:\n    if x > 0:\n        return x\n    return 0\n",
        encoding="utf-8",
    )
    (src / "middle.py").write_text(
        "from src.base import base_fn\n\ndef middle_fn(x: int) -> int:\n    return base_fn(x) + 1\n",
        encoding="utf-8",
    )
    (src / "top.py").write_text(
        "from src.middle import middle_fn\n"
        "from src.base import base_fn\n\n"
        "def top_fn(x: int) -> int:\n"
        "    return middle_fn(x) + base_fn(x)\n",
        encoding="utf-8",
    )
    (src / "orphan.py").write_text(
        "def orphan_fn(a: int, b: int) -> int:\n"
        "    total = 0\n"
        "    for i in range(a):\n"
        "        if i % 2 == 0:\n"
        "            total += b\n"
        "    return total\n",
        encoding="utf-8",
    )
    (src / "cyclic_a.py").write_text(
        "from src.cyclic_b import b_fn\n\ndef a_fn() -> int:\n    return b_fn()\n",
        encoding="utf-8",
    )
    (src / "cyclic_b.py").write_text(
        "from src.cyclic_a import a_fn\n\ndef b_fn() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_top.py").write_text(
        "from src.top import top_fn\n\ndef test_top() -> None:\n    assert top_fn(1) >= 0\n",
        encoding="utf-8",
    )

    cap = SemanticFileMemoryCapability(cache_root)
    for py in [*sorted(src.glob("*.py")), repo / "tests" / "test_top.py"]:
        cap.summarize_file(py)
    return cap


def test_blast_radius_reports_reverse_dependency_closure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cap = _build_capability(repo, tmp_path / "cache")
    analytics = cap.graph_analytics()

    result = analytics.blast_radius(str(repo / "src" / "base.py"))
    importers = set(result["direct_importers"]) | set(result["transitive_importers"])
    # middle and top both reach base; risk tier is computed from the closure.
    assert str(repo / "src" / "middle.py") in importers
    assert str(repo / "src" / "top.py") in importers
    assert result["risk_level"] in {"low", "medium", "high", "critical"}
    # The linked test file appears in the affected-tests view.
    assert any("test_top.py" in t for t in result["affected_tests"])


def test_dead_code_flags_orphans_but_not_tests_or_entrypoints(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cap = _build_capability(repo, tmp_path / "cache")
    analytics = cap.graph_analytics()

    result = analytics.dead_code()
    dead_paths = {row["path"] for row in result["dead_files"]}
    assert str(repo / "src" / "orphan.py") in dead_paths
    # top.py is imported by the test; base/middle are imported; none are dead.
    assert str(repo / "src" / "top.py") not in dead_paths
    assert str(repo / "src" / "base.py") not in dead_paths
    # __init__.py (entrypoint) and the test file are never reported.
    assert not any("__init__.py" in p for p in dead_paths)
    assert not any("test_top.py" in p for p in dead_paths)
    # orphan carries a non-zero complexity score (loop + branch) and is ranked.
    orphan_row = next(r for r in result["dead_files"] if r["path"].endswith("orphan.py"))
    assert orphan_row["complexity_score"] >= 2


def test_cycles_detects_two_node_import_cycle(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cap = _build_capability(repo, tmp_path / "cache")
    analytics = cap.graph_analytics()

    result = analytics.cycles()
    cyclic = {str(repo / "src" / "cyclic_a.py"), str(repo / "src" / "cyclic_b.py")}
    assert result["cycle_count"] >= 1
    assert any(set(cycle) == cyclic for cycle in result["cycles"])
    # The acyclic base/middle/top chain must NOT be reported as a cycle.
    for cycle in result["cycles"]:
        assert not (str(repo / "src" / "base.py") in cycle and len(cycle) >= 2 and cyclic != set(cycle))


def test_coupling_reports_instability_metric(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cap = _build_capability(repo, tmp_path / "cache")
    analytics = cap.graph_analytics()

    result = analytics.coupling()
    by_path = {row["path"]: row for row in result["files"]}
    base = by_path[str(repo / "src" / "base.py")]
    # base is depended on (afferent>0) but depends on nothing (efferent 0): stable.
    assert base["afferent"] >= 2
    assert base["efferent"] == 0
    assert base["instability"] == 0.0
    top = by_path[str(repo / "src" / "top.py")]
    # top depends on others (efferent>0); imported only by the test.
    assert top["efferent"] >= 2
    assert 0.0 < top["instability"] <= 1.0


def test_analytics_is_pure_over_empty_index(tmp_path: Path) -> None:
    cap = SemanticFileMemoryCapability(tmp_path / "empty_cache")
    analytics = GraphAnalytics(cap._symbol_index)
    assert analytics.dead_code()["dead_file_count"] == 0
    assert analytics.cycles()["cycle_count"] == 0
    assert analytics.coupling()["coupled_file_count"] == 0
