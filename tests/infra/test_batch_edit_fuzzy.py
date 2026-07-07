"""Integration tests for fuzzy mode in batch_edit (WP-24)."""

from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.tool_supervision.batch_edit import apply_batch_edit


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_batch_edit_fuzzy_replace_succeeds_for_indentation_drift(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    _write(target, "def f():\n\tif True:\n\t\treturn 1\n")

    old = "def f():\n    if True:\n        return 1\n"
    new = "def f():\n    if True:\n        return 2\n"

    result = apply_batch_edit(
        [
            {
                "path": str(target),
                "op": "replace",
                "old_string": old,
                "new_string": new,
                "fuzzy": True,
            }
        ],
        atomic=True,
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert result["rolled_back"] is False
    assert "return 2" in target.read_text(encoding="utf-8")


def test_batch_edit_without_fuzzy_keeps_exact_semantics(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    _write(target, "def f():\n\tif True:\n\t\treturn 1\n")

    old = "def f():\n    if True:\n        return 1\n"
    new = "def f():\n    if True:\n        return 2\n"

    result = apply_batch_edit(
        [{"path": str(target), "op": "replace", "old_string": old, "new_string": new}],
        atomic=True,
        repo_root=tmp_path,
    )

    assert len(result["failed"]) == 1
    assert "not found" in result["failed"][0]["error"].lower()
    assert target.read_text(encoding="utf-8") == "def f():\n\tif True:\n\t\treturn 1\n"


def test_batch_edit_fuzzy_ambiguity_fails_loudly_and_rolls_back(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    _write(target, "def g():\n    return 1\n\ndef g():\n    return 1\n")

    old = "def g():\n\treturn 1\n"
    new = "def g():\n    return 2\n"

    result = apply_batch_edit(
        [
            {
                "path": str(target),
                "op": "replace",
                "old_string": old,
                "new_string": new,
                "fuzzy": True,
            }
        ],
        atomic=True,
        repo_root=tmp_path,
    )

    assert result["applied"] == []
    assert result["rolled_back"] is True
    assert len(result["failed"]) == 1
    assert "ambiguous" in result["failed"][0]["error"].lower()
    assert "1-2" in result["failed"][0]["error"]
    assert "4-5" in result["failed"][0]["error"]
