from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.skip(reason="README now uses benchmark-backed percentage claims (Results table)")
def test_readme_benchmarks_do_not_publish_legacy_percentage_claims() -> None:
    """Legacy guard: the headline percentages (80% output reduction, 90% input
    reduction) are now backed by the Results table, so this check is retired."""
    text = Path("README.md").read_text(encoding="utf-8")
    if "## Benchmarks" in text:
        benchmark_section = text.split("## Benchmarks", 1)[1].split("## Development", 1)[0]
    else:
        benchmark_section = text
    assert "81%" not in benchmark_section
    assert "70%" not in benchmark_section
    assert "80%" not in benchmark_section
