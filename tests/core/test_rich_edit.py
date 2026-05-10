from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities.tool_supervision.rich_edit import apply_rich_edits


def test_rich_edit_sequential_same_file_and_line_range(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text("first\nsecond\nthird\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "code.py#2", "old_string": "second", "new_string": "middle"},
            {"file_path": "code.py", "old_string": "middle", "new_string": "SECOND"},
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert path.read_text(encoding="utf-8") == "first\nSECOND\nthird\n"


def test_rich_edit_typography_placeholder_fuzzy_and_indent(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text("def f():\n    value = “old”\n    keep = 1\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "code.py", "old_string": 'value = "old"', "new_string": 'value = "new"'},
            {
                "file_path": "code.py",
                "old_string": "value = ...\n    keep = 1",
                "new_string": "value = 2\nkeep = 3",
            },
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    assert "    keep = 3" in path.read_text(encoding="utf-8")


def test_rich_edit_atomic_rollback_and_protected_paths(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("original\n", encoding="utf-8")
    (tmp_path / ".atelier").mkdir()
    (tmp_path / ".atelier" / "state.txt").write_text("do not touch\n", encoding="utf-8")

    result = apply_rich_edits(
        [
            {"file_path": "file.txt", "old_string": "original", "new_string": "changed"},
            {"file_path": ".atelier/state.txt", "old_string": "do", "new_string": "DO"},
        ],
        repo_root=tmp_path,
        atomic=True,
    )

    assert result["rolled_back"] is True
    assert path.read_text(encoding="utf-8") == "original\n"


def test_rich_edit_notebook_cell_operations_clear_outputs(tmp_path: Path) -> None:
    path = tmp_path / "nb.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "metadata": {},
                        "source": "print(1)",
                        "outputs": [{"name": "stdout"}],
                        "execution_count": 3,
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )

    result = apply_rich_edits(
        [
            {"file_path": "nb.ipynb#cell=0", "overwrite": True, "new_string": "print(2)"},
            {
                "file_path": "nb.ipynb#cell=0",
                "cell_action": "insert_after",
                "cell_type": "markdown",
                "new_string": "# note",
            },
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    notebook = json.loads(path.read_text(encoding="utf-8"))
    assert notebook["cells"][0]["source"] == "print(2)"
    assert notebook["cells"][0]["outputs"] == []
    assert notebook["cells"][1]["cell_type"] == "markdown"


def test_rich_edit_peer_level_def_not_indented(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text(
        "def test_foo() -> None:\n    resp = call()\n    assert resp\n",
        encoding="utf-8",
    )

    result = apply_rich_edits(
        [
            {
                "file_path": "code.py",
                "old_string": "def test_foo() -> None:\n    resp = call()\n    assert resp",
                "new_string": (
                    "def test_foo(mp) -> None:\n"
                    "    mp.setenv('X', '1')\n"
                    "    resp = call()\n"
                    "    assert resp\n"
                    "\n"
                    "\n"
                    "def test_bar(mp) -> None:\n"
                    "    mp.delenv('X', raising=False)"
                ),
            }
        ],
        repo_root=tmp_path,
    )

    assert result["failed"] == []
    text = path.read_text(encoding="utf-8")
    # peer-level def must stay at column 0
    assert "\ndef test_bar(mp) -> None:\n" in text
    # body lines must be indented
    assert "    mp.setenv" in text
