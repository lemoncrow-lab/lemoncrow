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
    assert result.sent is True
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


@pytest.mark.parametrize(
    ("host", "expected"),
    (
        ("opencode", ["/usr/bin/opencode", "run", "--session"]),
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
    assert result.state == "sent"
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

    assert result.state == "sent"
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
        pid = 4242

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired(seen, timeout)

    def popen(args: list[str], **_kwargs: object):
        seen.extend(args)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = deliver_prompt_to_agent_session("codex", SESSION, tmp_path, "Inspect src/a.py:L3")

    assert result.state == "sent"
    assert "feedback" not in result.message.lower()
    assert "prompt" in result.message.lower()
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
        )
        self.updated: dict[str, object] = {}

    def get_annotation(self, annotation_id: str):
        return self.annotation if annotation_id == self.annotation.id else None

    def get_session(self, review_id: str):
        return self.review if review_id == self.annotation.review_id else None

    def list_deliveries(self, annotation_id: str):
        return (self.delivery,) if annotation_id == self.annotation.id else ()

    def list_annotation_versions(self, annotation_id: str):
        return (SimpleNamespace(version_number=1),) if annotation_id == self.annotation.id else ()

    def latest_revision(self, review_id: str):
        return SimpleNamespace(id="rrv-1") if review_id == self.annotation.review_id else None

    def update_annotation(self, annotation_id: str, history=None, /, **fields: object):
        assert annotation_id == self.annotation.id
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
