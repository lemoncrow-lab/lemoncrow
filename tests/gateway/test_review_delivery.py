from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.pro.capabilities.review import delivery as review_delivery
from lemoncrow.pro.capabilities.review.delivery import (
    deliver_prompt_to_agent_session,
    deliver_to_agent_session,
    deliver_to_claude_session,
    mark_feedback_addressed,
)
from lemoncrow.pro.capabilities.review.session_inbox import claim_session_messages
from lemoncrow.pro.capabilities.review.store import AnnotationVersionConflict

SESSION = "11111111-2222-4333-8444-555555555555"


def _completed(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["claude"], returncode, stdout=stdout, stderr=stderr)


def test_delivery_queues_an_active_exact_session_without_forking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setenv("LEMONCROW_ROOT", str(store_root))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude" if name == "claude" else None)

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["/usr/bin/claude", "agents", "--json"]
        return _completed(stdout=json.dumps([{"sessionId": SESSION, "kind": "interactive", "status": "idle"}]))

    monkeypatch.setattr(subprocess, "run", run)
    result = deliver_to_claude_session(SESSION, tmp_path, "review feedback")
    assert result.state == "queued"
    assert result.accepted is True
    assert result.target_ref == f"claude:{SESSION}"
    assert result.remote_ref.startswith("inbox:msg-")
    (queued,) = claim_session_messages(store_root, host="claude", session_id=SESSION)
    assert "Human review feedback from LemonCrow" in queued.message
    assert "review feedback" in queued.message
    assert claim_session_messages(store_root, host="claude", session_id=SESSION) == ()


def test_delivery_resumes_and_verifies_the_exact_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    calls: list[list[str]] = []
    agents_calls = 0

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal agents_calls
        calls.append(args)
        if args[1:] == ["agents", "--json"]:
            agents_calls += 1
            if agents_calls == 1:
                return _completed(stdout="[]")
            return _completed(
                stdout=json.dumps([{"id": "deadbeef", "sessionId": SESSION, "kind": "background", "state": "running"}])
            )
        assert args[1:4] == ["--resume", SESSION, "--bg"]
        assert "Human review feedback from LemonCrow" in args[4]
        assert "review feedback" in args[4]
        return _completed(stdout="Background agent deadbeef started")

    monkeypatch.setattr(subprocess, "run", run)
    result = deliver_to_claude_session(SESSION, tmp_path, "review feedback")
    assert result.state == "working"
    assert result.sent is False
    assert result.remote_ref == "deadbeef"
    assert result.target_ref == f"claude:{SESSION}"
    assert agents_calls == 2


def test_delivery_stops_a_copy_instead_of_claiming_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    agents_calls = 0
    stopped: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal agents_calls
        if args[1:] == ["agents", "--json"]:
            agents_calls += 1
            if agents_calls == 1:
                return _completed(stdout="[]")
            return _completed(
                stdout=json.dumps(
                    [
                        {
                            "id": "cafebabe",
                            "sessionId": "99999999-2222-4333-8444-555555555555",
                            "kind": "background",
                            "state": "running",
                        }
                    ]
                )
            )
        return _completed(stdout="Background agent cafebabe started")

    class FakePopen:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            stopped.append(args)

        def __enter__(self) -> FakePopen:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    result = deliver_to_claude_session(SESSION, tmp_path, "review feedback")
    assert result.state == "blocked"
    assert "created a copy" in result.message
    assert stopped == [["/usr/bin/claude", "stop", "cafebabe"]]


def test_delivery_fails_when_claude_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = deliver_to_claude_session(SESSION, tmp_path, "review feedback")
    assert result.state == "failed"
    assert "not installed" in result.message


def test_opencode_delivery_queues_when_exact_session_is_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir
    from lemoncrow.pro.capabilities.review.session_inbox import claim_session_messages

    store_root = tmp_path / "store"
    monkeypatch.setenv("LEMONCROW_ROOT", str(store_root))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/opencode" if name == "opencode" else None)
    state_path = resolve_workspace_store_dir(workspace_root=tmp_path) / "session_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"host": "opencode", "session_id": SESSION, "activity": "active"}),
        encoding="utf-8",
    )

    def forbidden_popen(*_args: object, **_kwargs: object):
        raise AssertionError("active OpenCode delivery must not spawn a second process")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    result = deliver_to_agent_session("opencode", SESSION, tmp_path, "review feedback")

    assert result.state == "queued"
    assert result.accepted is True
    assert result.remote_ref.startswith("inbox:msg-")
    (queued,) = claim_session_messages(store_root, host="opencode", session_id=SESSION)
    assert "Human review feedback from LemonCrow" in queued.message
    assert "review feedback" in queued.message


def test_lemoncode_delivery_queues_when_exact_session_is_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir
    from lemoncrow.pro.capabilities.review.session_inbox import claim_session_messages

    store_root = tmp_path / "store"
    monkeypatch.setenv("LEMONCROW_ROOT", str(store_root))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/lemoncode" if name == "lemoncode" else None)
    state_path = resolve_workspace_store_dir(workspace_root=tmp_path) / "session_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"host": "lemoncode", "session_id": SESSION, "activity": "active"}),
        encoding="utf-8",
    )

    def forbidden_popen(*_args: object, **_kwargs: object):
        raise AssertionError("active LemonCode delivery must not spawn a second process")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    result = deliver_to_agent_session("lemoncode", SESSION, tmp_path, "review feedback")

    assert result.state == "queued"
    assert result.accepted is True
    (queued,) = claim_session_messages(store_root, host="lemoncode", session_id=SESSION)
    assert "review feedback" in queued.message


def test_copilot_delivery_queues_into_exact_working_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

    store_root = tmp_path / "store"
    monkeypatch.setenv("LEMONCROW_ROOT", str(store_root))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/copilot" if name == "copilot" else None)
    state_path = resolve_workspace_store_dir(workspace_root=tmp_path) / "session_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"host": "copilot", "session_id": SESSION, "activity": "working"}),
        encoding="utf-8",
    )

    def forbidden_popen(*_args: object, **_kwargs: object):
        raise AssertionError("working Copilot delivery must wait for the in-process agentStop continuation")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    result = deliver_to_agent_session("copilot", SESSION, tmp_path, "review feedback")

    assert result.state == "queued"
    assert result.accepted is True
    assert result.remote_ref.startswith("inbox:msg-")
    assert "agentStop" in result.message
    (queued,) = claim_session_messages(store_root, host="copilot", session_id=SESSION)
    assert "Human review feedback from LemonCrow" in queued.message
    assert "review feedback" in queued.message


@pytest.mark.parametrize(
    ("host", "expected"),
    (
        ("opencode", ["/usr/bin/opencode", "run", "--session"]),
        ("lemoncode", ["/usr/bin/lemoncode", "run", "--session"]),
        ("copilot", ["/usr/bin/copilot", f"--resume={SESSION}"]),
    ),
)
def test_generic_delivery_uses_exact_session_resume_flags(
    host: str, expected: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == host else None)
    seen: list[str] = []

    class FakeProcess:
        pid = 4242

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired(seen, timeout)

    def popen(args: list[str], **_kwargs: object):
        seen.extend(args)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = deliver_to_agent_session(host, SESSION, tmp_path, "review feedback")
    assert result.state == "working"
    assert "consumption is still in flight" in result.message
    assert result.target_ref == f"{host}:{SESSION}"
    assert seen[: len(expected)] == expected
    assert any(SESSION in token for token in seen)


def test_codex_delivery_resumes_the_exact_thread_instead_of_queueing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`codex queue` strands feedback on an idle thread; delivery must run it."""

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    seen: list[str] = []

    class FakeProcess:
        pid = 4242

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired(seen, timeout)

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"delivery must not shell out synchronously: {args}")

    def popen(args: list[str], **_kwargs: object):
        seen.extend(args)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    result = deliver_to_agent_session("codex", SESSION, tmp_path, "review feedback")

    assert result.state == "working"
    assert seen[:3] == ["/usr/bin/codex", "exec", "resume"]
    assert "queue" not in seen
    assert SESSION in seen
    assert any("Human review feedback from LemonCrow" in token for token in seen)


def test_generic_prompt_delivery_does_not_inject_review_feedback_instructions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    seen: list[str] = []

    class FakeProcess:
        pid = 4343

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired(seen, timeout)

    def popen(args: list[str], **_kwargs: object):
        seen.extend(args)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = deliver_prompt_to_agent_session("codex", SESSION, tmp_path, "Inspect src/a.py:L3")

    assert result.state == "working"
    assert "feedback" not in result.message.lower()
    assert "prompt" in result.message.lower()
    assert seen[:3] == ["/usr/bin/codex", "exec", "resume"]
    assert "Inspect src/a.py:L3" in seen
    assert not any("Human review feedback from LemonCrow" in token for token in seen)


def test_author_claim_rejects_a_comment_edited_after_delivery(tmp_path: Path) -> None:
    store = FakeReviewStore(tmp_path)
    store.list_annotation_versions = lambda _annotation_id: (SimpleNamespace(version_number=2),)
    with pytest.raises(ValueError, match="changed after delivery"):
        mark_feedback_addressed(store, tmp_path, ["ann-1"], host="claude", session_id=SESSION)
    assert store.updated == {}


class FakeReviewStore:
    def __init__(
        self,
        repo_root: Path,
        *,
        delivered_to: str = SESSION,
        state: str = "open",
        delivery_state: str = "sent",
        advance_version_on_enter: bool = False,
    ) -> None:
        self.annotation = SimpleNamespace(
            id="ann-1",
            review_id="rev-1",
            revision_id="rrv-1",
            source="human",
            parent_id="",
            state=state,
            author_response="none",
            author_response_source_id="",
        )
        self.review = SimpleNamespace(repo_root=str(repo_root))
        self.delivery = SimpleNamespace(
            target_type="agent_session",
            target_ref=f"claude:{delivered_to}",
            state=delivery_state,
            annotation_version=1,
            revision_id="rrv-delivered",
        )
        self.current_version = 1
        self.advance_version_on_enter = advance_version_on_enter
        self.updated: dict[str, object] = {}
        self.history: object = None
        self.conflict_on_update = False

    def get_annotation(self, annotation_id: str):
        return self.annotation if annotation_id == self.annotation.id else None

    def get_session(self, review_id: str):
        return self.review if review_id == self.annotation.review_id else None

    def list_deliveries(self, annotation_id: str):
        return (self.delivery,) if annotation_id == self.annotation.id else ()

    def list_annotation_versions(self, annotation_id: str):
        return (SimpleNamespace(version_number=self.current_version),) if annotation_id == self.annotation.id else ()

    def latest_revision(self, review_id: str):
        return SimpleNamespace(id="rrv-latest") if review_id == self.annotation.review_id else None

    def atomic_review_transition(self):
        store = self

        class _Transition:
            def __enter__(self):
                if store.advance_version_on_enter:
                    store.current_version += 1
                return None

            def __exit__(self, _exc_type, _exc, _tb):
                return False

        return _Transition()

    def update_annotation(self, annotation_id: str, history=None, /, **fields: object):

        assert annotation_id == self.annotation.id
        self.history = history
        if self.conflict_on_update:
            raise AnnotationVersionConflict("edited between validation and write")
        self.updated = fields
        values = vars(self.annotation).copy()
        values.update(fields)
        return SimpleNamespace(**values)


def test_author_addressed_claim_requires_the_same_session_that_received_feedback(tmp_path: Path) -> None:
    store = FakeReviewStore(tmp_path, delivered_to="99999999-2222-4333-8444-555555555555")
    with pytest.raises(ValueError, match="not delivered to this exact agent session"):
        mark_feedback_addressed(store, tmp_path, ["ann-1"], host="claude", session_id=SESSION)
    assert store.updated == {}


@pytest.mark.parametrize("delivery_state", ["sent", "queued"])
def test_author_addressed_claim_never_resolves_the_human_comment(tmp_path: Path, delivery_state: str) -> None:
    store = FakeReviewStore(tmp_path, delivery_state=delivery_state)
    (updated,) = mark_feedback_addressed(store, tmp_path, ["ann-1"], host="claude", session_id=SESSION)
    assert updated.state == "open"
    assert updated.author_response == "addressed"
    assert updated.author_response_source_id == SESSION
    assert updated.author_response_at
    assert "state" not in store.updated


def test_author_claim_is_bound_to_the_delivered_version(tmp_path: Path) -> None:
    store = FakeReviewStore(tmp_path)
    mark_feedback_addressed(store, tmp_path, ["ann-1"], host="claude", session_id=SESSION)
    assert store.history.expected_version  # type: ignore[attr-defined] == store.delivery.annotation_version


def test_an_edit_racing_the_claim_invalidates_it(tmp_path: Path) -> None:
    store = FakeReviewStore(tmp_path)
    store.conflict_on_update = True
    with pytest.raises(ValueError, match="changed after delivery"):
        mark_feedback_addressed(store, tmp_path, ["ann-1"], host="claude", session_id=SESSION)
    assert store.updated == {}


def test_author_cannot_address_a_comment_the_human_already_resolved(tmp_path: Path) -> None:
    store = FakeReviewStore(tmp_path, state="resolved")
    with pytest.raises(ValueError, match="already resolved by the reviewer"):
        mark_feedback_addressed(store, tmp_path, ["ann-1"], host="claude", session_id=SESSION)
    assert store.updated == {}


def test_mcp_addressed_tool_is_an_author_claim_not_a_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "store"))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_resolved_host_session", lambda: (SESSION, "claude"))
    monkeypatch.setattr(mcp_server, "_session_worktree_root", lambda _root: None)
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.gitdiff.detect_repo_root", lambda _root: tmp_path)
    seen: dict[str, object] = {}

    def mark(_store, repo_root: Path, annotation_ids, *, host: str, session_id: str):
        seen.update(
            repo_root=repo_root,
            annotation_ids=list(annotation_ids),
            host=host,
            session_id=session_id,
        )
        return (SimpleNamespace(id="ann-1"),)

    monkeypatch.setattr(review_delivery, "mark_feedback_addressed", mark)
    result = mcp_server.TOOLS["review_feedback_addressed"]["handler"]({"annotation_ids": ["ann-1"]})
    assert result["status"] == "addressed"
    assert result["annotation_ids"] == ["ann-1"]
    assert result["note"].endswith("remains open until the reviewer resolves it")
    assert seen["session_id"] == SESSION
    assert seen["host"] == "claude"


def test_session_inbox_lease_ack_and_stale_recovery(tmp_path: Path) -> None:
    import os
    import time

    from lemoncrow.pro.capabilities.review.session_inbox import (
        ack_session_messages,
        enqueue_session_message,
        lease_session_messages,
    )

    root = tmp_path / "store"
    first = enqueue_session_message(root, host="opencode", session_id=SESSION, message="recoverable feedback")
    leased = lease_session_messages(root, host="opencode", session_id=SESSION, lease_timeout_s=60)
    assert [item.id for item in leased] == [first.id]
    assert lease_session_messages(root, host="opencode", session_id=SESSION, lease_timeout_s=60) == ()

    inflight = root / "review" / "session-inbox" / "opencode" / SESSION / "inflight" / f"{first.id}.json"
    old = time.time() - 120
    os.utime(inflight, (old, old))
    recovered = lease_session_messages(root, host="opencode", session_id=SESSION, lease_timeout_s=1)
    assert [item.id for item in recovered] == [first.id]

    ack_session_messages(
        root,
        host="opencode",
        session_id=SESSION,
        message_ids=[first.id],
    )
    assert lease_session_messages(root, host="opencode", session_id=SESSION, lease_timeout_s=1) == ()
    retry = enqueue_session_message(root, host="opencode", session_id=SESSION, message="recoverable feedback")
    assert retry.id == first.id
    assert lease_session_messages(root, host="opencode", session_id=SESSION, lease_timeout_s=1) == ()


def test_session_inbox_ack_is_a_durable_review_consumption_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import (
        ack_session_messages,
        enqueue_session_message,
        lease_session_messages,
    )
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    root = tmp_path / "store"
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        ReviewStore,
        "acknowledge_delivery_remote_refs",
        lambda _self, refs: seen.append(tuple(refs)) or len(tuple(refs)),
    )
    message = enqueue_session_message(root, host="claude", session_id=SESSION, message="consume me")
    assert [item.id for item in lease_session_messages(root, host="claude", session_id=SESSION)] == [message.id]

    ack_session_messages(root, host="claude", session_id=SESSION, message_ids=[message.id])

    assert seen == [(f"inbox:{message.id}",)]


def test_session_inbox_release_returns_failed_hook_lease_to_pending(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import (
        enqueue_session_message,
        lease_session_messages,
        release_session_messages,
    )

    root = tmp_path / "store"
    first = enqueue_session_message(root, host="claude", session_id=SESSION, message="retry after hook failure")
    leased = lease_session_messages(root, host="claude", session_id=SESSION)
    assert [item.id for item in leased] == [first.id]
    release_session_messages(root, host="claude", session_id=SESSION, message_ids=[first.id])
    retry = lease_session_messages(root, host="claude", session_id=SESSION)
    assert [item.id for item in retry] == [first.id]


def test_session_inbox_retry_after_claim_is_not_redelivered(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

    root = tmp_path / "store"
    first = enqueue_session_message(root, host="opencode", session_id=SESSION, message="same exact feedback")
    claimed = claim_session_messages(root, host="opencode", session_id=SESSION)
    assert [item.id for item in claimed] == [first.id]

    retry = enqueue_session_message(root, host="opencode", session_id=SESSION, message="same exact feedback")
    assert retry.id == first.id
    assert claim_session_messages(root, host="opencode", session_id=SESSION) == ()


def test_session_inbox_concurrent_same_payload_enqueue_is_idempotent(tmp_path: Path) -> None:
    import threading

    from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

    root = tmp_path / "store"
    results: list[str] = []
    failures: list[BaseException] = []

    def write() -> None:
        try:
            results.append(
                enqueue_session_message(
                    root,
                    host="opencode",
                    session_id=SESSION,
                    message="one semantic payload",
                ).id
            )
        except BaseException as exc:  # pragma: no cover - asserted empty below
            failures.append(exc)

    threads = [threading.Thread(target=write) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(results) == 8
    assert len(set(results)) == 1
    claimed = claim_session_messages(root, host="opencode", session_id=SESSION)
    assert len(claimed) == 1


def test_session_inbox_enqueue_and_claim_race_delivers_semantic_message_once(tmp_path: Path) -> None:
    import threading
    import time

    from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

    root = tmp_path / "store"
    delivered: list[str] = []
    failures: list[BaseException] = []
    start = threading.Barrier(3)

    def producer() -> None:
        try:
            start.wait()
            for _ in range(100):
                enqueue_session_message(root, host="opencode", session_id=SESSION, message="one race-safe payload")
        except BaseException as exc:  # pragma: no cover - asserted empty below
            failures.append(exc)

    def consumer() -> None:
        try:
            start.wait()
            for _ in range(100):
                delivered.extend(item.id for item in claim_session_messages(root, host="opencode", session_id=SESSION))
                time.sleep(0)
        except BaseException as exc:  # pragma: no cover - asserted empty below
            failures.append(exc)

    threads = [threading.Thread(target=producer), threading.Thread(target=consumer)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()
    delivered.extend(item.id for item in claim_session_messages(root, host="opencode", session_id=SESSION))

    assert failures == []
    assert len(delivered) == 1
    assert len(set(delivered)) == 1
