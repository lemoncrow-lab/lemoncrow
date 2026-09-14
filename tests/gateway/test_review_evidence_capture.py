from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.pro.capabilities.review.evidence_capture import record_agent_evidence
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.models import ProvenanceRecord
from lemoncrow.pro.capabilities.review.packet import PacketBuild, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    (root / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "base", tree, [])


def _exact_build(repo_root: Path, store_root: Path, session_id: str, *, staged: bool = False) -> PacketBuild:
    rng = resolve_rev_range(repo_root, staged=staged, working_tree=not staged)
    build = build_review_packet_with_blobs(
        repo_root,
        rng,
        store_root=store_root,
        with_patch_text=True,
        unbounded_patch_text=True,
    )
    provenance = ProvenanceRecord(
        status="matched",
        host="claude",
        model="claude-opus-test",
        session_id=session_id,
        certainty="exact",
        match_confidence=1.0,
        match_reason="test exact session",
    )
    return replace(build, packet=replace(build.packet, provenance=provenance))


def test_agent_evidence_is_sidecar_only_then_imports_into_the_matching_revision(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    (repo_root / "app.py").write_text("def app():\n    return 2\n", encoding="utf-8")
    (repo_root / "proof.png").write_bytes(b"PNG-proof")
    store_root = tmp_path / "store"

    captured = record_agent_evidence(
        store_root,
        repo_root,
        host="claude",
        session_id="session-1",
        model="claude-opus-test",
        entries=[
            {
                "file": "proof.png",
                "path": "app.py",
                "title": "Focused tests",
                "kind": "screenshot",
                "status": "PASS",
                "detail": "12 tests passed",
            }
        ],
    )
    assert len(captured) == 1
    assert not (store_root / DB_NAME).exists(), "evidence capture must not create/advance review state"

    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, working_tree=True)
    build = _exact_build(repo_root, store_root, "session-1")
    session = open_or_create_session(store, repo_root, rng, title="evidence")
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

    (evidence,) = store.list_evidence(session.id)
    assert evidence.revision_id == revision.id
    assert evidence.source == "agent"
    assert evidence.kind == "screenshot"
    assert evidence.path == "app.py"
    assert evidence.source_ref.startswith("capture:cap-")
    assert evidence.verification_status == "PASS"
    assert evidence.detail == "12 tests passed"
    assert store.read_evidence_artifact(evidence) == b"PNG-proof"

    # Same exact revision does not duplicate imported proof.
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    assert len(store.list_evidence(session.id)) == 1


def test_agent_evidence_imports_into_a_staged_review(tmp_path: Path) -> None:
    """An agent that stages its work before capturing proof must still match.

    ``source_state`` builds per-path identity from the index for a staged range
    and from the worktree otherwise, so the two fingerprints are structurally
    different over identical bytes. Stamping only the worktree one meant
    ``lc review --staged`` imported nothing and said nothing about why.
    """

    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    (repo_root / "app.py").write_text("def app():\n    return 2\n", encoding="utf-8")
    (repo_root / "proof.png").write_bytes(b"PNG-proof")
    repo = pygit2.Repository(str(repo_root))
    repo.index.add_all()
    repo.index.write()
    store_root = tmp_path / "store"

    (captured,) = record_agent_evidence(
        store_root,
        repo_root,
        host="claude",
        session_id="session-staged",
        model="claude-opus-test",
        entries=[{"file": "proof.png", "path": "app.py", "title": "Focused tests", "status": "PASS"}],
    )
    assert captured.staged_fingerprint
    assert captured.staged_fingerprint != captured.source_fingerprint

    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, staged=True)
    build = _exact_build(repo_root, store_root, "session-staged", staged=True)
    session = open_or_create_session(store, repo_root, rng, title="staged evidence")
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

    (evidence,) = store.list_evidence(session.id)
    assert evidence.revision_id == revision.id
    assert evidence.source == "agent"
    assert evidence.verification_status == "PASS"
    assert store.read_evidence_artifact(evidence) == b"PNG-proof"


def test_loopback_preview_capture_uses_local_chrome_and_freezes_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    store_root = tmp_path / "store"

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.evidence_capture.shutil.which", lambda _name: "/chrome")

    def fake_run(argv: list[str], **_kwargs: Any) -> None:
        shot = next(item.split("=", 1)[1] for item in argv if item.startswith("--screenshot="))
        Path(shot).write_bytes(b"captured-pixels")

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.evidence_capture.subprocess.run", fake_run)
    (record,) = record_agent_evidence(
        store_root,
        repo_root,
        host="claude",
        session_id="session-2",
        entries=[{"url": "http://127.0.0.1:3000/review", "capture": True, "title": "Review workspace"}],
    )
    assert record.kind == "screenshot"
    assert record.url == ""
    assert record.mime_type == "image/png"
    assert record.bytes == len(b"captured-pixels")
    assert record.content_hash


def test_automatic_capture_refuses_external_urls(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    with pytest.raises(ValueError, match="limited to loopback"):
        record_agent_evidence(
            tmp_path / "store",
            repo_root,
            host="claude",
            session_id="session-3",
            entries=[{"url": "https://example.com", "capture": True}],
        )


def test_mcp_review_evidence_records_without_opening_a_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    (repo_root / "proof.txt").write_text("verification output", encoding="utf-8")
    store_root = tmp_path / "store"
    monkeypatch.setenv("LEMONCROW_ROOT", str(store_root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo_root))
    monkeypatch.setattr(mcp_server, "_resolved_host_session", lambda: ("claude-session", "claude"))
    monkeypatch.setattr(mcp_server, "_get_mcp_model", lambda: "claude-opus-test")
    monkeypatch.setattr(mcp_server, "_session_worktree_root", lambda _root: None)

    result = mcp_server.TOOLS["review_evidence"]["handler"](
        {"entries": [{"file": "proof.txt", "path": "app.py", "title": "Verification output"}]}
    )
    assert result["status"] == "recorded"
    assert result["count"] == 1
    assert result["session_id"] == "claude-session"
    assert not (store_root / DB_NAME).exists()
