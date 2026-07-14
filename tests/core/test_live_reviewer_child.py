from __future__ import annotations

import os
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.live_reviewer import child, sink
from lemoncrow.pro.capabilities.live_reviewer import runner as runner_mod
from lemoncrow.pro.capabilities.live_reviewer import settings as settings_mod
from lemoncrow.pro.capabilities.live_reviewer.settings import ReviewerSettings

_PATCH_VERDICT = {
    "verdict": "NEEDS_FIX",
    "findings": [
        {"type": "patch", "file": "f.py", "old_string": "alpha = 1", "new_string": "alpha = 2", "reason": "fix"}
    ],
}


def _wire(monkeypatch: pytest.MonkeyPatch, repo: Path, settings: ReviewerSettings) -> None:
    monkeypatch.setenv("LEMONCROW_IN_REVIEW", "0")  # owned by monkeypatch so child's mutation is undone
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo))
    monkeypatch.setattr(runner_mod, "run_review", lambda *a, **k: dict(_PATCH_VERDICT))
    monkeypatch.setattr(settings_mod, "load_reviewer_settings", lambda root: settings)


def test_live_pass_auto_applies_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("alpha = 1\n", encoding="utf-8")
    _wire(monkeypatch, repo, ReviewerSettings(live_reviewer=True, auto_apply=True))

    assert child.main(["--session", "s1", "--mode", "live", "--path", "f.py", "--root", str(tmp_path)]) == 0

    assert (repo / "f.py").read_text(encoding="utf-8") == "alpha = 2\n"  # auto-applied
    latest = sink.latest_verdict(tmp_path, "s1")
    assert latest is not None and latest.get("auto_applied", {}).get("count") == 1


def test_deep_pass_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("alpha = 1\n", encoding="utf-8")
    _wire(monkeypatch, repo, ReviewerSettings(deep_edit_count_reviewer=True, auto_apply=True))

    child.main(["--session", "s1", "--mode", "deep", "--path", "f.py", "--root", str(tmp_path)])

    assert (repo / "f.py").read_text(encoding="utf-8") == "alpha = 1\n"  # deep = read-only, never applied


def test_auto_apply_disabled_is_review_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("alpha = 1\n", encoding="utf-8")
    _wire(monkeypatch, repo, ReviewerSettings(live_reviewer=True, auto_apply=False))

    child.main(["--session", "s1", "--mode", "live", "--path", "f.py", "--root", str(tmp_path)])

    assert (repo / "f.py").read_text(encoding="utf-8") == "alpha = 1\n"  # auto_apply off -> not applied


def test_single_flight_blocks_concurrent_then_reclaims(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = child._claim_review_lock(str(tmp_path), "s1", str(repo))
    assert first is not None
    # a second reviewer for the same (session, repo) is turned away while the first runs
    assert child._claim_review_lock(str(tmp_path), "s1", str(repo)) is None
    first.unlink()
    # once released, the slot is reclaimable
    assert child._claim_review_lock(str(tmp_path), "s1", str(repo)) is not None


def test_single_flight_reclaims_stale_lock(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lock = child._lock_path(str(tmp_path), "s1", str(repo))
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("2147483646", encoding="utf-8")  # a pid that is not alive
    claimed = child._claim_review_lock(str(tmp_path), "s1", str(repo))
    assert claimed is not None and int(claimed.read_text("utf-8")) == os.getpid()  # stale lock reclaimed
