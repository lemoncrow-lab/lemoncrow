from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.delivery import ClaudeDeliveryResult, mark_feedback_addressed
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.models import ProvenanceRecord
from lemoncrow.pro.capabilities.review.packet import PacketBuild, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "feedback-loop-token"
PORT = 46001
BASE = f"http://127.0.0.1:{PORT}"
SESSION_ID = "11111111-2222-4333-8444-555555555555"


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Review Loop"
    repo.config["user.email"] = "review-loop@example.invalid"
    return repo


def _commit(repo: Any, message: str) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Review Loop", "review-loop@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, message, tree, parents)


def _settled(body: dict[str, Any]) -> list[str]:
    """Paths whose every review target the reviewer has judged reviewed.

    A verdict is recorded against the targets a path covers, never against the
    path's file unit -- a file-level claim would reopen on any edit anywhere in
    the file and so re-ask a human to re-read code that did not change. "Has
    this file been reviewed" is therefore a question about the outline's
    per-path counts, not about a file-level mark.
    """

    return sorted(
        str(row["path"])
        for row in body["outline"]
        if int(row["target_count"]) > 0 and int(row["reviewed"]) == int(row["target_count"])
    )


def _reopened(body: dict[str, Any]) -> list[str]:
    """Paths carrying at least one target whose content moved under its mark."""

    return sorted(str(row["path"]) for row in body["outline"] if int(row["changed_since_review"]) > 0)


def test_human_feedback_to_agent_revision_reopens_only_changed_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core supervision loop preserves attention already spent.

    Human reviews two files -> sends one request to the exact authoring Claude
    session -> the agent edits only ``a.py`` -> source detection reports a new
    revision without moving the review -> accepting it reopens ``a.py`` while
    ``b.py`` stays reviewed and the human comment remains human-owned/open.
    """

    repo_root = tmp_path / "repo"
    repo = _repo(repo_root)
    _write(repo_root, "src/a.py", "def a():\n    return 1\n")
    _write(repo_root, "src/b.py", "def b():\n    return 1\n")
    _commit(repo, "base")
    _write(repo_root, "src/a.py", "def a():\n    return 2\n")
    _write(repo_root, "src/b.py", "def b():\n    return 2\n")

    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, working_tree=True)
    built = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    exact = ProvenanceRecord(
        status="matched",
        host="claude",
        model="claude-opus-5",
        session_id=SESSION_ID,
        match_confidence=1.0,
        certainty="exact",
        match_reason="captured at authoring time",
    )
    build = PacketBuild(packet=replace(built.packet, provenance=exact), blobs=built.blobs)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    first = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)

    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
    )
    client = TestClient(app, base_url=BASE)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    for path in ("src/a.py", "src/b.py"):
        marked = client.post(
            f"/api/reviews/{session.id}/marks",
            headers=headers,
            json={"path": path, "state": "reviewed"},
        )
        assert marked.status_code == 200, marked.text

    annotation = client.post(
        f"/api/reviews/{session.id}/annotations",
        headers=headers,
        json={
            "path": "src/a.py",
            "start_line": 1,
            "body": "keep the public function stable while fixing the behavior",
            "kind": "request_change",
        },
    )
    assert annotation.status_code == 201, annotation.text
    annotation_id = annotation.json()["annotation"]["id"]

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.review.delivery.deliver_to_claude_session",
        lambda target, _repo, _feedback: ClaudeDeliveryResult(
            "sent",
            f"claude:{target}",
            remote_ref="deadbeef",
            message="feedback sent to the exact Claude session",
        ),
    )
    delivered = client.post(f"/api/reviews/{session.id}/feedback/claude", headers=headers)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["annotation_count"] == 1
    (record,) = store.list_deliveries(annotation_id)
    assert record.state == "sent"

    (addressed,) = mark_feedback_addressed(
        store,
        repo_root,
        [annotation_id],
        host="claude",
        session_id=SESSION_ID,
    )
    assert addressed.state == "open"
    assert addressed.author_response == "addressed"

    before_edit = client.get(f"/api/reviews/{session.id}", headers=headers).json()
    assert _settled(before_edit) == ["src/a.py", "src/b.py"]
    assert before_edit["revision"]["id"] == first.id

    # The agent changes only A. The detector must not itself advance the review.
    _write(repo_root, "src/a.py", "def a():\n    return 3\n")
    state = client.get(f"/api/reviews/{session.id}/source-state", headers=headers)
    assert state.status_code == 200
    assert state.json()["changed"] is True
    frozen = client.get(f"/api/reviews/{session.id}", headers=headers).json()
    assert frozen["revision"]["id"] == first.id
    assert _settled(frozen) == ["src/a.py", "src/b.py"]

    refreshed = client.post(f"/api/reviews/{session.id}/refresh", headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["revision"]["revision_number"] == 2
    assert _reopened(body) == ["src/a.py"]
    assert _settled(body) == ["src/b.py"]

    comments = client.get(f"/api/reviews/{session.id}/annotations", headers=headers).json()["annotations"]
    human = next(item for item in comments if item["id"] == annotation_id)
    assert human["source"] == "human"
    assert human["state"] == "open"
    assert human["author_response"] == "addressed"
    assert human["author_response_source_id"] == SESSION_ID
    assert human["anchored"] is True
    assert human["path"] == "src/a.py"

    # Accepting the new revision establishes the new quiet baseline.
    quiet = client.get(f"/api/reviews/{session.id}/source-state", headers=headers).json()
    assert quiet["changed"] is False
