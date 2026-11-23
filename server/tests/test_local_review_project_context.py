"""PK2 over the local server: a Review reads the history synced from its own Git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from lemoncrow_server_core.local_reviews import LocalReviewReader, LocalReviewRepository, _ResolvedReader

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.project_knowledge import ProjectKnowledgeStore, RepositoryIdentity
from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge
from lemoncrow.pro.capabilities.review.project_knowledge_surface import LocalKnowledgeSources
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

REPO_ID = "repo-pk2"
PORT = 7420
GUIDE = "<!-- generated header -->\n# Guide\n\nCharges always go through the gateway.\n"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _repo(root: Path) -> Path:
    (root / "docs").mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "PK2 Test")
    _git(root, "config", "user.email", "pk2@example.invalid")
    (root / "docs" / "guide.md").write_text(GUIDE, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    return root


def _sync(state: Path, checkout: Path, key: str = "pk2") -> RepositoryIdentity:
    repository = RepositoryIdentity("local", key)
    CanonicalGitKnowledge(checkout, repository, "refs/heads/main").sync(ProjectKnowledgeStore(ReviewStore(state)))
    LocalKnowledgeSources(state).record(repository, canonical_ref="refs/heads/main", repo_root=checkout)
    return repository


def _context(tmp_path: Path, checkout: Path) -> dict[str, Any]:
    """Capture *checkout*'s uncommitted change as a Review and read its Brief history over HTTP."""

    (checkout / "docs" / "guide.md").write_text(GUIDE + "\nRefunds too.\n", encoding="utf-8")
    repository = LocalReviewRepository(tmp_path / "reviews")
    store = repository.store(REPO_ID)
    store_root = Path(store.db_path).parent
    rng = resolve_rev_range(checkout, None, working_tree=True)
    build = build_review_packet_with_blobs(checkout, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, checkout, rng, title=build.packet.title)
    snapshot_revision(store, session, checkout, rng, store_root=store_root, build=build)
    store.update_session(session.id, repo_root=f"local:{REPO_ID}")
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    repository.set_review_checkout_root(REPO_ID, session.id, checkout)
    reader = LocalReviewReader(repository, listen_port=PORT)
    app = reader._app(_ResolvedReader(REPO_ID, store, session.id))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        response = client.get(f"/api/reviews/{session.id}/project-context", params={"purpose": "brief"})
    assert response.status_code == 200, response.text
    context: dict[str, Any] = response.json()["project_context"]
    return context


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    return root


def test_a_linked_worktree_reads_what_its_main_checkout_synced(tmp_path: Path, state: Path) -> None:
    main = _repo(tmp_path / "repo")
    _sync(state, main)
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-b", "feature", str(linked))

    context = _context(tmp_path, linked)

    assert context["available"] is True and context["authority"] == "advisory"
    row = context["entries"][0]
    assert row["subject"]["key"] == "docs/guide.md" and row["relevance"] == "changed_path"
    assert row["summary"].startswith("# Guide"), "the leading HTML comment is not what the document says"
    # The base commit is the synced commit, and Git proves it: not "unknown order".
    assert row["relation_to_change"] == "predates" and row["stale_for_snapshot"] is True
    assert row["freshness_label"] == "Landed on main · predates this Change, which edits docs/guide.md"


def test_history_landed_after_the_changes_base_is_labelled_so(tmp_path: Path, state: Path) -> None:
    main = _repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-b", "feature", str(linked))
    (main / "docs" / "guide.md").write_text(GUIDE + "\nLanded later.\n", encoding="utf-8")
    _git(main, "commit", "-am", "landed after the branch")
    _sync(state, main)

    row = _context(tmp_path, linked)["entries"][0]

    assert row["relation_to_change"] == "after_base" and row["stale_for_snapshot"] is False


def test_another_repositorys_history_is_never_read(tmp_path: Path, state: Path) -> None:
    other = _repo(tmp_path / "other")
    _sync(state, other)
    context = _context(tmp_path, _repo(tmp_path / "repo"))
    assert context["available"] is False and context["reason"] == "not_configured"
    assert context["entries"] == []


def test_two_keys_over_one_repository_bind_neither(tmp_path: Path, state: Path) -> None:
    main = _repo(tmp_path / "repo")
    _sync(state, main, "first")
    _sync(state, main, "second")
    context = _context(tmp_path, main)
    assert context["available"] is False and context["reason"] == "not_configured"


def test_without_a_sync_the_review_is_unchanged(tmp_path: Path, state: Path) -> None:
    context = _context(tmp_path, _repo(tmp_path / "repo"))
    assert context["available"] is False and context["reason"] == "not_configured"
    assert not (state / "lemoncrow_reviews.db").exists(), "reading history never creates the store"
