from __future__ import annotations

from pathlib import Path

REQUIRED_TOPICS = {
    "stub_embedding",
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


def _first_existing(*candidates: str) -> Path:
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return Path(candidates[0])


def test_v2_to_v3_migration_guide_covers_operator_steps() -> None:
    path = _first_existing(
        "docs/migrations/v2-to-v3.md",
        "docs-internal/migrations/v2-to-v3.md",
        "docs-archive/migrations/v2-to-v3.md",
    )
    text = path.read_text(encoding="utf-8")

    for topic in REQUIRED_TOPICS:
        assert topic in text


def test_v2_to_v3_deprecation_matrix_covers_changed_surfaces() -> None:
    path = _first_existing(
        "docs/migrations/v2-to-v3-deprecation-matrix.md",
        "docs-internal/migrations/v2-to-v3-deprecation-matrix.md",
        "docs-archive/migrations/v2-to-v3-deprecation-matrix.md",
    )
    text = path.read_text(encoding="utf-8")

    for area in REQUIRED_MATRIX_AREAS:
        assert area in text
