from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pygit2
import pytest

from lemoncrow.core.capabilities.session_comment_capture import capture_session_comment
from lemoncrow.pro.capabilities.review.api import _annotation_payload
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_comments import SessionCommentIngestor, brief_agent_context, ingestor_for
from lemoncrow.pro.capabilities.review.sources.local import (
    open_or_create_session,
    record_revision,
    refresh,
    snapshot_revision,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore


def _repo(root: Path):
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    (root / "app.py").write_text("def price():\n    return 1\n")
    repo.index.add_all()
    repo.index.write()
    sig = pygit2.Signature("Tester", "tester@example.invalid")
    repo.create_commit("HEAD", sig, sig, "base", repo.index.write_tree(), [])
    (root / "app.py").write_text("def price():\n    return 2\n")
    return repo


def _snapshot(root: Path, repo_root: Path, *, title: str = "hook comments"):
    store = ReviewStore(root)
    rng = resolve_rev_range(repo_root, working_tree=True)
    build = build_review_packet_with_blobs(
        repo_root, rng, store_root=root, with_patch_text=True, unbounded_patch_text=True
    )
    session = open_or_create_session(store, repo_root, rng, title=title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=root, build=build)
    return store, session, revision


def _next_snapshot(root: Path, repo: Path, store: ReviewStore, session):
    # Create a new Snapshot while preserving the referenced app.py bytes.
    marker = repo / "notes.txt"
    marker.write_text(str(len(store.list_revisions(session.id))))
    git = pygit2.Repository(str(repo))
    git.index.add("notes.txt")
    git.index.write()
    refresh(store, session, repo, store_root=root)
    return store.latest_revision(session.id)


def _reply(root: Path, repo: Path, sid: str, role: str, body: str):
    transcript = root.parent / f"{sid}.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": datetime.now(UTC).isoformat(),
                "message": {
                    "id": sid + "-message",
                    "model": "fixture-model",
                    "content": [{"type": "text", "text": body}],
                },
            }
        )
        + "\n"
    )
    payload = {"session_id": sid, "cwd": str(repo), "transcript_path": str(transcript), "agent_role": role}
    assert capture_session_comment(root, payload, host="claude") == 1
    return payload


def _finding(repo: Path):
    return json.dumps(
        {
            "findings": [
                {
                    "title": "Wrong price",
                    "body": "Return value needs validation.",
                    "code_location": {"absolute_file_path": str(repo / "app.py"), "line_range": {"start": 2, "end": 2}},
                }
            ],
            "overall_correctness": "patch is incorrect",
        }
    )


def test_plan_before_review_stays_discussion_without_a_claimed_file(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    body = "Plan this change.\n" + "Full visible explanation. " * 1200
    _reply(root, repo, "planner", "plan", body)
    assert not (root / "lemoncrow_reviews.db").exists()
    store, session, revision = _snapshot(root, repo)
    [comment] = store.list_discussion_comments(session.id)
    assert (comment.agent_role, comment.agent_host, comment.agent_model) == ("plan", "claude", "fixture-model")
    assert comment.source_id == "planner" and comment.source_message_id == "planner-message"
    assert comment.body == body and comment.revision_id == revision.id
    assert not comment.anchor.path
    wire = _annotation_payload(comment)
    assert wire["comment_scope"] == "change" and not wire["anchored"] and not wire["is_human_judgment"]
    ingestor = SessionCommentIngestor(store, root)
    ingestor.sync(session)
    ingestor.sync(session, force=True)
    assert store.list_discussion_comments_at_revision(session.id, revision.id) == (comment,)
    assert not store.list_annotations(session.id)


def test_comment_ingestor_cache_is_explicitly_owned_by_review_store(tmp_path: Path):
    store = ReviewStore(tmp_path / "store")
    first = ingestor_for(store, tmp_path / "ledgers")
    assert ingestor_for(store, tmp_path / "ledgers") is first
    other = ingestor_for(store, tmp_path / "other-ledgers")
    assert other is not first
    assert set(store._session_comment_ingestors) == {
        str((tmp_path / "ledgers").resolve()),
        str((tmp_path / "other-ledgers").resolve()),
    }


def test_captured_plan_names_generic_change_and_first_snapshot(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    _reply(root, repo, "planner-title", "plan", "Add safer price validation. Keep existing callers compatible.")
    store, session, revision = _snapshot(root, repo, title="")

    current = store.get_session(session.id)
    stored_revision = store.get_revision(revision.id)
    assert current is not None and current.title == "Add safer price validation"
    assert stored_revision is not None and stored_revision.title == "Add safer price validation"


def test_latest_author_output_names_snapshot_and_brief_without_overwriting_custom_change_title(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api

    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, first = _snapshot(root, repo)
    (repo / "app.py").write_text("def price():\n    return 7\n")
    _reply(root, repo, "coder-progress", "code", "I'll inspect the current implementation before changing it.")
    _reply(
        root,
        repo,
        "coder-final",
        "code",
        "## Summary\nImplemented safer price validation. Existing callers remain compatible.",
    )
    SessionCommentIngestor(store, root).sync(session)
    refresh(store, session, repo, store_root=root)

    revision = store.latest_revision(session.id)
    current = store.get_session(session.id)
    assert revision is not None and revision.id != first.id
    assert revision.title == "Implemented safer price validation"
    assert current is not None and current.title == "hook comments"

    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency("fixture-token"), repo_root=repo, port=45997)
    with TestClient(app, base_url="http://127.0.0.1:45997") as client:
        headers = {"Authorization": "Bearer fixture-token"}
        response = client.get(f"/api/reviews/{session.id}", headers=headers)
        projection = client.get(f"/api/reviews/{session.id}/projection", headers=headers)
    assert response.status_code == 200
    assert response.json()["title"] == "hook comments"
    assert response.json()["revision"]["title"] == revision.title
    assert response.json()["brief"]["summary"] == revision.title
    assert projection.status_code == 200
    context = [row for row in projection.json()["items"] if row["id"].startswith("agent:")]
    assert [(row["source_label"], row["title"]) for row in context] == [
        ("Captured author · Code", "Implemented safer price validation"),
    ]
    assert "Existing callers remain compatible" in context[0]["rationale"]


def test_captured_author_output_populates_brief_without_turning_review_findings_into_author_context(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api

    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    _reply(root, repo, "planner-brief", "plan", "Keep price validation compatible with existing callers.")
    store, session, first = _snapshot(root, repo, title="")

    (repo / "app.py").write_text("def price():\n    return 9\n")
    _reply(root, repo, "coder-brief", "code", "Implemented safer validation around the returned price.")
    _reply(root, repo, "reviewer-brief", "review", "Possible regression in app.py:2.")
    ingestor = SessionCommentIngestor(store, root)
    ingestor.sync(session)
    refresh(store, session, repo, store_root=root)
    ingestor.sync(session, force=True)
    second = store.latest_revision(session.id)
    assert second is not None and second.id != first.id

    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency("fixture-token"), repo_root=repo, port=45998)
    with TestClient(app, base_url="http://127.0.0.1:45998") as client:
        latest = client.get(
            f"/api/reviews/{session.id}/projection",
            headers={"Authorization": "Bearer fixture-token"},
        )
        historical = client.get(
            f"/api/reviews/{session.id}/revisions/0/projection",
            headers={"Authorization": "Bearer fixture-token"},
        )

    assert latest.status_code == 200 and historical.status_code == 200
    latest_payload = latest.json()
    intents = {(row["producer"], row["intent"], row["origin"]) for row in latest_payload["plan"]["intents"]}
    assert (
        "claude:planner-brief",
        "Keep price validation compatible with existing callers",
        "captured",
    ) in intents
    context = [row for row in latest_payload["items"] if row["id"].startswith("agent:")]
    assert [row["title"] for row in context] == ["Implemented safer validation around the returned price"]
    assert context[0]["source_label"] == "Captured author · Code"
    assert context[0]["attention"] == "understand"
    assert not any("Possible regression" in row["title"] for row in latest_payload["items"])

    historical_payload = historical.json()
    assert any(
        row["intent"] == "Keep price validation compatible with existing callers" and row["origin"] == "captured"
        for row in historical_payload["plan"]["intents"]
    )
    assert not any(row["id"].startswith("agent:") for row in historical_payload["items"])


def test_completed_summary_survives_later_chatter_from_the_same_agent(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    _reply(root, repo, "coder-summary", "code", "Implemented safer price validation. Tests: 5 passed.")
    _reply(root, repo, "coder-summary", "code", "Source capture already runs in parallel. I'll check the next step.")
    store, session, revision = _snapshot(root, repo)
    rows = brief_agent_context(store, session, revision)
    assert [row["title"] for row in rows] == ["Implemented safer price validation"]
    assert len(store.list_discussion_comments(session.id)) == 2


def test_independent_reviewer_anchors_only_proven_bytes_and_stales_on_refresh(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, revision = _snapshot(root, repo)
    body = _finding(repo)
    payload = _reply(root, repo, "reviewer", "review", body)
    ingestor = SessionCommentIngestor(store, root)
    ingestor.sync(session)
    assert not store.list_annotations(session.id)
    assert not store.list_discussion_comments(session.id)[0].revision_id
    assert not store.list_discussion_comments_at_revision(session.id, revision.id)
    revision = _next_snapshot(root, repo, store, session)
    annotations = store.list_annotations(session.id)
    assert len(annotations) == 1
    finding = annotations[0]
    assert finding.anchor.path == "app.py" and finding.anchor.start_line == 2
    assert finding.revision_id == revision.id and finding.source == "ai_review"
    assert finding.agent_role == "review" and finding.source_message_id == "reviewer-message"
    assert store.list_discussion_comments(session.id)[0].body == body
    assert capture_session_comment(root, payload, host="claude") == 0
    ingestor.sync(session, force=True)
    assert len(store.list_annotations(session.id)) == 1
    (repo / "app.py").write_text("def price():\n    return 3\n")
    refresh(store, session, repo, store_root=root)
    assert store.get_annotation(finding.id).state == "obsolete"
    assert len(store.list_discussion_comments(session.id)) == 1


def test_changed_bytes_fall_back_to_referenced_file_without_fake_line(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    _reply(root, repo, "reviewer", "review", _finding(repo))
    (repo / "app.py").write_text("def price():\n    return 4\n")
    store, session, revision = _snapshot(root, repo)
    annotations = store.list_annotations(session.id)
    assert len(annotations) == 1
    finding = annotations[0]
    assert finding.revision_id == revision.id
    assert (finding.anchor.path, finding.anchor.start_line, finding.anchor.end_line) == ("app.py", 0, 0)
    assert finding.anchor_method == "file"
    comments = store.list_discussion_comments(session.id)
    assert len(comments) == 1 and comments[0].title == "Agent summary"


def test_structured_locations_are_imported_from_any_recorded_role(tmp_path: Path):
    for case_index, role in enumerate(["code", "test", "performance-auditor", "UX Specialist", "unknown"]):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        repo, root = (case_tmp_path / "repo", case_tmp_path / "store")
        _repo(repo)
        store, session, _ = _snapshot(root, repo)
        _reply(root, repo, "independent", role, _finding(repo))
        ingestor = SessionCommentIngestor(store, root)
        ingestor.sync(session)
        assert not store.list_annotations(session.id)
        _next_snapshot(root, repo, store, session)
        annotation = store.list_annotations(session.id)[0]
        assert annotation.agent_role == role
        assert annotation.anchor.path == "app.py" and annotation.anchor.start_line == 2
        assert annotation.source == "author" and annotation.created_by_actor == "agent"
        assert not _annotation_payload(annotation)["is_human_judgment"]


def test_intermediate_prose_locations_and_arbitrary_roles_reach_review_on_one_stop(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, _ = _snapshot(root, repo)
    transcript = tmp_path / "rollout.jsonl"
    bodies = ["Inspect `app.py:2`; this value needs validation.", "Unlocated progress note", "Completed."]
    roles = ["performance-auditor", "test", "release-coordinator"]
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": f"m{number}",
                "agent_role": role,
                "phase": "commentary" if number < 2 else "final_answer",
                "content": [{"type": "output_text", "text": body}],
            },
        }
        for number, (role, body) in enumerate(zip(roles, bodies, strict=True))
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    payload = {"session_id": "independent", "cwd": str(repo), "transcript_path": str(transcript)}
    assert capture_session_comment(root, payload, host="codex") == 3
    ingestor = SessionCommentIngestor(store, root)
    ingestor.sync(session)
    comments = store.list_discussion_comments(session.id)
    assert {comment.body for comment in comments} == set(bodies)
    assert {comment.agent_role for comment in comments} == set(roles)
    assert all(not comment.revision_id for comment in comments)
    assert not store.list_annotations(session.id)
    revision = _next_snapshot(root, repo, store, session)
    comments = store.list_discussion_comments(session.id)
    assert {comment.body for comment in comments} == set(bodies[1:])
    assert all(comment.revision_id == revision.id and not comment.anchor.path for comment in comments)
    [inline] = store.list_annotations(session.id)
    assert inline.agent_role == roles[0]
    assert inline.anchor.path == "app.py" and inline.anchor.start_line == 2
    assert capture_session_comment(root, payload, host="codex") == 0
    ingestor.sync(session, force=True)
    assert len(store.list_discussion_comments(session.id)) == 2
    assert len(store.list_annotations(session.id)) == 1


def test_stop_batches_context_reads_but_keeps_each_messages_own_file_targets(tmp_path: Path, monkeypatch):
    from lemoncrow.core.capabilities import session_comment_capture
    from lemoncrow.core.foundation.paths import session_dir

    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    (repo / "other.py").write_text("value = 3\n")
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "message", "role": "assistant", "id": path, "content": f"Inspect `{path}:1`"}
        for path in ["app.py", "other.py", "app.py"]
    ]
    # Same original message is repeated to exercise message idempotency too.
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    calls, reads = [], []
    original_context, original_read = session_comment_capture._context, Path.read_text

    def context(*args):
        calls.append(args[1])
        return original_context(*args)

    def read(path, *args, **kwargs):
        if path.parent == repo:
            reads.append(path.name)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(session_comment_capture, "_context", context)
    monkeypatch.setattr(Path, "read_text", read)
    assert (
        capture_session_comment(
            root, {"session_id": "sid", "transcript_path": str(transcript), "cwd": str(repo)}, host="codex"
        )
        == 2
    )
    run = json.loads((session_dir(root, "codex", "sid") / "run.json").read_text())
    assert calls == [str(repo)]
    assert sorted(reads) == ["app.py", "other.py"]
    messages = [event["payload"] for event in run["events"]]
    assert set(messages[0]["review_context"]["targets"]) == {"app.py"}
    assert set(messages[1]["review_context"]["targets"]) == {"other.py"}
    assert all("_target_paths" not in message["review_context"] for message in messages)


def test_unproven_prose_reference_falls_back_to_referenced_file(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    body = "Inspect `app.py:2`."
    _reply(root, repo, "explorer", "explorer", body)
    (repo / "app.py").write_text("def price():\n    return 4\n")
    store, session, revision = _snapshot(root, repo)
    assert not store.list_discussion_comments(session.id)
    annotation = store.list_annotations(session.id)[0]
    assert annotation.body == body and annotation.revision_id == revision.id
    assert (annotation.anchor.path, annotation.anchor.start_line, annotation.anchor.end_line) == ("app.py", 0, 0)
    assert annotation.anchor_method == "file"


def test_unbound_other_checkout_never_attaches(tmp_path: Path):
    repo, other, root = tmp_path / "repo", tmp_path / "other", tmp_path / "store"
    _repo(repo)
    _repo(other)
    _reply(root, other, "other-planner", "plan", "Other repository plan")
    store, session, _ = _snapshot(root, repo)
    assert not store.list_discussion_comments(session.id)
    assert not store.session_comment_cursor(session.id, "claude", "other-planner")


def test_import_failure_rolls_back_comments_and_cursor(tmp_path: Path, monkeypatch):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, _ = _snapshot(root, repo)
    _reply(root, repo, "coder", "code", "Implemented the price change.")
    ingestor = SessionCommentIngestor(store, root)
    original = store.set_session_comment_cursor

    def fail(*args, **kwargs):
        raise RuntimeError("simulate interrupted import")

    monkeypatch.setattr(store, "set_session_comment_cursor", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        ingestor.sync(session)
    assert not store.list_discussion_comments(session.id)
    assert not store.session_comment_cursor(session.id, "claude", "coder")
    monkeypatch.setattr(store, "set_session_comment_cursor", original)
    ingestor.sync(session)
    assert len(store.list_discussion_comments(session.id)) == 1


def test_branch_reuse_does_not_reattribute_previous_agent_messages(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    _reply(root, repo, "planner", "plan", "Original Change plan")
    store, first, _ = _snapshot(root, repo)
    rng = resolve_rev_range(repo, working_tree=True)
    second = open_or_create_session(store, repo, rng, force_new=True)
    ingestor = SessionCommentIngestor(store, root)
    ingestor.sync(second)
    assert not store.list_discussion_comments(second.id)
    assert [item.body for item in store.list_discussion_comments(first.id)] == ["Original Change plan"]
    assert not store.list_annotations(first.id)
    _reply(root, repo, "new-planner", "plan", "Second Change plan")
    ingestor.sync(second)
    assert [comment.body for comment in store.list_discussion_comments(second.id)] == ["Second Change plan"]


def test_historical_file_comment_excludes_later_draft_imports(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    _reply(root, repo, "planner", "plan", "Plan before revision one")
    store, session, first = _snapshot(root, repo)
    (repo / "app.py").write_text("def price():\n    return 3\n")
    refresh(store, session, repo, store_root=root)
    _reply(root, repo, "coder", "code", "Explanation after revision two")
    SessionCommentIngestor(store, root).sync(session)
    assert {comment.body for comment in store.list_discussion_comments(session.id)} == {
        "Plan before revision one",
        "Explanation after revision two",
    }
    assert [comment.body for comment in store.list_discussion_comments_at_revision(session.id, first.id)] == [
        "Plan before revision one"
    ]
    assert not store.list_annotations_at_revision(session.id, first.id)
    assert [row["title"] for row in brief_agent_context(store, session, first, historical=True)] == [
        "Plan before revision one"
    ]


def test_repository_identity_namespace_migration_falls_back_to_exact_worktree(tmp_path: Path):
    repo, trace, db_root = tmp_path / "repo", tmp_path / "trace", tmp_path / "review"
    _repo(repo)
    _reply(trace, repo, "server-coder", "code", "Implementation output from the Git-identity host ledger.")

    store = ReviewStore(db_root)
    rng = resolve_rev_range(repo, working_tree=True)
    build = build_review_packet_with_blobs(
        repo, rng, store_root=db_root, with_patch_text=True, unbounded_patch_text=True
    )
    session = open_or_create_session(store, repo, rng)
    with store._transaction() as conn:
        conn.execute(
            "UPDATE review_bindings SET repository_identity=? WHERE review_id=?",
            ("local:repo_test_namespace", session.id),
        )
    revision = record_revision(
        store,
        session,
        repo,
        rng,
        store_root=db_root,
        source_store_root=trace,
        build=build,
    ).revision

    annotations = store.list_discussion_comments_at_revision(session.id, revision.id)
    assert len(annotations) == 1
    assert annotations[0].source_id == "server-coder"


def test_foreign_review_id_falls_back_to_exact_repository_binding(tmp_path: Path):
    repo, trace, db_root = tmp_path / "repo", tmp_path / "trace", tmp_path / "review"
    _repo(repo)
    _reply(trace, repo, "legacy-coder", "code", "Implementation output captured before the Review store moved.")

    [run_file] = list((trace / "sessions").glob("*/*/*/*/legacy-coder/run.json"))
    run = json.loads(run_file.read_text(encoding="utf-8"))
    for event in run["events"]:
        payload = event.get("payload") or {}
        context = payload.get("review_context") or {}
        if isinstance(context, dict):
            context["review_id"] = "legacy-review-id-from-another-store"
    run_file.write_text(json.dumps(run, indent=2), encoding="utf-8")

    store = ReviewStore(db_root)
    rng = resolve_rev_range(repo, working_tree=True)
    build = build_review_packet_with_blobs(
        repo, rng, store_root=db_root, with_patch_text=True, unbounded_patch_text=True
    )
    session = open_or_create_session(store, repo, rng)
    revision = record_revision(
        store,
        session,
        repo,
        rng,
        store_root=db_root,
        source_store_root=trace,
        build=build,
    ).revision

    annotations = store.list_discussion_comments_at_revision(session.id, revision.id)
    assert len(annotations) == 1
    assert annotations[0].source_id == "legacy-coder"


def test_record_revision_imports_comments_from_separate_source_store_root(tmp_path: Path):
    repo, trace, db_root = tmp_path / "repo", tmp_path / "trace", tmp_path / "review"
    _repo(repo)
    _reply(trace, repo, "coder-separate", "code", "Direct implementation explanation from the host ledger.")

    store = ReviewStore(db_root)
    rng = resolve_rev_range(repo, working_tree=True)
    build = build_review_packet_with_blobs(
        repo, rng, store_root=db_root, with_patch_text=True, unbounded_patch_text=True
    )
    session = open_or_create_session(store, repo, rng)
    revision = record_revision(
        store,
        session,
        repo,
        rng,
        store_root=db_root,
        source_store_root=trace,
        build=build,
    ).revision

    annotations = store.list_discussion_comments_at_revision(session.id, revision.id)
    assert len(annotations) == 1
    assert annotations[0].source_id == "coder-separate"
    assert annotations[0].agent_role == "code"
    assert "Direct implementation explanation" in annotations[0].body


def test_separate_trace_root_imports_later_direct_sessions(tmp_path: Path):
    repo, trace, db_root = tmp_path / "repo", tmp_path / "trace", tmp_path / "review"
    _repo(repo)
    store, session, _ = _snapshot(db_root, repo)
    _reply(trace, repo, "coder", "code", "Direct implementation explanation.")
    assert not (trace / "lemoncrow_reviews.db").exists()
    SessionCommentIngestor(store, trace).sync(session)
    assert store.list_discussion_comments(session.id)[0].agent_role == "code"


def test_annotations_api_and_live_cursor_refresh_from_stop_without_handoff(tmp_path: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api

    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, _ = _snapshot(root, repo)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency("fixture-token"), repo_root=repo, port=45998)
    headers = {"Authorization": "Bearer fixture-token"}
    with TestClient(app, base_url="http://127.0.0.1:45998") as client:
        annotation_path = f"/api/reviews/{session.id}/annotations"
        live_path = annotation_path.rsplit("/", 1)[0] + "/live-state?wait_ms=0"
        before = client.get(live_path, headers=headers)
        assert before.status_code == 200
        _reply(root, repo, "coder", "code", "Session Stop explanation")
        after = client.get(live_path, headers=headers)
        assert after.status_code == 200
        assert after.json()["annotations_cursor"] != before.json()["annotations_cursor"]
        result = client.get(annotation_path, headers=headers)
        assert result.status_code == 200
        row = result.json()["annotations"][0]
        assert (row["agent_role"], row["agent_host"], row["comment_scope"]) == ("code", "claude", "change")
        assert row["body"] == "Session Stop explanation" and not row["is_human_judgment"]
        assert result.json()["feedback"]["open_total"] == 0
        assert row["revision_id"] == ""
        revision = _next_snapshot(root, repo, store, session)
        promoted = client.get(live_path, headers=headers)
        assert promoted.json()["annotations_cursor"] != after.json()["annotations_cursor"]
        assert client.get(annotation_path, headers=headers).json()["annotations"][0]["revision_id"] == revision.id


def test_draft_promotes_on_snapshot_without_another_stop_and_survives_restart(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, first = _snapshot(root, repo)
    (repo / "app.py").write_text("def price():\n    return 5\n")
    _reply(root, repo, "coder", "custom role", "Changed `app.py:2`.")
    _reply(root, repo, "planner", "plan", "Explanation without a file.")
    SessionCommentIngestor(store, root).sync(session)
    draft = store.list_discussion_comments(session.id)
    assert len(draft) == 2 and all(not item.revision_id for item in draft)
    assert not store.list_annotations(session.id)
    assert not store.list_discussion_comments_at_revision(session.id, first.id)
    assert store.session_comment_cursor(session.id, "claude", "coder")["pending"]
    # Durable cursor, rather than process memory, owns the pending handoff.
    store = ReviewStore(root)
    refresh(store, session, repo, store_root=root)
    second = store.latest_revision(session.id)
    assert second.id != first.id
    [discussion] = store.list_discussion_comments(session.id)
    assert discussion.body == "Explanation without a file." and discussion.revision_id == second.id
    assert not discussion.anchor.path
    [annotation] = store.list_annotations(session.id)
    assert annotation.revision_id == second.id and annotation.anchor.start_line == 2
    assert not store.session_comment_cursor(session.id, "claude", "coder")["pending"]
    assert not store.list_discussion_comments_at_revision(session.id, first.id)
    third = _next_snapshot(root, repo, store, session)
    SessionCommentIngestor(store, root).sync(session, force=True)
    assert third.id != second.id
    assert store.list_discussion_comments(session.id) == (discussion,)
    assert len(store.list_annotations(session.id)) == 1


def test_unlocated_message_stays_discussion_despite_larger_changed_files(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    (repo / "older.py").write_text("\n".join(f"old_{i} = {i}" for i in range(30)) + "\n")
    store, session, first = _snapshot(root, repo)

    (repo / "app.py").write_text("def price():\n    return 3\n")
    (repo / "new.py").write_text("\n".join(f"new_{i} = {i}" for i in range(6)) + "\n")
    _reply(root, repo, "coder", "code", "Implementation note without a location.")
    SessionCommentIngestor(store, root).sync(session)
    assert len(store.list_discussion_comments(session.id)) == 1

    refresh(store, session, repo, store_root=root)
    second = store.latest_revision(session.id)
    assert second.id != first.id
    [discussion] = store.list_discussion_comments(session.id)
    assert discussion.revision_id == second.id
    assert discussion.body == "Implementation note without a location." and not discussion.anchor.path
    assert not store.list_annotations(session.id)


def test_unproven_reference_beats_larger_file_fallback(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, _ = _snapshot(root, repo)

    (repo / "app.py").write_text("def price():\n    return 3\n")
    (repo / "new.py").write_text("\n".join(f"new_{i} = {i}" for i in range(12)) + "\n")
    body = "Inspect `app.py:999`; exact line is uncertain."
    _reply(root, repo, "reviewer", "review", body)
    SessionCommentIngestor(store, root).sync(session)
    refresh(store, session, repo, store_root=root)

    assert not store.list_discussion_comments(session.id)
    annotation = store.list_annotations(session.id)[0]
    assert annotation.body == body
    assert (annotation.anchor.path, annotation.anchor.start_line, annotation.anchor.end_line) == ("app.py", 0, 0)
    assert annotation.anchor_method == "file"


def test_draft_snapshot_mismatch_falls_back_to_file_without_inventing_a_line(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, _ = _snapshot(root, repo)
    _reply(root, repo, "reviewer", "review", _finding(repo))
    SessionCommentIngestor(store, root).sync(session)
    assert len(store.list_discussion_comments(session.id)) == 1
    (repo / "app.py").write_text("def price():\n    return 6\n")
    refresh(store, session, repo, store_root=root)
    revision = store.latest_revision(session.id)
    annotations = store.list_annotations(session.id)
    assert len(annotations) == 1
    finding = annotations[0]
    assert finding.revision_id == revision.id
    assert (finding.anchor.path, finding.anchor.start_line, finding.anchor.end_line) == ("app.py", 0, 0)
    assert finding.anchor_method == "file"
    comments = store.list_discussion_comments(session.id)
    assert len(comments) == 1 and comments[0].revision_id == revision.id
    assert comments[0].title == "Agent summary"
    assert not store.session_comment_cursor(session.id, "claude", "reviewer")["pending"]
    # Restoring older bytes later must not revive the old finding.
    (repo / "app.py").write_text("def price():\n    return 2\n")
    _next_snapshot(root, repo, store, session)
    assert store.get_annotation(finding.id).state == "obsolete"


def test_old_consumed_cursor_backfills_pending_comments(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    store, session, _ = _snapshot(root, repo)
    _reply(root, repo, "coder", "code", "Inspect `app.py:2`.")
    SessionCommentIngestor(store, root).sync(session)
    cursor = store.session_comment_cursor(session.id, "claude", "coder")
    for field in ("version", "pending", "revision_id"):
        cursor.pop(field)
    store.set_session_comment_cursor(session.id, "claude", "coder", cursor)
    _next_snapshot(root, repo, store, session)
    assert not store.list_discussion_comments(session.id)
    assert len(store.list_annotations(session.id)) == 1


def test_late_discovery_attaches_first_snapshot_and_projects_through_later_edits(tmp_path: Path):
    repo, trace, db_root = tmp_path / "repo", tmp_path / "trace", tmp_path / "review"
    _repo(repo)
    store, session, first = _snapshot(db_root, repo)
    _reply(trace, repo, "coder", "code", "Inspect `app.py:2`.")
    second = _next_snapshot(db_root, repo, store, session)
    (repo / "app.py").write_text("def price():\n    return 9\n")
    refresh(store, session, repo, store_root=db_root)
    third = store.latest_revision(session.id)
    assert third.id != second.id
    (repo / "app.py").write_text("def price():\n    return 2\n")
    _next_snapshot(db_root, repo, store, session)
    SessionCommentIngestor(store, trace).sync(session)
    assert not store.list_discussion_comments(session.id)
    annotation = store.list_annotations(session.id)[0]
    assert annotation.revision_id == second.id
    assert annotation.state == "obsolete"
    assert not store.list_discussion_comments_at_revision(session.id, first.id)
    assert not store.list_discussion_comments_at_revision(session.id, second.id)
    assert len(store.list_annotations_at_revision(session.id, second.id)) == 1


def test_unchanged_ledger_is_not_reread_on_live_refresh(tmp_path: Path, monkeypatch):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    _reply(root, repo, "planner", "plan", "Plan.")
    store, session, _ = _snapshot(root, repo)
    ingestor = SessionCommentIngestor(store, root)
    ingestor.sync(session)
    original = Path.read_text

    def checked(path, *args, **kwargs):
        if path.name == "run.json":
            raise AssertionError("unchanged session ledger reread")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked)
    assert ingestor.sync(session) == 0


def test_outputs_consume_only_their_preceding_edit_interval(tmp_path: Path):
    from lemoncrow.core.foundation.paths import session_dir

    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    (repo / "other.py").write_text("value = 3\n")
    sid = "scoped-output"
    run_file = session_dir(root, "codex", sid) / "run.json"
    run_file.parent.mkdir(parents=True)
    # Stop appends outputs after tool events; chronology must ignore ledger order.
    run_file.write_text(
        json.dumps(
            {
                "session_id": sid,
                "agent": "codex",
                "workspace_path": str(repo),
                "files_touched": ["unrelated.py"],
                "events": [
                    {"kind": "file_edit", "at": "2020-01-01T00:03:00Z", "payload": {"path": "other.py:L1-L1"}},
                    {"kind": "file_edit", "at": "2020-01-01T00:01:00Z", "payload": {"path": "app.py"}},
                ],
            }
        )
    )
    transcript = tmp_path / "scoped.jsonl"
    bodies = ["Implemented price validation.", "Updated the other value.", "Verification passed."]
    records = [
        {
            "type": "response_item",
            "timestamp": f"2020-01-01T00:0{minute}:00Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": f"m{number}",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": body}],
            },
        }
        for number, (minute, body) in enumerate(zip([2, 4, 5], bodies, strict=True))
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    hook = {"session_id": sid, "cwd": str(repo), "transcript_path": str(transcript), "agent_role": "code"}
    assert capture_session_comment(root, hook, host="codex") == 3
    run = json.loads(run_file.read_text())
    outputs = [event["payload"] for event in run["events"] if event["kind"] == "agent_message"]
    assert [output["review_context"]["edited_paths"] for output in outputs] == [["app.py"], ["other.py"], []]

    store, session, first = _snapshot(root, repo)
    annotations = store.list_annotations(session.id)
    assert {(item.body, item.anchor.path) for item in annotations} == {
        (bodies[0], "app.py"),
        (bodies[1], "other.py"),
    }
    assert all(item.anchor.start_line == 0 for item in annotations)
    [discussion] = store.list_discussion_comments(session.id)
    assert discussion.body == bodies[2] and discussion.revision_id == first.id
    assert not discussion.anchor.path

    # The next Stop consumes fresh edits, without giving earlier outputs that file.
    run["events"].append({"kind": "file_edit", "at": "2020-01-01T00:07:00Z", "payload": {"path": "app.py"}})
    run_file.write_text(json.dumps(run))
    records.append(
        {
            "type": "response_item",
            "timestamp": "2020-01-01T00:08:00Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": "m3",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Finished price changes."}],
            },
        }
    )
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    assert capture_session_comment(root, hook, host="codex") == 1
    second = _next_snapshot(root, repo, store, session)
    [new] = [item for item in store.list_annotations(session.id) if item.body == "Finished price changes."]
    assert new.anchor.path == "app.py" and new.revision_id == second.id
    assert len(store.list_annotations(session.id)) == 3


def test_missing_reference_never_falls_back_to_an_unrelated_changed_file(tmp_path: Path):
    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    body = "Inspect missing.py:2; this needs checking."
    _reply(root, repo, "reviewer-missing", "review", body)
    store, session, revision = _snapshot(root, repo)
    assert not store.list_annotations(session.id)
    [discussion] = store.list_discussion_comments(session.id)
    assert discussion.body == body and discussion.revision_id == revision.id
    assert not discussion.anchor.path


def test_old_guessed_file_annotation_is_retired_without_losing_the_message(tmp_path: Path):
    from lemoncrow.pro.capabilities.review.sources.local import annotate

    repo, root = tmp_path / "repo", tmp_path / "store"
    _repo(repo)
    body = "Verification passed without a file reference."
    _reply(root, repo, "legacy-fallback", "code", body)
    store, session, revision = _snapshot(root, repo)
    [discussion] = store.list_discussion_comments(session.id)
    guessed = annotate(
        store,
        session,
        revision,
        path="app.py",
        start_line=0,
        end_line=0,
        body=body,
        source="author",
        created_by="claude",
        created_by_actor="agent",
        new_text=(repo / "app.py").read_text(),
        agent_role="code",
        source_id=discussion.source_id,
        source_message_id=discussion.source_message_id,
        annotation_id=discussion.id.replace("dsc-", "ann-", 1),
    )
    store.delete_discussion_comment(session.id, discussion.id)
    cursor = store.session_comment_cursor(session.id, "claude", "legacy-fallback")
    cursor["version"] = 2
    store.set_session_comment_cursor(session.id, "claude", "legacy-fallback", cursor)
    ingestor = SessionCommentIngestor(store, root)
    ingestor.sync(session)
    assert store.get_annotation(guessed.id).state == "obsolete"
    [restored] = store.list_discussion_comments(session.id)
    assert restored.body == body and restored.revision_id == revision.id and not restored.anchor.path
    ingestor.sync(session, force=True)
    assert store.list_discussion_comments(session.id) == (restored,)
