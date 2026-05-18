from __future__ import annotations

from pathlib import Path

REQUIRED_TOPICS = {
    "stub_embedding",
    "make bench-savings-honest",
    "ATELIER_MEMORY_BACKEND=letta",
    "consolidate",
}


REQUIRED_MATRIX_AREAS = {
    "Runtime embeddings",
    "Savings benchmark",
    "Memory backend",
    "Sleeptime summaries",
    "Tool output",
    "Repo context",
    "Memory updates",
}


def test_v2_to_v3_migration_guide_covers_operator_steps() -> None:
    path = Path("docs/migrations/v2-to-v3.md")
    if not path.exists():
        path = Path("docs-archive/migrations/v2-to-v3.md")
    text = path.read_text(encoding="utf-8")

    for topic in REQUIRED_TOPICS:
        assert topic in text


def test_v2_to_v3_deprecation_matrix_covers_changed_surfaces() -> None:
    path = Path("docs/migrations/v2-to-v3-deprecation-matrix.md")
    if not path.exists():
        path = Path("docs-archive/migrations/v2-to-v3-deprecation-matrix.md")
    text = path.read_text(encoding="utf-8")

    for area in REQUIRED_MATRIX_AREAS:
        assert area in text


def test_changelog_links_to_migration_docs() -> None:
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert "docs/migrations/v2-to-v3.md" in changelog or "docs-archive/migrations/v2-to-v3.md" in changelog
    assert (
        "docs/migrations/v2-to-v3-deprecation-matrix.md" in changelog
        or "docs-archive/migrations/v2-to-v3-deprecation-matrix.md" in changelog
    )
