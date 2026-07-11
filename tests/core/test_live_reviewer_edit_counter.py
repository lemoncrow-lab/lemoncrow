from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.core.capabilities.live_reviewer.edit_counter import count_file_edits


def _write_run(path: Path, events: list[dict]) -> None:
    path.write_text(json.dumps({"events": events}), encoding="utf-8")


def test_counts_only_file_edit_events(tmp_path: Path) -> None:
    run = tmp_path / "s.json"
    _write_run(
        run,
        [
            {"kind": "file_edit"},
            {"kind": "token_usage"},
            {"kind": "file_edit"},
            {"kind": "file_edit"},
        ],
    )
    assert count_file_edits(run) == 3


def test_missing_file_is_zero(tmp_path: Path) -> None:
    assert count_file_edits(tmp_path / "nope.json") == 0


def test_corrupt_file_is_zero(tmp_path: Path) -> None:
    run = tmp_path / "bad.json"
    run.write_text("{not json", encoding="utf-8")
    assert count_file_edits(run) == 0


def test_no_events_key_is_zero(tmp_path: Path) -> None:
    run = tmp_path / "e.json"
    run.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    assert count_file_edits(run) == 0
