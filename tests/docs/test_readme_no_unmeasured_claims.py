from __future__ import annotations

from pathlib import Path


def test_readme_benchmarks_do_not_publish_legacy_percentage_claims() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    if "## Benchmarks" in text:
        benchmark_section = text.split("## Benchmarks", 1)[1].split("## Development", 1)[0]
    else:
        benchmark_section = text
    assert "81%" not in benchmark_section
    assert "70%" not in benchmark_section
    assert "80%" not in benchmark_section
