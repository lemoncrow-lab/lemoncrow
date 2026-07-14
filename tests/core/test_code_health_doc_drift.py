"""WS10 G15 -- doc-vs-code drift / design-gap detection.

Builds a tiny on-disk module so the symbol index has real symbols, then checks
that doc references to missing/renamed symbols are flagged while valid ones are
not.
"""

from __future__ import annotations

from pathlib import Path

from lemoncrow.pro.capabilities.code_health.doc_drift import (
    DocDriftAnalyzer,
    design_gaps,
    verify_design,
)
from lemoncrow.pro.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def _build_indexed_repo(repo: Path, cache_root: Path) -> SemanticFileMemoryCapability:
    src = repo / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "service.py").write_text(
        "class OrderService:\n"
        "    def place_order(self, item: str, qty: int) -> int:\n"
        "        return qty\n\n"
        "def compute_total(price: float, count: int) -> float:\n"
        "    return price * count\n",
        encoding="utf-8",
    )
    cap = SemanticFileMemoryCapability(cache_root)
    cap.summarize_file(src / "service.py")
    return cap


def test_design_gaps_flags_missing_symbol_not_valid_one(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cap = _build_indexed_repo(repo, tmp_path / "cache")
    doc = repo / "docs" / "design.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "# Design\n\n"
        "The `OrderService` handles ordering via `place_order`.\n"
        "It also exposes `compute_total` for pricing.\n"
        "A future `LegacyDispatcher` will be wired in.\n"  # missing symbol
        "We may call `process_refund` someday.\n"  # missing symbol
        "This is just prose with words like design and service.\n",  # prose, not symbols
        encoding="utf-8",
    )

    analyzer = DocDriftAnalyzer(cap)
    result = analyzer.design_gaps([doc])

    flagged = {f["symbol"] for f in result["gaps"]}
    # Missing/aspirational doc references are flagged.
    assert "LegacyDispatcher" in flagged
    assert "process_refund" in flagged
    # Valid symbols present in the index are NOT flagged.
    assert "OrderService" not in flagged
    assert "place_order" not in flagged
    assert "compute_total" not in flagged
    # Bare prose words are never treated as symbols.
    assert "design" not in flagged
    assert "service" not in flagged
    assert result["gap_count"] == len(result["gaps"])
    # Findings carry doc + line provenance.
    for finding in result["gaps"]:
        assert finding["doc"].endswith("design.md")
        assert finding["line"] >= 1


def test_verify_design_flags_signature_drift_not_matching_signature(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cap = _build_indexed_repo(repo, tmp_path / "cache")
    doc = repo / "docs" / "api.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "# API\n\n"
        "Call `compute_total(price, count)` to total a line.\n"  # matches index
        "Older docs say `place_order(sku, quantity, warehouse)`.\n",  # drifted params
        encoding="utf-8",
    )

    analyzer = DocDriftAnalyzer(cap)
    result = analyzer.verify_design([doc])

    drifted = {f["symbol"]: f for f in result["drifts"]}
    # place_order's documented params (sku/quantity/warehouse) differ from the
    # indexed (item/qty) -> drift.
    assert "place_order" in drifted
    assert drifted["place_order"]["kind"] == "signature_drift"
    # compute_total's documented params match the index -> no drift.
    assert "compute_total" not in drifted


def test_design_gaps_entry_point_fail_open_on_missing_paths(tmp_path: Path) -> None:
    # No docs anywhere -> structurally valid empty result, never raises.
    result = design_gaps(
        repo_root=tmp_path / "nope",
        lemoncrow_root=tmp_path / "cache",
        paths=["does_not_exist.md"],
    )
    assert result["kind"] == "design_gaps"
    assert result["gap_count"] == 0
    assert result["gaps"] == []


def test_verify_design_entry_point_reports_kind(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build_indexed_repo(repo, tmp_path / "cache")
    doc = repo / "docs" / "d.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# D\n\n`place_order(sku, quantity, warehouse)`\n", encoding="utf-8")
    result = verify_design(
        repo_root=repo,
        lemoncrow_root=tmp_path / "cache",
        paths=["docs/d.md"],
    )
    assert result["kind"] == "verify_design"
    assert result["drift_count"] >= 1
