from __future__ import annotations

from pathlib import Path
from typing import Any

import pygit2

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.rationale import record_author_rationales
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, refresh, snapshot_revision
from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore


def _init_repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    (root / "app.py").write_text("def price():\n    return 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "base", tree, [])
    return repo


def _capture(store_root: Path, repo_root: Path) -> tuple[ReviewStore, Any, Any]:
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, working_tree=True)
    build = build_review_packet_with_blobs(
        repo_root,
        rng,
        store_root=store_root,
        with_patch_text=True,
        unbounded_patch_text=True,
    )
    session = open_or_create_session(store, repo_root, rng, title="rationale")
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    return store, session, revision


def test_rationale_capture_is_sidecar_only_until_a_review_revision_is_captured(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    (repo_root / "app.py").write_text("def price():\n    return 2\n", encoding="utf-8")
    store_root = tmp_path / "store"

    records = record_author_rationales(
        store_root,
        repo_root,
        host="claude",
        session_id="session-1",
        model="claude-opus-test",
        entries=[
            {
                "path": "app.py",
                "symbol": "price",
                "title": "Why the price changed",
                "body": "The fixture now represents the post-migration value.",
            }
        ],
    )

    assert len(records) == 1
    assert not (store_root / DB_NAME).exists(), "capturing author intent must not advance/create review state"

    store, session, revision = _capture(store_root, repo_root)
    annotations = store.list_annotations(session.id)
    assert len(annotations) == 1
    annotation = annotations[0]
    assert annotation.source == "author"
    assert annotation.source_id == "session-1"
    assert annotation.title == "Why the price changed"
    assert annotation.anchor.start_line == 1
    assert "rationale:" in " ".join(annotation.evidence)
    assert revision.revision_number == 1

    # Same captured tree + same sidecar is idempotent.
    _capture(store_root, repo_root)
    assert len(store.list_annotations(session.id)) == 1


def test_author_rationale_goes_obsolete_when_the_underlying_content_changes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    (repo_root / "app.py").write_text("def price():\n    return 2\n", encoding="utf-8")
    store_root = tmp_path / "store"
    record_author_rationales(
        store_root,
        repo_root,
        host="claude",
        session_id="session-1",
        entries=[{"path": "app.py", "body": "Return 2 because the migration is complete."}],
    )
    store, session, _ = _capture(store_root, repo_root)
    original = store.list_annotations(session.id)[0]
    assert original.state == "open"

    (repo_root / "app.py").write_text("def price():\n    return 3\n", encoding="utf-8")
    refresh(store, session, repo_root, store_root=store_root)
    stale = store.get_annotation(original.id)
    assert stale is not None
    assert stale.state == "obsolete"
    assert stale.anchor_method == "stale"
    assert "underlying file content changed" in stale.anchor_detail


def test_ambiguous_identical_content_from_two_author_sessions_is_not_attributed(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    (repo_root / "app.py").write_text("def price():\n    return 2\n", encoding="utf-8")
    store_root = tmp_path / "store"
    for sid in ("session-a", "session-b"):
        record_author_rationales(
            store_root,
            repo_root,
            host="claude",
            session_id=sid,
            entries=[{"path": "app.py", "body": f"rationale from {sid}"}],
        )

    store, session, _ = _capture(store_root, repo_root)
    assert store.list_annotations(session.id) == ()


def test_mcp_review_rationale_requires_exact_claude_session_and_records_without_opening_review(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    (repo_root / "app.py").write_text("def price():\n    return 2\n", encoding="utf-8")
    store_root = tmp_path / "store"
    monkeypatch.setenv("LEMONCROW_ROOT", str(store_root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo_root))
    monkeypatch.setattr(mcp_server, "_resolved_host_session", lambda: ("claude-session", "claude"))
    monkeypatch.setattr(mcp_server, "_get_mcp_model", lambda: "claude-opus-test")
    monkeypatch.setattr(mcp_server, "_session_worktree_root", lambda _root: None)

    result = mcp_server.TOOLS["review_rationale"]["handler"](
        {"entries": [{"path": "app.py", "symbol": "price", "body": "This is the post-migration value."}]}
    )
    assert result["status"] == "recorded"
    assert result["session_id"] == "claude-session"
    assert result["count"] == 1
    assert not (store_root / DB_NAME).exists()
