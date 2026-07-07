from __future__ import annotations

from pathlib import Path


def test_runtime_source_does_not_export_stub_embedding() -> None:
    source_root = Path("src/atelier")
    offenders = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "stub_embedding" in text:
            offenders.append(str(path))
    assert offenders == []
