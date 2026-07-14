from __future__ import annotations

from pathlib import Path

from lemoncrow.pro.capabilities.live_reviewer.sink import (
    append_verdict,
    latest_unconsumed,
    latest_verdict,
    mark_consumed,
    read_verdicts,
)


def test_append_and_read(tmp_path: Path) -> None:
    append_verdict(tmp_path, "s1", {"verdict": "NEEDS_FIX", "paths": ["a.py"]})
    append_verdict(tmp_path, "s1", {"verdict": "DONE"})
    rows = read_verdicts(tmp_path, "s1")
    assert len(rows) == 2
    latest = latest_verdict(tmp_path, "s1")
    assert latest is not None
    assert latest["verdict"] == "DONE"


def test_latest_unconsumed_and_mark(tmp_path: Path) -> None:
    append_verdict(tmp_path, "s1", {"verdict": "NEEDS_FIX"})
    assert len(latest_unconsumed(tmp_path, "s1")) == 1
    mark_consumed(tmp_path, "s1")
    assert latest_unconsumed(tmp_path, "s1") == []
    assert read_verdicts(tmp_path, "s1")[0]["consumed"] is True


def test_missing_log_is_safe(tmp_path: Path) -> None:
    assert read_verdicts(tmp_path, "nope") == []
    assert latest_verdict(tmp_path, "nope") is None
    mark_consumed(tmp_path, "nope")  # must not raise
