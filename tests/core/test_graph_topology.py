"""WS11 G17 -- module-topology discovery over the file import graph."""

from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def _build(repo: Path, cache: Path) -> SemanticFileMemoryCapability:
    pkg_a = repo / "pkg_a"
    pkg_b = repo / "pkg_b"
    pkg_a.mkdir(parents=True)
    pkg_b.mkdir(parents=True)
    (pkg_a / "__init__.py").write_text("", encoding="utf-8")
    (pkg_b / "__init__.py").write_text("", encoding="utf-8")
    (pkg_b / "base.py").write_text("def base() -> int:\n    return 1\n", encoding="utf-8")
    (pkg_a / "uses.py").write_text(
        "from pkg_b.base import base\n\ndef use() -> int:\n    return base()\n",
        encoding="utf-8",
    )
    cap = SemanticFileMemoryCapability(cache)
    for f in (pkg_b / "base.py", pkg_a / "uses.py"):
        cap.summarize_file(f)
    return cap


def test_topology_clusters_modules(tmp_path: Path) -> None:
    cap = _build(tmp_path / "repo", tmp_path / "cache")
    result = cap.graph_analytics().topology()
    assert result["module_count"] >= 2
    mods = {m["module"] for m in result["modules"]}
    assert any(m.endswith("/pkg_a") for m in mods)
    assert any(m.endswith("/pkg_b") for m in mods)
    assert "hotspots" in result


def test_topology_is_safe_on_empty_index(tmp_path: Path) -> None:
    cap = SemanticFileMemoryCapability(tmp_path / "cache")
    result = cap.graph_analytics().topology()
    assert result["module_count"] == 0
    assert result["modules"] == []
