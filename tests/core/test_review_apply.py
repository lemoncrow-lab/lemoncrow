from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.live_reviewer import apply as review_apply
from lemoncrow.pro.capabilities.live_reviewer.apply import apply_review_patches, patch_findings


def test_patch_findings_filters_well_formed() -> None:
    record = {
        "findings": [
            {"type": "patch", "file": "a.py", "old_string": "x", "new_string": "y", "reason": "r"},
            {"type": "nudge", "anchor": {"file": "a.py", "line": 1}, "reason": "r"},
            {"type": "patch", "file": "b.py"},  # incomplete
            "junk",
        ]
    }
    out = patch_findings(record)
    assert len(out) == 1 and out[0]["file"] == "a.py"


def test_patch_findings_empty_inputs() -> None:
    assert patch_findings(None) == []
    assert patch_findings({}) == []
    assert patch_findings({"findings": "nope"}) == []


def test_apply_review_patches_applies_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("alpha = 1\n", encoding="utf-8")
    record = {
        "findings": [
            {"type": "patch", "file": "f.py", "old_string": "alpha = 1", "new_string": "alpha = 2", "reason": "fix"}
        ]
    }
    monkeypatch.setattr(review_apply, "latest_verdict", lambda root, sid: record)
    result = apply_review_patches(tmp_path, repo, "sid")
    assert result["count"] == 1
    assert not result["failed"]
    assert (repo / "f.py").read_text(encoding="utf-8") == "alpha = 2\n"


def test_apply_review_patches_selected_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")
    record = {
        "findings": [
            {"type": "patch", "file": "a.py", "old_string": "a = 1", "new_string": "a = 2", "reason": "r"},
            {"type": "patch", "file": "b.py", "old_string": "b = 1", "new_string": "b = 2", "reason": "r"},
        ]
    }
    monkeypatch.setattr(review_apply, "latest_verdict", lambda root, sid: record)
    result = apply_review_patches(tmp_path, repo, "sid", indices=[1])
    assert result["count"] == 1
    assert (repo / "a.py").read_text(encoding="utf-8") == "a = 1\n"  # untouched
    assert (repo / "b.py").read_text(encoding="utf-8") == "b = 2\n"


def test_apply_review_patches_none_when_no_patches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(review_apply, "latest_verdict", lambda root, sid: {"findings": []})
    result = apply_review_patches(tmp_path, tmp_path, "sid")
    assert result == {"applied": [], "failed": [], "count": 0}
