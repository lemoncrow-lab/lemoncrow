from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.core.capabilities.live_reviewer.knowledge import (
    collect_review_context,
    load_overlay,
)


def test_load_overlay_missing_returns_empty(tmp_path: Path) -> None:
    assert load_overlay(tmp_path) == {"notes": [], "suppress": [], "boost": []}


def test_load_overlay_reads_and_filters(tmp_path: Path) -> None:
    (tmp_path / "review_overlay.json").write_text(
        json.dumps({"notes": ["a", "", "  "], "suppress": ["b"], "boost": ["c"], "junk": 1}),
        encoding="utf-8",
    )
    overlay = load_overlay(tmp_path)
    assert overlay["notes"] == ["a"]
    assert overlay["suppress"] == ["b"]
    assert overlay["boost"] == ["c"]


def test_collect_empty_when_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert collect_review_context(tmp_path, repo) == ""


def test_ensure_repo_share_gitignore(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.live_reviewer.knowledge import ensure_repo_share_gitignore

    ensure_repo_share_gitignore(tmp_path)
    gi = (tmp_path / ".lemoncrow" / ".gitignore").read_text(encoding="utf-8")
    assert "!review.json" in gi and gi.splitlines()[2] == "*"
    # Non-destructive: never clobbers a user's customised allow-list.
    (tmp_path / ".lemoncrow" / ".gitignore").write_text("custom\n", encoding="utf-8")
    ensure_repo_share_gitignore(tmp_path)
    assert (tmp_path / ".lemoncrow" / ".gitignore").read_text(encoding="utf-8") == "custom\n"


def test_collect_merges_team_and_personal_layers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    repo = tmp_path / "repo"
    (repo / ".lemoncrow").mkdir(parents=True)
    (repo / ".lemoncrow" / "review.json").write_text(
        json.dumps({"notes": ["team authz rule"], "boost": ["security"], "suppress": []}), encoding="utf-8"
    )
    (root / "review_overlay.json").write_text(
        json.dumps({"notes": ["personal style rule"], "boost": [], "suppress": ["nits"]}), encoding="utf-8"
    )
    out = collect_review_context(root, repo)
    assert "team rule: team authz rule" in out
    assert "your rule: personal style rule" in out
    assert "security" in out
    assert "nits" in out


def test_collect_merges_overlay_and_lessons(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "review_overlay.json").write_text(
        json.dumps({"notes": ["check authz"], "suppress": ["nits"], "boost": ["security"]}),
        encoding="utf-8",
    )
    blocks = tmp_path / "repo" / ".lemoncrow" / "lessons" / "blocks"
    blocks.mkdir(parents=True)
    (blocks / "l1.md").write_text("# Use DI for services\nbody text", encoding="utf-8")
    out = collect_review_context(root, tmp_path / "repo")
    assert "Repository review knowledge" in out
    assert "check authz" in out
    assert "Use DI for services" in out
    assert "security" in out
    assert "nits" in out
