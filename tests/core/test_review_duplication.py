from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities.live_reviewer import duplication
from lemoncrow.core.capabilities.live_reviewer.duplication import added_symbols, find_duplications


def test_added_symbols_detects_decls() -> None:
    diffs = {
        "a.py": "+def compute_widget():\n+    pass\n-old",
        "b.ts": "+export function buildThing() {}\n+const helperRoutine = () => {}",
        "c.py": "+class WidgetFactory:\n+    pass",
    }
    names = added_symbols(diffs)
    assert {"compute_widget", "buildThing", "helperRoutine", "WidgetFactory"} <= set(names)


def test_added_symbols_skips_common_and_short() -> None:
    diffs = {"a.py": "+def run():\n+def go():\n+def main():"}
    assert added_symbols(diffs) == []  # run/main are common; go is too short


def test_find_duplications_flags_external_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    diffs = {"new.py": "+def compute_widget():\n+    pass"}
    monkeypatch.setattr(
        duplication,
        "_rg_defs",
        lambda repo, name: [
            f"{tmp_path / 'other.py'}:10:def compute_widget():",
            f"{tmp_path / 'new.py'}:1:def compute_widget():",
        ],
    )
    notes = find_duplications(tmp_path, diffs)
    assert len(notes) == 1
    assert "compute_widget" in notes[0]
    assert "other.py" in notes[0]
    assert "new.py" not in notes[0]  # the changed file is excluded


def test_find_duplications_none_when_only_self(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    diffs = {"new.py": "+def compute_widget():"}
    monkeypatch.setattr(duplication, "_rg_defs", lambda repo, name: [f"{tmp_path / 'new.py'}:1:def compute_widget():"])
    assert find_duplications(tmp_path, diffs) == []
